"""Aggregated dashboard health endpoint."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI

_SCHEMA = "hermes-health-v1"
_STATUS_RANK = {"healthy": 0, "degraded": 1, "offline": 2}
# OpenClaw (Mission Control, :3000) decommissioned 2026-06-01 — no longer probed
# or aggregated so the overall light is not pinned red by a system we shut down on
# purpose. _probe_openclaw_status() is kept (unused) for a clean revert.
_SUBSYSTEM_NAMES = ("gateway", "autoresearch", "kanban_db")


async def _run_blocking(callable_: Any) -> Any:
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return await loop.run_in_executor(executor, callable_)
    finally:
        executor.shutdown(wait=True)


def _latency_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _status_dict(
    status: str,
    detail: str,
    *,
    latency_ms: int | None = None,
    heartbeat_age_s: float | None = None,
    include_heartbeat_age: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "detail": detail}
    if latency_ms is not None:
        out["latency_ms"] = latency_ms
    if include_heartbeat_age or heartbeat_age_s is not None:
        out["heartbeat_age_s"] = heartbeat_age_s
    out["error"] = error
    return out


async def _probe_gateway_status() -> dict[str, Any]:
    """Probe gateway liveness without blocking the event loop.

    Same-host first: here the gateway and the dashboard share a machine, so a
    running gateway PID is authoritative liveness — this mirrors ``/api/status``
    and avoids the health light being pinned red just because the optional
    cross-container ``GATEWAY_HEALTH_URL`` env isn't set (it usually isn't on a
    single-host deploy). Falls back to the HTTP health probe for split deploys.
    """
    start = time.perf_counter()
    try:
        from hermes_cli.web_server import _probe_gateway_health, get_running_pid

        pid = await _run_blocking(get_running_pid)
        if pid is not None:
            return _status_dict("healthy", "gateway running", latency_ms=_latency_ms(start))
        alive, body = await _run_blocking(_probe_gateway_health)
        latency = _latency_ms(start)
    except Exception as exc:
        return _status_dict(
            "offline",
            "gateway probe failed",
            latency_ms=_latency_ms(start),
            error=str(exc),
        )

    if alive:
        return _status_dict("healthy", "gateway responding", latency_ms=latency)
    error = None
    if isinstance(body, dict):
        raw_error = body.get("error") or body.get("detail")
        error = str(raw_error) if raw_error is not None else None
    return _status_dict(
        "offline",
        "gateway not responding",
        latency_ms=latency,
        error=error or "no response",
    )


async def _probe_openclaw_status() -> dict[str, Any]:
    """Probe Mission Control through the existing OpenClaw read helper."""
    start = time.perf_counter()
    try:
        from hermes_cli.openclaw_view import read_openclaw_agents

        result = await read_openclaw_agents()
        latency = _latency_ms(start)
    except Exception as exc:
        return _status_dict(
            "offline",
            "openclaw probe failed",
            latency_ms=_latency_ms(start),
            error=str(exc),
        )

    error = result.get("error") if isinstance(result, dict) else "non-object response"
    if error:
        return _status_dict(
            "offline",
            "Mission Control error",
            latency_ms=latency,
            error=str(error),
        )

    if latency >= 3000:
        return _status_dict("degraded", "slow response", latency_ms=latency)
    return _status_dict("healthy", "agents retrieved", latency_ms=latency)


async def _probe_autoresearch_status() -> dict[str, Any]:
    """Probe autoresearch runner state from its local status files."""
    try:
        from hermes_cli.autoresearch_view import read_runner_status

        runner_status = read_runner_status()
    except Exception as exc:
        return _status_dict(
            "offline",
            "failed to read status",
            heartbeat_age_s=None,
            include_heartbeat_age=True,
            error=str(exc),
        )

    state = str(runner_status.get("state") or "unknown")
    heartbeat_fresh = bool(runner_status.get("heartbeat_fresh"))
    heartbeat_age_s = runner_status.get("heartbeat_age_s")

    if state == "crashed":
        status = "offline"
    elif state == "idle" or (state in {"running", "stopping"} and heartbeat_fresh):
        status = "healthy"
    elif state in {"running", "stopping"}:
        status = "degraded"
    else:
        status = "degraded"

    return _status_dict(
        status,
        state,
        heartbeat_age_s=heartbeat_age_s,
        include_heartbeat_age=True,
        error=None,
    )


def _probe_kanban_db_sync() -> dict[str, Any]:
    start = time.perf_counter()
    conn: sqlite3.Connection | None = None
    try:
        from hermes_cli.kanban_db import kanban_db_path

        path = Path(kanban_db_path())
        if not path.exists():
            return _status_dict(
                "offline",
                "database file missing",
                latency_ms=_latency_ms(start),
                error=f"not found: {path}",
            )

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        latency = _latency_ms(start)
    except Exception as exc:
        return _status_dict(
            "offline",
            "query failed",
            latency_ms=_latency_ms(start),
            error=str(exc),
        )
    finally:
        if conn is not None:
            conn.close()

    if latency < 500:
        return _status_dict("healthy", "database healthy", latency_ms=latency)
    return _status_dict("degraded", "slow query", latency_ms=latency)


async def _probe_kanban_db_status() -> dict[str, Any]:
    return await _run_blocking(_probe_kanban_db_sync)


def _offline_from_exception(name: str, exc: BaseException) -> dict[str, Any]:
    if name == "autoresearch":
        return _status_dict(
            "offline",
            "probe failed",
            heartbeat_age_s=None,
            include_heartbeat_age=True,
            error=str(exc),
        )
    return _status_dict("offline", "probe failed", latency_ms=0, error=str(exc))


def _overall(subsystems: dict[str, dict[str, Any]]) -> str:
    worst_rank = max(
        _STATUS_RANK.get(subsystems[name].get("status"), _STATUS_RANK["offline"])
        for name in _SUBSYSTEM_NAMES
    )
    for status, rank in _STATUS_RANK.items():
        if rank == worst_rank:
            return status
    return "offline"


async def _get_health_status() -> dict[str, Any]:
    results = await asyncio.gather(
        _probe_gateway_status(),
        _probe_autoresearch_status(),
        _probe_kanban_db_status(),
        return_exceptions=True,
    )

    subsystems: dict[str, dict[str, Any]] = {}
    for name, result in zip(_SUBSYSTEM_NAMES, results):
        if isinstance(result, BaseException):
            subsystems[name] = _offline_from_exception(name, result)
        else:
            subsystems[name] = result

    return {
        "schema": _SCHEMA,
        "checked_at": int(time.time()),
        "overall": _overall(subsystems),
        "subsystems": subsystems,
    }


def register_health_status_routes(app: FastAPI) -> None:
    """Register the aggregated health endpoint before the SPA catch-all."""

    @app.get("/api/health-status")
    async def health_status() -> dict[str, Any]:
        return await _get_health_status()

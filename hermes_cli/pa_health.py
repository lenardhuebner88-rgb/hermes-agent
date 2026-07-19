"""Honest, read-only self-health for the Personal Assistant.

The v1 thresholds are intentionally explicit and conservative:

* the latest 20 terminal PA turns degrade at an error rate of 20 percent;
* Kanban event silence degrades after 2 hours;
* receipt silence degrades after 24 hours.

Every source is read independently.  SQLite databases are opened with
``mode=ro`` and the receipt tree is only stat'ed.  The route always returns a
diagnostic payload with HTTP 200 so the UI can explain degradation instead of
hiding it behind a generic request failure.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI

from hermes_constants import get_hermes_home

ENGINE_SAMPLE_SIZE = 20
ENGINE_ERROR_RATE_THRESHOLD = 0.20
KANBAN_STALE_AFTER_SECONDS = 2 * 60 * 60
RECEIPT_STALE_AFTER_SECONDS = 24 * 60 * 60
SQLITE_BUSY_TIMEOUT_SECONDS = 2.0

WATCHER_STATUS = "not_deployed"
PUSH_STATUS = "not_deployed"

_log = logging.getLogger(__name__)


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(
        f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
    )
    return conn


def _pa_db_path() -> Path:
    return get_hermes_home() / "pa" / "pa.db"


def _kanban_db_path() -> Path:
    # Import only for canonical path resolution.  The returned database is
    # opened separately in immutable read-only mode above.
    from hermes_cli import kanban_db

    return kanban_db.kanban_db_path()


def _receipts_root() -> Path:
    configured = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    vault = Path(configured).expanduser() if configured else Path("/home/piet/vault")
    return vault / "03-Agents"


def _collect_engine_health() -> dict[str, Any]:
    conn = _open_sqlite_readonly(_pa_db_path())
    try:
        rows = conn.execute(
            "SELECT status, error, ts, updated_ts FROM pa_turns "
            "WHERE status IN ('done','error') "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (ENGINE_SAMPLE_SIZE,),
        ).fetchall()
        last_error = conn.execute(
            "SELECT error, updated_ts, ts FROM pa_turns "
            "WHERE status='error' ORDER BY updated_ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    errors = sum(1 for row in rows if row["status"] == "error")
    sample_count = len(rows)
    error_rate = errors / sample_count if sample_count else 0.0
    degraded = sample_count > 0 and error_rate >= ENGINE_ERROR_RATE_THRESHOLD
    latest_error = None
    if last_error is not None:
        latest_error = {
            "text": str(last_error["error"] or "Unbekannter Engine-Fehler")[:1000],
            "ts": int(last_error["updated_ts"] or last_error["ts"]),
        }
    result: dict[str, Any] = {
        "status": "degraded" if degraded else "healthy",
        "sample_size": sample_count,
        "sample_limit": ENGINE_SAMPLE_SIZE,
        "error_count": errors,
        "error_rate": round(error_rate, 4),
        "error_rate_threshold": ENGINE_ERROR_RATE_THRESHOLD,
        "last_error": latest_error,
    }
    if degraded:
        result["reason"] = (
            f"Engine-Fehlerrate {error_rate:.0%} erreicht den "
            f"Schwellwert {ENGINE_ERROR_RATE_THRESHOLD:.0%}"
        )
    return result


def _collect_latest_kanban_event() -> int | None:
    conn = _open_sqlite_readonly(_kanban_db_path())
    try:
        row = conn.execute("SELECT MAX(created_at) AS latest_ts FROM task_events").fetchone()
    finally:
        conn.close()
    if row is None or row["latest_ts"] is None:
        return None
    return int(row["latest_ts"])


def _collect_latest_receipt() -> int | None:
    root = _receipts_root()
    if not root.is_dir():
        raise FileNotFoundError(root)
    latest: int | None = None
    for path in root.glob("*/receipts/**/*.md"):
        if path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        timestamp = int(stat.st_mtime)
        if latest is None or timestamp > latest:
            latest = timestamp
    return latest


def _freshness_check(
    *,
    latest_ts: int | None,
    now: int,
    stale_after_seconds: int,
    noun: str,
) -> dict[str, Any]:
    age_seconds = None if latest_ts is None else max(0, now - latest_ts)
    degraded = latest_ts is None or age_seconds > stale_after_seconds
    result: dict[str, Any] = {
        "status": "degraded" if degraded else "healthy",
        "latest_ts": latest_ts,
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
    }
    if degraded:
        if latest_ts is None:
            result["reason"] = f"Kein {noun} gefunden"
        else:
            result["reason"] = (
                f"Seit {age_seconds} Sekunden kein neuer {noun}; "
                f"Schwellwert {stale_after_seconds} Sekunden"
            )
    return result


def _failed_check(exc: BaseException) -> dict[str, Any]:
    detail = str(exc).strip() or exc.__class__.__name__
    return {
        "status": "degraded",
        "reason": f"Check nicht lesbar: {detail[:500]}",
        "source_error": detail[:500],
    }


def _safe_collect(collector: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return collector()
    except Exception as exc:
        _log.warning("PA health source failed: %s", exc)
        return _failed_check(exc)


def build_pa_health(*, now: int | None = None) -> dict[str, Any]:
    """Build one independently fault-tolerant PA health snapshot."""
    generated_at = int(time.time()) if now is None else int(now)
    checks: dict[str, Any] = {
        "engine": _safe_collect(_collect_engine_health),
        "kanban_events": _safe_collect(
            lambda: _freshness_check(
                latest_ts=_collect_latest_kanban_event(),
                now=generated_at,
                stale_after_seconds=KANBAN_STALE_AFTER_SECONDS,
                noun="Kanban-Event",
            )
        ),
        "receipts": _safe_collect(
            lambda: _freshness_check(
                latest_ts=_collect_latest_receipt(),
                now=generated_at,
                stale_after_seconds=RECEIPT_STALE_AFTER_SECONDS,
                noun="Receipt",
            )
        ),
        "watcher": WATCHER_STATUS,
        "push": PUSH_STATUS,
    }

    degraded: list[dict[str, Any]] = []
    for name in ("engine", "kanban_events", "receipts"):
        check = checks[name]
        if check.get("status") == "degraded":
            degraded.append(
                {
                    "check": name,
                    "reason": str(check.get("reason") or "Check degradiert"),
                    "since_ts": check.get("latest_ts")
                    or (check.get("last_error") or {}).get("ts"),
                }
            )
    degraded.extend(
        (
            {
                "check": "watcher",
                "reason": "PA-Wächter ist noch nicht deployed (S3.1)",
                "since_ts": None,
            },
            {
                "check": "push",
                "reason": "PA-Push ist noch nicht deployed (S3.2)",
                "since_ts": None,
            },
        )
    )
    return {
        "ok": not degraded,
        "degraded": degraded,
        "checks": checks,
        "generated_at": generated_at,
    }


def _emergency_health(exc: BaseException) -> dict[str, Any]:
    generated_at = int(time.time())
    detail = str(exc).strip() or exc.__class__.__name__
    reason = f"Selbstcheck ausgefallen: {detail[:500]}"
    return {
        "ok": False,
        "degraded": [
            {"check": "self_check", "reason": reason, "since_ts": generated_at}
        ],
        "checks": {
            "self_check": {
                "status": "degraded",
                "reason": reason,
                "source_error": detail[:500],
            },
            "watcher": WATCHER_STATUS,
            "push": PUSH_STATUS,
        },
        "generated_at": generated_at,
    }


def register_pa_health_routes(app: FastAPI) -> None:
    @app.get("/api/pa/health")
    async def pa_health() -> dict[str, Any]:
        from hermes_cli.pa_chat import _run_sync

        try:
            return await _run_sync(build_pa_health)
        except Exception as exc:  # Last-resort contract: diagnostics, never HTTP 500.
            _log.exception("PA health snapshot failed catastrophically")
            return _emergency_health(exc)

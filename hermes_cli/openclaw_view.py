"""Read-only OpenClaw bridge for Hermes Control.

The Hermes dashboard serves the operator UI on :9119, while Mission Control
keeps the live OpenClaw fleet state on :3000. This module exposes a single
read-only proxy route so the browser never reaches across origins directly.

B2: async httpx.AsyncClient so the FastAPI event-loop is never blocked by the
MC call (replacing the earlier sync httpx.get).

Sprint E4 (MC ``/agents`` parity): Mission Control speaks ISO-8601 timestamps
(``"2026-05-29T21:49:45.940Z"``) and a numeric ``throughput``/``loadCount``,
but the Control SPA's contract is epoch-seconds + a ``"N/h"`` string. Left raw,
``z.coerce.number()`` turns every ISO heartbeat into ``NaN → 0``, so heartbeat
age and the stuck-signal it feeds read as "ancient" for the whole fleet. We
normalise here — in the read-only proxy, defensively — so the UI gets honest
heartbeat ages, the per-metric ``truth`` MC already computes, and the load
count, i.e. everything an operator opens MC ``/agents`` for today.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi.responses import JSONResponse

_MISSION_CONTROL_AGENTS_URL = "http://127.0.0.1:3000/api/agents/live"
_MISSION_CONTROL_SEND_URL = "http://127.0.0.1:3000/api/discord/send"
_READ_HEADERS = {"x-actor-kind": "service", "x-request-class": "read"}
_WRITE_HEADERS = {"x-actor-kind": "service", "x-request-class": "write"}
_TIMEOUT_SECONDS = 2.5
# MC's live-agent payload carries the full recentDone history and routinely takes
# ~2.8s to assemble — above the 2.5s write/ping budget. A read timeout that tight
# made every other 5s poll trip, blanking the OpenClaw tab with an EMPTY error
# string (httpx timeout → str(exc) == "") so nothing surfaced. Give the read its
# own, roomier budget; the stale-retain guard on the frontend covers the rest.
_READ_TIMEOUT_SECONDS = 6.0

_PRIORITY_MAP = {"high": "high", "medium": "med", "med": "med", "low": "low"}


def _empty_error_response(error: str) -> dict[str, Any]:
    return {"agents": [], "updatedAt": None, "error": error}


def _ping_error_response(error: str) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "detail": error})


def _reachability_ping_message(agent_id: str) -> str:
    safe_agent_id = "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in str(agent_id)
    )[:80] or "unknown"
    return f"Reachability ping for OpenClaw agent '{safe_agent_id}'."


def _mission_control_write_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)


def _iso_to_epoch(value: Any) -> Any:
    """ISO-8601 (with trailing ``Z``) → epoch seconds (int). Numbers pass
    through untouched; anything unparseable is returned as-is so the frontend
    schema's ``.catch`` still owns the final coercion."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return value
    return value


def _throughput_str(value: Any) -> Any:
    """MC sends throughput as a number (tasks/hour); the card expects ``"N/h"``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return f"{int(value)}/h"
    return value


def _normalize_task(task: Any) -> Any:
    if not isinstance(task, dict):
        return task
    out = dict(task)
    if "priority" in out:
        out["priority"] = _PRIORITY_MAP.get(str(out.get("priority")).lower(), "med")
    return out


def _normalize_tasks(tasks: Any) -> Any:
    if not isinstance(tasks, dict):
        return tasks
    out = dict(tasks)
    for bucket in ("queued", "active", "review", "recentDone"):
        if isinstance(out.get(bucket), list):
            out[bucket] = [_normalize_task(t) for t in out[bucket]]
    return out


def _normalize_agent(agent: Any) -> Any:
    """Best-effort: copy the MC agent verbatim, then convert ONLY the fields the
    Control contract needs in a different shape. Unknown/absent fields are left
    alone so future MC additions survive untouched. Never raises."""
    if not isinstance(agent, dict):
        return agent
    out = dict(agent)
    if "lastActive" in out:
        out["lastActive"] = _iso_to_epoch(out.get("lastActive"))

    fleet = out.get("fleetHealth")
    if isinstance(fleet, dict):
        fleet = dict(fleet)
        if "heartbeat" in fleet:
            fleet["heartbeat"] = _iso_to_epoch(fleet.get("heartbeat"))
        if "throughput" in fleet:
            fleet["throughput"] = _throughput_str(fleet.get("throughput"))
        # MC computes a per-metric provenance map (live/derived/fallback/
        # unavailable). Surface each so the card can flag a guessed value instead
        # of presenting it as ground truth (E4: heartbeat → F1: all four metrics).
        truth = fleet.get("truth")
        if isinstance(truth, dict):
            for _metric in ("heartbeat", "throughput", "currentTool", "currentTask"):
                if truth.get(_metric):
                    out[f"{_metric}Truth"] = truth.get(_metric)
        out["fleetHealth"] = fleet

    if "tasks" in out:
        out["tasks"] = _normalize_tasks(out.get("tasks"))

    # MC's loadCount → a plain queue-depth number for the card.
    if isinstance(out.get("loadCount"), (int, float)) and not isinstance(out.get("loadCount"), bool):
        out["load"] = int(out["loadCount"])
    return out


def _normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if "updatedAt" in out:
        out["updatedAt"] = _iso_to_epoch(out.get("updatedAt"))
    agents = out.get("agents")
    if isinstance(agents, list):
        normalized: list[Any] = []
        for agent in agents:
            try:
                normalized.append(_normalize_agent(agent))
            except Exception:  # one bad agent must never sink the whole fleet view
                normalized.append(agent)
        out["agents"] = normalized
    return out


async def read_openclaw_agents() -> dict[str, Any]:
    """Fetch the Mission Control live-agent payload without mutating MC state,
    normalised into the Control SPA contract (epoch seconds, ``N/h`` strings).
    Uses async httpx.AsyncClient to avoid blocking the FastAPI event-loop (B2)."""
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT_SECONDS) as client:
            response = await client.get(
                _MISSION_CONTROL_AGENTS_URL,
                headers=_READ_HEADERS,
            )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        # httpx timeout exceptions stringify to "" — never return a blank error,
        # or the UI can't tell "MC slow" from "MC reports zero agents".
        return _empty_error_response(
            str(exc) or f"Mission-Control-Timeout (>{_READ_TIMEOUT_SECONDS:g}s)"
        )

    if not isinstance(data, dict):
        return _empty_error_response("Mission Control returned a non-object response")
    agents = data.get("agents")
    if not isinstance(agents, list):
        return _empty_error_response("Mission Control response is missing agents[]")
    return _normalize_payload(data)


async def ping_openclaw_agent(agent_id: str) -> Any:
    """Ask Mission Control to send a fixed reachability ping for one agent.

    Hermes deliberately builds the only outbound message server-side. Client
    request bodies are ignored so arbitrary text, channel IDs, or tokens cannot
    be smuggled through this proxy.
    """
    payload = {"message": _reachability_ping_message(agent_id)}
    try:
        async with _mission_control_write_client() as client:
            response = await client.post(
                _MISSION_CONTROL_SEND_URL,
                headers=_WRITE_HEADERS,
                json=payload,
            )
        response.raise_for_status()
    except Exception as exc:
        return _ping_error_response(str(exc))

    return {"ok": True}


def register_openclaw_routes(app: Any) -> None:
    """Register OpenClaw API routes before the SPA catch-all."""

    @app.get("/api/openclaw/agents")
    async def openclaw_agents() -> dict[str, Any]:
        return await read_openclaw_agents()

    @app.post("/api/openclaw/agents/{agent_id}/ping")
    async def openclaw_agent_ping(agent_id: str) -> Any:
        return await ping_openclaw_agent(agent_id)

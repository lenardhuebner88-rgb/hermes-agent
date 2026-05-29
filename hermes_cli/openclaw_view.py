"""Read-only OpenClaw bridge for Hermes Control.

The Hermes dashboard serves the operator UI on :9119, while Mission Control
keeps the live OpenClaw fleet state on :3000. This module exposes a single
read-only proxy route so the browser never reaches across origins directly.
"""
from __future__ import annotations

from typing import Any

import httpx

_MISSION_CONTROL_AGENTS_URL = "http://127.0.0.1:3000/api/agents/live"
_READ_HEADERS = {"x-actor-kind": "service", "x-request-class": "read"}
_TIMEOUT_SECONDS = 2.5


def _empty_error_response(error: str) -> dict[str, Any]:
    return {"agents": [], "updatedAt": None, "error": error}


def read_openclaw_agents() -> dict[str, Any]:
    """Fetch the Mission Control live-agent payload without mutating MC state."""
    try:
        response = httpx.get(
            _MISSION_CONTROL_AGENTS_URL,
            headers=_READ_HEADERS,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return _empty_error_response(str(exc))

    if not isinstance(data, dict):
        return _empty_error_response("Mission Control returned a non-object response")
    agents = data.get("agents")
    if not isinstance(agents, list):
        return _empty_error_response("Mission Control response is missing agents[]")
    return data


def register_openclaw_routes(app: Any) -> None:
    """Register the read-only OpenClaw API route before the SPA catch-all."""

    @app.get("/api/openclaw/agents")
    async def openclaw_agents() -> dict[str, Any]:
        return read_openclaw_agents()

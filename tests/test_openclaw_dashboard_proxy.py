"""Tests for the read-only OpenClaw proxy (hermes_cli.openclaw_view).

B2: read_openclaw_agents is now async (httpx.AsyncClient); these tests stub the
async context-manager so no real network calls are made. FastAPI TestClient runs
the full ASGI event-loop, so async route handlers work fine end-to-end.

E4: normalization helpers (_iso_to_epoch, _normalize_agent, _normalize_payload)
are exercised as pure-unit tests (no HTTP layer involved) and via the full HTTP
route to confirm the pipeline is wired together.
"""
from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers: fake async httpx.AsyncClient context-manager
# ---------------------------------------------------------------------------
def _make_async_client_mock(monkeypatch, payload, *, raise_exc=None):
    """Patch httpx.AsyncClient so __aenter__ returns a fake client whose
    get() is an async coroutine returning a fake Response."""
    view = importlib.import_module("hermes_cli.openclaw_view")

    class FakeResponse:
        def raise_for_status(self):
            if raise_exc:
                raise raise_exc
        def json(self):
            return payload

    fake_get = AsyncMock(return_value=FakeResponse())

    class FakeClient:
        async def get(self, url, **kwargs):
            fake_get.url = url
            fake_get.kwargs = kwargs
            return FakeResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view.httpx, "AsyncClient", lambda **_kw: FakeClient())
    return fake_get


def _make_async_client_error(monkeypatch, exc):
    """Patch httpx.AsyncClient so get() raises exc."""
    view = importlib.import_module("hermes_cli.openclaw_view")

    class FakeClientRaises:
        async def get(self, *_args, **_kwargs):
            raise exc
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view.httpx, "AsyncClient", lambda **_kw: FakeClientRaises())


# ---------------------------------------------------------------------------
# B2 + E4 via HTTP route
# ---------------------------------------------------------------------------
def test_openclaw_agents_proxy_forwards_readonly_headers(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")
    payload = {"agents": [{"id": "main", "name": "Atlas"}], "updatedAt": 1780041720}

    class FakeClient:
        captured = {}

        async def get(self, url, **kwargs):
            FakeClient.captured["url"] = url
            FakeClient.captured["kwargs"] = kwargs

            class Resp:
                def raise_for_status(self):
                    pass
                def json(self):
                    return payload

            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view.httpx, "AsyncClient", lambda timeout: FakeClient())
    app = FastAPI()
    view.register_openclaw_routes(app)
    body = TestClient(app).get("/api/openclaw/agents").json()

    # numeric updatedAt passes through unchanged (already epoch); agents too
    assert body["agents"] == payload["agents"]
    assert body["updatedAt"] == 1780041720
    assert FakeClient.captured["url"] == "http://127.0.0.1:3000/api/agents/live"
    assert FakeClient.captured["kwargs"]["headers"] == {
        "x-actor-kind": "service", "x-request-class": "read",
    }


def test_openclaw_agents_proxy_degrades_when_mission_control_is_down(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")

    class FakeClientError:
        async def get(self, *_args, **_kwargs):
            raise view.httpx.ConnectError("connection refused")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view.httpx, "AsyncClient", lambda **_kw: FakeClientError())
    app = FastAPI()
    view.register_openclaw_routes(app)
    response = TestClient(app).get("/api/openclaw/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["agents"] == []
    assert body["updatedAt"] is None
    assert "connection refused" in body["error"]


def test_proxy_normalizes_iso_timestamps_via_http_route(monkeypatch):
    """E4: end-to-end through the HTTP route — ISO timestamps become epoch ints."""
    view = importlib.import_module("hermes_cli.openclaw_view")
    payload = {
        "agents": [{"id": "main", "name": "Atlas",
                    "fleetHealth": {"heartbeat": "2026-05-29T21:49:45.940Z", "throughput": 2}}],
        "updatedAt": "2026-05-29T21:49:45.940Z",
    }

    class FakeClient:
        async def get(self, *_a, **_k):
            class Resp:
                def raise_for_status(self):
                    pass
                def json(self):
                    return payload
            return Resp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view.httpx, "AsyncClient", lambda **_kw: FakeClient())
    app = FastAPI()
    view.register_openclaw_routes(app)
    body = TestClient(app).get("/api/openclaw/agents").json()

    assert isinstance(body["updatedAt"], int) and body["updatedAt"] > 1_700_000_000
    assert isinstance(body["agents"][0]["fleetHealth"]["heartbeat"], int)
    assert body["agents"][0]["fleetHealth"]["throughput"] == "2/h"


# ---------------------------------------------------------------------------
# E4 normalisation helpers — pure unit tests (no HTTP layer)
# ---------------------------------------------------------------------------
def test_normalize_agent_converts_iso_and_throughput():
    view = importlib.import_module("hermes_cli.openclaw_view")
    agent = {
        "id": "main", "name": "Atlas", "status": "monitoring",
        "lastActive": "2026-05-29T21:49:45.940Z",
        "loadCount": 3,
        "tasks": {"queued": [{"id": "t1", "title": "x", "priority": "medium"}],
                  "active": [], "review": [], "recentDone": []},
        "fleetHealth": {
            "heartbeat": "2026-05-29T21:49:45.940Z",
            "throughput": 5,
            "truth": {"heartbeat": "fallback"},
        },
    }
    out = view._normalize_agent(agent)
    assert isinstance(out["lastActive"], int) and out["lastActive"] > 1_700_000_000
    assert isinstance(out["fleetHealth"]["heartbeat"], int)
    assert out["fleetHealth"]["throughput"] == "5/h"
    assert out["heartbeatTruth"] == "fallback"
    assert out["load"] == 3
    assert out["tasks"]["queued"][0]["priority"] == "med"
    assert agent["lastActive"] == "2026-05-29T21:49:45.940Z"  # input not mutated


def test_normalize_agent_is_defensive_on_missing_fields():
    view = importlib.import_module("hermes_cli.openclaw_view")
    assert view._normalize_agent({"id": "main", "name": "Atlas"}) == {"id": "main", "name": "Atlas"}
    assert view._normalize_agent("nope") == "nope"


def test_iso_to_epoch_passthrough_and_parse():
    view = importlib.import_module("hermes_cli.openclaw_view")
    assert view._iso_to_epoch(1780041720) == 1780041720
    assert view._iso_to_epoch("2026-05-29T21:49:45.940Z") > 1_700_000_000
    assert view._iso_to_epoch("not-a-date") == "not-a-date"
    assert view._iso_to_epoch(None) is None

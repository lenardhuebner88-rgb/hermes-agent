from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_openclaw_agents_proxy_forwards_readonly_headers(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")
    captured: dict[str, object] = {}
    payload = {"agents": [{"id": "main", "name": "Atlas"}], "updatedAt": 1780041720}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    def fake_get(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(view.httpx, "get", fake_get)
    app = FastAPI()
    view.register_openclaw_routes(app)

    body = TestClient(app).get("/api/openclaw/agents").json()

    assert body == payload
    assert captured["url"] == "http://127.0.0.1:3000/api/agents/live"
    assert captured["kwargs"] == {
        "headers": {"x-actor-kind": "service", "x-request-class": "read"},
        "timeout": 2.5,
    }


def test_openclaw_agents_proxy_degrades_when_mission_control_is_down(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")

    def fake_get(*_args, **_kwargs):
        raise view.httpx.ConnectError("connection refused")

    monkeypatch.setattr(view.httpx, "get", fake_get)
    app = FastAPI()
    view.register_openclaw_routes(app)

    response = TestClient(app).get("/api/openclaw/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["agents"] == []
    assert body["updatedAt"] is None
    assert "connection refused" in body["error"]


# --------------------------------------------------------------------------
# Sprint E4 — MC /agents parity: normalise ISO timestamps + numeric throughput
# into the Control SPA contract (epoch seconds, "N/h"), surface truth + load.
# --------------------------------------------------------------------------
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
    # original ISO untouched-source: we never mutate the input dict in place
    assert agent["lastActive"] == "2026-05-29T21:49:45.940Z"


def test_normalize_agent_is_defensive_on_missing_fields():
    view = importlib.import_module("hermes_cli.openclaw_view")
    # a minimal agent (no timestamps / fleetHealth) must pass through unchanged
    assert view._normalize_agent({"id": "main", "name": "Atlas"}) == {"id": "main", "name": "Atlas"}
    # non-dict is returned as-is
    assert view._normalize_agent("nope") == "nope"


def test_iso_to_epoch_passthrough_and_parse():
    view = importlib.import_module("hermes_cli.openclaw_view")
    assert view._iso_to_epoch(1780041720) == 1780041720
    assert view._iso_to_epoch("2026-05-29T21:49:45.940Z") > 1_700_000_000
    assert view._iso_to_epoch("not-a-date") == "not-a-date"
    assert view._iso_to_epoch(None) is None


def test_proxy_normalizes_live_payload(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")
    payload = {
        "agents": [{"id": "main", "name": "Atlas",
                    "fleetHealth": {"heartbeat": "2026-05-29T21:49:45.940Z", "throughput": 2}}],
        "updatedAt": "2026-05-29T21:49:45.940Z",
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(view.httpx, "get", lambda *_a, **_k: Response())
    app = FastAPI()
    view.register_openclaw_routes(app)
    body = TestClient(app).get("/api/openclaw/agents").json()
    assert isinstance(body["updatedAt"], int)
    assert isinstance(body["agents"][0]["fleetHealth"]["heartbeat"], int)
    assert body["agents"][0]["fleetHealth"]["throughput"] == "2/h"

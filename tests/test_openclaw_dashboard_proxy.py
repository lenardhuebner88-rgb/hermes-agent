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

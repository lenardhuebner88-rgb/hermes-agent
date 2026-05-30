"""Tests for the OpenClaw reachability ping proxy."""
from __future__ import annotations

import importlib

import httpx
import pytest
from fastapi import FastAPI


async def _post(app: FastAPI, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, **kwargs)


@pytest.mark.asyncio
async def test_openclaw_agent_ping_posts_fixed_message_to_mission_control(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")

    class FakeClient:
        captured = {}

        async def post(self, url, **kwargs):
            FakeClient.captured["url"] = url
            FakeClient.captured["kwargs"] = kwargs

            class Resp:
                def raise_for_status(self):
                    pass

            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view, "_mission_control_write_client", lambda: FakeClient())
    app = FastAPI()
    view.register_openclaw_routes(app)

    response = await _post(app, "/api/openclaw/agents/atlas/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert FakeClient.captured["url"] == "http://127.0.0.1:3000/api/discord/send"
    assert FakeClient.captured["kwargs"]["headers"] == {
        "x-actor-kind": "service",
        "x-request-class": "write",
    }
    assert FakeClient.captured["kwargs"]["json"] == {
        "message": "Reachability ping for OpenClaw agent 'atlas'.",
    }


@pytest.mark.asyncio
async def test_openclaw_agent_ping_returns_clean_error_when_mission_control_is_down(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")

    class FakeClientError:
        async def post(self, *_args, **_kwargs):
            raise TimeoutError("mission control timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view, "_mission_control_write_client", lambda: FakeClientError())
    app = FastAPI()
    view.register_openclaw_routes(app)

    response = await _post(app, "/api/openclaw/agents/atlas/ping")

    assert response.status_code == 502
    body = response.json()
    assert body["ok"] is False
    assert "mission control timeout" in body["detail"]


@pytest.mark.asyncio
async def test_openclaw_agent_ping_does_not_forward_free_client_payload(monkeypatch):
    view = importlib.import_module("hermes_cli.openclaw_view")

    class FakeClient:
        captured = {}

        async def post(self, _url, **kwargs):
            FakeClient.captured["json"] = kwargs["json"]

            class Resp:
                def raise_for_status(self):
                    pass

            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(view, "_mission_control_write_client", lambda: FakeClient())
    app = FastAPI()
    view.register_openclaw_routes(app)

    response = await _post(
        app,
        "/api/openclaw/agents/atlas/ping",
        json={
            "message": "client controlled text",
            "channel": "client-channel",
            "token": "client-token",
        },
    )

    assert response.status_code == 200
    outbound = FakeClient.captured["json"]
    assert outbound == {"message": "Reachability ping for OpenClaw agent 'atlas'."}
    assert "client controlled text" not in str(outbound)
    assert "client-channel" not in str(outbound)
    assert "client-token" not in str(outbound)

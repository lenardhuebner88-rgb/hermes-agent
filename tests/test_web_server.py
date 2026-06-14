"""Test that start_server configures ws-ping keepalive.

The server now uses uvicorn.Server directly (not uvicorn.run) so we stub
Config + Server + asyncio.run to capture kwargs without starting an event loop.
"""

import asyncio
import contextlib

import uvicorn

from hermes_cli import web_server


def _stub_uvicorn(monkeypatch):
    """Replace uvicorn.Config/Server with fakes so start_server returns
    immediately.  Returns a dict with captured Config kwargs."""
    captured: dict = {}

    class _FakeConfig:
        loaded = True
        host = "127.0.0.1"
        port = 8000

        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def load(self):
            pass

        class lifespan_class:
            should_exit = False
            state: dict = {}

            def __init__(self, *a, **kw):
                pass

            async def startup(self):
                pass

            async def shutdown(self):
                pass

    class _FakeServer:
        should_exit = False
        started = True
        servers: list = []
        lifespan = None

        @staticmethod
        def capture_signals():
            return contextlib.nullcontext()

        async def startup(self, sockets=None):
            pass

        async def main_loop(self):
            pass

        async def shutdown(self, sockets=None):
            pass

    monkeypatch.setattr(uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", lambda config: _FakeServer())
    return captured


def test_start_server_enables_ws_ping_for_half_open_detection(monkeypatch):
    """WS ping must be configured so half-open connections (reverse-proxy 524,
    dropped tunnels) raise WebSocketDisconnect into the reaping path (#32377)."""
    captured = _stub_uvicorn(monkeypatch)

    # Loopback bind => no auth gate, so this reaches the Config constructor.
    web_server.start_server(host="127.0.0.1", port=0, open_browser=False)

    assert captured["ws_ping_interval"] == 20.0
    assert captured["ws_ping_timeout"] == 20.0
    # Graceful-stop cap: open SPA WebSockets must not hold the stopping server
    # past the systemd unit's 10s budget (SIGKILL storms, 2026-06-11 journal).
    assert captured["timeout_graceful_shutdown"] == 5


def test_body_size_limit_rejects_oversized_requests_before_buffering(monkeypatch):
    """Oversized bodies must be refused from the Content-Length header alone —
    FastAPI would otherwise buffer the whole body in RAM before the handler's
    own size check runs (2026-06-11 memory-peak audit)."""
    import asyncio

    sent = []
    inner_called = {"v": False}

    async def inner_app(scope, receive, send):
        inner_called["v"] = True

    async def send(message):
        sent.append(message)

    mw = web_server._BodySizeLimitMiddleware(inner_app)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/files/upload",
        "headers": [(b"content-length", str(web_server._MAX_HTTP_BODY_BYTES + 1).encode())],
    }
    asyncio.run(mw(scope, None, send))
    assert not inner_called["v"], "oversized request reached the app (body would buffer)"
    assert sent[0]["status"] == 413

    # A normal-sized request passes through untouched.
    sent.clear()
    scope["headers"] = [(b"content-length", b"1024")]
    asyncio.run(mw(scope, None, send))
    assert inner_called["v"]
    assert sent == []

    # Chunked (no Content-Length) passes through.
    inner_called["v"] = False
    scope["headers"] = []
    asyncio.run(mw(scope, None, send))
    assert inner_called["v"]


def test_gzip_middleware_compresses_large_json(monkeypatch):
    """The SPA polls multi-hundred-KB JSON over slow Tailscale links; the
    server must honour Accept-Encoding: gzip (2026-06-11 perf audit C2).
    Small bodies stay uncompressed (minimum_size)."""
    from fastapi.testclient import TestClient

    app = web_server.app

    @app.get("/api/_test/gzip-large")
    def _gzip_large():
        return {"rows": ["x" * 100] * 200}  # ~20 KB JSON

    @app.get("/api/_test/gzip-small")
    def _gzip_small():
        return {"ok": True}

    # The SPA catch-all (/{full_path:path}) was registered at import time and
    # matches before late-added routes — move the test routes to the front.
    app.router.routes[:0] = [app.router.routes.pop(), app.router.routes.pop()]

    try:
        # base_url with a loopback host: an earlier test may have installed
        # the host-guard middleware, which rejects the default "testserver".
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = web_server._SESSION_TOKEN
            headers = {
                "X-Hermes-Session-Token": token,
                "Accept-Encoding": "gzip",
            }
            large = client.get("/api/_test/gzip-large", headers=headers)
            assert large.status_code == 200
            assert large.headers.get("content-encoding") == "gzip"
            assert large.json()["rows"][0] == "x" * 100  # transparently decoded

            small = client.get("/api/_test/gzip-small", headers=headers)
            assert small.status_code == 200
            assert small.headers.get("content-encoding") != "gzip"
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes
            if getattr(r, "path", "") not in ("/api/_test/gzip-large", "/api/_test/gzip-small")
        ]

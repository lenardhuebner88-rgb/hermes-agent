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
        _loop_factory = None

        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def load(self):
            pass

        def get_loop_factory(self):
            return self._loop_factory

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


def test_start_server_disables_ws_ping_on_loopback(monkeypatch):
    """Loopback binds (the Desktop case) MUST disable uvicorn's protocol-level
    keepalive ping so an event-loop stall can never trigger a false disconnect.

    uvicorn's ws ping runs on the same event loop as agent turns. A single
    synchronous GIL-holding call on a worker thread can starve that loop for
    minutes, so the loop can't process the pong and uvicorn kills an
    otherwise-healthy local connection (#53773 "event loop stalled 226.3s",
    #48445/#50005). On loopback there is no network/proxy path where a
    half-open connection can occur — a dead local client tears the socket down
    with a real FIN/RST that surfaces as WebSocketDisconnect regardless — so
    the ping provides no liveness value and only harms. Assert it is disabled.
    """
    captured = _stub_uvicorn(monkeypatch)

    # Loopback bind => no auth gate, so this reaches the Config constructor.
    web_server.start_server(host="127.0.0.1", port=0, open_browser=False)

    assert captured["ws_ping_interval"] is None
    assert captured["ws_ping_timeout"] is None


def test_start_server_enables_ws_ping_for_half_open_detection(monkeypatch):
    """Non-loopback (public) binds MUST keep the ws ping enabled so half-open
    connections (reverse-proxy 524, dropped Cloudflare Tunnel) raise
    WebSocketDisconnect into the reaping path (#32377).

    The invariant asserted here is that ping stays enabled (non-None, positive)
    and the timeout is never shorter than the interval — not a frozen literal,
    which churns every time the window is retuned. Loopback disables the ping
    (see test_start_server_disables_ws_ping_on_loopback); this covers the
    public-bind half-open case, so the auth gate is active here.
    """
    captured = _stub_uvicorn(monkeypatch)

    # Non-loopback bind so the _is_loopback branch selects the enabled-ping
    # window. Neutralize the auth gate so start_server reaches uvicorn.Config
    # without requiring a registered provider (a real public bind would raise
    # SystemExit here). The ping window keys off the host, not the auth flag.
    monkeypatch.setattr(web_server, "should_require_auth", lambda *a, **k: False)
    web_server.start_server(host="0.0.0.0", port=0, open_browser=False)

    assert captured["ws_ping_interval"] and captured["ws_ping_interval"] > 0
    assert captured["ws_ping_timeout"] and captured["ws_ping_timeout"] > 0
    assert captured["ws_ping_timeout"] >= captured["ws_ping_interval"]
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

    # Chunked (no Content-Length) within the limit passes through.
    # Finding #12 fix: chunked bodies are now body-counted, so a proper
    # async receive callable returning an empty body is required.
    inner_called["v"] = False

    async def empty_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope["headers"] = []
    asyncio.run(mw(scope, empty_receive, send))
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


def test_start_server_runs_on_uvicorns_loop_factory(monkeypatch):
    """The dashboard/desktop backend must serve uvicorn on the loop *uvicorn*
    selects, not the interpreter default.

    On Windows ``asyncio.run`` defaults to a ProactorEventLoop, but uvicorn's
    socket-serving stack forces a SelectorEventLoop on win32
    (``uvicorn/loops/asyncio.py``). Serving on the proactor loop binds a socket
    that never accepts — the backend prints "Skipping web UI build" and hangs
    forever with the port LISTENING but no TCP handshake (#50641). We fix that
    by routing the serve call through ``uvicorn._compat.asyncio_run`` with
    ``config.get_loop_factory()`` — exactly what ``uvicorn.Server.run`` does.

    This asserts the behavioral contract: on Windows the loop factory the runner
    receives is the one uvicorn's own Config produced, and bare ``asyncio.run``
    is never the serve path when the loop-factory runner exists.
    """
    _stub_uvicorn(monkeypatch)

    # The fix only changes behavior on win32; simulate it so the Windows branch
    # is actually exercised on a POSIX CI host.
    monkeypatch.setattr(web_server.sys, "platform", "win32")

    # The fake Config (installed by _stub_uvicorn) returns its ``_loop_factory``
    # from get_loop_factory(). Pin a sentinel so we can assert it is threaded
    # through to the runner unchanged.
    sentinel_factory = object()
    monkeypatch.setattr(uvicorn.Config, "_loop_factory", sentinel_factory, raising=False)

    seen: dict = {}

    def _fake_runner(coro, *, loop_factory=None):
        seen["loop_factory"] = loop_factory
        coro.close()  # drain without an event loop

    monkeypatch.setattr("uvicorn._compat.asyncio_run", _fake_runner, raising=False)

    # Bare asyncio.run must NOT be the serve path on Windows when the
    # loop-factory runner is importable.
    called_bare = {"hit": False}

    def _guard_asyncio_run(coro):
        called_bare["hit"] = True
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "run", _guard_asyncio_run)

    web_server.start_server(host="127.0.0.1", port=0, open_browser=False)

    assert seen.get("loop_factory") is sentinel_factory, (
        "start_server must pass uvicorn's get_loop_factory() result to the "
        "runner so Windows serves on a SelectorEventLoop"
    )
    assert called_bare["hit"] is False, (
        "start_server must not fall back to bare asyncio.run when uvicorn's "
        "loop-factory runner is available"
    )


def test_start_server_keeps_bare_asyncio_run_on_posix(monkeypatch):
    """POSIX behavior must be byte-for-byte unchanged: serve via the plain
    ``asyncio.run(_serve())`` path, never the Windows loop-factory branch.

    The #50641 fix is intentionally win32-scoped to keep the blast radius
    minimal — Python's default loop on POSIX is already a SelectorEventLoop
    (or uvloop), which is what uvicorn serves on, so there is nothing to fix.
    """
    _stub_uvicorn(monkeypatch)
    monkeypatch.setattr(web_server.sys, "platform", "linux")

    # If the Windows branch were taken, the loop-factory runner would fire.
    runner_called = {"hit": False}

    def _fake_runner(coro, *, loop_factory=None):
        runner_called["hit"] = True
        coro.close()

    monkeypatch.setattr("uvicorn._compat.asyncio_run", _fake_runner, raising=False)

    bare_called = {"hit": False}

    def _fake_asyncio_run(coro):
        bare_called["hit"] = True
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "run", _fake_asyncio_run)

    web_server.start_server(host="127.0.0.1", port=0, open_browser=False)

    assert bare_called["hit"] is True, "POSIX must serve via bare asyncio.run"
    assert runner_called["hit"] is False, (
        "POSIX must not take the Windows loop-factory branch"
    )

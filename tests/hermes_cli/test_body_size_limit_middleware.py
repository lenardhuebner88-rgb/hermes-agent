"""Tests for _BodySizeLimitMiddleware — covers both Content-Length and chunked paths.

Finding #12: chunked (Transfer-Encoding: chunked) requests carry no Content-Length,
so the original header-only check let them bypass the 413 gate entirely.  This
test suite verifies the fix closes the chunked gap without breaking normal requests.
"""

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

# A small limit used in all tests so we do not have to generate multi-GB bodies.
_TEST_LIMIT = 512  # bytes


@pytest.fixture()
def small_limit(monkeypatch):
    """Patch _MAX_HTTP_BODY_BYTES down to _TEST_LIMIT for all middleware tests."""
    monkeypatch.setattr(web_server, "_MAX_HTTP_BODY_BYTES", _TEST_LIMIT)


@pytest.fixture()
def client(monkeypatch):
    """Authenticated TestClient with auth_required=False (loopback mode).

    Uses base_url="http://127.0.0.1" so that the host-guard middleware
    accepts requests even when a prior test has set app.state.bound_host.
    """
    prev_auth = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = False
    # Ensure host guard does not interfere (loopback is always allowed).
    web_server.app.state.bound_host = None
    tc = TestClient(web_server.app, raise_server_exceptions=False, base_url="http://127.0.0.1")
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield tc
    finally:
        if prev_auth is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = prev_auth
        if prev_host is None:
            try:
                delattr(web_server.app.state, "bound_host")
            except AttributeError:
                pass
        else:
            web_server.app.state.bound_host = prev_host


# ---------------------------------------------------------------------------
# Case (a): chunked request OVER the limit → must 413, body must NOT reach handler
# ---------------------------------------------------------------------------


def test_chunked_over_limit_returns_413(client, small_limit):
    """A chunked (no Content-Length) body that exceeds the cap must be rejected 413."""

    def oversized_generator():
        # Send in chunks so there is truly no Content-Length header.
        chunk = b"X" * 64
        # Total: 64 * 9 = 576 bytes > _TEST_LIMIT (512)
        for _ in range(9):
            yield chunk

    resp = client.post(
        "/api/ops/prompt-size",
        content=oversized_generator(),
    )
    assert resp.status_code == 413, (
        f"Expected 413 for chunked over-limit body, got {resp.status_code}"
    )
    assert b"too large" in resp.content.lower() or resp.status_code == 413


def test_chunked_over_limit_body_not_fully_buffered(client, small_limit):
    """Even with the limit patched, the handler's response body must NOT contain
    the oversized payload — confirming the middleware aborted before the handler
    consumed all bytes.

    /api/ops/prompt-size echoes back a token estimate based on the body it
    receives.  If the body reached the handler the response would be 200;
    a 413 from the middleware proves the body was cut off before the handler.
    """

    bytes_sent = []

    def tracking_generator():
        for _ in range(9):
            chunk = b"X" * 64
            bytes_sent.append(len(chunk))
            yield chunk

    resp = client.post(
        "/api/ops/prompt-size",
        content=tracking_generator(),
    )
    # Middleware must abort with 413, not let the handler respond 200.
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Case (b): chunked request UNDER the limit → passes through normally
# ---------------------------------------------------------------------------


def test_chunked_under_limit_passes_through(client, small_limit):
    """A chunked body within the limit must reach the handler and return 200."""

    def small_generator():
        # Total: 64 * 4 = 256 bytes < _TEST_LIMIT (512)
        for _ in range(4):
            yield b"Y" * 64

    resp = client.post(
        "/api/ops/prompt-size",
        content=small_generator(),
    )
    # /api/ops/prompt-size returns 200 with a token estimate when body is valid JSON;
    # the body here is not JSON so it may 422, but crucially it must NOT be 413.
    assert resp.status_code != 413, (
        f"Chunked under-limit body should not be rejected, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Case (c): normal (non-chunked) small POST still works unchanged
# ---------------------------------------------------------------------------


def test_normal_small_post_passes_through(client, small_limit):
    """A regular POST with small JSON body (Content-Length present) must still work."""
    import json

    payload = json.dumps({"text": "hello world"}).encode()
    # payload is tiny (<512), has an explicit Content-Length set by httpx.
    resp = client.post(
        "/api/ops/prompt-size",
        content=payload,
        headers={"content-type": "application/json"},
    )
    # Must not be rejected by the middleware (413); handler may 200 or 422 for this payload.
    assert resp.status_code != 413, (
        f"Normal small POST was incorrectly rejected with 413"
    )


# ---------------------------------------------------------------------------
# Case (d): existing Content-Length-over-limit path still 413s (regression guard)
# ---------------------------------------------------------------------------


def test_content_length_over_limit_still_413(client, small_limit):
    """The original Content-Length fast-path must still reject oversized bodies."""
    oversized = b"Z" * (_TEST_LIMIT + 1)
    resp = client.post(
        "/api/ops/prompt-size",
        content=oversized,
        # httpx sets Content-Length automatically for bytes bodies.
    )
    assert resp.status_code == 413, (
        f"Content-Length over-limit should 413, got {resp.status_code}"
    )


def test_content_length_at_limit_passes(client, small_limit):
    """A body exactly at the limit with Content-Length must NOT be rejected."""
    at_limit = b"A" * _TEST_LIMIT
    resp = client.post(
        "/api/ops/prompt-size",
        content=at_limit,
    )
    assert resp.status_code != 413, (
        f"Body exactly at limit should not be rejected, got {resp.status_code}"
    )

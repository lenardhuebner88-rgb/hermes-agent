from __future__ import annotations

import json

import pytest

from scripts.smoke_health_status_auth import (
    SmokeError,
    _json_request,
    _session_token_from_html,
    _summary,
    _validate_health_payload,
)


class _Response:
    def __init__(self, payload: str, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload.encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Opener:
    def __init__(self, payload: str, status: int = 200) -> None:
        self._payload = payload
        self._status = status
        self.last_request = None

    def open(self, request, timeout=None) -> _Response:
        self.last_request = request
        return _Response(self._payload, self._status)


def test_session_token_from_real_spa_injection_shape() -> None:
    html = '<script>window.__HERMES_SESSION_TOKEN__="tok-123\\u0026safe";</script>'

    assert _session_token_from_html(html) == "tok-123&safe"


def test_session_token_parser_fails_closed_without_injection() -> None:
    with pytest.raises(SmokeError, match="session token"):
        _session_token_from_html("<html><body>authenticated but malformed</body></html>")


def test_json_request_accepts_exactly_the_2xx_window() -> None:
    """Only 200..299 may pass: the boundary 200 succeeds and the boundary
    300 raises — off-by-ones here would accept redirects or reject a
    healthy login."""
    ok = _json_request(_Opener('{"ok": true}', status=200), "GET", "https://h/x", timeout=1)
    assert ok == {"ok": True}
    with pytest.raises(SmokeError, match="HTTP 300"):
        _json_request(_Opener('{"ok": true}', status=300), "GET", "https://h/x", timeout=1)
    with pytest.raises(SmokeError, match="HTTP 199"):
        _json_request(_Opener('{"ok": true}', status=199), "GET", "https://h/x", timeout=1)


def test_json_request_serializes_payload_and_skips_absent_extra_headers() -> None:
    """A payload is sent as JSON bytes with the Content-Type header; an
    absent extra_headers mapping must not be merged at all."""
    opener = _Opener('{"ok": true}')
    _json_request(
        opener, "POST", "https://h/login", payload={"user": "u"}, timeout=1
    )
    request = opener.last_request
    assert json.loads(request.data.decode("utf-8")) == {"user": "u"}
    assert request.get_header("Content-type") == "application/json"

    bare = _Opener('{"ok": true}')
    _json_request(bare, "GET", "https://h/x", timeout=1)  # no extra_headers
    assert bare.last_request.data is None


def test_validate_health_payload_pins_schema_and_subsystems() -> None:
    """The payload contract: schema hermes-health-v1, a known overall
    status, and a non-empty subsystems dict — anything else fails closed."""
    good = {
        "schema": "hermes-health-v1",
        "overall": "healthy",
        "subsystems": {"gateway": {"status": "ok"}},
    }
    assert _validate_health_payload(good) is None
    with pytest.raises(SmokeError, match="schema"):
        _validate_health_payload({**good, "schema": "other"})
    with pytest.raises(SmokeError, match="subsystem"):
        _validate_health_payload({**good, "subsystems": {}})


def test_summary_annotates_only_the_dispatcher_heartbeat() -> None:
    """The heartbeat age annotation belongs to kanban_dispatcher alone —
    sprinkling (age_s=...) over every subsystem muddies the one-line
    smoke output."""
    payload = {
        "subsystems": {
            "gateway": {"status": "ok"},
            "kanban_dispatcher": {"status": "ok", "heartbeat_age_s": 5},
        }
    }
    line = _summary(payload)
    assert "gateway=ok" in line
    assert "kanban_dispatcher=ok(age_s=5)" in line
    assert "gateway=ok(age_s" not in line

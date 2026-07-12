from __future__ import annotations

import pytest

from scripts.smoke_health_status_auth import SmokeError, _session_token_from_html


def test_session_token_from_real_spa_injection_shape() -> None:
    html = '<script>window.__HERMES_SESSION_TOKEN__="tok-123\\u0026safe";</script>'

    assert _session_token_from_html(html) == "tok-123&safe"


def test_session_token_parser_fails_closed_without_injection() -> None:
    with pytest.raises(SmokeError, match="session token"):
        _session_token_from_html("<html><body>authenticated but malformed</body></html>")

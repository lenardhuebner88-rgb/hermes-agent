from datetime import datetime, timezone

import httpx

from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
    _fetch_xai_account_usage,
    _format_anthropic_plan,
    _resolve_anthropic_plan_label,
    fetch_account_usage,
    render_account_usage_lines,
)
from hermes_cli.auth import AuthError

# Real Grok CLI billing line captured 2026-07-16 (verbatim).
_XAI_BILLING_FIXTURE_LINE = (
    '{"ts":"2026-07-16T09:47:14.767Z","src":"shell","pid":3418557,"lvl":"info",'
    '"msg":"billing: fetched credits config","ctx":{"config":{"creditUsagePercent":21.0,'
    '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-12T17:58:33.973068+00:00",'
    '"end":"2026-07-19T17:58:33.973068+00:00"},"onDemandCap":{"val":0},"onDemandUsed":{"val":0},'
    '"prepaidBalance":{"val":0},"isUnifiedBillingUser":true,'
    '"billingPeriodStart":"2026-07-12T17:58:33.973068+00:00",'
    '"billingPeriodEnd":"2026-07-19T17:58:33.973068+00:00","historyLen":0},'
    '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
)

_XAI_BILLING_OLDER_LINE = (
    '{"ts":"2026-07-16T08:00:00.000Z","src":"shell","pid":1,"lvl":"info",'
    '"msg":"billing: fetched credits config","ctx":{"config":{"creditUsagePercent":32.0,'
    '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-12T17:58:33.973068+00:00",'
    '"end":"2026-07-19T17:58:33.973068+00:00"},"onDemandCap":{"val":0},"onDemandUsed":{"val":0},'
    '"prepaidBalance":{"val":0},"isUnifiedBillingUser":true},'
    '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        return _Response(self._payload)


class _RoutingClient:
    def __init__(self, payloads):
        self._payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        return _Response(self._payloads[url])


def test_fetch_account_usage_codex(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_codex_runtime_credentials",
        lambda refresh_if_expiring=True: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "access-token",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage._read_codex_tokens",
        lambda: {"tokens": {"account_id": "acct_123"}},
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 15,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": 40,
                        "reset_at": 1_900_500_000,
                        "limit_window_seconds": 604800,
                    },
                },
                "credits": {"has_credits": True, "balance": 12.5},
            }
        ),
    )

    snapshot = fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.plan == "Pro"
    assert len(snapshot.windows) == 2
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[0].used_percent == 15.0
    assert snapshot.windows[0].reset_at == datetime.fromtimestamp(1_900_000_000, tz=timezone.utc)
    assert "Credits balance: $12.50" in snapshot.details


def test_fetch_account_usage_anthropic_maps_windows(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_anthropic_token",
        lambda: "sk-ant-oat01-test-oauth-token",
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "five_hour": {
                    "utilization": 0.15,
                    "resets_at": "2026-07-16T18:00:00Z",
                },
                "seven_day": {
                    "utilization": 40,
                    "resets_at": "2026-07-20T00:00:00Z",
                },
                "seven_day_opus": {
                    "utilization": 55,
                    "resets_at": "2026-07-20T00:00:00Z",
                },
                "extra_usage": {
                    "is_enabled": True,
                    "used_credits": 1.5,
                    "monthly_limit": 10.0,
                    "currency": "USD",
                },
            }
        ),
    )

    snapshot = fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.provider == "anthropic"
    assert snapshot.source == "oauth_usage_api"
    assert snapshot.unavailable_reason is None
    assert len(snapshot.windows) == 3
    assert snapshot.windows[0].label == "Current session"
    assert snapshot.windows[0].used_percent == 15.0
    assert snapshot.windows[0].window_key == "session"
    assert snapshot.windows[1].label == "Current week"
    assert snapshot.windows[1].used_percent == 40.0
    assert snapshot.windows[1].window_key == "weekly"
    assert snapshot.windows[2].label == "Opus week"
    assert snapshot.windows[2].used_percent == 55.0
    assert snapshot.windows[2].window_key == "opus_week"
    assert "Extra usage: 1.50 / 10.00 USD" in snapshot.details


def test_fetch_account_usage_anthropic_missing_token(monkeypatch):
    monkeypatch.setattr("agent.account_usage.resolve_anthropic_token", lambda: "")

    snapshot = fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.provider == "anthropic"
    assert snapshot.source == "oauth_usage_api"
    assert snapshot.unavailable_reason is not None
    assert "not configured" in snapshot.unavailable_reason
    assert snapshot.windows == ()


def test_fetch_account_usage_anthropic_rejected_token_returns_unavailable(monkeypatch):
    fake_token = "sk-ant-oat01-secret-should-not-leak"
    monkeypatch.setattr(
        "agent.account_usage.resolve_anthropic_token",
        lambda: fake_token,
    )

    class _UnauthorizedClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None):
            class _UnauthorizedResponse:
                status_code = 401

                def raise_for_status(self):
                    raise Exception(f"Unauthorized token {fake_token}")

                def json(self):
                    return {"error": fake_token}

            return _UnauthorizedResponse()

    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _UnauthorizedClient(),
    )

    snapshot = fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.unavailable_reason is not None
    assert "rejected" in snapshot.unavailable_reason
    assert fake_token not in snapshot.unavailable_reason
    assert "secret-should-not-leak" not in snapshot.unavailable_reason


def test_fetch_account_usage_anthropic_unreachable_connect_error(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_anthropic_token",
        lambda: "sk-ant-oat01-test-oauth-token",
    )

    class _ConnectFailClient:
        def __enter__(self):
            raise httpx.ConnectError("connection refused")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _ConnectFailClient(),
    )

    snapshot = fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.unavailable_reason == (
        "Anthropic usage API unreachable (ConnectError)."
    )
    assert "connection refused" not in snapshot.unavailable_reason


def test_fetch_account_usage_codex_credentials_not_available(monkeypatch):
    def _boom(base_url=None, api_key=None):
        raise AuthError("no ChatGPT credentials for sk-secret-codex-token")

    monkeypatch.setattr(
        "agent.account_usage._resolve_codex_usage_credentials",
        _boom,
    )

    snapshot = fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.provider == "openai-codex"
    assert snapshot.source == "usage_api"
    assert snapshot.unavailable_reason == "Codex credentials not available."
    assert "sk-secret-codex-token" not in snapshot.unavailable_reason
    assert "ChatGPT credentials" not in snapshot.unavailable_reason


def test_fetch_account_usage_codex_rejected_token_returns_unavailable(monkeypatch):
    fake_token = "sk-codex-secret-should-not-leak"
    monkeypatch.setattr(
        "agent.account_usage._resolve_codex_usage_credentials",
        lambda base_url=None, api_key=None: (fake_token, "https://chatgpt.com/backend-api/codex", "acct"),
    )

    class _UnauthorizedClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None):
            class _UnauthorizedResponse:
                status_code = 401

                def raise_for_status(self):
                    raise Exception(f"Unauthorized {fake_token}")

                def json(self):
                    return {"error": fake_token}

            return _UnauthorizedResponse()

    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _UnauthorizedClient(),
    )

    snapshot = fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.unavailable_reason == "ChatGPT/Codex token rejected."
    assert fake_token not in snapshot.unavailable_reason


def test_render_account_usage_lines_includes_reset_and_provider():
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime.now(timezone.utc),
        plan="Pro",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=25,
                reset_at=datetime.now(timezone.utc),
            ),
        ),
        details=("Credits balance: $9.99",),
    )
    lines = render_account_usage_lines(snapshot)

    assert lines[0] == "📈 Account limits"
    assert "openai-codex (Pro)" in lines[1]
    assert "Session: 75% remaining (25% used)" in lines[2]
    assert "Credits balance: $9.99" in lines[3]


def test_fetch_account_usage_openrouter_uses_limit_remaining_and_ignores_deprecated_rate_limit(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_runtime_provider",
        lambda requested, explicit_base_url=None, explicit_api_key=None: {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-test",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=10.0: _RoutingClient(
            {
                "https://openrouter.ai/api/v1/credits": {
                    "data": {"total_credits": 300.0, "total_usage": 10.92}
                },
                "https://openrouter.ai/api/v1/key": {
                    "data": {
                        "limit": 100.0,
                        "limit_remaining": 70.0,
                        "limit_reset": "monthly",
                        "usage": 12.5,
                        "usage_daily": 0.5,
                        "usage_weekly": 2.0,
                        "usage_monthly": 8.0,
                        "rate_limit": {"requests": -1, "interval": "10s"},
                    }
                },
            }
        ),
    )

    snapshot = fetch_account_usage("openrouter")

    assert snapshot is not None
    assert snapshot.windows == (
        AccountUsageWindow(
            label="API key quota",
            used_percent=30.0,
            detail="$70.00 of $100.00 remaining • resets monthly",
        ),
    )
    assert "Credits balance: $289.08" in snapshot.details
    assert "API key usage: $12.50 total • $0.50 today • $2.00 this week • $8.00 this month" in snapshot.details
    assert all("-1 requests / 10s" not in line for line in render_account_usage_lines(snapshot))


def test_fetch_account_usage_openrouter_omits_quota_window_when_key_has_no_limit(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_runtime_provider",
        lambda requested, explicit_base_url=None, explicit_api_key=None: {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-test",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=10.0: _RoutingClient(
            {
                "https://openrouter.ai/api/v1/credits": {
                    "data": {"total_credits": 100.0, "total_usage": 25.5}
                },
                "https://openrouter.ai/api/v1/key": {
                    "data": {
                        "limit": None,
                        "limit_remaining": None,
                        "usage": 25.5,
                        "usage_daily": 1.25,
                        "usage_weekly": 4.5,
                        "usage_monthly": 18.0,
                    }
                },
            }
        ),
    )

    snapshot = fetch_account_usage("openrouter")

    assert snapshot is not None
    assert snapshot.windows == ()
    assert "Credits balance: $74.50" in snapshot.details
    assert "API key usage: $25.50 total • $1.25 today • $4.50 this week • $18.00 this month" in snapshot.details


def test_fetch_account_usage_kimi_unavailable_without_api_key(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "")

    snapshot = fetch_account_usage("kimi")

    assert snapshot is not None
    assert snapshot.provider == "kimi"
    assert snapshot.source == "usage_api"
    assert snapshot.unavailable_reason is not None
    assert "Kimi API key not configured" in snapshot.unavailable_reason
    assert snapshot.windows == ()


def test_fetch_account_usage_kimi_maps_live_usages_shape(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test")
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "user": {"membership": {"level": "LEVEL_BASIC"}},
                "usage": {
                    "limit": "100",
                    "used": "22",
                    "remaining": "78",
                    "resetTime": "2026-07-14T17:55:48.457288Z",
                },
                "limits": [
                    {
                        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {
                            "limit": "100",
                            "used": "17",
                            "remaining": "83",
                            "resetTime": "2026-07-09T12:55:48.457288Z",
                        },
                    }
                ],
                "parallel": {"limit": "10"},
                "totalQuota": {"limit": "100", "remaining": "99"},
                "authentication": {"method": "METHOD_API_KEY", "scope": "FEATURE_CODING"},
                "subType": "TYPE_PURCHASE",
            }
        ),
    )

    snapshot = fetch_account_usage("kimi")

    assert snapshot is not None
    assert snapshot.unavailable_reason is None
    assert snapshot.provider == "kimi"
    assert snapshot.source == "usage_api"
    assert snapshot.title == "Kimi"
    assert snapshot.plan == "Basic · Purchase"
    assert snapshot.windows == (
        AccountUsageWindow(
            label="Diese Woche",
            used_percent=22.0,
            window_key="weekly",
            reset_at=datetime(2026, 7, 14, 17, 55, 48, 457288, tzinfo=timezone.utc),
            detail="78/100 verbleibend",
        ),
        AccountUsageWindow(
            label="5-Std-Fenster",
            used_percent=17.0,
            window_key="session",
            reset_at=datetime(2026, 7, 9, 12, 55, 48, 457288, tzinfo=timezone.utc),
            detail="83/100 verbleibend",
        ),
    )
    assert "Gesamt-Quota: 99/100 verbleibend" in snapshot.details
    assert "Parallel: 10" in snapshot.details


def test_fetch_account_usage_kimi_rejected_api_key_returns_unavailable(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-invalid")

    class _UnauthorizedClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None):
            class _Response:
                status_code = 401

                def raise_for_status(self):
                    raise Exception("Unauthorized")

            return _Response()

    monkeypatch.setattr("agent.account_usage.httpx.Client", lambda timeout=15.0: _UnauthorizedClient())

    snapshot = fetch_account_usage("kimi")

    assert snapshot is not None
    assert snapshot.unavailable_reason == "Kimi API key rejected."


def test_fetch_xai_account_usage_latest_billing_line_wins(tmp_path):
    log_path = tmp_path / "unified.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"ts":"2026-07-16T07:00:00Z","msg":"noise line one","lvl":"info"}',
                _XAI_BILLING_OLDER_LINE,
                '{"ts":"2026-07-16T09:00:00Z","msg":"noise line two","lvl":"debug"}',
                _XAI_BILLING_FIXTURE_LINE,
                '{"msg":"unrelated shell event"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = _fetch_xai_account_usage(log_path=log_path)

    assert snapshot is not None
    assert snapshot.available is True
    assert snapshot.provider == "xai"
    assert snapshot.source == "grok_cli_log"
    assert snapshot.title == "Grok"
    assert snapshot.plan == "SuperGrok"
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.label == "Diese Woche"
    assert window.used_percent == 21.0
    assert window.window_key == "weekly"
    assert window.reset_at == datetime(2026, 7, 19, 17, 58, 33, 973068, tzinfo=timezone.utc)
    assert snapshot.details
    assert snapshot.details[0].startswith("Stand: 2026-07-16")
    assert len(snapshot.details) == 1
    assert snapshot.signal_at == datetime(2026, 7, 16, 9, 47, 14, 767000, tzinfo=timezone.utc)


def test_fetch_xai_account_usage_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"

    snapshot = _fetch_xai_account_usage(log_path=missing)

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.provider == "xai"
    assert snapshot.source == "grok_cli_log"
    assert snapshot.unavailable_reason is not None
    assert "nicht gefunden" in snapshot.unavailable_reason
    assert snapshot.windows == ()


def test_fetch_xai_account_usage_noise_only(tmp_path):
    log_path = tmp_path / "unified.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"ts":"2026-07-16T07:00:00Z","msg":"shell start","lvl":"info"}',
                "not even json",
                '{"msg":"some other event","ctx":{}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = _fetch_xai_account_usage(log_path=log_path)

    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.unavailable_reason == "Keine Billing-Daten im Grok-CLI-Log."


def test_fetch_xai_account_usage_malformed_newest_falls_back_to_older(tmp_path):
    log_path = tmp_path / "unified.jsonl"
    # Newest line contains the marker but is truncated JSON; older valid line wins.
    malformed = (
        '{"ts":"2026-07-16T10:00:00Z","msg":"billing: fetched credits config",'
        '"ctx":{"config":{"creditUsagePercent":99.0'
    )
    log_path.write_text(
        "\n".join([_XAI_BILLING_FIXTURE_LINE, malformed]) + "\n",
        encoding="utf-8",
    )

    snapshot = _fetch_xai_account_usage(log_path=log_path)

    assert snapshot is not None
    assert snapshot.available is True
    assert snapshot.windows[0].used_percent == 21.0
    assert snapshot.plan == "SuperGrok"


def test_fetch_account_usage_dispatches_xai_and_grok(monkeypatch):
    sentinel = AccountUsageSnapshot(
        provider="xai",
        source="grok_cli_log",
        fetched_at=datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
        title="Grok",
        plan="SuperGrok",
        windows=(
            AccountUsageWindow(
                label="Diese Woche",
                used_percent=21.0,
                window_key="weekly",
            ),
        ),
    )
    calls = []

    def _fake_fetch(log_path=None):
        calls.append(log_path)
        return sentinel

    monkeypatch.setattr("agent.account_usage._fetch_xai_account_usage", _fake_fetch)

    assert fetch_account_usage("xai") is sentinel
    assert fetch_account_usage("grok") is sentinel
    assert len(calls) == 2


def test_fetch_account_usage_codex_seven_day_primary_window_is_weekly(monkeypatch):
    """Live Codex Pro shape: a single rolling 7-day ``primary_window`` (604800 s),
    no secondary. It must be labelled Weekly — not mis-called a 5-hour session."""
    monkeypatch.setattr(
        "agent.account_usage.resolve_codex_runtime_credentials",
        lambda refresh_if_expiring=True: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "access-token",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage._read_codex_tokens",
        lambda: {"tokens": {"account_id": "acct_123"}},
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 83,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 604800,
                    },
                    "secondary_window": None,
                },
            }
        ),
    )

    snapshot = fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].label == "Weekly"
    assert snapshot.windows[0].window_key == "weekly"
    assert snapshot.windows[0].used_percent == 83.0


def test_fetch_xai_account_usage_new_period_without_pct_reports_unknown(tmp_path):
    """Period boundary: the newest line (fresh period, new unified-billing format)
    omits ``creditUsagePercent``. The fetcher must NOT resurrect the previous
    period's stale value — it reports an honest unknown with the fresh reset."""
    new_period_line = (
        '{"ts":"2026-07-19T17:58:45.928Z","src":"shell","pid":1,"lvl":"info",'
        '"msg":"billing: fetched credits config","ctx":{"config":{'
        '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-19T17:58:33.973068+00:00",'
        '"end":"2026-07-26T17:58:33.973068+00:00"},"onDemandCap":{"val":0},"onDemandUsed":{"val":0},'
        '"prepaidBalance":{"val":0},"isUnifiedBillingUser":true},'
        '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
    )
    old_period_line = (
        '{"ts":"2026-07-19T17:58:15.949Z","src":"shell","pid":1,"lvl":"info",'
        '"msg":"billing: fetched credits config","ctx":{"config":{"creditUsagePercent":100.0,'
        '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-12T17:58:33.973068+00:00",'
        '"end":"2026-07-19T17:58:33.973068+00:00"},"isUnifiedBillingUser":true},'
        '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
    )
    log_path = tmp_path / "unified.jsonl"
    log_path.write_text(
        "\n".join([old_period_line, new_period_line]) + "\n",
        encoding="utf-8",
    )

    snapshot = _fetch_xai_account_usage(log_path=log_path)

    assert snapshot is not None
    assert snapshot.available is True
    assert snapshot.plan == "SuperGrok"
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.window_key == "weekly"
    # Honest unknown for the just-started period — not the stale previous 100 %.
    assert window.used_percent is None
    assert window.reset_at == datetime(2026, 7, 26, 17, 58, 33, 973068, tzinfo=timezone.utc)


def test_fetch_account_usage_anthropic_maps_scoped_weekly_limit(monkeypatch):
    """The structured ``limits[]`` carries a model-scoped weekly cap the flat
    top-level fields don't expose; it becomes a secondary ``scoped_week`` window
    with the model name in ``detail``."""
    monkeypatch.setattr(
        "agent.account_usage.resolve_anthropic_token",
        lambda: "sk-ant-oat01-test-oauth-token",
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "five_hour": {"utilization": 4.0, "resets_at": "2026-07-20T19:29:59Z"},
                "seven_day": {"utilization": 82.0, "resets_at": "2026-07-24T03:59:59Z"},
                "limits": [
                    {"kind": "session", "group": "session", "percent": 4},
                    {"kind": "weekly_all", "group": "weekly", "percent": 82},
                    {
                        "kind": "weekly_scoped",
                        "group": "weekly",
                        "percent": 94,
                        "resets_at": "2026-07-24T03:59:59Z",
                        "scope": {"model": {"display_name": "Fable"}, "surface": None},
                        "is_active": True,
                    },
                ],
            }
        ),
    )

    snapshot = fetch_account_usage("anthropic")

    assert snapshot is not None
    scoped = [w for w in snapshot.windows if w.window_key == "scoped_week"]
    assert len(scoped) == 1
    assert scoped[0].label == "Modell-Limit"
    assert scoped[0].used_percent == 94.0
    assert scoped[0].detail == "Fable"
    assert scoped[0].reset_at == datetime(2026, 7, 24, 3, 59, 59, tzinfo=timezone.utc)


def test_fetch_account_usage_anthropic_scoped_percent_is_percentage_points(monkeypatch):
    """``limits[].percent`` is in percentage points: ``percent=1`` means 1 % (NOT
    100 %), an explicitly inactive scoped cap is skipped, and a bool percent is
    rejected."""
    monkeypatch.setattr(
        "agent.account_usage.resolve_anthropic_token",
        lambda: "sk-ant-oat01-test-oauth-token",
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "limits": [
                    {"kind": "weekly_scoped", "percent": 1, "scope": {"model": {"display_name": "Low"}}, "is_active": True},
                    {"kind": "weekly_scoped", "percent": 99, "scope": {"model": {"display_name": "Off"}}, "is_active": False},
                    {"kind": "weekly_scoped", "percent": True, "scope": {"model": {"display_name": "Bool"}}},
                ],
            }
        ),
    )

    snapshot = fetch_account_usage("anthropic")

    assert snapshot is not None
    scoped = [w for w in snapshot.windows if w.window_key == "scoped_week"]
    # percent=1 → 1 % (not 100 %); inactive "Off" skipped; bool percent rejected.
    assert len(scoped) == 1
    assert scoped[0].used_percent == 1.0
    assert scoped[0].detail == "Low"


def test_fetch_account_usage_codex_ambiguous_window_falls_back_to_position(monkeypatch):
    """A window length in the ambiguous band (24 h < len < 2 d) is not force-classified
    — it falls back to the position-based default (primary → session)."""
    monkeypatch.setattr(
        "agent.account_usage.resolve_codex_runtime_credentials",
        lambda refresh_if_expiring=True: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "access-token",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage._read_codex_tokens",
        lambda: {"tokens": {"account_id": "acct_123"}},
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 50,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 129600,  # 36 h — ambiguous band
                    },
                },
            }
        ),
    )

    snapshot = fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[0].window_key == "session"


def test_fetch_xai_account_usage_no_period_anchor_uses_newest_only(tmp_path):
    """Newest billing line has no ``currentPeriod`` (no period anchor): trust only
    that newest record — never fall back to an arbitrary older percent."""
    newest_no_period = (
        '{"ts":"2026-07-19T18:00:00.000Z","src":"shell","pid":1,"lvl":"info",'
        '"msg":"billing: fetched credits config","ctx":{"config":{'
        '"onDemandCap":{"val":0},"onDemandUsed":{"val":0},"prepaidBalance":{"val":0}},'
        '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
    )
    older_with_pct = (
        '{"ts":"2026-07-19T17:00:00.000Z","src":"shell","pid":1,"lvl":"info",'
        '"msg":"billing: fetched credits config","ctx":{"config":{"creditUsagePercent":77.0,'
        '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-12T17:58:33.973068+00:00",'
        '"end":"2026-07-19T17:58:33.973068+00:00"}},'
        '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
    )
    log_path = tmp_path / "unified.jsonl"
    log_path.write_text(
        "\n".join([older_with_pct, newest_no_period]) + "\n",
        encoding="utf-8",
    )

    snapshot = _fetch_xai_account_usage(log_path=log_path)

    assert snapshot is not None
    assert snapshot.available is True
    # No stale 77 % resurrected from the older line — honest unknown.
    assert snapshot.windows[0].used_percent is None
    assert snapshot.signal_at == datetime(2026, 7, 19, 18, 0, 0, tzinfo=timezone.utc)


def test_fetch_xai_account_usage_same_period_older_pct_metadata_from_newest(tmp_path):
    """Newest line of the current period lacks pct but an older line of the SAME
    period has it: use that percent, but take signal/plan/reset from the newest line."""
    newest_no_pct = (
        '{"ts":"2026-07-16T10:00:00.000Z","src":"shell","pid":1,"lvl":"info",'
        '"msg":"billing: fetched credits config","ctx":{"config":{'
        '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-12T17:58:33.973068+00:00",'
        '"end":"2026-07-19T17:58:33.973068+00:00"},"onDemandCap":{"val":0}},'
        '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
    )
    older_same_period_pct = (
        '{"ts":"2026-07-16T08:00:00.000Z","src":"shell","pid":1,"lvl":"info",'
        '"msg":"billing: fetched credits config","ctx":{"config":{"creditUsagePercent":42.0,'
        '"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY","start":"2026-07-12T17:58:33.973068+00:00",'
        '"end":"2026-07-19T17:58:33.973068+00:00"}},'
        '"onDemandEnabled":null,"subscriptionTier":"SuperGrok"}}'
    )
    log_path = tmp_path / "unified.jsonl"
    log_path.write_text(
        "\n".join([older_same_period_pct, newest_no_pct]) + "\n",
        encoding="utf-8",
    )

    snapshot = _fetch_xai_account_usage(log_path=log_path)

    assert snapshot is not None
    assert snapshot.windows[0].used_percent == 42.0  # pct from older same-period line
    # Signal comes from the NEWEST line, not the older pct line.
    assert snapshot.signal_at == datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)
    assert snapshot.windows[0].reset_at == datetime(2026, 7, 19, 17, 58, 33, 973068, tzinfo=timezone.utc)


def test_render_account_usage_lines_shows_detail_alongside_reset():
    """A window with BOTH reset and detail renders both (detail no longer swallowed
    by the reset branch)."""
    snapshot = AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        title="Claude",
        windows=(
            AccountUsageWindow(
                label="Modell-Limit",
                used_percent=94.0,
                reset_at=datetime(2026, 7, 24, 3, 59, 59, tzinfo=timezone.utc),
                detail="Fable",
                window_key="scoped_week",
            ),
        ),
    )

    lines = render_account_usage_lines(snapshot)
    scoped_line = next(line for line in lines if line.startswith("Modell-Limit"))
    assert "resets" in scoped_line
    assert "Fable" in scoped_line


# --- Claude subscription plan label (B: "Max 20×") ---------------------------


def test_format_anthropic_plan_max_20x():
    # Real live values: subscriptionType="max", rateLimitTier="default_claude_max_20x".
    assert _format_anthropic_plan("max", "default_claude_max_20x") == "Max 20×"


def test_format_anthropic_plan_base_only_when_no_multiplier():
    assert _format_anthropic_plan("pro", None) == "Pro"


def test_format_anthropic_plan_infers_base_from_tier():
    # subscriptionType missing → derive base + multiplier from the tier alone.
    assert _format_anthropic_plan(None, "default_claude_max_5x") == "Max 5×"


def test_format_anthropic_plan_none_when_empty():
    assert _format_anthropic_plan(None, None) is None
    assert _format_anthropic_plan("", "") is None


def test_resolve_anthropic_plan_label_reads_credentials_file(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "x", '
        '"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.account_usage.Path.home", lambda: tmp_path)
    assert _resolve_anthropic_plan_label() == "Max 20×"


def test_resolve_anthropic_plan_label_fail_soft_when_missing(tmp_path, monkeypatch):
    # No credential files at all → None, never raises.
    monkeypatch.setattr("agent.account_usage.Path.home", lambda: tmp_path)
    assert _resolve_anthropic_plan_label() is None


def test_resolve_anthropic_plan_label_fail_soft_on_non_object_root(tmp_path, monkeypatch):
    # A malformed credentials file whose JSON root is not an object must never
    # raise (it would otherwise drop the whole Claude usage snapshot).
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text("[1, 2, 3]", encoding="utf-8")
    (tmp_path / ".claude.json").write_text('"not-an-object"', encoding="utf-8")
    monkeypatch.setattr("agent.account_usage.Path.home", lambda: tmp_path)
    assert _resolve_anthropic_plan_label() is None

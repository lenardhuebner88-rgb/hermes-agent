from datetime import datetime, timezone

from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
    _fetch_xai_account_usage,
    fetch_account_usage,
    render_account_usage_lines,
)

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

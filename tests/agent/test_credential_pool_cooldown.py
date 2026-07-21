"""Characterization tests for the credential-pool cooldown arithmetic.

These pure helpers decide *when an exhausted credential becomes usable again*.
A silent regression here either burns a credential by retrying it too early
(into another 401/429) or blacklists a healthy one forever — both degrade
autonomy without any visible error. We pin:

* ``_extract_retry_delay_seconds`` — parses provider-emitted retry hints
  (``quotaResetDelay``, ``retry after N seconds``, ``resets in 4hr 5min``).
* ``_exhausted_ttl`` — status→cooldown-seconds table (401 short, 429/default 1h).
* ``_parse_absolute_timestamp`` — epoch-s / epoch-ms / ISO-8601 best-effort parse.
* ``_exhausted_until`` — precedence: provider reset_at > last_status_at+TTL > None.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.credential_pool import (
    EXHAUSTED_TTL_401_SECONDS,
    EXHAUSTED_TTL_429_SECONDS,
    EXHAUSTED_TTL_DEFAULT_SECONDS,
    PooledCredential,
    STATUS_EXHAUSTED,
    STATUS_OK,
    _exhausted_ttl,
    _exhausted_until,
    _extract_retry_delay_seconds,
    _parse_absolute_timestamp,
)


def _make_entry(**overrides) -> PooledCredential:
    base = dict(
        provider="anthropic",
        id="abc123",
        label="test-key",
        auth_type="api_key",
        priority=0,
        source="manual",
        access_token="sk-ant-dummy",
    )
    base.update(overrides)
    return PooledCredential(**base)


# ─── _exhausted_ttl: status → cooldown seconds ───────────────────────────────


def test_ttl_401_is_short():
    assert _exhausted_ttl(401) == EXHAUSTED_TTL_401_SECONDS == 300


def test_ttl_429_is_one_hour():
    assert _exhausted_ttl(429) == EXHAUSTED_TTL_429_SECONDS == 3600


def test_ttl_unknown_status_falls_back_to_default_hour():
    # 402 (billing), 500, None — anything not 401/429 uses the default.
    assert _exhausted_ttl(None) == EXHAUSTED_TTL_DEFAULT_SECONDS == 3600
    assert _exhausted_ttl(402) == 3600
    assert _exhausted_ttl(500) == 3600


# ─── _extract_retry_delay_seconds: provider hint → seconds ───────────────────


def test_empty_or_no_hint_returns_none():
    assert _extract_retry_delay_seconds("") is None
    assert _extract_retry_delay_seconds("rate limit exceeded, try later") is None


def test_quota_reset_delay_in_ms_is_divided_by_1000():
    # Mutation target: the /1000.0 for the "ms" unit.
    assert _extract_retry_delay_seconds('quotaResetDelay: 5000ms') == 5.0
    assert _extract_retry_delay_seconds('quotaResetDelay":2500ms') == 2.5


def test_quota_reset_delay_in_seconds_is_not_scaled():
    assert _extract_retry_delay_seconds("quotaResetDelay: 30s") == 30.0


def test_retry_after_seconds_phrasing():
    assert _extract_retry_delay_seconds("Retry after 120 seconds") == 120.0
    assert _extract_retry_delay_seconds("please retry after 30 sec") == 30.0
    assert _extract_retry_delay_seconds("retry 45s and back off") == 45.0


def test_resets_in_hours_and_minutes_combined():
    # OpenCode Go weekly-limit phrasing. Mutation target: hr*3600 + min*60.
    assert _extract_retry_delay_seconds("Resets in 4hr 5min") == 4 * 3600 + 5 * 60
    assert _extract_retry_delay_seconds("reset in 1 hr 30 min") == 3600 + 1800


def test_resets_in_hours_only():
    assert _extract_retry_delay_seconds("Resets in 2 hr") == 7200
    assert _extract_retry_delay_seconds("resets in 3hr") == 10800


def test_resets_in_minutes_only():
    assert _extract_retry_delay_seconds("Resets in 15 min") == 900
    assert _extract_retry_delay_seconds("resets in 90min") == 5400


def test_hr_min_takes_precedence_over_hr_only_and_min_only():
    # If the combined pattern were dropped, "4hr 5min" would match hr-only (14400)
    # and lose the 5 minutes — pin the combined result.
    assert _extract_retry_delay_seconds("Resets in 4hr 5min") == 14700


def test_matching_is_case_insensitive():
    assert _extract_retry_delay_seconds("QUOTARESETDELAY: 7S") == 7.0
    assert _extract_retry_delay_seconds("RETRY AFTER 10 SECONDS") == 10.0


# ─── _parse_absolute_timestamp: epoch-s / epoch-ms / ISO-8601 ────────────────


def test_none_empty_and_nonpositive_return_none():
    assert _parse_absolute_timestamp(None) is None
    assert _parse_absolute_timestamp("") is None
    assert _parse_absolute_timestamp("   ") is None
    assert _parse_absolute_timestamp(0) is None
    assert _parse_absolute_timestamp(-5) is None


def test_epoch_seconds_pass_through():
    assert _parse_absolute_timestamp(1_700_000_000) == 1_700_000_000.0


def test_epoch_milliseconds_above_threshold_are_scaled_down():
    # Mutation target: the > 1e12 branch divides by 1000.
    assert _parse_absolute_timestamp(1_700_000_000_000) == 1_700_000_000.0


def test_numeric_string_is_parsed_like_a_number():
    assert _parse_absolute_timestamp("1700000000") == 1_700_000_000.0
    assert _parse_absolute_timestamp("1700000000000") == 1_700_000_000.0


def test_iso8601_with_z_suffix_is_parsed_as_utc():
    expected = datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    assert _parse_absolute_timestamp("2026-07-21T00:00:00Z") == expected


def test_unparseable_string_and_wrong_type_return_none():
    assert _parse_absolute_timestamp("not-a-timestamp") is None
    assert _parse_absolute_timestamp(["2026-07-21"]) is None


# ─── _exhausted_until: precedence of reset_at over TTL math ──────────────────


def test_non_exhausted_entry_has_no_deadline():
    entry = _make_entry(last_status=STATUS_OK, last_status_at=1000.0)
    assert _exhausted_until(entry) is None
    assert _exhausted_until(_make_entry(last_status=None)) is None


def test_provider_reset_at_wins_over_ttl():
    # Even with a last_status_at present, an explicit reset_at is authoritative.
    entry = _make_entry(
        last_status=STATUS_EXHAUSTED,
        last_status_at=1000.0,
        last_error_code=401,
        last_error_reset_at=9999.0,
    )
    assert _exhausted_until(entry) == 9999.0


def test_provider_reset_at_as_iso_string_is_normalized():
    # reset_at rehydrated from JSON may be an ISO string; parse path applies.
    iso = "2026-07-21T00:00:00Z"
    expected = datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    entry = _make_entry(
        last_status=STATUS_EXHAUSTED,
        last_status_at=1000.0,
        last_error_reset_at=iso,
    )
    assert _exhausted_until(entry) == expected


def test_falls_back_to_status_at_plus_ttl_when_no_reset_at():
    entry = _make_entry(
        last_status=STATUS_EXHAUSTED,
        last_status_at=1000.0,
        last_error_code=401,
        last_error_reset_at=None,
    )
    assert _exhausted_until(entry) == 1000.0 + EXHAUSTED_TTL_401_SECONDS


def test_ttl_selection_follows_error_code():
    entry = _make_entry(
        last_status=STATUS_EXHAUSTED, last_status_at=1000.0, last_error_code=429
    )
    assert _exhausted_until(entry) == 1000.0 + EXHAUSTED_TTL_429_SECONDS


def test_exhausted_without_any_timestamp_has_no_deadline():
    entry = _make_entry(
        last_status=STATUS_EXHAUSTED, last_status_at=None, last_error_reset_at=None
    )
    assert _exhausted_until(entry) is None

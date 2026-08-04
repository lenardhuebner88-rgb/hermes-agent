"""Tests for the fork-owned usage-facts pricing seam (P3/P4 Preiswahrheit).

Rates under test are sourced: Anthropic platform.claude.com pricing docs
(fetched 2026-08-03), Alibaba model-studio pricing + context-cache docs
(P1, 2026-08-03). The real-row fixture is verbatim from the live
usage_facts.db (see tests/fixtures/usage_facts_pricing/real_rows.json).
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import hermes_cli.usage_facts_readmodel as readmodel
from hermes_cli.execution_facts_reconcile import (
    _canonical_usage,
    reconcile_usage_facts,
)
from hermes_cli.usage_facts_db import initialize_usage_facts_db, upsert_run_facts
from hermes_cli.usage_facts_pricing import (
    USAGE_FACTS_PRICING_VERSION,
    CanonicalUsage,
    UsageFactsUsage,
    estimate_equivalent_cost,
    estimate_usage_cost,
    priceability,
    resolve_model_alias,
)

REAL_ROWS = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "usage_facts_pricing"
        / "real_rows.json"
    ).read_text(encoding="utf-8")
)

_ONE_MILLION = 1_000_000


def _probe_rate(model: str, component: str, provider: str | None) -> Decimal | None:
    """Per-component $/1M rate via the same probe the readmodel uses."""
    usage_kwargs = {name: 0 for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_write_1h_tokens",
        "cache_write_5m_tokens",
        "reasoning_tokens",
    )}
    usage_kwargs["cache_write_1h_tokens"] = None
    usage_kwargs["cache_write_5m_tokens"] = None
    usage_kwargs[component] = _ONE_MILLION
    result = estimate_equivalent_cost(
        model,
        UsageFactsUsage(**usage_kwargs, request_count=0),
        provider=provider,
    )
    return result.amount_usd


def test_anthropic_rates_carry_ttl_split() -> None:
    # claude-opus-4-8 lives in the upstream catalog; the fork derives the
    # 1h cache-write rate (2x input, sourced) next to the stored 5m rate.
    assert _probe_rate("claude-opus-4-8", "input_tokens", "anthropic") == Decimal("5.00")
    assert _probe_rate("claude-opus-4-8", "output_tokens", "anthropic") == Decimal("25.00")
    assert _probe_rate("claude-opus-4-8", "cache_read_tokens", "anthropic") == Decimal("0.50")
    assert _probe_rate("claude-opus-4-8", "cache_write_tokens", "anthropic") == Decimal("6.25")
    assert _probe_rate("claude-opus-4-8", "cache_write_1h_tokens", "anthropic") == Decimal("10.00")
    assert _probe_rate("claude-opus-4-8", "cache_write_5m_tokens", "anthropic") == Decimal("6.25")


def test_fork_catalog_additions_are_sourced() -> None:
    # Both models are absent from the upstream catalog (checked 2026-08-03).
    assert _probe_rate("claude-opus-5", "input_tokens", "anthropic") == Decimal("5.00")
    assert _probe_rate("claude-opus-5", "output_tokens", "anthropic") == Decimal("25.00")
    assert _probe_rate("claude-opus-5", "cache_write_1h_tokens", "anthropic") == Decimal("10.00")
    assert _probe_rate("claude-fable-5", "input_tokens", "anthropic") == Decimal("10.00")
    assert _probe_rate("claude-fable-5", "output_tokens", "anthropic") == Decimal("50.00")
    assert _probe_rate("claude-fable-5", "cache_read_tokens", "anthropic") == Decimal("1.00")
    assert _probe_rate("claude-fable-5", "cache_write_5m_tokens", "anthropic") == Decimal("12.50")
    assert _probe_rate("claude-fable-5", "cache_write_1h_tokens", "anthropic") == Decimal("20.00")


def test_sonnet_5_uses_list_price_not_introductory() -> None:
    # Canon 7.2: list prices, no temporal discounts. The introductory
    # window ($2/$10, 5m $2.50, 1h $4, read $0.20) must not be priced.
    assert _probe_rate("claude-sonnet-5", "input_tokens", "anthropic") == Decimal("3.00")
    assert _probe_rate("claude-sonnet-5", "output_tokens", "anthropic") == Decimal("15.00")
    assert _probe_rate("claude-sonnet-5", "cache_read_tokens", "anthropic") == Decimal("0.30")
    assert _probe_rate("claude-sonnet-5", "cache_write_5m_tokens", "anthropic") == Decimal("3.75")
    assert _probe_rate("claude-sonnet-5", "cache_write_1h_tokens", "anthropic") == Decimal("6.00")


def test_qwen_implicit_path_prices_cache_read_at_20_percent() -> None:
    provider = "alibaba-token-plan"
    # OpenAI-compatible paths (qwen_cli, hermes runtime) send no
    # cache_control; the automatic implicit cache bills reads at 20% of
    # input and does not bill writes at all.
    assert _probe_rate("qwen3.7-max", "input_tokens", provider) == Decimal("2.50")
    assert _probe_rate("qwen3.7-max", "output_tokens", provider) == Decimal("7.50")
    assert _probe_rate("qwen3.7-max", "cache_read_tokens", provider) == Decimal("0.50")
    assert _probe_rate("qwen3.7-max", "cache_write_tokens", provider) is None
    assert _probe_rate("qwen3.6-flash", "input_tokens", provider) == Decimal("0.25")
    assert _probe_rate("qwen3.6-flash", "cache_read_tokens", provider) == Decimal("0.05")
    assert _probe_rate("qwen3.7-plus", "input_tokens", provider) == Decimal("0.40")
    assert _probe_rate("qwen3.7-plus", "cache_read_tokens", provider) == Decimal("0.08")

    # No implicit write rate: rows with cache-write tokens stay unknown
    # (fail-closed, Canon rule 3); rows without cache price cleanly.
    with_cache = estimate_equivalent_cost(
        "qwen3.7-max",
        UsageFactsUsage(input_tokens=1000, cache_write_tokens=500),
        provider=provider,
    )
    assert with_cache.amount_usd is None
    assert with_cache.status == "unknown"
    without_cache = estimate_equivalent_cost(
        "qwen3.7-max",
        UsageFactsUsage(input_tokens=_ONE_MILLION),
        provider=provider,
    )
    assert without_cache.amount_usd == Decimal("2.50")


def test_qwen_explicit_path_for_anthropic_protocol_origin() -> None:
    provider = "alibaba-token-plan"

    def probe(model: str, component: str) -> Decimal | None:
        usage_kwargs = {name: 0 for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_1h_tokens",
            "cache_write_5m_tokens",
            "reasoning_tokens",
        )}
        usage_kwargs["cache_write_1h_tokens"] = None
        usage_kwargs["cache_write_5m_tokens"] = None
        usage_kwargs[component] = _ONE_MILLION
        result = estimate_equivalent_cost(
            model,
            UsageFactsUsage(**usage_kwargs, request_count=0),
            provider=provider,
            origin="claude_code",
        )
        return result.amount_usd

    # claude_code speaks the Anthropic protocol with cache_control to the
    # Token-Plan endpoint: explicit cache — read 10%, write 125% of input,
    # no TTL differentiation on the Alibaba side.
    assert probe("qwen3.7-max", "cache_read_tokens") == Decimal("0.25")
    assert probe("qwen3.7-max", "cache_write_tokens") == Decimal("3.125")
    assert probe("qwen3.7-max", "cache_write_1h_tokens") == Decimal("3.125")
    assert probe("qwen3.7-max", "cache_write_5m_tokens") == Decimal("3.125")
    assert probe("qwen3.6-flash", "cache_write_tokens") == Decimal("0.3125")


def test_reasoning_included_in_output_does_not_fail_closed() -> None:
    # gpt-5.6 runs report reasoning tokens. Upstream PricingEntry semantics
    # bill them through output_tokens ("included_in_output"), so a missing
    # separate reasoning rate must not fail-closed the whole row.
    # Regression: the round-2 seam initially dropped these groups, flipping
    # hermes_agent gpt-5.6 breakdowns to unknown in the readmodel.
    result = estimate_equivalent_cost(
        "gpt-5.6-sol",
        UsageFactsUsage(input_tokens=_ONE_MILLION, reasoning_tokens=500_000),
        provider="openai-codex",
        origin="hermes_agent",
    )
    assert result.status == "equivalent"
    assert result.amount_usd == Decimal("5.00")
    assert priceability("gpt-5.6-sol", "openai-codex", "hermes_agent")[
        "priceable"
    ]


def test_round2_entries_and_documented_absences() -> None:
    # gpt-5.5: priced, cache-write line item sourcedly absent.
    assert _probe_rate("gpt-5.5", "input_tokens", "openai") == Decimal("5.00")
    assert _probe_rate("gpt-5.5", "cache_read_tokens", "openai") == Decimal("0.50")
    assert _probe_rate("gpt-5.5", "cache_write_tokens", "openai") is None
    check = priceability("gpt-5.5", "openai")
    assert check["priceable"] is False
    assert check["classification"] == "documented_absent"

    # grok-4.5 (<200k tier): priced, write line item sourcedly absent.
    assert _probe_rate("grok-4.5", "input_tokens", "xai-oauth") == Decimal("2.00")
    assert _probe_rate("grok-4.5", "cache_read_tokens", "xai-oauth") == Decimal("0.30")
    assert _probe_rate("grok-4.5", "cache_write_tokens", "xai-oauth") is None
    check = priceability("grok-4.5", "xai-oauth")
    assert check["classification"] == "documented_absent"

    # kimi-k3: fully automatic cache, no separate write rate exists.
    check = priceability("kimi-k3", "kimi-coding")
    assert check["classification"] == "documented_absent"
    assert "write rate does not exist" in check["reason"]

    # gpt-5.6 terra/luna corrected to the current pricing page (the
    # upstream launch-blog entries are outdated); sol unchanged upstream.
    assert _probe_rate("gpt-5.6-terra", "input_tokens", "openai-codex") == Decimal("2.00")
    assert _probe_rate("gpt-5.6-terra", "output_tokens", "openai-codex") == Decimal("12.00")
    assert _probe_rate("gpt-5.6-terra", "cache_write_tokens", "openai-codex") == Decimal("2.50")
    assert _probe_rate("gpt-5.6-luna", "input_tokens", "openai-codex") == Decimal("0.20")
    assert _probe_rate("gpt-5.6-luna", "cache_write_tokens", "openai-codex") == Decimal("0.25")

    # qwen3.8-*: PAYG rate does not exist — documented, fail-closed.
    check = priceability("qwen3.8-max-preview", "alibaba-token-plan")
    assert check["classification"] == "documented_absent"
    assert "does not exist" in check["reason"]

    # codex-auto-review: product feature, no vendor price.
    check = priceability("codex-auto-review", "openai")
    assert check["classification"] == "documented_absent"

    # Capture gaps stay their own class.  A missing provider is a mapping
    # problem, not a price gap: gpt-5.4-mini resolves unambiguously to
    # openai (provider_inferred), where the sourcedly-absent write rate
    # classifies it — only unresolvable models stay provider_missing.
    assert priceability(None, "xai")["classification"] == "model_missing"
    check = priceability("gpt-5.4-mini", None)
    assert check["provider_inferred"] is True
    assert check["resolved_provider"] == "openai"
    assert check["classification"] == "documented_absent"
    check = priceability("<synthetic>", None)
    assert check["classification"] == "model_missing"


def test_qwen38_models_have_no_payg_rate() -> None:
    for model in ("qwen3.8-max-preview", "qwen3.8-max"):
        result = estimate_equivalent_cost(
            model,
            UsageFactsUsage(input_tokens=_ONE_MILLION),
            provider="alibaba-token-plan",
        )
        assert result.amount_usd is None
        assert result.status == "unknown"
        check = priceability(model, "alibaba-token-plan")
        assert check["priceable"] is False
        assert "Token-Plan" in check["reason"] or "PAYG" in check["reason"]


def test_aliases_resolve_onto_priced_targets() -> None:
    assert resolve_model_alias("kimi-for-coding") == "k3"
    assert resolve_model_alias("kimi-k3") == "k3"
    # Upstream route resolution strips the vendor prefix first; the alias
    # must still fire on the stripped name (live: 283M tokens, 30 days).
    assert resolve_model_alias("kimi-code/kimi-for-coding") == "k3"
    assert resolve_model_alias("kimi-code/k3") == "kimi-code/k3"
    assert resolve_model_alias("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert resolve_model_alias("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"

    assert _probe_rate("kimi-for-coding", "input_tokens", "kimi-code") == Decimal("3.00")
    assert _probe_rate("kimi-k3", "input_tokens", "kimi-coding") == Decimal("3.00")
    assert _probe_rate(
        "kimi-code/kimi-for-coding", "input_tokens", "kimi-code"
    ) == Decimal("3.00")
    assert _probe_rate(
        "claude-haiku-4-5-20251001", "input_tokens", "anthropic"
    ) == Decimal("1.00")

    # qwen-route was never observed and has no sourced target: fail-closed.
    assert resolve_model_alias("qwen-route") == "qwen-route"
    unknown = estimate_equivalent_cost(
        "qwen-route", UsageFactsUsage(input_tokens=1000), provider="qwen"
    )
    assert unknown.amount_usd is None


def test_ttl_split_replaces_total_in_billing() -> None:
    usage = UsageFactsUsage(
        cache_write_tokens=9_999_999,  # must be ignored when the split exists
        cache_write_1h_tokens=1_000_000,
        cache_write_5m_tokens=500_000,
    )
    result = estimate_equivalent_cost("claude-opus-4-8", usage, provider="anthropic")
    assert result.amount_usd == Decimal("10.00") + Decimal("3.125")
    assert result.pricing_version is not None
    assert USAGE_FACTS_PRICING_VERSION in result.pricing_version


def test_billable_input_subtracts_cache_when_contained() -> None:
    """Canon register §5f: OpenAI-convention rows count cached reads inside
    input_tokens; pricing both bills them twice (measured 8.1x on codex/kimi
    lanes over 30d).  gpt-5.5 rates: input $5.00, cache_read $0.50."""
    usage = UsageFactsUsage(input_tokens=1_000_000, cache_read_tokens=400_000)
    result = estimate_equivalent_cost("gpt-5.5", usage, provider="openai")
    # billable input 600k * $5 + 400k * $0.50
    assert result.amount_usd == Decimal("3.00") + Decimal("0.20")


def test_billable_input_untouched_for_anthropic_convention() -> None:
    """Anthropic input excludes cache; cache reads far above input must not
    trigger the subtraction.  claude-sonnet-5: input $3.00, cache_read $0.30."""
    usage = UsageFactsUsage(input_tokens=1_000, cache_read_tokens=50_000)
    result = estimate_equivalent_cost("claude-sonnet-5", usage, provider="anthropic")
    assert result.amount_usd == (
        Decimal("1000") * Decimal("3.00") / Decimal(_ONE_MILLION)
        + Decimal("50000") * Decimal("0.30") / Decimal(_ONE_MILLION)
    )


def test_anthropic_row_with_small_cache_is_not_subtracted() -> None:
    """Review-1-Blocker: 77 production rows carry cache_read <= input on the
    Anthropic path — their input is real billed input, never cache-inclusive.
    The subtraction must NOT fire for provider=anthropic even then."""
    usage = UsageFactsUsage(input_tokens=24_798, cache_read_tokens=22_837)
    result = estimate_equivalent_cost(
        "claude-fable-5", usage, provider="anthropic", origin="claude_code"
    )
    assert result.amount_usd == (
        Decimal("24798") * Decimal("10.00") / Decimal(_ONE_MILLION)
        + Decimal("22837") * Decimal("1.00") / Decimal(_ONE_MILLION)
    )


def test_qwen_under_claude_code_is_anthropic_convention() -> None:
    """The qwen token-plan traffic runs the Anthropic protocol through
    claude_code (explicit cache, cf. _EXPLICIT_CACHE_ORIGINS): no
    subtraction — while the same model on the OpenAI-compatible qwen_cli
    path subtracts."""
    usage = UsageFactsUsage(input_tokens=1_000_000, cache_read_tokens=400_000)
    explicit = estimate_equivalent_cost(
        "qwen3.7-max", usage, provider="alibaba-token-plan", origin="claude_code"
    )
    implicit = estimate_equivalent_cost(
        "qwen3.7-max", usage, provider="qwen", origin="qwen_cli"
    )
    # explicit: full input 2.50 + read 400k*0.25 (10% of 2.50)
    assert explicit.amount_usd == Decimal("2.50") + Decimal("0.10")
    # implicit: billable input 600k*2.50 + read 400k*0.50 (20% of 2.50)
    assert implicit.amount_usd == Decimal("1.50") + Decimal("0.20")


def test_billable_input_fully_cached_row_prices_zero_uncached_input() -> None:
    usage = UsageFactsUsage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
    result = estimate_equivalent_cost("gpt-5.5", usage, provider="openai")
    assert result.amount_usd == Decimal("0.50")


def test_billable_input_neutral_without_cache() -> None:
    usage = UsageFactsUsage(input_tokens=_ONE_MILLION)
    result = estimate_equivalent_cost("gpt-5.5", usage, provider="openai")
    assert result.amount_usd == Decimal("5.00")


def test_no_split_falls_back_to_total() -> None:
    usage = UsageFactsUsage(cache_write_tokens=708)
    result = estimate_equivalent_cost("claude-opus-4-8", usage, provider="anthropic")
    assert result.amount_usd == Decimal("708") * Decimal("6.25") / Decimal(_ONE_MILLION)


def test_outlier_split_wins_over_zero_total() -> None:
    # Register Nachtrag fingerprint: cache_creation_input_tokens=0 while the
    # cache_creation object carried 1h tokens. The split is the more
    # complete observation and must be priced.
    usage = UsageFactsUsage(cache_write_tokens=0, cache_write_1h_tokens=2646)
    result = estimate_equivalent_cost("claude-opus-4-8", usage, provider="anthropic")
    assert result.amount_usd == Decimal("2646") * Decimal("10.00") / Decimal(_ONE_MILLION)
    assert result.amount_usd > 0


def test_subscription_route_included_for_metered_priced_for_equivalent() -> None:
    usage = UsageFactsUsage(input_tokens=_ONE_MILLION)
    metered = estimate_usage_cost(
        "qwen3.7-max", usage, provider="alibaba-token-plan"
    )
    assert metered.amount_usd == Decimal("0")
    assert metered.status == "included"
    equivalent = estimate_equivalent_cost(
        "qwen3.7-max", usage, provider="alibaba-token-plan"
    )
    assert equivalent.amount_usd == Decimal("2.50")
    assert equivalent.status == "equivalent"


def test_canonical_usage_adapts_without_inventing_split() -> None:
    result = estimate_equivalent_cost(
        "claude-opus-4-8",
        CanonicalUsage(cache_write_tokens=_ONE_MILLION, request_count=0),
        provider="anthropic",
    )
    assert result.amount_usd == Decimal("6.25")


def test_real_run_usage_facts_row_prices_ttl_aware() -> None:
    row = REAL_ROWS["fable5_row"]
    usage, validity = _canonical_usage(row)
    assert validity.name == "EXACT"
    result = estimate_equivalent_cost(
        row["model"], usage, provider=row["provider"]
    )
    # 266*10 + 996*50 + 168835*1 + split 708*20 (1h) + 0*12.5 (5m), per 1M.
    assert result.amount_usd == Decimal("0.235455")
    # Pricing with the stored total instead of the split would be 708*12.5:
    wrong_total = Decimal("0.226590")
    assert result.amount_usd != wrong_total


def test_real_outlier_row_prices_split_not_zero_total() -> None:
    row = REAL_ROWS["opus48_outlier_row"]
    usage, _validity = _canonical_usage(row)
    result = estimate_equivalent_cost(
        row["model"], usage, provider=row["provider"]
    )
    # 2*5 + 1529*25 + 148870*0.5 + 2646*10 (split; total is 0), per 1M.
    assert result.amount_usd == Decimal("0.139130")


def test_real_rows_through_reconcile_emit_corrected_costs(tmp_path: Path) -> None:
    db_path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db_path)
    for row in (REAL_ROWS["fable5_row"], REAL_ROWS["opus48_outlier_row"]):
        fields = {
            key: value
            for key, value in row.items()
            if key != "run_id" and key != "llm_call_count"
        }
        fields["source"] = "measured"
        upsert_run_facts(row["run_id"], fields, path=db_path)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        events = reconcile_usage_facts(connection)
    finally:
        connection.close()

    amounts = {
        event.attributes.get("api_equivalent_cost")
        for event in events
        if getattr(event, "attributes", None)
        and event.attributes.get("api_equivalent_cost") is not None
    }
    assert "0.235455" in amounts
    assert "0.139130" in amounts


def test_readmodel_prices_split_when_observed() -> None:
    normalized = readmodel.normalize_token_totals(
        "claude_code",
        token_rows=1,
        input_tokens=2,
        output_tokens=1529,
        cache_read_tokens=148870,
        cache_write_tokens=0,
        reasoning_tokens=None,
        input_observed_rows=1,
        output_observed_rows=1,
        cache_read_observed_rows=1,
        cache_write_observed_rows=1,
        reasoning_observed_rows=0,
        cache_write_1h_tokens=2646,
        cache_write_5m_tokens=0,
        cache_write_1h_observed_rows=1,
        cache_write_5m_observed_rows=1,
    )
    pricing_cache: dict = {}
    result = readmodel._price_normalized_usage(
        "equivalent",
        provider="anthropic",
        model="claude-opus-4-8",
        origin="claude_code",
        normalized=normalized,
        raw={"request_count": 1},
        pricing_cache=pricing_cache,
    )
    assert result["status"] == "equivalent"
    assert Decimal(result["amount_usd"]) == Decimal("0.139130")
    # Total-based pricing would have seen cache_write_tokens=0 and missed
    # the 2646 1h tokens entirely.
    assert Decimal(result["amount_usd"]) > Decimal("0.112670")

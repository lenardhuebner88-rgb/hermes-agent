"""Tests for the consumption metrics read model (Phase C4).

The fixture rows are derived from live usage_facts.db measurements
(2026-08-04): a claude_code call with Anthropic input convention (input
excludes cache), a codex_cli-style run-level row with OpenAI convention
(input includes cache, the §5f double-count case), a buzz run-level-only
row, and an unpriceable qwen3.8-max row (documented-absent).  Run ids
are anonymized; token figures are scaled-down real shapes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_cli.usage_consumption_readmodel import (
    CONTRACT_VERSION,
    build_consumption_payload,
)
from hermes_cli.usage_facts_db import (
    initialize_usage_facts_db,
    record_llm_call,
    upsert_run_facts,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
RECENT = "2026-08-03T10:00:00Z"
OLD = "2026-06-01T10:00:00Z"  # outside every window


def _fact_db(tmp_path: Path) -> Path:
    path = tmp_path / "facts.db"
    initialize_usage_facts_db(path)

    # claude_code call: Anthropic convention (input excludes cache).
    record_llm_call(
        "run-claude-1",
        1,
        {
            "origin": "claude_code",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "input_tokens": 1_000,
            "output_tokens": 500,
            "cache_read_tokens": 100_000,
            "cache_write_tokens": 10_000,
            "cache_write_1h_tokens": 4_000,
            "cache_write_5m_tokens": 6_000,
            "tool_call_count": 3,
            "occurred_at": RECENT,
        },
        run_fields={
            "origin": "claude_code",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "billing_mode": "subscription_included",
            "captured_at": RECENT,
        },
        path=path,
    )
    # codex_cli run-level-only: OpenAI convention (input includes cache).
    upsert_run_facts(
        "codex_cli:session-anon:loop0",
        {
            "origin": "codex_cli",
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "billing_mode": "subscription_included",
            "input_tokens": 200_000,
            "output_tokens": 2_000,
            "cache_read_tokens": 180_000,
            "cache_write_tokens": 0,
            "captured_at": RECENT,
            "source": "measured",
        },
        path=path,
    )
    # buzz run-level-only claude row.
    upsert_run_facts(
        "claude_code:msg-anon:req-anon",
        {
            "origin": "buzz_agent",
            "provider": "anthropic",
            "model": "claude-opus-5",
            "billing_mode": "subscription_included",
            "lane": "claude",
            "input_tokens": 100,
            "output_tokens": 900,
            "cache_read_tokens": 50_000,
            "cache_write_tokens": 0,
            "captured_at": RECENT,
            "source": "measured",
        },
        path=path,
    )
    # unpriceable: qwen3.8-max-preview (documented-absent PAYG).
    upsert_run_facts(
        "qwen_cli:anon:loop0",
        {
            "origin": "qwen_cli",
            "provider": "qwen",
            "model": "qwen3.8-max-preview",
            "billing_mode": "subscription_included",
            "input_tokens": 500_000,
            "output_tokens": 1_000,
            "cache_read_tokens": 400_000,
            "captured_at": RECENT,
            "source": "measured",
        },
        path=path,
    )
    # hermes_agent call WITHOUT occurred_at (18k live rows carry none —
    # Review 1): it must still land in the payload via the run's time.
    record_llm_call(
        "run-hermes-1",
        1,
        {
            "origin": "hermes_agent",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "input_tokens": 50_000,
            "output_tokens": 1_000,
            "cache_read_tokens": 40_000,
            # occurred_at deliberately absent
        },
        run_fields={
            "origin": "hermes_agent",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "billing_mode": "subscription_included",
            "captured_at": RECENT,
        },
        path=path,
    )
    # out-of-window row: must not appear anywhere.
    record_llm_call(
        "run-old",
        1,
        {
            "origin": "claude_code",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "input_tokens": 999,
            "output_tokens": 999,
            "occurred_at": OLD,
        },
        run_fields={"origin": "claude_code", "captured_at": OLD},
        path=path,
    )
    return path


def test_totals_derive_billable_input_and_costs(tmp_path: Path) -> None:
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    assert payload["contract"] == CONTRACT_VERSION
    totals = payload["totals"]
    # sonnet-5 call: 1000 in, 500 out, 100k cr, 4k 1h, 6k 5m
    #   = 1000*3 + 500*15 + 100000*.30 + 4000*6 + 6000*3.75 (per 1M)
    sonnet = Decimal("0.003") + Decimal("0.0075") + Decimal("0.03") + Decimal(
        "0.024"
    ) + Decimal("0.0225")
    # codex run: billable input 20k (200k-180k), out 2k, cr 180k at sol rates
    # (upstream openai gpt-5.6-sol entry)
    assert totals["runs"] == 5  # incl. unpriceable qwen run
    assert totals["equivalent_usd"] > float(sonnet)
    # subscription everywhere: metered 0, saving == equivalent
    assert totals["metered_usd"] == 0.0
    assert totals["subscription_saving_usd"] == pytest.approx(
        totals["equivalent_usd"]
    )
    cov = totals["cost_coverage"]
    assert cov["numerator"] == 4 and cov["denominator"] == 5


def test_unpriceable_is_not_applicable_never_zero(tmp_path: Path) -> None:
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    series = {entry["key"]: entry for entry in payload["breakdown"]["series"]}
    qwen = series["qwen_cli"]
    assert qwen["equivalent_usd"] == "not applicable"
    assert qwen["cost_coverage"]["numerator"] == 0
    assert qwen["unpriced_tokens"] > 0


def test_cache_hit_rate_uses_self_detecting_prompt(tmp_path: Path) -> None:
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    rate = payload["cache_hit_rate"]
    # cache_read total = 100k + 180k + 50k + 400k + 40k (hermes) = 770_000
    # prompt = billable_input + cache_read
    # billable = 1_000 + 20_000 + 100 + 100_000 + 10_000 = 131_100
    assert rate["numerator"] == 770_000
    assert rate["denominator"] == 770_000 + 131_100
    assert rate["value"] == pytest.approx(770_000 / 901_100, rel=1e-4)


def test_component_shares_carry_tokens_and_cost(tmp_path: Path) -> None:
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    shares = payload["component_shares"]
    assert shares["billable_input"]["token_share"]["denominator"] > 0
    assert shares["output"]["cost_usd"] is not None
    # output is costlier per token than cache: cost share > token share
    assert (
        shares["output"]["cost_share"]["value"]
        > shares["output"]["token_share"]["value"]
    )


def test_daily_and_trend_respect_window(tmp_path: Path) -> None:
    # The aggregate refresh restamps captured_at to the REAL write time
    # (documented semantics), so the second day is whatever calendar day
    # this test runs on — never a literal. Bracketing the build call keeps
    # the assertion honest across a midnight rollover mid-test.
    before_utc = datetime.now(timezone.utc).date().isoformat()
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    after_utc = datetime.now(timezone.utc).date().isoformat()
    days = {entry["day"] for entry in payload["daily"]}
    # RECENT day + the restamped "today"; OLD is outside every window.
    assert "2026-08-03" in days
    assert days - {"2026-08-03"} in ({before_utc}, {after_utc})
    assert OLD[:10] not in days
    assert payload["trend"]["equivalent_usd_per_day_full"] > 0
    # Same single data day in both windows: the 7d average is 30/7 of the
    # 30d average (denominator convention, documented).
    assert payload["trend"]["equivalent_usd_per_day_7d"] == pytest.approx(
        payload["trend"]["equivalent_usd_per_day_full"] * 30 / 7
    )


def test_distributions_and_top_runs(tmp_path: Path) -> None:
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    dist = payload["distributions"]["tokens_per_run"]
    assert dist["count"] == 5
    assert dist["max"] >= dist["p90"] >= dist["p50"]
    top = payload["top_runs"]
    assert top
    assert top[0]["equivalent_usd"] >= top[-1]["equivalent_usd"]
    assert all("run_id" in entry for entry in top)


def test_levers_have_counterfactual_and_assumption(tmp_path: Path) -> None:
    payload = build_consumption_payload(
        _fact_db(tmp_path), days=30, breakdown="origin", now=NOW
    )
    assert isinstance(payload["levers"], list)
    for lever in payload["levers"]:
        assert lever["assumption"]
        assert "counterfactual_usd" in lever
        assert "plausibility" in lever
        assert lever["savings_usd"] is not None


def test_determinism(tmp_path: Path) -> None:
    path = _fact_db(tmp_path)
    first = build_consumption_payload(path, days=30, breakdown="model", now=NOW)
    second = build_consumption_payload(path, days=30, breakdown="model", now=NOW)
    assert first == second


def test_zero_denominator_empty_db(tmp_path: Path) -> None:
    path = initialize_usage_facts_db(tmp_path / "empty.db")
    payload = build_consumption_payload(path, days=30, breakdown="origin", now=NOW)
    assert payload["totals"]["runs"] == 0
    assert payload["cache_hit_rate"]["value"] == "not applicable"
    assert payload["totals"]["cost_coverage"]["value"] == "not applicable"
    assert payload["distributions"]["tokens_per_run"]["count"] == 0
    assert payload["levers"] == []

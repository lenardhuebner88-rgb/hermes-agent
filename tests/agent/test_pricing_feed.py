from __future__ import annotations

import json

import pytest

from agent.pricing_feed import load_vendored_pricing
from agent.usage_pricing import (
    CanonicalUsage,
    estimate_equivalent_cost,
    estimate_usage_cost,
    get_pricing_entry,
)


def _write_feed(tmp_path, payload) -> "object":
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _meta(**overrides):
    meta = {
        "source_url": "https://example.invalid/feed.json",
        "fetched_at": "2026-07-27T00:00:00+00:00",
        "pricing_version": "test-v1",
    }
    meta.update(overrides)
    return meta


def test_vendored_feed_entries_are_typed_and_traceable():
    entries = load_vendored_pricing()

    assert ("anthropic", "claude-fable-5") in entries
    assert ("xai", "grok-4.5") in entries
    assert ("openai", "gpt-5.3-codex") in entries
    assert ("zai", "glm-5.1") in entries
    assert entries
    for entry in entries.values():
        assert entry.source == "litellm_feed"
        assert entry.fetched_at is not None
        assert entry.fetched_at.tzinfo is not None
        assert entry.pricing_version


def test_feed_reasoning_rate_is_model_specific(tmp_path):
    path = tmp_path / "prices.json"
    payload = {
        "_meta": {
            "source_url": "https://example.invalid/feed.json",
            "fetched_at": "2026-07-27T00:00:00+00:00",
            "pricing_version": "test-v1",
        },
        "models": {
            "dashscope/qwen-reasoning": {
                "litellm_provider": "dashscope",
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                "output_cost_per_reasoning_token": 0.000003,
            },
            "dashscope/qwen-included": {
                "litellm_provider": "dashscope",
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    entries = load_vendored_pricing(path)

    separate = entries[("alibaba", "qwen-reasoning")]
    included = entries[("alibaba", "qwen-included")]
    assert separate.reasoning_billing == "separate_rate"
    assert separate.reasoning_cost_per_million == 3
    assert included.reasoning_billing == "included_in_output"
    assert included.reasoning_cost_per_million is None


def test_xai_oauth_equivalent_uses_the_pricing_table_cache_read_rate():
    """The route and equivalent calculation must read the same canonical row."""
    usage = CanonicalUsage(cache_read_tokens=1_000_000)

    entry = get_pricing_entry("grok-4.5", provider="xai-oauth")
    equivalent = estimate_equivalent_cost("grok-4.5", usage, provider="xai-oauth")

    assert entry is not None
    assert entry.cache_read_cost_per_million == 0.50
    assert equivalent.amount_usd == entry.cache_read_cost_per_million


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("kimi-coding", "k3"),
        ("alibaba-token-plan", "qwen3.8-max-preview"),
    ],
)
def test_subscription_lane_equivalents_have_a_canonical_price(provider, model):
    equivalent = estimate_equivalent_cost(
        model,
        CanonicalUsage(input_tokens=1_000_000),
        provider=provider,
    )

    assert equivalent.amount_usd is not None
    assert equivalent.amount_usd > 0


def test_subscription_actual_cost_stays_zero_while_equivalent_is_priced():
    usage = CanonicalUsage(input_tokens=1_000_000)

    actual = estimate_usage_cost("k3", usage, provider="kimi-coding")
    equivalent = estimate_equivalent_cost("k3", usage, provider="kimi-coding")

    assert actual.amount_usd == 0
    assert actual.status == "included"
    assert equivalent.amount_usd is not None
    assert equivalent.amount_usd > 0


def test_kanban_db_no_longer_declares_a_second_pricing_table_or_lookup():
    import ast
    from pathlib import Path

    source = Path("hermes_cli/kanban_db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assigned_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "_lookup_model_price_per_mtok" not in declared_names
    assert "_equiv_from_tokens" not in declared_names
    assert "_PRICE_OVERRIDES_PER_MTOK" not in assigned_names


def test_feed_requires_both_meta_and_models_objects(tmp_path):
    """A feed without the models object must fail closed with ValueError —
    not crash later on None.items() halfway through the load."""
    path = _write_feed(tmp_path, {"_meta": _meta()})
    with pytest.raises(ValueError, match="_meta and models"):
        load_vendored_pricing(path)


def test_feed_requires_non_empty_version_and_source_url(tmp_path):
    """Empty-string provenance fields are missing provenance — every
    priced token must stay traceable to a version and a source."""
    for overrides in ({"pricing_version": ""}, {"source_url": ""}):
        path = _write_feed(tmp_path, {"_meta": _meta(**overrides), "models": {}})
        with pytest.raises(ValueError):
            load_vendored_pricing(path)


def test_feed_model_source_falls_back_to_feed_source_url(tmp_path):
    """A model without its own 'source' field inherits the feed-level
    source_url — never the literal string 'None'."""
    models = {
        "claude-x": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000002,
        }
    }
    path = _write_feed(tmp_path, {"_meta": _meta(), "models": models})
    entries = load_vendored_pricing(path)
    assert entries[("anthropic", "claude-x")].source_url == "https://example.invalid/feed.json"


def test_feed_keeps_models_with_only_one_rate(tmp_path):
    """A model priced on ONE side only (input-only or output-only) is
    still a valid entry — the skip rule fires only when BOTH rates are
    missing."""
    models = {
        "input-only": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 0.000001,
        },
        "output-only": {
            "litellm_provider": "anthropic",
            "output_cost_per_token": 0.000002,
        },
    }
    path = _write_feed(tmp_path, {"_meta": _meta(), "models": models})
    entries = load_vendored_pricing(path)
    assert ("anthropic", "input-only") in entries
    assert ("anthropic", "output-only") in entries

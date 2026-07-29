from __future__ import annotations

import json

import pytest

from agent.pricing_feed import load_vendored_pricing


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

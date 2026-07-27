from __future__ import annotations

import json

from agent.pricing_feed import load_vendored_pricing


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

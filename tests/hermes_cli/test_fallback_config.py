"""Characterization tests for ``hermes_cli.fallback_config``.

This module computes the *effective fallback provider chain* — the safety net
the agent falls back to when the primary provider fails. A silent regression
that truncates the chain, drops the legacy ``fallback_model`` key, or
over-dedupes distinct routes means **no fallback when the primary dies**.

All four helpers are pure; we pin their malformed-input handling and the
merge/dedup semantics of ``get_fallback_chain``.
"""
from __future__ import annotations

from hermes_cli.fallback_config import (
    _entry_identity,
    _iter_fallback_entries,
    _normalized_base_url,
    get_fallback_chain,
)

# ─── _normalized_base_url ────────────────────────────────────────────────────


def test_non_string_base_url_normalizes_to_empty():
    assert _normalized_base_url(None) == ""
    assert _normalized_base_url(123) == ""
    assert _normalized_base_url(["https://x.com"]) == ""


def test_base_url_is_stripped_and_trailing_slashes_removed():
    assert _normalized_base_url("https://x.com/") == "https://x.com"
    assert _normalized_base_url("  https://x.com//  ") == "https://x.com"
    assert _normalized_base_url("https://x.com/path/") == "https://x.com/path"
    assert _normalized_base_url("") == ""


# ─── _iter_fallback_entries ──────────────────────────────────────────────────


def test_non_container_raw_yields_no_entries():
    assert _iter_fallback_entries(None) == []
    assert _iter_fallback_entries("anthropic") == []
    assert _iter_fallback_entries(42) == []


def test_single_dict_raw_is_treated_as_one_entry():
    entries = _iter_fallback_entries({"provider": "anthropic", "model": "claude"})
    assert entries == [{"provider": "anthropic", "model": "claude"}]


def test_non_dict_entries_in_list_are_skipped():
    raw = ["anthropic", 5, None, {"provider": "openai", "model": "gpt"}]
    entries = _iter_fallback_entries(raw)
    assert entries == [{"provider": "openai", "model": "gpt"}]


def test_entries_missing_provider_or_model_are_dropped():
    raw = [
        {"provider": "anthropic"},            # no model
        {"model": "gpt"},                      # no provider
        {"provider": "  ", "model": "gpt"},    # blank provider
        {"provider": "openai", "model": ""},   # blank model
        {"provider": "openai", "model": "gpt-4o"},
    ]
    assert _iter_fallback_entries(raw) == [{"provider": "openai", "model": "gpt-4o"}]


def test_provider_and_model_are_stripped_and_preserved():
    entries = _iter_fallback_entries([{"provider": "  openai ", "model": " gpt-4o "}])
    assert entries == [{"provider": "openai", "model": "gpt-4o"}]


def test_base_url_is_normalized_when_present_and_omitted_when_absent():
    entries = _iter_fallback_entries(
        [
            {"provider": "a", "model": "m", "base_url": "https://x.com/"},
            {"provider": "b", "model": "m"},
        ]
    )
    assert entries[0]["base_url"] == "https://x.com"
    assert "base_url" not in entries[1]


def test_invalid_base_url_is_dropped_not_leaked():
    # BUG FIX (R4): previously `dict(entry)` copied the raw base_url first and the
    # `if base_url:` guard only OVERWROTE with the normalized value when that was
    # truthy — so a non-str / whitespace base_url (normalizes to "", falsy) leaked
    # the raw ORIGINAL into the chain (e.g. int 123), and a downstream consumer that
    # trusts entry["base_url"] is a string would break. Now an invalid base_url is
    # DROPPED so entry["base_url"], when present, is always a valid normalized str.
    # Valid base_url and the no-base_url case are unchanged (see the tests above),
    # and the dedup identity is unaffected (both invalid forms normalize to "").
    entries = _iter_fallback_entries(
        [
            {"provider": "c", "model": "m", "base_url": 123},
            {"provider": "d", "model": "m", "base_url": "   "},
        ]
    )
    assert "base_url" not in entries[0]
    assert "base_url" not in entries[1]


def test_extra_keys_survive_normalization():
    entries = _iter_fallback_entries(
        [{"provider": "a", "model": "m", "api_key_env": "FOO_KEY"}]
    )
    assert entries[0]["api_key_env"] == "FOO_KEY"


# ─── _entry_identity (dedup key) ─────────────────────────────────────────────


def test_identity_is_case_insensitive_and_base_url_normalized():
    a = _entry_identity({"provider": "OpenAI", "model": "GPT-4o", "base_url": "https://x.com/"})
    b = _entry_identity({"provider": "openai", "model": "gpt-4o", "base_url": "https://x.com"})
    assert a == b


def test_identity_distinguishes_different_routes():
    base = {"provider": "openai", "model": "gpt-4o"}
    assert _entry_identity({**base, "base_url": "https://a.com"}) != _entry_identity(
        {**base, "base_url": "https://b.com"}
    )


# ─── get_fallback_chain (merge + dedup) ──────────────────────────────────────


def test_none_or_empty_config_yields_empty_chain():
    assert get_fallback_chain(None) == []
    assert get_fallback_chain({}) == []


def test_fallback_providers_order_is_preserved():
    config = {
        "fallback_providers": [
            {"provider": "anthropic", "model": "claude"},
            {"provider": "openai", "model": "gpt"},
        ]
    }
    chain = get_fallback_chain(config)
    assert [e["provider"] for e in chain] == ["anthropic", "openai"]


def test_legacy_fallback_model_is_appended_after_fallback_providers():
    config = {
        "fallback_providers": [{"provider": "anthropic", "model": "claude"}],
        "fallback_model": {"provider": "openai", "model": "gpt"},
    }
    chain = get_fallback_chain(config)
    assert [e["provider"] for e in chain] == ["anthropic", "openai"]


def test_duplicate_route_across_keys_is_deduped():
    # Same provider/model/base_url in both keys → legacy entry dropped.
    config = {
        "fallback_providers": [{"provider": "anthropic", "model": "claude"}],
        "fallback_model": {"provider": "ANTHROPIC", "model": " Claude "},
    }
    chain = get_fallback_chain(config)
    assert len(chain) == 1
    assert chain[0]["provider"] == "anthropic"


def test_distinct_base_url_routes_are_not_deduped():
    config = {
        "fallback_providers": [
            {"provider": "openai", "model": "gpt", "base_url": "https://a.com"},
            {"provider": "openai", "model": "gpt", "base_url": "https://b.com"},
        ]
    }
    assert len(get_fallback_chain(config)) == 2


def test_returned_entries_are_fresh_copies():
    config = {"fallback_providers": [{"provider": "a", "model": "m"}]}
    chain = get_fallback_chain(config)
    chain[0]["provider"] = "MUTATED"
    # Re-deriving must not see the mutation → copies, not references.
    assert get_fallback_chain(config)[0]["provider"] == "a"

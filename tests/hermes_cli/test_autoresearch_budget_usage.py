"""Characterization tests for autoresearch_budget token-usage parsing.

``_usage_total`` / ``_usage_field`` coerce a provider ``usage`` object (an SDK
object with attributes OR a plain mapping) into int token counts. They sit on
the budget-accounting input path: a silent miscount means the nightly token
budget is under-counted (overspend) or a malformed usage field throws and kills
the accounting run. We pin the attr-vs-mapping resolution, the string coercion,
and the swallow-to-0 fallback on garbage.

TEST-ONLY: budget code is never refactored (per the brief's forbidden list).
"""
from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.autoresearch_budget import _usage_field, _usage_total

# ─── _usage_total ────────────────────────────────────────────────────────────


def test_total_from_object_attribute():
    assert _usage_total(SimpleNamespace(total_tokens=100)) == 100


def test_total_from_mapping():
    assert _usage_total({"total_tokens": 250}) == 250


def test_total_missing_or_none_is_zero():
    assert _usage_total({}) == 0
    assert _usage_total({"total_tokens": None}) == 0
    assert _usage_total(None) == 0
    assert _usage_total(SimpleNamespace()) == 0


def test_total_zero_is_zero():
    assert _usage_total({"total_tokens": 0}) == 0


def test_total_numeric_string_is_coerced():
    assert _usage_total({"total_tokens": "300"}) == 300


def test_total_garbage_string_falls_back_to_zero():
    assert _usage_total({"total_tokens": "not-a-number"}) == 0


def test_total_attribute_takes_precedence_over_mapping():
    # An object exposing the attribute wins even if it is also mapping-like.
    class _Both(dict):
        total_tokens = 7

    assert _usage_total(_Both({"total_tokens": 99})) == 7


# ─── _usage_field (multi-name fallback) ──────────────────────────────────────


def test_field_returns_first_present_name():
    usage = SimpleNamespace(input_tokens=10)
    assert _usage_field(usage, "input_tokens", "prompt_tokens") == 10


def test_field_falls_through_to_next_name_when_first_absent():
    usage = {"prompt_tokens": 20}
    assert _usage_field(usage, "input_tokens", "prompt_tokens") == 20


def test_field_skips_unparseable_value_and_uses_next_name():
    # "bad" fails int() → continue → prompt_tokens=30 wins.
    usage = {"input_tokens": "bad", "prompt_tokens": 30}
    assert _usage_field(usage, "input_tokens", "prompt_tokens") == 30


def test_field_numeric_string_is_coerced():
    assert _usage_field({"input_tokens": "55"}, "input_tokens") == 55


def test_field_none_value_is_treated_as_absent():
    usage = {"input_tokens": None, "prompt_tokens": 12}
    assert _usage_field(usage, "input_tokens", "prompt_tokens") == 12


def test_field_no_names_match_returns_zero():
    assert _usage_field({"other": 1}, "input_tokens", "prompt_tokens") == 0
    assert _usage_field(None, "input_tokens") == 0


def test_field_all_values_unparseable_returns_zero():
    assert _usage_field({"input_tokens": "x", "prompt_tokens": "y"},
                        "input_tokens", "prompt_tokens") == 0

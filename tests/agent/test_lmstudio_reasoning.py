"""Characterization tests for ``agent.lmstudio_reasoning.resolve_lmstudio_effort``.

Pure mapping from a user ``reasoning_config`` + a model's ``allowed_options``
onto LM Studio's OpenAI-compatible ``reasoning_effort`` vocabulary, with
aliasing (``off``→``none``, ``on``→``medium``) and clamping against the model's
allowed set. ``None`` means "omit the field".
"""
from __future__ import annotations

from agent.lmstudio_reasoning import resolve_lmstudio_effort

# ─── base resolution (no allowed_options → no clamping) ─────────────────────


def test_default_is_medium_when_no_config():
    assert resolve_lmstudio_effort(None, None) == "medium"
    assert resolve_lmstudio_effort({}, None) == "medium"  # empty dict is falsy


def test_enabled_false_maps_to_none():
    assert resolve_lmstudio_effort({"enabled": False}, None) == "none"


def test_enabled_false_takes_precedence_over_effort():
    assert resolve_lmstudio_effort({"enabled": False, "effort": "high"}, None) == "none"


def test_explicit_effort_is_used():
    assert resolve_lmstudio_effort({"effort": "high"}, None) == "high"
    assert resolve_lmstudio_effort({"effort": "low"}, None) == "low"
    assert resolve_lmstudio_effort({"effort": "minimal"}, None) == "minimal"


def test_effort_is_normalized_case_and_whitespace():
    assert resolve_lmstudio_effort({"effort": "  HIGH  "}, None) == "high"


def test_toggle_aliases_are_mapped():
    assert resolve_lmstudio_effort({"effort": "on"}, None) == "medium"
    assert resolve_lmstudio_effort({"effort": "off"}, None) == "none"


def test_invalid_effort_falls_back_to_medium():
    # An unrecognized effort leaves the default ("medium") in place.
    assert resolve_lmstudio_effort({"effort": "bogus"}, None) == "medium"
    assert resolve_lmstudio_effort({"effort": ""}, None) == "medium"


# ─── clamping against allowed_options ───────────────────────────────────────


def test_clamp_returns_none_when_effort_not_allowed():
    # Toggle model only allows off/on → "high" cannot be honored → omit.
    assert resolve_lmstudio_effort({"effort": "high"}, ["off", "on"]) is None


def test_clamp_allows_mapped_medium_for_toggle_model():
    # allowed ["off","on"] → {"none","medium"}; default medium is allowed.
    assert resolve_lmstudio_effort(None, ["off", "on"]) == "medium"


def test_clamp_allows_graduated_model_option():
    allowed = ["off", "minimal", "low"]
    assert resolve_lmstudio_effort({"effort": "low"}, allowed) == "low"
    assert resolve_lmstudio_effort({"effort": "high"}, allowed) is None


def test_clamp_maps_aliases_in_allowed_set():
    # enabled False → "none"; allowed ["on","off"] → {"medium","none"} → allowed.
    assert resolve_lmstudio_effort({"enabled": False}, ["on", "off"]) == "none"


def test_empty_allowed_options_skips_clamping():
    # Falsy allowed_options (probe failed) → send the resolved effort anyway.
    assert resolve_lmstudio_effort({"effort": "high"}, []) == "high"
    assert resolve_lmstudio_effort({"effort": "high"}, None) == "high"

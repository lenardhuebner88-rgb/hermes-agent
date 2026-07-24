"""Characterization tests for ``agent.lmstudio_reasoning.resolve_lmstudio_effort``.

Pure mapping from a user ``reasoning_config`` + a model's ``allowed_options``
onto LM Studio's OpenAI-compatible ``reasoning_effort`` vocabulary, with
aliasing (``off``→``none``, ``on``→``medium``) and clamping against the model's
allowed set. ``None`` means "omit the field".
"""
from __future__ import annotations

import pytest

from agent.lmstudio_reasoning import resolve_lmstudio_effort
from hermes_constants import VALID_REASONING_EFFORTS

_LM_RANK = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4}

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


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_strong_efforts_clamp_to_lmstudio_ceiling(effort):
    """"max"/"ultra" exceed LM Studio's vocabulary and clamp to its ceiling.

    Without the clamp they miss the valid set, keep the "medium" default and
    resolve *below* "xhigh" -- more requested reasoning yielding less.
    """
    assert resolve_lmstudio_effort({"enabled": True, "effort": effort}, None) == "xhigh"


def test_effort_ladder_is_monotonic():
    """Resolving Hermes' canonical ladder never produces an inversion."""
    resolved = [
        resolve_lmstudio_effort({"enabled": True, "effort": effort}, None)
        for effort in VALID_REASONING_EFFORTS
    ]
    ranks = [_LM_RANK[value] for value in resolved]
    assert ranks == sorted(ranks), dict(zip(VALID_REASONING_EFFORTS, resolved))


@pytest.mark.parametrize(
    "effort,expected",
    [
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
    ],
)
def test_levels_within_lmstudio_vocabulary_are_unchanged(effort, expected):
    """Negative control: the clamp must not disturb levels LM Studio knows."""
    assert resolve_lmstudio_effort({"enabled": True, "effort": effort}, None) == expected


def test_unparseable_effort_still_falls_back_to_medium():
    """Negative control: clamping must not change the unrecognized-input path.

    This is the behaviour "max"/"ultra" were previously conflated with.
    """
    assert resolve_lmstudio_effort({"enabled": True, "effort": "banana"}, None) == "medium"


def test_disabled_reasoning_still_resolves_to_none():
    """Negative control: the clamp sits after the enabled=False short-circuit."""
    assert resolve_lmstudio_effort({"enabled": False, "effort": "max"}, None) == "none"


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_clamped_effort_is_still_checked_against_allowed_options(effort):
    """A clamped value stays subject to the model's published allowed set.

    "max" resolves to "xhigh"; a model that does not publish "xhigh" gets the
    field omitted (``None``) so LM Studio applies the model's own default --
    exactly how a directly-requested "xhigh" already behaves.
    """
    assert (
        resolve_lmstudio_effort(
            {"enabled": True, "effort": effort}, ["off", "minimal", "low"]
        )
        is None
    )
    assert (
        resolve_lmstudio_effort(
            {"enabled": True, "effort": effort}, ["low", "medium", "high", "xhigh"]
        )
        == "xhigh"
    )


def test_clamp_does_not_rewrite_published_allowed_options():
    """The clamp must not leak into allowed_options normalization.

    A model publishing "max" is not claiming LM Studio's request vocabulary
    accepts it; allowed_options passes through untouched, so a resolved
    "xhigh" that the model does not publish is still omitted.
    """
    assert resolve_lmstudio_effort({"enabled": True, "effort": "max"}, ["max"]) is None

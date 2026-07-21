"""Edge-path characterization for ``hermes_cli.timeouts`` (additive).

``tests/hermes_cli/test_timeouts.py`` covers the config-driven happy path and a
few invalid values; this file pins the *edge paths* of the pure helpers and the
defensive type-guards around config loading that the happy-path tests never hit.

Autonomy relevance: these functions are the provider request/stale **timeout
guard**. A silent regression that returns ``nan``/``inf`` or silently disables
the timeout (``None``) lets a provider call hang indefinitely or get killed
prematurely — degraded autonomy with no visible error.
"""
from __future__ import annotations

import math

from hermes_cli.timeouts import (
    _coerce_timeout,
    _get_model_config,
    get_provider_request_timeout,
    get_provider_stale_timeout,
)

# ─── _coerce_timeout: the raw value → float|None coercion ────────────────────


def test_none_and_non_numeric_string_return_none():
    assert _coerce_timeout(None) is None
    assert _coerce_timeout("fast") is None
    assert _coerce_timeout("") is None


def test_numeric_string_and_whitespace_are_parsed():
    assert _coerce_timeout("30") == 30.0
    assert _coerce_timeout("  30  ") == 30.0
    assert _coerce_timeout(30) == 30.0


def test_non_positive_values_disable_the_timeout():
    assert _coerce_timeout(0) is None
    assert _coerce_timeout(-5) is None
    assert _coerce_timeout(False) is None  # bool False → 0.0 → disabled


def test_bool_true_coerces_to_one_second():
    # CHARACTERIZATION quirk: bool is an int subclass → True coerces to 1.0.
    assert _coerce_timeout(True) == 1.0


def test_nan_and_inf_pass_through_unguarded():
    # CHARACTERIZATION / latent edge: `nan <= 0` and `inf <= 0` are both False,
    # so the `<= 0` guard does NOT catch them — nan/inf are returned as-is and
    # would be handed to the HTTP client. Pinned as current behavior; a future
    # guard would flip these to None.
    assert math.isnan(_coerce_timeout(float("nan")))
    assert _coerce_timeout(float("inf")) == float("inf")


# ─── _get_model_config: per-model override lookup guards ─────────────────────


def test_no_model_returns_none():
    assert _get_model_config({}, None) is None
    assert _get_model_config({"models": {"x": {}}}, "") is None


def test_model_present_but_models_not_a_dict_returns_empty_dict():
    # Non-dict `models` → the lookup degrades to an empty dict (not None),
    # which the caller treats as "no override" and falls through to provider level.
    assert _get_model_config({"models": [1, 2]}, "x") == {}


def test_model_value_that_is_not_a_dict_returns_none():
    assert _get_model_config({"models": {"x": "oops"}}, "x") is None


def test_valid_model_config_is_returned():
    cfg = {"models": {"x": {"timeout_seconds": 42}}}
    assert _get_model_config(cfg, "x") == {"timeout_seconds": 42}


# ─── defensive guards around config loading ──────────────────────────────────


def test_empty_provider_id_short_circuits_before_loading_config(monkeypatch):
    # The empty-provider_id guard must return None WITHOUT reading config.
    # Pin the short-circuit with a call-spy: if the guard were removed, the
    # config load below would run (and the spy would return a real timeout,
    # flipping the result away from None).
    calls = {"n": 0}

    def _spy():
        calls["n"] += 1
        return {"providers": {"anthropic": {"request_timeout_seconds": 30}}}

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", _spy)
    assert get_provider_request_timeout("") is None
    assert get_provider_stale_timeout("") is None
    assert calls["n"] == 0  # guard short-circuited; config never loaded
    # Sanity: a real provider id DOES load config and resolve the timeout.
    assert get_provider_request_timeout("anthropic") == 30.0
    assert calls["n"] == 1


def test_config_load_exception_returns_none(monkeypatch):
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)
    assert get_provider_request_timeout("anthropic") is None
    assert get_provider_stale_timeout("anthropic") is None


def test_providers_not_a_dict_returns_none(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"providers": ["anthropic"]}
    )
    assert get_provider_request_timeout("anthropic") is None


def test_provider_config_not_a_dict_returns_none(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"providers": {"anthropic": "oops"}},
    )
    assert get_provider_request_timeout("anthropic") is None
    assert get_provider_stale_timeout("anthropic") is None


def test_non_dict_top_level_config_returns_none(monkeypatch):
    # load_config_readonly returning a non-dict (e.g. a list) must not raise.
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: ["nope"])
    assert get_provider_request_timeout("anthropic") is None

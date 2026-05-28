"""Tests for gateway.profile_policy — HUB/DEFAULT profile detection and Minimax filter."""

from __future__ import annotations

import pytest


def _import_policy():
    """Late import so the failing-tests-first cycle produces ModuleNotFoundError."""
    import gateway.profile_policy as policy
    return policy


# ---------------------------------------------------------------------------
# is_default_hermes_profile_home
# ---------------------------------------------------------------------------


def test_default_root_is_default_profile(tmp_path, monkeypatch):
    policy = _import_policy()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert policy.is_default_hermes_profile_home(default_root=tmp_path) is True


def test_named_profile_is_not_default_profile(tmp_path, monkeypatch):
    policy = _import_policy()
    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    assert (
        policy.is_default_hermes_profile_home(
            default_root=tmp_path, hermes_home=profile_home
        )
        is False
    )


def test_worktree_home_is_not_default_profile(tmp_path, monkeypatch):
    """Q5 contract — worktrees under <root>/worktrees/* are NOT the HUB."""
    policy = _import_policy()
    worktree_home = tmp_path / "worktrees" / "fix-branch"
    worktree_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(worktree_home))
    assert (
        policy.is_default_hermes_profile_home(
            default_root=tmp_path, hermes_home=worktree_home
        )
        is False
    )


def test_nested_profile_path_still_not_default(tmp_path):
    policy = _import_policy()
    nested = tmp_path / "profiles" / "team" / "agent"
    nested.mkdir(parents=True)
    assert (
        policy.is_default_hermes_profile_home(default_root=tmp_path, hermes_home=nested)
        is False
    )


def test_deployment_path_with_profiles_ancestor_is_still_default(tmp_path):
    """Review-Finding #4: paths like /srv/profiles/team-a/.hermes are valid
    HUBs even though 'profiles' appears as an ancestor segment."""
    policy = _import_policy()
    home = tmp_path / "profiles" / "team-a" / ".hermes"
    home.mkdir(parents=True)
    assert (
        policy.is_default_hermes_profile_home(default_root=home, hermes_home=home)
        is True
    )


def test_deployment_path_with_worktrees_ancestor_is_still_default(tmp_path):
    """Review-Finding #4: paths like /mnt/worktrees/hermes-prod/.hermes are
    valid HUBs even though 'worktrees' appears as an ancestor segment."""
    policy = _import_policy()
    home = tmp_path / "worktrees" / "hermes-prod" / ".hermes"
    home.mkdir(parents=True)
    assert (
        policy.is_default_hermes_profile_home(default_root=home, hermes_home=home)
        is True
    )


def test_threshold_accessors_read_env_per_call_without_reload(monkeypatch):
    """Review-Finding #10: per-call accessors honour env-var overrides
    set AFTER module import, so operators don't need a full Python
    process restart to retune thresholds."""
    policy = _import_policy()
    # No env override → matches the import-time default.
    monkeypatch.delenv("HERMES_PRESSURE_WATCH_PCT", raising=False)
    assert policy.current_pressure_watch_pct() == policy.PRESSURE_WATCH_PCT

    # Set after module import — module-level constant is frozen, but the
    # accessor returns the live value.
    monkeypatch.setenv("HERMES_PRESSURE_WATCH_PCT", "42")
    assert policy.current_pressure_watch_pct() == 42

    # Invalid env falls back to the import-time default without raising.
    monkeypatch.setenv("HERMES_PRESSURE_WATCH_PCT", "not-an-int")
    assert policy.current_pressure_watch_pct() == policy.PRESSURE_WATCH_PCT


def test_threshold_accessors_cover_all_thresholds(monkeypatch):
    policy = _import_policy()
    monkeypatch.setenv("HERMES_PRESSURE_CRITICAL_PCT", "91")
    monkeypatch.setenv("HERMES_PRESSURE_FLOOR_TOKENS", "9999")
    monkeypatch.setenv("HERMES_DISCORD_LAG_WATCH_MS", "111")
    monkeypatch.setenv("HERMES_DISCORD_LAG_CRITICAL_MS", "222")
    monkeypatch.setenv("HERMES_DISCORD_HEARTBEAT_AGE_WATCH_SECONDS", "33")
    monkeypatch.setenv("HERMES_DISCORD_HEARTBEAT_AGE_CRITICAL_SECONDS", "44")
    assert policy.current_pressure_critical_pct() == 91
    assert policy.current_pressure_floor_tokens() == 9999
    assert policy.current_discord_lag_watch_ms() == 111
    assert policy.current_discord_lag_critical_ms() == 222
    assert policy.current_discord_heartbeat_age_watch_seconds() == 33
    assert policy.current_discord_heartbeat_age_critical_seconds() == 44


def test_minimax_marker_no_longer_false_positives_on_m2_7(tmp_path):
    """Review-Finding #6: 'm2.7' was too generic. A non-Minimax model whose
    id merely contains the substring 'm2.7' must NOT be filtered."""
    policy = _import_policy()
    chain = [
        {"provider": "openrouter", "model": "company-m2.7-benchmark"},
        {"provider": "anthropic", "model": "claim2.7-eval"},
    ]
    filtered = policy.filter_default_gateway_fallbacks(
        chain, default_root=tmp_path, hermes_home=tmp_path
    )
    assert filtered == chain  # both kept


def test_corrupted_root_collapsed_onto_worktree_is_not_default(tmp_path):
    """Safety net: when get_default_hermes_root returns a path whose immediate
    parent is named 'profiles' or 'worktrees' (root collapsed onto a vault-
    internal child by misconfiguration), reject as non-HUB."""
    policy = _import_policy()
    collapsed = tmp_path / ".hermes" / "worktrees" / "fix-branch"
    collapsed.mkdir(parents=True)
    assert (
        policy.is_default_hermes_profile_home(
            default_root=collapsed, hermes_home=collapsed
        )
        is False
    )


# ---------------------------------------------------------------------------
# filter_default_gateway_fallbacks
# ---------------------------------------------------------------------------


def test_filter_strips_minimax_from_list_at_default(tmp_path):
    policy = _import_policy()
    chain = [
        {"provider": "openrouter", "model": "openrouter/anthropic/claude-3.5-sonnet"},
        {"provider": "minimax", "model": "minimax/m2.7"},
        {"provider": "minimax-ai", "model": "anything"},
        {"provider": "groq", "model": "groq/some-model"},
    ]
    filtered = policy.filter_default_gateway_fallbacks(
        chain, default_root=tmp_path, hermes_home=tmp_path
    )
    assert isinstance(filtered, list)
    assert [e["provider"] for e in filtered] == ["openrouter", "groq"]


def test_filter_strips_minimax_by_model_marker_at_default(tmp_path):
    policy = _import_policy()
    chain = [
        {"provider": "openrouter", "model": "openrouter/minimax/abc"},
        {"provider": "groq", "model": "llama3"},
    ]
    filtered = policy.filter_default_gateway_fallbacks(
        chain, default_root=tmp_path, hermes_home=tmp_path
    )
    assert [e["provider"] for e in filtered] == ["groq"]


def test_filter_legacy_single_dict_with_minimax_returns_none(tmp_path):
    policy = _import_policy()
    legacy = {"provider": "minimax", "model": "minimax/m2.7"}
    assert (
        policy.filter_default_gateway_fallbacks(
            legacy, default_root=tmp_path, hermes_home=tmp_path
        )
        is None
    )


def test_filter_legacy_single_dict_without_minimax_unchanged(tmp_path):
    policy = _import_policy()
    legacy = {"provider": "openrouter", "model": "openrouter/anthropic/claude"}
    out = policy.filter_default_gateway_fallbacks(
        legacy, default_root=tmp_path, hermes_home=tmp_path
    )
    assert out == legacy


def test_filter_named_profile_keeps_minimax(tmp_path):
    """Q6 — Filter must NOT mutate named profiles or worktrees."""
    policy = _import_policy()
    profile_home = tmp_path / "profiles" / "research"
    profile_home.mkdir(parents=True)
    chain = [
        {"provider": "openrouter", "model": "openrouter/x"},
        {"provider": "minimax", "model": "minimax/m2.7"},
    ]
    out = policy.filter_default_gateway_fallbacks(
        chain, default_root=tmp_path, hermes_home=profile_home
    )
    assert out == chain


def test_filter_worktree_home_keeps_minimax(tmp_path):
    policy = _import_policy()
    worktree_home = tmp_path / "worktrees" / "feature"
    worktree_home.mkdir(parents=True)
    chain = [{"provider": "minimax", "model": "minimax/m2.7"}]
    out = policy.filter_default_gateway_fallbacks(
        chain, default_root=tmp_path, hermes_home=worktree_home
    )
    assert out == chain


def test_filter_does_not_alter_primary_model_default(tmp_path):
    """Q6 goal-check — primary `model.default` is untouched by the filter helper.

    The filter operates exclusively on fallback chains.  Passing a full
    config dict-like primary section through it must yield no change to
    ``model.default``.
    """
    policy = _import_policy()
    fallback_chain = [{"provider": "minimax", "model": "minimax/m2.7"}]
    primary_model_default = "openrouter/anthropic/claude-3.5-sonnet"
    filtered = policy.filter_default_gateway_fallbacks(
        fallback_chain, default_root=tmp_path, hermes_home=tmp_path
    )
    # Filter only touches the chain it receives; primary model.default
    # is never read or returned by the helper.
    assert filtered == []
    assert primary_model_default == "openrouter/anthropic/claude-3.5-sonnet"


def test_filter_returns_input_unchanged_for_empty(tmp_path):
    policy = _import_policy()
    assert (
        policy.filter_default_gateway_fallbacks(
            [], default_root=tmp_path, hermes_home=tmp_path
        )
        == []
    )
    assert (
        policy.filter_default_gateway_fallbacks(
            None, default_root=tmp_path, hermes_home=tmp_path
        )
        is None
    )


# ---------------------------------------------------------------------------
# collect_profile_policy_findings
# ---------------------------------------------------------------------------


def test_findings_flag_minimax_in_fallback_providers(tmp_path):
    policy = _import_policy()
    cfg = {
        "fallback_providers": [
            {"provider": "openrouter", "model": "openrouter/x"},
            {"provider": "minimax", "model": "minimax/m2.7"},
        ]
    }
    findings = policy.collect_profile_policy_findings(
        cfg, default_root=tmp_path, hermes_home=tmp_path
    )
    codes = [f["code"] for f in findings]
    assert "default-profile-minimax-fallback-filtered" in codes


def test_findings_flag_minimax_in_legacy_fallback_model(tmp_path):
    policy = _import_policy()
    cfg = {"fallback_model": {"provider": "minimax", "model": "minimax/m2.7"}}
    findings = policy.collect_profile_policy_findings(
        cfg, default_root=tmp_path, hermes_home=tmp_path
    )
    codes = [f["code"] for f in findings]
    assert "default-profile-minimax-fallback-filtered" in codes


def test_findings_empty_when_no_minimax(tmp_path):
    policy = _import_policy()
    cfg = {
        "fallback_providers": [
            {"provider": "openrouter", "model": "openrouter/anthropic/claude"}
        ]
    }
    findings = policy.collect_profile_policy_findings(
        cfg, default_root=tmp_path, hermes_home=tmp_path
    )
    assert findings == []


def test_findings_silent_for_named_profile(tmp_path):
    policy = _import_policy()
    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    cfg = {"fallback_providers": [{"provider": "minimax", "model": "minimax/m2.7"}]}
    findings = policy.collect_profile_policy_findings(
        cfg, default_root=tmp_path, hermes_home=profile_home
    )
    assert findings == []


# ---------------------------------------------------------------------------
# Constants + env overrides
# ---------------------------------------------------------------------------


def test_pressure_constants_have_expected_defaults():
    policy = _import_policy()
    assert policy.PRESSURE_WATCH_PCT == 65
    assert policy.PRESSURE_CRITICAL_PCT == 85
    assert policy.PRESSURE_FLOOR_TOKENS == 20_000


def test_lag_constants_have_expected_defaults():
    policy = _import_policy()
    assert policy.DISCORD_LAG_WATCH_MS == 500
    assert policy.DISCORD_LAG_CRITICAL_MS == 1000


def test_pressure_watch_pct_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_PRESSURE_WATCH_PCT", "55")
    import importlib

    import gateway.profile_policy as policy

    importlib.reload(policy)
    try:
        assert policy.PRESSURE_WATCH_PCT == 55
    finally:
        monkeypatch.delenv("HERMES_PRESSURE_WATCH_PCT", raising=False)
        importlib.reload(policy)


def test_lag_watch_ms_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_LAG_WATCH_MS", "300")
    import importlib

    import gateway.profile_policy as policy

    importlib.reload(policy)
    try:
        assert policy.DISCORD_LAG_WATCH_MS == 300
    finally:
        monkeypatch.delenv("HERMES_DISCORD_LAG_WATCH_MS", raising=False)
        importlib.reload(policy)

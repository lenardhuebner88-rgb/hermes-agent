"""Tests for the Alibaba Coding Plan provider aliases used by Looptap."""

from __future__ import annotations


def test_alibaba_token_plan_alias_resolves_to_coding_plan_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("alibaba-token-plan")

    assert profile is not None
    assert profile.name == "alibaba-coding-plan"
    assert "alibaba-token-plan" in profile.aliases

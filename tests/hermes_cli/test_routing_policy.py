from hermes_cli.routing_policy import (
    DEFAULT_ROUTING_CANDIDATES,
    RoutingIntent,
    choose_routing,
    fallback_providers_for,
)


def _ids(candidates):
    return [(candidate.provider, candidate.model) for candidate in candidates]


def test_default_task_uses_codex_primary_and_cheap_green_fallbacks():
    decision = choose_routing(RoutingIntent(task_type="kanban_default"))

    assert (decision.primary.provider, decision.primary.model, decision.primary.api_mode) == (
        "openai-codex",
        "gpt-5.5",
        "codex_responses",
    )
    assert _ids(decision.fallbacks)[:2] == [
        ("minimax", "MiniMax-M2.7"),
        ("openrouter", "deepseek/deepseek-v4-pro"),
    ]
    assert all(candidate.routing_class != "avoid_or_review" for candidate in decision.fallbacks)
    assert ("openrouter", "anthropic/claude-opus-4.7") not in _ids(decision.fallbacks)
    assert decision.ask_user_for_go is False


def test_conserve_budget_prefers_cheap_fallbacks_without_free_or_opus():
    decision = choose_routing(RoutingIntent(cost_mode="conserve_budget"))

    assert _ids(decision.fallbacks) == [
        ("minimax", "MiniMax-M2.7"),
        ("openrouter", "deepseek/deepseek-v4-pro"),
    ]
    assert all(candidate.source != "openrouter_free" for candidate in decision.fallbacks)
    assert all(candidate.expected_cost_pressure != "high" for candidate in decision.fallbacks)


def test_free_fallback_is_only_added_when_explicitly_allowed_for_noncritical_tasks():
    critical = choose_routing(RoutingIntent(cost_mode="free_ok", allow_free=True, task_criticality="critical"))
    noncritical = choose_routing(RoutingIntent(cost_mode="free_ok", allow_free=True, task_criticality="low"))

    free_id = ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free")
    assert free_id not in _ids(critical.fallbacks)
    assert free_id in _ids(noncritical.fallbacks)
    assert _ids(noncritical.fallbacks)[-1] == free_id


def test_quality_escalation_requires_reason_and_keeps_opus_gated():
    missing_reason = choose_routing(RoutingIntent(cost_mode="quality"))
    with_reason = choose_routing(RoutingIntent(cost_mode="quality", quality_reason="hard code review"))

    sonnet = ("openrouter", "anthropic/claude-sonnet-4.6")
    opus = ("openrouter", "anthropic/claude-opus-4.7")
    assert missing_reason.ask_user_for_go is True
    assert "quality reason" in missing_reason.reason
    assert sonnet not in _ids(missing_reason.fallbacks)
    assert sonnet in _ids(with_reason.fallbacks)
    assert opus not in _ids(with_reason.fallbacks)


def test_explicit_cost_escalation_requires_user_go_for_opus():
    without_go = choose_routing(RoutingIntent(cost_mode="explicit_cost", quality_reason="premium review"))
    with_go = choose_routing(
        RoutingIntent(cost_mode="explicit_cost", quality_reason="premium review", user_cost_go=True)
    )

    opus = ("openrouter", "anthropic/claude-opus-4.7")
    assert without_go.ask_user_for_go is True
    assert "explicit user Go" in without_go.reason
    assert opus not in _ids(without_go.fallbacks)
    assert opus in _ids(with_go.fallbacks)


def test_avoid_or_review_and_yellow_models_are_never_auto_selected():
    decision = choose_routing(RoutingIntent(cost_mode="explicit_cost", quality_reason="full review", user_cost_go=True, allow_free=True))

    selected = {decision.primary, *decision.fallbacks}
    selected_ids = _ids(selected)
    for candidate in DEFAULT_ROUTING_CANDIDATES:
        if candidate.routing_class == "avoid_or_review" or candidate.schema_status == "yellow":
            assert (candidate.provider, candidate.model) not in selected_ids


def test_fallback_provider_export_is_agent_compatible_and_secret_free():
    decision = choose_routing(RoutingIntent(cost_mode="quality", quality_reason="hard code review"))
    exported = fallback_providers_for(decision)

    assert exported
    assert all(set(entry) == {"provider", "model"} for entry in exported)
    assert exported[:2] == [
        {"provider": "minimax", "model": "MiniMax-M2.7"},
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
    ]
    forbidden_keys = {"api_key", "key", "token", "headers", "authorization", "label", "hash"}
    assert all(forbidden_keys.isdisjoint({key.lower() for key in entry}) for entry in exported)

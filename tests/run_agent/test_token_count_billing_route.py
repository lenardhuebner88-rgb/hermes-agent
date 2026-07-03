from types import SimpleNamespace

from agent.conversation_loop import _billing_route_for_token_counts


def test_token_count_billing_route_uses_primary_runtime_after_fallback_mutation():
    agent = SimpleNamespace(
        provider="openai-codex",
        base_url="https://api.openai.com/v1",
        _primary_runtime={
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
        },
    )

    assert _billing_route_for_token_counts(agent) == (
        "anthropic",
        "https://api.anthropic.com",
    )


def test_token_count_billing_route_falls_back_to_current_runtime_without_snapshot():
    agent = SimpleNamespace(
        provider="openai-codex",
        base_url="https://api.openai.com/v1",
    )

    assert _billing_route_for_token_counts(agent) == (
        "openai-codex",
        "https://api.openai.com/v1",
    )

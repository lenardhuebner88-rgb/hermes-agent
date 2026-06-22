"""Regression: explicit base_url override must drive transport detection.

When a built-in provider whose *resolved* credential endpoint speaks Anthropic
Messages (e.g. ``kimi-coding`` with an ``sk-kimi-`` key → api.kimi.com/coding)
is reached with an explicit OpenAI-wire ``base_url`` override — the shape used
by ``fallback_model`` / ``custom_providers`` entries that route through a
built-in provider name but target a user-specified endpoint — the auxiliary
client must be a plain OpenAI client, NOT rewrapped as Anthropic.

Before the fix, ``_wrap_if_needed`` received the stale resolved raw base URL
(api.kimi.com/coding, Anthropic-wire) even though ``explicit_base_url`` pointed
at an OpenAI-wire endpoint, so the client was wrongly rewrapped as
``AnthropicAuxiliaryClient`` and every aux call spoke the wrong wire.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
        "KIMI_API_KEY", "KIMI_CODING_API_KEY", "KIMI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_explicit_openai_wire_override_not_rewrapped_anthropic(monkeypatch, tmp_path):
    from agent.auxiliary_client import (
        resolve_provider_client,
        AnthropicAuxiliaryClient,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # sk-kimi- prefix redirects the resolved credential base_url to the Kimi
    # Coding Plan endpoint (api.kimi.com/coding), which speaks Anthropic
    # Messages — so the provider's effective resolved default is Anthropic-wire.
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-faketesttoken123")

    client, _model = resolve_provider_client(
        "kimi-coding",
        "kimi-for-coding",
        explicit_base_url="https://litellm.example.com/v1",  # OpenAI-wire override
    )

    assert client is not None, "Should resolve a client"
    assert not isinstance(client, AnthropicAuxiliaryClient), (
        "Explicit OpenAI-wire base_url override must not be rewrapped as "
        f"Anthropic; got {type(client).__name__}"
    )
    assert "litellm.example.com" in str(getattr(client, "base_url", "")), (
        "Resolved client must point at the explicit override endpoint"
    )

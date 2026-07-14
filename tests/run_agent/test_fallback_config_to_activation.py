"""Prove that `fallback_providers[0]` is the entry the real fallback path picks.

Why this file exists
--------------------
The live check that was supposed to prove the fallback chain works — temporarily
pointing the gateway at an invalid model route — came back HTTP 200 with no
switch telemetry. A 200 proves nothing: the primary never failed, so no failover
ever ran. `fallback_providers[0]` was never shown to be the selected entry.

The existing chain tests (``test_provider_fallback.py``) hand-build
``agent._fallback_chain`` in memory, so they cannot catch a break in the wiring
that carries ``config.yaml`` into that chain. This file walks the whole real
path end to end::

    config.yaml (fallback_providers)
      → hermes_cli.fallback_config.get_fallback_chain
      → gateway.run.GatewayRunner._load_fallback_model   (+ profile policy filter)
      → AIAgent(fallback_model=…)  → agent._fallback_chain
      → agent.error_classifier.classify_api_error(real provider error)
      → agent.chat_completion_helpers.try_activate_fallback

and asserts the provider/model that actually gets selected is entry 0 of the
configured list.

Safety: no production credentials, no network, no invented providers. The only
injected seam is ``resolve_provider_client`` — the documented boundary where
credentials/clients are resolved (the same seam the existing fallback tests use)
— so everything from chain ordering through dedup, model normalisation, api-mode
detection, client swap and telemetry is production code. The provider/model pairs
are the ones the repo's own fallback tests already use.
"""

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest
import yaml

from agent.error_classifier import classify_api_error
from run_agent import AIAgent

# Same real provider/model pairs the existing fallback tests use.
FB0 = {"provider": "openai", "model": "gpt-4o", "key_env": "HERMES_TEST_FB0_KEY"}
FB1 = {"provider": "zai", "model": "glm-4.7", "key_env": "HERMES_TEST_FB1_KEY"}

# The primary the gateway is running on when the failure hits — deliberately a
# different provider AND model, so the chain's "skip entries that match the
# backend that just failed" dedup does not apply.
PRIMARY_PROVIDER = "openrouter"
PRIMARY_MODEL = "z-ai/glm-4.6"


@pytest.fixture
def hermes_home_with_chain(tmp_path, monkeypatch):
    """An isolated HERMES_HOME whose config.yaml carries a real fallback chain."""
    import gateway.run as gateway_run

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"fallback_providers": [FB0, FB1]}),
        encoding="utf-8",
    )
    # GatewayRunner._load_fallback_model reads config.yaml relative to this.
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    return tmp_path


def _make_agent(fallback_chain):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_chain,
        )
    agent.client = MagicMock()
    agent.provider = PRIMARY_PROVIDER
    agent.model = PRIMARY_MODEL
    agent.base_url = "https://openrouter.ai/api/v1"
    return agent


def _primary_failure():
    """A real openai SDK error object, as the primary provider would raise."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(500, request=request)
    return openai.APIStatusError(
        "Internal Server Error", response=response, body=None
    )


def test_config_chain_reaches_agent_in_configured_order(hermes_home_with_chain):
    """config.yaml order survives get_fallback_chain → gateway → agent."""
    from gateway.run import GatewayRunner

    chain = GatewayRunner._load_fallback_model()

    assert chain == [FB0, FB1], "gateway must hand the chain over in config order"

    agent = _make_agent(chain)
    assert agent._fallback_chain == [FB0, FB1]
    assert agent._fallback_index == 0
    # `_fallback_model` is what the rest of the runtime reports as "the fallback".
    assert agent._fallback_model == FB0


def test_injected_primary_failure_selects_fallback_providers_zero(
    hermes_home_with_chain, caplog
):
    """A classified primary failure activates fallback_providers[0] — not just 'a' fallback."""
    import logging

    from gateway.run import GatewayRunner

    agent = _make_agent(GatewayRunner._load_fallback_model())

    # Real classifier on a real provider error — production decides the reason.
    classified = classify_api_error(
        _primary_failure(), provider=agent.provider, model=agent.model
    )

    resolved = []

    def _fake_resolve(provider, model=None, **kwargs):
        """Stand in for credential/client resolution only. Records the ask."""
        resolved.append((provider, model))
        client = MagicMock()
        client.base_url = "https://api.openai.com/v1"
        client.api_key = "injected-not-a-real-credential"
        return client, model

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=_fake_resolve),
        caplog.at_level(logging.INFO),
    ):
        activated = agent._try_activate_fallback(reason=classified.reason)

    assert activated is True

    # The heart of the AC: the FIRST entry of fallback_providers is the one the
    # real path asked for and the one the agent switched to.
    assert resolved[0] == (FB0["provider"], FB0["model"]), (
        f"expected fallback_providers[0] to be selected first, got {resolved}"
    )
    assert (FB1["provider"], FB1["model"]) not in resolved, (
        "entry 1 must not be touched while entry 0 resolves cleanly"
    )
    assert agent.provider == FB0["provider"]
    assert agent.model == FB0["model"]
    assert agent._fallback_activated is True
    # Index advanced past entry 0 → a later failure continues at entry 1.
    assert agent._fallback_index == 1

    # Switch telemetry — the thing the HTTP-200 live probe could never show.
    telemetry = [r.getMessage() for r in caplog.records if "Fallback activated" in r.getMessage()]
    assert len(telemetry) == 1, "the switch must be observable in the logs"
    assert PRIMARY_MODEL in telemetry[0] and FB0["model"] in telemetry[0]


def test_second_failure_advances_to_fallback_providers_one(hermes_home_with_chain):
    """Chain order is real: entry 0 first, entry 1 only after entry 0 also fails."""
    from gateway.run import GatewayRunner

    agent = _make_agent(GatewayRunner._load_fallback_model())
    classified = classify_api_error(
        _primary_failure(), provider=agent.provider, model=agent.model
    )

    resolved = []

    def _fake_resolve(provider, model=None, **kwargs):
        resolved.append((provider, model))
        client = MagicMock()
        client.base_url = "https://api.openai.com/v1"
        client.api_key = "injected-not-a-real-credential"
        return client, model

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=_fake_resolve):
        assert agent._try_activate_fallback(reason=classified.reason) is True
        assert agent.model == FB0["model"]
        # Entry 0 is now the failing backend — fail over again.
        assert agent._try_activate_fallback(reason=classified.reason) is True
        assert agent.provider == FB1["provider"]
        assert agent.model == FB1["model"]
        # Chain exhausted.
        assert agent._try_activate_fallback(reason=classified.reason) is False

    assert resolved == [
        (FB0["provider"], FB0["model"]),
        (FB1["provider"], FB1["model"]),
    ]

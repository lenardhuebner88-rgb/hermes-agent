"""Regression tests: ``hermes -z`` must honor the profile's agent.service_tier.

Live-proven gap (2026-07-27): oneshot never read ``agent.service_tier`` from
the config and passed no ``request_overrides`` to AIAgent, so oneshot
pipelines silently ran on the default tier while the interactive CLI and
kanban workers honored ``fast``/``flex``. The fix lives in the fork-owned
``hermes_cli/oneshot_service_tier`` module, wired into upstream-owned
``hermes_cli/oneshot.py`` with one-line calls.

The integration tests drive the real oneshot path (``_run_agent`` → real
``AIAgent`` → ``_build_api_kwargs``) with the OpenAI transport mocked and
the final request kwargs captured — the in-test analog of the httpx capture
used to prove the bug live. ``run_oneshot``'s wrapper side effects
(``logging.disable``, env mutation, stateless-channel ContextVar) are
process-global and intentionally bypassed to keep the test process hermetic.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.oneshot_service_tier import (
    apply_oneshot_service_tier_audit,
    resolve_oneshot_service_tier_kwargs,
)


# --------------------------------------------------------------------------
# 1. Helper unit tests — parsing semantics identical to the interactive CLI
# --------------------------------------------------------------------------
class TestResolveOneshotServiceTierKwargs:
    def test_fast_yields_priority_kwargs_for_openai_model(self):
        cfg = {"agent": {"service_tier": "fast"}}
        out = resolve_oneshot_service_tier_kwargs(cfg, "gpt-4.1")
        assert out == {
            "service_tier": "priority",
            "request_overrides": {"service_tier": "priority"},
        }

    def test_vendor_prefixed_openai_model(self):
        cfg = {"agent": {"service_tier": "priority"}}
        out = resolve_oneshot_service_tier_kwargs(cfg, "openai/gpt-5.5")
        assert out["request_overrides"] == {"service_tier": "priority"}

    def test_anthropic_fast_model_yields_speed_override(self):
        cfg = {"agent": {"service_tier": "on"}}
        out = resolve_oneshot_service_tier_kwargs(cfg, "claude-opus-4-6")
        assert out == {
            "service_tier": "priority",
            "request_overrides": {"speed": "fast"},
        }

    def test_codex_model_excluded_from_overrides(self):
        # Codex Responses API rejects service_tier — overrides must be None
        # (mirrors cli_agent_setup_mixin, which passes the tier marker anyway).
        cfg = {"agent": {"service_tier": "fast"}}
        out = resolve_oneshot_service_tier_kwargs(cfg, "gpt-5.3-codex")
        assert out == {"service_tier": "priority", "request_overrides": None}

    def test_unset_service_tier_returns_empty(self):
        assert resolve_oneshot_service_tier_kwargs({"agent": {}}, "gpt-4.1") == {}
        assert resolve_oneshot_service_tier_kwargs({}, "gpt-4.1") == {}
        assert resolve_oneshot_service_tier_kwargs(
            {"agent": {"service_tier": ""}}, "gpt-4.1"
        ) == {}

    def test_disabled_values_return_empty(self):
        for raw in ("normal", "default", "standard", "off", "none"):
            assert resolve_oneshot_service_tier_kwargs(
                {"agent": {"service_tier": raw}}, "gpt-4.1"
            ) == {}, raw

    def test_invalid_value_warns_and_is_ignored(self, caplog):
        cfg = {"agent": {"service_tier": "ludicrous"}}
        with caplog.at_level(logging.WARNING):
            out = resolve_oneshot_service_tier_kwargs(cfg, "gpt-4.1")
        assert out == {}
        assert any("service_tier" in r.message for r in caplog.records)

    def test_non_dict_agent_section_returns_empty(self):
        assert resolve_oneshot_service_tier_kwargs(
            {"agent": "oops"}, "gpt-4.1"
        ) == {}


class TestApplyOneshotServiceTierAudit:
    def test_fills_tier_from_top_level_override(self):
        agent = SimpleNamespace(request_overrides={"service_tier": "priority"})
        out = apply_oneshot_service_tier_audit({"service_tier": None}, agent)
        assert out["service_tier"] == "priority"

    def test_fills_tier_from_anthropic_speed_override(self):
        agent = SimpleNamespace(request_overrides={"speed": "fast"})
        out = apply_oneshot_service_tier_audit({}, agent)
        assert out["service_tier"] == "fast"

    def test_extra_body_tier_wins_and_result_not_mutated(self):
        # turn_finalizer already reported an extra_body tier (e.g. flex) —
        # never overwrite it, never mutate the caller's dict.
        result = {"service_tier": "flex"}
        agent = SimpleNamespace(request_overrides={"service_tier": "priority"})
        out = apply_oneshot_service_tier_audit(result, agent)
        assert out is result
        assert result["service_tier"] == "flex"

    def test_no_overrides_leaves_result_untouched(self):
        result = {"service_tier": None}
        agent = SimpleNamespace(request_overrides=None)
        assert apply_oneshot_service_tier_audit(result, agent) is result


# --------------------------------------------------------------------------
# 2. End-to-end through the oneshot path with transport-level kwargs capture
# --------------------------------------------------------------------------
def _mock_response(content="Done.", finish_reason="stop"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="gpt-4.1", usage=None)


@pytest.fixture
def oneshot_harness(tmp_path, monkeypatch):
    """Drive hermes_cli.oneshot._run_agent with a captured fake transport.

    Returns (captured_kwargs_list, run_fn). Everything that could touch the
    real environment is isolated: HERMES_HOME + Path.home() point at tmp_path
    (AGENTS.md hard requirement), config/provider resolution and the OpenAI
    client are mocked, no session DB, no memory provider, no tools.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)

    captured: list[dict] = []

    def _run(cfg: dict, model: str = "gpt-4.1", provider: str = "openrouter"):
        captured.clear()
        client = MagicMock()
        client.chat.completions.create.side_effect = lambda **kw: (
            captured.append(kw) or _mock_response()
        )
        runtime = {
            "api_key": "test-key-1234567890",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": provider,
            "requested_provider": provider,
            "api_mode": "chat_completions",
            "credential_pool": None,
        }
        with (
            patch("hermes_cli.config.load_config", return_value=cfg),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value=runtime,
            ),
            patch("hermes_cli.oneshot._create_session_db_for_oneshot", return_value=None),
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI", return_value=client),
            patch("plugins.memory.load_memory_provider", return_value=None),
        ):
            from hermes_cli.oneshot import _run_agent

            return _run_agent("say hi", model=model, provider=provider)

    return captured, _run


class TestOneshotServiceTierEndToEnd:
    def test_fast_profile_sends_priority_tier_on_the_wire(self, oneshot_harness):
        captured, run = oneshot_harness
        cfg = {
            "agent": {"service_tier": "fast"},
            "model": {"default": "gpt-4.1", "provider": "openrouter"},
        }
        response, result = run(cfg)
        assert response == "Done."
        assert captured, "transport was never invoked"
        assert captured[0]["service_tier"] == "priority"
        # The --usage-file audit field reflects the tier that actually went out.
        assert result["service_tier"] == "priority"

    def test_unset_service_tier_sends_no_override(self, oneshot_harness):
        captured, run = oneshot_harness
        cfg = {"model": {"default": "gpt-4.1", "provider": "openrouter"}}
        response, result = run(cfg)
        assert response == "Done."
        assert captured, "transport was never invoked"
        assert "service_tier" not in captured[0]
        assert result.get("service_tier") is None

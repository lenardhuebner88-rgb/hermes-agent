"""Characterization tests for two autonomy guards.

* ``agent.tool_executor._resolve_concurrent_tool_timeout`` — resolves the
  ``HERMES_CONCURRENT_TOOL_TIMEOUT_S`` env var into the deadline for a batch of
  concurrent tool calls. A silent regression here either disables the deadline
  (a wedged tool hangs the whole agent loop / leaks threads that block CLI
  exit) or sets it absurdly low (tools killed prematurely).

* ``agent.conversation_loop._ollama_context_limit_error`` — returns a remediation
  message when Ollama is loaded with too little runtime context for tool use.
  Without it the agent loops on empty Ollama responses, burning the iteration
  budget with no user-visible explanation.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

from agent.conversation_loop import (
    MINIMUM_CONTEXT_LENGTH,
    _ollama_context_limit_error,
)
from agent.tool_executor import (
    _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S,
    _resolve_concurrent_tool_timeout,
)

# ─── _resolve_concurrent_tool_timeout ────────────────────────────────────────


def test_unset_env_uses_default(monkeypatch):
    monkeypatch.delenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
    assert _resolve_concurrent_tool_timeout() == _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S == 420.0


def test_blank_env_uses_default(monkeypatch):
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "   ")
    assert _resolve_concurrent_tool_timeout() == 420.0


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "fast")
    assert _resolve_concurrent_tool_timeout() == 420.0


def test_non_positive_disables_the_timeout(monkeypatch):
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "0")
    assert _resolve_concurrent_tool_timeout() is None
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "-5")
    assert _resolve_concurrent_tool_timeout() is None


def test_positive_value_is_used(monkeypatch):
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "60")
    assert _resolve_concurrent_tool_timeout() == 60.0


def test_nan_and_inf_pass_through_unguarded(monkeypatch):
    # CHARACTERIZATION / latent edge (mirrors timeouts._coerce_timeout): the
    # `<= 0` guard does not catch nan/inf, so they are returned as-is.
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "nan")
    assert math.isnan(_resolve_concurrent_tool_timeout())
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "inf")
    assert _resolve_concurrent_tool_timeout() == float("inf")


# ─── _ollama_context_limit_error ─────────────────────────────────────────────


def _agent(*, tools=None, num_ctx=None, model="glm-4", base_url="http://localhost:11434",
           provider="ollama", session_id="sess-1"):
    return SimpleNamespace(
        tools=tools,
        _ollama_num_ctx=num_ctx,
        model=model,
        base_url=base_url,
        provider=provider,
        session_id=session_id,
    )


def test_no_tools_returns_none():
    # Without tools there is no tool-use context requirement to enforce.
    assert _ollama_context_limit_error(_agent(tools=None, num_ctx=1024), 5000) is None
    assert _ollama_context_limit_error(_agent(tools=[], num_ctx=1024), 5000) is None


def test_non_positive_or_non_int_ctx_returns_none():
    tools = [{"name": "t"}]
    assert _ollama_context_limit_error(_agent(tools=tools, num_ctx=None), 5000) is None
    assert _ollama_context_limit_error(_agent(tools=tools, num_ctx=0), 5000) is None
    assert _ollama_context_limit_error(_agent(tools=tools, num_ctx=-1), 5000) is None
    # A string ctx (not an int) is not acted on.
    assert _ollama_context_limit_error(_agent(tools=tools, num_ctx="8192"), 5000) is None


def test_ctx_at_or_above_minimum_returns_none():
    tools = [{"name": "t"}]
    assert _ollama_context_limit_error(_agent(tools=tools, num_ctx=MINIMUM_CONTEXT_LENGTH), 5000) is None
    assert _ollama_context_limit_error(
        _agent(tools=tools, num_ctx=MINIMUM_CONTEXT_LENGTH + 1000), 5000
    ) is None


def test_small_ctx_with_tools_returns_remediation_message():
    tools = [{"name": "terminal"}, {"name": "read_file"}]
    msg = _ollama_context_limit_error(_agent(tools=tools, num_ctx=2048, model="glm-4"), 5000)
    assert msg is not None
    assert "glm-4" in msg
    assert "2,048" in msg  # runtime ctx formatted with thousands separators
    assert f"{MINIMUM_CONTEXT_LENGTH:,}" in msg
    assert "ollama_num_ctx" in msg  # remediation points at the config key


def test_missing_model_falls_back_to_placeholder():
    tools = [{"name": "t"}]
    msg = _ollama_context_limit_error(_agent(tools=tools, num_ctx=1024, model=""), 5000)
    assert "the selected model" in msg

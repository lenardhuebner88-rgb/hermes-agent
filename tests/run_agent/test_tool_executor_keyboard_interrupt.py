"""Regression: KeyboardInterrupt during SEQUENTIAL tool execution must not
orphan the persisted assistant(tool_calls) block.

``run_conversation`` flushes the assistant tool-call turn to the session DB
BEFORE tool execution starts (#49045).  The sequential path's
``except KeyboardInterrupt`` handlers used to re-raise after emitting only
the telemetry hook — never appending a tool result — so a Ctrl-C landing
mid-tool persisted a transcript whose tail is ``assistant(tool_calls=[...])``
with zero tool results.  ``KeyboardInterrupt`` is a ``BaseException``: the
``except Exception`` backfill in ``run_conversation`` never fires, and none
of the message-repair passes fix this shape, so the session stayed broken
for every following turn.

The concurrent path (``_run_tool``) already contained the interrupt and
stored a cancelled result; these tests pin the sequential path to the same
contract, for both its branches (quiet-mode and default):

    * the interrupted call gets a proper cancelled tool_result;
    * the remaining calls in the batch get skip results (via the existing
      top-of-loop interrupt check);
    * the interrupt flag is set so the turn ends as interrupted;
    * no KeyboardInterrupt propagates out of the executor.
"""

from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent(quiet_mode: bool):
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-test-home-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=quiet_mode,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._flush_messages_to_session_db = MagicMock()
    return agent


def _mock_tool_call(name="web_search", arguments="{}", call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.parametrize("quiet_mode", [True, False])
def test_keyboard_interrupt_mid_tool_appends_cancelled_results(quiet_mode):
    agent = _make_agent(quiet_mode)
    tool_calls = [
        _mock_tool_call(call_id="c1"),
        _mock_tool_call(call_id="c2"),
    ]
    assistant_message = SimpleNamespace(content="", tool_calls=tool_calls)
    messages: list = []

    def _fake_dispatch(function_name, function_args, effective_task_id, **kwargs):
        if kwargs.get("tool_call_id") == "c1":
            raise KeyboardInterrupt()
        return "should-never-run"

    hook_calls: list = []

    with (
        patch("run_agent.handle_function_call", side_effect=_fake_dispatch),
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
        patch(
            "model_tools._emit_post_tool_call_hook",
            side_effect=lambda **kwargs: hook_calls.append(kwargs),
        ),
    ):
        # Must NOT raise KeyboardInterrupt out of the executor.
        agent._execute_tool_calls_sequential(assistant_message, messages, "task-1")

    # The cancelled telemetry hook fires exactly once for the interrupted
    # call — the fall-through must not double-fire post_tool_call for it.
    cancelled_hooks = [
        h for h in hook_calls
        if h.get("status") == "cancelled" and h.get("tool_call_id") == "c1"
    ]
    assert len(cancelled_hooks) == 1
    assert len([h for h in hook_calls if h.get("tool_call_id") == "c1"]) == 1

    # Every tool_call in the batch got a result — nothing orphaned.
    assert [m["role"] for m in messages] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in messages] == ["c1", "c2"]

    # The interrupted call carries a cancelled result, the follow-up call a
    # skip marker from the top-of-loop interrupt check.
    def _text(m):
        c = m["content"]
        if isinstance(c, list):
            return " ".join(
                str(p.get("text", "")) for p in c if isinstance(p, dict)
            )
        return str(c)

    assert "cancelled" in _text(messages[0]).lower()
    assert "cancelled" in _text(messages[1]).lower() or "skipped" in _text(messages[1]).lower()

    # The turn is flagged interrupted so the loop ends cleanly.
    assert agent._interrupt_requested is True

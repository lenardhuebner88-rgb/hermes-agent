"""Regression: the CONCURRENT tool path reset the memory/skill nudge
counters at the top of its parse loop — before scope/plugin/guardrail
block evaluation — so a ``memory`` (or ``skill_manage``) call that never
executed still silenced the nudge.  A model whose memory calls keep
getting blocked (scope-restricted session, guardrail-halted) was then
never nudged to actually write memory.

The sequential path has an explicit ``_execution_blocked`` guard for
exactly this; these tests pin the concurrent path to the same contract:

    * blocked memory call  → ``_turns_since_memory`` untouched;
    * executed memory call → ``_turns_since_memory`` reset to 0.
"""

from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _make_agent():
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-test-home-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("memory", "web_search"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
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


def _mock_tool_call(name, call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def _run_concurrent(agent, tool_calls):
    assistant_message = SimpleNamespace(content="", tool_calls=tool_calls)
    messages: list = []
    with patch(
        "agent.tool_executor.maybe_persist_tool_result",
        side_effect=lambda **kwargs: kwargs["content"],
    ):
        agent._execute_tool_calls_concurrent(
            assistant_message, messages, "task-1"
        )
    return messages


def test_blocked_memory_call_does_not_reset_nudge_counter():
    agent = _make_agent()
    agent._turns_since_memory = 7

    with patch(
        "hermes_cli.plugins.resolve_pre_tool_block",
        return_value="memory is blocked in this session",
    ):
        messages = _run_concurrent(
            agent,
            [
                _mock_tool_call("memory", "c1"),
                _mock_tool_call("memory", "c2"),
            ],
        )

    # Both calls got (blocked) tool results, but the nudge counter is intact.
    assert [m["role"] for m in messages] == ["tool", "tool"]
    assert agent._turns_since_memory == 7


def test_executed_memory_call_resets_nudge_counter():
    agent = _make_agent()
    agent._turns_since_memory = 7

    with (
        patch(
            "hermes_cli.plugins.resolve_pre_tool_block",
            return_value=None,
        ),
        patch.object(agent, "_invoke_tool", return_value="memory ok"),
    ):
        messages = _run_concurrent(
            agent,
            [
                _mock_tool_call("memory", "c1"),
                _mock_tool_call("web_search", "c2"),
            ],
        )

    assert [m["role"] for m in messages] == ["tool", "tool"]
    assert agent._turns_since_memory == 0

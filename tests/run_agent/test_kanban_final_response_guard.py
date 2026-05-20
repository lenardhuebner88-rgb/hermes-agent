"""Regression tests for Kanban worker final-response protocol guard."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_agent
from hermes_cli import kanban_db as kb
from run_agent import (
    _kanban_terminal_recovery_prompt,
    _kanban_task_still_running,
    _maybe_block_kanban_task_after_final_response,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task_with_env(monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs terminal call", assignee="reviewer")
        kb.claim_task(conn, tid, claimer="host:test-worker")
        run = kb.latest_run(conn, tid)
    assert run is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    return tid, run.id


def test_final_response_guard_blocks_running_kanban_task(kanban_home, monkeypatch):
    tid, run_id = _running_task_with_env(monkeypatch)

    blocked = _maybe_block_kanban_task_after_final_response(
        tid,
        "Reviewer prose without a terminal kanban call.",
    )

    assert blocked is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "blocked"
    assert "worker-final-response-without-terminal-call" in run.summary
    assert "Reviewer prose without a terminal kanban call" in run.summary
    assert run.id == run_id
    assert run.outcome == "blocked"
    assert any(e.kind == "blocked" for e in events)


def test_final_response_guard_does_not_double_mutate_completed_task(kanban_home, monkeypatch):
    tid, run_id = _running_task_with_env(monkeypatch)
    with kb.connect() as conn:
        assert kb.complete_task(
            conn,
            tid,
            result="completed via kanban_complete",
            expected_run_id=run_id,
        )

    blocked = _maybe_block_kanban_task_after_final_response(
        tid,
        "Post-complete prose should not matter.",
    )

    assert blocked is False
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "done"
    assert [e.kind for e in events].count("blocked") == 0


def test_running_kanban_task_detection_tracks_terminal_state(kanban_home, monkeypatch):
    tid, run_id = _running_task_with_env(monkeypatch)

    assert _kanban_task_still_running(tid) is True

    with kb.connect() as conn:
        assert kb.complete_task(
            conn,
            tid,
            result="completed via kanban_complete",
            expected_run_id=run_id,
        )

    assert _kanban_task_still_running(tid) is False


def test_terminal_recovery_prompt_preserves_task_id_prose_and_demands_tool_call():
    prompt = _kanban_terminal_recovery_prompt(
        "Reviewer prose without terminal call.\nSecond line.",
        task_id="t_reviewer123",
    )

    assert "t_reviewer123" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "Do not provide more final prose" in prompt
    assert "Reviewer prose without terminal call. Second line." in prompt


def test_reviewer_lane_e2e_blocks_after_bounded_recovery_without_terminal_tool(
    kanban_home, monkeypatch
):
    tid, run_id = _running_task_with_env(monkeypatch)
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=f"APPROVED prose for {tid}, but no terminal tool.",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="Still prose after first recovery nudge.",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=17),
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="Still prose after second recovery nudge.",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=13, completion_tokens=5, total_tokens=18),
        ),
    ]
    captured_api_messages = []

    def fake_api_call(api_kwargs):
        captured_api_messages.append(copy.deepcopy(api_kwargs["messages"]))
        return responses.pop(0)

    agent = run_agent.AIAgent(
        model="test/model",
        api_key="test-key",
        base_url="http://localhost:1234/v1",
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
        max_iterations=5,
    )
    agent._interruptible_api_call = fake_api_call
    agent._disable_streaming = True
    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    agent._save_session_log = lambda messages: None
    agent.valid_tool_names = {"kanban_complete", "kanban_block"}

    result = agent.run_conversation("review gate task", task_id="test-run")

    assert result["final_response"] == "Still prose after second recovery nudge."
    assert responses == []
    recovery_prompts = [
        message["content"]
        for message in captured_api_messages[-1]
        if isinstance(message, dict) and message.get("_kanban_terminal_recovery_synthetic")
        and message.get("role") == "user"
    ]
    assert len(recovery_prompts) == 2
    assert all(tid in prompt for prompt in recovery_prompts)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "blocked"
    assert run.id == run_id
    assert run.outcome == "blocked"
    assert "worker-final-response-without-terminal-call" in run.summary
    assert "Still prose after second recovery nudge" in run.summary
    assert [e.kind for e in events].count("blocked") == 1


def test_run_conversation_recovers_final_prose_into_terminal_kanban_call(
    kanban_home, monkeypatch
):
    tid, run_id = _running_task_with_env(monkeypatch)

    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="APPROVED but forgot the terminal tool.",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        id="call_terminal",
                        function=SimpleNamespace(
                            name="kanban_complete",
                            arguments='{"task_id":"%s","result":"terminal after nudge"}' % tid,
                        ),
                    )],
                    reasoning_content=None,
                ),
                finish_reason="tool_calls",
            )],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4, total_tokens=16),
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="Done after terminal call.",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4, total_tokens=12),
        ),
    ]

    def fake_api_call(_api_kwargs):
        return responses.pop(0)

    def fake_handle_function_call(name, args, _task_id=None, **_kwargs):
        assert name == "kanban_complete"
        with kb.connect() as conn:
            assert kb.complete_task(
                conn,
                args["task_id"],
                result=args["result"],
                expected_run_id=run_id,
            )
        return "completed"

    monkeypatch.setattr(run_agent, "handle_function_call", fake_handle_function_call)
    agent = run_agent.AIAgent(
        model="test/model",
        api_key="test-key",
        base_url="http://localhost:1234/v1",
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
        max_iterations=5,
    )
    agent._interruptible_api_call = fake_api_call
    agent._disable_streaming = True
    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    agent._save_session_log = lambda messages: None
    agent.valid_tool_names = {"kanban_complete"}

    result = agent.run_conversation("work kanban task", task_id="test-run")

    assert result["final_response"] == "Done after terminal call."
    assert responses == []
    assert any(
        m.get("_kanban_terminal_recovery_synthetic")
        for m in result["messages"]
        if isinstance(m, dict)
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
    assert task.status == "done"
    assert run.outcome == "completed"


def test_final_response_guard_does_not_double_mutate_blocked_task(kanban_home, monkeypatch):
    tid, run_id = _running_task_with_env(monkeypatch)
    with kb.connect() as conn:
        assert kb.block_task(
            conn,
            tid,
            reason="blocked via kanban_block",
            expected_run_id=run_id,
        )

    blocked = _maybe_block_kanban_task_after_final_response(
        tid,
        "Post-block prose should not matter.",
    )

    assert blocked is False
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "blocked"
    assert run.summary == "blocked via kanban_block"
    assert [e.kind for e in events].count("blocked") == 1

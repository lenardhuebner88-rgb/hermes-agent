"""Regression tests for protocol-violation respawn reminders."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

import cli as cli_module

from hermes_cli import kanban_db as kb


PROTOCOL_VIOLATION_ERROR = (
    "worker exited cleanly (rc=0) without calling "
    "kanban_complete or kanban_block — protocol violation"
)
REMINDER_SNIPPET = (
    "Vorheriger Run endete ohne kanban_complete/kanban_block"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    db_path = kb.kanban_db_path(board="default")
    live_db = Path("/home/piet/.hermes/kanban.db").resolve()
    assert db_path.resolve() != live_db
    assert home.resolve() in db_path.resolve().parents
    kb.init_db()
    return home


def _append_ended_run(
    conn,
    task_id: str,
    *,
    outcome: str,
    error: str,
) -> int:
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, status, started_at, ended_at, outcome, error
        ) VALUES (?, 'default', ?, ?, ?, ?, ?)
        """,
        (task_id, outcome, now - 10, now - 5, outcome, error),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _finish_current_run_with_protocol_violation(conn, task_id: str) -> None:
    now = int(time.time())
    conn.execute(
        """
        UPDATE task_runs
           SET status = 'crashed',
               outcome = 'crashed',
               ended_at = ?,
               error = ?
         WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)
        """,
        (now, PROTOCOL_VIOLATION_ERROR, task_id),
    )
    conn.execute(
        """
        UPDATE tasks
           SET status = 'ready',
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               current_run_id = NULL
         WHERE id = ?
        """,
        (task_id,),
    )
    conn.commit()


def _reminder_comments(conn, task_id: str) -> list[str]:
    return [
        comment.body
        for comment in kb.list_comments(conn, task_id)
        if REMINDER_SNIPPET in comment.body
    ]


def test_protocol_violation_last_run_injects_one_visible_respawn_reminder(
    kanban_home: Path,
) -> None:
    seen_by_spawn: list[str] = []

    def fake_spawn(task: kb.Task, _workspace: str, *, board: str | None = None):
        seen_by_spawn.extend(
            comment.body for comment in kb.list_comments(conn, task.id)
        )
        return None

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="protocol miss", assignee="default")
        _append_ended_run(
            conn,
            task_id,
            outcome="crashed",
            error=PROTOCOL_VIOLATION_ERROR,
        )

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, dry_run=False)
        assert result.spawned and result.spawned[0][0] == task_id
        assert any(REMINDER_SNIPPET in body for body in seen_by_spawn)
        assert len(_reminder_comments(conn, task_id)) == 1

        _finish_current_run_with_protocol_violation(conn, task_id)
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, dry_run=False)
        assert result.spawned and result.spawned[0][0] == task_id
        assert len(_reminder_comments(conn, task_id)) == 1


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("blocked", "worker blocked with a normal task-level reason"),
        ("timed_out", "elapsed 120s > limit 120s"),
    ],
)
def test_non_protocol_last_run_does_not_inject_respawn_reminder(
    kanban_home: Path,
    outcome: str,
    error: str,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title=f"{outcome} retry", assignee="default")
        _append_ended_run(conn, task_id, outcome=outcome, error=error)

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: None)

        assert result.spawned and result.spawned[0][0] == task_id
        assert _reminder_comments(conn, task_id) == []



def _running_task_with_deliverable(conn, *, title: str = "implement lifecycle guard") -> str:
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="coder",
        kind="code",
    )
    assert kb.claim_task(conn, task_id) is not None
    kb.add_comment(
        conn,
        task_id,
        "coder",
        f"# RESULT: {title}\n\nImplementation and focused tests complete. "
        + "x" * 160,
    )
    return task_id


def test_terminalization_nudge_exposes_only_lifecycle_tools_and_restores_tools(
    kanban_home: Path,
) -> None:
    import model_tools

    seen: list[dict[str, object]] = []

    class FakeAgent:
        def __init__(self) -> None:
            self.tools = [
                {"type": "function", "function": {"name": "kanban_complete"}},
                {"type": "function", "function": {"name": "kanban_block"}},
                {"type": "function", "function": {"name": "kanban_comment"}},
                {"type": "function", "function": {"name": "terminal"}},
            ]
            self.valid_tool_names = {
                "kanban_complete", "kanban_block", "kanban_comment", "terminal",
            }
            self.max_iterations = 30
            self.session_id = "same-session"

        async def run_conversation(self, **_kwargs) -> str:
            seen.append({
                "tools": [tool["function"]["name"] for tool in self.tools],
                "valid_tool_names": set(self.valid_tool_names),
                "max_iterations": self.max_iterations,
                "global_tool_names": list(model_tools._last_resolved_tool_names),
            })
            return "terminalized"

    agent = FakeAgent()
    original_tools = agent.tools
    original_names = agent.valid_tool_names
    original_global_names = ["terminal", "kanban_comment", "kanban_complete"]
    model_tools._last_resolved_tool_names = list(original_global_names)
    worker = SimpleNamespace(
        agent=agent,
        conversation_history=[{"role": "user", "content": "original task"}],
        session_id="same-session",
    )

    with kb.connect_closing() as conn:
        task_id = _running_task_with_deliverable(conn)

    result = cli_module._run_kanban_finalize_nudge_q(worker, task_id=task_id)

    assert result == "terminalized"
    assert seen == [{
        "tools": ["kanban_complete", "kanban_block"],
        "valid_tool_names": {"kanban_complete", "kanban_block"},
        "max_iterations": 1,
        "global_tool_names": ["kanban_complete", "kanban_block"],
    }]
    assert agent.tools is original_tools
    assert agent.valid_tool_names is original_names
    assert agent.max_iterations == 30
    assert model_tools._last_resolved_tool_names == original_global_names


def test_terminalization_nudge_is_single_bounded_turn_when_lifecycle_call_is_missing(
    kanban_home: Path,
) -> None:
    calls = 0

    class FakeAgent:
        tools = [
            {"type": "function", "function": {"name": "kanban_complete"}},
            {"type": "function", "function": {"name": "kanban_block"}},
        ]
        valid_tool_names = {"kanban_complete", "kanban_block"}
        max_iterations = 30
        session_id = "same-session"

        async def run_conversation(self, **_kwargs) -> str:
            nonlocal calls
            calls += 1
            return "I forgot the lifecycle call again"

    with kb.connect_closing() as conn:
        task_id = _running_task_with_deliverable(conn)

    result = cli_module._run_kanban_finalize_nudge_q(
        SimpleNamespace(
            agent=FakeAgent(),
            conversation_history=[],
            session_id="same-session",
        ),
        task_id=task_id,
    )

    assert result == "I forgot the lifecycle call again"
    assert calls == 1
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "running"


def test_terminalization_nudge_filters_flat_runtime_tool_schemas(
    kanban_home: Path,
) -> None:
    """Live AIAgent stores resolved tools as flat schemas, not API wrappers."""
    seen: list[str] = []

    class FakeAgent:
        def __init__(self) -> None:
            self.tools = [
                {"name": "kanban_complete", "parameters": {"type": "object"}},
                {"name": "kanban_block", "parameters": {"type": "object"}},
                {"name": "kanban_comment", "parameters": {"type": "object"}},
            ]
            self.valid_tool_names = {
                "kanban_complete", "kanban_block", "kanban_comment",
            }
            self.max_iterations = 30
            self.session_id = "same-session"

        def run_conversation(self, **_kwargs) -> str:
            seen.extend(str(tool.get("name") or "") for tool in self.tools)
            return "terminalized"

    agent = FakeAgent()
    original_tools = agent.tools
    with kb.connect_closing() as conn:
        task_id = _running_task_with_deliverable(conn)

    result = cli_module._run_kanban_finalize_nudge_q(
        SimpleNamespace(
            agent=agent,
            conversation_history=[],
            session_id="same-session",
        ),
        task_id=task_id,
    )

    assert result == "terminalized"
    assert seen == ["kanban_complete", "kanban_block"]
    assert agent.tools is original_tools


def test_terminalization_nudge_ignores_deliverable_from_before_current_run(
    kanban_home: Path,
) -> None:
    calls = 0

    class FakeAgent:
        tools = [
            {"type": "function", "function": {"name": "kanban_complete"}},
            {"type": "function", "function": {"name": "kanban_block"}},
        ]
        valid_tool_names = {"kanban_complete", "kanban_block"}
        max_iterations = 30
        session_id = "same-session"

        def run_conversation(self, **_kwargs) -> str:
            nonlocal calls
            calls += 1
            return "must not run"

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="implement lifecycle guard",
            assignee="coder",
            kind="code",
        )
        kb.add_comment(
            conn,
            task_id,
            "coder",
            "# RESULT: implement lifecycle guard\n\nOld deliverable from a prior episode. "
            + "x" * 160,
        )
        assert kb.claim_task(conn, task_id) is not None

    result = cli_module._run_kanban_finalize_nudge_q(
        SimpleNamespace(
            agent=FakeAgent(),
            conversation_history=[],
            session_id="same-session",
        ),
        task_id=task_id,
    )

    assert result is None
    assert calls == 0


def test_terminalization_nudge_requires_successful_initial_turn(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = _running_task_with_deliverable(conn)

    result = cli_module._run_kanban_finalize_nudge_q(
        SimpleNamespace(
            agent=SimpleNamespace(tools=[]),
            conversation_history=[],
            session_id="same-session",
        ),
        task_id=task_id,
        initial_run_succeeded=False,
    )

    assert result is None


def test_terminalization_nudge_restores_absent_agent_attributes(
    kanban_home: Path,
) -> None:
    import model_tools

    class FakeAgent:
        def __init__(self) -> None:
            self.tools = [
                {"type": "function", "function": {"name": "kanban_complete"}},
                {"type": "function", "function": {"name": "kanban_block"}},
                object(),
            ]
            self.session_id = "same-session"

        def run_conversation(self, **_kwargs) -> str:
            assert self.max_iterations == 1
            assert self.valid_tool_names == {"kanban_complete", "kanban_block"}
            return "no lifecycle call"

    agent = FakeAgent()
    original_tools = agent.tools
    original_global_names = ["terminal"]
    model_tools._last_resolved_tool_names = list(original_global_names)
    with kb.connect_closing() as conn:
        task_id = _running_task_with_deliverable(conn)

    result = cli_module._run_kanban_finalize_nudge_q(
        SimpleNamespace(
            agent=agent,
            conversation_history=[],
            session_id="same-session",
        ),
        task_id=task_id,
    )

    assert result == "no lifecycle call"
    assert agent.tools is original_tools
    assert not hasattr(agent, "valid_tool_names")
    assert not hasattr(agent, "max_iterations")
    assert model_tools._last_resolved_tool_names == original_global_names

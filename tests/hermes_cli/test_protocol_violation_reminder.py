"""Regression tests for protocol-violation respawn reminders."""

from __future__ import annotations

import asyncio
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



def test_terminalization_nudge_exposes_only_lifecycle_tools_and_restores_tools() -> None:
    seen: list[list[str]] = []

    class FakeAgent:
        def __init__(self) -> None:
            self.tools = [
                {"type": "function", "function": {"name": "kanban_complete"}},
                {"type": "function", "function": {"name": "kanban_block"}},
                {"type": "function", "function": {"name": "kanban_comment"}},
                {"type": "function", "function": {"name": "terminal"}},
            ]

        async def run_conversation(self, _prompt: str, **_kwargs) -> str:
            seen.append([tool["function"]["name"] for tool in self.tools])
            return "terminalized"

    agent = FakeAgent()
    original_tools = agent.tools
    worker = SimpleNamespace(agent=agent)

    result = cli_module._run_kanban_finalize_nudge_q(worker, task_id="t_demo")

    assert result == "terminalized"
    assert seen == [["kanban_complete", "kanban_block"]]
    assert agent.tools is original_tools


def test_terminalization_nudge_is_single_bounded_turn_when_lifecycle_call_is_missing() -> None:
    calls = 0

    class FakeAgent:
        tools = [
            {"type": "function", "function": {"name": "kanban_complete"}},
            {"type": "function", "function": {"name": "kanban_block"}},
        ]

        async def run_conversation(self, _prompt: str, **_kwargs) -> str:
            nonlocal calls
            calls += 1
            return "I forgot the lifecycle call again"

    result = cli_module._run_kanban_finalize_nudge_q(
        SimpleNamespace(agent=FakeAgent()), task_id="t_demo"
    )

    assert result == "I forgot the lifecycle call again"
    assert calls == 1

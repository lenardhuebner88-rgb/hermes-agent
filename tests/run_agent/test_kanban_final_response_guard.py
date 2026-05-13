"""Regression tests for Kanban worker final-response protocol guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from run_agent import _maybe_block_kanban_task_after_final_response


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

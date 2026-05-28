"""Focused tests for bounded Kanban auto-continuation on iteration-budget exhaustion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _ready_task(conn, *, max_continuations=None):
    return kb.create_task(
        conn,
        title="budget-heavy audit",
        assignee="coder",
        max_iterations=4,
        max_continuations=max_continuations,
    )


def _claim(conn, tid):
    claimed = kb.claim_task(conn, tid, claimer="test-host:worker")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return claimed


def test_iteration_budget_exhausted_schedules_bounded_continuation(kanban_home):
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=2)
        claimed = _claim(conn, tid)

        ok = kb.record_iteration_budget_exhausted(
            conn,
            tid,
            summary="Inspected 7 files; continue from API routing section.",
            metadata={"phase": "routing"},
            expected_run_id=claimed.current_run_id,
        )

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.current_run_id is None
        assert task.continuation_count == 1
        assert task.max_continuations == 2
        assert task.last_continuation_reason == "iteration_budget_exhausted"
        assert task.consecutive_failures == 0

        run = kb.list_runs(conn, tid)[-1]
        assert run.status == "iteration_budget_exhausted"
        assert run.outcome == "iteration_budget_exhausted"
        assert run.summary == "Inspected 7 files; continue from API routing section."
        assert run.metadata == {"phase": "routing"}

        events = kb.list_events(conn, tid)
        assert [event.kind for event in events][-2:] == [
            "iteration_budget_exhausted",
            "auto_continuation_scheduled",
        ]
        assert events[-1].payload["count"] == 1
        assert events[-1].payload["limit"] == 2


def test_iteration_budget_exhausted_at_limit_blocks_with_reason(kanban_home):
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=1)

        first = _claim(conn, tid)
        assert kb.record_iteration_budget_exhausted(
            conn, tid, summary="first slice", expected_run_id=first.current_run_id,
        )

        second = _claim(conn, tid)
        assert kb.record_iteration_budget_exhausted(
            conn, tid, summary="second slice", expected_run_id=second.current_run_id,
        )

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.continuation_count == 1
        assert task.current_run_id is None
        assert "continuation limit exhausted (1/1)" in (task.result or "")
        assert task.consecutive_failures == 0

        events = kb.list_events(conn, tid)
        assert events[-1].kind == "auto_continuation_exhausted"
        assert events[-1].payload["count"] == 1
        assert events[-1].payload["limit"] == 1


def test_max_continuations_zero_disables_auto_continuation(kanban_home):
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=0)
        claimed = _claim(conn, tid)

        assert kb.record_iteration_budget_exhausted(
            conn, tid, summary="budget spent", expected_run_id=claimed.current_run_id,
        )

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.continuation_count == 0
        assert "auto-continuation disabled" in (task.result or "")
        assert kb.list_events(conn, tid)[-1].kind == "auto_continuation_disabled"


def test_expected_run_id_prevents_double_continuation_schedule(kanban_home):
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=2)
        claimed = _claim(conn, tid)

        assert kb.record_iteration_budget_exhausted(
            conn, tid, expected_run_id=claimed.current_run_id,
        )
        assert not kb.record_iteration_budget_exhausted(
            conn, tid, expected_run_id=claimed.current_run_id,
        )

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.continuation_count == 1
        assert sum(
            1 for event in kb.list_events(conn, tid)
            if event.kind == "auto_continuation_scheduled"
        ) == 1


def test_worker_context_adds_continuation_hint_only_after_requeue(kanban_home):
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=3)
        first_context = kb.build_worker_context(conn, tid)
        assert "continuation run" not in first_context.lower()

        claimed = _claim(conn, tid)
        assert kb.record_iteration_budget_exhausted(
            conn,
            tid,
            summary="Read config and tests; next inspect dispatcher.",
            expected_run_id=claimed.current_run_id,
        )

        continuation_context = kb.build_worker_context(conn, tid)
        assert "This is continuation run 1/3" in continuation_context
        assert "Continue from the latest run summary/log" in continuation_context
        assert "Read config and tests" in continuation_context


def test_kanban_continue_tool_uses_worker_ownership_and_run_guard(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=2)
        claimed = _claim(conn, tid)
        run_id = claimed.current_run_id

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = kt._handle_continue({
        "summary": "Budget reached after file audit; continue with tests.",
        "metadata": {"next": "tests"},
    })

    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["task_id"] == tid

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.continuation_count == 1
        run = kb.list_runs(conn, tid)[-1]
        assert run.outcome == "iteration_budget_exhausted"
        assert run.metadata["next"] == "tests"

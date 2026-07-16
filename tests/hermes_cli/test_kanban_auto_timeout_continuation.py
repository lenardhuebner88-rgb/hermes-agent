"""Automatic entry into the continuation ladder on iteration-budget timeout.

Production call path (agent/turn_finalizer.py ~172-191) when a kanban worker
hits its tool-call budget:

    _record_task_failure(
        conn, task_id,
        error="Iteration budget exhausted (N/N) — task could not complete "
              "within the allowed iterations",
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra={
            "budget_used": N,
            "budget_max": N,
            "workspace_progress_size": <int>,   # 648c7b0b7
            "budget_progress_marker": <int|omit>,  # 648c7b0b7
        },
        summary=...,
        expected_run_id=...,
    )

Before this wiring, that path only counted consecutive_failures and re-spawned
with the same static max_iterations — two burns then breaker (DEFAULT_FAILURE_LIMIT=2).
The voluntary kanban_continue tool already reaches _schedule_continuation_after_closed_run;
this module proves the timed_out failure path also enters that ladder when progress
evidence is present and caps still have room.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Exact production error shape from agent/turn_finalizer.py
_BUDGET_EXHAUSTED_ERROR = (
    "Iteration budget exhausted (6/6) — task could not complete "
    "within the allowed iterations"
)


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


def _ready_task(conn, *, max_iterations=6, max_continuations=2):
    return kb.create_task(
        conn,
        title="budget-exhaust-live",
        assignee="coder",
        max_iterations=max_iterations,
        max_continuations=max_continuations,
    )


def _claim(conn, tid):
    claimed = kb.claim_task(conn, tid, claimer="test-host:worker")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return claimed


def _production_timeout_failure(
    conn,
    tid,
    *,
    expected_run_id,
    workspace_progress_size=None,
    budget_progress_marker=None,
    budget_used=6,
    budget_max=6,
    failure_limit=None,
):
    """Mirror agent/turn_finalizer.py:172-191 parameter combination exactly."""
    extra = {
        "budget_used": budget_used,
        "budget_max": budget_max,
    }
    if workspace_progress_size is not None:
        extra["workspace_progress_size"] = int(workspace_progress_size)
    if budget_progress_marker is not None:
        extra["budget_progress_marker"] = int(budget_progress_marker)
    kwargs = dict(
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra=extra,
        summary="worker stopped mid-slice with progress on disk",
        expected_run_id=expected_run_id,
    )
    if failure_limit is not None:
        kwargs["failure_limit"] = failure_limit
    return kb._record_task_failure(
        conn,
        tid,
        _BUDGET_EXHAUSTED_ERROR,
        **kwargs,
    )


def test_timed_out_with_progress_schedules_continuation_not_failure(kanban_home):
    """done_when (1): progress evidence → continuation ladder, not failure burn."""
    with kb.connect() as conn:
        tid = _ready_task(conn, max_iterations=6, max_continuations=2)
        claimed = _claim(conn, tid)

        blocked = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed.current_run_id,
            workspace_progress_size=12,
            budget_progress_marker=3,
        )

        assert blocked is False
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.current_run_id is None
        assert task.consecutive_failures == 0
        assert task.continuation_count == 1
        assert task.last_continuation_reason == "iteration_budget_exhausted"
        # Spawn still exports the per-task max_iterations; the expanded
        # "budget" is the continuation allowance (one more bounded run),
        # not a silent max_iterations rewrite.
        assert task.max_iterations == 6

        runs = kb.list_runs(conn, tid)
        assert runs[-1].outcome == "iteration_budget_exhausted"

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "auto_continuation_scheduled" in kinds
        cont = next(e for e in events if e.kind == "auto_continuation_scheduled")
        assert cont.payload["count"] == 1
        assert cont.payload["limit"] == 2
        assert "gave_up" not in kinds


def test_timed_out_without_progress_keeps_failure_breaker_semantics(kanban_home):
    """done_when (2): empty/zero progress → exact prior failure behaviour."""
    with kb.connect() as conn:
        tid = _ready_task(conn, max_iterations=6, max_continuations=2)
        claimed = _claim(conn, tid)

        blocked = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed.current_run_id,
            workspace_progress_size=0,
            failure_limit=kb.DEFAULT_FAILURE_LIMIT,
        )

        assert blocked is False  # first failure; limit=2
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert task.continuation_count == 0
        assert "Iteration budget exhausted" in (task.last_failure_error or "")

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "auto_continuation_scheduled" not in kinds
        assert "timed_out" in kinds

        # Second identical burn without progress trips the breaker (limit=2).
        claimed2 = _claim(conn, tid)
        blocked2 = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed2.current_run_id,
            workspace_progress_size=0,
            failure_limit=kb.DEFAULT_FAILURE_LIMIT,
        )
        assert blocked2 is True
        task2 = kb.get_task(conn, tid)
        assert task2.status == "blocked"
        assert task2.consecutive_failures == 2
        kinds2 = [e.kind for e in kb.list_events(conn, tid)]
        assert "gave_up" in kinds2
        assert "auto_continuation_scheduled" not in kinds2


def test_timed_out_with_progress_at_continuation_cap_falls_back_to_failure(
    kanban_home,
):
    """done_when (3): caps exhausted → today's failure path (no endless extend)."""
    with kb.connect() as conn:
        tid = _ready_task(conn, max_iterations=6, max_continuations=1)
        # Spend the only continuation slot via the voluntary production path.
        first = _claim(conn, tid)
        assert kb.record_iteration_budget_exhausted(
            conn,
            tid,
            summary="voluntary slice",
            expected_run_id=first.current_run_id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.continuation_count == 1

        # Next timed_out with progress must NOT freeride past the cap.
        claimed = _claim(conn, tid)
        blocked = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed.current_run_id,
            workspace_progress_size=99,
            budget_progress_marker=1,
            failure_limit=kb.DEFAULT_FAILURE_LIMIT,
        )

        assert blocked is False  # first failure only
        task2 = kb.get_task(conn, tid)
        assert task2.status == "ready"
        assert task2.consecutive_failures == 1
        assert task2.continuation_count == 1  # not extended past cap
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        # No second auto_continuation_scheduled after the cap was hit.
        cont_events = [e for e in kb.list_events(conn, tid)
                       if e.kind == "auto_continuation_scheduled"]
        assert len(cont_events) == 1
        assert "timed_out" in kinds

        # Second failure still trips the breaker under DEFAULT_FAILURE_LIMIT.
        claimed3 = _claim(conn, tid)
        blocked3 = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed3.current_run_id,
            workspace_progress_size=99,
            failure_limit=kb.DEFAULT_FAILURE_LIMIT,
        )
        assert blocked3 is True
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).consecutive_failures == 2


def test_missing_progress_fields_are_not_a_freepass(kanban_home):
    """Progress keys omitted entirely (pre-648c7b0b7 shape) stay on failure path."""
    with kb.connect() as conn:
        tid = _ready_task(conn)
        claimed = _claim(conn, tid)
        blocked = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed.current_run_id,
            # no workspace_progress_size / budget_progress_marker
        )
        assert blocked is False
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        assert task.continuation_count == 0
        assert "auto_continuation_scheduled" not in [
            e.kind for e in kb.list_events(conn, tid)
        ]

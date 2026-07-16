"""Operator unblock/answer restores continuation budget after capacity exhaustion.

Production bug: ``unblock_task`` and ``answer_operator_question`` zero
``consecutive_failures`` / ``transient_retry_count`` / ``auto_retry_count``
but leave ``continuation_count`` untouched. After an operator unblocks a
capacity-exhausted task, the next ``iteration_budget_exhausted`` self-report
immediately re-blocks as capacity — the unblock is a placebo.

Builder choice (anti_scope): unconditional ``continuation_count = 0`` reset,
matching the sibling counters on the same UPDATE statements. Not gated on
``block_kind='capacity'``.

Core path uses production APIs only:
``record_iteration_budget_exhausted`` → ``_schedule_continuation_after_closed_run``,
``unblock_task``, ``hold_task`` + ``answer_operator_question``. No direct
UPDATE for the prove-reset path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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


def _ready_task(conn, *, max_continuations: int):
    return kb.create_task(
        conn,
        title="capacity-exhausted audit",
        assignee="coder",
        max_iterations=4,
        max_continuations=max_continuations,
    )


def _claim(conn, tid):
    claimed = kb.claim_task(conn, tid, claimer="test-host:worker")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return claimed


def _exhaust(conn, tid, *, summary: str = "slice"):
    """Production worker self-report that closes the run and schedules/blocks."""
    claimed = _claim(conn, tid)
    ok = kb.record_iteration_budget_exhausted(
        conn,
        tid,
        summary=summary,
        expected_run_id=claimed.current_run_id,
    )
    assert ok is True
    return kb.get_task(conn, tid)


def _drive_to_capacity_block(conn, *, max_continuations: int = 1):
    """Reach block_kind='capacity' with continuation_count == limit via production path.

    With max_continuations=N: N exhausts schedule continuations (count goes 1..N),
    exhaust N+1 hits the cap and parks as capacity.
    """
    tid = _ready_task(conn, max_continuations=max_continuations)
    for i in range(max_continuations):
        task = _exhaust(conn, tid, summary=f"continuation slice {i + 1}")
        assert task.status == "ready"
        assert task.continuation_count == i + 1
    task = _exhaust(conn, tid, summary="final slice at limit")
    assert task.status == "blocked"
    assert task.block_kind == "capacity"
    assert task.continuation_count == max_continuations
    return tid, max_continuations


def test_unblock_task_restores_continuation_budget_after_capacity_block(kanban_home):
    """done_when (1)+(2): capacity block → unblock_task → next exhaust requeues."""
    with kb.connect() as conn:
        tid, limit = _drive_to_capacity_block(conn, max_continuations=1)

        assert kb.unblock_task(conn, tid) is True
        after = kb.get_task(conn, tid)
        assert after.status == "ready"
        assert after.block_kind is None
        # Unconditional reset (builder choice): budget is real again.
        assert after.continuation_count == 0, (
            "unblock_task must reset continuation_count like sibling counters; "
            f"got continuation_count={after.continuation_count}"
        )

        # Next production budget-exhaustion report must schedule a continuation
        # (not immediately re-block as capacity).
        next_task = _exhaust(conn, tid, summary="post-unblock first slice")
        assert next_task.status == "ready", (
            "after operator unblock, first exhaustion must requeue, not capacity-block; "
            f"status={next_task.status} block_kind={next_task.block_kind} "
            f"result={next_task.result!r}"
        )
        assert next_task.block_kind is None
        assert next_task.continuation_count == 1
        assert next_task.continuation_count <= limit
        events = kb.list_events(conn, tid)
        assert events[-1].kind == "auto_continuation_scheduled"
        assert events[-1].payload["count"] == 1


def test_answer_operator_question_restores_continuation_budget(kanban_home):
    """done_when (3): answer_operator_question also restores the budget.

    Capacity-blocked runs are not operator-question-eligible
    (``iteration_budget_exhausted`` outcome is not a blocked run). Reach
    ``continuation_count == limit`` via the production schedule path, then
    park with ``hold_task`` (production operator-hold) so
    ``answer_operator_question`` is the release path under test.
    """
    with kb.connect() as conn:
        limit = 1
        tid = _ready_task(conn, max_continuations=limit)
        # Spend the single continuation slot (production schedule path).
        spent = _exhaust(conn, tid, summary="spend continuation budget")
        assert spent.status == "ready"
        assert spent.continuation_count == limit

        # Operator-hold while running: production path that makes the task
        # answerable via answer_operator_question.
        _claim(conn, tid)
        assert kb.hold_task(
            conn,
            tid,
            reason="operator hold: which scope should the next slice use?",
        ) is True
        held = kb.get_task(conn, tid)
        assert held.status == "blocked"
        assert held.continuation_count == limit
        assert kb.blocked_task_operator_questions(conn, [held]).get(tid) is True

        assert (
            kb.answer_operator_question(
                conn,
                tid,
                answer="Proceed with the local audit scope only.",
                author="operator",
            )
            == "ready"
        )
        after = kb.get_task(conn, tid)
        assert after.status == "ready"
        assert after.continuation_count == 0, (
            "answer_operator_question must reset continuation_count like "
            f"sibling counters; got continuation_count={after.continuation_count}"
        )

        # Next budget-exhaustion must schedule (not capacity-block at the old count).
        next_task = _exhaust(conn, tid, summary="post-answer first slice")
        assert next_task.status == "ready", (
            "after operator answer, first exhaustion must requeue, not capacity-block; "
            f"status={next_task.status} block_kind={next_task.block_kind} "
            f"result={next_task.result!r}"
        )
        assert next_task.block_kind is None
        assert next_task.continuation_count == 1
        events = kb.list_events(conn, tid)
        assert events[-1].kind == "auto_continuation_scheduled"
        assert events[-1].payload["count"] == 1

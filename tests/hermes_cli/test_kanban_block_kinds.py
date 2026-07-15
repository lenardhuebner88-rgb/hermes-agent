"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` clears visible block metadata on the task row, while the
  event trail preserves enough memory for a same-cause re-block to trip the
  loop breaker.
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_first_typed_block_lands_in_blocked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.block_recurrences == 1


def test_unblock_clears_visible_metadata(kanban_home: Path) -> None:
    """Resolved blocks should not leave stale badges on ready tasks."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 0
        assert t.block_kind is None


def test_same_cause_reblock_routes_to_triage(kanban_home: Path) -> None:
    """Dale's loop: block → unblock → re-block same kind → triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_untyped_block_loop_also_protected(kanban_home: Path) -> None:
    """Legacy un-typed blocks (kind=None) still trip the breaker."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="a again")
        assert kb.get_task(conn, tid).status == "triage"


def test_different_kinds_do_not_compound(kanban_home: Path) -> None:
    """A re-block for a DIFFERENT reason resets the counter to 1."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="b", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_recurrences == 1


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_block_routes_to_todo(kanban_home: Path) -> None:
    """Dependency waits never enter the human 'blocked' bucket."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


def test_completion_clears_block_memory(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).block_recurrences == 0
        kb.complete_task(conn, tid, result="done")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.block_recurrences == 0
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None


def test_transient_is_a_valid_kind(kanban_home: Path) -> None:
    """Regression: VALID_BLOCK_KINDS must include 'transient' — dropped by the
    v0.18 upstream merge (413638a28) alongside project_id/block_kind access
    paths. 'transient' marks a maybe-flaky failure and lands in 'blocked'
    like needs_input/capability (only 'dependency' routes to 'todo')."""
    assert "transient" in kb.VALID_BLOCK_KINDS
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="flaky network", kind="transient")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "transient"


def test_system_park_kinds_are_valid(kanban_home: Path) -> None:
    """capacity/integration are first-class kinds for system parks."""
    assert "capacity" in kb.VALID_BLOCK_KINDS
    assert "integration" in kb.VALID_BLOCK_KINDS
    assert "capacity" in kb.OPERATOR_ONLY_BLOCK_KINDS
    assert "integration" in kb.OPERATOR_ONLY_BLOCK_KINDS


def test_park_budget_runaway_sets_capacity_kind(kanban_home: Path) -> None:
    """Live operator lever: token-runaway parks must not leave block_kind NULL."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway", assignee="coder")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert kb._park_budget_runaway(conn, row, token_sum=5000, cap=100, runs=4)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "capacity"
        blocked_events = [
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        ]
        assert blocked_events
        assert blocked_events[-1].payload.get("kind") == "capacity"
        # Capacity parks are not auto-retryable.
        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)
        assert retried == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_park_integration_sets_integration_kind(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park-int", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb._park_integration(
            conn,
            tid,
            {"reason": "missing branch evidence", "park_class": "DIRTY_WORKTREE"},
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "integration"
        blocked_events = [
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        ]
        assert blocked_events[-1].payload.get("kind") == "integration"


def test_hold_task_sets_needs_input_kind(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.hold_task(conn, tid, reason="operator hold: choose deploy target")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"


def test_board_stats_blocked_by_kind(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        a = _running_task(conn, title="cap")
        b = _running_task(conn, title="rev")
        # capacity via system park helper surface
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (a,)).fetchone()
        # hold a first so it is blocked as needs_input, then park budget on ready b
        assert kb.hold_task(conn, a, reason="operator hold")
        # b still running — block as review_revision via worker-style block after
        # forging a review claim event is heavy; use typed block_task instead.
        # block_task rejects forging review_revision without review origin, so
        # use needs_input + capacity via parks only.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (b,))
        row_b = conn.execute("SELECT * FROM tasks WHERE id = ?", (b,)).fetchone()
        assert kb._park_budget_runaway(conn, row_b, token_sum=9, cap=1, runs=2)
        stats = kb.board_stats(conn)
        assert stats["blocked_by_kind"]["needs_input"] >= 1
        assert stats["blocked_by_kind"]["capacity"] >= 1


def test_capacity_not_auto_retried_after_review_retry_cleared_kind(
    kanban_home: Path,
) -> None:
    """Regression for live t_6943fc6f sequence: auto_retry clears block_kind,
    then budget runaway re-parks — must land as capacity (not null) and stay."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        # First a typed human block then unblock+retry path clears kind.
        assert kb.block_task(conn, tid, reason="need choice?", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_kind is None
        # Budget park after retry must re-stamp capacity.
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert kb._park_budget_runaway(conn, row, token_sum=999, cap=10, runs=3)
        task = kb.get_task(conn, tid)
        assert task.block_kind == "capacity"
        kind = kb._blocked_kind_for_auto_retry(
            "token cap",
            explicit_block_kind=task.block_kind,
        )
        assert kind == "capacity"
        assert kind != "retryable"

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


def test_default_typed_block_loop_also_protected(kanban_home: Path) -> None:
    """Omitted kinds are classified and still trip the recurrence breaker."""
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


def test_dependency_block_without_wait_is_visible_needs_input(
    kanban_home: Path,
) -> None:
    """A legacy/naked dependency block must never become a vacuous auto-wait."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.wait_for is None
        rejected = [
            event
            for event in kb.list_events(conn, tid)
            if event.kind == "dependency_wait_rejected"
        ]
        assert rejected
        assert rejected[-1].payload["reason"] == "wait_for is required"


def test_legacy_dependency_wait_with_parent_edges_is_backfilled(
    kanban_home: Path,
) -> None:
    """Existing self-healing dependency parks survive the additive migration."""
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="legacy parent")
        child = _running_task(conn, title="legacy child")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='todo', block_kind='dependency', "
                "wait_for=NULL, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                "WHERE id=?",
                (child,),
            )

        assert kb._backfill_legacy_dependency_waits(conn) == 1
        assert kb._backfill_legacy_dependency_waits(conn) == 0
        migrated = kb.get_task(conn, child)
        assert migrated.wait_for == {
            "type": "parents_all_done",
            "task_ids": [parent],
        }
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"

        assert kb.complete_task(conn, parent, result="legacy parent done")
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.block_task(
            conn,
            child,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent]},
        )
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, child).wait_for == {
            "type": "parents_all_done",
            "task_ids": [parent],
        }
        assert kb.parent_ids(conn, child) == [parent]
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        released = kb.get_task(conn, child)
        assert released.status == "ready"
        assert released.wait_for is None
        assert any(
            event.kind == "wait_released" for event in kb.list_events(conn, child)
        )


def test_claim_backstop_demotes_legacy_ready_task_with_active_wait(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        assert kb.block_task(
            conn,
            child,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent]},
        )
        before_run = kb.latest_run(conn, child).id
        # Simulate any forgotten/legacy writer bypassing recompute_ready.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))

        assert kb.claim_task(conn, child, claimer="second-worker") is None
        task = kb.get_task(conn, child)
        assert task.status == "todo"
        assert task.wait_for is not None
        assert kb.latest_run(conn, child).id == before_run
        rejected = [
            event for event in kb.list_events(conn, child) if event.kind == "claim_rejected"
        ]
        assert rejected[-1].payload["reason"] == "active_wait"


def test_not_before_wait_uses_due_at_and_is_claim_guarded(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1_700_000_000}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    release_at = "2023-11-14T22:13:30Z"
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(
            conn,
            tid,
            reason="wait ten seconds",
            kind="dependency",
            wait_for={"type": "not_before", "at": release_at},
        )
        task = kb.get_task(conn, tid)
        assert task.status == "todo"
        assert task.due_at == 1_700_000_010

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="too-early") is None
        assert kb.get_task(conn, tid).status == "todo"

        clock["now"] = 1_700_000_011
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, tid).wait_for is None
        assert kb.claim_task(conn, tid, claimer="on-time") is not None


def test_event_wait_releases_only_after_allowlisted_event(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        signal_task = kb.create_task(conn, title="signal", assignee="worker")
        waiting = _running_task(conn, title="waiter")
        assert kb.block_task(
            conn,
            waiting,
            reason="wait for completion signal",
            kind="dependency",
            wait_for={
                "type": "event_seen",
                "task_id": signal_task,
                "event_kind": "completed",
            },
        )
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, waiting).status == "todo"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (signal_task,))
        assert kb.claim_task(conn, signal_task, claimer="worker") is not None
        assert kb.complete_task(conn, signal_task, result="done")
        # complete_task triggers the normal readiness recompute itself.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, waiting).status == "ready"


def test_dependency_wait_rejects_missing_parent_without_partial_edge(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(
            conn,
            tid,
            reason="wait for missing",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": ["missing"]},
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.wait_for is None
        assert kb.parent_ids(conn, tid) == []


def test_active_wait_blocks_promote_schedule_and_unblock_until_operator_override(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        assert kb.block_task(
            conn,
            child,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent]},
        )
        before = kb.get_task(conn, child)

        assert kb.promote_task(
            conn, child, actor="operator", force=True, dry_run=True
        ) == (False, "active dependency wait is not satisfied")
        assert kb.promote_task(
            conn, child, actor="operator", force=True
        ) == (False, "active dependency wait is not satisfied")
        assert not kb.schedule_task(conn, child, due_at=before.due_at)
        assert not kb.unblock_task(conn, child)
        unchanged = kb.get_task(conn, child)
        assert unchanged.status == "todo"
        assert unchanged.wait_for == before.wait_for

        assert kb.unblock_task(
            conn,
            child,
            override_wait=True,
            actor="Piet",
            reason="Dependency bewusst verworfen",
        )
        overridden = kb.get_task(conn, child)
        assert overridden.status == "ready"
        assert overridden.wait_for is None
        assert kb.parent_ids(conn, child) == []
        assert kb.claim_task(conn, child, claimer="operator-override") is not None
        events = [
            event for event in kb.list_events(conn, child) if event.kind == "wait_overridden"
        ]
        assert events[-1].payload["actor"] == "Piet"
        assert events[-1].payload["reason"] == "Dependency bewusst verworfen"
        assert events[-1].payload["removed_parent_edges"] == [parent]


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


def test_block_without_kind_classifies_retryable_reason(kanban_home: Path) -> None:
    """Existing callers that omit kind still create a triageable block."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "transient"


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



# ---------------------------------------------------------------------------
# Wait-guard review hardening (2026-07-22, follow-up review findings)
# ---------------------------------------------------------------------------


def test_backfill_skips_terminal_legacy_dependency_tasks(kanban_home: Path) -> None:
    """A typed wait on a terminal owner could never be released and would make
    the referenced parents impossible to archive/purge — backfill skips them."""
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="legacy parent")
        child = _running_task(conn, title="terminal legacy child")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='archived', block_kind='dependency', "
                "wait_for=NULL, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                "WHERE id=?",
                (child,),
            )
        assert kb._backfill_legacy_dependency_waits(conn) == 0
        assert kb.get_task(conn, child).wait_for is None
        # The parent stays removable; no phantom wait protects it.
        assert kb.archive_task(conn, parent)


def test_satisfied_dependency_block_leaves_no_rebackfillable_marker(
    kanban_home: Path,
) -> None:
    """An immediately satisfied wait is resolved, not parked: ready with no
    dependency marker the legacy backfill could re-stamp after a restart."""
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="parent")
        assert kb.complete_task(conn, parent, result="done")
        child = _running_task(conn, title="child")
        assert kb.block_task(
            conn,
            child,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent]},
        )
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.wait_for is None
        assert task.block_kind is None
        assert kb._backfill_legacy_dependency_waits(conn) == 0


def test_not_before_release_preserves_later_operator_due_at(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normalizer folds a later operator-set ``due_at`` into the wait
    (max of both); at release that consumed value is cleared, but a due date
    the operator moved *while the task was waiting* must survive."""
    clock = {"now": 1_700_000_000}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    release_at = "2023-11-14T22:13:30Z"  # epoch 1_700_000_010
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET due_at=? WHERE id=?", (1_700_005_000, tid)
            )
        assert kb.block_task(
            conn,
            tid,
            reason="wait ten seconds",
            kind="dependency",
            wait_for={"type": "not_before", "at": release_at},
        )
        # Folded: the operator deadline becomes the wait's effective time.
        assert kb.get_task(conn, tid).due_at == 1_700_005_000
        # Operator moves the deadline further out while the task is waiting.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET due_at=? WHERE id=?", (1_700_006_000, tid)
            )
        clock["now"] = 1_700_005_001
        # The evaluator honours the moved due date: the wait is not satisfied
        # until now >= max(wait_at, due_at) — nothing releases or promotes yet.
        assert kb.recompute_ready(conn) == 0
        held = kb.get_task(conn, tid)
        assert held.wait_for is not None
        assert held.status == "todo"
        clock["now"] = 1_700_006_001
        assert kb.recompute_ready(conn) == 1
        released = kb.get_task(conn, tid)
        assert released.wait_for is None
        # The concurrently moved operator deadline survives the release.
        assert released.due_at == 1_700_006_000
        assert released.status == "ready"

        # Without a concurrent edit the wait's own (folded) due_at is cleared.
        clock["now"] = 1_700_000_000
        tid2 = _running_task(conn, title="plain not_before")
        assert kb.block_task(
            conn,
            tid2,
            reason="wait ten seconds",
            kind="dependency",
            wait_for={"type": "not_before", "at": release_at},
        )
        assert kb.get_task(conn, tid2).due_at == 1_700_000_010
        clock["now"] = 1_700_000_011
        assert kb.recompute_ready(conn) == 1
        released2 = kb.get_task(conn, tid2)
        assert released2.wait_for is None
        assert released2.due_at is None


def test_schedule_releases_satisfied_wait_and_refuses_active(
    kanban_home: Path,
) -> None:
    """schedule_task behaves like promote_task: satisfied waits release, only
    waiting/invalid waits refuse."""
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="parent")
        child = _running_task(conn, title="child")
        assert kb.block_task(
            conn,
            child,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent]},
        )
        assert kb.get_task(conn, child).status == "todo"
        # Active wait: scheduling is not an implicit override.
        assert not kb.schedule_task(conn, child, reason="not yet")
        assert kb.complete_task(conn, parent, result="done")
        # Satisfied wait: released through the same guard, then parked on time.
        assert kb.schedule_task(conn, child, reason="park on time")
        task = kb.get_task(conn, child)
        assert task.status == "scheduled"
        assert task.wait_for is None
        assert any(
            event.kind == "wait_released" for event in kb.list_events(conn, child)
        )


def test_superseded_block_with_active_wait_does_not_raise_or_archive(
    kanban_home: Path,
) -> None:
    """The superseded auto-archive runs after the block committed; a wait-guard
    refusal there must not surface as a failed block. The task stays blocked
    with its wait preserved (mirrors funnel.archive_stale)."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        tid = _running_task(conn)
        assert kb.block_task(
            conn,
            tid,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent]},
        )
        # Drive back to running without touching the wait (legacy writer).
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        assert (
            kb.block_task(conn, tid, reason="superseded: replaced by newer run")
            is True
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.wait_for is not None

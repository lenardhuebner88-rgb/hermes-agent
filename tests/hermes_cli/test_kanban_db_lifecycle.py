"""Kanban DB tests: lifecycle.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

from tests.hermes_cli._kanban_test_helpers import (
    _seed_completed_run,
)

# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------

def test_complete_records_result(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        assert kb.complete_task(conn, t, result="done and dusted")
        task = kb.get_task(conn, t)
    assert task.status == "done"
    assert task.result == "done and dusted"
    assert task.completed_at is not None


def test_block_then_unblock(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        assert kb.get_task(conn, t).status == "blocked"
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"


def test_block_task_stores_reviewer_metadata(kanban_home):
    """B-T10: block_task persists structured reviewer_metadata into
    task_runs.metadata (no second migration). Default None = byte-identical."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        rid = kb._current_run_id(conn, tid)
        meta = {"verdict": "REQUEST_CHANGES",
                "blocking_findings": ["null deref in foo()", "missing test for bar"]}
        assert kb.block_task(conn, tid, reason="changes needed", reviewer_metadata=meta)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (rid,)
        ).fetchone()
        stored = json.loads(row["metadata"])
        assert stored["blocking_findings"][0].startswith("null deref")
        assert stored["verdict"] == "REQUEST_CHANGES"


def test_block_task_without_metadata_is_unchanged(kanban_home):
    """Default None reviewer_metadata leaves the run metadata empty (today)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        rid = kb._current_run_id(conn, tid)
        assert kb.block_task(conn, tid, reason="plain block")
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (rid,)
        ).fetchone()
        assert row["metadata"] in (None, "", "{}", "null")


def test_unblock_resets_failure_counters(kanban_home):
    """unblock_task must reset consecutive_failures and last_failure_error."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        # Simulate accumulated failures from the circuit breaker
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 5, "
            "last_failure_error = 'test error' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.unblock_task(conn, t)
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_skips_tasks_at_failure_limit(kanban_home):
    """recompute_ready must not auto-recover tasks whose consecutive_failures
    has reached the circuit-breaker limit (#35072).

    Without this guard, a task that repeatedly exhausts its iteration
    budget would cycle forever: block → auto-recover (counter reset)
    → respawn → budget exhausted → block → …
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a",
                               parents=[parent])
        # Complete the parent so the child's dependencies are satisfied.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="done")

        # Simulate the child having exhausted its budget twice,
        # hitting the default failure limit (2).
        kb.claim_task(conn, child)
        kb._record_task_failure(
            conn, child, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        kb._record_task_failure(
            conn, child, error="budget exhausted 2",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 2

        # recompute_ready must NOT promote this task — the circuit
        # breaker has tripped and it should stay blocked.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"

        # Explicit unblock should still work and reset the counter.
        assert kb.unblock_task(conn, child)
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_recompute_ready_recovers_below_limit(kanban_home):
    """recompute_ready auto-recovers blocked tasks that haven't hit the
    failure limit yet — the counter is preserved across recovery."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="task", assignee="a")
        kb.claim_task(conn, t)
        # One failure, below the default limit of 2.
        kb._record_task_failure(
            conn, t, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Simulate being blocked by something else (not circuit breaker).
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        # Counter must be preserved, not reset.
        assert task.consecutive_failures == 1


def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kb.connect_closing() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"


def test_recompute_ready_honours_persisted_gave_up_effective_limit(kanban_home):
    """A later recompute without dispatcher config must not reopen a task
    that was parked by a stricter failure_limit in ``_record_task_failure``.
    """
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="strict breaker", assignee="a")
        assert kb.claim_task(conn, task_id, claimer="host:1") is not None

        kb._record_task_failure(
            conn,
            task_id,
            error="spawn boom",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1

        # No failure_limit argument here: this simulates a later dashboard or
        # maintenance recompute pass that only has DEFAULT_FAILURE_LIMIT (2).
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"


def test_recompute_ready_per_task_max_retries_overrides_dispatcher(kanban_home):
    """A per-task ``max_retries`` wins over the dispatcher failure_limit,
    matching ``_record_task_failure``'s resolution order."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="per-task", assignee="a")
        # Per-task allows 4 retries; dispatcher config says 2.
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=2, "
            "max_retries=4 WHERE id=?",
            (t,),
        )
        conn.commit()
        # failures(2) < per-task limit(4) → recover, despite dispatcher=2.
        promoted = kb.recompute_ready(conn, failure_limit=2)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 2


# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------

def test_claim_rejects_when_parents_not_done(kanban_home):
    """claim_task must refuse ready->running if any parent isn't 'done'.

    Simulates the create-then-link race: a task gets status='ready' via a
    racy writer while it still has undone parents. The claim gate must
    detect the violation, demote the child back to 'todo', append a
    'claim_rejected' event, and return None. Covers Fix 1 of the RCA.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Child correctly starts 'todo' because parent is not 'done'.
        assert kb.get_task(conn, child).status == "todo"
        # Simulate the race: a racy writer force-promotes the child to
        # 'ready' while parent is still pending.
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "ready"

        result = kb.claim_task(conn, child, claimer="host:1")

    assert result is None
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"
        events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? ORDER BY id",
            (child,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "claim_rejected" in kinds
    # No 'claimed' event was emitted for the blocked attempt.
    assert "claimed" not in kinds


def test_claim_succeeds_once_parents_done(kanban_home):
    """After parents complete, recompute_ready -> claim_task must succeed."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        claimed = kb.claim_task(conn, child, claimer="host:1")
    assert claimed is not None
    assert claimed.status == "running"


def test_create_with_parents_stays_todo_until_parents_done(kanban_home):
    """kanban_create(parents=[...]) must land in 'todo' and only promote on parent done."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        assert kb.get_task(conn, child).status == "todo"
        # Dispatcher tick between create and some later event must NOT
        # produce a winner for this child.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "todo"
        # Complete parent; complete_task internally runs recompute_ready,
        # which promotes the child to 'ready'.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_with_pending_parents_goes_to_todo(kanban_home):
    """unblock_task must re-gate on parent completion (Fix 3).

    A task blocked while parents are still in progress must return to
    'todo' (not 'ready') on unblock. Otherwise the dispatcher will claim
    it immediately, repeating Bug 2 from the RCA.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Force child into 'blocked' regardless of parent progress
        # (simulates a worker that self-blocked, or an operator block).
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child,),
        )
        conn.commit()
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "todo"
        # After parent completes + recompute, the child is ready.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_without_parents_goes_to_ready(kanban_home):
    """Parent-free unblock still produces 'ready' (behavior preserved)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="lone", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"


def test_block_task_from_ready_state_synthesizes_run(kanban_home):
    """A never-claimed 'ready' task can be blocked; because no run is open the
    reason is preserved on a synthesized ended run rather than dropped."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="lone ready", assignee="a")
        assert kb.get_task(conn, t).status == "ready"  # parent-free -> ready
        # No claim => current_run_id is NULL, so block must synthesize a run.
        assert kb.block_task(conn, t, reason="operator hold")
        assert kb.get_task(conn, t).status == "blocked"
        row = conn.execute(
            "SELECT outcome, summary FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert row is not None  # synthesized, not silently dropped
        assert row["outcome"] == "blocked"
        assert row["summary"] == "operator hold"


def test_block_task_on_todo_task_is_rejected(kanban_home):
    """block_task only fires from 'running'/'ready'. A gated 'todo' child
    (open parent) cannot be force-blocked, and no orphan run is synthesized."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"  # gated by open parent
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE task_id = ?", (child,)
        ).fetchone()["c"]
        assert kb.block_task(conn, child, reason="noop") is False
        assert kb.get_task(conn, child).status == "todo"  # unchanged
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE task_id = ?", (child,)
        ).fetchone()["c"]
        assert after == before  # rejected block synthesizes nothing


def test_block_task_expected_run_id_mismatch_is_rejected(kanban_home):
    """A stale worker's expected_run_id must not block the live attempt
    (compare-and-swap guard). Mismatch -> no transition, status stays running;
    the matching id blocks as expected."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        live_run = kb._current_run_id(conn, t)
        assert live_run is not None
        assert kb.block_task(conn, t, reason="stale", expected_run_id=live_run + 1) is False
        assert kb.get_task(conn, t).status == "running"  # untouched
        assert kb.block_task(conn, t, reason="real", expected_run_id=live_run) is True
        assert kb.get_task(conn, t).status == "blocked"


def test_complete_task_clears_consecutive_failures(kanban_home):
    """complete_task wipes the circuit-breaker counters on success (the
    'complete_task reset' path) — symmetric with unblock_task."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 4, transient_retry_count = 2, "
            "last_failure_error = 'boom' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.complete_task(conn, t, summary="green")
        task = kb.get_task(conn, t)
        assert task.status == "done"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_leaves_blocked_child_gated_while_parent_open(kanban_home):
    """A dependency-blocked child whose parent is still open must NOT be
    promoted by recompute_ready. Promoting it back to 'ready' here is exactly
    what would let the dispatcher reclaim-then-reblock in a tight loop; the
    child only becomes eligible once the parent is done."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        # Parent in progress (claimed, not done); force the child to blocked.
        kb.claim_task(conn, parent)
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (child,),
        )
        conn.commit()
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "blocked"  # still gated
        # Only once the parent is done does the child become eligible.
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_wait_block_kind_is_todo_not_reclaimable(kanban_home):
    """AC-1: A child with an open parent is naturally gated to 'todo' by
    create_task/claim_task. Additionally, a running child whose parent is
    reopened can be blocked with kind='dependency' and lands in 'todo'; it is
    not claimable or runnable while the parent is still open."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        # Natural gate: child is created as todo, cannot be claimed.
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child) is None
        ok, msg = kb.promote_task(conn, child, actor="test")
        assert ok is False
        assert msg is not None and "unsatisfied parent" in msg

        # Simulate the rare dispatcher path: parent is done, child runs,
        # parent somehow becomes un-done again (e.g. rollback/reopen), and the
        # run realises it needs to dependency-wait. Block with kind='dependency'
        # must park the child back on todo, not blocked/triage.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, child) is not None
        # Reopen parent manually to simulate the dependency-wait trigger.
        conn.execute(
            "UPDATE tasks SET status='todo' WHERE id=?", (parent,)
        )
        conn.commit()
        assert kb.block_task(conn, child, reason="waiting for parent", kind="dependency")
        task = kb.get_task(conn, child)
        assert task.status == "todo"
        assert task.block_kind == "dependency"
        # claim_task and promote_task must refuse while parent is open.
        assert kb.claim_task(conn, child) is None
        ok, msg = kb.promote_task(conn, child, actor="test")
        assert ok is False
        assert msg is not None and (
            "dependency wait" in msg or "unsatisfied parent dependencies" in msg
        )


def test_dependency_wait_promotes_when_parent_done_and_resets_block_kind(kanban_home):
    """AC-2: Once the parent completes, the dependency-wait child is promoted to
    'ready' and its block_kind/recurrences are cleared."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        # Parent must be done before the child can run and then be parked.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child) is not None
        # Reopen parent to create the dependency-wait condition.
        conn.execute(
            "UPDATE tasks SET status='todo' WHERE id=?", (parent,)
        )
        conn.commit()
        assert kb.block_task(conn, child, reason="waiting for parent", kind="dependency")
        task = kb.get_task(conn, child)
        assert task.status == "todo"
        assert task.block_kind == "dependency"
        # Re-complete the parent; child should be promoted and block_kind reset.
        kb.recompute_ready(conn)  # parent was reopened; get it back to ready first
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "ready"
        assert task.block_kind is None
        assert task.block_recurrences == 0
        # claim should now succeed.
        assert kb.claim_task(conn, child) is not None


def test_dependency_wait_does_not_escalate_loop_or_recurrence(kanban_home):
    """AC-3: Pure dependency waits must never be counted as loop/recurrence
    escalation and never produce triage, even when the dependency-wait
    pattern repeats on the same task."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])

        def block_dependency_wait():
            # Make the parent ready/claimed/done, then promote child to ready,
            # claim it, reopen the parent and dependency-wait the child.
            kb.recompute_ready(conn)  # parent may be todo after prior reopen
            kb.claim_task(conn, parent)
            kb.complete_task(conn, parent, result="ok")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, child) is not None
            # Reopen parent to trigger the dependency wait.
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent,))
            conn.commit()
            assert kb.block_task(conn, child, reason="waiting", kind="dependency")
            assert kb.get_task(conn, child).status == "todo"
            assert kb.get_task(conn, child).block_recurrences == 1
            event_count = conn.execute(
                "SELECT COUNT(*) AS c FROM task_events "
                "WHERE task_id = ? AND kind = 'block_loop_detected'",
                (child,),
            ).fetchone()["c"]
            assert event_count == 0

        # Repeat the dependency-wait cycle several times.  Recurrences must
        # stay pinned to 1 and triage/loop-detected events must never fire.
        for _ in range(3):
            block_dependency_wait()
        # While parent is still open, unblock_task must not promote it either.
        kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "todo"


def test_assign_refuses_while_running(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        with pytest.raises(RuntimeError, match="currently running"):
            kb.assign_task(conn, t, "b")


def test_assign_reassigns_when_not_running(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        assert kb.assign_task(conn, t, "b")
        assert kb.get_task(conn, t).assignee == "b"


def test_assignee_normalized_to_lowercase_on_create_and_assign(kanban_home):
    """Dashboard/CLI may pass title-cased profile labels; DB + spawn use canonical id."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cased", assignee="Jules")
        assert kb.get_task(conn, tid).assignee == "jules"
        assert kb.assign_task(conn, tid, "Librarian")
        assert kb.get_task(conn, tid).assignee == "librarian"


def test_list_tasks_assignee_filter_case_insensitive(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="q", assignee="jules")
        found = kb.list_tasks(conn, assignee="Jules")
        assert len(found) == 1 and found[0].id == tid


def test_archive_hides_from_default_list(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.complete_task(conn, t)
        assert kb.archive_task(conn, t)
        assert len(kb.list_tasks(conn)) == 0
        assert len(kb.list_tasks(conn, include_archived=True)) == 1


def test_delete_archived_task_removes_related_rows(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_archived_task_rejects_non_archived_rows(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="live")
        assert kb.delete_archived_task(conn, tid) is False
        assert kb.get_task(conn, tid) is not None


def test_list_tasks_order_by(kanban_home):
    with kb.connect_closing() as conn:
        # Create tasks with different titles and priorities
        t_a = kb.create_task(conn, title="alpha", priority=1)
        t_b = kb.create_task(conn, title="beta", priority=2)
        t_c = kb.create_task(conn, title="gamma", priority=1)

        # Default sort: priority DESC, created ASC
        default = kb.list_tasks(conn)
        assert [t.id for t in default] == [t_b, t_a, t_c]

        # Sort by title ASC
        by_title = kb.list_tasks(conn, order_by="title")
        assert [t.id for t in by_title] == [t_a, t_b, t_c]

        # Sort by assignee
        kb.assign_task(conn, t_a, "alice")
        kb.assign_task(conn, t_b, "bob")
        kb.assign_task(conn, t_c, "alice")
        by_assignee = kb.list_tasks(conn, order_by="assignee")
        # alice's tasks first (alphabetically), then bob's
        assignees = [t.assignee for t in by_assignee]
        assert assignees[:2] == ["alice", "alice"]
        assert assignees[2] == "bob"

        # Invalid sort order raises ValueError
        try:
            kb.list_tasks(conn, order_by="bogus")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "order_by must be one of" in str(e)


def test_delete_task_removes_task_and_cascades(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0


def test_delete_task_returns_false_for_missing_task(kanban_home):
    with kb.connect_closing() as conn:
        assert not kb.delete_task(conn, "t_nonexistent")


def test_delete_task_cascades_links(kanban_home):
    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        child = kb.get_task(conn, c)
        assert child is not None and child.status == "todo"
        kb.delete_task(conn, p)
        assert kb.get_task(conn, p) is None
        child_after = kb.get_task(conn, c)
        assert child_after is not None and child_after.status == "ready"


# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------

def test_comments_recorded_in_order(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "user", "first")
        kb.add_comment(conn, t, "researcher", "second")
        comments = kb.list_comments(conn, t)
    assert [c.body for c in comments] == ["first", "second"]
    assert [c.author for c in comments] == ["user", "researcher"]


def test_empty_comment_rejected(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        with pytest.raises(ValueError, match="body is required"):
            kb.add_comment(conn, t, "user", "")


def test_events_capture_lifecycle(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="ok")
        events = kb.list_events(conn, t)
    kinds = [e.kind for e in events]
    assert "created" in kinds
    assert "claimed" in kinds
    assert "completed" in kinds


def test_worker_context_includes_parent_results_and_comments(kanban_home):
    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="p")
        kb.complete_task(conn, p, result="PARENT_RESULT_MARKER")
        c = kb.create_task(conn, title="child", parents=[p])
        kb.add_comment(conn, c, "user", "CLARIFICATION_MARKER")
        ctx = kb.build_worker_context(conn, c)
    assert "PARENT_RESULT_MARKER" in ctx
    assert "CLARIFICATION_MARKER" in ctx
    assert c in ctx
    assert "child" in ctx


def test_worker_context_worker_slim_uses_tighter_caps(kanban_home):
    big_body = "BODY-" + ("x" * 9000)
    big_comment = "COMMENT-" + ("y" * 3000)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="caps", body=big_body, assignee="coder")
        for idx in range(12):
            kb.add_comment(conn, t, "worker", f"{idx}-" + big_comment)
        now = 1_800_000_000
        for idx in range(5):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, started_at, ended_at, outcome, summary
                ) VALUES (?, ?, 'done', ?, ?, 'completed', ?)
                """,
                (t, "coder", now + idx, now + idx + 1, f"summary-{idx}"),
            )
        conn.commit()
        full = kb.build_worker_context(conn, t)
        slim = kb.build_worker_context(conn, t, profile="worker_slim")

    assert len(slim) < len(full)
    assert "showing most recent 8" in slim
    assert "showing most recent 30" not in slim
    assert "showing most recent 3" in slim
    assert "summary-0" not in slim
    assert "summary-4" in slim
    assert "[truncated," in slim


def test_worker_context_reviewer_review_uses_larger_body_cap(kanban_home):
    """Reviewer code-review cards keep a larger diff/test body visible.

    Regression: compact code-review cards exceeded the default 8 KiB body cap,
    so the verdict-only reviewer could see instructions but not the full
    implementation/test evidence and exhausted its iteration budget asking for
    more context. Ordinary tasks keep the default cap; only assignee=reviewer +
    kind=review gets the larger opening-body window.
    """
    default_cap = kb._CTX_CAP_PROFILES["full"]["body_bytes"]
    reviewer_cap = kb._CTX_CAP_PROFILES["reviewer_review"]["body_bytes"]
    assert reviewer_cap > default_cap
    body = "BEGIN\n" + ("x" * (default_cap + 100)) + "\nVISIBLE_REVIEW_EVIDENCE"

    with kb.connect_closing() as conn:
        reviewer_task = kb.create_task(
            conn, title="review patch", body=body, assignee="reviewer", kind="review"
        )
        coder_task = kb.create_task(conn, title="ordinary", body=body, assignee="coder")
        reviewer_ctx = kb.build_worker_context(conn, reviewer_task, profile="full")
        coder_ctx = kb.build_worker_context(conn, coder_task, profile="full")

    assert "VISIBLE_REVIEW_EVIDENCE" in reviewer_ctx
    assert "VISIBLE_REVIEW_EVIDENCE" not in coder_ctx
    assert "[truncated," in coder_ctx


def test_worker_context_reviewer_review_continuation_uses_retry_caps(kanban_home):
    """The larger reviewer body cap must not apply to continuation retries."""
    retry_cap = kb._CTX_CAP_PROFILES["retry"]["body_bytes"]
    body = "BEGIN\n" + ("x" * (retry_cap + 100)) + "\nHIDDEN_ON_RETRY"
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="review retry", body=body, assignee="reviewer", kind="review"
        )
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (task_id,))
        conn.commit()
        ctx = kb.build_worker_context(conn, task_id, profile="full")

    assert "This is continuation run 1/" in ctx
    assert "HIDDEN_ON_RETRY" not in ctx
    assert "[truncated," in ctx


def test_worker_context_worker_slim_retry_uses_retry_profile(kanban_home):
    """Continuation workers on worker_slim use the tighter retry caps."""
    big_body = "BODY-" + ("x" * 3000)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="retry caps", body=big_body, assignee="coder")
        for idx in range(6):
            kb.add_comment(conn, t, "worker", f"comment-{idx}")
        now = 1_800_000_000
        for idx in range(3):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, started_at, ended_at, outcome, summary
                ) VALUES (?, ?, 'done', ?, ?, 'completed', ?)
                """,
                (t, "coder", now + idx, now + idx + 1, f"summary-{idx}"),
            )
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="worker_slim")

    assert "This is continuation run 1/" in ctx
    assert "showing most recent 1" in ctx
    assert "summary-0" not in ctx
    assert "summary-1" not in ctx
    assert "summary-2" in ctx
    assert "showing most recent 4" in ctx
    assert "comment-1" not in ctx
    assert "comment-2" in ctx
    assert "[truncated," in ctx


def test_worker_context_full_retry_uses_retry_profile_caps(kanban_home):
    """Continuation workers on full context also use the retry caps.

    Regression: verifier review runs request ``profile='full'``. Continuation
    review runs must still get the small retry caps, keyed by the context
    profile parameter rather than by task assignee.
    """
    big_body = "BODY-" + ("x" * (kb._CTX_CAP_PROFILES["retry"]["body_bytes"] + 500))
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="retry caps", body=big_body, assignee="verifier")
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["comments"] + 2):
            kb.add_comment(conn, t, "worker", f"comment-{idx}")
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["prior_attempts"] + 2):
            _seed_completed_run(conn, t, "verifier", 1_800_000_000 + idx, f"summary-{idx}")
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="full")

    retry_caps = kb._CTX_CAP_PROFILES["retry"]
    full_caps = kb._CTX_CAP_PROFILES["full"]
    assert retry_caps["prior_attempts"] < full_caps["prior_attempts"]
    assert retry_caps["comments"] < full_caps["comments"]
    assert "This is continuation run 1/" in ctx
    assert f"showing most recent {retry_caps['prior_attempts']}" in ctx
    assert "summary-0" not in ctx
    assert "summary-2" in ctx
    assert f"showing most recent {retry_caps['comments']}" in ctx
    assert "comment-0" not in ctx
    assert "comment-5" in ctx
    assert "[truncated," in ctx


def test_worker_context_full_without_continuation_keeps_full_profile_caps(kanban_home):
    """Non-continuation full contexts must not be downgraded to retry caps."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="full caps", assignee="verifier")
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["prior_attempts"] + 2):
            _seed_completed_run(conn, t, "verifier", 1_800_000_000 + idx, f"full-summary-{idx}")
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="full")

    assert "This is continuation run" not in ctx
    assert f"showing most recent {kb._CTX_CAP_PROFILES['retry']['prior_attempts']}" not in ctx
    assert "full-summary-0" in ctx
    assert "full-summary-2" in ctx


def test_worker_context_prior_attempts_unchanged_by_shared_renderer_refactor(kanban_home):
    """Parity guard: build_worker_context's 'Prior attempts on this task'
    section (now backed by the shared _render_prior_attempts helper) must
    keep the exact same strings/ordering as before the refactor that
    extracted it for reuse by the claude-CLI worker path."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ship the widget", assignee="coder")
        kb.claim_task(conn, t)
        meta = {
            "verdict": "REQUEST_CHANGES",
            "blocking_findings": ["null deref in foo()", "missing test for bar"],
        }
        kb.block_task(conn, t, reason="lint failed, see foo()", reviewer_metadata=meta)
        kb.unblock_task(conn, t)

        ctx = kb.build_worker_context(conn, t, profile="full")

    assert "## Prior attempts on this task" in ctx
    assert "### Attempt 1 —" in ctx
    assert "lint failed, see foo()" in ctx
    assert "null deref in foo()" in ctx
    assert "REQUEST_CHANGES" in ctx
    # Ordering unchanged: prior attempts render after the header/knowledge
    # pointers block and before the end of the context (no parents/comments
    # here, so this pins the section stays where it always has).
    assert ctx.index("## Knowledge pointers") < ctx.index(
        "## Prior attempts on this task"
    )


def test_worker_context_retry_suppresses_recent_work(kanban_home):
    """Continuation workers do not receive cross-task recent-work history."""
    with kb.connect_closing() as conn:
        previous = kb.create_task(conn, title="previous", assignee="coder")
        _seed_completed_run(conn, previous, "coder", 1_800_000_000, "PRIOR_RECENT_WORK")

        t = kb.create_task(conn, title="retry followup", assignee="coder")
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="worker_slim")

    assert "## Recent work by @coder" not in ctx
    assert "PRIOR_RECENT_WORK" not in ctx


def test_worker_context_recent_work_tenant_scoped(kanban_home):
    """AC-TENANT-SCOPED: on a multi-tenant board the recent-work section
    must only surface completed runs for the active tenant, not cross-tenant."""
    with kb.connect_closing() as conn:
        # Two tenants, same assignee, completed runs in both.
        t_a1 = kb.create_task(conn, title="tenant-A task 1", assignee="coder", tenant="tenantA")
        t_a2 = kb.create_task(conn, title="tenant-A task 2", assignee="coder", tenant="tenantA")
        t_b1 = kb.create_task(conn, title="tenant-B task", assignee="coder", tenant="tenantB")
        now = 1_800_000_000
        _seed_completed_run(conn, t_a1, "coder", now + 10, "SUMMARY_TENANT_A1")
        _seed_completed_run(conn, t_a2, "coder", now + 20, "SUMMARY_TENANT_A2")
        _seed_completed_run(conn, t_b1, "coder", now + 30, "SUMMARY_TENANT_B")
        conn.commit()
        ctx_a = kb.build_worker_context(conn, t_a1)
    # Extract just the "Recent work" section.
    rw_start = ctx_a.find("## Recent work by @coder")
    rw_section = ctx_a[rw_start:] if rw_start >= 0 else ""
    assert rw_section, "Recent work section should be present"
    # Tenant A's other task surfaces in recent work.
    assert "SUMMARY_TENANT_A2" in rw_section
    assert "tenant-A task 2" in rw_section
    # Cross-tenant history must NOT leak into tenant A's worker context.
    assert "SUMMARY_TENANT_B" not in rw_section
    assert "tenant-B task" not in rw_section


def test_worker_context_recent_work_untenanted_stable(kanban_home):
    """AC-UNTENANTED-STABLE: on an untenanted board the recent-work output
    is byte-identical to the pre-fix behavior (no tenant filter applied)."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="coder")
        t2 = kb.create_task(conn, title="task-2", assignee="coder")
        now = 1_800_000_000
        _seed_completed_run(conn, t1, "coder", now + 10, "SUMMARY_ONE")
        _seed_completed_run(conn, t2, "coder", now + 20, "SUMMARY_TWO")
        conn.commit()
        ctx = kb.build_worker_context(conn, t2)
    # Extract just the "Recent work" section.
    rw_start = ctx.find("## Recent work by @coder")
    rw_section = ctx[rw_start:] if rw_start >= 0 else ""
    assert rw_section, "Recent work section should be present"
    # The other task's completed run surfaces (no tenant scoping on untenanted board).
    assert "SUMMARY_ONE" in rw_section
    assert "task-1" in rw_section
    # The current task t2 is excluded from its own recent-work (r.task_id != ?).
    assert "SUMMARY_TWO" not in rw_section


# ---------------------------------------------------------------------------
# F4: operator directives (kind='directive')
# ---------------------------------------------------------------------------

def test_add_comment_defaults_to_comment_kind(kanban_home):
    """Existing callers (and inline INSERTs) keep the 'comment' kind."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "user", "ordinary note")
        comments = kb.list_comments(conn, t)
    assert [c.kind for c in comments] == ["comment"]


def test_add_comment_directive_kind_persists(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "operator", "switch to plan B", kind="directive")
        comments = kb.list_comments(conn, t)
    assert [c.kind for c in comments] == ["directive"]


def test_add_comment_rejects_unknown_kind(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        with pytest.raises(ValueError, match="kind"):
            kb.add_comment(conn, t, "operator", "body", kind="bogus")


def test_directive_renders_as_priority_block(kanban_home):
    """A directive surfaces in build_worker_context as a distinct ⚠️ block,
    NOT under the 'comment from worker' framing."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", body="ORIGINAL_BODY_INSTRUCTION")
        kb.add_comment(conn, t, "operator", "STOP — do C instead", kind="directive")
        ctx = kb.build_worker_context(conn, t)
    assert "⚠️ OPERATOR DIRECTIVE — supersedes the task body above" in ctx
    assert "STOP — do C instead" in ctx
    # Distinct framing — a directive must not be rendered as a worker comment.
    assert "comment from worker `operator`" not in ctx


def test_directive_kept_separate_from_regular_comment_thread(kanban_home):
    """Directives go in the priority block; ordinary comments stay in the
    '## Comment thread' section under the worker-comment framing."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "worker", "REGULAR_WORKER_NOTE")
        kb.add_comment(conn, t, "operator", "DIRECTIVE_PAYLOAD", kind="directive")
        ctx = kb.build_worker_context(conn, t)
    # The directive block sits ABOVE the regular comment thread.
    assert ctx.index("OPERATOR DIRECTIVE") < ctx.index("## Comment thread")
    assert "comment from worker `worker`" in ctx
    assert "REGULAR_WORKER_NOTE" in ctx
    # The directive body is not duplicated into the worker-comment thread.
    assert ctx.count("DIRECTIVE_PAYLOAD") == 1


def test_no_directive_block_without_directives(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "worker", "just a note")
        ctx = kb.build_worker_context(conn, t)
    assert "OPERATOR DIRECTIVE" not in ctx


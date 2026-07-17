"""Kanban DB tests: escalation.

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

def _redispatch(conn, task_id):
    """Simulate a bounded auto-retry re-dispatch (NOT an operator resolving
    the escalation): counter reset, re-claimed. Mirrors the raw status flip
    ``auto_retry_blocked_tasks`` performs — no ``unblocked``/``promoted_manual``
    event. Genuine operator resolution (``unblock_task`` / ``promote_task`` /
    ``answer_operator_question``) starts a NEW escalation episode instead and
    is exercised separately below (episode-boundary tests).
    """
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, block_kind=NULL, "
            "block_recurrences=0 WHERE id=? AND status='blocked'",
            (task_id,),
        )
        assert cur.rowcount == 1
    assert kb.claim_task(conn, task_id) is not None


def _make_integration_parked(conn, reason_suffix, *, title="parked finalizer"):
    """Create a task blocked with an ``integration parked: <reason>`` event."""
    tid = kb.create_task(conn, title=title, assignee="coder")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason=f"integration parked: {reason_suffix}")
    return tid


def _patch_integrate(monkeypatch, outcomes):
    """Patch maybe_integrate_on_complete; record call task_ids.

    ``outcomes`` may be a list (popped per call) or a callable(task_id)->dict.
    """
    import hermes_cli.kanban_worktrees as kwt
    calls = []

    def fake(conn, task_id, **kw):
        calls.append(task_id)
        if callable(outcomes):
            return outcomes(task_id)
        return outcomes.pop(0) if outcomes else None

    monkeypatch.setattr(kwt, "maybe_integrate_on_complete", fake)
    return calls


def _make_integration_parked_in_worktree(
    conn,
    reason_suffix,
    *,
    repo=None,
    root="t_chainroot",
    create_worktree=True,
):
    """A parked finalizer whose workspace_path is a provisioned chain worktree,
    so the non-transient branch can route a fixer into it."""
    tid = kb.create_task(conn, title="parked finalizer", assignee="coder")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason=f"integration parked: {reason_suffix}")
    repo_path = Path(repo) if repo is not None else Path(os.environ["HERMES_HOME"]) / "repo"
    wt = str(repo_path / ".worktrees" / "kanban" / root)
    if create_worktree:
        Path(wt).mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(conn, tid, wt)
    return tid, wt, root


def _close_task(conn, task_id, status="failed"):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id),
        )


def _block_as_review_revision(conn, task_id, *, reason="review needs changes"):
    # Enter through the real review-run completion path. Direct callers are
    # deliberately forbidden from forging the internal review_revision kind.
    with kb.write_txn(conn):
        conn.execute(
            """
            UPDATE tasks
               SET status = 'review',
                   claim_lock = NULL,
                   claim_expires = NULL
             WHERE id = ?
            """,
            (task_id,),
        )
    claimed = kb.claim_review_task(
        conn,
        task_id,
        reviewer_profile="reviewer",
    )
    assert claimed is not None
    assert claimed.current_run_id is not None
    assert kb.complete_task(
        conn,
        task_id,
        summary=reason,
        metadata={"verdict": "REQUEST_CHANGES"},
        expected_run_id=claimed.current_run_id,
        review_gate=True,
    )


def test_escalation_coalesce_same_class_writes_one_raw_event(kanban_home):
    """A re-dispatched root that exhausts its ladder again with the SAME class
    must NOT append a duplicate raw operator_escalation event — at most one raw
    event per class — yet every cycle still records a gave_up + classification
    so the repetition is never invisibly dropped."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        kb.claim_task(conn, tid)
        # cycle 1: spawn_failed -> transient class -> first escalation written
        assert kb._record_task_failure(
            conn, tid, "spawn boom", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        _redispatch(conn, tid)
        # cycle 2: spawn_failed again -> SAME transient class -> coalesced
        assert kb._record_task_failure(
            conn, tid, "spawn boom 2", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    heiler = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
    # one RAW event for the (single) class — the repeat is coalesced away
    assert len(escalations) == 1
    # but BOTH cycles left a gave_up (the counter source) ...
    assert len(gave_ups) == 2
    # ... and BOTH cycles were classified into the by-class ledger (the count
    # of reported real problems must NOT shrink because of the coalesce)
    assert len(heiler) == 2


def test_escalation_coalesce_new_class_stays_visible(kanban_home):
    """A genuinely NEW failure class on the same root is NOT suppressed: it
    writes its own raw escalation event and stays visible, while same-class
    repeats are still coalesced to one raw event per class."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        kb.claim_task(conn, tid)
        kb._record_task_failure(
            conn, tid, "spawn boom", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        _redispatch(conn, tid)
        kb._record_task_failure(  # same transient class -> coalesced
            conn, tid, "spawn boom 2", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        _redispatch(conn, tid)
        kb._record_task_failure(  # NEW real-bug class -> fresh raw event
            conn, tid, "tests failed: assertion", outcome="crashed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    classes = sorted(
        {kb._classify_escalation_payload(e.payload or {})[0] for e in escalations}
    )
    assert len(gave_ups) == 3              # every cycle recorded
    assert len(escalations) == 2           # one per class (transient + real-bug)
    assert classes == ["real-bug", "transient"]


def test_escalation_coalesce_decision_queue_counter(kanban_home):
    """decision_queue surfaces the full escalation count (every cycle, incl. the
    coalesced ones) plus the distinct classes and how many repeats were
    coalesced — so the operator sees N escalations, nothing silently dropped."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        cycles = [
            ("spawn boom", "spawn_failed"),       # transient
            ("spawn boom 2", "spawn_failed"),     # same class -> coalesced
            ("tests failed: assertion", "crashed"),  # new real-bug class
        ]
        for i, (err, oc) in enumerate(cycles):
            if i == 0:
                kb.claim_task(conn, tid)
            assert kb._record_task_failure(
                conn, tid, err, outcome=oc, failure_limit=1,
                release_claim=True, end_run=True,
            )
            if i < len(cycles) - 1:
                _redispatch(conn, tid)
        result = kb.decision_queue(conn)

    row = next(d for d in result["decisions"] if d["task_id"] == tid)
    assert row["kind"] == "operator_escalation"
    # N escalations of this root = every breaker-trip cycle, not just the raw
    # events left in the ledger after coalescing
    assert row["escalation_count"] == 3
    assert sorted(row["escalation_classes"]) == ["real-bug", "transient"]
    # one same-class duplicate was coalesced (3 cycles -> 2 raw events)
    assert row["coalesced_repeats"] == 1


def test_escalation_coalesce_counts_gave_up_after_non_gave_up_writer(kanban_home):
    """Mixed writer + gave_up regression: when a NON-gave_up escalation writer
    (here budget-runaway) already escalated a class, a later breaker-trip cycle
    of the SAME class is coalesced — and that suppressed cycle must STILL be
    explicit in decision_queue. The raw event from the non-gave_up writer shares
    no event with the coalesced gave_up, so a max(raw, gave_up) counter would
    silently lose the second cycle (escalation_count=1, coalesced_repeats=0)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        # NON-gave_up writer: a budget-runaway park writes a raw operator_escalation
        # (HEILER-CLASSIFY-SIGNAL-GAP-S2: classifies capacity, not unclassified)
        # without going through the gave_up branch.
        fresh = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert kb._park_budget_runaway(conn, fresh, token_sum=999, cap=10, runs=3)
        # re-dispatch, then trip the breaker with the SAME (capacity) class
        _redispatch(conn, tid)
        assert kb._record_task_failure(
            conn, tid, "iteration budget exhausted", outcome="unknown",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)
        result = kb.decision_queue(conn)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    # one RAW event (the non-gave_up writer); the same-class gave_up is coalesced
    assert len(escalations) == 1
    assert len(gave_ups) == 1
    # the coalesced gave_up cycle carries the explicit flag so the counter sees it
    assert gave_ups[0].payload.get("escalation_coalesced") is True

    row = next(d for d in result["decisions"] if d["task_id"] == tid)
    assert row["kind"] == "operator_escalation"
    # 2 escalation cycles total: the budget-runaway park + the coalesced gave_up
    assert row["escalation_count"] == 2
    assert row["escalation_classes"] == ["capacity"]
    # exactly one suppressed repeat, made explicit (was invisibly dropped before)
    assert row["coalesced_repeats"] == 1


def test_escalation_coalesce_resets_after_operator_unblock(kanban_home):
    """Episode-scoped write-side gate (mirrors ``_operator_escalation_is_active``
    / commit 28c296871): a genuine operator resolution (``unblock_task``)
    starts a NEW escalation episode, so the SAME class recurring afterwards
    must escalate again — not be coalesced away forever."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb._record_task_failure(
            conn, tid, "spawn boom", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        # Operator resolves the hold (NOT the bounded auto-retry lane) —
        # ends the current escalation episode.
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid) is not None
        # Same (transient) class recurs in the new episode.
        assert kb._record_task_failure(
            conn, tid, "spawn boom 2", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    assert len(gave_ups) == 2
    # NOT coalesced: the operator unblock reset the episode boundary.
    assert len(escalations) == 2
    assert gave_ups[1].payload.get("escalation_coalesced") is False


def test_4a_scheduled_overdue_is_unblocked_once(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="wake later", assignee="coder")
        assert kb.schedule_task(conn, tid, reason="timer") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'scheduled'",
                (now - 7200, tid),
            )

        first = kb.no_silent_stall_sweep(
            conn, now=now, min_age_seconds=3600,
        )
        second = kb.no_silent_stall_sweep(
            conn, now=now + 10, min_age_seconds=3600,
        )
        task = kb.get_task(conn, tid)
        markers = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.NO_SILENT_STALL_EVENT
        ]

    assert first["self_healed"] == [
        {"task_id": tid, "class": "scheduled_overdue", "action": "unblocked"}
    ]
    assert second["self_healed"] == []
    assert task.status == "ready"
    assert len(markers) == 1
    assert markers[0].payload["action"] == "nudged"


def test_4a_scheduled_future_due_is_not_treated_as_stall(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="wake at due time", assignee="coder")
        assert kb.schedule_task(conn, tid, reason="timer") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET due_at = ? WHERE id = ?",
                (now + 3600, tid),
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'scheduled'",
                (now - 7200, tid),
            )

        summary = kb.no_silent_stall_sweep(
            conn, now=now, min_age_seconds=3600,
        )
        task = kb.get_task(conn, tid)

    assert task.status == "scheduled"
    assert summary["self_healed"] == []


def test_4a_scheduled_due_is_unblocked_without_stall_age(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="wake now", assignee="coder")
        assert kb.schedule_task(conn, tid, reason="timer") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET due_at = ? WHERE id = ?",
                (now, tid),
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'scheduled'",
                (now - 60, tid),
            )

        summary = kb.no_silent_stall_sweep(
            conn, now=now, min_age_seconds=3600,
        )
        task = kb.get_task(conn, tid)

    assert task.status == "ready"
    assert summary["self_healed"] == [
        {"task_id": tid, "class": "scheduled_due", "action": "unblocked"}
    ]


def test_4a_scheduled_due_skips_funnel_root(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="operator funnel root",
            assignee="research",
            created_by="family",
        )
        assert kb.schedule_task(conn, tid, reason="funnel hold") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET due_at = ? WHERE id = ?",
                (now - 1, tid),
            )

        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)

    assert task.status == "scheduled"
    assert summary["skipped_funnel"] == [tid]
    assert summary["self_healed"] == []


def test_4a_scheduled_overdue_skips_operator_held_chain(kanban_home):
    # A freigabe:operator PlanSpec chain is held in 'scheduled' for explicit
    # operator release (propose-and-wait). The no-silent-stall sweep must NOT
    # mistake that intentional hold for a stall and nudge it live — neither the
    # held root NOR its held build children (a dep-free build child would
    # dispatch behind the operator's back if nudged to ready). Built via the
    # REAL decompose topology (links: parent_id=child, child_id=root), not a
    # hand-rolled link, so the child->root walk is exercised in production shape.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="held root", assignee="orchestrator", triage=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET freigabe = 'operator', due_at = ? WHERE id = ?",
                (now - 1, root),
            )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "build child"}],
            initial_child_status="scheduled",
        )
        assert child_ids is not None and len(child_ids) == 1
        build_child = child_ids[0]

        # Real F1 hold: both root and build child land held in 'scheduled'.
        assert kb.get_task(conn, root).status == "scheduled"
        assert kb.get_task(conn, build_child).status == "scheduled"

        # Age past the no-silent-stall window — both the 'scheduled' event and the
        # created_at fallback the sweep reads, so age is never the reason to skip.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id IN (?, ?) AND kind = 'scheduled'",
                (now - 7200, root, build_child),
            )
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id IN (?, ?)",
                (now - 7200, root, build_child),
            )

        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, build_child)

    # The intentional hold survived: neither root nor child was nudged.
    assert root_task.status == "scheduled"
    assert child_task.status == "scheduled"
    # Recorded as a deliberate skip, not a self-heal.
    assert root in summary.get("skipped_held", [])
    assert build_child in summary.get("skipped_held", [])
    assert summary["self_healed"] == []


def test_4a_scheduled_overdue_skips_ui_real_held_root(kanban_home):
    # The ui-real operator hold (Phase 4 A) shares the scheduled-park mechanism
    # and must be exempt from the stall nudge for the same reason.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="ui-real held root", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET live_test_depth = 'ui-real' WHERE id = ?",
                (root,),
            )
        assert kb.schedule_task(conn, root, reason="ui-real hold") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'scheduled'",
                (now - 7200, root),
            )
        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)

    assert root_task.status == "scheduled"
    assert root in summary.get("skipped_held", [])
    assert summary["self_healed"] == []


def test_4a_decompose_failure_parks_once_and_skips_funnel_root(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        normal = kb.create_task(
            conn, title="normal triage", assignee="coder", triage=True,
        )
        funnel = kb.create_task(
            conn,
            title="funnel triage",
            assignee="coder",
            triage=True,
            created_by="family",
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, normal)
            kb.record_decompose_failure(conn, funnel)

        first = kb.no_silent_stall_sweep(conn, now=now)
        second = kb.no_silent_stall_sweep(conn, now=now + 1)
        normal_task = kb.get_task(conn, normal)
        funnel_task = kb.get_task(conn, funnel)
        normal_escalations = [
            e for e in kb.list_events(conn, normal)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        funnel_escalations = [
            e for e in kb.list_events(conn, funnel)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert {"task_id": normal, "class": "triage_decompose_failed"} in first["parked"]
    assert second["parked"] == []
    assert normal_task.status == "blocked"
    assert funnel_task.status == "triage"
    assert len(normal_escalations) == 1
    assert funnel_escalations == []


def test_4a_decompose_failure_skips_operator_held_chain(kanban_home):
    # The triage-decompose-failed branch parks any task with
    # decompose_failed >= limit whose status is not done/archived — and
    # 'scheduled' is in that set. A freigabe:operator chain is held in
    # 'scheduled' for explicit operator release; the held root carries the
    # decompose_failed counter (the counter reset on a successful decompose is
    # fail-soft and runs in a SEPARATE txn after the scheduled-flip commits, so
    # a crash / swallowed-error window can leave a held root 'scheduled' with a
    # non-zero counter). The sweep must NOT mistake that intentional hold for a
    # decompose stall and park it to 'blocked' — that strips the operator hold
    # and makes the chain eligible for auto_retry_blocked_tasks -> 'ready',
    # building the held proposal behind the operator's back. Built via the REAL
    # decompose topology (links: parent_id=child, child_id=root) so the
    # child->root walk in _is_operator_held is exercised in production shape.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="held root", assignee="orchestrator", triage=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET freigabe = 'operator' WHERE id = ?", (root,)
            )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "build child"}],
            initial_child_status="scheduled",
        )
        assert child_ids is not None and len(child_ids) == 1
        build_child = child_ids[0]

        # Real F1 hold: both root and build child land held in 'scheduled'.
        assert kb.get_task(conn, root).status == "scheduled"
        assert kb.get_task(conn, build_child).status == "scheduled"

        # Push BOTH past the decompose-failure limit so the §3 query would
        # select them absent the hold exemption (root = realistic vector,
        # child = exercises the child->root walk).
        for _ in range(3):
            kb.record_decompose_failure(conn, root)
            kb.record_decompose_failure(conn, build_child)

        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, build_child)
        root_escalations = [
            e for e in kb.list_events(conn, root)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    # The intentional hold survived: neither was parked to 'blocked'.
    assert root_task.status == "scheduled"
    assert child_task.status == "scheduled"
    # Recorded as a deliberate skip, not a stall park, and no escalation fired.
    assert root in summary.get("skipped_held", [])
    assert build_child in summary.get("skipped_held", [])
    assert summary["parked"] == []
    assert root_escalations == []


def test_4a_decompose_failure_exemption_is_scoped_to_active_hold(kanban_home):
    # The §3 hold exemption must protect ONLY a chain still actively held in
    # 'scheduled'. Once the operator RELEASES (release_freigabe_hold flips the
    # root 'scheduled' -> 'todo' but never clears the freigabe column), the row
    # is no longer held and must regain its pre-exemption behaviour — i.e. a
    # released root carrying decompose_failed >= limit is park-eligible again,
    # exactly as a plain non-held 'todo' root would be. Otherwise the
    # exemption would permanently shield a real decompose stall behind a stale
    # freigabe='operator' tag. Guards the asymmetry in _is_operator_held (the
    # direct _held check must be scheduled-gated like the child->root walk).
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="released root", assignee="orchestrator", triage=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET freigabe = 'operator' WHERE id = ?", (root,)
            )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "build child"}],
            initial_child_status="scheduled",
        )
        assert child_ids is not None and len(child_ids) == 1

        for _ in range(3):
            kb.record_decompose_failure(conn, root)

        # Operator releases: root 'scheduled' -> 'todo', freigabe still 'operator'.
        assert kb.release_freigabe_hold(conn, root) is True
        assert kb.get_task(conn, root).status == "todo"

        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)

    # The released root is no longer exempt: a genuine decompose stall is parked.
    assert root not in summary.get("skipped_held", [])
    assert {"task_id": root, "class": "triage_decompose_failed"} in summary["parked"]
    assert root_task.status == "blocked"


def test_release_freigabe_hold_releases_transitive_chain_members(kanban_home):
    # Regression for operator PlanSpec approval: a sink/root can have only the
    # final review task as a direct parent while earlier build tasks are ancestors
    # of that review. Releasing only parent_ids(root) leaves the real first task
    # stuck in scheduled, so the chain appears to start as a single task.
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="held planspec root", triage=True, freigabe="operator")
        build = kb.create_task(conn, title="build", assignee="premium")
        review = kb.create_task(conn, title="review", assignee="reviewer", parents=[build])
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (review, root))
            conn.execute(
                "UPDATE tasks SET status='scheduled' WHERE id IN (?, ?, ?)",
                (root, build, review),
            )

        assert kb.release_freigabe_hold(conn, root) is True

        assert kb.get_task(conn, root).status == "todo"
        assert kb.get_task(conn, build).status == "ready"
        assert kb.get_task(conn, review).status == "todo"


def test_release_uireal_root_promotes_scheduled_chain_members(kanban_home):
    # S1 regression: release_uireal_root only flipped the root scheduled->todo
    # and left the chain's scheduled children stranded forever (recompute_ready
    # never auto-releases ui-real roots' children by design). Must mirror
    # release_freigabe_hold: promote held children via unblock_task +
    # recompute_ready too, so the chain actually dispatches after release.
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="ui-real root", triage=True)
        build = kb.create_task(conn, title="build", assignee="premium")
        review = kb.create_task(conn, title="review", assignee="reviewer", parents=[build])
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (review, root))
            conn.execute(
                "UPDATE tasks SET live_test_depth='ui-real', status='scheduled' WHERE id = ?",
                (root,),
            )
            conn.execute(
                "UPDATE tasks SET status='scheduled' WHERE id IN (?, ?)",
                (build, review),
            )

        assert kb.release_uireal_root(conn, root, author="pytest") is True

        assert kb.get_task(conn, root).status == "todo"
        assert kb.get_task(conn, build).status == "ready"
        assert kb.get_task(conn, review).status == "todo"


def test_decompose_failure_is_transient_pure_rule():
    # HEILER-DECOMPOSE-FALLBACK-S1: pure classifier that tells a transient/infra
    # decompose failure (aux client down, LLM error, benign race) from a genuine
    # spec defect. The transient set is sourced from the ok=False reason strings
    # in kanban_decompose.decompose_task. Case-insensitive substring match; a
    # None/empty/unknown reason is NOT transient (defaults to the unchanged
    # bad-spec escalation, so a counter bumped without a reason behaves as before).
    transient = [
        "no auxiliary client configured",
        "auxiliary client unavailable",
        "LLM error: APITimeoutError",
        "LLM returned malformed JSON",
        "DB error: OperationalError",
        "task moved out of triage before promotion",
        "task moved out of triage before decomposition",
        "unknown task id",
        "task is not in triage (status='todo')",
        "decompose_task crashed: RuntimeError",
    ]
    for r in transient:
        assert kb._decompose_failure_is_transient(r), r
    genuine = [
        "decomposer returned fanout=false with no title/body",
        "decomposer returned fanout=true with empty tasks list",
        "tasks[0].title is missing or empty",
        "DB rejected graph: invalid assignee",
    ]
    for r in genuine:
        assert not kb._decompose_failure_is_transient(r), r
    assert not kb._decompose_failure_is_transient(None)
    assert not kb._decompose_failure_is_transient("")


def test_decompose_transient_failure_retries_then_parks_transient(kanban_home):
    # HEILER-DECOMPOSE-FALLBACK-S1 (AC-1): a decompose failure whose recorded
    # reason is transient/infra must NOT escalate as bad-spec. It runs through the
    # SAME bounded, backoff-spaced transient-retry budget the rate-limit loop uses
    # (the task stays in triage so the decomposer re-attempts it once the aux
    # client recovers — a success would reset the counter and escalate nothing);
    # only an EXHAUSTED budget escalates, and then classified TRANSIENT (infra),
    # never bad-spec. Sweeps step the logical clock; the per-attempt backoff is
    # keyed on the real-time event stamp (moot under a far-future ``now``, exactly
    # like the rate-limit test).
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="decompose triage", assignee="coder", triage=True,
        )
        # Record limit failures carrying a transient reason (aux client down).
        for _ in range(3):
            kb.record_decompose_failure(
                conn, tid, reason="no auxiliary client configured",
            )

        s1 = kb.no_silent_stall_sweep(conn, now=now)
        s2 = kb.no_silent_stall_sweep(conn, now=now + 1)
        # TRANSIENT_RETRY_LIMIT (=2) rounds spent → budget exhausted → parks.
        s3 = kb.no_silent_stall_sweep(conn, now=now + 2)

        task = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    # Never parked or classified as bad-spec on any sweep.
    for s in (s1, s2, s3):
        assert {"task_id": tid, "class": "triage_decompose_failed"} not in s["parked"]
    # Retried within budget, then transient-parked exactly once.
    assert {"task_id": tid, "class": "triage_decompose_transient"} in s3["parked"]
    assert task.status == "blocked"
    assert len(escalations) == 1
    assert kb.HEILER_CLASS_TRANSIENT in heiler
    assert kb.HEILER_CLASS_BAD_SPEC not in heiler


def test_decompose_genuine_defect_still_parks_bad_spec(kanban_home):
    # HEILER-DECOMPOSE-FALLBACK-S1 (AC-2 guard): a genuine spec defect (the
    # decomposer engaged but the spec cannot be turned into work) must STILL
    # escalate as bad-spec — never silently pushed through as a single atomic
    # task. Unchanged path; this pins that the discrimination did not weaken it.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="vague triage", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(
                conn, tid,
                reason="decomposer returned fanout=false with no title/body",
            )
        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]
    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert task.status == "blocked"
    assert kb.HEILER_CLASS_BAD_SPEC in heiler
    assert kb.HEILER_CLASS_TRANSIENT not in heiler


def test_decompose_bad_spec_park_reason_carries_cause(kanban_home):
    # The bad-spec park used to discard the already-read latest_reason,
    # escalating with an ursachenlose "auto_decompose failed N times" signature
    # (11x identical signatures on the live board in one week — the operator had
    # to open events every time to triage). _latest_decompose_failure_reason is
    # already read in this block for the transient check just above; surface it
    # in reason + evidence for the (non-transient) bad-spec park too.
    now = 1_900_000_000
    cause = "decomposer returned fanout=false with no title/body"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="vague triage cause", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid, reason=cause)
        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert task.status == "blocked"
    assert len(escalations) == 1
    why_now = escalations[0].payload["why_now"]
    assert f"auto_decompose failed 3 times ({cause})" in why_now
    assert escalations[0].payload["evidence"]["latest_reason"] == cause
    # Classification stays bad-spec: the stall_class STRONG mapping (checked
    # before free-text signals in _classify_failure) wins regardless of what
    # the now-enriched reason text says.
    assert kb.HEILER_CLASS_BAD_SPEC in heiler
    assert kb.HEILER_CLASS_TRANSIENT not in heiler


def test_decompose_bad_spec_park_reason_truncates_long_cause(kanban_home):
    # done_when requires the surfaced cause to be truncated (~200 chars) so a
    # verbose decomposer error can't blow up the reason/evidence text.
    now = 1_900_000_000
    long_cause = (
        "decomposer returned fanout=false with no title/body: " + "x" * 250
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="vague triage long cause", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid, reason=long_cause)
        s = kb.no_silent_stall_sweep(conn, now=now)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    excerpt = escalations[0].payload["evidence"]["latest_reason"]
    assert excerpt == long_cause[:200]
    assert len(excerpt) == 200


def test_decompose_no_reason_event_preserves_bad_spec_park(kanban_home):
    # Back-compat: a decompose_failed counter bumped WITHOUT a reason (older code
    # path / direct counter use) has no decompose_attempt_failed event, so the
    # latest-reason lookup returns None → not transient → unchanged bad-spec park.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="no-reason triage", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid)  # no reason
        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert task.status == "blocked"


def test_decompose_no_reason_after_reset_overrides_stale_transient_reason(
    kanban_home,
):
    # Mixed-history guard: a successful/reset decompose attempt must create a new
    # boundary. Later no-reason failures are legacy/genuine-defect bumps and must
    # not inherit an older transient reason from before the reset.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="mixed-history triage", assignee="coder", triage=True,
        )
        kb.record_decompose_failure(
            conn, tid, reason="no auxiliary client configured",
        )
        kb.reset_decompose_failed(conn, tid)

        for _ in range(3):
            kb.record_decompose_failure(conn, tid)  # no reason after reset

        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert {"task_id": tid, "class": "triage_decompose_transient"} not in s["transient_retried"]
    assert task.status == "blocked"
    assert kb.HEILER_CLASS_BAD_SPEC in heiler
    assert kb.HEILER_CLASS_TRANSIENT not in heiler


def test_4a_rate_limited_loop_retries_then_parks(kanban_home):
    # HEILER-TRANSIENT-RETRY-BUDGET-S1: a persistent rate-limit loop is transient
    # infra, so it now runs through a BOUNDED, backoff-spaced retry budget before
    # paging the operator (instead of escalating on the first detection). Each
    # retry round emits a ``transient_retry`` event (NOT a heiler_classification),
    # so a root that self-heals within budget never lands a transient escalation;
    # only the EXHAUSTED budget escalates — exactly once.
    # Sweeps step the logical clock forward; the per-attempt backoff is keyed on
    # the real-time event stamp (so it bites in production but is moot under a
    # far-future test ``now``, exactly like the §5 integration-retry tests). The
    # backoff rule itself is covered by test_transient_retry_phase_pure_rule.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="quota loop", assignee="coder")
        with kb.write_txn(conn):
            for i in range(3):
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, outcome, error, started_at, ended_at) "
                    "VALUES (?, 'coder', 'rate_limited', 'rate_limited', "
                    "'429 quota', ?, ?)",
                    (tid, now - 100 - i, now - 90 - i),
                )
        # Rounds 1..LIMIT: retry, NOT park.
        retried = []
        for k in range(kb.TRANSIENT_RETRY_LIMIT):
            s = kb.no_silent_stall_sweep(
                conn, now=now + k, rate_limit_attempt_limit=3,
            )
            retried.append(s)
        t_mid = kb.get_task(conn, tid)
        # Budget exhausted: escalate exactly once.
        s_park = kb.no_silent_stall_sweep(
            conn, now=now + kb.TRANSIENT_RETRY_LIMIT, rate_limit_attempt_limit=3,
        )
        t_park = kb.get_task(conn, tid)
        # Idempotent: no second escalation.
        s_after = kb.no_silent_stall_sweep(
            conn, now=now + kb.TRANSIENT_RETRY_LIMIT + 1, rate_limit_attempt_limit=3,
        )
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heiler = [e for e in kb.list_events(conn, tid)
                  if e.kind == kb.HEILER_CLASSIFICATION_EVENT]

    # Every round below the limit retried, stayed ready, no escalation/park.
    for s in retried:
        assert {"task_id": tid, "class": "rate_limited_loop"} in s["transient_retried"]
        assert s["parked"] == []
    assert t_mid.status == "ready"
    assert t_mid.transient_retry_count == kb.TRANSIENT_RETRY_LIMIT
    # Budget spent → escalate once, byte-identically to the old park path.
    assert {"task_id": tid, "class": "rate_limited_loop"} in s_park["parked"]
    assert t_park.status == "blocked"
    assert s_after["parked"] == [] and s_after["transient_retried"] == []
    assert len(escalations) == 1
    assert escalations[0].payload["attempts_already_made"] == 3
    # The retries themselves never classified the root as a transient escalation;
    # only the final park did (the AC-1 ledger reduction lever).
    assert kb.TRANSIENT_RETRY_EVENT in kinds
    assert len(heiler) == 1
    assert heiler[0].payload["class"] == "transient"


def test_park_integration_comments_dirty_artifact_recovery(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="artifact policy miss", assignee="coder")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None
        ok = kb._park_integration(
            conn,
            tid,
            {
                "action": "parked",
                "reason": (
                    "ARTIFACT_POLICY_MISSING: coverage/index.html. "
                    "Recovery: extend the artifact policy."
                ),
                "branch": "kanban/t_artifact_policy",
                "park_class": "ARTIFACT_POLICY_MISSING",
                "dirty_files": ["coverage/index.html"],
            },
            expected_run_id=task.current_run_id,
        )
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?",
            (tid,),
        ).fetchall()

    assert ok is True
    assert comments[-1]["author"] == "integrator"
    assert "ARTIFACT_POLICY_MISSING" in comments[-1]["body"]
    assert "coverage/index.html" in comments[-1]["body"]
    assert "Recovery: extend the artifact policy" in comments[-1]["body"]
    assert "worker contract" not in comments[-1]["body"]


def test_integration_retry_skips_active_operator_escalation(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(
            conn, "chain worktree has uncommitted changes: foo.py",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": tid, "title": "parked finalizer"},
                    "why_now": "operator must decide whether to retry integration",
                    "attempts_already_made": 1,
                    "evidence": {"reason": "integration parked"},
                    "recommended_human_action": "inspect held integration park",
                },
            )
        calls = _patch_integrate(monkeypatch, [{
            "action": "merged", "branch": "kanban/chain-x",
            "merge_commit": "abc123def456", "target": "main",
        }])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)

    assert calls == []
    assert task.status == "blocked"
    assert tid in summary.get("skipped_held", [])
    assert summary["self_healed"] == []


def test_integration_retry_transient_park_reintegrates_and_completes(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(
            conn, "chain worktree has uncommitted changes: foo.py",
        )
        calls = _patch_integrate(monkeypatch, [{
            "action": "merged", "branch": "kanban/chain-x",
            "merge_commit": "abc123def456", "target": "main",
        }])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert calls == [tid]                       # re-integration WAS attempted
    assert task.status == "done"                # completed, NOT ready
    assert task.integration_retry_count == 1
    assert kb.INTEGRATION_RETRY_EVENT in kinds
    assert kb.INTEGRATION_RETRY_SUCCEEDED_EVENT in kinds
    assert "operator_escalation" not in kinds   # no premature escalation
    assert {
        "task_id": tid, "class": "integration_retry", "action": "reintegrated",
    } in summary["self_healed"]


@pytest.mark.parametrize("reason_suffix", [
    "merge conflict/failure (aborted): foo.py",
    "post-merge gate failed: vitest 3 failing",
])
def test_integration_retry_non_transient_no_worktree_escalates(
    kanban_home, monkeypatch, reason_suffix,
):
    # needs_orchestrator park WITHOUT a provisioned worktree to fix in (the
    # park reason here is on a scratch finalizer): no transient retry AND no
    # fixer to route to → escalate byte-identically to the needs_operator path.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(conn, reason_suffix)
        calls = _patch_integrate(monkeypatch, [])  # any call would be a bug
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [k for k in kinds if k == kb.OPERATOR_ESCALATION_EVENT]

    assert calls == []                          # merge conflict/red gate: NO retry
    assert task.status == "blocked"
    assert task.integration_retry_count == 0
    assert len(escalations) == 1               # classified + escalated
    # No worktree → no fixer dispatched (byte-equal to the old escalate path).
    assert kb.CONFLICT_FIXER_DISPATCHED_EVENT not in kinds
    assert summary["conflict_fixer_dispatched"] == []
    assert {"task_id": tid, "class": "integration_parked"} in summary["parked"]


def test_integration_retry_count_separate_from_auto_retry_count(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    reason = "dirty files in live checkout overlap the branch diff: a.py"
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(conn, reason)
        # Re-park (still transient) so the task stays blocked and we can read
        # the counters without it completing.
        _patch_integrate(monkeypatch, [{"action": "parked", "reason": reason}])
        kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)

    assert task.integration_retry_count == 1    # OWN counter advanced
    assert task.auto_retry_count == 0           # shared premium/opus ladder untouched
    assert task.status == "blocked"             # re-parked, NOT ready


def test_integration_retry_bounded_escalates_after_two_rounds(
    kanban_home, monkeypatch,
):
    reason = (
        "live checkout has an operation in progress (rebase): "
        ".git/rebase-merge"
    )
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(conn, reason)
        calls = _patch_integrate(
            monkeypatch, lambda _t: {"action": "parked", "reason": reason},
        )
        s1 = kb.no_silent_stall_sweep(conn, now=1_900_000_000)
        t1 = kb.get_task(conn, tid)
        s2 = kb.no_silent_stall_sweep(conn, now=1_900_000_100)
        t2 = kb.get_task(conn, tid)
        s3 = kb.no_silent_stall_sweep(conn, now=1_900_000_200)
        t3 = kb.get_task(conn, tid)
        s4 = kb.no_silent_stall_sweep(conn, now=1_900_000_300)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert t1.integration_retry_count == 1 and t1.status == "blocked"
    assert t2.integration_retry_count == 2 and t2.status == "blocked"
    assert len(calls) == 2                       # only 2 transient retry rounds
    assert t3.status == "blocked"                # bounded — never ready
    assert len(escalations) == 1                 # escalated exactly once (round 3)
    assert {
        "task_id": tid, "class": "integration_retry_exhausted",
    } in s3["parked"]
    assert s4["parked"] == []                    # idempotent: no 2nd escalation
    assert s1["parked"] == [] and s2["parked"] == []


def test_integration_retry_repark_turned_non_transient_escalates(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(
            conn, "chain worktree has uncommitted changes: foo.py",
        )
        # First (and only) attempt re-parks with a NON-transient reason.
        calls = _patch_integrate(monkeypatch, [{
            "action": "parked",
            "reason": "merge conflict/failure (aborted): foo.py",
        }])
        s1 = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert calls == [tid]                        # one attempt happened
    assert task.integration_retry_count == 1
    assert task.status == "blocked"
    assert len(escalations) == 1                 # reclassified → escalate, stop
    assert {"task_id": tid, "class": "integration_parked"} in s1["parked"]


# ---------------------------------------------------------------------------
# Heiler: GENERAL bounded transient-INFRA retry lane (HEILER-TRANSIENT-RETRY-
# BUDGET-S1). spawn_failed / rate_limited_loop / scheduled_overdue run through a
# bounded, backoff-spaced retry budget (OWN counter, never auto_retry_count /
# integration_retry_count) before paging the operator.
# ---------------------------------------------------------------------------

def test_transient_retry_phase_pure_rule():
    bo = kb.TRANSIENT_RETRY_BACKOFF_SECONDS
    lim = kb.TRANSIENT_RETRY_LIMIT
    # No prior attempt → retry.
    assert kb._transient_retry_phase(0, None, 1000) == "retry"
    # Inside backoff window → backoff.
    assert kb._transient_retry_phase(1, 1000, 1000 + bo - 1) == "backoff"
    # Backoff elapsed → retry.
    assert kb._transient_retry_phase(1, 1000, 1000 + bo + 1) == "retry"
    # Budget spent → exhausted (even with no recent event).
    assert kb._transient_retry_phase(lim, None, 10**12) == "exhausted"


def test_transient_retry_spawn_bounded_then_escalates(kanban_home):
    # The spawn-dispatch helper re-queues a claimed (running) task running→ready
    # up to TRANSIENT_RETRY_LIMIT times — emitting a transient_retry event and
    # NOT a heiler_classification — then falls back to the normal spawn-failure
    # escalation. Driven directly (re-claim each round) so the test does not
    # depend on the real-time backoff (that lives in check_respawn_guard).
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky spawn", assignee="coder", max_retries=1)
        phases = []
        for _ in range(kb.TRANSIENT_RETRY_LIMIT + 1):
            assert kb.claim_task(conn, tid) is not None      # ready → running + run
            phase, auto = kb._spawn_failure_or_transient_retry(
                conn, tid, "spawn boom", failure_limit=1, now=now,
            )
            phases.append((phase, auto))
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        tretry_runs = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs "
            "WHERE task_id = ? AND outcome = ?",
            (tid, kb.TRANSIENT_RETRY_OUTCOME),
        ).fetchone()["n"]

    # First N rounds re-queued; the (N+1)-th exhausted the budget and escalated.
    assert phases[:-1] == [("retried", False)] * kb.TRANSIENT_RETRY_LIMIT
    assert phases[-1][0] == "escalated"
    assert task.transient_retry_count == kb.TRANSIENT_RETRY_LIMIT
    # OWN counter only — the premium/opus + re-integration ladders are untouched.
    assert task.auto_retry_count == 0
    assert task.integration_retry_count == 0
    assert task.status == "blocked"                          # finally escalated
    assert tretry_runs == kb.TRANSIENT_RETRY_LIMIT
    # Bounded retries emit transient_retry, NOT heiler_classification: a self-
    # heal within budget never lands a transient escalation in the ledger. The
    # single heiler_classification belongs to the final (exhausted) escalation.
    assert kb.TRANSIENT_RETRY_EVENT in kinds
    heiler = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
    assert len(heiler) == 1 and heiler[0].payload["class"] == "transient"
    assert len([e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]) == 1


def test_transient_retry_self_heal_leaves_no_transient_escalation(kanban_home):
    # A spawn blip that self-heals on the next attempt must NOT show up as a
    # transient escalation in the ledger — the whole AC-1 reduction lever.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="blip", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        phase, _auto = kb._spawn_failure_or_transient_retry(
            conn, tid, "spawn boom", failure_limit=2, now=now,
        )
        assert phase == "retried"
        task = kb.get_task(conn, tid)
        # Next attempt succeeds.
        assert kb.claim_task(conn, tid) is not None
        kb.complete_task(conn, tid, summary="ok")
        done = kb.get_task(conn, tid)
        ledger = kb.read_escalation_ledger(conn)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert task.status == "ready" and task.transient_retry_count == 1
    # Completed: the transient budget resets so a later, unrelated blip starts
    # from a clean slate.
    assert done.status == "done" and done.transient_retry_count == 0
    assert "operator_escalation" not in kinds
    assert kb.HEILER_CLASSIFICATION_EVENT not in kinds
    assert ledger["roots_by_class"].get("transient", 0) == 0


def test_transient_retry_backoff_guard_defers_then_releases(kanban_home):
    # check_respawn_guard spaces the bounded spawn retries apart: a task whose
    # latest run ended transient_retry is deferred inside the backoff window and
    # respawnable once it elapses.
    real_now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'coder', ?, ?, ?, ?)",
                (tid, kb.TRANSIENT_RETRY_OUTCOME, kb.TRANSIENT_RETRY_OUTCOME,
                 real_now - 5, real_now - 5),
            )
        assert kb.check_respawn_guard(conn, tid) == "transient_retry_backoff"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET ended_at = ? WHERE task_id = ?",
                (real_now - kb.TRANSIENT_RETRY_BACKOFF_SECONDS - 5, tid),
            )
        assert kb.check_respawn_guard(conn, tid) is None


def test_transient_retry_dispatch_end_to_end(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    # End-to-end through dispatch_once: a persistently-raising spawn_fn is
    # re-queued a bounded number of times, then escalates. Backoff is collapsed
    # so the loop can exhaust the budget without wall-clock waits.
    monkeypatch.setattr(kb, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0)

    def boom(task, workspace):
        raise RuntimeError("spawn boom")

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky", assignee="alice", max_retries=1)
        for _ in range(kb.TRANSIENT_RETRY_LIMIT + 3):
            kb.dispatch_once(conn, spawn_fn=boom)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    tretries = [e for e in events if e.kind == kb.TRANSIENT_RETRY_EVENT]
    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    assert len(tretries) == kb.TRANSIENT_RETRY_LIMIT     # bounded re-queues
    assert task.status == "blocked"                      # then escalated
    assert len(escalations) == 1


def test_scheduled_overdue_failed_nudge_retries_then_escalates(
    kanban_home, monkeypatch,
):
    # When the deterministic scheduled-overdue nudge cannot apply (forced here),
    # the sweep runs a bounded transient-retry budget before escalating, instead
    # of paging on the first failed nudge.
    now = 1_900_000_000
    bo = kb.TRANSIENT_RETRY_BACKOFF_SECONDS
    monkeypatch.setattr(kb, "unblock_task", lambda conn, task_id: False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="overdue", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled', created_at = ? WHERE id = ?",
                (now - 10_000, tid),
            )
        s1 = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        t1 = kb.get_task(conn, tid)
        s2 = kb.no_silent_stall_sweep(conn, now=now + bo + 1, min_age_seconds=3600)
        s3 = kb.no_silent_stall_sweep(conn, now=now + 2 * bo + 2, min_age_seconds=3600)
        t3 = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert {"task_id": tid, "class": "scheduled_overdue"} in s1["transient_retried"]
    assert t1.status == "scheduled" and t1.transient_retry_count == 1
    assert {"task_id": tid, "class": "scheduled_overdue"} in s2["transient_retried"]
    assert {"task_id": tid, "class": "scheduled_overdue"} in s3["parked"]
    assert t3.status == "blocked"
    assert len(escalations) == 1


@pytest.mark.parametrize("reason_suffix", [
    "merge conflict/failure (aborted): foo.py",
    "post-merge gate failed: vitest 3 failing",
])
def test_conflict_park_routes_bounded_fixer_not_escalation(
    kanban_home, monkeypatch, reason_suffix,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid, wt, root = _make_integration_parked_in_worktree(conn, reason_suffix)
        calls = _patch_integrate(monkeypatch, [])  # transient retry would be a bug
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        dispatched = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]
        # The fixer subtask itself (payload is already a parsed dict).
        child_id = dispatched[0].payload["child_id"]
        child = kb.get_task(conn, child_id)
        child_kinds = [e.kind for e in kb.list_events(conn, child_id)]

    assert calls == []                                 # no transient retry
    assert task.status == "blocked"                    # parked chain stays blocked
    assert task.integration_retry_count == 0           # transient counter untouched
    # Routed to a fixer, NOT escalated.
    assert kb.OPERATOR_ESCALATION_EVENT not in kinds
    assert {"task_id": tid, "class": "integration_parked"} not in summary["parked"]
    assert len(dispatched) == 1
    assert summary["conflict_fixer_dispatched"] == [
        {"task_id": tid, "child_id": child_id, "attempt": 1}
    ]
    # The fixer is a dispatchable Claude-coder task pinned to the chain worktree.
    # Phase A: the coder-claude lane folds into premium (same claude-cli/Opus runtime).
    assert child.assignee == "premium"
    assert child.status == "ready"
    assert child.workspace_kind == "dir"
    assert child.workspace_path == wt
    assert root in child.title
    # Linked back to the stalled chain on both ends.
    assert f"kanban/{root}" in (child.body or "")   # chain branch in context
    assert "conflict_fixer_for" in child_kinds


def test_conflict_park_missing_worktree_escalates_not_fixer(
    kanban_home, monkeypatch, tmp_path,
):
    """A stale provisioned-path string is not enough to route a fixer.

    Live failure evidence showed workers launched against
    ``.worktrees/kanban/<root>`` paths that no longer existed, then failing with
    ``fatal: cannot change to ...``. The sweep should page the operator instead
    of creating another task pinned to a missing cwd.
    """
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        missing_repo = tmp_path / "repo"
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn,
            "merge conflict/failure (aborted): foo.py",
            repo=str(missing_repo),
            create_worktree=False,
        )
        calls = _patch_integrate(monkeypatch, [])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [k for k in kinds if k == kb.OPERATOR_ESCALATION_EVENT]

    assert calls == []
    assert task.status == "blocked"
    assert len(escalations) == 1
    assert kb.CONFLICT_FIXER_DISPATCHED_EVENT not in kinds
    assert summary["conflict_fixer_dispatched"] == []
    assert {"task_id": tid, "class": "integration_parked"} in summary["parked"]


def test_conflict_park_fixer_not_stacked_while_in_flight(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn, "merge conflict/failure (aborted): foo.py",
        )
        _patch_integrate(monkeypatch, [])
        s1 = kb.no_silent_stall_sweep(conn, now=1_900_000_000)
        # The fixer from round 1 is still open (ready) → round 2 must NOT
        # dispatch a second one.
        s2 = kb.no_silent_stall_sweep(conn, now=1_900_000_500)
        dispatched = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]

    assert len(s1["conflict_fixer_dispatched"]) == 1
    assert s2["conflict_fixer_dispatched"] == []        # waited on the in-flight fixer
    assert s2["parked"] == []                            # not escalated yet
    assert len(dispatched) == 1                          # exactly one fixer exists


def test_conflict_park_fixer_bounded_then_escalates(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "CONFLICT_FIXER_MAX_ATTEMPTS", 2)
    with kb.connect_closing() as conn:
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn, "merge conflict/failure (aborted): foo.py",
        )
        _patch_integrate(monkeypatch, [])

        def _sweep_and_close(ts):
            s = kb.no_silent_stall_sweep(conn, now=ts)
            for entry in s["conflict_fixer_dispatched"]:
                _close_task(conn, entry["child_id"])  # fixer ran, didn't resolve
            return s

        s1 = _sweep_and_close(1_900_000_000)            # attempt 1
        s2 = _sweep_and_close(1_900_000_500)            # attempt 2
        s3 = kb.no_silent_stall_sweep(conn, now=1_900_001_000)  # budget spent
        s4 = kb.no_silent_stall_sweep(conn, now=1_900_001_500)  # idempotent
        task = kb.get_task(conn, tid)
        dispatched = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert len(s1["conflict_fixer_dispatched"]) == 1     # attempt 1
    assert len(s2["conflict_fixer_dispatched"]) == 1     # attempt 2
    assert len(dispatched) == 2                          # bounded at MAX attempts
    assert s3["conflict_fixer_dispatched"] == []         # no 3rd fixer
    assert {"task_id": tid, "class": "integration_parked"} in s3["parked"]
    assert len(escalations) == 1                         # unresolvable → escalate once
    assert task.status == "blocked"
    assert s4["parked"] == []                            # idempotent: no 2nd escalation


def test_conflict_park_needs_operator_unchanged(kanban_home, monkeypatch):
    # An unknown (needs_operator) park is byte-unchanged: it escalates with NO
    # fixer routed, even when a worktree is present.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn, "some entirely unrecognized park reason",
        )
        calls = _patch_integrate(monkeypatch, [])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [k for k in kinds if k == kb.OPERATOR_ESCALATION_EVENT]

    assert calls == []
    assert task.status == "blocked"
    assert len(escalations) == 1
    assert kb.CONFLICT_FIXER_DISPATCHED_EVENT not in kinds
    assert summary["conflict_fixer_dispatched"] == []
    assert {"task_id": tid, "class": "integration_parked"} in summary["parked"]


def test_4a_funnel_root_skipped_but_funnel_build_child_dispatches(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append(task.id)

    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="funnel root", assignee="research", created_by="family",
        )
        done_root = kb.create_task(
            conn, title="approved root", assignee="research", created_by="family",
        )
        kb.claim_task(conn, done_root)
        kb.complete_task(conn, done_root, summary="draft done")
        child = kb.create_task(
            conn,
            title="approved build child",
            assignee="coder",
            created_by="family",
            parents=(done_root,),
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, child)

    assert root_task.status == "ready"
    assert child_task.status == "running"
    assert child in spawned
    assert (root, "funnel_protected") in result.respawn_guarded


def test_4a_funnel_build_child_not_blocked_by_root_contract_rule(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn,
            title="approved root",
            assignee="research",
            created_by="discord-idee",
        )
        kb.claim_task(conn, root)
        kb.complete_task(conn, root, summary="draft approved")
        child = kb.create_task(
            conn,
            title="approved scratch build child",
            assignee="coder",
            created_by="discord-idee",
            parents=(root,),
            kind="code",
        )
        task = kb.get_task(conn, child)
        events = kb.list_events(conn, child)

    assert task is not None
    assert task.status == "ready"
    assert [e for e in events if e.kind == "needs_contract"] == []
    assert [e for e in events if e.kind == "code_task_contract_inferred"]


def test_4a_auto_retry_skips_funnel_root_but_not_funnel_child(
    kanban_home,
):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn,
            title="blocked funnel root",
            assignee="research",
            created_by="family",
        )
        kb.claim_task(conn, root)
        kb.block_task(conn, root, reason="transient")

        done_root = kb.create_task(
            conn,
            title="done funnel root",
            assignee="research",
            created_by="family",
        )
        kb.claim_task(conn, done_root)
        kb.complete_task(conn, done_root, summary="draft done")
        child = kb.create_task(
            conn,
            title="blocked funnel child",
            assignee="research",
            created_by="family",
            parents=(done_root,),
        )
        kb.claim_task(conn, child)
        kb.block_task(conn, child, reason="transient")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, child)

    assert retried == [(child, 1)]
    assert root_task.status == "blocked"
    assert child_task.status == "ready"


def test_review_revision_first_block_auto_retries_once(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review revision retry",
            body="acceptance contract v1",
            assignee="coder",
        )
        _block_as_review_revision(conn, task_id)

        retried = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
            failure_limit=5,
        )
        task = kb.get_task(conn, task_id)

    assert retried == [(task_id, 1)]
    assert task is not None
    assert task.status == "ready"
    assert task.auto_retry_count == 1
    assert task.block_kind is None


def test_review_revision_same_body_after_retry_needs_operator(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="repeated review revision",
            body="acceptance contract unchanged",
            assignee="coder",
        )
        _block_as_review_revision(conn, task_id, reason="first review revision")
        assert kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
            failure_limit=5,
        ) == [(task_id, 1)]

        _block_as_review_revision(conn, task_id, reason="same review revision")
        blocked = kb.get_task(conn, task_id)
        assert blocked is not None
        assert blocked.status == "blocked"
        assert kb.blocked_task_operator_questions(conn, [blocked]) == {task_id: True}

        retried = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
            failure_limit=5,
        )
        task = kb.get_task(conn, task_id)
        skipped = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "auto_retry_skipped"
        ]

    assert retried == []
    assert task is not None
    assert task.status == "blocked"
    assert task.auto_retry_count == 1
    assert any(payload.get("blocked_kind") == "needs_operator" for payload in skipped)


def test_review_revision_changed_body_honors_global_retry_limit(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="bounded review revisions",
            body="acceptance contract v1",
            assignee="coder",
        )
        _block_as_review_revision(conn, task_id, reason="revision one")
        assert kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
            failure_limit=5,
        ) == [(task_id, 1)]

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET body = ? WHERE id = ?",
                ("acceptance contract v2", task_id),
            )
        _block_as_review_revision(conn, task_id, reason="revision two")
        assert kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
            failure_limit=5,
        ) == [(task_id, 2)]

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET body = ? WHERE id = ?",
                ("acceptance contract v3", task_id),
            )
        _block_as_review_revision(conn, task_id, reason="revision three")
        retried = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
            failure_limit=5,
        )
        task = kb.get_task(conn, task_id)
        exhausted = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "auto_retry_exhausted"
        ]

    assert retried == []
    assert task is not None
    assert task.status == "blocked"
    assert task.auto_retry_count == 2
    assert exhausted[-1] == {"attempts": 2, "limit": 2}


def test_review_revision_recurrence_does_not_enter_generic_triage(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review revision recurrence",
            body="same contract",
            assignee="coder",
        )
        _block_as_review_revision(conn, task_id, reason="first revision")
        assert kb.unblock_task(conn, task_id)
        _block_as_review_revision(conn, task_id, reason="second revision")
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "review_revision"
    assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT


def test_4a_dispatcher_heartbeat_file_written(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs operator", assignee="coder")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": tid, "title": "needs operator"},
                    "why_now": "test",
                    "attempts_already_made": 1,
                    "evidence": {},
                    "recommended_human_action": "inspect",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )

    payload = kb.write_kanban_dispatcher_heartbeat(now=now, tick_health="ok")
    path = kb.kanban_dispatcher_heartbeat_path()
    written = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == kb.KANBAN_DISPATCHER_HEARTBEAT_FILENAME
    assert payload["last_tick_at"] == now
    assert payload["last_green_gate_at"] == now
    assert written["counts"]["open_escalations"] == 1


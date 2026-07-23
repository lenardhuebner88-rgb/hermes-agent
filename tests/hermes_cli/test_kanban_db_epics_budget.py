"""Kanban DB tests: epics budget.

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
    _set_task_status,
    _latest_run_verdict,
    _kinds_for,
)

def _seed_run(conn, task_id, *, profile, tokens=0, cost=None, age_seconds=10):
    """Insert a synthetic ended run with token/cost accounting for budget tests."""
    started = int(time.time()) - age_seconds
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
        "outcome, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 'done', ?, ?, 'completed', ?, 0, ?)",
        (task_id, profile, started, started, tokens, cost),
    )
    conn.commit()


def _insert_bare_run(conn, task_id, *, started_at, ended_at=None, verdict=None):
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, verdict) "
        "VALUES (?, 'coder', 'done', ?, ?, ?)",
        (task_id, started_at, ended_at, verdict),
    )
    return cur.lastrowid


def _running_review_for_attention(conn, *, title: str, now: int) -> tuple[str, int]:
    task_id = kb.create_task(conn, title=title, assignee="coder")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'review', claim_lock = NULL, "
            "claim_expires = NULL WHERE id = ?",
            (task_id,),
        )
    task = kb.claim_review_task(conn, task_id, reviewer_profile="reviewer")
    assert task is not None
    assert task.current_run_id is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (now, task.current_run_id),
        )
    return task_id, task.current_run_id


def _count_spawn_retry_events(conn, tid, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?", (tid, kind)
    ).fetchone()[0]


def test_e1_decision_queue_empty_board(kanban_home):
    with kb.connect_closing() as conn:
        result = kb.decision_queue(conn)
    assert result["decisions"] == []
    assert result["count"] == 0
    assert "checked_at" in result


def test_e1_decision_queue_sticky_blocked_appears_once(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stuck", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="needs human eyes")
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["sticky_blocked"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert row["suggested_command"] == f"hermes kanban unblock {t}"
    assert row["age_seconds"] is not None


def test_e1_decision_queue_review_rejection_outranks_sticky(kanban_home):
    """A blocked task whose latest run was a verifier REQUEST_CHANGES is
    classified as review_rejected, not the generic sticky_blocked — appears
    exactly once."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        kb.claim_review_task(conn, t)
        kb.block_task(conn, t, reason="missing tests")
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["review_rejected"]


def test_4b_decision_queue_operator_escalation_outranks_sticky(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="needs operator", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="needs human eyes")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                t,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": t, "title": "needs operator"},
                    "why_now": "retry ladder exhausted",
                    "attempts_already_made": 2,
                    "evidence": {},
                    "recommended_human_action": "inspect",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )
        result = kb.decision_queue(conn)

    assert _kinds_for(t, result) == ["operator_escalation"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert row["reason"] == "retry ladder exhausted"
    assert row["suggested_command"] == f"hermes kanban show {t}"


def test_4b_decision_queue_specific_recovery_classes_beat_generic_escalation(
    kanban_home,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        parked = kb.create_task(conn, title="merge parked", assignee="coder")
        kb.claim_task(conn, parked)
        kb.block_task(conn, parked, reason="integration parked: merge gate red")

        limited = kb.create_task(conn, title="quota loop", assignee="coder")
        with kb.write_txn(conn):
            for i in range(3):
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, outcome, error, started_at, ended_at) "
                    "VALUES (?, 'coder', 'rate_limited', 'rate_limited', "
                    "'429 quota', ?, ?)",
                    (limited, now - 100 - i, now - 90 - i),
                )

        # The rate-limit loop now runs through the bounded transient-retry
        # budget first (HEILER-TRANSIENT-RETRY-BUDGET-S1); sweep until it is
        # exhausted and escalates, so the decision queue sees the recovery class.
        for k in range(kb.TRANSIENT_RETRY_LIMIT + 1):
            kb.no_silent_stall_sweep(conn, now=now + k, rate_limit_attempt_limit=3)
        result = kb.decision_queue(conn, now=now + 10)

    assert _kinds_for(parked, result) == ["integration_parked"]
    assert _kinds_for(limited, result) == ["rate_limited_loop"]
    parked_row = next(d for d in result["decisions"] if d["task_id"] == parked)
    limited_row = next(d for d in result["decisions"] if d["task_id"] == limited)
    assert "integration parked:" in parked_row["reason"]
    assert "rate-limit loop" in limited_row["reason"]


def test_4b_decision_queue_skips_funnel_root_but_not_child(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="funnel root", assignee="research", created_by="family",
        )
        done_root = kb.create_task(
            conn, title="approved root", assignee="research", created_by="family",
        )
        kb.claim_task(conn, done_root)
        kb.complete_task(conn, done_root, summary="draft approved")
        child = kb.create_task(
            conn,
            title="approved build child",
            assignee="coder",
            created_by="family",
            parents=(done_root,),
        )
        with kb.write_txn(conn):
            for task_id in (root, child):
                kb._append_event(
                    conn,
                    task_id,
                    kb.OPERATOR_ESCALATION_EVENT,
                    {
                        "task": {"id": task_id},
                        "why_now": "operator must decide",
                        "attempts_already_made": 1,
                        "evidence": {},
                        "recommended_human_action": "inspect",
                        "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                    },
                )
        result = kb.decision_queue(conn)

    assert _kinds_for(root, result) == []
    assert _kinds_for(child, result) == ["operator_escalation"]


def test_e1_decision_queue_role_fit_held(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="reviewer probe", assignee="reviewer")
        _set_task_status(conn, t, "ready")
        with kb.write_txn(conn):
            kb._append_event(conn, t, "role_fit_held", {"reason": "wants repo gates"})
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["role_fit_held"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert "wants repo gates" in row["reason"]


def test_e1_decision_queue_decompose_failed(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="undecomposable", assignee="coder")
        kb.record_decompose_failure(conn, t)
        kb.record_decompose_failure(conn, t)
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["decompose_failed"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert "2" in row["reason"]


def test_e1_decision_queue_done_task_with_decompose_failed_excluded(kanban_home):
    """A completed task that once failed decompose is not a pending decision."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="done now", assignee="coder")
        kb.record_decompose_failure(conn, t)
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="done", summary="done")
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == []


def test_e1_decision_queue_failsoft_on_corrupt_event_payload(kanban_home):
    """A blocked task with a non-JSON event payload must not crash the queue."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stuck", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="x")
        # Corrupt the blocked-event payload directly.
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'blocked'",
            ("{not json", t),
        )
        conn.commit()
        result = kb.decision_queue(conn)
    # Still surfaces (fail-soft reason fallback), no exception.
    assert _kinds_for(t, result) == ["sticky_blocked"]


# ---------------------------------------------------------------------------
# E3 (N-E3): durable epics + tasks.epic_id + propagation
# ---------------------------------------------------------------------------

def test_e3_epic_id_column_and_table_migrate_idempotently(kanban_home):
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "epic_id" in cols
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "epics" in tables
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert cols2.count("epic_id") == 1


def test_e3_create_and_list_epic(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="Q3 reliability", body="close the loops")
        assert eid.startswith("e_")
        epics = kb.list_epics(conn)
    assert len(epics) == 1
    assert epics[0]["id"] == eid
    assert epics[0]["title"] == "Q3 reliability"
    assert epics[0]["status"] == "open"
    assert epics[0]["task_count"] == 0


def test_e3_create_task_with_epic_sets_column(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        t = kb.create_task(conn, title="member", assignee="coder", epic_id=eid)
        task = kb.get_task(conn, t)
        assert task.epic_id == eid


def test_e3_task_without_epic_is_null(kanban_home):
    """Regression guard: the common path leaves epic_id NULL (pre-E3)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="loner", assignee="coder")
        assert kb.get_task(conn, t).epic_id is None


def test_e3_decompose_propagates_epic_to_children(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        root = kb.create_task(
            conn, title="root", assignee="orchestrator",
            triage=True, epic_id=eid,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "child A"},
                {"title": "child B", "parents": [0]},
            ],
        )
        assert child_ids is not None and len(child_ids) == 2
        for cid in child_ids:
            assert kb.get_task(conn, cid).epic_id == eid


def test_e3_decompose_without_epic_leaves_children_null(kanban_home):
    """Regression guard: a root with no epic → children stay NULL."""
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="root", assignee="orchestrator", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "child A"}],
        )
        assert kb.get_task(conn, child_ids[0]).epic_id is None


def test_e3_epic_stats_count_and_cost(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        t1 = kb.create_task(conn, title="t1", assignee="coder", epic_id=eid)
        t2 = kb.create_task(conn, title="t2", assignee="coder", epic_id=eid)
        # t1 completes, t2 stays open.
        kb.claim_task(conn, t1)
        kb.complete_task(conn, t1, result="done", summary="done")
        # Attribute some cost to t2's run.
        kb.claim_task(conn, t2)
        conn.execute(
            "UPDATE task_runs SET cost_usd = 0.5, input_tokens = 100, "
            "output_tokens = 40 WHERE task_id = ?",
            (t2,),
        )
        conn.commit()
        epic = kb.get_epic(conn, eid)
    assert epic["task_count"] == 2
    assert epic["done_tasks"] == 1
    assert epic["open_tasks"] == 1
    assert epic["cost_usd"] == 0.5
    assert epic["input_tokens"] == 100
    assert {row["id"] for row in epic["tasks"]} == {t1, t2}


def test_e3_close_epic(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        assert kb.close_epic(conn, eid) is True
        assert kb.get_epic(conn, eid)["status"] == "closed"
        assert kb.close_epic(conn, "e_ghost") is False


def test_e3_get_missing_epic_returns_none(kanban_home):
    with kb.connect_closing() as conn:
        assert kb.get_epic(conn, "e_nope") is None


def test_e3_set_task_epic_attach_and_detach(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        t = kb.create_task(conn, title="late member", assignee="coder")
        assert kb.set_task_epic(conn, t, eid) is True
        assert kb.get_task(conn, t).epic_id == eid
        # Detach (explicit None) always works.
        assert kb.set_task_epic(conn, t, None) is True
        assert kb.get_task(conn, t).epic_id is None
        # Both moves leave an audit event.
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (t,),
            )
        ]
        assert kinds.count("epic_changed") == 2


def test_e3_set_task_epic_validates_target(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="member", assignee="coder")
        # Unknown task → False, no crash.
        assert kb.set_task_epic(conn, "t_ghost", None) is False
        # Unknown epic → ValueError.
        with pytest.raises(ValueError, match="not found"):
            kb.set_task_epic(conn, t, "e_ghost")
        # Closed epic → ValueError on attach …
        eid = kb.create_epic(conn, title="done epic")
        kb.close_epic(conn, eid)
        with pytest.raises(ValueError, match="closed"):
            kb.set_task_epic(conn, t, eid)
        assert kb.get_task(conn, t).epic_id is None
        # … but detaching from a since-closed epic is allowed.
        eid2 = kb.create_epic(conn, title="open then closed")
        kb.set_task_epic(conn, t, eid2)
        kb.close_epic(conn, eid2)
        assert kb.set_task_epic(conn, t, None) is True
        assert kb.get_task(conn, t).epic_id is None


def test_c1_caps_off_is_byte_identical(kanban_home, all_assignees_spawnable):
    """Caps unset (the live default) → no hold even with heavy prior usage."""
    spawns = []
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=10_000_000)
        t = kb.create_task(conn, title="ready", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=lambda task, ws: spawns.append(task.id))
        assert res.budget_held == []
        assert t in spawns
        assert kb.get_task(conn, t).status == "running"


def test_c1_token_cap_holds_only_over_budget_profile(
    kanban_home, all_assignees_spawnable
):
    spawns = []
    with kb.connect_closing() as conn:
        # alice has blown her token budget; bob has not.
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=5000)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        tb = kb.create_task(conn, title="bob task", assignee="bob")
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: spawns.append(task.id),
            daily_token_cap_per_profile=1000,
        )
        held_ids = [x[0] for x in res.budget_held]
        assert ta in held_ids and tb not in held_ids
        assert ta not in spawns and tb in spawns
        assert kb.get_task(conn, ta).status == "ready"  # held, not blocked
        # Exactly one budget_held event.
        n = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'budget_held'",
            (ta,),
        ).fetchone()[0]
        assert n == 1


def test_c1_token_cap_event_deduped_across_ticks(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=5000)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: None, daily_token_cap_per_profile=1000)
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: None, daily_token_cap_per_profile=1000)
        n = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'budget_held'",
            (ta,),
        ).fetchone()[0]
        assert n == 1


def test_c1_cost_cap_holds_board_wide(kanban_home, all_assignees_spawnable):
    spawns = []
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", cost=2.50)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        tb = kb.create_task(conn, title="bob task", assignee="bob")
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: spawns.append(task.id),
            daily_cost_cap_usd=1.0,
        )
        held_ids = {x[0] for x in res.budget_held}
        assert {ta, tb} <= held_ids  # board-wide hold (prior is held too)
        assert spawns == []


def test_c1_null_tokens_count_as_zero(kanban_home, all_assignees_spawnable):
    spawns = []
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        # A run with NULL tokens contributes 0 → under any positive cap.
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'alice', 'done', ?, ?, 'completed')",
            (prior, int(time.time()) - 5, int(time.time()) - 5),
        )
        conn.commit()
        t = kb.create_task(conn, title="ready", assignee="alice")
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: spawns.append(task.id),
            daily_token_cap_per_profile=1000,
        )
        assert res.budget_held == []
        assert t in spawns


def test_c1_budget_held_surfaces_in_decision_queue(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=5000)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: None, daily_token_cap_per_profile=1000)
        result = kb.decision_queue(conn)
    assert "budget_held" in _kinds_for(ta, result)


def test_c1_tree_root_woke_all_children_done(kanban_home):
    """A decompose root that is 'ready' with all children 'done' surfaces
    as tree_root_woke. Reuses the same all-children-done predicate as
    recompute_ready (only 'done' counts; not archived/failed)."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root task", assignee="orchestrator")
        child1 = kb.create_task(conn, title="child A", assignee="coder")
        child2 = kb.create_task(conn, title="child B", assignee="coder")
        # A decompose root DEPENDS ON its subtasks: the root is the child_id and
        # each subtask is a parent_id (the same direction decompose_triage_task
        # creates and recompute_ready reads). Building the links the other way
        # round would make this test pass while the production query never fires.
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child1, root),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child2, root),
            )
        kb._append_event(conn, root, "decomposed", {"child_ids": [child1, child2]})
        # Complete both children
        _set_task_status(conn, child1, "done")
        _set_task_status(conn, child2, "done")
        # Root is now ready (woken up by completion)
        _set_task_status(conn, root, "ready")

        result = kb.decision_queue(conn)

    assert _kinds_for(root, result) == ["tree_root_woke"]
    row = next(d for d in result["decisions"] if d["task_id"] == root)
    assert row["suggested_command"] == f"hermes kanban show {root}"
    assert row["age_seconds"] is not None
    # Same shape as existing kinds
    for key in ("kind", "task_id", "title", "reason", "age_seconds", "suggested_command"):
        assert key in row, f"missing key {key!r} in tree_root_woke entry"


def test_c1_tree_root_woke_not_emitted_if_child_not_done(kanban_home):
    """Root must NOT appear when even one child is not yet done."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root task", assignee="orchestrator")
        child1 = kb.create_task(conn, title="child A", assignee="coder")
        child2 = kb.create_task(conn, title="child B", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child1, root),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child2, root),
            )
        # Only child1 done; child2 still todo
        _set_task_status(conn, child1, "done")
        # child2 stays in 'todo' (default)
        _set_task_status(conn, root, "ready")

        result = kb.decision_queue(conn)

    assert _kinds_for(root, result) == []


def test_c1_tree_root_woke_no_children_excluded(kanban_home):
    """A ready task with NO children must NOT appear as tree_root_woke
    (it was never decomposed)."""
    with kb.connect_closing() as conn:
        leaf = kb.create_task(conn, title="plain ready", assignee="coder")
        _set_task_status(conn, leaf, "ready")
        result = kb.decision_queue(conn)
    assert "tree_root_woke" not in _kinds_for(leaf, result)


def test_c1_release_gate_parked_surfaces_in_decision_queue(kanban_home):
    """A non-terminal task with a release_gate_parked event surfaces in the
    decision queue with a suggested_command from _RELEASE_GATE_COMMANDS."""
    from hermes_cli.kanban_worktrees import _RELEASE_GATE_COMMANDS

    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="release gate task", assignee="verifier")
        # Record the release_gate_parked event (status stays blocked/non-terminal)
        _set_task_status(conn, task, "blocked")
        with kb.write_txn(conn):
            kb._append_event(
                conn, task, "release_gate_parked",
                {
                    "state": "GREEN_CODE_NOT_RUNTIME_ACTIVATED",
                    "reason": "awaiting release-gate GO",
                    "commands": list(_RELEASE_GATE_COMMANDS),
                },
            )

        result = kb.decision_queue(conn)

    assert _kinds_for(task, result) == ["release_gate_parked"]
    row = next(d for d in result["decisions"] if d["task_id"] == task)
    # suggested_command must carry the FULL gate sequence, not just the bare cd
    assert row["suggested_command"]
    for cmd in _RELEASE_GATE_COMMANDS:
        assert cmd in row["suggested_command"]
    assert row["reason"] == "awaiting release-gate GO"
    # Same shape as existing kinds
    for key in ("kind", "task_id", "title", "reason", "age_seconds", "suggested_command"):
        assert key in row, f"missing key {key!r} in release_gate_parked entry"


def test_c1_release_gate_parked_excluded_when_done(kanban_home):
    """A task that carries release_gate_parked but is already done must NOT
    appear in the decision queue."""
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="done gate", assignee="verifier")
        with kb.write_txn(conn):
            kb._append_event(conn, task, "release_gate_parked", {"reason": "GO"})
        _set_task_status(conn, task, "done")

        result = kb.decision_queue(conn)

    assert _kinds_for(task, result) == []


def test_c1_release_gate_suggested_command_carries_full_sequence(kanban_home):
    """#7: the suggested_command for a release_gate_parked decision must carry the
    FULL command sequence from the event payload — not just the first bare ``cd``.

    Regression for the original ``next(iter(_RELEASE_GATE_COMMANDS))`` which
    surfaced only ``cd .../web`` (a no-op alone) instead of the whole gate."""
    commands = [
        "cd /home/piet/.hermes/hermes-agent/web",
        "npm run build",
        "test -f /home/piet/.hermes/hermes-agent/hermes_cli/web_dist/index.html",
        "curl -fsS http://127.0.0.1:9119/control >/dev/null",
    ]
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="gate task", assignee="verifier")
        _set_task_status(conn, task, "blocked")
        with kb.write_txn(conn):
            kb._append_event(
                conn, task, "release_gate_parked",
                {"reason": "awaiting release-gate GO", "commands": commands},
            )

        result = kb.decision_queue(conn)

    row = next(d for d in result["decisions"] if d["task_id"] == task)
    suggested = row["suggested_command"]
    # Every command from the payload must be present, chained — not just the cd.
    for cmd in commands:
        assert cmd in suggested, f"{cmd!r} missing from suggested_command {suggested!r}"
    assert "npm run build" in suggested
    assert suggested != commands[0]  # not the bare leading cd


def test_c1_release_gate_suggested_command_falls_back_without_payload_commands(kanban_home):
    """#7: when the event payload has no ``commands`` list, fall back to the
    canonical _RELEASE_GATE_COMMANDS sequence (still the full gate, not a bare cd)."""
    from hermes_cli.kanban_worktrees import _RELEASE_GATE_COMMANDS

    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="gate task no cmds", assignee="verifier")
        _set_task_status(conn, task, "blocked")
        with kb.write_txn(conn):
            kb._append_event(conn, task, "release_gate_parked", {"reason": "GO"})

        result = kb.decision_queue(conn)

    row = next(d for d in result["decisions"] if d["task_id"] == task)
    suggested = row["suggested_command"]
    assert suggested
    for cmd in _RELEASE_GATE_COMMANDS:
        assert cmd in suggested


def test_c1_release_gate_parked_beats_generic_operator_escalation(kanban_home):
    """Regression (2026-07-07 live find): the no-silent-stall safety net
    (``escalate_silent_blocks_sweep``) auto-emits a GENERIC ``operator_escalation``
    event for every settled blocked task — including a release-gate child, which
    has no task_runs at all and so is treated as settled immediately. On the live
    board this lands ~1min after the gate parks, giving the task BOTH a
    ``release_gate_parked`` event (real payload shape from
    ``_create_parked_release_gate_child``) and a real ``operator_escalation``
    event (real payload shape from ``escalate_silent_blocks_sweep`` itself, not a
    hand-authored fake). Before the fix, decision_queue's seen-set let the
    earlier-running generic operator_escalation _add() claim the row first, so
    the more specific release_gate_parked decision — and the ``release_gate``
    button metadata the frontend renders "Release-Gate ausführen" from — never
    surfaced for that task again."""
    from hermes_cli.kanban_worktrees import _RELEASE_GATE_COMMANDS

    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root task", assignee="orchestrator")
        task = kb.create_task(
            conn,
            title="release gate task",
            assignee="verifier",
            parents=(root,),
            initial_status="blocked",
        )
        merge_commit = "abc123def4560"
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task,
                "release_gate_parked",
                {
                    "state": "GREEN_CODE_NOT_RUNTIME_ACTIVATED",
                    "source_task": root,
                    "root_id": root,
                    "merge_commit": merge_commit,
                    "reason": "awaiting release-gate GO",
                    "commands": list(_RELEASE_GATE_COMMANDS),
                },
            )

        # Real production write path (not a hand-authored event): the silent-
        # block safety net escalates any settled blocked task it finds.
        sweep_summary = kb.escalate_silent_blocks_sweep(conn)
        assert any(e["task_id"] == task for e in sweep_summary["escalated"]), (
            "test setup invalid: the sweep did not escalate the parked task — "
            "the scenario this regression guards against was not reproduced"
        )
        # Confirm the real operator_escalation event actually landed, so the
        # test is provably exercising the precedence race, not a no-op sweep.
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'operator_escalation'",
            (task,),
        ).fetchone() is not None

        result = kb.decision_queue(conn)

    # The specific release_gate_parked decision must win — not the generic
    # operator_escalation the sweep also wrote for the very same task.
    assert _kinds_for(task, result) == ["release_gate_parked"]
    row = next(d for d in result["decisions"] if d["task_id"] == task)
    assert row["kind"] == "release_gate_parked"
    assert "release_gate" in row, "release-gate button metadata missing from row"
    assert row["release_gate"]["root_id"] == root
    assert row["release_gate"]["source_task_id"] == root
    assert row["release_gate"]["merge_commit"] == merge_commit


def test_set_run_verdict_records_binary_score(kanban_home):
    """APPROVED→1.0 / REQUEST_CHANGES→0.0 landen automatisch in scores;
    erneutes Verdict auf demselben Run erzeugt keine zweite Zeile."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="judged work")
        with kb.write_txn(conn):
            r_ok = _insert_bare_run(conn, t, started_at=1000, ended_at=1300)
            r_bad = _insert_bare_run(conn, t, started_at=2000, ended_at=2300)
            kb._set_run_verdict(conn, r_ok, "APPROVED")
            kb._set_run_verdict(conn, r_bad, "REQUEST_CHANGES")
            kb._set_run_verdict(conn, r_ok, "APPROVED")  # idempotent
        rows = conn.execute(
            "SELECT run_id, task_id, name, value, value_type, source "
            "FROM scores ORDER BY run_id",
        ).fetchall()
    assert [(r["run_id"], r["value"]) for r in rows] == [(r_ok, 1.0), (r_bad, 0.0)]
    for r in rows:
        assert r["task_id"] == t
        assert r["name"] == "review_verdict"
        assert r["value_type"] == "binary"
        assert r["source"] == "review_gate"


def test_set_run_verdict_score_fails_soft_without_table(kanban_home):
    """Score-Spiegelung darf einen Abschluss nie brechen (Legacy-DB ohne
    scores-Tabelle): Verdict bleibt gesetzt, kein Raise."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="legacy")
        with kb.write_txn(conn):
            r = _insert_bare_run(conn, t, started_at=1000)
            conn.execute("DROP TABLE scores")
            kb._set_run_verdict(conn, r, "APPROVED")
        row = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?", (r,)
        ).fetchone()
    assert row["verdict"] == "APPROVED"


def test_backfill_verdict_scores_idempotent_with_run_timestamps(kanban_home):
    """Backfill spiegelt historische Verdicts mit Run-Endzeit als created_at,
    überspringt verdictlose Runs und ist wiederholbar (0 beim 2. Lauf)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="history")
        with kb.write_txn(conn):
            r1 = _insert_bare_run(conn, t, started_at=1000, ended_at=1500, verdict="APPROVED")
            r2 = _insert_bare_run(conn, t, started_at=2000, ended_at=None, verdict="REQUEST_CHANGES")
            _insert_bare_run(conn, t, started_at=3000, ended_at=3100)  # kein Verdict
        assert kb.backfill_verdict_scores(conn) == 2
        assert kb.backfill_verdict_scores(conn) == 0  # idempotent
        rows = {
            r["run_id"]: r for r in conn.execute(
                "SELECT run_id, value, created_at FROM scores",
            ).fetchall()
        }
    assert rows[r1]["value"] == 1.0 and rows[r1]["created_at"] == 1500
    # ohne ended_at fällt der Zeitstempel ehrlich auf started_at zurück
    assert rows[r2]["value"] == 0.0 and rows[r2]["created_at"] == 2000


def test_scores_name_created_query_uses_index(kanban_home):
    """Trend-Queries (name + Zeitfenster) laufen über idx_scores_name_created —
    der Index ist die <200ms@10k-Garantie, deterministischer als Timing."""
    with kb.connect_closing() as conn:
        plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT AVG(value) FROM scores "
                "WHERE name = 'review_verdict' AND created_at >= 0",
            )
        )
    assert "idx_scores_name_created" in plan


def test_issue_signature_normalisation_cases():
    """Mind. 5 Fälle: PIDs/Zähler/IDs/Hex maskiert, Whitespace kollabiert,
    erste nicht-leere Zeile zählt, leerer Text wird ehrlich benannt."""
    sig = kb._issue_signature
    # 1+2: gleiche PID-Fehlerklasse → identische Signatur trotz anderer PID
    assert sig("pid 4053999 exited with code 1") == "pid N exited with code N"
    assert sig("pid 12 exited with code 1") == "pid N exited with code N"
    # 3: Iterations-Zähler maskiert
    assert sig("Iteration budget exhausted (60/60) — task could not complete") \
        == "Iteration budget exhausted (N/N) — task could not complete"
    # 4: Task-IDs maskiert
    assert sig("worker for t_82c04f63 vanished") == "worker for t_… vanished"
    # 5: lange Hex-IDs maskiert
    assert sig("session 0123456789abcdef crashed") == "session … crashed"
    # 6: Mehrzeiler → erste nicht-leere Zeile, Whitespace kollabiert
    assert sig("\n\n  Error:   boom   \nTraceback ...") == "Error: boom"
    # 7: leer/None
    assert sig("") == "(kein Fehlertext)"
    assert sig(None) == "(kein Fehlertext)"


def test_runs_issues_groups_by_profile_and_signature(kanban_home):
    """Gleicher Fehlertyp + gleiches Profil = ein Issue mit Zähler; blocked
    fällt auf summary zurück; Beispiel-Run ist das jüngste Auftreten."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="flaky")
        with kb.write_txn(conn):
            def run(profile, outcome, started, error=None, summary=None):
                return conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at, error, summary) VALUES (?,?,?,?,?,?,?,?)",
                    (t, profile, "done", outcome, started, started + 10, error, summary),
                ).lastrowid
            run("coder", "crashed", now - 500, error="pid 111 exited with code 1")
            newest = run("coder", "crashed", now - 100, error="pid 222 exited with code 1")
            run("research", "crashed", now - 300, error="pid 333 exited with code 1")
            run("coder", "blocked", now - 200, error="  ",
                summary="Edit-risk blocked by open overlapping session")
            # außerhalb des Fensters → unsichtbar
            run("coder", "crashed", now - 40 * 86400, error="pid 9 exited with code 1")
        data = kb.runs_issues(conn, days=30)
    assert data["total_failed_runs"] == 4
    assert data["group_count"] == 3
    top = data["issues"][0]
    assert top["profile"] == "coder"
    assert top["signature"] == "pid N exited with code N"
    assert top["count"] == 2
    assert top["outcomes"] == {"crashed": 2}
    assert top["example_run_id"] == newest  # jüngstes Auftreten als Beispiel
    assert top["last_seen"] == now - 100
    # research-PID-Crash ist ein EIGENES Issue (Profil gehört zum Schlüssel)
    profiles = {(i["profile"], i["signature"]) for i in data["issues"]}
    assert ("research", "pid N exited with code N") in profiles
    # blocked ohne error nutzt die summary
    blocked = next(i for i in data["issues"] if i["outcomes"].get("blocked"))
    assert "Edit-risk blocked" in blocked["example_text"]


def test_heartbeat_worker_persists_note_as_event_payload(kanban_home):
    """Die Activity-Note landet als heartbeat-Event-Payload am Run — das ist
    die Quelle für last_heartbeat_note in /workers/active."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="busy")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'coder', 'running', 1000)", (t,),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                (run_id, t),
            )
        assert kb.heartbeat_worker(conn, t, note="Bash: npm test", expected_run_id=run_id)
        row = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' AND run_id = ? "
            "ORDER BY id DESC LIMIT 1", (t, run_id),
        ).fetchone()
    assert row["note"] == "Bash: npm test"


def test_review_wait_attention_emits_once_without_mutating_run(
    kanban_home, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id, run_id = _running_review_for_attention(
            conn, title="silent review", now=base
        )

        assert kb.emit_review_wait_attention(conn, now=base + 179) == []
        assert kb.emit_review_wait_attention(conn, now=base + 180) == [task_id]
        assert kb.emit_review_wait_attention(conn, now=base + 360) == []

        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "review_wait_attention"
        ]
        assert len(events) == 1
        assert events[0].run_id == run_id
        assert events[0].payload["idle_seconds"] == 180
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert run["status"] == "running"
    assert run["outcome"] is None
    assert run["ended_at"] is None


@pytest.mark.parametrize(
    "note",
    [
        "waiting for provider response (streaming)",
        "waiting for non-streaming response (181s elapsed)",
        "claude-cli running · log 0B · last output 181s",
    ],
)
def test_review_wait_process_heartbeats_are_not_productive(
    kanban_home, monkeypatch, note
):
    base = 1_800_000_000
    clock = {"now": base}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    with kb.connect_closing() as conn:
        task_id, run_id = _running_review_for_attention(
            conn, title=f"waiting review {note}", now=base
        )
        clock["now"] = base + 179
        assert kb.heartbeat_worker(
            conn, task_id, note=note, expected_run_id=run_id
        )

        assert kb.emit_review_wait_attention(conn, now=base + 180) == [task_id]


@pytest.mark.parametrize(
    "note",
    [
        "receiving stream response",
        "executing tool: terminal",
        "executing 2 tools concurrently: terminal, browser",
        "tool completed: terminal (3.2s)",
        "terminal command running",
        "execute_code running",
        "modal command running",
    ],
)
def test_review_wait_productive_activity_restarts_idle_clock(
    kanban_home, monkeypatch, note
):
    base = 1_800_000_000
    clock = {"now": base}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    with kb.connect_closing() as conn:
        task_id, run_id = _running_review_for_attention(
            conn, title=f"productive review {note}", now=base
        )
        clock["now"] = base + 170
        assert kb.heartbeat_worker(
            conn, task_id, note=note, expected_run_id=run_id
        )

        assert kb.emit_review_wait_attention(conn, now=base + 180) == []
        assert kb.emit_review_wait_attention(conn, now=base + 349) == []
        assert kb.emit_review_wait_attention(conn, now=base + 350) == [task_id]


def test_review_wait_attention_ignores_non_review_runs(kanban_home, monkeypatch):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ordinary worker", assignee="coder")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        assert task.current_run_id is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (base, task.current_run_id),
            )

        assert kb.emit_review_wait_attention(conn, now=base + 1000) == []
        assert not any(
            event.kind == "review_wait_attention"
            for event in kb.list_events(conn, task_id)
        )


def test_run_duration_percentiles_per_profile_with_min_n(kanban_home):
    """p50/p90 nur aus completed-Runs des Profils; unter min_n ehrlich None."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="timed")
        with kb.write_txn(conn):
            for dur in (100, 200, 300, 400, 1000):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at) VALUES (?, 'coder', 'done', 'completed', ?, ?)",
                    (t, now - 5000, now - 5000 + dur),
                )
            # failed-Run desselben Profils zählt NICHT in die ETA
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at) VALUES (?, 'coder', 'done', 'crashed', ?, ?)",
                (t, now - 5000, now - 5000 + 9999),
            )
            # dünnes Profil: nur 1 completed-Run
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at) VALUES (?, 'research', 'done', 'completed', ?, ?)",
                (t, now - 5000, now - 4900),
            )
        stats = kb.run_duration_percentiles(conn, ["coder", "research", "verifier"])
    assert stats["coder"]["p50"] == 300
    assert stats["coder"]["p90"] == 1000
    assert stats["coder"]["n"] == 5
    assert stats["research"] == {"p50": None, "p90": None, "n": 1}
    assert stats["verifier"] == {"p50": None, "p90": None, "n": 0}


def test_runs_failures_dedupes_per_task_and_filters_recovered(kanban_home):
    """Phase F: jüngster Fehl-Run pro Task; bereits fertige/laufende Tasks
    erscheinen nicht mehr in der Triage."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        t_open = kb.create_task(conn, title="kaputt und wartet")
        t_done = kb.create_task(conn, title="kaputt aber erledigt")
        kb.block_task(conn, t_open, reason="worker crashed")
        with kb.write_txn(conn):
            # beide Crash-Runs enden NACH dem block_task-eigenen blocked-Run,
            # damit "jüngster Run gewinnt" über die pid-Runs läuft
            for started, ended, err in (
                (now - 7200, now + 60, "pid 1 exited with code 1"),
                (now - 3600, now + 120, "pid 2 exited with code 1"),
            ):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at, error) VALUES (?, 'coder', 'done', 'crashed', ?, ?, ?)",
                    (t_open, started, ended, err),
                )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at, error) VALUES (?, 'coder', 'done', 'crashed', ?, ?, 'x')",
                (t_done, now - 1800, now - 1700),
            )
        kb.complete_task(conn, t_done, summary="doch geschafft")
        data = kb.runs_failures(conn, hours=48)
    assert data["count"] == 1  # t_done ist raus (status done), t_open dedupliziert
    f = data["failures"][0]
    assert f["task_id"] == t_open
    assert f["reason"] == "pid 2 exited with code 1"  # jüngster Run gewinnt
    assert f["task_status"] == "blocked"


def test_transient_provisioning_timeout_requeues_without_burning_budget(
    kanban_home, monkeypatch, all_assignees_spawnable, tmp_path
):
    from hermes_cli import kanban_worktrees as kwt
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    def boom(*a, **k):
        raise kwt.WorktreeTimeout("contention")
    monkeypatch.setattr(kwt, "provision_for_task", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", workspace_path=str(repo),
                             max_retries=1)
        kb.dispatch_once(conn, board="default")
        row = conn.execute(
            "SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["status"] == "ready"            # re-queued, not blocked
        assert row["consecutive_failures"] == 0    # budget NOT consumed
        assert _count_spawn_retry_events(conn, tid, "spawn_retry") == 1


def test_spawn_retry_budget_exhaustion_blocks(
    kanban_home, monkeypatch, all_assignees_spawnable, tmp_path
):
    from hermes_cli import kanban_worktrees as kwt
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    monkeypatch.setattr(
        kwt, "provision_for_task",
        lambda *a, **k: (_ for _ in ()).throw(kwt.WorktreeTimeout("x")),
    )
    monkeypatch.setenv("HERMES_SPAWN_RETRY_LIMIT", "2")
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", workspace_path=str(repo),
                             max_retries=1)
        for _ in range(3):
            kb.dispatch_once(conn, board="default")
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"          # spawn budget spent → normal block


def test_permanent_provisioning_error_blocks_immediately(
    kanban_home, monkeypatch, all_assignees_spawnable, tmp_path
):
    from hermes_cli import kanban_worktrees as kwt
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    monkeypatch.setattr(
        kwt, "provision_for_task",
        lambda *a, **k: (_ for _ in ()).throw(kwt.WorktreeError("disk full")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", workspace_path=str(repo),
                             max_retries=1)
        kb.dispatch_once(conn, board="default")
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"          # permanent error: unchanged behavior


"""Kanban DB tests: tokens workspace.

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
    _operator_escalations,
)

def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


def _seed_input_token_run(conn, task_id, *, input_tokens, profile="alice"):
    """Insert a completed task_run stamped with ``input_tokens`` (K5a).

    The run is dated OUTSIDE the respawn-guard success window so the
    pre-existing ``recent_success`` guard does not interfere — the per-task
    token sum is age-independent (it spans ALL runs), so a stale run still
    counts toward the G1 cap while leaving the task otherwise spawnable."""
    end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 300
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, "
        "started_at, ended_at, input_tokens) "
        "VALUES (?, ?, 'done', 'completed', ?, ?, ?)",
        (task_id, profile, end - 300, end, input_tokens),
    )


def _seed_terminal_review_run(
    conn,
    task_id,
    *,
    verdict="REQUEST_CHANGES",
    outcome="blocked",
    metadata=None,
    profile="reviewer",
):
    """Insert a terminal run that was claimed from the review lane."""
    ended_at = int(time.time()) - 10
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, outcome, verdict, summary, metadata, "
            "started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, 'structured review finding', ?, ?, ?)",
            (
                task_id,
                profile,
                "blocked" if outcome == "blocked" else "review",
                outcome,
                verdict,
                json.dumps(metadata) if metadata is not None else None,
                ended_at - 60,
                ended_at,
            ),
        )
        run_id = int(cur.lastrowid)
        kb._append_event(
            conn,
            task_id,
            "claimed",
            {"source_status": "review", "run_id": run_id},
            run_id=run_id,
        )
    return run_id


def test_dispatch_budget_runaway_grants_first_actionable_review_extension(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """An actionable terminal review finding earns one bounded recovery run."""
    spawned_ids = []
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 1},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="review recovery", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        review_run_id = _seed_terminal_review_run(
            conn,
            task_id,
            profile="verifier",
            verdict="NEEDS_REVISION",
            metadata={
                "review_verdict": "NEEDS_REVISION",
                "blocking_findings": ["fix the duplicate history entry"],
                "required_verification": ["add the mounted regression"],
            },
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned_ids.append(task.id),
            per_task_input_token_cap=1_000,
        )

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        comments = kb.list_comments(conn, task_id)

    assert spawned_ids == [task_id]
    assert not result.budget_runaway_parked
    assert task.status == "running"
    assert task.budget_extension_count == 1
    extension = next(e for e in events if e.kind == "budget_review_extension_granted")
    assert extension.payload["extension"] == 1
    assert extension.payload["limit"] == 1
    assert extension.payload["review_run_id"] == review_run_id
    assert any(
        "Budget recovery extension 1/1" in comment.body
        and f"review run {review_run_id}" in comment.body
        for comment in comments
    )


def test_dispatch_budget_runaway_hold_defers_extension_until_claim(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A temporary profile hold cannot spend the one recovery extension."""
    spawned_ids = []
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 1},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="held review recovery", assignee="alice")
        blocker_id = kb.create_task(conn, title="temporary profile hold", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["fix the held path"],
            },
        )
        assert kb.claim_task(conn, blocker_id) is not None

        held = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned_ids.append(task.id),
            max_in_progress_per_profile=1,
            per_task_input_token_cap=1_000,
        )
        assert held.skipped_per_profile_capped == [(task_id, "alice", 1)]
        assert kb.get_task(conn, task_id).budget_extension_count == 0

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL, "
                "claim_expires = NULL WHERE id = ?",
                (blocker_id,),
            )
        released = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned_ids.append(task.id),
            max_in_progress_per_profile=1,
            per_task_input_token_cap=1_000,
        )
        task = kb.get_task(conn, task_id)
        grants = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "budget_review_extension_granted"
        ]

    assert not released.budget_runaway_parked
    assert spawned_ids == [task_id]
    assert task.status == "running"
    assert task.budget_extension_count == 1
    assert len(grants) == 1


def test_dispatch_budget_runaway_dry_run_reports_recovery_without_mutation(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Dry-run simulates the recovery claim and leaves grant state untouched."""
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 1},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="dry-run review recovery", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["fix the dry-run path"],
            },
        )
        before_events = kb.list_events(conn, task_id)
        before_comments = kb.list_comments(conn, task_id)

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            per_task_input_token_cap=1_000,
        )
        task = kb.get_task(conn, task_id)
        after_events = kb.list_events(conn, task_id)
        after_comments = kb.list_comments(conn, task_id)

    assert result.spawned == [(task_id, "alice", "")]
    assert not result.budget_runaway_parked
    assert task.status == "ready"
    assert task.budget_extension_count == 0
    assert after_events == before_events
    assert after_comments == before_comments


def test_concurrent_budget_review_extension_claims_only_consume_once(
    kanban_home, monkeypatch
):
    """The recovery grant shares the task claim transaction's CAS winner."""
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 1},
    )
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="concurrent review recovery", assignee="alice")
        _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["fix the concurrent path"],
            },
        )

    def attempt(index):
        with kb.connect_closing() as conn:
            return kb.claim_task(
                conn,
                task_id,
                claimer=f"test:{index}",
                budget_review_extension=(1_300, 1_000),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        winners = [result for result in executor.map(attempt, range(8)) if result]
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        grants = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "budget_review_extension_granted"
        ]

    assert len(winners) == 1
    assert task.budget_extension_count == 1
    assert len(grants) == 1


def test_dispatch_budget_runaway_does_not_reconsume_same_review_finding(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A repeated ready tick cannot spend another extension on the same review."""
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 2},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="single review", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        review_run_id = _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["fix once"],
            },
        )

        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: None,
            per_task_input_token_cap=1_000,
        )
        # Simulate a claim being released back to ready before any newer review.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                (task_id,),
            )
        second_spawned = []
        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: second_spawned.append(task.id),
            per_task_input_token_cap=1_000,
        )

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    grants = [e for e in events if e.kind == "budget_review_extension_granted"]
    assert second_spawned == []
    assert second.budget_runaway_parked == [(task_id, 1_300)]
    assert task.status == "blocked"
    assert task.budget_extension_count == 1
    assert len(grants) == 1
    assert grants[0].payload["review_run_id"] == review_run_id


def test_dispatch_budget_runaway_parks_when_extension_budget_is_exhausted(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 1},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="spent recovery", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["fix remains"],
            },
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET budget_extension_count = 1 WHERE id = ?",
                (task_id,),
            )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=1_000,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.budget_runaway_parked == [(task_id, 1_300)]
    assert task.status == "blocked"
    # Reconcile the budget-recovery branch with typed system parks: exhausted
    # recovery remains operator-owned, but uses the canonical capacity kind.
    assert task.block_kind == "capacity"
    assert task.budget_extension_count == 1
    assert "budget_review_extension_granted" not in [e.kind for e in events]
    parked = next(e for e in events if e.kind == kb.BUDGET_RUNAWAY_PARKED_EVENT)
    assert parked.payload["stall_class"] == "budget_runaway"


def test_dispatch_budget_runaway_parks_without_actionable_review_finding(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Verification notes alone are not a machine-readable blocking finding."""
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 1},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="no finding", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "required_verification": ["run the focused gate"],
            },
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=1_000,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.budget_runaway_parked == [(task_id, 1_300)]
    assert task.status == "blocked"
    assert task.block_kind == "capacity"
    assert task.budget_extension_count == 0
    assert "budget_review_extension_granted" not in [e.kind for e in events]


def test_dispatch_budget_runaway_parks_when_latest_review_state_is_inconsistent(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A stale rejection cannot override a newer non-blocked review run."""
    monkeypatch.setattr(
        kb,
        "_budget_extension_config",
        lambda: {"enabled": True, "min_progress_delta": 1, "max_extensions": 2},
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="stale review", assignee="alice")
        _seed_input_token_run(conn, task_id, input_tokens=1_300)
        _seed_terminal_review_run(
            conn,
            task_id,
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["old finding"],
            },
        )
        _seed_terminal_review_run(
            conn,
            task_id,
            verdict="REQUEST_CHANGES",
            outcome="completed",
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["inconsistent terminal state"],
            },
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=1_000,
        )
        task = kb.get_task(conn, task_id)

    assert result.budget_runaway_parked == [(task_id, 1_300)]
    assert task.status == "blocked"
    assert task.block_kind == "capacity"
    assert task.budget_extension_count == 0


def test_dispatch_per_task_input_token_guard_parks_over_threshold(
    kanban_home, all_assignees_spawnable
):
    """AC1: when the cumulative input_tokens across all runs exceeds
    ``per_task_input_token_cap`` the task is PARKED (blocked, not re-spawned),
    bucketed in ``budget_runaway_parked``, and gets both a
    ``budget_runaway_parked`` event (with the token sum) and an
    ``operator_escalation`` event."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="runaway", assignee="alice")
        # Two runs that sum over the 1000-token cap (700 + 600 = 1300).
        _seed_input_token_run(conn, t, input_tokens=700)
        _seed_input_token_run(conn, t, input_tokens=600)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=1000
        )

    # Not spawned this tick.
    assert t not in spawned_ids
    # Bucketed with the summed input tokens.
    assert (t, 1300) in res.budget_runaway_parked
    # Hard-parked to blocked (not left advisory-ready).
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "blocked"
        events = kb.list_events(conn, t)
    kinds = [e.kind for e in events]
    assert "budget_runaway_parked" in kinds
    assert "operator_escalation" in kinds
    parked_evt = next(e for e in events if e.kind == "budget_runaway_parked")
    assert parked_evt.payload.get("input_token_sum") == 1300
    assert parked_evt.payload.get("cap") == 1000


def test_dispatch_per_task_input_token_guard_under_threshold_spawns(
    kanban_home, all_assignees_spawnable
):
    """AC2: a task whose cumulative input_tokens stay under the cap is
    untouched — spawned normally, not parked, no runaway event."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="frugal", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=400)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=1000
        )

    assert t in spawned_ids
    assert not res.budget_runaway_parked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "running"
        kinds = [e.kind for e in kb.list_events(conn, t)]
    assert "budget_runaway_parked" not in kinds


def test_dispatch_per_task_input_token_guard_inert_when_cap_none(
    kanban_home, all_assignees_spawnable
):
    """AC3: with the cap unset (None — the dispatch_once default) the guard is
    inert even for a task far over any sane threshold."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="uncapped", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=9_000_000)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)  # no cap kwarg

    assert t in spawned_ids
    assert not res.budget_runaway_parked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "running"


def test_dispatch_per_task_input_token_guard_inert_when_cap_zero(
    kanban_home, all_assignees_spawnable
):
    """AC3: an explicit cap of 0 disables the guard (same as None)."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="zero-cap", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=9_000_000)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=0
        )

    assert t in spawned_ids
    assert not res.budget_runaway_parked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "running"


def test_dispatch_per_task_input_token_guard_surfaces_in_decision_queue(
    kanban_home, all_assignees_spawnable
):
    """A parked runaway uses the operator_escalation path, so it appears in the
    decision_queue (Sprint 2 4B wired operator_escalation → decision_queue)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="runaway-q", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=2_500_000)
        kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: None, per_task_input_token_cap=1_000_000
        )
        dq = kb.decision_queue(conn)

    ids = [item["task_id"] for item in dq.get("decisions", [])]
    assert t in ids
    item = next(i for i in dq["decisions"] if i["task_id"] == t)
    assert item["kind"] == "operator_escalation"
    assert item["operator_escalation"]["evidence"]["input_token_sum"] == 2_500_000


def test_dispatch_per_task_input_token_guard_skips_null_token_runs(
    kanban_home, all_assignees_spawnable
):
    """Runs with NULL input_tokens (no usage data) count as 0 and never trip
    the guard on their own — fail-soft like the C1 budget caps."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="no-usage", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=None)
        _seed_input_token_run(conn, t, input_tokens=None)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=1000
        )

    assert t in spawned_ids
    assert not res.budget_runaway_parked


def test_per_task_input_token_cap_config_default_is_two_million():
    """The config default ships the guard ON at 2_000_000 input tokens."""
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["per_task_input_token_cap"] == 2_000_000


def test_dispatch_nonspawnable_emits_one_diagnostic_event(kanban_home, monkeypatch):
    """A ready task whose assignee is not a runnable profile leaves a single
    ``nonspawnable`` event so the skip is visible on the board timeline,
    instead of the task silently rotting in ``ready`` with no diagnosis.
    Deduped (F2 pattern): a second dispatch tick does not duplicate it."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="visual check", assignee="ui-verifier")
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert kinds.count("nonspawnable") == 1
    evt = next(e for e in events if e.kind == "nonspawnable")
    assert isinstance(evt.payload, dict)
    assert evt.payload.get("assignee") == "ui-verifier"


def test_dispatch_nonspawnable_misassignment_escalates_to_operator(kanban_home, monkeypatch):
    """A ready task whose assignee is neither a profile nor a known terminal
    lane raises ONE operator escalation (decision-inbox + Discord path)
    alongside the diagnostic event — mis-assignments must not rot silently
    (2026-06 finding: assignee ``ui-verifier`` sat in ready with no alarm).

    The dedup must survive the REAL gateway tick, which interleaves
    ``classify_escalations_sweep`` after every dispatch (review finding
    2026-07-02: a latest-event-kind guard alone re-fires once the sweep
    appends its classification, paging Discord every tick). Hence the
    escalation classifies INLINE (one paired ``heiler_classification``)
    and its dedup is durable across arbitrary later events."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="visual check", assignee="ui-verifier")
        for _ in range(3):
            kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
            kb.classify_escalations_sweep(conn)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert kinds.count("operator_escalation") == 1
    assert kinds.count(kb.HEILER_CLASSIFICATION_EVENT) == 1
    assert kinds.count("nonspawnable") == 1
    esc = next(e for e in events if e.kind == "operator_escalation")
    assert esc.payload["evidence"]["trigger_outcome"] == "nonspawnable_assignee"
    assert esc.payload["task"]["assignee"] == "ui-verifier"
    diag = next(e for e in events if e.kind == "nonspawnable")
    assert diag.payload.get("escalated") is True


def test_nonspawnable_escalation_does_not_exempt_later_silent_block(
    kanban_home, monkeypatch
):
    """A ready-stage mis-assignment escalation must NOT count as "this task's
    block was escalated": after the operator fixes the assignee and the task
    later genuinely silent-blocks, the silent-block guard still catches it."""
    from hermes_cli import profiles

    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="misassigned then blocked", assignee="ui-verifier")
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        assert len(_operator_escalations(conn, t)) == 1
        # The ready-stage escalation parks nothing: it must not register as an
        # ACTIVE escalation, or every later unrelated block of this task would
        # be held out of the self-heal lanes forever (resolved by reassign,
        # never by an ``unblocked`` event).
        assert kb._operator_escalation_is_active(conn, t) is False

        # Operator "fixes" the assignee; later the task blocks on a question.
        monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential should I use?")
        assert kb.silent_block_task_ids(conn, now=base) == [t]


def test_dispatch_nonspawnable_terminal_lane_stays_quiet(kanban_home, monkeypatch):
    """Known terminal lanes (pulled via ``claim_task`` by interactive
    terminals) are intentionally non-spawnable: diagnostic event only,
    NO operator escalation — otherwise every orion-cc task would page the
    operator."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="terminal work", assignee="orion-cc")
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "operator_escalation" not in kinds
    assert kinds.count("nonspawnable") == 1
    diag = next(e for e in events if e.kind == "nonspawnable")
    assert diag.payload.get("escalated") is False


def test_terminal_lane_allowlist_env_extends_default(monkeypatch):
    """HERMES_KANBAN_TERMINAL_LANES EXTENDS the built-in default (it must not
    replace it — an operator adding one lane would silently de-list orion-cc)."""
    monkeypatch.setenv("HERMES_KANBAN_TERMINAL_LANES", "my-lane, other ,")
    lanes = kb._terminal_lane_assignees()
    assert {"my-lane", "other"} <= lanes
    assert {"orion-cc", "orion-research"} <= lanes
    monkeypatch.delenv("HERMES_KANBAN_TERMINAL_LANES")
    assert "orion-cc" in kb._terminal_lane_assignees()


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def test_scratch_workspace_created_under_hermes_home(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
    assert ws.exists()
    assert ws.is_dir()
    assert "kanban" in str(ws)


def test_dir_workspace_honors_given_path(kanban_home, tmp_path):
    target = tmp_path / "my-vault"
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="biz", workspace_kind="dir", workspace_path=str(target)
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
    assert ws == target
    assert ws.exists()


def test_worktree_workspace_repo_root_anchor_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="ship", workspace_kind="worktree", workspace_path=str(repo)
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    expected = repo / ".worktrees" / t
    assert ws == expected
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {expected}" in listed
    assert f"branch refs/heads/wt/{t}" in listed


def test_worktree_no_path_anchors_on_board_default_workdir(kanban_home, tmp_path):
    """A worktree task created with no explicit path inherits the board's
    default_workdir as its anchor and materializes a per-task linked worktree
    at ``<repo>/.worktrees/<id>`` — NOT the dispatcher's CWD, and NOT the
    shared default_workdir verbatim (which would collapse every task into one
    directory)."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("wt-default-board", default_workdir=str(repo))
    with kb.connect(board="wt-default-board") as conn:
        t = kb.create_task(
            conn, title="ship", workspace_kind="worktree", board="wt-default-board"
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task, board="wt-default-board")

    expected = repo / ".worktrees" / t
    assert ws == expected
    assert ws.exists()
    assert ws != repo  # not the shared default verbatim


def test_worktree_no_path_no_board_default_raises(kanban_home, tmp_path, monkeypatch):
    """With neither an explicit workspace_path nor a board default_workdir,
    resolution fails loudly pointing at default_workdir / worktree:<path> —
    rather than silently materializing under the dispatcher's CWD (the old
    behavior that scattered worktrees under whatever dir launched the
    gateway)."""
    # Park the dispatcher CWD inside a real git repo so the OLD cwd-anchored
    # code would have "succeeded" — proving the new code does NOT use cwd.
    decoy_repo = tmp_path / "decoy"
    _init_git_repo(decoy_repo)
    monkeypatch.chdir(decoy_repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ship", workspace_kind="worktree")
        task = kb.get_task(conn, t)
        assert task is not None
        with pytest.raises(ValueError, match="default_workdir"):
            kb.resolve_workspace(task)


def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {target}" in listed
    assert f"branch refs/heads/{branch}" in listed


def test_dispatch_worktree_task_persists_materialized_workspace_and_branch(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("worktree-board", default_workdir=str(repo))
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect(board="worktree-board") as conn:
        tid = kb.create_task(
            conn,
            title="ship",
            assignee="sentinel",
            workspace_kind="worktree",
            board="worktree-board",
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-board")
        task = kb.get_task(conn, tid)

    expected = repo / ".worktrees" / tid
    assert result.spawned == [(tid, "sentinel", str(expected))]
    assert spawns == [(tid, str(expected))]
    assert task is not None
    assert task.workspace_path == str(expected)
    assert task.branch_name == f"wt/{tid}"
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {expected}" in listed
    assert f"branch refs/heads/wt/{tid}" in listed


@pytest.mark.parametrize(
    ("workspace_kind", "managed_isolation"),
    (("worktree", False), ("dir", True)),
)
def test_first_dispatch_injects_materialized_branch_into_worker_env(
    kanban_home,
    tmp_path,
    monkeypatch,
    all_assignees_spawnable,
    workspace_kind,
    managed_isolation,
):
    """The claimed Task must see the branch created during materialization."""
    from hermes_cli import kanban_worktrees as kwt

    repo = tmp_path / f"repo-{workspace_kind}"
    _init_git_repo(repo)
    captured_envs = []
    monkeypatch.setattr(
        kwt,
        "isolation_mode",
        lambda: "worktree" if managed_isolation else "off",
    )
    monkeypatch.setattr(
        kb,
        "_persisted_spawn_identity",
        lambda task, board=None: {
            "worker_runtime": "hermes",
            "route_provider": "test-provider",
            "model": "test-model",
            "fallback_providers": [],
        },
    )
    monkeypatch.setattr(
        kb,
        "_launch_worker_process",
        lambda spec: captured_envs.append(dict(spec.env)) or 4242,
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="first dispatch branch",
            assignee="coder",
            workspace_kind=workspace_kind,
            workspace_path=str(repo),
        )
        result = kb.dispatch_once(conn)
        stored = kb.get_task(conn, tid)

    assert result.spawned and result.spawned[0][0] == tid
    assert stored is not None and stored.branch_name
    assert len(captured_envs) == 1
    assert captured_envs[0]["HERMES_KANBAN_BRANCH"] == stored.branch_name


def test_dispatch_worktree_task_rerun_reuses_existing_linked_worktree_and_branch(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("worktree-rerun-board", default_workdir=str(repo))
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect(board="worktree-rerun-board") as conn:
        tid = kb.create_task(
            conn,
            title="ship",
            assignee="sentinel",
            workspace_kind="worktree",
            board="worktree-rerun-board",
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-rerun-board")
        first_task = kb.get_task(conn, tid)
        assert first_task is not None
        expected = repo / ".worktrees" / tid
        assert first_task.workspace_path == str(expected)
        assert first_task.branch_name == f"wt/{tid}"

        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()

        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-rerun-board")
        second_task = kb.get_task(conn, tid)

    assert first.spawned == [(tid, "sentinel", str(expected))]
    assert second.spawned == [(tid, "sentinel", str(expected))]
    assert spawns == [(tid, str(expected)), (tid, str(expected))]
    assert second_task is not None
    assert second_task.workspace_path == str(expected)
    actual_branch = subprocess.run(
        ["git", "-C", str(expected), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert actual_branch == f"wt/{tid}"
    assert second_task.branch_name == actual_branch
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count(f"worktree {expected}\n") == 1
    assert f"worktree {expected}/.worktrees/{tid}" not in listed
    assert f"branch refs/heads/{actual_branch}" in listed


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------

def test_cleanup_workspace_removes_managed_scratch_dir(kanban_home):
    """A scratch workspace under the kanban workspaces root is removed."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="scratchy")
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        assert ws.is_dir()
        kb.complete_task(conn, t, result="ok")
    assert not ws.exists(), "Hermes-managed scratch dir should be cleaned up"


def test_complete_task_persists_scratch_artifacts_before_cleanup(kanban_home):
    """Completion artifacts from scratch workspaces survive workspace cleanup."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render chart")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert persisted.exists(), "artifact copy should survive scratch cleanup"
    assert persisted.parent == kb.task_attachments_dir(t)
    assert persisted.name == "chart.png"
    assert persisted.read_bytes() == b"png-bytes"
    assert str(persisted) != str(artifact)
    assert run is not None
    assert run.metadata["artifacts"] == [str(persisted)]
    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, t)
    assert [(a.filename, a.stored_path) for a in attachments] == [
        ("chart.png", str(persisted.resolve()))
    ]


def test_complete_task_rejects_missing_declared_scratch_artifact(kanban_home):
    """A declared scratch deliverable must not disappear behind a false Done."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="missing report")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        missing = ws / "report.md"

        with pytest.raises(kb.ArtifactPreservationError, match="unavailable"):
            kb.complete_task(
                conn,
                t,
                result="report complete",
                metadata={"artifacts": [str(missing)]},
            )

        assert kb.get_task(conn, t).status == "ready"
        assert kb.list_attachments(conn, t) == []
    assert ws.exists(), "failed completion must keep scratch available for retry"


def test_complete_task_preserves_legacy_artifact_path_from_summary(kanban_home):
    """Summary-only workers keep the file they tell the user was delivered."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="legacy report")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        report = ws / "report.md"
        report.write_text("legacy deliverable", encoding="utf-8")

        assert kb.complete_task(
            conn,
            t,
            summary=f"Task complete — delivered {report}",
        )
        run = kb.latest_run(conn, t)

    persisted = Path(run.metadata["artifacts"][0])
    assert not ws.exists()
    assert persisted.read_text(encoding="utf-8") == "legacy deliverable"
    assert persisted.parent == kb.task_attachments_dir(t)


def test_complete_task_leaves_non_scratch_artifact_paths_unchanged(
    kanban_home,
    tmp_path,
):
    """Only artifacts inside the managed scratch workspace are copied."""
    external = tmp_path / "report.md"
    external.write_text("keep me here", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(conn, title="external report")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(external)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert external.exists()
    assert completed.payload["artifacts"] == [str(external)]
    assert run is not None
    assert run.metadata["artifacts"] == [str(external)]


def test_complete_task_persists_duplicate_scratch_artifact_names(kanban_home):
    """Scratch artifact persistence does not overwrite duplicate basenames."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render reports")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        first = ws / "a" / "report.txt"
        second = ws / "b" / "report.txt"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(first), str(second)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = [Path(p) for p in completed.payload["artifacts"]]

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert [p.name for p in persisted] == ["report.txt", "report_1.txt"]
    assert [p.read_text(encoding="utf-8") for p in persisted] == ["first", "second"]
    assert all(p.parent == kb.task_attachments_dir(t) for p in persisted)
    reports = kb.kanban_home() / "reports" / "by-task" / t
    assert sorted(p.name for p in reports.iterdir()) == ["report.txt", "report_1.txt"]


def test_complete_task_rejects_oversized_scratch_artifact_directory(
    kanban_home, monkeypatch
):
    """Recursive directory preservation is bounded by a cumulative byte cap."""
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="directory artifact")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, tid, workspace)
        artifact_dir = workspace / "large-tree"
        artifact_dir.mkdir()
        (artifact_dir / "report.txt").write_text("report", encoding="utf-8")

        with pytest.raises(kb.ArtifactPreservationError, match="byte limit"):
            kb.complete_task(
                conn,
                tid,
                result="ok",
                metadata={"artifacts": [str(artifact_dir)]},
            )
        assert kb.get_task(conn, tid).status == "ready"
    assert workspace.exists()
    reports = kb.kanban_home() / "reports" / "by-task" / tid
    assert not reports.exists() or list(reports.iterdir()) == []


def test_complete_task_rejects_over_entry_limit_artifact_directory(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb, "KANBAN_ARTIFACT_TREE_MAX_ENTRIES", 1)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="too many artifact entries")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, tid, workspace)
        artifact_dir = workspace / "tree"
        artifact_dir.mkdir()
        (artifact_dir / "one.txt").write_text("1", encoding="utf-8")
        (artifact_dir / "two.txt").write_text("2", encoding="utf-8")

        with pytest.raises(kb.ArtifactPreservationError, match="entry limit"):
            kb.complete_task(
                conn,
                tid,
                result="ok",
                metadata={"artifacts": [str(artifact_dir)]},
            )
        assert kb.get_task(conn, tid).status == "ready"
    reports = kb.kanban_home() / "reports" / "by-task" / tid
    assert not reports.exists() or list(reports.iterdir()) == []


def test_complete_task_rejects_root_and_nested_artifact_symlinks(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="symlink artifact")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, tid, workspace)
        target_dir = workspace / "target"
        target_dir.mkdir()
        target_file = target_dir / "report.txt"
        target_file.write_text("report", encoding="utf-8")
        root_link = workspace / "root-link"
        nested_dir = workspace / "nested"
        nested_dir.mkdir()
        nested_link = nested_dir / "report-link"
        try:
            root_link.symlink_to(target_dir, target_is_directory=True)
            nested_link.symlink_to(target_file)
        except OSError:
            pytest.skip("filesystem does not support symlinks")

        with pytest.raises(kb.ArtifactPreservationError, match="must not be a symlink"):
            kb.complete_task(
                conn,
                tid,
                result="ok",
                metadata={"artifacts": [str(root_link)]},
            )
        with pytest.raises(kb.ArtifactPreservationError, match="contains a symlink"):
            kb.complete_task(
                conn,
                tid,
                result="ok",
                metadata={"artifacts": [str(nested_dir)]},
            )
        assert kb.get_task(conn, tid).status == "ready"


def test_complete_task_persists_board_scratch_artifacts_to_board_attachments(kanban_home):
    """Board scratch artifacts are copied under that board's attachment root."""
    kb.create_board("work-proj")

    with kb.connect(board="work-proj") as conn:
        t = kb.create_task(conn, title="board chart", board="work-proj")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task, board="work-proj")
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"board-png")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])

    assert not ws.exists(), "board scratch workspace should still be cleaned up"
    assert persisted.exists()
    assert persisted.parent == kb.task_attachments_dir(t, board="work-proj")


def test_cleanup_workspace_refuses_path_outside_scratch_root(kanban_home, tmp_path):
    """A scratch task with a user path outside the workspaces root must NOT be deleted (#28818).

    Reproduces the data-loss vector where a board's ``default_workdir`` is set
    to a real source directory; tasks created without an explicit
    ``workspace_kind`` inherit ``scratch`` semantics, and the old cleanup path
    would ``shutil.rmtree`` the user's source tree on task completion.
    """
    real_source = tmp_path / "real-source"
    real_source.mkdir()
    (real_source / ".git").mkdir()
    (real_source / "README.md").write_text("important", encoding="utf-8")

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ship")
        # Simulate the bad state directly: workspace_kind='scratch' (default)
        # but workspace_path pointing at the user's real source tree, which is
        # exactly what board.default_workdir produces when the task is created
        # without an explicit workspace_kind.
        conn.execute(
            "UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=?",
            ("scratch", str(real_source), t),
        )
        conn.commit()
        kb.complete_task(conn, t, result="ok")

    assert real_source.exists(), "User source tree must not be deleted by scratch cleanup"
    assert (real_source / ".git").exists()
    assert (real_source / "README.md").read_text(encoding="utf-8") == "important"


def test_cleanup_workspace_honors_workspaces_root_env_override(tmp_path, monkeypatch):
    """``HERMES_KANBAN_WORKSPACES_ROOT`` extends the managed-scratch set.

    Worker subprocesses run with this env var injected by the dispatcher. The
    cleanup containment check must treat paths under it as managed even when
    they sit outside the active kanban home.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspaces_override = tmp_path / "ext-workspaces"
    workspaces_override.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(workspaces_override))
    kb.init_db()

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ext")
        scratch_dir = workspaces_override / t
        scratch_dir.mkdir()
        conn.execute(
            "UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=?",
            ("scratch", str(scratch_dir), t),
        )
        conn.commit()
        kb.complete_task(conn, t, result="ok")

    assert not scratch_dir.exists(), "Override-root scratch dir should be cleaned up"


# ---------------------------------------------------------------------------
# Deferred scratch cleanup for parent/child handoff (#33774)
# ---------------------------------------------------------------------------

def test_cleanup_workspace_deferred_while_child_active(kanban_home):
    """A scratch parent's workspace survives completion while a child is still active.

    The dependency chain (parents=[A]) must guarantee child B can read A's
    handoff artifacts. The old cleanup deleted A's scratch dir immediately on
    A's completion, before B ever ran.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)  # child depends on parent
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)
        assert parent_ws.is_dir()
        # Parent completes; child is still 'todo' -> cleanup must be deferred.
        kb.complete_task(conn, parent, result="handoff written")

    assert parent_ws.exists(), (
        "Parent scratch workspace must survive while a linked child is active"
    )


def test_cleanup_workspace_swept_after_last_child_completes(kanban_home):
    """Once all children are terminal, the deferred parent scratch dir is removed."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)
        # Give the child its own scratch dir too.
        c_task = kb.get_task(conn, child)
        child_ws = kb.resolve_workspace(c_task)
        kb.set_workspace_path(conn, child, child_ws)

        kb.complete_task(conn, parent, result="ok")
        assert parent_ws.exists(), "deferred while child active"

        # Child completes -> recompute promotes nothing new; the child's
        # cleanup sweep should now reap the parent's deferred workspace.
        kb.complete_task(conn, child, result="done")

    assert not parent_ws.exists(), (
        "Parent scratch workspace should be swept once all children are terminal"
    )
    assert not child_ws.exists(), "Child scratch workspace should be cleaned up too"


def test_dir_child_completion_unblocks_deferred_scratch_parent(kanban_home, tmp_path):
    """A non-scratch ('dir') child completing must still sweep its scratch parent.

    Regression for the gap where ``_cleanup_workspace`` returned early for a
    non-scratch task and never ran the parent sweep — leaking the parent's
    deferred scratch dir forever.
    """
    child_dir = tmp_path / "persistent-child"
    child_dir.mkdir()
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)

        kb.complete_task(conn, parent, result="handoff")
        assert parent_ws.exists(), "deferred while dir child active"

        kb.complete_task(conn, child, result="built")

    assert not parent_ws.exists(), (
        "A 'dir' child completing must trigger the parent scratch sweep"
    )
    assert child_dir.exists(), "Non-scratch 'dir' child workspace is never deleted"


def test_is_managed_scratch_path_accepts_per_board_workspaces(kanban_home, tmp_path):
    """Per-board scratch dirs under ``<kanban_home>/kanban/boards/<slug>/workspaces`` are managed."""
    board_scratch = kanban_home / "kanban" / "boards" / "my-board" / "workspaces" / "task-1"
    board_scratch.mkdir(parents=True)
    assert kb._is_managed_scratch_path(board_scratch)


def test_is_managed_scratch_path_rejects_real_source_tree(kanban_home, tmp_path):
    """A path outside any managed root (e.g. a user's repo) is NOT managed."""
    real = tmp_path / "code" / "my-project"
    real.mkdir(parents=True)
    assert not kb._is_managed_scratch_path(real)


def test_is_managed_scratch_path_rejects_kanban_metadata_subtrees(kanban_home):
    """Hermes' own DB/metadata/log subtrees under ``<kanban_home>/kanban`` are NOT managed.

    Regression guard for the Copilot finding on #28819: a scratch task whose
    ``workspace_path`` was mis-set to the kanban home, the logs dir, or a
    board's metadata dir (i.e. the board root itself, not its ``workspaces/``
    child) must be refused. Without this, the containment check would happily
    ``shutil.rmtree`` Hermes' DB/metadata/logs on task completion.
    """
    kanban_root = kanban_home / "kanban"
    kanban_root.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kb._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kb._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kb._is_managed_scratch_path(task_dir)


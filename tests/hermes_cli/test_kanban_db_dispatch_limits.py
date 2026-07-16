"""Kanban DB tests: dispatch limits.

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
    _set_task_status,
)

def test_dispatch_max_in_progress_skips_when_at_limit(kanban_home, all_assignees_spawnable):
    """When max_in_progress=N and N tasks are already running, spawn nothing."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        # Two running tasks.
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        kb.claim_task(conn, t1)
        kb.claim_task(conn, t2)
        # Two more ready to spawn — but cap is 2 so none should fire.
        kb.create_task(conn, title="c", assignee="bob")
        kb.create_task(conn, title="d", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=2)

    assert len(spawns) == 0, f"expected 0 spawns, got {len(spawns)}"


def test_dispatch_max_in_progress_spawns_up_to_cap(kanban_home, all_assignees_spawnable):
    """When max_in_progress=3 and only 1 is running, spawn up to 2 more."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        # One running task.
        t1 = kb.create_task(conn, title="a", assignee="alice")
        kb.claim_task(conn, t1)
        # Three ready tasks — only the first 2 should be spawned.
        kb.create_task(conn, title="b", assignee="bob")
        kb.create_task(conn, title="c", assignee="bob")
        kb.create_task(conn, title="d", assignee="bob")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=3)

    assert len(spawns) == 2, f"expected 2 spawns (cap 3 - 1 running), got {len(spawns)}"


def test_dispatch_max_in_progress_none_is_unlimited(kanban_home, all_assignees_spawnable):
    """Default None means no limit — all ready tasks are spawned."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        for title in ["a", "b", "c", "d"]:
            kb.create_task(conn, title=title, assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=None)

    assert len(spawns) == 4, f"expected 4 spawns (unlimited), got {len(spawns)}"


def test_claim_review_task_transitions_to_running(kanban_home):
    """claim_review_task atomically transitions review -> running."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.claim_lock is not None


@pytest.mark.parametrize(
    ("provider", "model", "expected_billing_mode", "expected_subscription", "expected_cost_source"),
    [
        ("openrouter", "openai/gpt-5-mini", "metered", None, "dispatch_metered_stamp"),
        ("openai-codex", "gpt-5.5", "subscription_included", "chatgpt", "dispatch_subscription_stamp"),
    ],
)
def test_claim_review_task_stamps_billing_identity_from_reviewer_lane(
    kanban_home,
    monkeypatch,
    provider,
    model,
    expected_billing_mode,
    expected_subscription,
    expected_cost_source,
):
    """review -> running verifier runs must be self-describing too."""
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)
    with kb.connect_closing() as conn:
        lane = kb.create_lane(
            conn,
            name=f"review-{expected_billing_mode}",
            profiles={"verifier": {
                "worker_runtime": "hermes",
                "provider": provider,
                "model": model,
            }},
        )
        kb.activate_lane(conn, lane["id"])
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")

        claimed = kb.claim_review_task(conn, t, reviewer_profile="verifier")
        assert claimed is not None
        row = conn.execute(
            "SELECT profile, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()

    assert row["profile"] == "verifier"
    meta = json.loads(row["metadata"])
    assert meta["worker_runtime"] == "hermes"
    assert meta["provider"] == provider
    assert meta["model"] == model
    assert meta["billing_mode"] == expected_billing_mode
    assert meta["cost_source"] == expected_cost_source
    if expected_subscription is None:
        assert "subscription" not in meta
    else:
        assert meta["subscription"] == expected_subscription


def test_review_claimed_full_context_retry_uses_retry_profile_caps(kanban_home):
    """review -> running verifier continuations use retry caps with profile='full'."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review continuation", assignee="coder")
        _set_task_status(conn, t, "review")
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["prior_attempts"] + 2):
            _seed_completed_run(conn, t, "verifier", 1_800_000_000 + idx, f"review-summary-{idx}")
        conn.commit()

        claimed = kb.claim_review_task(conn, t, reviewer_profile="verifier")
        ctx = kb.build_worker_context(conn, t, profile="full")

    assert claimed is not None
    assert claimed.assignee == "coder"
    assert "This is continuation run 1/" in ctx
    assert f"showing most recent {kb._CTX_CAP_PROFILES['retry']['prior_attempts']}" in ctx
    assert "review-summary-0" not in ctx
    assert "review-summary-2" in ctx


def test_claim_review_task_clears_inherited_heartbeat(kanban_home):
    """review -> running must reset last_heartbeat_at.

    Regression: a stage whose worker does not self-heartbeat (the claude-CLI
    verifier/reviewer runs) otherwise inherits the previous (coder) stage's
    last beat. That stale timestamp ages past the dashboard's stuck threshold
    and shows an actively-running review as "Hängt". A fresh run must start
    with a NULL heartbeat (liveness via claim_expires, like any other
    non-self-beating worker)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        # Previous stage's lingering heartbeat.
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (1_000_000, t),
        )
        conn.commit()
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None and claimed.status == "running"
        hb = conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()[0]
    assert hb is None


def test_claim_task_clears_inherited_heartbeat(kanban_home):
    """ready -> running starts the run with a clean heartbeat slate."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="claim me", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (1_000_000, t),
        )
        conn.commit()
        claimed = kb.claim_task(conn, t)
        assert claimed is not None and claimed.status == "running"
        hb = conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()[0]
    assert hb is None


def test_claim_review_task_fails_on_non_review(kanban_home):
    """claim_review_task returns None if task is not in review status."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # Task is in 'ready', not 'review'
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None


def test_claim_review_task_fails_when_already_claimed(kanban_home):
    """claim_review_task returns None if the task was already claimed."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        first = kb.claim_review_task(conn, t)
        assert first is not None
        second = kb.claim_review_task(conn, t)
    assert second is None


def test_dispatch_review_dry_run(kanban_home, all_assignees_spawnable):
    """dispatch_once dry-run sees review tasks and reports them as spawned."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t
    # Dry run must NOT mutate status.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "review"


def test_dispatch_review_spawns_as_verifier_profile(
    kanban_home, all_assignees_spawnable,
):
    """Review tasks spawn as the independent ``verifier`` profile — not the
    task's own (code-writing) assignee — and without forcing the historical
    ``sdlc-review`` skill (which does not exist in this tree). The DB
    ``assignee`` is left unchanged so a REQUEST_CHANGES keeps the task owned
    by the original coder for the follow-up fix."""
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        # DB assignee is unchanged (override is in-memory, for the spawn only).
        assert kb.get_task(conn, t).assignee == "alice"
        run = kb.list_runs(conn, t)[0]
        assert run.profile == "verifier"
    assert len(res.spawned) == 1
    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].assignee == "verifier"
    assert spawned_tasks[0].skills == []


def test_dispatch_review_never_falls_back_to_coder_when_verifier_missing(
    kanban_home, monkeypatch,
):
    """A missing verifier is retryable review infrastructure, not self-review."""
    from hermes_cli import profiles
    # The task's assignee resolves, but 'verifier' does not.
    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name != "verifier"
    )
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert kb.get_task(conn, t).status == "review"
        unavailable = [e for e in kb.list_events(conn, t) if e.kind == "review_unavailable"]
    assert spawned_tasks == []
    assert t in res.skipped_nonspawnable
    assert len(unavailable) == 1
    assert unavailable[0].payload["target_profile"] == "verifier"
    assert unavailable[0].payload["retryable"] is True


def test_review_unavailable_auto_spawns_when_verifier_returns(
    kanban_home, monkeypatch,
):
    """Every dispatcher tick re-resolves the frozen stage without operator input."""
    from hermes_cli import profiles

    available = False
    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: available and name == "verifier",
    )
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")

        first = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert t in first.skipped_nonspawnable
        assert spawned_tasks == []
        assert kb.get_task(conn, t).status == "review"

        available = True
        second = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert t not in second.skipped_nonspawnable
        assert kb.get_task(conn, t).status == "running"

    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].assignee == "verifier"


def test_dispatch_review_skips_unassigned(kanban_home):
    """Unassigned review tasks go to skipped_unassigned, not spawned."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review floater")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert not res.spawned


def test_dispatch_review_counts_toward_max_spawn(
    kanban_home, all_assignees_spawnable,
):
    """Review spawns count against max_spawn alongside ready tasks."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect_closing() as conn:
        # Create 2 ready tasks + 1 review task, max_spawn=2
        t1 = kb.create_task(conn, title="ready 1", assignee="alice")
        t2 = kb.create_task(conn, title="ready 2", assignee="bob")
        t3 = kb.create_task(conn, title="review", assignee="alice")
        _set_task_status(conn, t3, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)
    # Only 2 should spawn (ready tasks get priority in the loop)
    assert len(res.spawned) == 2
    assert len(spawns) == 2


def test_dispatch_review_spawns_when_ready_empty(
    kanban_home, all_assignees_spawnable,
):
    """When only review tasks exist, they still get dispatched."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    assert len(res.spawned) == 1
    assert spawns[0] == t


def test_has_spawnable_review_true(kanban_home, monkeypatch):
    """Spawnability follows the independent stage target, not the assignee."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="default")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is True


def test_has_spawnable_review_false_on_empty(kanban_home):
    """has_spawnable_review returns False when no review tasks exist."""
    with kb.connect_closing() as conn:
        assert kb.has_spawnable_review(conn) is False


def test_has_spawnable_review_false_when_only_terminal_lanes(
    kanban_home, monkeypatch,
):
    """has_spawnable_review returns False when review tasks are terminal lanes."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is False


def test_dispatch_review_skips_nonspawnable(kanban_home, monkeypatch):
    """Review tasks with non-existent profiles go to skipped_nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert not res.spawned


def test_dispatch_review_spawns_stage_profile_when_assignee_profile_missing(
    kanban_home, monkeypatch,
):
    """B cross-family-review fix: spawnability keys off the CURRENT stage target
    (verifier→reviewer→critic), not the original coder assignee. A review task
    whose coder-lane profile is gone is still spawnable via its stage profile —
    not stranded as nonspawnable."""
    from hermes_cli import profiles
    # only the verifier stage profile exists; the coder assignee profile is gone
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="removed-lane")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t not in res.skipped_nonspawnable
    assert any(tid == t and prof == "verifier" for (tid, prof, _) in res.spawned)


def test_has_spawnable_review_true_via_stage_profile(kanban_home, monkeypatch):
    """has_spawnable_review agrees with dispatch: a stage target that exists
    makes the review task spawnable even when the assignee profile is gone."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="removed-lane")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is True


def test_blank_assignee_review_spawnability_is_consistent(kanban_home, monkeypatch):
    """A blank-assignee review task is bucketed skipped_unassigned by dispatch,
    so the stage-aware spawnability helper must NOT report it spawnable — else
    health/sweep disagree with dispatch and it sits unspawned-and-unparked."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="coder")
        _set_task_status(conn, t, "review")
        conn.execute("UPDATE tasks SET assignee = '' WHERE id = ?", (t,))
        res = kb.dispatch_once(conn, dry_run=True)
        assert t in res.skipped_unassigned
        assert t not in res.skipped_nonspawnable
        assert not any(tid == t for (tid, _p, _w) in res.spawned)
        # health must agree with dispatch (not spawnable here)
        assert kb.has_spawnable_review(conn) is False
        # and the helper itself returns None for a blank assignee
        assert kb._review_spawn_profile_for(conn, t, "", kb._review_gate_config()) is None


def test_review_status_in_valid_statuses():
    """'review' is a valid task status."""
    assert "review" in kb.VALID_STATUSES


def test_dispatch_review_does_not_claim_ready_tasks(
    kanban_home, all_assignees_spawnable,
):
    """Review dispatch uses claim_review_task, which only claims review tasks."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # claim_review_task should NOT claim a ready task
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None


# Stale detection — detect_stale_running
# ---------------------------------------------------------------------------

def test_detect_stale_returns_running_task_with_no_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout with zero heartbeats gets reclaimed as stale."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale-no-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        # Rewind started_at so the task appears to have been running for 5 hours.
        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
        # No heartbeat set — last_heartbeat_at stays NULL.

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        killed = []
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: killed.append(s),
        )
        assert t in stale, "Task with no heartbeat for >4h should be reclaimed"
        task = kb.get_task(conn, t)
        assert task.status == "ready"


def test_detect_stale_returns_task_with_stale_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout with a heartbeat older than 1h gets reclaimed."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        heartbeat_2h_ago = int(time.time()) - (2 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (five_hours_ago, heartbeat_2h_ago, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert t in stale, (
            "Task with heartbeat >1h old and started >4h ago should be stale"
        )
        assert kb.get_task(conn, t).status == "ready"


def test_detect_stale_skips_task_with_recent_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout but with a recent heartbeat is NOT reclaimed."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="alive-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        heartbeat_now = int(time.time())  # heartbeat just happened
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (five_hours_ago, heartbeat_now, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Task with recent heartbeat should not be reclaimed"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_recently_started_task(kanban_home, monkeypatch):
    """A task started < timeout ago is NOT reclaimed even with no heartbeat."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="fresh", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        # Started only 1 hour ago — well within the 4h threshold.
        one_hour_ago = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (one_hour_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (one_hour_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Task started <4h ago should not be reclaimed"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_when_timeout_zero(kanban_home, monkeypatch):
    """stale_timeout_seconds=0 disables stale detection entirely."""

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="disabled", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=0, signal_fn=lambda p, s: None,
        )
        assert stale == [], "timeout=0 should disable stale detection"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_blocked_tasks(kanban_home, monkeypatch):
    """Blocked tasks are NOT reclaimed by stale detection."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked-task", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
        # Block the task explicitly.
        kb.block_task(conn, t, reason="human requested block")

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Blocked task should not be reclaimed by stale detection"
        assert kb.get_task(conn, t).status == "blocked"


def test_detect_stale_does_not_tick_failure_counter(kanban_home, monkeypatch):
    """Stale reclaim must NOT tick consecutive_failures.

    Stale detection is dispatcher-side absence-of-heartbeat detection,
    not a worker failure. Counting it as a failure would let two
    legitimately-long-running tasks (>4h without explicit heartbeat) trip
    the circuit breaker and auto-block at the default failure_limit=2,
    even though no worker actually failed. The 'stale' event in
    task_events is the right audit surface; the consecutive_failures
    counter is reserved for spawn_failed / timed_out / crashed.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale-no-counter-tick", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
            # Counter starts at 0; assert that's our baseline.
            row = conn.execute(
                "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,)
            ).fetchone()
            assert row["consecutive_failures"] in (0, None)

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert t in stale, "Task should be reclaimed by stale detection"

        # Critical assertion: the failure counter MUST NOT have ticked.
        # Stale reclaim resets to ready for re-dispatch without penalty.
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["consecutive_failures"] in (0, None), (
            f"Stale reclaim ticked consecutive_failures to "
            f"{row['consecutive_failures']!r}; should remain 0/NULL."
        )

        # And the audit trail still records the stale event so operators
        # can see what happened.
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "stale" in kinds, (
            f"Expected 'stale' event in task_events; got {kinds!r}"
        )


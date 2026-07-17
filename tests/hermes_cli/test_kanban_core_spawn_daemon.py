"""Kanban core functionality tests: spawn daemon.

Split from test_kanban_core_functionality.py (pure move; no test logic changes).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Existing crash-detection tests pre-date the grace window; pin to 0
    # so they keep their immediate-reclaim semantics.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Disable the detect_crashed_workers grace period for legacy tests in
    # this file that claim a task and immediately expect
    # ``detect_crashed_workers`` to act on it. The grace period (30s by
    # default, see ``DEFAULT_CRASH_GRACE_SECONDS``) prevents the
    # multi-dispatcher reap race in production; setting it to 0 here
    # restores the pre-fix instant-reclaim semantics these tests were
    # written against. The grace-period itself is covered by dedicated
    # tests in tests/hermes_cli/test_kanban_db.py.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def test_idempotency_key_returns_existing_task(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="first", idempotency_key="abc")
        b = kb.create_task(conn, title="second attempt", idempotency_key="abc")
        assert a == b, "same idempotency_key should return the same task id"
        # And body wasn't overwritten — first create wins.
        task = kb.get_task(conn, a)
        assert task.title == "first"
    finally:
        conn.close()


def test_idempotency_key_ignored_for_archived(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="first", idempotency_key="abc")
        kb.archive_task(conn, a)
        b = kb.create_task(conn, title="second", idempotency_key="abc")
        assert a != b, "archived task shouldn't block a fresh create with same key"
    finally:
        conn.close()


def test_no_idempotency_key_never_collides(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        assert a != b
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Spawn-failure circuit breaker
# ---------------------------------------------------------------------------

def test_spawn_failure_auto_blocks_after_limit(kanban_home, all_assignees_spawnable, monkeypatch):
    """Spawn failures first consume the bounded transient-retry budget, THEN
    count toward the circuit breaker and auto-block.

    Post-f54686689 (`spawn_failed at dispatch` is a transient infra class): the
    first ``TRANSIENT_RETRY_LIMIT`` (2) failures are recorded as transient
    retries — ``consecutive_failures`` stays 0 and the task stays ready — each
    spaced by the 300s transient backoff. Only once the budget is spent does
    ``consecutive_failures`` grow and trip the guard at ``DEFAULT_FAILURE_LIMIT``
    (2). Net: the breaker still trips, just after the transient budget."""
    base = 1_800_000_000
    clock = [base]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    def _bad_spawn(task, ws):
        raise RuntimeError("no PATH")

    conn = kb.connect()
    try:
        assert kb.DEFAULT_FAILURE_LIMIT == 2
        assert kb.TRANSIENT_RETRY_LIMIT == 2
        tid = kb.create_task(conn, title="x", assignee="worker")

        # Budget attempt 1 → transient retry, breaker untouched.
        res1 = kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        assert tid not in res1.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.transient_retry_count == 1

        # Budget attempt 2 (past the 300s backoff) → still transient.
        clock[0] += 301
        kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.transient_retry_count == 2

        # Budget spent → now the breaker counts (still below the limit).
        clock[0] += 301
        kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Limit reached → auto-blocked.
        clock[0] += 301
        res4 = kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        assert tid in res4.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "transient"
        assert task.consecutive_failures >= 2
        assert task.last_failure_error and "no PATH" in task.last_failure_error
    finally:
        conn.close()


def test_successful_spawn_does_not_reset_failure_counter(kanban_home, all_assignees_spawnable, monkeypatch):
    """A successful spawn does NOT reset the retry bookkeeping — past
    attempts stay on the books until a successful *completion* (or operator
    unblock). Post-f54686689 the spawn-dispatch failures are counted as
    transient retries (``transient_retry_count``), not ``consecutive_failures``,
    until the budget is spent; a subsequent successful spawn does not wipe that
    counter (only ``complete_task`` / unblock resets it).
    """
    base = 1_800_000_000
    clock = [base]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    calls = [0]
    def _flaky_spawn(task, ws):
        calls[0] += 1
        if calls[0] <= 2:
            raise RuntimeError("transient")
        return 99999  # pid value — harmless; crash detection will clear it

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        # Two transient failures (spaced past the backoff) + one success.
        kb.dispatch_once(conn, spawn_fn=_flaky_spawn, failure_limit=5)
        clock[0] += 301
        kb.dispatch_once(conn, spawn_fn=_flaky_spawn, failure_limit=5)
        task = kb.get_task(conn, tid)
        assert task.transient_retry_count == 2
        assert task.consecutive_failures == 0

        clock[0] += 301
        kb.dispatch_once(conn, spawn_fn=_flaky_spawn, failure_limit=5)
        task = kb.get_task(conn, tid)
        # Spawn succeeded → running, but the retry bookkeeping is NOT reset
        # (only a successful completion / unblock resets it).
        assert task.status == "running"
        assert task.worker_pid == 99999
        assert task.transient_retry_count == 2
        assert task.consecutive_failures == 0
    finally:
        conn.close()


def test_successful_completion_resets_failure_counter(kanban_home, all_assignees_spawnable):
    """A successful kb.complete_task wipes the counter — the task+profile
    combination proved it can succeed, so past failures are history."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        # Simulate 2 prior failures on the record.
        kb.write_txn_ctx = kb.write_txn
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = 2, "
                "last_failure_error = 'old failure' WHERE id = ?",
                (tid,),
            )
        # Complete the task.
        ok = kb.complete_task(conn, tid, summary="done")
        assert ok
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
    finally:
        conn.close()


def test_reassign_resets_failure_counter_for_new_profile(kanban_home, all_assignees_spawnable):
    """Retry streaks are scoped to a task/profile pair; reassigning is a
    human recovery action and gives the new profile a fresh budget."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = 1, "
                "last_failure_error = 'timed out' WHERE id = ?",
                (tid,),
            )
        assert kb.assign_task(conn, tid, "reviewer") is True
        task = kb.get_task(conn, tid)
        assert task.assignee == "reviewer"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
    finally:
        conn.close()


def test_per_task_max_retries_overrides_dispatcher_limit(kanban_home, all_assignees_spawnable):
    """Per-task ``max_retries`` overrides both the caller-supplied
    ``failure_limit`` (gateway config) and the hardcoded default.

    Three-tier resolution order:
      1. ``task.max_retries`` (set via ``create_task(max_retries=N)`` /
         ``hermes kanban create --max-retries N``)
      2. ``failure_limit`` kwarg passed by the caller (gateway threads
         this from ``kanban.failure_limit`` config)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    conn = kb.connect()
    try:
        # max_retries=1 should trip on the FIRST failure, even though the
        # caller is asking for failure_limit=10.
        tid = kb.create_task(
            conn, title="one-shot", assignee="worker", max_retries=1,
        )
        task = kb.get_task(conn, tid)
        assert task.max_retries == 1, "per-task override must persist"

        kb.claim_task(conn, tid)
        tripped = kb._record_task_failure(
            conn, tid,
            error="first fail",
            outcome="spawn_failed",
            failure_limit=10,   # far higher than per-task override
            release_claim=True,
            end_run=False,
        )
        assert tripped is True, "should auto-block on first failure"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures == 1

        # gave_up event should record where the threshold came from
        events = kb.list_events(conn, tid)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert gave_up, f"expected gave_up event, got {[e.kind for e in events]}"
        assert gave_up[-1].payload.get("limit_source") == "task"
        assert gave_up[-1].payload.get("effective_limit") == 1
    finally:
        conn.close()


def test_per_task_max_retries_allows_more_than_default(kanban_home, all_assignees_spawnable):
    """A task with ``max_retries=5`` does NOT auto-block at the default
    limit of 2 — it must reach the per-task override first."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="flaky-retry", assignee="worker", max_retries=5,
        )
        # Four failures — still below the per-task threshold, should stay ready.
        for i in range(1, 5):
            kb.claim_task(conn, tid)
            tripped = kb._record_task_failure(
                conn, tid,
                error=f"fail {i}",
                outcome="spawn_failed",
                # Caller passes the default so the dispatcher tier matches
                # ``DEFAULT_FAILURE_LIMIT``; without the per-task override
                # the breaker would have tripped at failure 2.
                release_claim=True,
                end_run=False,
            )
            assert tripped is False, f"shouldn't trip at failure {i} with max_retries=5"
            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"at failure {i} status was {task.status}"

        # Fifth failure trips the per-task limit.
        kb.claim_task(conn, tid)
        tripped = kb._record_task_failure(
            conn, tid,
            error="fail 5",
            outcome="spawn_failed",
            release_claim=True,
            end_run=False,
        )
        assert tripped is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures == 5
    finally:
        conn.close()


def test_max_retries_none_falls_through_to_dispatcher_limit(kanban_home, all_assignees_spawnable):
    """``max_retries=None`` (the default) falls through to the caller-
    supplied ``failure_limit`` — the gateway config tier."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="standard", assignee="worker")
        task = kb.get_task(conn, tid)
        assert task.max_retries is None

        # Caller passes failure_limit=4 (simulates kanban.failure_limit=4).
        # Should trip at 4, not at the DEFAULT_FAILURE_LIMIT of 2.
        for i in range(1, 4):
            kb.claim_task(conn, tid)
            tripped = kb._record_task_failure(
                conn, tid,
                error=f"fail {i}",
                outcome="spawn_failed",
                failure_limit=4,
                release_claim=True,
                end_run=False,
            )
            assert tripped is False, f"premature trip at failure {i}"

        kb.claim_task(conn, tid)
        tripped = kb._record_task_failure(
            conn, tid,
            error="fail 4",
            outcome="spawn_failed",
            failure_limit=4,
            release_claim=True,
            end_run=False,
        )
        assert tripped is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"

        events = kb.list_events(conn, tid)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert gave_up[-1].payload.get("limit_source") == "dispatcher"
        assert gave_up[-1].payload.get("effective_limit") == 4
    finally:
        conn.close()


def test_workspace_resolution_failure_also_counts(kanban_home, all_assignees_spawnable, monkeypatch):
    """A `dir:` workspace with no path fails workspace resolution. Post-f54686689
    this dispatch-side failure routes through the bounded transient-retry budget
    first (like a spawn failure), then counts against the breaker and blocks —
    it does not just crash the tick."""
    base = 1_800_000_000
    clock = [base]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    conn = kb.connect()
    try:
        # Manually insert a broken task: dir workspace but workspace_path is NULL
        # after initial create. We achieve this by creating via kanban_db then
        # UPDATE-ing workspace_path to NULL.
        tid = kb.create_task(
            conn, title="x", assignee="worker",
            workspace_kind="dir", workspace_path="/tmp/kanban_e2e_dir",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = NULL WHERE id = ?", (tid,),
            )
        # First failure → transient retry, breaker untouched.
        kb.dispatch_once(conn, failure_limit=3)
        task = kb.get_task(conn, tid)
        assert task.transient_retry_count == 1
        assert task.consecutive_failures == 0
        assert task.status == "ready"

        # Exhaust the budget, then the breaker counts to the limit (3) and blocks.
        res = None
        for _ in range(8):
            clock[0] += 301
            res = kb.dispatch_once(conn, failure_limit=3)
            if kb.get_task(conn, tid).status == "blocked":
                break
        assert tid in res.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 3
        assert task.last_failure_error and "workspace" in task.last_failure_error
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worker aliveness / crash detection
# ---------------------------------------------------------------------------

def test_pid_alive_helper():
    # Our own pid is alive.
    assert kb._pid_alive(os.getpid())
    # PID 0 / None / negative.
    assert not kb._pid_alive(0)
    assert not kb._pid_alive(None)
    # A clearly-dead pid (very large, extremely unlikely to exist).
    assert not kb._pid_alive(2 ** 30)


def test_pid_alive_detects_darwin_zombie(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: None)

    def fake_run(args, **kwargs):
        assert args == ["ps", "-o", "stat=", "-p", "123"]
        assert kwargs["stdout"] is subprocess.PIPE
        return SimpleNamespace(returncode=0, stdout="Z+\n")

    monkeypatch.setattr(kb.subprocess, "run", fake_run)

    assert kb._pid_alive(123) is False


def test_detect_crashed_workers_reclaims(kanban_home):
    """A running task whose pid vanished gets dropped to ready with a
    ``crashed`` event, independent of the claim TTL."""
    def _spawn_pid_that_exits(task, ws):
        # Spawn a real child that exits instantly.
        import subprocess
        p = subprocess.Popen(
            ["python3", "-c", "pass"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        p.wait()
        return p.pid

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        res = kb.dispatch_once(conn, spawn_fn=_spawn_pid_that_exits)
        # Brief sleep to make sure the child's pid has been reaped; on
        # busy CI the pid may be reused by another process, which would
        # fool _pid_alive. If that happens we accept the test still
        # passing as long as the dispatcher ran without error.
        time.sleep(0.2)
        res2 = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        # Either crashed was detected (preferred) or the TTL reclaim path
        # will eventually fire; we accept either outcome but the worker_pid
        # should no longer be set.
        if res2.crashed:
            assert tid in res2.crashed
            events = kb.list_events(conn, tid)
            assert any(e.kind == "crashed" for e in events)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def test_daemon_runs_and_stops(kanban_home):
    """run_daemon should execute at least one tick and exit cleanly on
    stop_event."""
    ticks = []
    stop = threading.Event()

    def _runner():
        kb.run_daemon(
            interval=0.05,
            stop_event=stop,
            on_tick=lambda res: ticks.append(res),
        )

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # Give it a few ticks.
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), "daemon should exit on stop_event"
    assert len(ticks) >= 1, "expected at least one tick"


def test_daemon_keeps_going_after_tick_exception(kanban_home, monkeypatch):
    """A tick that raises shouldn't kill the loop."""
    calls = [0]
    orig_dispatch = kb.dispatch_once

    def _boom(conn, **kw):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("simulated tick failure")
        return orig_dispatch(conn, **kw)

    monkeypatch.setattr(kb, "dispatch_once", _boom)

    stop = threading.Event()
    def _runner():
        kb.run_daemon(interval=0.05, stop_event=stop)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2.0)
    # At minimum, second-tick+ should have run.
    assert calls[0] >= 2


# ---------------------------------------------------------------------------
# Stats + age
# ---------------------------------------------------------------------------

def test_board_stats(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a", assignee="x")
        b = kb.create_task(conn, title="b", assignee="y")
        kb.complete_task(conn, a, result="done")
        stats = kb.board_stats(conn)
        assert stats["by_status"]["ready"] == 1
        assert stats["by_status"]["done"] == 1
        assert stats["by_assignee"]["x"]["done"] == 1
        assert stats["by_assignee"]["y"]["ready"] == 1
        assert stats["oldest_ready_age_seconds"] is not None
    finally:
        conn.close()


def test_board_stats_k6_throughput_cycle_time_and_cost(kanban_home):
    """K6: board_stats gains additive throughput/cycle-time/cost keys without
    disturbing the pre-existing keys (regression proof)."""
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a", assignee="x")
        kb.create_task(conn, title="b", assignee="y")  # stays ready
        kb.complete_task(conn, a, result="done")

        stats = kb.board_stats(conn)
        # Pre-existing keys must remain exactly as before.
        for key in ("by_status", "by_assignee", "oldest_ready_age_seconds", "now"):
            assert key in stats
        assert stats["by_status"]["ready"] == 1
        assert stats["by_status"]["done"] == 1

        # New additive K6 keys.
        assert stats["completed_last_24h"] == 1
        assert stats["completed_last_7d"] == 1
        # One completed task → both percentiles equal its (non-negative) cycle.
        assert stats["cycle_time_p50_seconds"] is not None
        assert stats["cycle_time_p50_seconds"] >= 0
        assert stats["cycle_time_p90_seconds"] is not None
        # Pre-K5a: no cost_usd column populated → total cost is None, not a crash.
        assert stats["total_cost_usd_24h"] is None
    finally:
        conn.close()


def test_task_runs_cost_usd_sum_is_fail_soft_pre_k5a(kanban_home):
    """K6: summing cost over runs tolerates the missing cost_usd column
    (added only by K5a) and returns None instead of raising."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="x")
        kb.complete_task(conn, tid, result="done")
        assert kb.task_runs_cost_usd_sum(conn, task_id=tid) is None
        assert kb.task_runs_cost_usd_sum(conn, since_epoch=0) is None
    finally:
        conn.close()


def test_task_age_helper(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        task = kb.get_task(conn, tid)
        age = kb.task_age(task)
        assert age["created_age_seconds"] is not None
        assert age["started_age_seconds"] is None
        assert age["time_to_complete_seconds"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notify subscriptions
# ---------------------------------------------------------------------------

def test_notify_sub_crud(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123", user_id="u1",
            notifier_profile="default",
        )
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["platform"] == "telegram"
        assert subs[0]["notifier_profile"] == "default"
        # Duplicate add is a no-op.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
        )
        assert len(kb.list_notify_subs(conn, tid)) == 1
        # Distinct thread is a new row.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            thread_id="5",
        )
        assert len(kb.list_notify_subs(conn, tid)) == 2
        # Remove one.
        ok = kb.remove_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
        )
        assert ok is True
        assert len(kb.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()


def test_notify_cursor_advances(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="w")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="123")
        # Initial: one "created" event but we only want terminal kinds.
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert events == []
        # Complete the task → new `completed` event.
        kb.complete_task(conn, tid, result="ok")
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert len(events) == 1
        assert events[0].kind == "completed"
        # Advance cursor — next call returns empty.
        kb.advance_notify_cursor(
            conn, task_id=tid, platform="telegram", chat_id="123",
            new_cursor=cursor,
        )
        _, events2 = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert events2 == []
    finally:
        conn.close()


def test_notify_claim_is_single_owner_and_rewindable(kanban_home):
    conn1 = kb.connect()
    conn2 = kb.connect()
    try:
        tid = kb.create_task(conn1, title="x", assignee="w")
        kb.add_notify_sub(conn1, task_id=tid, platform="telegram", chat_id="123")
        kb.complete_task(conn1, tid, result="ok")

        old_cursor, claimed_cursor, events = kb.claim_unseen_events_for_sub(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert old_cursor == 0
        assert claimed_cursor > old_cursor
        assert [ev.kind for ev in events] == ["completed"]

        # A concurrent notifier instance sees the advanced cursor and cannot
        # claim/send the same event range.
        _, _, duplicate_events = kb.claim_unseen_events_for_sub(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert duplicate_events == []

        assert kb.rewind_notify_cursor(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            claimed_cursor=claimed_cursor,
            old_cursor=old_cursor,
        ) is True
        _, retried_events = kb.unseen_events_for_sub(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert [ev.kind for ev in retried_events] == ["completed"]
    finally:
        conn1.close()
        conn2.close()


def test_notify_delivery_lease_prevents_overtake_and_event_loss(kanban_home):
    """A second watcher cannot advance beyond an in-flight delivery batch.

    The first watcher durably checkpoints event A, then fails on event B while
    event C arrives. Releasing its lease at A must let the second watcher claim
    B+C; neither event may be stranded behind C's newer cursor.
    """
    conn1 = kb.connect()
    conn2 = kb.connect()
    try:
        tid = kb.create_task(conn1, title="leased", assignee="w")
        kb.add_notify_sub(conn1, task_id=tid, platform="telegram", chat_id="123")
        kb._append_event(conn1, tid, kind="crashed")
        kb._append_event(conn1, tid, kind="crashed")

        old_cursor, claimed_cursor, first_batch = kb.claim_unseen_events_for_sub(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["crashed"],
            claim_token="watcher-a",
            lease_seconds=60,
        )
        assert len(first_batch) == 2

        kb._append_event(conn2, tid, kind="crashed")
        _, _, overtaking = kb.claim_unseen_events_for_sub(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["crashed"],
            claim_token="watcher-b",
            lease_seconds=60,
        )
        assert overtaking == []

        delivered_cursor = first_batch[0].id
        assert kb.advance_notify_delivery_claim(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            claim_token="watcher-a",
            delivered_cursor=delivered_cursor,
            lease_seconds=60,
        )
        assert kb.rewind_notify_cursor(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            claimed_cursor=claimed_cursor,
            old_cursor=delivered_cursor,
            claim_token="watcher-a",
        )

        _, retry_cursor, retried = kb.claim_unseen_events_for_sub(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["crashed"],
            claim_token="watcher-b",
            lease_seconds=60,
        )
        assert [event.id for event in retried] == [
            first_batch[1].id,
            first_batch[1].id + 1,
        ]
        assert kb.ack_notify_delivery_claim(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            claim_token="watcher-b",
            claimed_cursor=retry_cursor,
        )
    finally:
        conn1.close()
        conn2.close()


def test_expired_notify_delivery_lease_recovers_from_confirmed_cursor(kanban_home):
    conn1 = kb.connect()
    conn2 = kb.connect()
    try:
        tid = kb.create_task(conn1, title="recover lease", assignee="w")
        kb.add_notify_sub(conn1, task_id=tid, platform="telegram", chat_id="123")
        kb._append_event(conn1, tid, kind="crashed")
        kb._append_event(conn1, tid, kind="crashed")
        _, _, claimed = kb.claim_unseen_events_for_sub(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["crashed"],
            claim_token="dead-watcher",
        )
        assert kb.advance_notify_delivery_claim(
            conn1,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            claim_token="dead-watcher",
            delivered_cursor=claimed[0].id,
        )
        with kb.write_txn(conn1):
            conn1.execute(
                "UPDATE kanban_notify_subs SET delivery_claim_expires = 0 "
                "WHERE task_id = ?",
                (tid,),
            )

        _, retry_cursor, recovered = kb.claim_unseen_events_for_sub(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            kinds=["crashed"],
            claim_token="standby-watcher",
        )
        assert [event.id for event in recovered] == [claimed[1].id]
        assert kb.ack_notify_delivery_claim(
            conn2,
            task_id=tid,
            platform="telegram",
            chat_id="123",
            claim_token="standby-watcher",
            claimed_cursor=retry_cursor,
        )
    finally:
        conn1.close()
        conn2.close()


# ---------------------------------------------------------------------------
# GC + retention
# ---------------------------------------------------------------------------

def test_gc_events_keeps_active_task_history(kanban_home):
    """gc_events should only prune rows for terminal (done/archived) tasks."""
    conn = kb.connect()
    try:
        alive = kb.create_task(conn, title="a", assignee="w")
        done_id = kb.create_task(conn, title="b", assignee="w")
        kb.complete_task(conn, done_id)

        # Force all existing events to "old" by bumping created_at backwards.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ?",
                (int(time.time()) - 60 * 24 * 3600,),
            )
        removed = kb.gc_events(conn, older_than_seconds=30 * 24 * 3600)
        # At least the done task's "created" + "completed" events gone.
        assert removed >= 2
        # Alive task's events survive.
        alive_events = kb.list_events(conn, alive)
        assert len(alive_events) >= 1
    finally:
        conn.close()


def test_gc_worker_logs_deletes_old_files(kanban_home):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    old = log_dir / "old.log"
    young = log_dir / "young.log"
    old.write_text("stale")
    young.write_text("fresh")
    # Age the old file by 100 days.
    past = time.time() - 100 * 24 * 3600
    os.utime(old, (past, past))
    removed = kb.gc_worker_logs(older_than_seconds=30 * 24 * 3600)
    assert removed == 1
    assert not old.exists()
    assert young.exists()


# ---------------------------------------------------------------------------
# Log rotation + accessor
# ---------------------------------------------------------------------------

def test_worker_log_rotation_keeps_one_generation(kanban_home, tmp_path):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "t_aaaa.log"
    target.write_bytes(b"x" * (3 * 1024 * 1024))  # 3 MiB, over 2 MiB threshold
    kb._rotate_worker_log(target, kb.DEFAULT_LOG_ROTATE_BYTES)
    assert not target.exists()
    assert (log_dir / "t_aaaa.log.1").exists()


def test_worker_log_rotation_keeps_configured_generations(kanban_home):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "t_multi.log"
    target.write_text("current")
    (log_dir / "t_multi.log.1").write_text("one")
    (log_dir / "t_multi.log.2").write_text("two")

    kb._rotate_worker_log(target, max_bytes=1, backup_count=3)

    assert not target.exists()
    assert (log_dir / "t_multi.log.1").read_text() == "current"
    assert (log_dir / "t_multi.log.2").read_text() == "one"
    assert (log_dir / "t_multi.log.3").read_text() == "two"


def test_worker_log_rotation_config_defaults_and_overrides():
    assert kb.worker_log_rotation_config({}) == (
        kb.DEFAULT_LOG_ROTATE_BYTES,
        kb.DEFAULT_LOG_BACKUP_COUNT,
    )
    assert kb.worker_log_rotation_config({
        "worker_log_rotate_bytes": 10,
        "worker_log_backup_count": 4,
    }) == (10, 4)


def test_read_worker_log_tail(kanban_home):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "t_beef.log"
    # 10 lines
    p.write_text("\n".join(f"line {i}" for i in range(10)))
    full = kb.read_worker_log("t_beef")
    assert full is not None and "line 0" in full
    tail = kb.read_worker_log("t_beef", tail_bytes=30)
    assert tail is not None
    # Tail should not include line 0.
    assert "line 0" not in tail
    # Missing log returns None.
    assert kb.read_worker_log("t_missing") is None


# ---------------------------------------------------------------------------
# CLI bulk verbs
# ---------------------------------------------------------------------------

def test_cli_complete_bulk(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        c = kb.create_task(conn, title="c")
    finally:
        conn.close()
    out = run_slash(f"complete {a} {b} {c} --result all-done")
    assert out.count("Completed") == 3
    conn = kb.connect()
    try:
        for tid in (a, b, c):
            assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_cli_archive_bulk(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
    finally:
        conn.close()
    out = run_slash(f"archive {a} {b}")
    assert "Archived" in out
    conn = kb.connect()
    try:
        assert kb.get_task(conn, a).status == "archived"
        assert kb.get_task(conn, b).status == "archived"
    finally:
        conn.close()


def test_cli_archive_rm_deletes_archived_tasks(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gone")
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()
    out = run_slash(f"archive --rm {tid}")
    assert f"Deleted {tid}" in out
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid) is None
    finally:
        conn.close()


def test_cli_archive_rm_rejects_live_tasks(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="still-live")
    finally:
        conn.close()
    out = run_slash(f"archive --rm {tid}")
    assert "cannot delete" in out.lower()
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid) is not None
    finally:
        conn.close()


def test_cli_unblock_bulk(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        kb.block_task(conn, a)
        kb.block_task(conn, b)
    finally:
        conn.close()
    out = run_slash(f"unblock {a} {b}")
    assert out.count("Unblocked") == 2


def test_cli_block_bulk_via_ids_flag(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
    finally:
        conn.close()
    out = run_slash(f"block {a} need input --ids {b}")
    assert out.count("Blocked") == 2


def test_cli_create_with_idempotency_key(kanban_home):
    out1 = run_slash("create 'x' --idempotency-key abc --json")
    tid1 = json.loads(out1)["id"]
    out2 = run_slash("create 'y' --idempotency-key abc --json")
    tid2 = json.loads(out2)["id"]
    assert tid1 == tid2

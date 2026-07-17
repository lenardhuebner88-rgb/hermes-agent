"""Kanban core functionality tests: recovery failure.

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


def _drive_worker_exit(conn, tid, fake_pid, raw_status):
    """Claim ``tid``, record ``raw_status`` for its dead worker pid, and run
    one reaper pass.

    Deliberately resolves ``hermes_cli.kanban_db`` fresh and uses that single
    module object for the exit registry, the liveness patch, AND the reaper:
    earlier tests in a full-suite run can reload the module, and recording
    the exit into one module object while reaping through another (stale)
    one makes ``_classify_worker_exit`` return ``unknown`` — silently turning
    a clean-exit protocol violation into a plain crash.
    """
    import hermes_cli.kanban_db as _kb
    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    _kb._set_worker_pid(conn, tid, fake_pid)
    _kb._record_worker_exit(fake_pid, raw_status)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kb.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def _drive_protocol_violation(conn, tid, fake_pid):
    """One clean-exit protocol violation reaper pass for ``tid``.

    os.W_EXITCODE(status=0, signal=0) == 0 on POSIX.
    """
    return _drive_worker_exit(conn, tid, fake_pid, 0)


def _drive_nonzero_crash(conn, tid, fake_pid):
    """One plain non-zero-exit crash reaper pass for ``tid``.

    W_EXITCODE(1, 0) == 256 — WIFEXITED True, WEXITSTATUS == 1.
    """
    return _drive_worker_exit(conn, tid, fake_pid, 256)


def _created_id(out: str) -> str:
    return out.split("Created ", 1)[1].split()[0]


# ---------------------------------------------------------------------------
# Hallucination gate (created_cards verify + prose scan)
# ---------------------------------------------------------------------------

def test_complete_with_created_cards_all_verified_records_manifest(kanban_home):
    """A completion with created_cards that all exist + belong to this
    worker records them on the ``completed`` event payload."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        c1 = kb.create_task(conn, title="c1", assignee="x", created_by="alice")
        c2 = kb.create_task(conn, title="c2", assignee="y", created_by="alice")
        ok = kb.complete_task(
            conn, parent,
            summary="done, created c1+c2",
            created_cards=[c1, c2],
        )
        assert ok is True
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (parent,),
        ))
        completed = [e for e in evs if e["kind"] == "completed"]
        assert len(completed) == 1
        import json as _json
        payload = _json.loads(completed[0]["payload"])
        assert payload.get("verified_cards") == [c1, c2]
    finally:
        conn.close()


def test_complete_with_phantom_created_cards_raises_and_audits(kanban_home):
    """A completion claiming a card id that doesn't exist raises
    HallucinatedCardsError, leaves the task in its prior state, and
    records a ``completion_blocked_hallucination`` event for auditing."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        phantom_id = "t_deadbeefcafe"

        with pytest.raises(kb.HallucinatedCardsError) as excinfo:
            kb.complete_task(
                conn, parent,
                summary="claimed phantom",
                created_cards=[real, phantom_id],
            )
        assert excinfo.value.phantom == [phantom_id]

        # Task still in prior state (ready, not done).
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (parent,),
        ).fetchone()
        assert row["status"] == "ready"

        # Audit event landed.
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (parent,),
            )
        ]
        assert "completion_blocked_hallucination" in kinds
        assert "completed" not in kinds
    finally:
        conn.close()


def test_complete_with_cross_worker_card_is_rejected(kanban_home):
    """A card that exists but was created by a different worker profile
    is treated as phantom (hallucinated attribution)."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        other = kb.create_task(conn, title="other", assignee="x", created_by="bob")

        with pytest.raises(kb.HallucinatedCardsError) as excinfo:
            kb.complete_task(
                conn, parent,
                summary="claiming someone else's card",
                created_cards=[other],
            )
        assert excinfo.value.phantom == [other]
    finally:
        conn.close()


def test_complete_accepts_cross_worker_card_when_linked_as_child(kanban_home):
    """A card created by a different principal but explicitly linked as
    a child of the completing task is accepted — the worker took
    ownership via ``kanban_create(parents=[current_task])`` or an
    explicit ``link_tasks`` call, which proves the relationship even
    when ``created_by`` doesn't match.

    (Relaxation salvaged from #20022 @LeonSGP43 — stricter version
    would incorrectly reject legitimate orchestrator flows where a
    specifier creates a card, then a worker picks it up and links it
    to its own parent task.)
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        # Card created by a DIFFERENT principal (not alice, not parent).
        other = kb.create_task(
            conn, title="other", assignee="x", created_by="bob",
            parents=[parent],  # explicitly links as child of the completing task
        )

        ok = kb.complete_task(
            conn, parent,
            summary="completed with linked child",
            created_cards=[other],
        )
        assert ok is True
        # The card should appear in the completed event's verified_cards list.
        import json as _json
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='completed' ORDER BY id DESC LIMIT 1",
            (parent,),
        ).fetchone()
        payload = _json.loads(row["payload"])
        assert other in payload.get("verified_cards", [])
    finally:
        conn.close()


def test_complete_can_retry_after_phantom_rejection(kanban_home):
    """A worker that hits the hallucinated-card gate must be able to
    retry kanban_complete on the same task — both with a corrected
    created_cards list and with an empty list (the documented escape
    hatch). Regression test for #22923, where workers were believed to
    be unrecoverable after the first rejection.
    """
    conn = kb.connect()
    try:
        # Two parallel completing tasks so we can exercise both retry
        # shapes without status interference.
        parent_a = kb.create_task(conn, title="retry-empty", assignee="alice")
        kb.claim_task(conn, parent_a)
        parent_b = kb.create_task(conn, title="retry-corrected", assignee="alice")
        kb.claim_task(conn, parent_b)
        real = kb.create_task(
            conn, title="real-child", assignee="x", created_by="alice",
        )

        # First attempt: phantom in the list rejects, task stays running.
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent_a,
                summary="oops",
                created_cards=["t_phantomdeadbeef"],
            )
        assert kb.get_task(conn, parent_a).status == "running"

        # Retry with [] (escape hatch): gate is skipped, completion lands.
        ok = kb.complete_task(
            conn, parent_a,
            summary="retry without claims",
            created_cards=[],
        )
        assert ok is True
        assert kb.get_task(conn, parent_a).status == "done"

        # Same flow on parent_b, but recover via a corrected list rather
        # than the empty escape hatch.
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent_b,
                summary="oops",
                created_cards=[real, "t_anotherphantom"],
            )
        assert kb.get_task(conn, parent_b).status == "running"

        ok = kb.complete_task(
            conn, parent_b,
            summary="retry with corrected list",
            created_cards=[real],
        )
        assert ok is True
        assert kb.get_task(conn, parent_b).status == "done"

        # Both audit events landed; the eventual completion event is
        # also present on each task.
        for parent in (parent_a, parent_b):
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                    (parent,),
                )
            ]
            assert kinds.count("completion_blocked_hallucination") == 1
            assert kinds.count("completed") == 1
    finally:
        conn.close()


def test_complete_prose_scan_flags_nonexistent_ids(kanban_home):
    """Successful completion whose summary references a ``t_<hex>`` id
    that doesn't resolve emits a ``suspected_hallucinated_references``
    event. Does not block the completion."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="x")
        ok = kb.complete_task(
            conn, parent,
            summary="also saw t_abcd1234ffff failing in CI",
        )
        assert ok is True
        kinds_and_payloads = list(conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (parent,),
        ))
        kinds = [r["kind"] for r in kinds_and_payloads]
        assert "suspected_hallucinated_references" in kinds
        import json as _json
        susp = [
            _json.loads(r["payload"])
            for r in kinds_and_payloads
            if r["kind"] == "suspected_hallucinated_references"
        ][0]
        assert "t_abcd1234ffff" in susp["phantom_refs"]
    finally:
        conn.close()


def test_complete_prose_scan_ignores_existing_ids(kanban_home):
    """Summaries referencing real task ids don't emit a warning."""
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="other", assignee="x")
        parent = kb.create_task(conn, title="parent", assignee="x")
        ok = kb.complete_task(
            conn, parent,
            summary=f"depended on {other}, now done",
        )
        assert ok is True
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (parent,),
            )
        ]
        assert "suspected_hallucinated_references" not in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recovery helpers (reclaim + reassign)
# ---------------------------------------------------------------------------

def test_reclaim_task_resets_running_to_ready(kanban_home, monkeypatch):
    """Manual reclaim releases the claim, resets status, and emits a
    ``reclaimed`` event even when claim_expires has not passed."""
    import signal
    import time
    import secrets
    import hermes_cli.kanban_db as _kb
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="stuck", assignee="broken")
        # Simulate a live claim (not expired).
        lock = f"{_kb._claimer_id().split(':', 1)[0]}:{secrets.token_hex(8)}"
        future = int(time.time()) + 3600
        killed: list[int] = []
        state = {"alive": True}

        def _signal(pid, sig):
            killed.append(sig)
            if sig == signal.SIGTERM:
                state["alive"] = False

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: state["alive"])
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 12345, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 12345, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()

        # release_stale_claims should NOT reclaim (not expired).
        assert kb.release_stale_claims(conn) == 0

        # reclaim_task should work immediately.
        assert kb.reclaim_task(conn, t, reason="test reason", signal_fn=_signal) is True

        row = conn.execute(
            "SELECT status, claim_lock, worker_pid FROM tasks WHERE id=?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        assert row["worker_pid"] is None

        import json as _json
        reclaim_evs = [
            _json.loads(r["payload"])
            for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='reclaimed'",
                (t,),
            )
        ]
        assert len(reclaim_evs) == 1
        assert reclaim_evs[0].get("manual") is True
        assert reclaim_evs[0].get("reason") == "test reason"
        assert reclaim_evs[0].get("termination_attempted") is True
        assert reclaim_evs[0].get("terminated") is True
        assert killed == [signal.SIGTERM]
    finally:
        conn.close()


def test_reclaim_task_returns_false_for_already_ready(kanban_home):
    """Reclaiming a task that's not running returns False (no-op)."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="ready task", assignee="x")
        assert kb.reclaim_task(conn, t) is False
    finally:
        conn.close()


def test_reassign_task_refuses_running_without_reclaim_first(kanban_home):
    """Without ``reclaim_first=True``, reassigning a running task is a
    no-op returning False (matches assign_task's RuntimeError via
    internal catch)."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="orig")
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=? WHERE id=?",
            ("live", t),
        )
        conn.commit()
        assert kb.reassign_task(conn, t, "new") is False
        # Assignee unchanged.
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "orig"
    finally:
        conn.close()


def test_reassign_task_with_reclaim_first_switches_profile(
    kanban_home, monkeypatch,
):
    """With ``reclaim_first=True``, a running task is reclaimed and
    reassigned in one operation."""
    import time
    import secrets
    # The fabricated claim uses a bare hex lock (not host-local), so the
    # real termination probe cannot confirm the fake worker is gone; stub
    # the canonical confirmed-dead shape — termination itself is not what
    # this test exercises.
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker",
        lambda *a, **k: {"terminated": True},
    )
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="switch me", assignee="orig")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()

        assert kb.reassign_task(
            conn, t, "new-profile",
            reclaim_first=True, reason="switch model",
        ) is True

        row = conn.execute(
            "SELECT assignee, status FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "new-profile"
        assert row["status"] == "ready"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unified failure counter — timeout + crash paths increment the same counter
# as spawn failures, and the circuit breaker trips after N consecutive
# failures regardless of which outcome caused them.
# ---------------------------------------------------------------------------

def test_enforce_max_runtime_increments_consecutive_failures(kanban_home, monkeypatch):
    """A single timeout increments consecutive_failures by 1 (was the
    infinite-respawn gap before unification)."""
    import hermes_cli.kanban_db as _kb
    state = {"sent_term": False}
    def _alive(pid):
        return not state["sent_term"]
    def _signal(pid, sig):
        import signal as _sig
        if sig == _sig.SIGTERM:
            state["sent_term"] = True
    monkeypatch.setattr(_kb, "_pid_alive", _alive)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="overrun", assignee="worker",
            max_runtime_seconds=1,
        )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 54321)
        # Since PR #19473 (salvaged) changed enforce_max_runtime to read
        # from task_runs.started_at (per-attempt) rather than
        # tasks.started_at (lifetime), we need to backdate BOTH to
        # guarantee the timeout fires regardless of which column the
        # query pulls from.
        with kb.write_txn(conn):
            long_ago = int(time.time()) - 30
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (long_ago, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (long_ago, tid),
            )
        before = kb.get_task(conn, tid)
        assert before.consecutive_failures == 0

        kb.enforce_max_runtime(conn, signal_fn=_signal)

        after = kb.get_task(conn, tid)
        assert after.consecutive_failures == 1
        assert "elapsed" in (after.last_failure_error or "")
        # Task status flipped back to ready (not yet past threshold).
        assert after.status == "ready"
    finally:
        conn.close()


def test_repeated_timeouts_trip_the_circuit_breaker(kanban_home, monkeypatch):
    """N consecutive timeouts with the unified counter should eventually
    hit the failure_limit threshold and auto-block the task. This closes
    the Forbidden-Seeds-reported gap where timeout loops never capped.
    """
    import hermes_cli.kanban_db as _kb
    state = {"sent_term": False}
    def _alive(pid):
        return not state["sent_term"]
    def _signal(pid, sig):
        import signal as _sig
        if sig == _sig.SIGTERM:
            state["sent_term"] = True
    monkeypatch.setattr(_kb, "_pid_alive", _alive)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="loop forever", assignee="slow-worker",
            max_runtime_seconds=1,
        )
        # Drop the failure_limit to 3 so we don't need 5 timeouts.
        # This uses the module-level DEFAULT; we simulate by calling
        # _record_task_failure directly with a tight limit.
        worker_pid = 54321
        for _ in range(3):
            # Fresh claim + "started long ago" each iteration.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='running', claim_lock=?, "
                    "claim_expires=?, worker_pid=?, started_at=? "
                    "WHERE id=?",
                    (
                        f"{_kb._claimer_id().split(':', 1)[0]}:lock",
                        int(time.time()) + 3600,
                        worker_pid,
                        int(time.time()) - 30,
                        tid,
                    ),
                )
                conn.execute(
                    "INSERT INTO task_runs (task_id, status, claim_lock, "
                    "claim_expires, worker_pid, started_at) "
                    "VALUES (?, 'running', ?, ?, ?, ?)",
                    (
                        tid,
                        f"{_kb._claimer_id().split(':', 1)[0]}:lock",
                        int(time.time()) + 3600,
                        worker_pid,
                        int(time.time()) - 30,
                    ),
                )
                rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "UPDATE tasks SET current_run_id=? WHERE id=?",
                    (rid, tid),
                )
            state["sent_term"] = False
            # Lower the threshold by monkeypatching the default.
            monkeypatch.setattr(_kb, "DEFAULT_FAILURE_LIMIT", 3)
            kb.enforce_max_runtime(conn, signal_fn=_signal)

        final = kb.get_task(conn, tid)
        # After 3 consecutive timeouts with failure_limit=3, task should
        # be auto-blocked, not looping forever as ``ready``.
        assert final.status == "blocked", \
            f"expected blocked after 3 timeouts, got {final.status}"
        assert final.consecutive_failures >= 3
        # ``gave_up`` event emitted (plus 3 ``timed_out`` events).
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (tid,),
            )
        ]
        assert kinds.count("timed_out") >= 3
        assert "gave_up" in kinds
    finally:
        conn.close()


def test_detect_crashed_workers_increments_counter(kanban_home):
    """An unknown dead PID increments the transient_retry_count side-channel.

    Post-1bd00640c an *unknown* dead worker PID (no recorded exit) is a bounded
    transient recovery: it bumps ``transient_retry_count`` and requeues the task
    ``ready`` WITHOUT counting a hard failure, so it is reported on
    ``_last_transient_recovered`` and ``consecutive_failures`` stays 0. A real
    crash (nonzero/signaled exit) bumping ``consecutive_failures`` is covered by
    test_kanban_death_recovery.py::test_nonzero_dead_pid_still_counts_as_real_crash."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="crashy", assignee="worker")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 99999)  # fake pid — not alive

        crashed = kb.detect_crashed_workers(conn)

        assert crashed == []
        assert kb.detect_crashed_workers._last_transient_recovered == [tid]
        task = kb.get_task(conn, tid)
        assert task.transient_retry_count == 1
        assert task.consecutive_failures == 0
        assert task.status == "ready"
    finally:
        conn.close()


def test_detect_crashed_workers_protocol_violation_first_occurrence_retries(kanban_home):
    """A first clean-exit protocol violation gets a retry, not a block.

    A worker that exited rc=0 while its task was still ``running`` skipped
    the terminal kanban call (model answered conversationally, transient tool
    wedge). Empirically these overwhelmingly complete on respawn, so the
    first violation must leave the task ``ready`` with corrective guidance
    stamped in ``last_failure_error`` — not trip the breaker like the pre-fix
    behavior did. The violation is accounted against its own violation-only
    streak, so it must NOT tick the unified ``consecutive_failures`` counter.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="quiet", assignee="worker")
        result_crashed = _drive_protocol_violation(conn, tid, 999998)
        assert tid in result_crashed, "should be detected as crashed"

        task = kb.get_task(conn, tid)
        assert task.status == "ready", (
            f"first protocol violation should retry, got status={task.status}"
        )
        assert task.consecutive_failures == 0, (
            "a below-budget violation must not consume the unified failure "
            f"budget, got consecutive_failures={task.consecutive_failures}"
        )
        assert "kanban_complete" in (task.last_failure_error or ""), (
            f"expected protocol-violation message, got {task.last_failure_error!r}"
        )

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "protocol_violation" in kinds, (
            f"expected 'protocol_violation' event, got {kinds}"
        )
        # The ``crashed`` event would be misleading here — the worker
        # didn't crash, it returned 0.
        assert "crashed" not in kinds, (
            f"should NOT emit 'crashed' event on clean exit, got {kinds}"
        )
        assert "gave_up" not in kinds, (
            f"breaker must not trip on the first violation, got {kinds}"
        )
    finally:
        conn.close()


def test_detect_crashed_workers_protocol_violation_streak_trips_at_limit(kanban_home):
    """The violation streak trips the terminal path exactly at the bound.

    Genuine repeat offenders (a worker whose CLI keeps returning 0 without a
    terminal transition) must still surface to a human: the
    ``_PROTOCOL_VIOLATION_FAILURE_LIMIT``-th consecutive violation blocks the
    task with a ``gave_up`` event carrying the streak accounting.
    """
    import hermes_cli.kanban_db as _kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="quiet", assignee="worker")
        limit = _kb._PROTOCOL_VIOLATION_FAILURE_LIMIT
        for i in range(limit - 1):
            _drive_protocol_violation(conn, tid, 990000 + i)
            assert kb.get_task(conn, tid).status == "ready", (
                f"violation {i + 1}/{limit} should still retry"
            )

        _drive_protocol_violation(conn, tid, 990900)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"violation streak at the bound must block, got {task.status}"
        )
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert kinds.count("protocol_violation") == limit
        assert "crashed" not in kinds
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert len(gave_up) == 1, f"expected exactly one gave_up, got {kinds}"
        payload = gave_up[0].payload or {}
        assert payload.get("protocol_violations") == limit
        assert payload.get("protocol_violation_limit") == limit
        # Side channel consumed by dispatch_once — read through the same
        # (current) module object the reaper ran in, see _drive_worker_exit.
        assert tid in _kb.detect_crashed_workers._last_auto_blocked
    finally:
        conn.close()


def test_protocol_violation_budget_not_consumed_by_other_failures(kanban_home):
    """Mixed failure kinds must not consume the violation retry budget.

    Regression for the #61233 review finding: expressed as a plain
    ``failure_limit`` over the unified ``consecutive_failures`` counter, the
    violation budget was consumed by earlier timeouts / nonzero exits. As a
    violation-only streak, a prior real crash must not eat violation
    retries, and below-budget violations must leave the unified counter
    untouched (so the two budgets stay independent).
    """
    import hermes_cli.kanban_db as _kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="mixed", assignee="worker")

        # One real crash: unified counter ticks to 1 (below
        # DEFAULT_FAILURE_LIMIT=2 — task stays ready).
        _drive_nonzero_crash(conn, tid, 991000)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Two violations after it: streak 1 and 2 — both retry, unified
        # counter untouched. (Pre-fix: the crash consumed the budget and the
        # violations blocked well before three of them happened.)
        for i, pid in enumerate((991001, 991002)):
            _drive_protocol_violation(conn, tid, pid)
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"violation {i + 1} after a crash must still retry, "
                f"got {task.status}"
            )
            assert task.consecutive_failures == 1, (
                "below-budget violations must not tick the unified counter"
            )

        # Third consecutive violation: streak hits the bound — blocked.
        _drive_protocol_violation(conn, tid, 991003)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        gave_up = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert len(gave_up) == 1
        assert (gave_up[0].payload or {}).get("protocol_violations") == \
            _kb._PROTOCOL_VIOLATION_FAILURE_LIMIT
    finally:
        conn.close()


def test_protocol_violation_streak_resets_on_other_failure_kind(kanban_home):
    """A non-violation failure between violations resets the streak.

    The budget counts CONSECUTIVE clean-exit violations: two violations, a
    real crash, then two more violations is a streak of 2 — not 4 — so the
    fourth violation must still retry; only a third consecutive one blocks.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="reset", assignee="worker")

        _drive_protocol_violation(conn, tid, 993000)
        _drive_protocol_violation(conn, tid, 993001)
        assert kb.get_task(conn, tid).status == "ready"

        # Real crash breaks the streak (and ticks the unified counter to 1).
        _drive_nonzero_crash(conn, tid, 993002)
        assert kb.get_task(conn, tid).status == "ready"

        # Streak restarts at 1, 2 — the pre-crash violations no longer count.
        _drive_protocol_violation(conn, tid, 993003)
        assert kb.get_task(conn, tid).status == "ready", (
            "violation streak must reset after a non-violation failure"
        )
        _drive_protocol_violation(conn, tid, 993004)
        assert kb.get_task(conn, tid).status == "ready"

        # Third consecutive violation since the crash: blocked.
        _drive_protocol_violation(conn, tid, 993005)
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_protocol_violation_respects_max_retries_precedence(kanban_home):
    """Per-task ``max_retries`` overrides the violation bound, both ways.

    Same top precedence it has for every other failure kind in
    ``_record_task_failure``: ``max_retries=1`` blocks on the FIRST violation
    (zero retries — the pre-fix behavior, now opt-in per task);
    ``max_retries=5`` keeps retrying past the default bound of 3 and blocks
    on the 5th consecutive violation.
    """
    conn = kb.connect()
    try:
        strict = kb.create_task(
            conn, title="strict", assignee="worker", max_retries=1,
        )
        _drive_protocol_violation(conn, strict, 992000)
        task = kb.get_task(conn, strict)
        assert task.status == "blocked", (
            f"max_retries=1 must block on the first violation, got {task.status}"
        )
        gave_up = [e for e in kb.list_events(conn, strict) if e.kind == "gave_up"]
        assert len(gave_up) == 1
        payload = gave_up[0].payload or {}
        assert payload.get("protocol_violations") == 1
        assert payload.get("protocol_violation_limit") == 1

        lenient = kb.create_task(
            conn, title="lenient", assignee="worker", max_retries=5,
        )
        for i in range(4):
            _drive_protocol_violation(conn, lenient, 992100 + i)
            assert kb.get_task(conn, lenient).status == "ready", (
                f"violation {i + 1}/5 should retry under max_retries=5"
            )
        _drive_protocol_violation(conn, lenient, 992104)
        assert kb.get_task(conn, lenient).status == "blocked"
    finally:
        conn.close()


def test_detect_crashed_workers_nonzero_exit_uses_default_limit(kanban_home):
    """A worker that exited non-zero (real error / crash) uses the
    normal counter path — one failure doesn't trip the breaker.
    """
    import hermes_cli.kanban_db as _kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="crashy", assignee="worker")
        host_prefix = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
        fake_pid = 999997
        kb._set_worker_pid(conn, tid, fake_pid)

        # W_EXITCODE(1, 0) == 256 — WIFEXITED True, WEXITSTATUS == 1.
        _kb._record_worker_exit(fake_pid, 256)
        original_alive = _kb._pid_alive
        _kb._pid_alive = lambda p: False
        try:
            kb.detect_crashed_workers(conn)
        finally:
            _kb._pid_alive = original_alive

        task = kb.get_task(conn, tid)
        assert task.status == "ready", (
            f"single non-zero crash shouldn't auto-block, got {task.status}"
        )
        assert task.consecutive_failures == 1
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "crashed" in kinds
        assert "protocol_violation" not in kinds
    finally:
        conn.close()


def test_reclaim_task_clears_failure_counter(kanban_home, monkeypatch):
    """Operator reclaim wipes the counter so the next retry gets a fresh
    budget."""
    import secrets
    # Bare hex lock is not host-local — stub the canonical confirmed-dead
    # termination shape so the reclaim path under test can proceed.
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker",
        lambda *a, **k: {"terminated": True},
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="stuck", assignee="worker")
        lock = secrets.token_hex(4)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "claim_expires=?, worker_pid=?, consecutive_failures=4, "
                "last_failure_error='prior issue' WHERE id=?",
                (lock, int(time.time()) + 3600, 12345, tid),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, status, claim_lock, "
                "claim_expires, worker_pid, started_at) "
                "VALUES (?, 'running', ?, ?, ?, ?)",
                (tid, lock, int(time.time()) + 3600, 12345, int(time.time())),
            )
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET current_run_id=? WHERE id=?",
                (rid, tid),
            )

        ok = kb.reclaim_task(conn, tid, reason="operator fixed config")
        assert ok

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
        assert task.status == "ready"
    finally:
        conn.close()


def test_dispatch_once_integrates_stale_detection(kanban_home, monkeypatch):
    """dispatch_once with stale_timeout_seconds reclaims stale running tasks."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="stale-dispatch", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, 99999)  # fake PID — avoid killing test

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

        res = kb.dispatch_once(
            conn,
            spawn_fn=lambda tsk, ws: None,
            stale_timeout_seconds=14400,
        )
        assert t in res.stale, "Stale task should appear in result.stale"
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_once_stale_disabled_when_timeout_zero(kanban_home, monkeypatch):
    """dispatch_once with stale_timeout_seconds=0 skips stale detection."""
    # Use os.getpid() so _pid_alive → True, preventing detect_crashed_workers
    # from reclaiming. Only stale detection (disabled via timeout=0) is tested.

    with kb.connect() as conn:
        t = kb.create_task(conn, title="skip-stale", assignee="worker")
        kb.claim_task(conn, t)
        # Claim sets worker_pid to 0 initially. Set it to os.getpid() so the
        # crash detector sees a live PID and skips it.
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

        res = kb.dispatch_once(
            conn,
            spawn_fn=lambda tsk, ws: None,
            stale_timeout_seconds=0,
        )
        assert res.stale == [], "stale_timeout_seconds=0 should disable detection"
        assert kb.get_task(conn, t).status == "running"


def test_cli_create_subscribes_to_home_channel(kanban_home, monkeypatch):
    """`hermes kanban create` auto-subscribes the task to every configured
    home channel so its terminal state reaches the home channel without a
    manual notify-subscribe (and feeds H1 inheritance for decompose children).
    """
    import gateway.config as gwc
    monkeypatch.setattr(
        gwc, "configured_home_channels",
        lambda: [{"platform": "telegram", "chat_id": "home-1", "thread_id": "", "name": "Home"}],
    )
    out = run_slash("create 'ship a feature'")
    tid = _created_id(out)

    with kb.connect_closing() as conn:
        subs = kb.list_notify_subs(conn, tid)
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "home-1"


def test_cli_create_no_notify_home_skips_subscription(kanban_home, monkeypatch):
    """--no-notify-home opts out of the home subscription."""
    import gateway.config as gwc
    monkeypatch.setattr(
        gwc, "configured_home_channels",
        lambda: [{"platform": "telegram", "chat_id": "home-1", "thread_id": "", "name": "Home"}],
    )
    out = run_slash("create 'no ping' --no-notify-home")
    tid = _created_id(out)

    with kb.connect_closing() as conn:
        subs = kb.list_notify_subs(conn, tid)
    assert subs == []


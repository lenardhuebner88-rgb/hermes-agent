"""Kanban core functionality tests: hardening.

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


# -------------------------------------------------------------------------
# Integration hardening (Apr 2026 audit fixes)
# -------------------------------------------------------------------------

def test_archive_of_running_task_closes_run(kanban_home):
    """Archiving a claimed task must close the in-flight run with
    outcome='reclaimed', not orphan it."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
        assert run.ended_at is None
        open_run_id = run.id

        assert kb.archive_task(conn, tid) is True

        task = kb.get_task(conn, tid)
        assert task.status == "archived"
        assert task.current_run_id is None
        # The previously-active run must now be closed.
        closed = kb.get_run(conn, open_run_id)
        assert closed.ended_at is not None
        assert closed.outcome == "reclaimed"
    finally:
        conn.close()


def test_archive_of_ready_task_does_not_create_spurious_run(kanban_home):
    """No active run → archive shouldn't synthesize one."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        # Never claimed. Move to ready (task starts in 'ready' here).
        assert kb.archive_task(conn, tid) is True
        runs = kb.list_runs(conn, tid)
        assert runs == []  # No run was ever opened; archive didn't fabricate one.
    finally:
        conn.close()


def test_dashboard_direct_status_change_off_running_closes_run(kanban_home):
    """Dashboard drag-drop running->ready must close the active run.

    Importing _set_status_direct directly to simulate the PATCH handler
    without spinning up FastAPI.
    """
    from plugins.kanban.dashboard.plugin_api import _set_status_direct

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        open_run = kb.latest_run(conn, tid)
        assert open_run.ended_at is None
        prev_run_id = open_run.id

        # Simulate yanking the worker back to the queue.
        assert _set_status_direct(conn, tid, "ready") is True

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.current_run_id is None
        closed = kb.get_run(conn, prev_run_id)
        assert closed.ended_at is not None
        assert closed.outcome == "reclaimed"
    finally:
        conn.close()


def test_dashboard_direct_status_change_within_same_state_is_noop_for_runs(kanban_home):
    """todo -> ready on an unclaimed task must not create any run rows."""
    from plugins.kanban.dashboard.plugin_api import _set_status_direct

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        # Force to todo for the sake of the test.
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        conn.commit()
        assert _set_status_direct(conn, tid, "ready") is True
        assert kb.list_runs(conn, tid) == []
    finally:
        conn.close()


def test_cli_bulk_complete_with_summary_rejects(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a", assignee="worker")
        b = kb.create_task(conn, title="b", assignee="worker")
        kb.claim_task(conn, a); kb.claim_task(conn, b)
    finally:
        conn.close()
    # Bulk + summary is refused (stderr message, no mutation).
    # Note: hermes_cli.main doesn't propagate sub-command exit codes
    # (args.func(args) discards the return value), so we check the side
    # effects instead.
    from subprocess import run as _run
    import os, sys
    env = os.environ.copy()
    r = _run(
        [sys.executable, "-m", "hermes_cli.main", "kanban",
         "complete", a, b, "--summary", "oops"],
        capture_output=True, text=True, env=env,
    )
    assert "per-task" in r.stderr, r.stderr
    # The tasks must still be running (no partial apply).
    conn = kb.connect()
    try:
        assert kb.get_task(conn, a).status == "running"
        assert kb.get_task(conn, b).status == "running"
    finally:
        conn.close()


def test_cli_bulk_complete_without_summary_still_works(kanban_home):
    """Bulk close with no per-task handoff is allowed — the common case."""
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a", assignee="worker")
        b = kb.create_task(conn, title="b", assignee="worker")
        kb.claim_task(conn, a); kb.claim_task(conn, b)
    finally:
        conn.close()
    out = run_slash(f"complete {a} {b}")
    assert f"Completed {a}" in out
    assert f"Completed {b}" in out


def test_completed_event_payload_carries_summary(kanban_home):
    """The 'completed' event must embed the run summary so gateway
    notifiers render structured handoffs without a second SQL hit."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="handoff line 1\nextra",
                         metadata={"n": 3})
        events = kb.list_events(conn, tid)
        comp = [e for e in events if e.kind == "completed"]
        assert len(comp) == 1
        # First-line-only, within the 400-char cap, preserved verbatim.
        assert comp[0].payload["summary"] == "handoff line 1"
    finally:
        conn.close()


def test_completed_event_payload_summary_none_when_missing(kanban_home):
    """If the caller passes no summary AND no result, payload.summary is None."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid)  # no summary, no result
        events = kb.list_events(conn, tid)
        comp = [e for e in events if e.kind == "completed"][0]
        assert comp.payload.get("summary") is None
    finally:
        conn.close()


# -------------------------------------------------------------------------
# Deep-scan fixes (Apr 2026 second audit)
# -------------------------------------------------------------------------

def test_complete_never_claimed_task_synthesizes_run(kanban_home):
    """complete_task on a ready (never-claimed) task must persist the
    handoff instead of silently dropping summary/metadata."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="skip claim", assignee="worker")
        # Task is in 'ready' state with no run opened.
        assert kb.list_runs(conn, tid) == []
        ok = kb.complete_task(
            conn, tid,
            summary="did it manually",
            metadata={"reason": "human intervention"},
        )
        assert ok is True

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1, f"expected 1 synthetic run, got {len(runs)}"
        r = runs[0]
        assert r.outcome == "completed"
        assert r.summary == "did it manually"
        assert r.metadata == {"reason": "human intervention"}
        # Zero-duration synthetic run.
        assert r.started_at == r.ended_at
        # Task pointer still NULL (we never claimed, never opened a run).
        assert kb.get_task(conn, tid).current_run_id is None

        # Event carries the synthetic run_id.
        evts = [e for e in kb.list_events(conn, tid) if e.kind == "completed"]
        assert len(evts) == 1
        assert evts[0].run_id == r.id
    finally:
        conn.close()


def test_block_never_claimed_task_synthesizes_run(kanban_home):
    """block_task on a ready task must persist --reason on a synthetic run."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="drop this", assignee="worker")
        ok = kb.block_task(conn, tid, reason="deprioritized")
        assert ok is True

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        r = runs[0]
        assert r.outcome == "blocked"
        assert r.summary == "deprioritized"
        assert r.started_at == r.ended_at

        evts = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert evts[0].run_id == r.id
    finally:
        conn.close()


def test_complete_never_claimed_without_handoff_skips_synthesis(kanban_home):
    """If a bulk-complete passes no summary/metadata/result, don't spam
    the runs table with empty synthetic rows."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="simple", assignee="worker")
        ok = kb.complete_task(conn, tid)  # no handoff fields
        assert ok is True
        assert kb.list_runs(conn, tid) == []  # no synthetic row
    finally:
        conn.close()


def test_event_dataclass_carries_run_id(kanban_home):
    """list_events and the Event dataclass must expose run_id so
    downstream consumers (notifier, dashboard) can group by attempt."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="done")

        events = kb.list_events(conn, tid)
        kinds_with_run = {
            e.kind: e.run_id for e in events if e.run_id is not None
        }
        # 'created' should NOT have a run_id (task-scoped).
        created = [e for e in events if e.kind == "created"][0]
        assert created.run_id is None
        # 'claimed' and 'completed' must have run_id.
        assert kinds_with_run.get("claimed") == run_id
        assert kinds_with_run.get("completed") == run_id
    finally:
        conn.close()


def test_unseen_events_for_sub_includes_run_id(kanban_home):
    """Gateway notifier path must also surface run_id on events."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify test", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram",
            chat_id="12345", thread_id="",
        )
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="notify-ready")

        cursor, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram",
            chat_id="12345", thread_id="",
            kinds=("completed",),
        )
        assert len(events) == 1
        assert events[0].run_id == run_id
    finally:
        conn.close()


def test_claim_task_recovers_from_invariant_leak(kanban_home):
    """Belt-and-suspenders: if a prior run somehow leaked (stranded
    current_run_id on a ready task), claim_task should recover rather
    than strand it further."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="invariant test", assignee="worker")
        # Manually engineer the invariant violation: create a run, then
        # flip status back to 'ready' without closing the run.
        kb.claim_task(conn, tid)
        leaked_run_id = kb.latest_run(conn, tid).id
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL "
            "WHERE id = ?", (tid,),
        )
        conn.commit()
        # The leaked run is still open.
        assert kb.get_run(conn, leaked_run_id).ended_at is None

        # Now re-claim — the defensive recovery must close the leak.
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        leaked = kb.get_run(conn, leaked_run_id)
        assert leaked.ended_at is not None
        assert leaked.outcome == "reclaimed"
        # New run opened and pointed to.
        new_run = kb.latest_run(conn, tid)
        assert new_run.id != leaked_run_id
        assert new_run.ended_at is None
    finally:
        conn.close()


# -------------------------------------------------------------------------
# Live-test findings (Apr 2026 third pass: auto-init, show --json carries runs)
# -------------------------------------------------------------------------

def test_cli_create_on_fresh_home_auto_inits(tmp_path, monkeypatch):
    """First CLI action on an empty HERMES_HOME must not error with
    'no such table: tasks' — init_db auto-runs now."""
    home = tmp_path / ".hermes"
    home.mkdir()
    profile = home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Sanity: kanban.db does NOT exist yet.
    import subprocess as _sp
    import sys as _sys
    worktree_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_HOME": str(home),
           "PYTHONPATH": str(worktree_root)}
    r = _sp.run(
        [_sys.executable, "-m", "hermes_cli.main", "kanban",
         "create", "smoke", "--assignee", "worker", "--json"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    import json as _json
    out = _json.loads(r.stdout)
    assert out["status"] == "ready"
    # DB file exists now.
    assert (home / "kanban.db").exists()


def test_connect_auto_inits_fresh_db(tmp_path, monkeypatch):
    """Calling connect() on a fresh HERMES_HOME must create the
    schema. Previously callers had to remember kb.init_db() first."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Flush the module-level cache so this path looks fresh.
    kb._INITIALIZED_PATHS.clear()

    # Direct connect() without init_db() — used to raise "no such table".
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        assert tid is not None
        assert kb.get_task(conn, tid).title == "x"
    finally:
        conn.close()


def test_cli_show_json_carries_runs(kanban_home):
    """hermes kanban show --json must include runs[] so scripts that
    inspect attempt history don't need a separate 'runs' call."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="show test", assignee="worker")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="inspected")
    finally:
        conn.close()

    out = run_slash(f"show {tid} --json")
    import json as _json
    # run_slash returns combined text; find the JSON block.
    # The output IS json, single doc.
    # Strip any leading ansi or surrounding noise.
    try:
        data = _json.loads(out)
    except _json.JSONDecodeError:
        # Some environments may prefix/suffix whitespace.
        data = _json.loads(out.strip())

    assert "runs" in data, f"show --json must include runs[], got keys: {list(data.keys())}"
    assert len(data["runs"]) == 1
    r = data["runs"][0]
    assert r["outcome"] == "completed"
    assert r["summary"] == "inspected"
    # Events also carry run_id field.
    for e in data["events"]:
        assert "run_id" in e


# -------------------------------------------------------------------------
# Pre-merge audit by @erosika (issue #16102 comment 4331125835) — fixes
# -------------------------------------------------------------------------

def test_unblock_invariant_recovery(kanban_home):
    """unblock_task must leave current_run_id NULL even if some other
    code path left it dangling. Engineer the leak, verify recovery."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="unblock invariant", assignee="worker")
        # Start on running, then open a run, then force to 'blocked' but
        # leave current_run_id pointing at the open run — simulate the
        # invariant violation erosika flagged.
        kb.claim_task(conn, tid)
        leaked_run_id = kb.latest_run(conn, tid).id
        # Force the bad state.
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,),
        )
        conn.commit()
        # current_run_id is still set; run is still open.
        assert kb.get_task(conn, tid).current_run_id == leaked_run_id
        assert kb.get_run(conn, leaked_run_id).ended_at is None

        # Unblock — the defensive recovery must close the leaked run.
        assert kb.unblock_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.current_run_id is None
        leaked = kb.get_run(conn, leaked_run_id)
        assert leaked.outcome == "reclaimed"
        assert leaked.ended_at is not None
    finally:
        conn.close()


def test_unblock_normal_path_no_spurious_run(kanban_home):
    """Happy path: claim -> block -> unblock. Unblock must be a no-op
    on runs (block_task already closed the run cleanly)."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="normal unblock", assignee="worker")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="pause")
        runs_before = len(kb.list_runs(conn, tid))
        assert kb.unblock_task(conn, tid) is True
        runs_after = len(kb.list_runs(conn, tid))
        # No new run created by the happy-path unblock.
        assert runs_after == runs_before
        # Task in ready with cleared pointer.
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.current_run_id is None
    finally:
        conn.close()


def test_migration_backfill_idempotent_under_re_run(tmp_path, monkeypatch):
    """init_db must be safe to re-run repeatedly. Each call should leave
    at most one run row per in-flight task, even if called while a
    dispatcher is simultaneously claiming."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Fresh DB, one task left in 'running' with a claim but no run row.
    # Simulates a pre-runs-era DB.
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy inflight", assignee="worker")
        now = int(time.time())
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock='old', "
            "claim_expires=?, started_at=?, current_run_id=NULL WHERE id=?",
            (now + 900, now, tid),
        )
        # Drop any synthetic run the normal claim path would have made.
        conn.execute("DELETE FROM task_runs WHERE task_id=?", (tid,))
        conn.commit()

        # Re-run init_db 3x — each should detect the orphan-inflight and
        # install exactly ONE run row, not three.
        for _ in range(3):
            kb.init_db()

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1, f"expected exactly 1 backfilled run, got {len(runs)}"
        # Pointer should be installed.
        assert kb.get_task(conn, tid).current_run_id == runs[0].id
    finally:
        conn.close()


def test_build_worker_context_includes_role_history(kanban_home):
    """build_worker_context must surface recent completed runs for the
    same assignee, giving cross-task continuity."""
    conn = kb.connect()
    try:
        # Three completed tasks for 'reviewer'
        for i, (title, summary) in enumerate([
            ("Review security PR #1", "approved, focus on CSRF"),
            ("Review security PR #2", "requested changes: SQL injection vector"),
            ("Review security PR #3", "approved, rate-limit added"),
        ]):
            tid = kb.create_task(conn, title=title, assignee="reviewer")
            kb.claim_task(conn, tid)
            kb.complete_task(conn, tid, summary=summary)

        # Now a NEW task for reviewer, not yet done
        new_tid = kb.create_task(
            conn, title="Review perf PR", assignee="reviewer",
        )
        ctx = kb.build_worker_context(conn, new_tid)

        assert "## Recent work by @reviewer" in ctx
        assert "Review security PR #3" in ctx
        assert "approved, rate-limit added" in ctx
        # Current task should be excluded from its own recent work list.
        assert "Review perf PR" not in ctx.split("## Recent work by")[1]
    finally:
        conn.close()


def test_build_worker_context_role_history_skipped_when_no_assignee(kanban_home):
    """If task has no assignee, the role-history section is omitted."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="orphan task")
        # Force no assignee (create_task already defaults to None).
        ctx = kb.build_worker_context(conn, tid)
        assert "## Recent work by" not in ctx
    finally:
        conn.close()


def test_build_worker_context_role_history_bounded_to_5(kanban_home):
    """Role history must be capped at 5 entries even when the assignee
    has many completed tasks."""
    conn = kb.connect()
    try:
        for i in range(10):
            tid = kb.create_task(
                conn, title=f"prior #{i}", assignee="worker",
            )
            kb.claim_task(conn, tid)
            kb.complete_task(conn, tid, summary=f"done #{i}")

        new_tid = kb.create_task(conn, title="new", assignee="worker")
        ctx = kb.build_worker_context(conn, new_tid)
        # Section should exist and contain exactly 5 bullet lines.
        section = ctx.split("## Recent work by @worker")[1]
        bullets = [l for l in section.splitlines() if l.startswith("- ")]
        assert len(bullets) == 5, f"expected 5 bullets, got {len(bullets)}"
    finally:
        conn.close()


# -------------------------------------------------------------------------
# Battle-test findings (May 2026: stress/ suite exposed zombie + id collision)
# -------------------------------------------------------------------------

@pytest.mark.skipif("linux" not in __import__("sys").platform,
                    reason="zombie detection is Linux-specific")
def test_pid_alive_detects_zombie(kanban_home):
    """_pid_alive must return False for a zombie process.

    Without the /proc check, kill(pid, 0) succeeds against zombies
    (process table entry exists until parent reaps), so the dispatcher
    would treat a dead-but-unreaped worker as alive. This catches a
    worker that exited normally but whose parent hasn't called wait().
    """
    import subprocess as _sp
    proc = _sp.Popen(
        ["sleep", "3600"],
        stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    pid = proc.pid
    try:
        assert kb._pid_alive(pid) is True  # live non-zombie
        os.kill(pid, 9)
        time.sleep(0.3)
        # Verify /proc reports zombie state so the test is actually
        # exercising the zombie path and not some other liveness failure
        with open(f"/proc/{pid}/status") as f:
            state_line = next(
                (l for l in f if l.startswith("State:")), ""
            )
        assert "Z" in state_line, f"expected zombie, got {state_line!r}"
        # And _pid_alive must see through it.
        assert kb._pid_alive(pid) is False
    finally:
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def test_task_ids_dont_collide_at_scale(kanban_home):
    """ID generator must be wide enough that creating 10k tasks doesn't
    hit a UNIQUE constraint violation.

    Regression test for the 2-hex-byte ID (65k space) that would
    collide at ~50% probability by 10k tasks due to birthday paradox.
    Current generator uses 4 hex bytes (4.3B space).
    """
    conn = kb.connect()
    try:
        # 500 is enough to exercise the generator diversity without
        # making the test slow. At 2-hex-byte width, collision chance
        # over 500 creates was ~1.3%; over 10000 the old generator
        # would fail reliably. We don't need the full 10k run to prove
        # the regression; distribution check is sufficient.
        ids = [kb.create_task(conn, title=f"scale-{i}") for i in range(500)]
        assert len(ids) == len(set(ids)), "ID collision at N=500"
        # Sanity: every id matches the expected format
        for tid in ids[:10]:
            assert tid.startswith("t_")
            assert len(tid) == 10  # "t_" + 8 hex chars
    finally:
        conn.close()


def test_cli_show_clamps_negative_elapsed(kanban_home):
    """When NTP jumps backward between claim and complete, started_at
    can exceed ended_at. CLI display must clamp to 0, not print '-3600s'.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="time-skewed", assignee="worker")
        kb.claim_task(conn, tid)
        # Force a future started_at via raw SQL — simulates NTP jump.
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE task_id = ?",
            (future, tid),
        )
        conn.commit()
        # Complete normally (ended_at < started_at now)
        kb.complete_task(conn, tid, summary="after skew")
    finally:
        conn.close()

    # Both `show` and `runs` render this. Neither should display a
    # negative elapsed token. We check specifically for the pattern
    # `-<digits>s` (the elapsed column) rather than any minus sign,
    # since timestamps legitimately contain dashes (2026-04-28).
    out_show = run_slash(f"show {tid}")
    out_runs = run_slash(f"runs {tid}")
    import re as _re
    neg_elapsed = _re.compile(r"-\d+s")
    assert not neg_elapsed.search(out_show), (
        f"show output has negative elapsed: {out_show!r}"
    )
    assert not neg_elapsed.search(out_runs), (
        f"runs output has negative elapsed: {out_runs!r}"
    )
    # Should show "0s" for the clamped elapsed
    assert "0s" in out_show or "0s" in out_runs


def test_resolve_workspace_rejects_relative_dir_path(kanban_home):
    """dir: workspace_path must be absolute. A relative path like
    '../../../tmp/attacker' would be resolved against the dispatcher's
    CWD — a confused-deputy escape vector."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="path-trav", assignee="worker",
            workspace_kind="dir",
            workspace_path="../../../tmp/attacker",
        )
        task = kb.get_task(conn, tid)
        # Storage is verbatim — that's fine.
        assert task.workspace_path == "../../../tmp/attacker"
        # But resolution must refuse.
        with pytest.raises(ValueError, match=r"non-absolute"):
            kb.resolve_workspace(task)
    finally:
        conn.close()


def test_resolve_workspace_accepts_absolute_dir_path(kanban_home, tmp_path):
    """Legitimate absolute paths are accepted and created."""
    conn = kb.connect()
    try:
        abs_path = str(tmp_path / "my-workspace")
        tid = kb.create_task(
            conn, title="legit", assignee="worker",
            workspace_kind="dir",
            workspace_path=abs_path,
        )
        task = kb.get_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        assert str(resolved) == abs_path
        assert resolved.exists()
    finally:
        conn.close()


def test_resolve_workspace_rejects_relative_worktree_path(kanban_home):
    """Worktree paths also must be absolute when explicitly set."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="wt", assignee="worker",
            workspace_kind="worktree",
            workspace_path="../escape",
        )
        with pytest.raises(ValueError, match=r"non-absolute"):
            kb.resolve_workspace(kb.get_task(conn, tid))
    finally:
        conn.close()


def test_build_worker_context_caps_prior_attempts(kanban_home):
    """When a task has more than _CTX_MAX_PRIOR_ATTEMPTS runs, only
    the most recent N are shown in full; earlier attempts are summarised
    in a one-line marker so the worker knows more exist without
    blowing the prompt."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry", assignee="worker")
        # Force 25 closed runs
        for i in range(25):
            kb.claim_task(conn, tid)
            kb._end_run(conn, tid, outcome="reclaimed",
                        summary=f"attempt {i} summary")
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL WHERE id=?", (tid,),
            )
            conn.commit()

        ctx = kb.build_worker_context(conn, tid)
        # Check: only _CTX_MAX_PRIOR_ATTEMPTS attempt headers present
        attempt_count = ctx.count("### Attempt ")
        assert attempt_count == kb._CTX_MAX_PRIOR_ATTEMPTS, (
            f"expected {kb._CTX_MAX_PRIOR_ATTEMPTS} attempts shown, got {attempt_count}"
        )
        # And the "omitted" marker appears with the right count
        omitted_count = 25 - kb._CTX_MAX_PRIOR_ATTEMPTS
        assert f"{omitted_count} earlier attempt" in ctx, (
            f"expected omitted-count marker, got ctx=\n{ctx[:2000]}"
        )
        # Total size is bounded — empirically we expect << 100KB even
        # for 1000 attempts (capped to N * ~500 chars)
        assert len(ctx) < 20_000, (
            f"context should be bounded even at 25 runs, got {len(ctx)} chars"
        )
        # Attempt numbering starts at the real index (not renumbered)
        assert "Attempt 16 " in ctx, (
            "first-shown attempt should be numbered 16 (25 - 10 + 1)"
        )
    finally:
        conn.close()


def test_build_worker_context_renders_author_with_safe_framing(kanban_home):
    """Author rendering wraps the operator-controlled author in code fences
    + "comment from worker" prefix so a misleading HERMES_PROFILE name
    (e.g. "hermes-system", "operator") can't be misread as a system
    directive above the comment body. Defense-in-depth — see #22452."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.add_comment(conn, tid, author="hermes-system", body="some note")
        ctx = kb.build_worker_context(conn, tid)

        # No bold-author rendering anywhere in the context.
        assert "**hermes-system**" not in ctx
        # Explicit provenance prefix is present.
        assert "comment from worker `hermes-system` at " in ctx
        # The body still renders.
        assert "some note" in ctx
    finally:
        conn.close()


def test_build_worker_context_caps_comments(kanban_home):
    """Same cap for comments — comment-storm tasks stay bounded."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="chatty", assignee="worker")
        for i in range(100):
            kb.add_comment(conn, tid, author=f"u{i % 3}", body=f"comment {i}")
        ctx = kb.build_worker_context(conn, tid)
        # Only _CTX_MAX_COMMENTS most-recent shown in full
        # Count by body text since author rendering uses code-fenced
        # "comment from worker `<author>` at <ts>:" framing (#22452).
        # Comment bodies are "comment 0".."comment 99" so we need to
        # match the body specifically (digit suffix), not the author
        # provenance line (which also starts with "comment ").
        import re
        body_count = sum(
            1 for line in ctx.splitlines() if re.fullmatch(r"comment \d+", line)
        )
        assert body_count == kb._CTX_MAX_COMMENTS, (
            f"expected {kb._CTX_MAX_COMMENTS} comments shown, got {body_count}"
        )
        omitted = 100 - kb._CTX_MAX_COMMENTS
        assert f"{omitted} earlier comment" in ctx
    finally:
        conn.close()


def test_build_worker_context_caps_huge_summary(kanban_home):
    """A 1 MB summary on a single prior run must not dominate the
    worker prompt. Per-field cap truncates with a visible ellipsis."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="giant", assignee="worker")
        kb.claim_task(conn, tid)
        huge = "X" * (1024 * 1024)  # 1 MB
        kb._end_run(conn, tid, outcome="reclaimed", summary=huge)
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL WHERE id=?", (tid,),
        )
        conn.commit()

        ctx = kb.build_worker_context(conn, tid)
        # Much smaller than 1 MB
        assert len(ctx) < 10_000, (
            f"1 MB summary should be capped, got {len(ctx)} chars"
        )
        # Truncation marker present
        assert "truncated" in ctx
    finally:
        conn.close()


def test_default_spawn_does_not_auto_load_any_skill(kanban_home, monkeypatch):
    """The dispatcher no longer auto-loads a bundled kanban skill.

    The kanban lifecycle (formerly the kanban-worker/kanban-orchestrator
    skills) is now injected into every worker's system prompt via
    KANBAN_GUIDANCE, so _default_spawn must NOT append a `--skills` flag
    when the task carries no per-task skills.

    We intercept Popen to capture the argv without actually spawning a
    hermes subprocess (which would hang trying to call an LLM).
    """
    captured = {}

    class FakeProc:
        def __init__(self):
            self.pid = 99999

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="skill-loading test",
                             assignee="some-profile")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        pid = kb._default_spawn(task, str(workspace))
        assert pid == 99999
    finally:
        conn.close()

    cmd = captured["cmd"]
    assert "--skills" not in cmd, (
        f"spawn argv should not auto-load any skill: {cmd}"
    )
    assert "--accept-hooks" in cmd, f"spawn argv missing --accept-hooks: {cmd}"
    assert cmd.index("--accept-hooks") < cmd.index("chat"), (
        f"--accept-hooks must come before 'chat' in argv: {cmd}"
    )
    # Assignee + task env are still present
    assert "some-profile" in cmd
    env = captured["env"]
    assert env.get("HERMES_KANBAN_TASK") == tid
    assert env.get("HERMES_PROFILE") == "some-profile"


def test_default_spawn_raises_terminal_timeout_to_task_runtime(kanban_home, monkeypatch):
    """A task runtime cap should raise the worker's terminal default.

    This is worker-scoped env only: normal CLI/gateway terminal settings stay
    untouched, but long kanban tasks no longer inherit a short generic
    TERMINAL_TIMEOUT that kills their foreground command first.
    """
    captured = {}

    class FakeProc:
        pid = 123

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    monkeypatch.delenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", raising=False)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="long worker",
            assignee="ops",
            max_runtime_seconds=3600,
        )
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb._default_spawn(task, str(workspace))
    finally:
        conn.close()

    assert captured["env"]["TERMINAL_TIMEOUT"] == "3570"
    assert captured["env"]["TERMINAL_MAX_FOREGROUND_TIMEOUT"] == "3570"
    assert os.environ["TERMINAL_TIMEOUT"] == "180"


def test_default_spawn_preserves_longer_terminal_timeout(kanban_home, monkeypatch):
    """Kanban should never lower an explicitly larger terminal timeout."""
    captured = {}

    class FakeProc:
        pid = 124

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "7200")
    monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "7200")

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="already tuned",
            assignee="ops",
            max_runtime_seconds=3600,
        )
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb._default_spawn(task, str(workspace))
    finally:
        conn.close()

    assert captured["env"]["TERMINAL_TIMEOUT"] == "7200"
    assert captured["env"]["TERMINAL_MAX_FOREGROUND_TIMEOUT"] == "7200"


def test_default_spawn_leaves_terminal_timeout_without_runtime_cap(kanban_home, monkeypatch):
    """Uncapped tasks keep the existing terminal timeout behavior."""
    captured = {}

    class FakeProc:
        pid = 125

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    monkeypatch.delenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", raising=False)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="uncapped", assignee="ops")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb._default_spawn(task, str(workspace))
    finally:
        conn.close()

    assert captured["env"]["TERMINAL_TIMEOUT"] == "180"
    assert "TERMINAL_MAX_FOREGROUND_TIMEOUT" not in captured["env"]


def test_build_worker_context_includes_runtime_timeout_budget(kanban_home, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="long context",
            assignee="ops",
            max_runtime_seconds=3600,
        )
        ctx = kb.build_worker_context(conn, tid)
    finally:
        conn.close()

    assert "Max runtime: 3600s" in ctx
    assert "Terminal timeout: 3570s" in ctx


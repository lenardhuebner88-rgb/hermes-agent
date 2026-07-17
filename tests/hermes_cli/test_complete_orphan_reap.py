"""Tests: manual ``complete_task`` reaps the orphaned worker process
(fl-20260704-complete-orphan-reap).

``complete_task`` used to only clear ``tasks.worker_pid`` on completion —
it never signalled the process itself. A manual completion (operator
``hermes kanban complete <id>`` racing a dispatcher claim, see
t_aa55b33c) could therefore close a task while its spawned worker kept
running, burning tokens/API calls against an already-closed task.

The fix reuses the existing SIGTERM->grace->SIGKILL reclaim helper
(``_terminate_reclaimed_worker``) rather than inventing new kill logic,
and only fires on the MANUAL completion path (no ``expected_run_id`` —
the CLI/operator path). A worker's own self-completion (which always
passes its own run id) must never be signalled here — it could still be
mid its own exit protocol (e.g. a review-gate submit).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _worker_reaped_events(conn, task_id):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [
        json.loads(r["payload"]) if r["payload"] else {}
        for r in rows
        if r["kind"] == "worker_reaped"
    ]


def test_manual_complete_reaps_live_worker(conn):
    """(1) Manual complete_task (no expected_run_id) on a task with a live
    ``worker_pid`` terminates the process within the grace window and
    records a ``worker_reaped`` event carrying the pid."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="orphan", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        kb._set_worker_pid(conn, tid, proc.pid)
        run_row = conn.execute(
            "SELECT current_run_id, worker_pid FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert run_row["current_run_id"] is not None
        assert run_row["worker_pid"] == proc.pid

        ok = kb.complete_task(conn, tid, result="done manually")
        assert ok is True

        assert (
            proc.poll() is not None
        ), "orphaned worker process still running after manual complete_task"

        reaped = _worker_reaped_events(conn, tid)
        assert len(reaped) == 1
        assert reaped[0]["pid"] == proc.pid
        assert reaped[0]["terminated"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_worker_self_completion_does_not_signal(conn):
    """(2) A worker completing its OWN run (expected_run_id set, the
    kanban_complete tool path) must never be signalled — the process
    lives on; the test tears it down itself."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="self-complete", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    proc = subprocess.Popen(["sleep", "300"])
    try:
        kb._set_worker_pid(conn, tid, proc.pid)
        run_id = kb._current_run_id(conn, tid)
        assert run_id is not None

        ok = kb.complete_task(
            conn,
            tid,
            result="done via tool",
            expected_run_id=run_id,
            review_gate=True,
        )
        assert ok is True

        assert (
            proc.poll() is None
        ), "worker self-completion must not signal its own process"
        assert _worker_reaped_events(conn, tid) == []
    finally:
        proc.terminate()
        proc.wait()


def test_manual_complete_dead_pid_records_not_alive(conn):
    """(3) A manual complete_task whose worker_pid is already gone must
    run through error-free and record a not-alive marker instead of
    raising."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="dead-pid", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)

    ok = kb.complete_task(conn, tid, result="done manually")
    assert ok is True

    reaped = _worker_reaped_events(conn, tid)
    assert len(reaped) == 1
    assert reaped[0]["pid"] == dead.pid
    assert reaped[0].get("already_exited") is True

"""Claim hygiene regressions: a breaker trip racing a re-claim must not
strand a live claim on a blocked task, and every blocked→ready recovery
path must clear the claim columns.

Background: ``enforce_max_runtime`` / ``detect_crashed_workers`` release
the claim (running → ready) in their own transaction and call
``_record_task_failure(release_claim=False)`` in a SEPARATE one. In the
gap the dispatcher can legitimately re-claim the task. The breaker flip
then matched ``status IN ('ready','running')`` with no claim CAS —
flipping the re-claimed row to 'blocked' while its fresh ``claim_lock``
survived. No recovery path cleared the claim columns, and both
``claim_task`` and the dispatcher's ready-candidate query require
``claim_lock IS NULL`` — the task became permanently undispatchable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    live_db = Path("/home/piet/.hermes/kanban.db").resolve()
    assert db_path.resolve() != live_db
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _claim_columns(conn, tid):
    row = conn.execute(
        "SELECT status, claim_lock, claim_expires, worker_pid "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()
    return row


def test_breaker_flip_skipped_when_task_was_reclaimed(kanban_home):
    """The timeout/crash breaker path must not blindside a task that a
    fresh claim raced onto — pre-fix it flipped the running row to
    'blocked' with the new claim still set."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="race-victim", assignee="a")
        claimed = kb.claim_task(conn, tid, claimer="host:w1")
        assert claimed is not None  # task now running with a live claim

        tripped = kb._record_task_failure(
            conn,
            tid,
            "elapsed 999s > limit 100s",
            outcome="timed_out",
            failure_limit=1,  # would trip on this failure
            release_claim=False,
            end_run=False,
        )

        assert tripped is False
        row = _claim_columns(conn, tid)
        # The re-claimed run is untouched: still running, claim intact.
        assert row["status"] == "running"
        assert row["claim_lock"] is not None
        # The failure was still counted at task level.
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1


def test_breaker_flip_applies_on_cleanly_released_task(kanban_home):
    """Normal (no-race) path: the caller already released the claim; the
    breaker flip to 'blocked' must still work."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="timeout-victim", assignee="a")
        claimed = kb.claim_task(conn, tid, claimer="host:w1")
        assert claimed is not None
        # Simulate the caller's own release txn (running → ready, claim cleared).
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (tid,),
        )
        conn.commit()

        tripped = kb._record_task_failure(
            conn,
            tid,
            "elapsed 999s > limit 100s",
            outcome="timed_out",
            failure_limit=1,
            release_claim=False,
            end_run=False,
        )

        assert tripped is True
        row = _claim_columns(conn, tid)
        assert row["status"] == "blocked"
        assert row["claim_lock"] is None


def test_stale_failure_not_written_after_task_completed(kanban_home):
    """The rowcount-0 fallback must not write stale failure state onto a
    task whose raced re-claim already COMPLETED — complete_task
    deliberately reset the counters; that old failure is history."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already-done", assignee="a")
        claimed = kb.claim_task(conn, tid, claimer="host:w1")
        assert claimed is not None
        assert kb.complete_task(
            conn, tid, summary="done", expected_run_id=claimed.current_run_id
        )

        tripped = kb._record_task_failure(
            conn,
            tid,
            "elapsed 999s > limit 100s",
            outcome="timed_out",
            failure_limit=1,
            release_claim=False,
            end_run=False,
        )

        assert tripped is False
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def _strand_claim_on_blocked(conn, tid):
    conn.execute(
        "UPDATE tasks SET status = 'blocked', claim_lock = 'stale:claimer', "
        "claim_expires = 9999999999, worker_pid = 4242 WHERE id = ?",
        (tid,),
    )
    conn.commit()


def test_unblock_clears_stranded_claim(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stranded", assignee="a")
        _strand_claim_on_blocked(conn, tid)

        assert kb.unblock_task(conn, tid) is True
        row = _claim_columns(conn, tid)
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None
        assert row["worker_pid"] is None
        # And the task is actually claimable again.
        assert kb.claim_task(conn, tid, claimer="host:w2") is not None


def test_promote_clears_stranded_claim(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stranded-promote", assignee="a")
        _strand_claim_on_blocked(conn, tid)

        ok, err = kb.promote_task(conn, tid, actor="test")
        assert ok is True, err
        row = _claim_columns(conn, tid)
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        assert kb.claim_task(conn, tid, claimer="host:w2") is not None

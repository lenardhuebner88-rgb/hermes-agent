"""Regression tests for dispatcher recovery when a worker PID disappears."""

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


def test_unknown_dead_pid_uses_bounded_transient_recovery(
    kanban_home, monkeypatch
):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        task_ids = []
        for i in range(4):
            tid = kb.create_task(conn, title=f"dead-pid-{i}", assignee="a")
            claimed = kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            assert claimed is not None
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (91000 + i, tid),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (91000 + i, claimed.current_run_id),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        tasks = [kb.get_task(conn, tid) for tid in task_ids]
        outcomes = {
            r["task_id"]: r["outcome"]
            for r in conn.execute(
                "SELECT task_id, outcome FROM task_runs WHERE task_id IN "
                f"({','.join('?' for _ in task_ids)})",
                task_ids,
            )
        }
        event_kinds = {
            r["task_id"]: r["kind"]
            for r in conn.execute(
                "SELECT task_id, kind FROM task_events WHERE task_id IN "
                f"({','.join('?' for _ in task_ids)}) "
                "AND kind IN ('crashed', 'transient_retry')",
                task_ids,
            )
        }

    assert crashed == []
    assert {t.status for t in tasks if t is not None} == {"ready"}
    assert {t.transient_retry_count for t in tasks if t is not None} == {1}
    assert set(outcomes.values()) == {kb.TRANSIENT_RETRY_OUTCOME}
    assert set(event_kinds.values()) == {kb.TRANSIENT_RETRY_EVENT}


def test_nonzero_dead_pid_still_counts_as_real_crash(
    kanban_home, monkeypatch
):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="real-crash", assignee="a")

        for i in range(2):
            claimed = kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            assert claimed is not None
            pid = 92000 + i
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (pid, tid),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (pid, claimed.current_run_id),
            )
            conn.commit()
            _kb._record_worker_exit(pid, 1 << 8)
            crashed = kb.detect_crashed_workers(conn)
            assert tid in crashed

        task = kb.get_task(conn, tid)
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id",
                (tid,),
            )
        ]

    assert task is not None
    assert task.status == "blocked"
    assert task.consecutive_failures == 2
    assert outcomes == ["crashed", "crashed"]

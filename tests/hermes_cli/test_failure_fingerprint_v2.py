"""Failure-fingerprint metadata stamping and same_cause_count escalation.

These tests verify the schema-free metadata approach:
``task_runs.metadata`` carries ``worker_exit_kind`` and
``worker_failure_fingerprint`` for failure outcomes, and operator escalations
surface ``evidence.same_cause_count``.
"""

from __future__ import annotations

import json
import os
import time
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


def _run_rows(conn, task_id):
    rows = conn.execute(
        "SELECT id, outcome, metadata FROM task_runs WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "outcome": r["outcome"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
        }
        for r in rows
    ]


def _latest_operator_escalation(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kb.OPERATOR_ESCALATION_EVENT),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    return json.loads(row["payload"])


def test_crashed_run_stamps_failure_metadata(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="crash-meta", assignee="a")
        claimed = kb.claim_task(conn, tid, claimer=f"{host}:w1")
        assert claimed is not None
        pid = 93001
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (pid, claimed.current_run_id),
        )
        conn.commit()
        _kb._record_worker_exit(pid, 1 << 8)
        kb.detect_crashed_workers(conn)

        rows = _run_rows(conn, tid)

    assert len(rows) == 1
    assert rows[0]["outcome"] == "crashed"
    assert rows[0]["metadata"].get("worker_exit_kind") == "crashed"
    fp = rows[0]["metadata"].get("worker_failure_fingerprint")
    assert fp is not None and isinstance(fp, str) and fp != ""
    assert "93001" not in fp  # PID-normalised


def test_fingerprint_is_pid_agnostic_but_cause_distinct(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(_kb, "DEFAULT_FAILURE_LIMIT", 10)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="fingerprint-compare", assignee="a")

        # Two crashes with the same root cause but different PIDs.
        fingerprints = []
        for i, pid in enumerate((94001, 94002)):
            claimed = kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            assert claimed is not None
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (pid, claimed.current_run_id),
            )
            conn.commit()
            _kb._record_worker_exit(pid, 1 << 8)
            kb.detect_crashed_workers(conn)
            rows = _run_rows(conn, tid)
            fingerprints.append(rows[-1]["metadata"]["worker_failure_fingerprint"])

        # A third crash with a genuinely different error text.
        claimed = kb.claim_task(conn, tid, claimer=f"{host}:w2")
        assert claimed is not None
        pid = 94003
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (pid, claimed.current_run_id),
        )
        conn.commit()
        _kb._record_worker_exit(pid, 9 << 8)  # killed by signal 9
        kb.detect_crashed_workers(conn)
        rows = _run_rows(conn, tid)
        different_fp = rows[-1]["metadata"]["worker_failure_fingerprint"]

    assert fingerprints[0] == fingerprints[1]
    assert "94001" not in fingerprints[0]
    assert "94002" not in fingerprints[0]
    assert different_fp != fingerprints[0]


def test_same_cause_count_in_escalation_after_two_crashes(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(_kb, "DEFAULT_FAILURE_LIMIT", 2)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="same-cause-escalation", assignee="a")

        for i, pid in enumerate((95001, 95002)):
            claimed = kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            assert claimed is not None
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (pid, claimed.current_run_id),
            )
            conn.commit()
            _kb._record_worker_exit(pid, 1 << 8)
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        escalation = _latest_operator_escalation(conn, tid)

    assert task is not None
    assert task.status == "blocked"
    assert escalation is not None
    assert escalation["evidence"]["same_cause_count"] == 2


def test_timed_out_run_stamps_failure_metadata(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    state = {"alive": True}
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: state["alive"])
    monkeypatch.setattr(_kb, "DEFAULT_FAILURE_LIMIT", 10)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(
            conn,
            title="timeout-meta",
            assignee="a",
            max_runtime_seconds=60,
        )
        claimed = kb.claim_task(conn, tid, claimer=f"{host}:w1")
        assert claimed is not None
        pid = 96001
        run_id = claimed.current_run_id
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, started_at = ? WHERE id = ?",
            (pid, int(time.time()) - 120, run_id),
        )
        conn.commit()

        def _signal(_pid, _sig):
            state["alive"] = False

        kb.enforce_max_runtime(conn, signal_fn=_signal)

        rows = _run_rows(conn, tid)

    assert len(rows) == 1
    assert rows[0]["outcome"] == "timed_out"
    assert rows[0]["metadata"].get("worker_exit_kind") == "timed_out"
    fp = rows[0]["metadata"].get("worker_failure_fingerprint")
    assert fp is not None and isinstance(fp, str) and fp != ""
    assert "96001" not in fp  # PID-normalised


def test_completed_run_has_no_failure_fingerprint(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="complete-no-fp", assignee="a")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        kb.complete_task(conn, tid, summary="done")

        rows = _run_rows(conn, tid)

    assert len(rows) == 1
    assert rows[0]["outcome"] == "completed"
    assert "worker_exit_kind" not in rows[0]["metadata"]
    assert "worker_failure_fingerprint" not in rows[0]["metadata"]

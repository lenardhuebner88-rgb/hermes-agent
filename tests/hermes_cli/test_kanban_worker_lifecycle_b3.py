from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exit_status(code: int) -> int:
    return int(code) << 8


def _claim_with_pid(conn, *, pid: int = 43210) -> tuple[str, int]:
    tid = kb.create_task(conn, title="worker lifecycle", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    kb._set_worker_pid(conn, tid, pid)
    run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    return tid, int(run_id)


def _run_row(conn, run_id: int):
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def _parse_kanban(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    return parser.parse_args(["kanban", *argv])


def test_worker_exit_columns_exist_on_fresh_schema(kanban_home):
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
    assert {
        "worker_exit_kind",
        "worker_exit_code",
        "worker_protocol_state",
        "worker_failure_fingerprint",
    } <= cols


def test_claim_task_initializes_worker_exit_fields_as_pending(kanban_home):
    with kb.connect() as conn:
        _tid, run_id = _claim_with_pid(conn)
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "pending"
    assert row["worker_protocol_state"] == "pending"
    assert row["worker_exit_code"] is None
    assert row["worker_failure_fingerprint"] is None


def test_claim_review_task_initializes_worker_exit_fields_as_pending(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review worker", assignee="reviewer")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None
        run_id = kb._current_run_id(conn, tid)
        row = _run_row(conn, int(run_id))
    assert row["worker_exit_kind"] == "pending"
    assert row["worker_protocol_state"] == "pending"


def test_worker_exit_migration_file_is_single_transaction():
    sql = Path("hermes_cli/migrations/2026_05_26_worker_exit_taxonomy.sql").read_text(
        encoding="utf-8"
    )
    assert sql.count("BEGIN;") == 1
    assert sql.count("COMMIT;") == 1
    assert sql.index("BEGIN;") < sql.index("ALTER TABLE task_runs")
    assert sql.rindex("ALTER TABLE task_runs") < sql.index("COMMIT;")


def test_worker_exit_migration_applies_cleanly_to_legacy_copy(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL
        );
        """
    )
    sql = Path("hermes_cli/migrations/2026_05_26_worker_exit_taxonomy.sql").read_text(
        encoding="utf-8"
    )
    conn.executescript(sql)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
    conn.close()
    assert {
        "worker_exit_kind",
        "worker_exit_code",
        "worker_protocol_state",
        "worker_failure_fingerprint",
    } <= cols


def test_record_worker_exit_clean_complete_persists_complete_state(kanban_home):
    pid = 50101
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=pid)
        assert kb.complete_task(conn, tid, result="done")
        kb._record_worker_exit(pid, _exit_status(0), conn)
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "clean_exit_complete"
    assert row["worker_exit_code"] == 0
    assert row["worker_protocol_state"] == "complete_emitted"
    assert row["worker_failure_fingerprint"] is None


def test_record_worker_exit_clean_running_persists_protocol_violation(kanban_home):
    pid = 50102
    with kb.connect() as conn:
        _tid, run_id = _claim_with_pid(conn, pid=pid)
        kb._record_worker_exit(pid, _exit_status(0), conn)
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "clean_exit_protocol_violation"
    assert row["worker_exit_code"] == 0
    assert row["worker_protocol_state"] == "silent"
    assert "kanban_complete" in row["worker_failure_fingerprint"]


def test_record_worker_exit_nonzero_persists_code_and_fingerprint(kanban_home):
    pid = 50103
    with kb.connect() as conn:
        _tid, run_id = _claim_with_pid(conn, pid=pid)
        kb._record_worker_exit(pid, _exit_status(7), conn)
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "nonzero_exit"
    assert row["worker_exit_code"] == 7
    assert row["worker_failure_fingerprint"] == "pid n exited with code 7"


def test_record_worker_exit_signaled_persists_signal_code(kanban_home):
    pid = 50104
    with kb.connect() as conn:
        _tid, run_id = _claim_with_pid(conn, pid=pid)
        kb._record_worker_exit(pid, 9, conn)
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "signaled"
    assert row["worker_exit_code"] == 9
    assert row["worker_failure_fingerprint"] == "pid n killed by signal 9"


def test_detect_crashed_workers_uses_persisted_protocol_violation(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=50105)
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_runs
                   SET worker_exit_kind = 'clean_exit_protocol_violation',
                       worker_exit_code = 0,
                       worker_protocol_state = 'silent'
                 WHERE id = ?
                """,
                (run_id,),
            )
        assert kb.detect_crashed_workers(conn) == [tid]
        events = [e.kind for e in kb.list_events(conn, tid)]
        task = kb.get_task(conn, tid)
    assert "protocol_violation" in events
    assert task.status == "blocked"


def test_detect_crashed_workers_falls_back_and_persists_null_clean_exit(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=50106)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET worker_exit_kind = NULL, "
                "worker_protocol_state = NULL WHERE id = ?",
                (run_id,),
            )
        assert kb.detect_crashed_workers(conn) == [tid]
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "clean_exit_protocol_violation"
    assert row["worker_protocol_state"] == "silent"


def test_detect_crashed_workers_falls_back_to_pid_not_alive(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("unknown", None))
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=50107)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET worker_exit_kind = NULL WHERE id = ?",
                (run_id,),
            )
        assert kb.detect_crashed_workers(conn) == [tid]
        row = _run_row(conn, run_id)
    assert row["worker_exit_kind"] == "pid_not_alive"
    assert row["worker_failure_fingerprint"] == "pid n not alive"


def test_worker_exit_diagnostics_cli_lists_counts(kanban_home, capsys):
    now = int(time.time())
    with kb.connect() as conn, kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, started_at, ended_at,
                worker_exit_kind, worker_exit_code,
                worker_protocol_state, worker_failure_fingerprint
            ) VALUES ('t_cli', 'crashed', ?, ?, 'nonzero_exit', 2, 'silent', 'boom')
            """,
            (now, now),
        )
    rc = kanban_cli.kanban_command(
        _parse_kanban(["diagnostics", "worker-exits", "--since", "7d"])
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "nonzero_exit" in out
    assert "boom" in out


def test_worker_exit_diagnostics_cli_json(kanban_home, capsys):
    now = int(time.time())
    with kb.connect() as conn, kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, started_at, ended_at,
                worker_exit_kind, worker_protocol_state
            ) VALUES ('t_json', 'running', ?, ?, 'pending', 'pending')
            """,
            (now, now),
        )
    rc = kanban_cli.kanban_command(
        _parse_kanban(["diagnostics", "worker-exits", "--since", "1d", "--json"])
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["since_seconds"] == 86400
    assert data["worker_exits"][0]["worker_exit_kind"] == "pending"


def test_parse_since_seconds_accepts_expected_units():
    assert kanban_cli._parse_since_seconds("30m") == 1800
    assert kanban_cli._parse_since_seconds("12h") == 43200
    assert kanban_cli._parse_since_seconds("7d") == 604800
    assert kanban_cli._parse_since_seconds("2w") == 1209600


def test_diagnostics_cli_handles_legacy_null_exit_fields(kanban_home, capsys):
    now = int(time.time())
    with kb.connect() as conn, kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at) "
            "VALUES ('t_legacy', 'crashed', ?, ?)",
            (now, now),
        )
    rc = kanban_cli.kanban_command(
        _parse_kanban(["diagnostics", "worker-exits", "--since", "1d"])
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out


def test_detect_stale_recent_heartbeat_emits_live_long_op(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    now = int(time.time())
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=os.getpid())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now - 500, now - 10, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now - 500, now - 10, run_id),
            )
        assert kb.detect_stale_running(conn, stale_timeout_seconds=60) == []
        events = [e.kind for e in kb.list_events(conn, tid)]
    assert "live_long_op" in events


def test_detect_stale_live_long_op_is_idempotent_per_run(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    now = int(time.time())
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=os.getpid())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now - 500, now - 10, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now - 500, now - 10, run_id),
            )
        kb.detect_stale_running(conn, stale_timeout_seconds=60)
        kb.detect_stale_running(conn, stale_timeout_seconds=60)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "live_long_op"]
    assert len(events) == 1


def test_detect_stale_old_heartbeat_reclaims_after_two_heartbeat_windows(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    now = int(time.time())
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=50108)
        old_hb = now - (2 * kb.WORKER_HEARTBEAT_SEC) - 5
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now - 500, old_hb, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now - 500, old_hb, run_id),
            )
        stale = kb.detect_stale_running(conn, stale_timeout_seconds=60)
    assert stale == [tid]


def test_worker_heartbeat_loop_emits_three_nonblocking_heartbeats():
    calls: list[str] = []
    stop_event = threading.Event()

    def heartbeat_fn(**kwargs):
        calls.append(kwargs["task_id"])
        if len(calls) >= 3:
            stop_event.set()
        return True

    kb._worker_heartbeat_loop(
        task_id="t_loop",
        run_id=1,
        claim_lock="host:lock",
        board="default",
        worker_pid=123,
        interval_seconds=0.001,
        stop_event=stop_event,
        heartbeat_fn=heartbeat_fn,
        pid_alive_fn=lambda _pid: True,
    )
    assert calls == ["t_loop", "t_loop", "t_loop"]


def test_worker_heartbeat_loop_keeps_running_after_emit_failure():
    calls = 0
    stop_event = threading.Event()

    def heartbeat_fn(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("heartbeat write failed")
        stop_event.set()
        return True

    kb._worker_heartbeat_loop(
        task_id="t_loop",
        run_id=1,
        claim_lock="host:lock",
        board="default",
        worker_pid=123,
        interval_seconds=0.001,
        stop_event=stop_event,
        heartbeat_fn=heartbeat_fn,
        pid_alive_fn=lambda _pid: True,
    )
    assert calls == 2


def test_emit_worker_auto_heartbeat_updates_task_and_run(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _claim_with_pid(conn, pid=os.getpid())
        ok = kb._emit_worker_auto_heartbeat(
            task_id=tid,
            run_id=run_id,
            claim_lock=kb.get_task(conn, tid).claim_lock,
            board="default",
        )
        task = kb.get_task(conn, tid)
        row = _run_row(conn, run_id)
        events = [e.kind for e in kb.list_events(conn, tid)]
    assert ok is True
    assert task.last_heartbeat_at is not None
    assert row["last_heartbeat_at"] is not None
    assert "heartbeat" in events


def test_default_spawn_starts_nonblocking_heartbeat_loop(
    kanban_home, tmp_path, monkeypatch
):
    started = {}

    class FakePopen:
        pid = 61234

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    def fake_start(**kwargs):
        started.update(kwargs)
        return object()

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setattr(kb, "_start_worker_heartbeat_loop", fake_start)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="spawn heartbeat", assignee="worker")
        task = kb.claim_task(conn, tid)
        assert task is not None
        pid = kb._default_spawn(task, str(tmp_path), board="default")

    assert pid == 61234
    assert started["task_id"] == tid
    assert started["run_id"] == task.current_run_id
    assert started["claim_lock"] == task.claim_lock
    assert started["worker_pid"] == 61234


def test_worker_heartbeat_interval_honors_env_override(monkeypatch):
    monkeypatch.delenv("HERMES_WORKER_HEARTBEAT_SEC", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", "3")
    assert kb._worker_heartbeat_interval_seconds() == 3


def test_worker_heartbeat_interval_honors_planspec_alias(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.setenv("HERMES_WORKER_HEARTBEAT_SEC", "7")
    assert kb._worker_heartbeat_interval_seconds() == 7


def test_worker_heartbeat_planspec_name_takes_precedence(monkeypatch):
    monkeypatch.setenv("HERMES_WORKER_HEARTBEAT_SEC", "11")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", "22")
    assert kb._worker_heartbeat_interval_seconds() == 11


def test_worker_heartbeat_interval_zero_disables(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.setenv("HERMES_WORKER_HEARTBEAT_SEC", "0")
    assert kb._worker_heartbeat_interval_seconds() == 0


def test_worker_heartbeat_legacy_zero_also_disables(monkeypatch):
    monkeypatch.delenv("HERMES_WORKER_HEARTBEAT_SEC", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", "0")
    assert kb._worker_heartbeat_interval_seconds() == 0


def test_worker_heartbeat_unset_returns_default(monkeypatch):
    monkeypatch.delenv("HERMES_WORKER_HEARTBEAT_SEC", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", raising=False)
    assert kb._worker_heartbeat_interval_seconds() == kb.WORKER_HEARTBEAT_SEC


def test_start_worker_heartbeat_loop_skips_thread_when_disabled(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.setenv("HERMES_WORKER_HEARTBEAT_SEC", "0")
    thread = kb._start_worker_heartbeat_loop(
        task_id="t-disable",
        run_id=1,
        claim_lock="claim",
        board=None,
        worker_pid=99999,
    )
    assert thread is None


def test_start_worker_heartbeat_loop_starts_thread_when_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_WORKER_HEARTBEAT_SEC", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS", raising=False)
    thread = kb._start_worker_heartbeat_loop(
        task_id="t-enable",
        run_id=1,
        claim_lock="claim",
        board=None,
        worker_pid=99999,
        interval_seconds=3600,
    )
    try:
        assert isinstance(thread, threading.Thread)
        assert thread.is_alive()
    finally:
        if thread is not None:
            thread.join(timeout=0.1)

"""Kanban DB tests: runtime.

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

def _write_corrupt_db(path: Path) -> bytes:
    """Write a kanban DB with a VALID SQLite header but malformed page content.

    This is the corruption shape the integrity guard specifically targets
    (e.g. issue #29507 follow-up reports where the file's first 16 bytes
    pass the header byte check but ``PRAGMA integrity_check`` then fails
    because the internal pages are damaged). It's what main's header-only
    validator was letting through, and what this PR adds the full guard
    for.
    """
    # 100-byte SQLite header (magic + minimal valid-looking fields) so the
    # cheap header check passes, then deliberate garbage so sqlite refuses
    # to read the file past the header.
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob


def test_init_db_refuses_corrupt_existing_file(tmp_path):
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)
    # Ensure the cache doesn't mask the guard.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.init_db(db_path=db_path)

    err = excinfo.value
    assert err.db_path == db_path
    assert err.backup_path is not None
    assert err.backup_path.exists()
    assert err.backup_path.read_bytes() == original
    # Original bytes untouched — no schema was written on top.
    assert db_path.read_bytes() == original
    assert str(db_path) in str(err)
    assert str(err.backup_path) in str(err)


def test_connect_refuses_corrupt_existing_file(tmp_path):
    db_path = tmp_path / "kanban.db"
    _write_corrupt_db(db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError):
        kb.connect(db_path=db_path)


def test_repeated_corrupt_open_reuses_single_backup(tmp_path):
    """Repeated quarantines of the same corrupt bytes must not amplify disk usage.

    Regression for the gateway dispatcher's 5-min retry loop on shared kanban
    DBs across multi-profile fleets: each retry on an unchanged corrupt file
    used to create a fresh ``.corrupt.<timestamp>.bak`` until disk filled. The
    content-addressed backup name is deterministic in the DB's sha256, so
    N retries of the same bytes share one backup.
    """
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)

    backups: set[Path] = set()
    for _ in range(10):
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        backups.add(excinfo.value.backup_path)

    assert len(backups) == 1, f"expected 1 deterministic backup, got {len(backups)}"
    (backup,) = backups
    assert backup.exists()
    assert backup.read_bytes() == original

    # Mutate the corrupt bytes — fingerprint changes, separate backup preserved.
    with db_path.open("r+b") as f:
        f.seek(4096)
        f.write(b"\xAB" * 64)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(kb.KanbanDbCorruptError) as excinfo2:
        kb.connect(db_path=db_path)
    second_backup = excinfo2.value.backup_path
    assert second_backup is not None
    assert second_backup != backup
    assert second_backup.exists()


def test_locked_healthy_db_does_not_classify_as_corrupt(tmp_path, monkeypatch):
    """A transient lock during the probe must not produce a .corrupt backup
    and must not be reported as :class:`KanbanDbCorruptError`. Raw sqlite
    ``OperationalError`` (lock/busy) is acceptable and expected."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        # First call is the integrity probe — simulate a lock.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError):
        kb.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="still here")
        titles = [t.title for t in kb.list_tasks(conn)]
    assert "still here" in titles


class _MalformedProbe:
    """Stand-in connection whose integrity probe always reports malformed."""

    def execute(self, *_a, **_k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    def close(self):
        pass


def test_guard_reprobes_transient_malformed_then_recovers(tmp_path, monkeypatch):
    """A one-shot 'database disk image is malformed' that clears on the next
    probe must NOT quarantine a healthy DB.

    Reproduces the 2026-05-28 storm: under multi-process WAL/SHM coordination
    the integrity probe occasionally read a torn page and the guard copied the
    whole DB to a ``.corrupt`` backup and killed the dispatcher, even though
    ``integrity_check`` passed moments later.
    """
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_sqlite_connect = kb._sqlite_connect
    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _MalformedProbe()  # transient torn read on first probe only
        return real_sqlite_connect(*args, **kwargs)

    monkeypatch.setattr(kb, "_sqlite_connect", flaky_connect)

    # Must return cleanly — no exception, no quarantine.
    kb._guard_existing_db_is_healthy(db_path, attempts=3, backoff_s=0)

    assert calls["n"] >= 2, "guard did not re-probe after a transient malformed read"
    assert list(tmp_path.glob("*.corrupt.*")) == [], "transient blip must not back up the DB"


def test_guard_quarantines_persistent_malformed(tmp_path, monkeypatch):
    """If every re-probe still reports malformed, the guard must still
    quarantine (backup + raise) — retries cannot mask real corruption."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    calls = {"n": 0}

    def always_malformed(*_args, **_kwargs):
        calls["n"] += 1
        return _MalformedProbe()

    monkeypatch.setattr(kb, "_sqlite_connect", always_malformed)

    with pytest.raises(kb.KanbanDbCorruptError):
        kb._guard_existing_db_is_healthy(db_path, attempts=3, backoff_s=0)

    assert calls["n"] == 3, "guard should re-probe exactly `attempts` times before quarantining"
    assert list(tmp_path.glob("*.corrupt.*")), "persistent corruption must still produce a backup"


def test_init_db_allows_missing_then_healthy(tmp_path):
    db_path = tmp_path / "fresh.db"
    assert not db_path.exists()
    kb.init_db(db_path=db_path)
    assert db_path.exists() and db_path.stat().st_size > 0

    # Idempotent on a healthy DB: data survives a second init.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="keeps")
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        tasks = kb.list_tasks(conn)
    assert [t.title for t in tasks] == ["keeps"]


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------

def test_maybe_emit_scratch_tip_fires_once_per_install(kanban_home, caplog):
    """First scratch workspace materialization warns + emits an event.

    Subsequent scratch workspaces on the SAME install stay silent — the
    sentinel file under kanban_home() flips after the first emit.
    """
    import logging

    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            kb._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kb._scratch_tip_shown()
    assert kb._scratch_tip_sentinel_path().exists()

    # Warning was logged exactly once.
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert len(tip_records) == 1, (
        f"Expected exactly one tip warning, got {len(tip_records)}: "
        f"{[r.getMessage() for r in tip_records]!r}"
    )

    # An event row was appended on the first task.
    with kb.connect_closing() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t1,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "tip_scratch_workspace" in kinds, (
        f"Expected tip_scratch_workspace event on first scratch task; "
        f"got {kinds!r}"
    )

    # Second scratch materialization on the same install stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            kb._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kb.connect_closing() as conn:
        events2 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t2,),
        ).fetchall()
    assert "tip_scratch_workspace" not in [e["kind"] for e in events2], (
        "Tip event should not be appended for subsequent scratch tasks."
    )


def test_maybe_emit_scratch_tip_skips_non_scratch_workspaces(kanban_home, caplog):
    """worktree/dir workspaces are preserved on completion and must not
    trigger the scratch-cleanup tip."""
    import logging

    with kb.connect_closing() as conn:
        t_wt = kb.create_task(conn, title="worktree task")
        t_dir = kb.create_task(conn, title="dir task")

    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            kb._maybe_emit_scratch_tip(conn, t_wt, "worktree")
            kb._maybe_emit_scratch_tip(conn, t_dir, "dir")

    # Sentinel stays unset — these workspaces are preserved by design,
    # so the warning is irrelevant for them and we save the one-shot
    # for a real scratch user.
    assert not kb._scratch_tip_shown()
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records == []
    with kb.connect_closing() as conn:
        for tid in (t_wt, t_dir):
            events = conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (tid,),
            ).fetchall()
            assert "tip_scratch_workspace" not in [e["kind"] for e in events]


def test_connect_sets_secure_delete_on(tmp_path):
    """secure_delete=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"


def test_connect_sets_cell_size_check_on(tmp_path):
    """cell_size_check=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA cell_size_check").fetchone()
    assert row[0] == 1, f"expected cell_size_check=1, got {row[0]}"


def test_connect_sets_synchronous_full(tmp_path):
    """synchronous must be FULL (=2), not NORMAL (=1)."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA synchronous").fetchone()
    assert row[0] == 2, f"expected synchronous=2 (FULL), got {row[0]}"


def test_connect_pragmas_applied_on_reconnect(tmp_path):
    """All three pragmas must be re-applied on every connect(), not just the first."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # First connection: write a task and close.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="reconnect-check")
    # Force re-init path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Second connection: pragmas must still be applied.
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_pragmas_not_accidentally_disabled_by_migrate_path(tmp_path):
    """Migration path must not reset connection pragmas."""
    db_path = tmp_path / "legacy.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Initialise with a fresh connect so schema + init run.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="pre-migration-task")
    # Simulate a re-entry through the init/migration path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_write_txn_preserves_original_exception_when_rollback_fails(kanban_home):
    """When a write inside write_txn raises an OperationalError that SQLite
    has already auto-rolled-back (e.g. ``disk I/O error``,
    ``database is locked``, ``database disk image is malformed``), the
    explicit ROLLBACK in ``write_txn.__exit__`` itself raises
    ``cannot rollback - no transaction is active``. The original cause
    must NOT be masked by the secondary rollback failure — operators rely
    on the original cause to diagnose the underlying issue.
    """

    class FailingConnWrapper:
        """Delegate to a real connection, simulating an EIO during an INSERT
        that SQLite has already auto-rolled-back."""

        def __init__(self, real):
            self._real = real
            self._fail_armed = True

        def execute(self, sql, *args, **kwargs):
            if (
                self._fail_armed
                and sql.lstrip().upper().startswith("INSERT")
                and "task_events" in sql.lower()
            ):
                self._fail_armed = False  # one-shot
                # Simulate SQLite auto-rolling back the transaction by
                # issuing a real ROLLBACK now. After this, BEGIN IMMEDIATE
                # is no longer active and an explicit ROLLBACK would error.
                try:
                    self._real.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with kb.connect_closing() as conn:
        wrapper = FailingConnWrapper(conn)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with kb.write_txn(wrapper):
                kb._append_event(wrapper, "t_bogus", "promoted", None)

    msg = str(excinfo.value)
    assert "disk I/O error" in msg, (
        f"write_txn masked the original exception with rollback failure; "
        f"got {msg!r} (expected to contain 'disk I/O error')"
    )
    assert "cannot rollback" not in msg, (
        f"write_txn surfaced the rollback failure instead of the original "
        f"OperationalError; got {msg!r}"
    )


def test_write_txn_healthy_commit_no_exception(tmp_path):
    """Normal commit does not trigger the torn-extend check."""
    from hermes_cli.kanban_db import connect, write_txn
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    # Should not raise
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
            "VALUES ('t_test01', 'test task', 'tester', 'todo', 0, 1234567890)"
        )
    row = conn.execute("SELECT title FROM tasks WHERE id='t_test01'").fetchone()
    assert row["title"] == "test task"
    conn.close()


def test_write_txn_raises_on_truncated_file(tmp_path):
    """A mocked smaller file size triggers the torn-extend check."""
    from hermes_cli.kanban_db import connect, write_txn
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    conn.execute("PRAGMA journal_mode=DELETE")
    # Get actual page size so we can fake a smaller file
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    original_getsize = os.path.getsize

    def fake_getsize(path):
        # Return a size that implies at least 1 fewer page than header claims
        real_size = original_getsize(path)
        return max(0, real_size - page_size)

    with pytest.raises(sqlite3.DatabaseError, match="torn-extend|page count mismatch"):
        with unittest.mock.patch("hermes_cli.kanban_db.os.path.getsize", side_effect=fake_getsize):
            with write_txn(conn) as c:
                c.execute(
                    "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                    "VALUES ('t_test02', 'test task 2', 'tester', 'todo', 0, 1234567890)"
                )
    conn.close()


def test_write_txn_wal_mode_ignores_transient_main_file_size_lag(tmp_path):
    """WAL commits must not treat an uncheckpointed main DB as torn-extend."""
    from hermes_cli.kanban_db import connect, write_txn

    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    original_getsize = os.path.getsize

    def fake_getsize(path):
        real_size = original_getsize(path)
        return max(0, real_size - page_size)

    with unittest.mock.patch("hermes_cli.kanban_db.os.path.getsize", side_effect=fake_getsize):
        with write_txn(conn) as c:
            c.execute(
                "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                "VALUES ('t_wal001', 'wal task', 'tester', 'todo', 0, 1234567890)"
            )
    row = conn.execute("SELECT title FROM tasks WHERE id='t_wal001'").fetchone()
    assert row["title"] == "wal task"
    conn.close()


def test_write_txn_post_commit_check_fires_every_call(tmp_path):
    """The invariant check runs on every write_txn call."""
    from hermes_cli.kanban_db import connect, write_txn
    import hermes_cli.kanban_db as kanban_db_module
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    call_count = 0
    real_check = kanban_db_module._check_file_length_invariant

    def counting_check(c):
        nonlocal call_count
        call_count += 1
        real_check(c)

    with unittest.mock.patch.object(kanban_db_module, "_check_file_length_invariant", counting_check):
        for i in range(3):
            with write_txn(conn) as c:
                c.execute(
                    f"INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                    f"VALUES ('t_fire{i:02d}', 'task {i}', 'tester', 'todo', 0, 1234567890)"
                )
    assert call_count == 3
    conn.close()


def test_connect_sets_wal_autocheckpoint_100(tmp_path):
    """connect() sets wal_autocheckpoint to 100."""
    from hermes_cli.kanban_db import connect
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    val = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    assert val == 100
    conn.close()


def test_write_txn_check_reads_correct_header_fields(tmp_path):
    """Synthetic DB file with mismatched header page_count triggers the check."""
    import struct
    from hermes_cli.kanban_db import _check_file_length_invariant

    class _Cursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _FakeConn:
        def __init__(self, db_path: Path, page_size: int):
            self._db_path = db_path
            self._page_size = page_size

        def execute(self, sql):
            sql = sql.lower()
            if "journal_mode" in sql:
                return _Cursor(("delete",))
            if "database_list" in sql:
                return _Cursor((0, "main", str(self._db_path)))
            if "page_size" in sql:
                return _Cursor((self._page_size,))
            raise AssertionError(f"unexpected SQL: {sql}")

    db = tmp_path / "synthetic.db"
    page_size = 4096
    header = bytearray(b"SQLite format 3\x00" + (b"\x00" * (page_size - 16)))
    header[16:18] = struct.pack(">H", page_size)
    header[28:32] = struct.pack(">I", 2)
    db.write_bytes(header)
    with pytest.raises(sqlite3.DatabaseError, match="torn-extend|page count mismatch"):
        _check_file_length_invariant(_FakeConn(db, page_size))  # type: ignore[arg-type]


def test_reap_worker_zombies_returns_count():
    """reap_worker_zombies() returns the list of reaped PIDs."""
    from unittest.mock import patch

    fake_pids = [12345, 67890, 11111]
    call_count = [0]

    def fake_waitpid(pid, flags):
        if call_count[0] < len(fake_pids):
            p = fake_pids[call_count[0]]
            call_count[0] += 1
            return p, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            pids = kb.reap_worker_zombies()
    assert pids == [12345, 67890, 11111]


def test_reap_worker_zombies_noop_on_windows(monkeypatch):
    """reap_worker_zombies() returns 0 and never calls os.waitpid on Windows."""
    from unittest.mock import patch

    monkeypatch.setattr("hermes_cli.kanban_db.os.name", "nt")
    with patch("hermes_cli.kanban_db.os.waitpid") as mock_waitpid:
        result = kb.reap_worker_zombies()
    mock_waitpid.assert_not_called()
    assert result == []


def test_reap_worker_zombies_noop_no_children():
    """reap_worker_zombies() returns 0 without error when there are no children."""
    from unittest.mock import patch

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=ChildProcessError):
        result = kb.reap_worker_zombies()
    assert result == []


def test_reap_worker_zombies_records_exit_status():
    """reap_worker_zombies() calls _record_worker_exit for each reaped pid."""
    from unittest.mock import patch

    calls = []
    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            return 12345, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch(
            "hermes_cli.kanban_db._record_worker_exit",
            side_effect=lambda p, s: calls.append((p, s)),
        ):
            kb.reap_worker_zombies()

    assert calls == [(12345, 0)]


def test_reap_worker_zombies_handles_waitpid_os_error():
    """reap_worker_zombies() does not propagate generic OSError from os.waitpid."""
    from unittest.mock import patch

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=OSError("test error")):
        result = kb.reap_worker_zombies()
    assert result == []


def test_zombie_reaper_runs_despite_board_connect_failure():
    """reap_worker_zombies runs even when a board tick raises an error."""
    from unittest.mock import patch

    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] <= 2:
            return [12345, 67890][call_count[0] - 1], 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            # Simulate a board tick failure before reaping
            try:
                raise sqlite3.OperationalError("disk I/O error")
            except sqlite3.OperationalError:
                pass

            # Reaper still runs independently
            pids = kb.reap_worker_zombies()

    assert pids == [12345, 67890]


def test_zombie_reaper_survives_all_boards_failing():
    """reap_worker_zombies runs each tick regardless of board tick failures."""
    from unittest.mock import patch

    total_reaped = 0

    def make_fake_waitpid(zombie_pids):
        call_count = [0]

        def fake_waitpid(pid, flags):
            if call_count[0] < len(zombie_pids):
                p = zombie_pids[call_count[0]]
                call_count[0] += 1
                return p, 0
            return 0, 0

        return fake_waitpid

    # 5 ticks, 2 zombies per tick = 10 total
    for tick in range(5):
        pids = [tick * 100 + 1, tick * 100 + 2]
        with patch(
            "hermes_cli.kanban_db.os.waitpid", side_effect=make_fake_waitpid(pids)
        ):
            with patch("hermes_cli.kanban_db._record_worker_exit"):
                pids = kb.reap_worker_zombies()
        total_reaped += len(pids)

    assert total_reaped == 10


def test_dispatch_once_still_reaps_via_extracted_fn(kanban_home):
    """The reaper inside dispatch_once still works after refactor to reap_worker_zombies()."""
    from unittest.mock import patch

    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            return 99999, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            with patch("hermes_cli.kanban_db.os.name", "posix"):
                pids = kb.reap_worker_zombies()

    assert pids == [99999]


def test_connect_closing_closes_connection_on_exit(tmp_path):
    """The new context manager MUST actually close the underlying FD."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect_closing(db_path=db_path) as conn:
        conn.execute("SELECT 1").fetchone()
    # After exit, the connection MUST be closed — subsequent execute
    # should raise ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_closing_closes_on_exception(tmp_path):
    """Connection closed even when the body raises."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    captured = []
    with pytest.raises(RuntimeError, match="boom"):
        with kb.connect_closing(db_path=db_path) as conn:
            captured.append(conn)
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_connect_closing_yields_usable_connection(tmp_path):
    """Smoke test: schema is initialized and basic ops work."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect_closing(db_path=db_path) as conn:
        tid = kb.create_task(conn, title="closing-cm test")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.title == "closing-cm test"


def test_bare_connect_does_not_close_on_context_exit(tmp_path):
    """Document the leak that connect_closing exists to prevent.

    sqlite3.Connection's __exit__ commits/rollbacks but doesn't close.
    This is the upstream behaviour we cannot change; the regression
    guard is to make sure connect_closing() does the right thing.
    """
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        pass
    # Still usable after with-block exit (the leak).
    conn.execute("SELECT 1").fetchone()
    conn.close()  # explicit close to avoid leaking THIS test


# --- Recovered: superseding-review / needs_revision tests (orig commit 92d8e718a) ---
def test_superseding_review_rewire_helper_is_explicit_and_audited(kanban_home):
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="source waiting on old review", assignee="coder")
        old_review = kb.create_task(conn, title="old review", assignee="reviewer")
        new_review = kb.create_task(conn, title="new review", assignee="reviewer")
        kb.link_tasks(conn, old_review, source)

        result = kb.rewire_superseding_review_parent(
            conn,
            source_task=source,
            old_review_task=old_review,
            new_review_task=new_review,
            reason="NEEDS_REVISION fixed and re-reviewed",
        )

        assert result == {
            "source_task": source,
            "old_review_task": old_review,
            "new_review_task": new_review,
            "old_parent_removed": True,
            "new_parent_added": True,
            "reason": "NEEDS_REVISION fixed and re-reviewed",
        }
        assert kb.parent_ids(conn, source) == [new_review]
        events = [
            e for e in kb.list_events(conn, source)
            if e.kind == "superseding_review_rewired"
        ]
        assert len(events) == 1
        assert events[0].payload == result


def test_superseding_review_rewire_is_noop_without_old_edge(kanban_home):
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="source", assignee="coder")
        old_review = kb.create_task(conn, title="old review", assignee="reviewer")
        new_review = kb.create_task(conn, title="new review", assignee="reviewer")

        result = kb.rewire_superseding_review_parent(
            conn,
            source_task=source,
            old_review_task=old_review,
            new_review_task=new_review,
            reason="operator requested audit-only check",
        )

        assert result["old_parent_removed"] is False
        assert result["new_parent_added"] is True
        assert kb.parent_ids(conn, source) == [new_review]
        event = [
            e for e in kb.list_events(conn, source)
            if e.kind == "superseding_review_rewired"
        ][-1]
        assert event.payload["old_parent_removed"] is False
        assert event.payload["new_parent_added"] is True


def test_needs_revision_fix_task_is_deterministic_idempotent_and_keeps_source_blocked(kanban_home):
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="implement lifecycle", assignee="coder")
        kb.claim_task(conn, source)
        # main renamed active_run() → latest_run(); after claim the latest run is the active one
        run = kb.latest_run(conn, source)
        assert run is not None
        assert kb.block_task(
            conn,
            source,
            reason="review-required: implementation ready for verdict",
            expected_run_id=run.id,
        )
        old_review = kb.create_task(conn, title="review implementation", assignee="reviewer")
        reviewer_metadata = {
            "verdict": "NEEDS_REVISION",
            "blocking_findings": ["missing supersedes relation"],
            "required_verification": ["pytest tests/hermes_cli/test_kanban_db.py -q"],
            "evidence_audited": [source, old_review],
            "residual_risk": "source must remain blocked until finalization gate",
        }

        first = kb.ensure_needs_revision_fix_task(
            conn,
            source_task=source,
            review_task=old_review,
            reviewer_metadata=reviewer_metadata,
            reason="Reviewer requested deterministic fix",
        )
        second = kb.ensure_needs_revision_fix_task(
            conn,
            source_task=source,
            review_task=old_review,
            reviewer_metadata=reviewer_metadata,
            reason="Reviewer requested deterministic fix",
        )

        assert second == first
        fix = kb.get_task(conn, first["fix_task"])
        assert fix is not None
        assert fix.assignee == "coder"
        assert fix.status == "ready"
        assert kb.parent_ids(conn, fix.id) == []
        assert "verdict: NEEDS_REVISION" in (fix.body or "")
        assert "source remains blocked" in (fix.body or "")
        assert kb.get_task(conn, source).status == "blocked"
        events = [
            e for e in kb.list_events(conn, source)
            if e.kind == "needs_revision_fix_task_ensured"
        ]
        assert len(events) == 1
        assert events[0].payload["source_task"] == source
        assert events[0].payload["review_task"] == old_review
        assert events[0].payload["fix_task"] == fix.id
        assert events[0].payload["created"] is True


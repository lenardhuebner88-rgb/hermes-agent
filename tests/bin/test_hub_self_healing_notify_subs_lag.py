"""F-2026-05-17-04 contract pin: _auto_fix_notify_subs_lag does a CAS UPDATE.

Pre-patch the function returned ``conservative_noop_for_safety`` and never
mutated state. Post-patch it must:

  - Snapshot stale subs (``MAX(task_events.id) - last_event_id > 50``).
  - Per row, ``UPDATE ... WHERE PK AND last_event_id = <snapshot_last>``
    (compare-and-swap defends against concurrent hub-watcher tick).
  - Return ``{rows_scanned, rows_bumped, skipped_race, tuples}`` with a
    list of ``(task_id, platform, chat_id, thread_id, from, to)`` tuples.

These tests use an isolated tmp sqlite that mirrors the live schema (PK +
columns) so a future schema rename is loud.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


HUB_SELF_HEALING = Path("/home/piet/.hermes/bin/hub_self_healing.py")


def _load_mod(monkeypatch, db_path: Path):
    """Load hub_self_healing with KANBAN_DB pointed at our tmp DB."""
    if not HUB_SELF_HEALING.exists():
        pytest.skip(f"local ops script not present: {HUB_SELF_HEALING}")
    spec = importlib.util.spec_from_file_location(
        "hub_self_healing_under_test", str(HUB_SELF_HEALING)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "KANBAN_DB", db_path)
    return mod


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE kanban_notify_subs (
            task_id       TEXT NOT NULL,
            platform      TEXT NOT NULL,
            chat_id       TEXT NOT NULL,
            thread_id     TEXT NOT NULL DEFAULT '',
            user_id       TEXT,
            notifier_profile TEXT,
            created_at    INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, platform, chat_id, thread_id)
        );
        CREATE TABLE task_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id   TEXT NOT NULL,
            kind      TEXT,
            payload   TEXT,
            created_at INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_sub(db_path: Path, task_id: str, last_event_id: int = 0) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, "
        " notifier_profile, created_at, last_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, "kanban-internal", "hub-watcher", "", "test:profile", 1, last_event_id),
    )
    conn.commit()
    conn.close()


def _insert_events(db_path: Path, task_id: str, count: int) -> int:
    """Insert N events for task; returns the MAX event id afterward."""
    conn = sqlite3.connect(str(db_path))
    for _ in range(count):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, "tick", "{}", 1),
        )
    conn.commit()
    latest = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    conn.close()
    return latest


def _read_sub(db_path: Path, task_id: str) -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_stale_subs_get_bumped(isolated_db, monkeypatch):
    """A sub lagging > 50 events advances to MAX(task_events.id)."""
    mod = _load_mod(monkeypatch, isolated_db)
    _insert_sub(isolated_db, "t_stale", last_event_id=5)
    latest = _insert_events(isolated_db, "t_stale", 200)
    # also a fresh sub that should not be bumped
    _insert_sub(isolated_db, "t_fresh", last_event_id=latest)
    _insert_events(isolated_db, "t_fresh", 0)  # no extra events, still fresh

    result = mod._auto_fix_notify_subs_lag()

    assert result["action"] == "notify_subs_lag_reset"
    assert result["rows_bumped"] == 1
    assert result["skipped_race"] == 0
    assert _read_sub(isolated_db, "t_stale") == latest
    assert _read_sub(isolated_db, "t_fresh") == latest
    assert "conservative_noop_for_safety" not in result.get("note", "")


def test_no_stale_subs_returns_zero(isolated_db, monkeypatch):
    """When nothing is lagging the function does not bump anything."""
    mod = _load_mod(monkeypatch, isolated_db)
    _insert_sub(isolated_db, "t_fresh1", last_event_id=200)
    latest = _insert_events(isolated_db, "t_fresh1", 200)
    # advance to exact latest so lag = 0
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = ?",
        (latest, "t_fresh1"),
    )
    conn.commit()
    conn.close()

    result = mod._auto_fix_notify_subs_lag()
    assert result["rows_bumped"] == 0
    assert result["tuples"] == []


def test_compare_and_swap_skips_concurrent_update(isolated_db, monkeypatch):
    """If hub-watcher bumps the row between snapshot and UPDATE, CAS skips.

    Hook the connection so that the SELECT returns the stale row, but BEFORE
    the per-row UPDATE runs we advance ``last_event_id`` via a separate
    connection. The function's UPDATE matches by the original snapshot value,
    so rowcount is 0 → ``skipped_race`` increments, ``rows_bumped`` does not.
    """
    mod = _load_mod(monkeypatch, isolated_db)
    _insert_sub(isolated_db, "t_race", last_event_id=5)
    latest = _insert_events(isolated_db, "t_race", 200)

    real_connect = sqlite3.connect

    class RacyConn:
        """Wraps a real connection; first UPDATE call triggers race-simulation."""

        def __init__(self, real):
            self._real = real
            self._raced = False

        def execute(self, sql, params=()):
            if (not self._raced) and sql.lstrip().upper().startswith("UPDATE"):
                # Concurrent hub-watcher tick: advance the row to latest before
                # our CAS UPDATE runs.
                other = real_connect(str(isolated_db))
                other.execute(
                    "UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = ?",
                    (latest, "t_race"),
                )
                other.commit()
                other.close()
                self._raced = True
            return self._real.execute(sql, params)

        def commit(self):
            return self._real.commit()

        def close(self):
            return self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    def racy_connect(*args, **kwargs):
        return RacyConn(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", racy_connect)
    result = mod._auto_fix_notify_subs_lag()

    assert result["rows_bumped"] == 0
    assert result["skipped_race"] == 1
    # Row already at latest (the simulated concurrent update wrote it).
    assert _read_sub(isolated_db, "t_race") == latest


def test_returns_tuples_list_per_row(isolated_db, monkeypatch):
    """tuples is a list of (task_id, platform, chat_id, thread_id, from, to)."""
    mod = _load_mod(monkeypatch, isolated_db)
    _insert_sub(isolated_db, "t_a", last_event_id=10)
    latest = _insert_events(isolated_db, "t_a", 200)

    result = mod._auto_fix_notify_subs_lag()

    assert result["rows_bumped"] == 1
    assert isinstance(result["tuples"], list)
    assert len(result["tuples"]) == 1
    tup = result["tuples"][0]
    assert tup[0] == "t_a"
    assert tup[1] == "kanban-internal"
    assert tup[2] == "hub-watcher"
    assert tup[3] == ""
    assert tup[4] == 10
    assert tup[5] == latest

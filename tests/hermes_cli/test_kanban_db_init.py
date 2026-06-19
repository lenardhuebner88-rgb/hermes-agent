from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb


def _make_legacy_db(path: Path) -> None:
    """Write a kanban DB with the pre-AUTOINCREMENT (TEXT PK) schema for the
    four tables #35096 affects, keeping every other table current so the
    additive-column migration runs cleanly on top.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(kb.SCHEMA_SQL)
    conn.executescript(
        """
        DROP TABLE task_events;
        DROP TABLE task_comments;
        DROP TABLE task_runs;
        DROP TABLE kanban_notify_subs;
        CREATE TABLE task_comments (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, status TEXT NOT NULL, started_at INTEGER NOT NULL);
        CREATE TABLE kanban_notify_subs (task_id TEXT NOT NULL, platform TEXT NOT NULL,
            chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,
            created_at INTEGER NOT NULL, last_event_id TEXT,
            PRIMARY KEY (task_id, platform, chat_id, thread_id));
        """
    )
    conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('task-1', 'T', 'done', 1000)")
    conn.execute("INSERT INTO task_comments VALUES ('c-1', 'task-1', 'agent', 'hi', 1500)")
    conn.execute("INSERT INTO task_events VALUES ('e-1', 'task-1', 'completed', NULL, 2000)")
    conn.execute("INSERT INTO task_events VALUES ('e-2', 'task-1', 'blocked', NULL, 2100)")
    conn.execute("INSERT INTO task_runs VALUES ('r-1', 'task-1', 'default', 'done', 1000)")
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at, last_event_id) "
        "VALUES ('task-1', 'telegram', '123', 1000, 'e-1')"
    )
    conn.commit()
    conn.close()


def _setup_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return db_path


def _table_struct(conn: sqlite3.Connection, table: str):
    cols = [
        (r["name"], (r["type"] or "").upper(), r["notnull"], r["pk"])
        for r in conn.execute(f"PRAGMA table_info({table})")
    ]
    idx = sorted(
        r["name"]
        for r in conn.execute(f"PRAGMA index_list({table})")
        if not r["name"].startswith("sqlite_")
    )
    return cols, idx


def test_connect_initialization_is_thread_safe(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            conn = kb.connect(board="default")
            conn.close()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with kb.connect(board="default") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_retries" in cols


def test_legacy_text_pk_tables_rebuilt_to_integer_autoincrement(tmp_path, monkeypatch):
    """A pre-AUTOINCREMENT DB is migrated in place: id columns become INTEGER
    PKs, ``last_event_id`` becomes INTEGER, data is preserved, and indexes
    are recreated (DROP TABLE would otherwise take them down)."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        for table in ("task_events", "task_comments", "task_runs"):
            id_col = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}["id"]
            assert id_col["type"].upper() == "INTEGER" and id_col["pk"] == 1

        lei = {r["name"]: r for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
        assert lei["last_event_id"]["type"].upper() == "INTEGER"

        # Data preserved across the rebuild.
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2
        assert conn.execute("SELECT body FROM task_comments").fetchone()["body"] == "hi"
        assert len(conn.execute("SELECT * FROM task_runs").fetchall()) == 1
        # Non-numeric legacy cursor ("e-1") casts to 0.
        assert conn.execute("SELECT last_event_id FROM kanban_notify_subs").fetchone()["last_event_id"] == 0

        # Indexes restored, including idx_events_run (added by the additive pass).
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("idx_events_task", "idx_events_run", "idx_comments_task",
                     "idx_runs_task", "idx_runs_status", "idx_notify_task"):
            assert name in indexes

        # AUTOINCREMENT actually works after the rebuild.
        conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('task-1', 'completed', 3000)")
        new_id = conn.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 1").fetchone()["id"]
        assert isinstance(new_id, int) and new_id >= 1


def test_rebuilt_schema_matches_fresh_db(tmp_path, monkeypatch):
    """The rebuilt tables must be structurally identical to a fresh DB, so the
    hand-written DDL in ``_REBUILD_SPECS`` can't silently drift from SCHEMA_SQL."""
    legacy_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(legacy_path)
    fresh_path = kb.kanban_db_path(board="fresh")
    fresh_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(fresh_path.resolve()))

    with kb.connect(legacy_path) as migrated, kb.connect(fresh_path) as fresh:
        for table in ("task_events", "task_comments", "task_runs", "kanban_notify_subs"):
            assert _table_struct(migrated, table) == _table_struct(fresh, table)


def test_task_events_kind_index_present(tmp_path, monkeypatch):
    """#11: decision_queue scans ``task_events WHERE kind = 'release_gate_parked'``.
    An index covering ``kind`` must exist so that scan is not full-table on a
    board with a large event log."""
    db_path = _setup_home(tmp_path, monkeypatch)
    with kb.connect(db_path) as conn:
        idx_sql = [
            (r["sql"] or "")
            for r in conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='task_events'"
            )
        ]
    assert any("kind" in s.lower() for s in idx_sql), (
        f"no index covering task_events.kind found: {idx_sql}"
    )


def test_kind_index_survives_legacy_rebuild(tmp_path, monkeypatch):
    """#11: the kind index is recreated by the additive migration pass even on a
    legacy DB rebuilt in place (same guarantee idx_events_run has)."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)
    with kb.connect(db_path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_events'"
        )}
    assert "idx_events_kind" in names, names


def test_kind_index_backfilled_for_pre_index_stamped_board(tmp_path, monkeypatch):
    """#11 regression: a board already stamped by the pre-#11 schema generation
    (the LIVE board's ``user_version`` 948194172, which lacks idx_events_kind)
    must get the index backfilled on the next connect.

    This guards the schema-generation bump.  Without it, connect()'s fast path
    sees the matching stamp and skips the whole migration pass, so the index is
    NEVER created on any already-stamped (i.e. every production) board — the
    fresh-DB tests stay green while prod is silently unfixed.
    """
    # Observed live-board user_version before #11 — the gen-2 stamp that shipped
    # without idx_events_kind in the migration pass.
    PRE_INDEX_STAMP = 948194172
    # The current code must compute a DIFFERENT stamp, else stamped boards skip
    # the index migration entirely.
    assert kb._SCHEMA_STAMP != PRE_INDEX_STAMP, (
        "schema generation not bumped: a board stamped by the pre-index code "
        "would take the fast path and never gain idx_events_kind"
    )

    db_path = _setup_home(tmp_path, monkeypatch)
    # Fresh init creates the index; simulate the older live board by dropping it
    # and stamping the header with the exact pre-#11 stamp.
    with kb.connect(db_path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_events_kind")
        conn.execute(f"PRAGMA user_version = {PRE_INDEX_STAMP}")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # Reconnect with current code: stamp mismatch → migration reruns → index back.
    with kb.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='task_events'"
            )
        }
    assert "idx_events_kind" in names, names


def test_task_comments_kind_column_present(tmp_path, monkeypatch):
    """F4: fresh DBs carry the additive ``kind`` column on task_comments,
    defaulting to 'comment' for plain inserts."""
    db_path = _setup_home(tmp_path, monkeypatch)
    with kb.connect(db_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_comments)")}
        assert "kind" in cols
        # A bare INSERT (the inline-comment paths) defaults to 'comment'.
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('t1', 'T', 'ready', 1)"
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES ('t1', 'a', 'b', 2)"
        )
        kind = conn.execute("SELECT kind FROM task_comments").fetchone()["kind"]
    assert kind == "comment"


def test_task_comments_kind_backfilled_for_pre_f4_stamped_board(tmp_path, monkeypatch):
    """A board stamped by the pre-F4 schema generation (no ``kind`` column on
    task_comments) must gain it on the next connect — otherwise connect()'s
    fast path skips the migration and the column never lands on prod boards.

    Guards the _SCHEMA_GENERATION bump that ships the kind migration."""
    db_path = _setup_home(tmp_path, monkeypatch)
    # Build a board, then simulate the older live shape: drop the kind column
    # (SQLite has no DROP COLUMN pre-3.35, so rebuild the table without it) and
    # stamp the header with a non-matching user_version.
    with kb.connect(db_path) as conn:
        conn.executescript(
            """
            ALTER TABLE task_comments RENAME TO task_comments_old;
            CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
                created_at INTEGER NOT NULL);
            INSERT INTO task_comments (id, task_id, author, body, created_at)
                SELECT id, task_id, author, body, created_at FROM task_comments_old;
            DROP TABLE task_comments_old;
            """
        )
        conn.execute("PRAGMA user_version = 1")  # any stamp != current
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_comments)")}
        assert "kind" not in cols  # precondition: simulated old board
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with kb.connect(db_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_comments)")}
    assert "kind" in cols, cols


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Re-opening an already-migrated DB is a no-op and leaves data intact."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path):
        pass
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        id_col = {r["name"]: r for r in conn.execute("PRAGMA table_info(task_events)")}["id"]
        assert id_col["type"].upper() == "INTEGER"
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2


def test_unseen_events_for_sub_survives_migrated_db(tmp_path, monkeypatch):
    """The crash that motivated #35096 — ``int(None)`` on a NULL cursor — is
    gone after migration; the notifier query returns an integer cursor."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id="task-1", platform="telegram", chat_id="123"
        )
        assert isinstance(cursor, int)
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# Cross-process init stamp (_SCHEMA_STAMP fast path)
# ---------------------------------------------------------------------------

def test_fresh_connect_stamps_user_version(tmp_path):
    db = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    conn = kb.connect(db_path=db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == kb._SCHEMA_STAMP
    finally:
        conn.close()


def test_stamped_db_connects_without_flock_or_integrity_probe(tmp_path, monkeypatch):
    """A NEW process (empty ``_INITIALIZED_PATHS``) connecting to a stamped DB
    must skip the exclusive cross-process flock, the integrity probe and the
    migration pass. That per-process combination (flock + integrity_check +
    full migration pass, busy_timeout 120s) was the 'dashboard hangs for
    minutes behind one worker spawn' shape."""
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))  # simulate a fresh process

    def _fail_lock(path):
        raise AssertionError("cross-process init lock taken on stamped fast path")

    def _fail_guard(path, **kwargs):
        raise AssertionError("integrity probe ran on stamped fast path")

    def _fail_migrate(conn):
        raise AssertionError("migration pass ran on stamped fast path")

    monkeypatch.setattr(kb, "_cross_process_init_lock", _fail_lock)
    monkeypatch.setattr(kb, "_guard_existing_db_is_healthy", _fail_guard)
    monkeypatch.setattr(kb, "_migrate_add_optional_columns", _fail_migrate)

    conn = kb.connect(db_path=db)
    try:
        kb.create_task(conn, title="fast path works")
        assert [t.title for t in kb.list_tasks(conn)] == ["fast path works"]
    finally:
        conn.close()


def test_stale_stamp_reruns_full_init(tmp_path, monkeypatch):
    """A stamp from a different schema generation must NOT satisfy the fast
    path — the migration pass re-runs and re-stamps."""
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()
    raw = sqlite3.connect(str(db))
    raw.execute("PRAGMA user_version=12345")
    raw.close()
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))

    calls = {"migrate": 0}
    real = kb._migrate_add_optional_columns

    def counting(conn):
        calls["migrate"] += 1
        return real(conn)

    monkeypatch.setattr(kb, "_migrate_add_optional_columns", counting)
    conn = kb.connect(db_path=db)
    try:
        assert calls["migrate"] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == kb._SCHEMA_STAMP
    finally:
        conn.close()


def test_init_db_forces_migration_pass_despite_stamp(tmp_path, monkeypatch):
    """``init_db`` promises an unconditional re-migration; the on-disk stamp
    must not short-circuit it."""
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()  # stamped now

    calls = {"migrate": 0}
    real = kb._migrate_add_optional_columns

    def counting(conn):
        calls["migrate"] += 1
        return real(conn)

    monkeypatch.setattr(kb, "_migrate_add_optional_columns", counting)
    kb.init_db(db_path=db)
    assert calls["migrate"] == 1


def test_busy_timeout_override_applies_per_connection(tmp_path):
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()

    conn = kb.connect(db_path=db, busy_timeout_ms=4321)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    finally:
        conn.close()

    conn = kb.connect(db_path=db)
    try:
        assert (
            conn.execute("PRAGMA busy_timeout").fetchone()[0]
            == kb.DEFAULT_BUSY_TIMEOUT_MS
        )
    finally:
        conn.close()


def test_fast_path_applies_connection_pragmas(tmp_path):
    """The stamped fast path must produce a connection indistinguishable from
    the init path: same per-connection PRAGMAs, WAL still active."""
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))

    conn = kb.connect(db_path=db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
    finally:
        conn.close()

"""Tests for the disposition-ledger repository layer (FRD-S1 additive DB slice).

Covers: schema present on fresh + migrated DBs, CRUD round-trips,
validation, filtering, status transitions, and supersession.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import disposition as disp


# ---------------------------------------------------------------------------
# Fixture — isolated fresh DB (mirrors test_kanban_db.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path, monkeypatch):
    """Fresh kanban DB in a temp HERMES_HOME; returns an open connection."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kb.connect(db_path=db_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _insert_basic(conn, *, source_task_id="t_abc", typ="risk", disposition="done",
                  next_action="", severity="none", evidence="", supersedes_id=None, item_id=None):
    return kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ=typ,
        disposition=disposition,
        next_action=next_action,
        severity=severity,
        evidence=evidence,
        supersedes_id=supersedes_id,
        item_id=item_id,
    )


# ===========================================================================
# 1. Insert + get roundtrip
# ===========================================================================


def test_insert_get_roundtrip_all_fields(db_conn):
    before = int(time.time())
    iid = kb.insert_disposition_item(
        db_conn,
        source_task_id="t_111",
        typ="risk",
        disposition="delegate",
        next_action="ping team",
        severity="real-risk",
        evidence="file.py:10",
    )
    after = int(time.time())
    assert iid.startswith("di_")

    item = kb.get_disposition_item(db_conn, iid)
    assert item is not None
    assert item["id"] == iid
    assert item["source_task_id"] == "t_111"
    assert item["typ"] == "risk"
    assert item["disposition"] == "delegate"
    assert item["next_action"] == "ping team"
    assert item["severity"] == "real-risk"
    assert item["evidence"] == "file.py:10"
    assert item["status"] == "open"
    assert before <= item["created_at"] <= after
    assert item["decided_at"] is None
    assert item["decided_by"] is None
    assert item["supersedes_id"] is None


# ===========================================================================
# 2. Validation on insert
# ===========================================================================


def test_insert_invalid_typ_raises_value_error(db_conn):
    with pytest.raises(ValueError):
        kb.insert_disposition_item(
            db_conn,
            source_task_id="t_x",
            typ="bad_typ",
            disposition="done",
        )


def test_insert_invalid_disposition_raises_value_error(db_conn):
    with pytest.raises(ValueError):
        kb.insert_disposition_item(
            db_conn,
            source_task_id="t_x",
            typ="risk",
            disposition="vanish",
        )


def test_insert_empty_source_task_id_raises_value_error(db_conn):
    with pytest.raises(ValueError):
        kb.insert_disposition_item(
            db_conn,
            source_task_id="",
            typ="risk",
            disposition="done",
        )


def test_insert_invalid_severity_raises_value_error(db_conn):
    with pytest.raises(ValueError):
        kb.insert_disposition_item(
            db_conn,
            source_task_id="t_x",
            typ="risk",
            disposition="done",
            severity="mega-bad",
        )


# ===========================================================================
# 3. list_disposition_items without filter = all, newest first
# ===========================================================================


def test_list_all_newest_first(db_conn, monkeypatch):
    # Freeze time so we can control created_at ordering
    fake_time = [1_000_000]

    def _fake_time():
        return fake_time[0]

    monkeypatch.setattr("hermes_cli.kanban_db.time.time", _fake_time)

    fake_time[0] = 1_000_001
    id1 = _insert_basic(db_conn, source_task_id="t_a")
    fake_time[0] = 1_000_002
    id2 = _insert_basic(db_conn, source_task_id="t_b")
    fake_time[0] = 1_000_003
    id3 = _insert_basic(db_conn, source_task_id="t_c")

    items = kb.list_disposition_items(db_conn)
    ids = [i["id"] for i in items]
    # newest (highest created_at) must appear first
    assert id3 in ids
    assert ids.index(id3) < ids.index(id2) < ids.index(id1)


# ===========================================================================
# 4. list filters
# ===========================================================================


def test_list_filter_by_status(db_conn):
    id_open = _insert_basic(db_conn, source_task_id="t_open")
    id_acc = _insert_basic(db_conn, source_task_id="t_acc")
    kb.set_disposition_status(db_conn, id_acc, status="accepted", decided_by="piet")

    open_items = kb.list_disposition_items(db_conn, status="open")
    assert all(i["status"] == "open" for i in open_items)
    open_ids = [i["id"] for i in open_items]
    assert id_open in open_ids
    assert id_acc not in open_ids

    acc_items = kb.list_disposition_items(db_conn, status="accepted")
    assert any(i["id"] == id_acc for i in acc_items)


def test_list_filter_by_source_task_id(db_conn):
    id_a1 = _insert_basic(db_conn, source_task_id="t_AAA")
    id_a2 = _insert_basic(db_conn, source_task_id="t_AAA")
    id_b = _insert_basic(db_conn, source_task_id="t_BBB")

    result = kb.list_disposition_items(db_conn, source_task_id="t_AAA")
    ids = [i["id"] for i in result]
    assert id_a1 in ids
    assert id_a2 in ids
    assert id_b not in ids


def test_list_filter_by_disposition(db_conn):
    id_drop = _insert_basic(db_conn, source_task_id="t_x", disposition="drop")
    id_done = _insert_basic(db_conn, source_task_id="t_x", disposition="done")

    drops = kb.list_disposition_items(db_conn, disposition="drop")
    ids = [i["id"] for i in drops]

    assert id_drop in ids
    assert id_done not in ids
    assert all(i["disposition"] == "drop" for i in drops)


def test_list_filter_by_typ(db_conn):
    id_risk = _insert_basic(db_conn, source_task_id="t_x", typ="risk")
    id_fu = _insert_basic(db_conn, source_task_id="t_x", typ="follow_up")

    risks = kb.list_disposition_items(db_conn, typ="risk")
    fups = kb.list_disposition_items(db_conn, typ="follow_up")

    assert any(i["id"] == id_risk for i in risks)
    assert all(i["typ"] == "risk" for i in risks)
    assert any(i["id"] == id_fu for i in fups)
    assert id_risk not in [i["id"] for i in fups]


def test_list_combined_filters(db_conn):
    id_match = _insert_basic(db_conn, source_task_id="t_Z", typ="risk", disposition="delegate")
    id_wrong_typ = _insert_basic(db_conn, source_task_id="t_Z", typ="follow_up")
    id_wrong_src = _insert_basic(db_conn, source_task_id="t_Y", typ="risk")
    id_wrong_disposition = _insert_basic(db_conn, source_task_id="t_Z", typ="risk", disposition="drop")

    result = kb.list_disposition_items(
        db_conn, source_task_id="t_Z", typ="risk", disposition="delegate"
    )
    ids = [i["id"] for i in result]
    assert id_match in ids
    assert id_wrong_typ not in ids
    assert id_wrong_src not in ids
    assert id_wrong_disposition not in ids


# ===========================================================================
# 5. set_disposition_status — terminal status sets decided_at + decided_by
# ===========================================================================


def test_set_terminal_status_sets_decided_fields(db_conn):
    iid = _insert_basic(db_conn, source_task_id="t_s1")
    before = int(time.time())
    updated = kb.set_disposition_status(db_conn, iid, status="accepted", decided_by="piet")
    after = int(time.time())

    assert updated is not None
    assert updated["status"] == "accepted"
    assert updated["decided_by"] == "piet"
    assert before <= updated["decided_at"] <= after

    # Verify persistence
    fresh = kb.get_disposition_item(db_conn, iid)
    assert fresh["status"] == "accepted"
    assert fresh["decided_by"] == "piet"
    assert fresh["decided_at"] is not None


def test_set_status_open_leaves_decided_at_none(db_conn):
    iid = _insert_basic(db_conn, source_task_id="t_s2")
    # First go terminal, then re-open (unusual but must be spec-compliant)
    kb.set_disposition_status(db_conn, iid, status="accepted", decided_by="piet")
    updated = kb.set_disposition_status(db_conn, iid, status="open")
    assert updated["decided_at"] is None


def test_all_terminal_statuses_set_decided_at(db_conn):
    terminal = ["accepted", "task_created", "dismissed", "superseded"]
    for st in terminal:
        iid = _insert_basic(db_conn, source_task_id="t_term")
        result = kb.set_disposition_status(db_conn, iid, status=st)
        assert result is not None, f"returned None for status={st}"
        assert result["decided_at"] is not None, f"decided_at not set for status={st}"


# ===========================================================================
# 6. set_disposition_status — validation & missing item
# ===========================================================================


def test_set_invalid_status_raises_value_error(db_conn):
    iid = _insert_basic(db_conn, source_task_id="t_v")
    with pytest.raises(ValueError):
        kb.set_disposition_status(db_conn, iid, status="flying")


def test_set_status_on_nonexistent_item_returns_none(db_conn):
    result = kb.set_disposition_status(db_conn, "di_deadbeef", status="accepted")
    assert result is None


def test_get_nonexistent_item_returns_none(db_conn):
    assert kb.get_disposition_item(db_conn, "di_nope") is None


# ===========================================================================
# 7. supersede_disposition_item
# ===========================================================================


def test_supersede_creates_new_and_marks_old(db_conn):
    old_id = _insert_basic(db_conn, source_task_id="t_sup", typ="risk")

    before = int(time.time())
    new_id = kb.supersede_disposition_item(
        db_conn,
        old_id,
        source_task_id="t_sup",
        typ="risk",
        disposition="defer",
        next_action="revisit next sprint",
        severity="scope-note",
        evidence="comment:42",
    )
    after = int(time.time())

    assert new_id is not None
    assert new_id.startswith("di_")
    assert new_id != old_id

    new_item = kb.get_disposition_item(db_conn, new_id)
    assert new_item["supersedes_id"] == old_id
    assert new_item["status"] == "open"
    assert new_item["typ"] == "risk"
    assert new_item["disposition"] == "defer"

    old_item = kb.get_disposition_item(db_conn, old_id)
    assert old_item["status"] == "superseded"
    assert before <= old_item["decided_at"] <= after


def test_supersede_nonexistent_old_returns_none(db_conn):
    result = kb.supersede_disposition_item(
        db_conn,
        "di_ghost",
        source_task_id="t_x",
        typ="follow_up",
        disposition="done",
    )
    assert result is None


# ===========================================================================
# 8. Idempotency: init_db / connect on same DB twice
# ===========================================================================


def test_double_connect_and_init_idempotent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    conn1 = kb.connect(db_path=db_path)
    conn1.close()

    # Second connect — must not error
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn2 = kb.connect(db_path=db_path)
    try:
        tables = {
            r[0]
            for r in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "disposition_items" in tables
    finally:
        conn2.close()

    # init_db (force_init path) also idempotent
    kb.init_db(db_path=db_path)
    conn3 = kb.connect(db_path=db_path)
    try:
        tables = {
            r[0]
            for r in conn3.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "disposition_items" in tables
    finally:
        conn3.close()


# ===========================================================================
# 9. Legacy-migration: DROP TABLE + user_version=0 → table recreated
# ===========================================================================


def test_legacy_migration_recreates_disposition_items(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # First init — disposition_items gets created
    conn = kb.connect(db_path=db_path)
    tables_before = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "disposition_items" in tables_before, "table must exist after first init"

    # Simulate legacy/old DB: drop the table, reset user_version to 0
    conn.execute("DROP TABLE disposition_items")
    conn.execute("PRAGMA user_version = 0")
    tables_after_drop = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "disposition_items" not in tables_after_drop, "table should be gone after DROP"
    conn.close()

    # Evict from in-process cache so connect() re-runs the init path
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # Reconnect via normal connect() — must recreate the table
    conn2 = kb.connect(db_path=db_path)
    try:
        tables_restored = {
            r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "disposition_items" in tables_restored, (
            "disposition_items not recreated after legacy migration"
        )
        # And the table must be usable
        iid = kb.insert_disposition_item(
            conn2,
            source_task_id="t_legacy",
            typ="follow_up",
            disposition="done",
        )
        assert kb.get_disposition_item(conn2, iid) is not None
    finally:
        conn2.close()


# ===========================================================================
# 10. VALID_LEDGER_STATUS constant in disposition module
# ===========================================================================


def test_valid_ledger_status_constant_defined():
    assert hasattr(disp, "VALID_LEDGER_STATUS")
    assert isinstance(disp.VALID_LEDGER_STATUS, frozenset)
    assert "open" in disp.VALID_LEDGER_STATUS
    assert "accepted" in disp.VALID_LEDGER_STATUS
    assert "task_created" in disp.VALID_LEDGER_STATUS
    assert "dismissed" in disp.VALID_LEDGER_STATUS
    assert "superseded" in disp.VALID_LEDGER_STATUS


# ===========================================================================
# 11. item_id override (caller-supplied ID)
# ===========================================================================


def test_custom_item_id_is_preserved(db_conn):
    custom_id = "di_c0ffee"
    returned_id = _insert_basic(db_conn, source_task_id="t_cid", item_id=custom_id)
    assert returned_id == custom_id
    item = kb.get_disposition_item(db_conn, custom_id)
    assert item is not None
    assert item["id"] == custom_id

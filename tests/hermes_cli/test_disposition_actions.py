"""Tests for disposition-item lifecycle actions (FRD Phase 3b — backend).

Covers:
  - dismiss_disposition_item: status=dismissed, reason comment, not-found
  - create_fix_task_from_disposition: parked task, item→task_created, idempotency,
    not-open ValueError, not-found None
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture — isolated fresh DB (mirrors test_disposition_ledger.py pattern)
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
# Helpers
# ---------------------------------------------------------------------------


def _make_source_task(conn: sqlite3.Connection, title: str = "Source task") -> str:
    """Create a minimal done task to use as source_task_id."""
    tid = kb.create_task(conn, title=title)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    return tid


def _insert_open_item(
    conn: sqlite3.Connection,
    source_task_id: str,
    *,
    typ: str = "risk",
    disposition: str = "delegate",
    next_action: str = "ping team",
    severity: str = "real-risk",
    evidence: str = "src/foo.py:42",
) -> str:
    return kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ=typ,
        disposition=disposition,
        next_action=next_action,
        severity=severity,
        evidence=evidence,
    )


# ===========================================================================
# dismiss_disposition_item
# ===========================================================================


def test_dismiss_sets_status_dismissed(db_conn):
    """Dismiss an open item → status becomes 'dismissed'."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    result = kb.dismiss_disposition_item(db_conn, iid)

    assert result is not None
    assert result["id"] == iid
    assert result["status"] == "dismissed"
    assert result["decided_by"] == "operator"


def test_dismiss_stamps_decided_at(db_conn):
    """dismissed item gets decided_at set (not None)."""
    import time
    before = int(time.time())
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    result = kb.dismiss_disposition_item(db_conn, iid)

    assert result is not None
    assert result["decided_at"] is not None
    assert result["decided_at"] >= before


def test_dismiss_with_reason_adds_comment_on_source_task(db_conn):
    """When reason is provided, a comment is written to source_task."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    kb.dismiss_disposition_item(db_conn, iid, reason="Not actionable right now")

    comments = db_conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC",
        (tid,),
    ).fetchall()
    assert len(comments) >= 1
    assert any("Not actionable right now" in row["body"] for row in comments)


def test_dismiss_without_reason_no_comment(db_conn):
    """Empty reason → no comment written (best-effort is not triggered)."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    kb.dismiss_disposition_item(db_conn, iid, reason="")

    comments = db_conn.execute(
        "SELECT id FROM task_comments WHERE task_id=?", (tid,)
    ).fetchall()
    assert len(comments) == 0


def test_dismiss_nonexistent_item_returns_none(db_conn):
    """Dismissing an item that does not exist returns None."""
    result = kb.dismiss_disposition_item(db_conn, "di_does_not_exist")
    assert result is None


# ===========================================================================
# create_fix_task_from_disposition
# ===========================================================================


def test_create_fix_task_creates_parked_task(db_conn):
    """Fix-task is created in triage status (parked, no auto-dispatch)."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid, next_action="Validate input at boundary")

    result = kb.create_fix_task_from_disposition(db_conn, iid)

    assert result is not None
    fix_task = result["fix_task"]
    assert fix_task is not None
    assert fix_task["status"] == "triage"
    assert "Validate input at boundary" in fix_task["title"] or \
           "Validate input at boundary" in (fix_task["body"] or "")


def test_create_fix_task_title_contains_next_action(db_conn):
    """Fix-task title is derived from next_action."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(
        db_conn, tid, next_action="Rotate the credentials before release"
    )

    result = kb.create_fix_task_from_disposition(db_conn, iid)
    assert result is not None
    assert "Rotate the credentials before release" in result["fix_task"]["title"]


def test_create_fix_task_sets_item_to_task_created(db_conn):
    """After fix-task creation, the disposition item status becomes 'task_created'."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    result = kb.create_fix_task_from_disposition(db_conn, iid)

    assert result is not None
    assert result["item"]["status"] == "task_created"
    # Also verify via fresh read
    fresh = kb.get_disposition_item(db_conn, iid)
    assert fresh is not None
    assert fresh["status"] == "task_created"


def test_create_fix_task_body_references_disposition_item(db_conn):
    """Fix-task body contains a reference to the disposition item id."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    result = kb.create_fix_task_from_disposition(db_conn, iid)

    assert result is not None
    body = result["fix_task"]["body"] or ""
    assert iid in body


def test_create_fix_task_idempotent(db_conn):
    """Second call returns same fix_task id (idempotency_key dedup)."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)

    # First call
    r1 = kb.create_fix_task_from_disposition(db_conn, iid)
    assert r1 is not None
    fix_id_1 = r1["fix_task"]["id"]

    # Manually reopen item so second call passes the status check
    # (we test idempotency at the task level, not re-calling on task_created)
    # Actually the real idempotency is: if the task already exists (same
    # idempotency_key), create_task returns the existing task's id.
    # The status check on "open" will raise ValueError on a second call
    # because item is now task_created.  So idempotency is tested by
    # checking the idempotency_key path: re-set item status to open and
    # call again.
    with kb.write_txn(db_conn):
        db_conn.execute(
            "UPDATE disposition_items SET status='open', decided_at=NULL, decided_by=NULL "
            "WHERE id=?", (iid,)
        )

    r2 = kb.create_fix_task_from_disposition(db_conn, iid)
    assert r2 is not None
    fix_id_2 = r2["fix_task"]["id"]

    assert fix_id_1 == fix_id_2, "Second call must return the same task (idempotency_key dedup)"


def test_create_fix_task_on_non_open_item_raises_value_error(db_conn):
    """Calling create_fix_task on an already-dismissed item raises ValueError."""
    tid = _make_source_task(db_conn)
    iid = _insert_open_item(db_conn, tid)
    kb.dismiss_disposition_item(db_conn, iid)

    with pytest.raises(ValueError, match="open"):
        kb.create_fix_task_from_disposition(db_conn, iid)


def test_create_fix_task_nonexistent_item_returns_none(db_conn):
    """Calling create_fix_task on a non-existent item returns None."""
    result = kb.create_fix_task_from_disposition(db_conn, "di_not_there")
    assert result is None

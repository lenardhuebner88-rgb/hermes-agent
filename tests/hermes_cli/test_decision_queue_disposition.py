"""Tests for FRD Phase 2a — Senke 1: offene Risiko-Disposition-Items als
decision_queue-Einträge (kind="disposition_risk").

TDD: Tests wurden ZUERST geschrieben (RED), dann Minimal-Code (GREEN).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture — isolated fresh DB (mirrors test_disposition_ledger.py)
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


def _make_done_task(conn: sqlite3.Connection, title: str = "a done task") -> str:
    """Create a task and immediately set its status to done."""
    task_id = kb.create_task(conn, title=title)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    return task_id


def _insert_risk(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    severity: str = "real-risk",
    typ: str = "risk",
    status: str = "open",
    next_action: str = "investigate",
    evidence: str = "found an issue",
    disposition: str = "defer",
) -> str:
    """Insert a disposition item and optionally close it."""
    item_id = kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ=typ,
        disposition=disposition,
        next_action=next_action,
        severity=severity,
        evidence=evidence,
    )
    if status != "open":
        kb.set_disposition_status(conn, item_id, status=status)
    return item_id


def _kinds_for_task(task_id: str, result: dict) -> list[str]:
    return [d["kind"] for d in result["decisions"] if d["task_id"] == task_id]


# ===========================================================================
# Test 1: offenes real-risk-Item → ein disposition_risk-Eintrag
# ===========================================================================


def test_open_real_risk_surfaces_as_disposition_risk(db_conn):
    """Ein offenes real-risk-Item an einem done-Task erzeugt genau einen
    decision_queue-Eintrag mit kind='disposition_risk' und risk_count=1."""
    conn = db_conn
    tid = _make_done_task(conn, title="Abgeschlossener Task")
    _insert_risk(conn, source_task_id=tid, severity="real-risk")

    result = kb.decision_queue(conn)

    kinds = _kinds_for_task(tid, result)
    assert kinds == ["disposition_risk"], f"expected ['disposition_risk'], got {kinds}"
    row = next(d for d in result["decisions"] if d["task_id"] == tid)
    assert row["risk_count"] == 1


# ===========================================================================
# Test 2: scope-note und none werden NICHT angezeigt (fail-safe Filter)
# ===========================================================================


def test_scope_note_and_none_severity_filtered_out(db_conn):
    """severity='scope-note' und severity='none' werden vom fail-safe-Filter
    ausgeschlossen — kein disposition_risk-Eintrag."""
    conn = db_conn
    tid = _make_done_task(conn)

    _insert_risk(conn, source_task_id=tid, severity="scope-note")
    _insert_risk(conn, source_task_id=tid, severity="none")

    result = kb.decision_queue(conn)
    kinds = _kinds_for_task(tid, result)
    assert "disposition_risk" not in kinds, (
        f"scope-note/none sollten gefiltert werden, aber got: {kinds}"
    )


# ===========================================================================
# Test 3: status != "open" wird nicht angezeigt
# ===========================================================================


def test_dismissed_item_not_in_queue(db_conn):
    """Ein auf 'dismissed' gesetztes Item erscheint nicht in der Queue."""
    conn = db_conn
    tid = _make_done_task(conn)
    _insert_risk(conn, source_task_id=tid, severity="real-risk", status="dismissed")

    result = kb.decision_queue(conn)
    kinds = _kinds_for_task(tid, result)
    assert "disposition_risk" not in kinds


# ===========================================================================
# Test 4: typ="follow_up" erscheint NICHT als disposition_risk
# ===========================================================================


def test_follow_up_type_not_surfaced_as_disposition_risk(db_conn):
    """Items vom Typ 'follow_up' gehören zu Senke 2, nicht Senke 1."""
    conn = db_conn
    tid = _make_done_task(conn)
    _insert_risk(
        conn,
        source_task_id=tid,
        typ="follow_up",
        severity="real-risk",
        disposition="delegate",
    )

    result = kb.decision_queue(conn)
    kinds = _kinds_for_task(tid, result)
    assert "disposition_risk" not in kinds


# ===========================================================================
# Test 5: zwei real-risk-Items am selben Task → genau EIN Eintrag, risk_count=2
# ===========================================================================


def test_two_risks_same_task_one_entry_risk_count_two(db_conn):
    """Zwei offene real-risk-Items am selben source_task → ein disposition_risk-
    Eintrag mit risk_count=2 (seen-Set verhindert Duplikat)."""
    conn = db_conn
    tid = _make_done_task(conn)
    _insert_risk(conn, source_task_id=tid, severity="real-risk", next_action="fix-a")
    _insert_risk(conn, source_task_id=tid, severity="real-risk", next_action="fix-b")

    result = kb.decision_queue(conn)
    matching = [d for d in result["decisions"] if d["task_id"] == tid]
    assert len(matching) == 1, f"erwartet genau 1 Eintrag, got {len(matching)}"
    assert matching[0]["risk_count"] == 2


# ===========================================================================
# Test 6a: title aus Task-Titel; 6b: fallback auf source_task_id
# ===========================================================================


def test_title_comes_from_task_title(db_conn):
    """Der title-Wert im decision_queue-Eintrag entspricht dem Task-Titel."""
    conn = db_conn
    tid = _make_done_task(conn, title="Mein bekannter Task-Titel")
    _insert_risk(conn, source_task_id=tid, severity="real-risk")

    result = kb.decision_queue(conn)
    row = next(d for d in result["decisions"] if d["task_id"] == tid)
    assert row["title"] == "Mein bekannter Task-Titel"


def test_title_falls_back_to_task_id_when_task_missing(db_conn):
    """Wenn kein Task für source_task_id existiert, wird source_task_id als
    title verwendet (orphaned ledger item — Task gelöscht)."""
    conn = db_conn
    ghost_id = "t_ghost999"
    # Kein Task anlegen — Item direkt inserieren (kein FK-Zwang in SQLite ohne
    # PRAGMA foreign_keys ON, und die Funktion prüft nur nicht-leer)
    kb.insert_disposition_item(
        conn,
        source_task_id=ghost_id,
        typ="risk",
        disposition="defer",
        next_action="check",
        severity="real-risk",
    )

    result = kb.decision_queue(conn)
    rows_for_ghost = [d for d in result["decisions"] if d["task_id"] == ghost_id]
    assert len(rows_for_ghost) == 1
    assert rows_for_ghost[0]["title"] == ghost_id


# ===========================================================================
# Test 7: Fail-soft — keine Ledger-Items → keine Regression bestehender Entries
# ===========================================================================


def test_no_disposition_items_no_crash_no_regression(db_conn):
    """Ein Board ohne Ledger-Items darf keine Exception werfen und keine leere
    decisions-Liste zurückgeben, wenn andere Kategorien Einträge hätten."""
    conn = db_conn
    # Lege einen sticky-blocked Task an, der normalerweise in der Queue erscheint.
    task_id = kb.create_task(conn, title="blocked task", assignee="coder")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="needs human eyes")

    result = kb.decision_queue(conn)

    # sticky_blocked muss weiterhin erscheinen
    kinds = _kinds_for_task(task_id, result)
    assert "sticky_blocked" in kinds, (
        f"sticky_blocked sollte noch kommen; got {kinds}"
    )
    # disposition_risk darf NICHT erscheinen (kein Ledger-Item)
    assert all(d["kind"] != "disposition_risk" for d in result["decisions"])

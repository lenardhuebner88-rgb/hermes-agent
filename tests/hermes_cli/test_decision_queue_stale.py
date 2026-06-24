"""Tests for FRD Phase 3a — Reaper: alternde offene follow_up/still_open
Disposition-Items als decision_queue-Einträge (kind="disposition_stale").

TDD: Tests wurden ZUERST geschrieben (RED), dann Minimal-Code (GREEN).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture — isolated fresh DB (mirrors test_decision_queue_disposition.py)
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

_DEFAULT_THRESHOLD = 4 * 24 * 3600  # 4 days in seconds
_STALE_CONFIG = {"disposition_stale_in_decision_queue": True}


def _make_done_task(conn: sqlite3.Connection, title: str = "a done task") -> str:
    """Create a task and immediately set its status to done."""
    task_id = kb.create_task(conn, title=title)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    return task_id


def _insert_item(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    typ: str = "follow_up",
    status: str = "open",
    next_action: str = "follow up on this",
    evidence: str = "pending action",
    disposition: str = "defer",
    age_seconds: int = _DEFAULT_THRESHOLD + 100,  # older than threshold by default
    now: int | None = None,
) -> str:
    """Insert a disposition item with a controlled created_at timestamp."""
    t_now = now if now is not None else int(time.time())
    created_at = t_now - age_seconds

    item_id = kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ=typ,
        disposition=disposition,
        next_action=next_action,
        evidence=evidence,
    )
    # Overwrite created_at to control age in tests
    conn.execute(
        "UPDATE disposition_items SET created_at = ? WHERE id = ?",
        (created_at, item_id),
    )
    if status != "open":
        kb.set_disposition_status(conn, item_id, status=status)
    return item_id


def _stale_entries_for_task(task_id: str, result: dict) -> list[dict]:
    return [
        d for d in result["decisions"]
        if d["task_id"] == task_id and d["kind"] == "disposition_stale"
    ]


def _kinds_for_task(task_id: str, result: dict) -> list[str]:
    return [d["kind"] for d in result["decisions"] if d["task_id"] == task_id]


# ===========================================================================
# Test 1: offenes follow_up-Item mit altem created_at → disposition_stale
# ===========================================================================


def test_old_follow_up_surfaces_as_disposition_stale(db_conn):
    """Ein offenes follow_up-Item, das älter als die Schwelle ist,
    erzeugt genau einen kind='disposition_stale'-Eintrag mit stale_count=1."""
    conn = db_conn
    t_now = int(time.time())
    tid = _make_done_task(conn, title="Follow-Up Task")
    _insert_item(
        conn,
        source_task_id=tid,
        typ="follow_up",
        age_seconds=_DEFAULT_THRESHOLD + 3600,
        now=t_now,
    )

    result = kb.decision_queue(conn, now=t_now, config=_STALE_CONFIG)

    entries = _stale_entries_for_task(tid, result)
    assert len(entries) == 1, f"expected 1 disposition_stale entry, got {entries}"
    assert entries[0]["stale_count"] == 1


# ===========================================================================
# Test 2: junges follow_up-Item (< Schwelle) → KEIN disposition_stale
# ===========================================================================


def test_young_follow_up_not_surfaced_as_stale(db_conn):
    """Ein offenes follow_up-Item, das jünger als die Schwelle ist,
    erscheint NICHT als disposition_stale."""
    conn = db_conn
    t_now = int(time.time())
    tid = _make_done_task(conn, title="Junger Follow-Up Task")
    _insert_item(
        conn,
        source_task_id=tid,
        typ="follow_up",
        age_seconds=_DEFAULT_THRESHOLD - 3600,  # jünger als Schwelle
        now=t_now,
    )

    result = kb.decision_queue(conn, now=t_now, config=_STALE_CONFIG)

    entries = _stale_entries_for_task(tid, result)
    assert len(entries) == 0, (
        f"junges Item sollte NICHT erscheinen, got {entries}"
    )


# ===========================================================================
# Test 3: typ=risk (alt) → NICHT als disposition_stale (läuft über disposition_risk)
# ===========================================================================


def test_old_risk_item_not_as_disposition_stale(db_conn):
    """Ein altes risk-Item erscheint als disposition_risk, NICHT als
    disposition_stale — Risiken laufen über Senke 1 (Block 8).
    Das item erhält severity='real-risk' damit es Block 8 passiert
    (nicht vom harmless-Filter gefiltert wird)."""
    conn = db_conn
    t_now = int(time.time())
    tid = _make_done_task(conn, title="Risk Task")

    # Direkt via insert_disposition_item mit real-risk severity
    item_id = kb.insert_disposition_item(
        conn,
        source_task_id=tid,
        typ="risk",
        disposition="defer",
        next_action="investigate risk",
        severity="real-risk",
        evidence="critical issue found",
    )
    # Älteres created_at setzen
    created_at = t_now - (_DEFAULT_THRESHOLD + 3600)
    conn.execute(
        "UPDATE disposition_items SET created_at = ? WHERE id = ?",
        (created_at, item_id),
    )

    result = kb.decision_queue(conn, now=t_now, config=_STALE_CONFIG)

    # Muss als disposition_risk auftauchen (via Senke 1 / Block 8)
    kinds = _kinds_for_task(tid, result)
    assert "disposition_risk" in kinds, (
        f"risk-Item sollte als disposition_risk erscheinen; got {kinds}"
    )
    # Darf NICHT als disposition_stale erscheinen
    stale_entries = _stale_entries_for_task(tid, result)
    assert len(stale_entries) == 0, (
        f"risk-Item sollte NICHT als disposition_stale erscheinen; got {stale_entries}"
    )


# ===========================================================================
# Test 4: typ=still_open, alt → erscheint als disposition_stale
# ===========================================================================


def test_old_still_open_surfaces_as_disposition_stale(db_conn):
    """Ein altes still_open-Item (offene Restarbeit) erscheint als
    disposition_stale."""
    conn = db_conn
    t_now = int(time.time())
    tid = _make_done_task(conn, title="Still Open Task")
    _insert_item(
        conn,
        source_task_id=tid,
        typ="still_open",
        age_seconds=_DEFAULT_THRESHOLD + 7200,
        now=t_now,
    )

    result = kb.decision_queue(conn, now=t_now, config=_STALE_CONFIG)

    entries = _stale_entries_for_task(tid, result)
    assert len(entries) == 1, (
        f"still_open sollte als disposition_stale erscheinen; got {entries}"
    )
    assert entries[0]["stale_count"] == 1


# ===========================================================================
# Test 5: status != "open" (z.B. dismissed) → nicht als disposition_stale
# ===========================================================================


def test_dismissed_stale_item_not_surfaced(db_conn):
    """Ein abgeschlossenes (dismissed) Item erscheint auch dann nicht,
    wenn es alt genug wäre."""
    conn = db_conn
    t_now = int(time.time())
    tid = _make_done_task(conn, title="Dismissed Task")
    _insert_item(
        conn,
        source_task_id=tid,
        typ="follow_up",
        status="dismissed",
        age_seconds=_DEFAULT_THRESHOLD + 3600,
        now=t_now,
    )

    result = kb.decision_queue(conn, now=t_now, config=_STALE_CONFIG)

    entries = _stale_entries_for_task(tid, result)
    assert len(entries) == 0, (
        f"dismissed Item sollte nicht erscheinen; got {entries}"
    )


# ===========================================================================
# Test 6: Cap — höchstens cap Tasks als disposition_stale
# ===========================================================================


def test_cap_limits_disposition_stale_entries(db_conn):
    """Wenn mehr Tasks als cap alte Items haben, erscheinen nur cap Einträge
    als disposition_stale (bounded pro Render)."""
    conn = db_conn
    t_now = int(time.time())
    cap = 2
    n_tasks = cap + 2  # mehr Tasks als Cap

    task_ids = []
    for i in range(n_tasks):
        tid = _make_done_task(conn, title=f"Stale Task {i}")
        _insert_item(
            conn,
            source_task_id=tid,
            typ="follow_up",
            age_seconds=_DEFAULT_THRESHOLD + 3600 + i,  # unterschiedlich alt
            now=t_now,
        )
        task_ids.append(tid)

    config = {**_STALE_CONFIG, "disposition_stale_cap": cap}
    result = kb.decision_queue(conn, now=t_now, config=config)

    stale_entries = [d for d in result["decisions"] if d["kind"] == "disposition_stale"]
    assert len(stale_entries) <= cap, (
        f"erwartet höchstens {cap} Einträge, got {len(stale_entries)}: {stale_entries}"
    )


# ===========================================================================
# Test 7: Konfigurierbare Schwelle — kleines max_age_seconds → "junges" Item wird alt
# ===========================================================================


def test_configurable_threshold_surfaces_young_item(db_conn):
    """Wenn max_age_seconds sehr klein konfiguriert wird, wird ein Item,
    das bei Default-Schwelle noch jung wäre, als stale angezeigt."""
    conn = db_conn
    t_now = int(time.time())
    tid = _make_done_task(conn, title="Konfigurierbare-Schwelle Task")

    item_age_seconds = 3600  # 1 Stunde alt — jünger als 4-Tage-Default
    _insert_item(
        conn,
        source_task_id=tid,
        typ="follow_up",
        age_seconds=item_age_seconds,
        now=t_now,
    )

    # Ohne Opt-in: sollte NICHT erscheinen (passiver stale-Block default-off)
    result_default = kb.decision_queue(conn, now=t_now)
    entries_default = _stale_entries_for_task(tid, result_default)
    assert len(entries_default) == 0, (
        f"Item sollte ohne Opt-in nicht erscheinen; got {entries_default}"
    )

    # Mit kleiner Schwelle (30 Minuten): sollte erscheinen
    config = {**_STALE_CONFIG, "disposition_stale_max_age_seconds": 1800}
    result_small = kb.decision_queue(conn, now=t_now, config=config)
    entries_small = _stale_entries_for_task(tid, result_small)
    assert len(entries_small) == 1, (
        f"Item sollte bei kleiner Schwelle erscheinen; got {entries_small}"
    )


# ===========================================================================
# Test 8: Fail-soft / Nicht-Regression — ohne alternde Items kein Crash
# ===========================================================================


def test_no_stale_items_no_crash_no_regression(db_conn):
    """Ohne alternde Items wirft decision_queue keine Exception und liefert
    weiterhin andere Kategorien (z.B. sticky_blocked) korrekt zurück."""
    conn = db_conn
    # Sticky-blocked Task anlegen — soll weiterhin erscheinen
    task_id = kb.create_task(conn, title="blocked task", assignee="coder")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, reason="needs human eyes")

    result = kb.decision_queue(conn)

    # sticky_blocked muss weiterhin erscheinen
    kinds = _kinds_for_task(task_id, result)
    assert "sticky_blocked" in kinds, (
        f"sticky_blocked sollte weiterhin kommen; got {kinds}"
    )
    # Kein disposition_stale ohne Ledger-Items
    assert all(d["kind"] != "disposition_stale" for d in result["decisions"]), (
        "disposition_stale sollte ohne Ledger-Items nicht erscheinen"
    )

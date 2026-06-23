"""Tests: Ledger-Harvest (Phase 2b) — disposition_items als primäre Follow-up-Quelle.

Deckt ab:
  1. load_followup_candidates_from_ledger: ein open follow_up-Item → ein Kandidat
  2. typ=risk → nicht geladen; status!=open → nicht; created_at < since_ts → nicht
  3. Titel-Fallback: source_task_id ohne tasks-Zeile → title == source_task_id
  4. run_harvest Merge+Dedup: Ledger-Item gewinnt gegen Keyword-Receipt für selben task_id
  5. run_harvest Backward-Compat: keine Ledger-Items → keyword-fallback wie bisher
"""
from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist


# --------------------------------------------------------------------------- #
# Fixture — isolierter HERMES_HOME (spiegelt test_strategist_harvest.py)
# --------------------------------------------------------------------------- #
@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# --------------------------------------------------------------------------- #
# Helfer
# --------------------------------------------------------------------------- #
def _done_task(conn, *, title, body, created_by="coder", completed_at=None):
    if completed_at is None:
        completed_at = int(time.time())
    tid = kb.create_task(conn, title=title, assignee="coder", created_by=created_by)
    kb.add_comment(conn, tid, "coder", body)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (completed_at, tid),
        )
    return tid


def _insert_follow_up(conn, *, source_task_id, next_action="Schritt A", evidence="Beweis B",
                      disposition="defer", created_at=None):
    """Hilfsfunktion: follow_up-Ledger-Item einsetzen (ggf. created_at überschreiben)."""
    iid = kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ="follow_up",
        disposition=disposition,
        next_action=next_action,
        evidence=evidence,
    )
    if created_at is not None:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE disposition_items SET created_at = ? WHERE id = ?",
                (int(created_at), iid),
            )
    return iid


# --------------------------------------------------------------------------- #
# 1. Basispfad: ein open follow_up-Item → genau ein Kandidat
# --------------------------------------------------------------------------- #
def test_load_ledger_one_follow_up(kanban_home):
    with kb.connect() as conn:
        tid = _done_task(conn, title="Endpoint bauen", body="x" * 50)
        iid = _insert_follow_up(
            conn,
            source_task_id=tid,
            next_action="Nächster Schritt",
            evidence="Linter-Fehler",
        )
        since_ts = int(time.time()) - 3600
        cands = strategist.load_followup_candidates_from_ledger(conn, since_ts=since_ts)

    assert len(cands) == 1
    c = cands[0]
    assert c["task_id"] == tid
    assert c["source"] == "ledger"
    assert c["suggested_key"] == f"disposition-{iid}"
    assert "Nächster Schritt" in c["excerpt"]
    assert c["title"] == "Endpoint bauen"


# --------------------------------------------------------------------------- #
# 2. Negative Filter: typ=risk, status!=open, created_at < since_ts
# --------------------------------------------------------------------------- #
def test_load_ledger_excludes_risk_type(kanban_home):
    with kb.connect() as conn:
        tid = _done_task(conn, title="Risiko-Task", body="x" * 50)
        kb.insert_disposition_item(
            conn,
            source_task_id=tid,
            typ="risk",
            disposition="defer",
        )
        cands = strategist.load_followup_candidates_from_ledger(conn, since_ts=0)
    assert cands == []


def test_load_ledger_excludes_non_open_status(kanban_home):
    with kb.connect() as conn:
        tid = _done_task(conn, title="Closed-Task", body="x" * 50)
        iid = _insert_follow_up(conn, source_task_id=tid)
        # Status auf 'done' setzen
        kb.set_disposition_status(conn, iid, status="accepted")
        cands = strategist.load_followup_candidates_from_ledger(conn, since_ts=0)
    assert cands == []


def test_load_ledger_excludes_old_created_at(kanban_home):
    old_ts = 1000
    since_ts = 5000
    with kb.connect() as conn:
        tid = _done_task(conn, title="Alter Task", body="x" * 50)
        _insert_follow_up(conn, source_task_id=tid, created_at=old_ts)
        cands = strategist.load_followup_candidates_from_ledger(conn, since_ts=since_ts)
    assert cands == []


# --------------------------------------------------------------------------- #
# 3. Titel-Fallback: source_task_id ohne tasks-Zeile → title == source_task_id
# --------------------------------------------------------------------------- #
def test_load_ledger_title_fallback_no_task_row(kanban_home):
    with kb.connect() as conn:
        # Ledger-Item einfügen ohne echten Tasks-Eintrag
        fake_tid = "t_nonexistent_xyz"
        iid = kb.insert_disposition_item(
            conn,
            source_task_id=fake_tid,
            typ="follow_up",
            disposition="defer",
            next_action="Was auch immer",
        )
        cands = strategist.load_followup_candidates_from_ledger(conn, since_ts=0)

    assert len(cands) == 1
    assert cands[0]["title"] == fake_tid


# --------------------------------------------------------------------------- #
# 4. run_harvest Merge+Dedup: Ledger gewinnt über Keyword für selben task_id
# --------------------------------------------------------------------------- #
def test_run_harvest_ledger_wins_over_keyword_for_same_task(kanban_home):
    """Ledger-Item + passender Keyword-Receipt für selbe task_id → 1 Kandidat, source=ledger."""
    body = "RESULT: erledigt. Der Cache-Refactor bleibt outside scope — separater Task. " * 4
    with kb.connect() as conn:
        tid = _done_task(conn, title="Merge-Task", body=body, completed_at=int(time.time()))
        _insert_follow_up(conn, source_task_id=tid, next_action="Ledger-Aktion")

    args = types.SimpleNamespace(board=None)
    result = strategist.run_harvest(args)

    assert result["mode"] == "harvest"
    assert result["ledger_candidates"] == 1
    # keyword_candidates darf > 0 sein (gleicher task hat keyword), aber merge dedupliziert
    state_dir = strategist.default_state_dir()
    cand_data = json.loads((state_dir / "harvest_candidates.json").read_text())
    # Genau 1 Kandidat nach Dedup
    assert len(cand_data["candidates"]) == 1
    assert cand_data["candidates"][0]["source"] == "ledger"
    assert cand_data["candidates"][0]["task_id"] == tid
    # Counter vorhanden
    assert "ledger_candidates" in cand_data
    assert "keyword_candidates" in cand_data


def test_run_harvest_keyword_only_task_appears_as_fallback(kanban_home):
    """Ein keyword-only-Receipt (kein Ledger-Item) erscheint als source=keyword-fallback."""
    body = "RESULT: erledigt. Der Cache-Refactor bleibt outside scope — separater Task. " * 4
    with kb.connect() as conn:
        _done_task(conn, title="Nur-Keyword-Task", body=body, completed_at=int(time.time()))
        # Kein Ledger-Item einfügen

    args = types.SimpleNamespace(board=None)
    result = strategist.run_harvest(args)

    assert result["ledger_candidates"] == 0
    state_dir = strategist.default_state_dir()
    cand_data = json.loads((state_dir / "harvest_candidates.json").read_text())
    assert len(cand_data["candidates"]) >= 1
    sources = {c["source"] for c in cand_data["candidates"]}
    assert "keyword-fallback" in sources
    assert "ledger" not in sources


def test_run_harvest_reaps_worker_drop_items_only(kanban_home):
    """Harvest dismisses explicit worker-drop ledger items, leaving other dispositions open."""
    with kb.connect() as conn:
        tid = _done_task(conn, title="Reaper-Task", body="x" * 50, completed_at=int(time.time()))
        drop_iid = _insert_follow_up(conn, source_task_id=tid, disposition="drop")
        defer_iid = _insert_follow_up(conn, source_task_id=tid, disposition="defer")
        delegate_iid = _insert_follow_up(conn, source_task_id=tid, disposition="delegate")
        risk_iid = kb.insert_disposition_item(
            conn,
            source_task_id=tid,
            typ="risk",
            disposition="defer",
            severity="real-risk",
        )

    strategist.run_harvest(types.SimpleNamespace(board=None))

    with kb.connect() as conn:
        drop_item = kb.get_disposition_item(conn, drop_iid)
        defer_item = kb.get_disposition_item(conn, defer_iid)
        delegate_item = kb.get_disposition_item(conn, delegate_iid)
        risk_item = kb.get_disposition_item(conn, risk_iid)

    assert drop_item is not None
    assert defer_item is not None
    assert delegate_item is not None
    assert risk_item is not None

    assert drop_item["status"] == "dismissed"
    assert drop_item["decided_by"] == "harvest-reaper"
    assert drop_item["decided_at"] is not None

    for item in (defer_item, delegate_item, risk_item):
        assert item["status"] == "open"
        assert item["decided_by"] is None
        assert item["decided_at"] is None


# --------------------------------------------------------------------------- #
# 5. Backward-Compat: keine Ledger-Items → keyword-fallback wie bisher, kein Crash
# --------------------------------------------------------------------------- #
def test_run_harvest_backward_compat_no_ledger(kanban_home):
    """Wenn das Ledger leer ist, verhält sich run_harvest wie vor FRD Phase 2b."""
    body = "RESULT: alles offen. Es gibt remaining items und follow-up work. " * 5
    with kb.connect() as conn:
        _done_task(conn, title="Legacy-Task", body=body, completed_at=int(time.time()))

    args = types.SimpleNamespace(board=None)
    result = strategist.run_harvest(args)

    assert result["mode"] == "harvest"
    assert result["ledger_candidates"] == 0
    assert result["candidates"] >= 1  # Keyword hat was gefunden
    state_dir = strategist.default_state_dir()
    cand_data = json.loads((state_dir / "harvest_candidates.json").read_text())
    assert cand_data["ledger_candidates"] == 0
    for c in cand_data["candidates"]:
        assert c["source"] == "keyword-fallback"


# --------------------------------------------------------------------------- #
# 6. filter_followup_candidates setzt source="keyword-fallback"
# --------------------------------------------------------------------------- #
def test_filter_followup_candidates_sets_source():
    receipts = [
        {"task_id": "t_x", "excerpt": "outside scope — separater Task nötig."},
    ]
    kept = strategist.filter_followup_candidates(receipts)
    assert len(kept) == 1
    assert kept[0]["source"] == "keyword-fallback"
    assert kept[0]["suggested_key"] == "receipt-t_x"


# --------------------------------------------------------------------------- #
# 7. excerpt-Fallback: next_action + evidence beide leer → disposition als excerpt
# --------------------------------------------------------------------------- #
def test_load_ledger_excerpt_fallback_to_disposition(kanban_home):
    with kb.connect() as conn:
        tid = _done_task(conn, title="Leerer-Excerpt-Task", body="x" * 50)
        # next_action und evidence weglassen (beide default leer/None)
        iid = kb.insert_disposition_item(
            conn,
            source_task_id=tid,
            typ="follow_up",
            disposition="defer",
        )
        cands = strategist.load_followup_candidates_from_ledger(conn, since_ts=0)

    assert len(cands) == 1
    # excerpt darf nicht leer sein
    assert cands[0]["excerpt"]
    # Disposition als Fallback
    assert "defer" in cands[0]["excerpt"]

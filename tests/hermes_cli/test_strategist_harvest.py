"""Tests für den Strategist Receipt-Harvest (--mode harvest)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolierter HERMES_HOME mit leerem Board (spiegelt test_kanban_db.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


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


def test_gather_returns_substantive_done_receipt(kanban_home):
    body = "RESULT: 22 von 204 Failures gefixt. " + (
        "Die verbleibenden 182 Failures liegen outside scope und brauchen einen separaten Task. " * 4
    )
    with kb.connect() as conn:
        tid = _done_task(conn, title="Flaky-Tests fixen", body=body)
        receipts = strategist.gather_recent_receipts(conn, since_ts=0)
    assert len(receipts) == 1
    assert receipts[0]["task_id"] == tid
    assert "outside scope" in receipts[0]["excerpt"]


def test_gather_excludes_thin_strategist_and_old(kanban_home):
    with kb.connect() as conn:
        _done_task(conn, title="Dünn", body="zu kurz")  # < 200 Zeichen
        _done_task(
            conn, title="Eigen", body="x" * 250, created_by="strategist-cron"
        )  # Eigenvorschlag
        _done_task(
            conn, title="Alt", body="y" * 250, completed_at=500
        )  # vor since_ts
        receipts = strategist.gather_recent_receipts(conn, since_ts=900)
    assert receipts == []


def test_filter_keeps_only_marker_receipts():
    receipts = [
        {"task_id": "t_a", "excerpt": "Alles erledigt, nichts offen."},
        {"task_id": "t_b", "excerpt": "Die Migration bleibt outside scope — separater Task nötig."},
    ]
    kept = strategist.filter_followup_candidates(receipts)
    assert [c["task_id"] for c in kept] == ["t_b"]
    assert kept[0]["suggested_key"] == "receipt-t_b"


def test_filter_ignores_generic_todo_and_should_be_phrases():
    receipts = [
        {"task_id": "t_a", "excerpt": "Die TODO-Liste wurde vollständig abgearbeitet."},
        {"task_id": "t_b", "excerpt": "The service should be stable after this patch."},
        {"task_id": "t_c", "excerpt": "Die Migration bleibt outside scope — separater Task nötig."},
    ]
    kept = strategist.filter_followup_candidates(receipts)
    assert [c["task_id"] for c in kept] == ["t_c"]


def test_gather_keeps_followup_marker_beyond_2000_chars(kanban_home):
    body = (
        "RESULT: erledigt. "
        + ("a" * 2200)
        + " Die Migration bleibt outside scope — separater Task nötig."
    )
    with kb.connect() as conn:
        tid = _done_task(conn, title="Langer Receipt", body=body)
        receipts = strategist.gather_recent_receipts(conn, since_ts=0)
    assert len(receipts) == 1
    assert receipts[0]["task_id"] == tid
    assert "outside scope" in receipts[0]["excerpt"]


def test_run_harvest_writes_candidates_and_marker(kanban_home):
    import json
    import time
    import types

    body = "RESULT: erledigt. Der Cache-Refactor bleibt outside scope — separater Task. " * 4
    with kb.connect() as conn:
        _done_task(conn, title="Endpoint bauen", body=body, completed_at=int(time.time()))
    args = types.SimpleNamespace(board=None)
    result = strategist.run_harvest(args)
    assert result["mode"] == "harvest"
    assert result["receipts"] == 1
    assert result["candidates"] == 1
    state_dir = strategist.default_state_dir()
    cand = json.loads((state_dir / "harvest_candidates.json").read_text())
    assert cand["candidates"][0]["suggested_key"].startswith("receipt-")
    assert (state_dir / "harvest_last_run.json").exists()


def test_run_harvest_since_uses_marker(kanban_home):
    import json

    state_dir = strategist.default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "harvest_last_run.json").write_text(json.dumps({"ts": 5000}))
    since = strategist._read_harvest_since(state_dir / "harvest_last_run.json", now=9999)
    assert since == 5000
    missing = strategist._read_harvest_since(state_dir / "nope.json", now=9999)
    assert missing == 9999 - strategist.HARVEST_WINDOW_FALLBACK_SECONDS

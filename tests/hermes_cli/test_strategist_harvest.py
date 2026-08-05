"""Tests für den Strategist Receipt-Harvest (--mode harvest)."""
from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist


def _patch_harvest_watch_config(monkeypatch, *, threshold=2, rearm=1):
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "disposition_realrisk_escalate_days": 2,
                "disposition_special_run_threshold": threshold,
                "disposition_special_run_rearm": rearm,
            }
        },
    )


def _open_disposition_items(conn, count: int) -> list[str]:
    ids: list[str] = []
    for idx in range(count):
        tid = kb.create_task(conn, title=f"Source {idx}", assignee="coder", created_by="test")
        ids.append(
            kb.insert_disposition_item(
                conn,
                source_task_id=tid,
                typ="follow_up",
                disposition="defer",
                next_action="do later",
                severity="none",
                evidence="test",
            )
        )
    return ids


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
    assert kept[0]["kind"] == "follow_up"
    assert kept[0]["source_severity"] == "scope-note"
    assert kept[0]["triage_severity"] == "scope-note"


def test_ledger_candidates_emit_deterministic_triage_schema(kanban_home):
    now = 10 * 86400
    cases = [
        ("risk", "real-risk", now - 3600, "real-risk", 0),
        ("risk", "real-risk", now - 3 * 86400, "overdue", 3),
        ("follow_up", "scope-note", now - 3600, "scope-note", 0),
        ("follow_up", "none", now - 3600, "none", 0),
    ]
    with kb.connect() as conn:
        for idx, (typ, severity, created_at, _triage, _age) in enumerate(cases):
            tid = kb.create_task(conn, title=f"Source {idx}", assignee="coder", created_by="test")
            item_id = kb.insert_disposition_item(
                conn,
                source_task_id=tid,
                typ=typ,
                disposition="defer",
                next_action="follow up",
                severity=severity,
                evidence="test",
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE disposition_items SET created_at=? WHERE id=?",
                    (created_at, item_id),
                )
        candidates = strategist.load_followup_candidates_from_ledger(
            conn,
            since_ts=now - 86400,
            realrisk_escalate_days=2,
            now=now,
        )

    by_triage = {c["triage_severity"]: c for c in candidates}
    assert set(by_triage) == {"real-risk", "overdue", "scope-note", "none"}
    for typ, severity, _created_at, triage, age_days in cases:
        cand = by_triage[triage]
        assert cand["kind"] == typ
        assert cand["source_severity"] == severity
        assert cand["severity"] == triage
        assert cand["age_days"] == age_days
        assert cand["overdue"] is (triage == "overdue")


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


def test_run_harvest_reaps_drop_and_excludes_terminal_dispositions(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        source_ids = {
            name: kb.create_task(conn, title=name, assignee="coder", created_by="test")
            for name in ("keep", "drop", "accepted", "task_created", "dismissed")
        }
        keep_id = kb.insert_disposition_item(
            conn,
            source_task_id=source_ids["keep"],
            typ="follow_up",
            disposition="defer",
            next_action="keep visible",
            severity="none",
            evidence="open item",
        )
        drop_id = kb.insert_disposition_item(
            conn,
            source_task_id=source_ids["drop"],
            typ="follow_up",
            disposition="drop",
            next_action="obsolete",
            severity="none",
            evidence="worker decided drop",
        )
        terminal_ids = []
        for status in ("accepted", "task_created", "dismissed"):
            item_id = kb.insert_disposition_item(
                conn,
                source_task_id=source_ids[status],
                typ="follow_up",
                disposition="defer",
                next_action=f"already {status}",
                severity="none",
                evidence="terminal item",
            )
            terminal_ids.append(item_id)
            kb.set_disposition_status(conn, item_id, status=status, decided_by="test")
        with kb.write_txn(conn):
            conn.execute("UPDATE disposition_items SET created_at=?", (now,))

    result = strategist.run_harvest(types.SimpleNamespace(board=None))

    assert result["reaped_dispositions"] == 1
    assert result["ledger_candidates"] == 1
    state_dir = strategist.default_state_dir()
    cand = json.loads((state_dir / "harvest_candidates.json").read_text())
    assert cand["reaped_dispositions"] == 1
    assert [c["disposition_item_id"] for c in cand["candidates"]] == [keep_id]

    with kb.connect() as conn:
        drop_item = kb.get_disposition_item(conn, drop_id)
        keep_item = kb.get_disposition_item(conn, keep_id)
        assert drop_item is not None
        assert keep_item is not None
        assert drop_item["status"] == "dismissed"
        assert drop_item["decided_by"] == "harvest-reaper"
        assert keep_item["status"] == "open"
        assert all(kb.get_disposition_item(conn, item_id) is not None for item_id in terminal_ids)


def test_run_harvest_since_uses_marker(kanban_home):
    import json

    state_dir = strategist.default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "harvest_last_run.json").write_text(json.dumps({"ts": 5000}))
    since = strategist._read_harvest_since(state_dir / "harvest_last_run.json", now=9999)
    assert since == 5000
    missing = strategist._read_harvest_since(state_dir / "nope.json", now=9999)
    assert missing == 9999 - strategist.HARVEST_WINDOW_FALLBACK_SECONDS


def test_harvest_watch_triggers_above_threshold(kanban_home, monkeypatch):
    _patch_harvest_watch_config(monkeypatch, threshold=2, rearm=1)
    monkeypatch.setattr(strategist.time, "time", lambda: 10_000)
    with kb.connect() as conn:
        _open_disposition_items(conn, 3)

    result = strategist.run_harvest_watch(types.SimpleNamespace(board=None))

    assert result["mode"] == "harvest-watch"
    assert result["triggered"] is True
    assert result["open_disposition_items"] == 3
    state_dir = strategist.default_state_dir()
    assert json.loads((state_dir / "harvest_last_run.json").read_text())["ts"] == 10_000
    state = json.loads((state_dir / "harvest_special_run.json").read_text())
    assert state == {"armed": False, "last_special_run_ts": 10_000}


def test_harvest_watch_does_not_fire_again_inside_cooldown(kanban_home, monkeypatch):
    _patch_harvest_watch_config(monkeypatch, threshold=2, rearm=1)
    now = 10_000
    monkeypatch.setattr(strategist.time, "time", lambda: now)
    with kb.connect() as conn:
        item_ids = _open_disposition_items(conn, 3)
    first = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert first["triggered"] is True

    with kb.connect() as conn:
        for item_id in item_ids:
            kb.set_disposition_status(conn, item_id, status="dismissed", decided_by="test")
    monkeypatch.setattr(strategist.time, "time", lambda: now + 60)
    rearm_check = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert rearm_check["reason"] == "below-threshold"

    with kb.connect() as conn:
        _open_disposition_items(conn, 3)
    second = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert second["triggered"] is False
    assert second["reason"] == "cooldown"


def test_harvest_watch_rearms_only_after_count_drops_below_rearm(kanban_home, monkeypatch):
    _patch_harvest_watch_config(monkeypatch, threshold=1, rearm=2)
    now = 10_000
    monkeypatch.setattr(strategist.time, "time", lambda: now)
    with kb.connect() as conn:
        item_ids = _open_disposition_items(conn, 3)
    first = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert first["triggered"] is True

    with kb.connect() as conn:
        kb.set_disposition_status(conn, item_ids[0], status="dismissed", decided_by="test")
    monkeypatch.setattr(
        strategist.time,
        "time",
        lambda: now + strategist.SPECIAL_HARVEST_COOLDOWN_SECONDS + 1,
    )
    still_high = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert still_high["triggered"] is False
    assert still_high["reason"] == "not-rearmed"

    with kb.connect() as conn:
        kb.set_disposition_status(conn, item_ids[1], status="dismissed", decided_by="test")
    low = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert low["reason"] == "below-threshold"

    with kb.connect() as conn:
        _open_disposition_items(conn, 2)
    rearmed = strategist.run_harvest_watch(types.SimpleNamespace(board=None))
    assert rearmed["triggered"] is True
    assert rearmed["open_disposition_items"] == 3

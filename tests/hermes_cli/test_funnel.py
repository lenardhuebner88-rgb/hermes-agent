"""Tests für hermes_cli/funnel.py — Demand-Funnel Cap/Dedupe/Auto-Archiv."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import funnel
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    conn = kb.connect()
    yield conn
    conn.close()


# --- value_class (T5) -------------------------------------------------------


def test_value_class_funnel_sources_are_nutzer():
    for src in kb.FUNNEL_CREATED_BY:
        assert kb.value_class(src) == "nutzer"


def test_value_class_review_chains_are_haertung():
    assert kb.value_class("kanban-review-chain") == "haertung"
    assert kb.value_class("verifier") == "haertung"
    assert kb.value_class("p2-gw-verify") == "haertung"


def test_value_class_rest_is_meta():
    for src in ("dashboard", "user", "worker", "coordinator", None, ""):
        assert kb.value_class(src) == "meta"


# --- wish_key ---------------------------------------------------------------


def test_wish_key_normalizes():
    assert funnel.wish_key("  Mehr   STATISTIK\nbitte ") == "wish:mehr statistik bitte"


# --- create_wish: triage + dedupe + cap --------------------------------------


def test_create_wish_lands_in_triage_with_created_by(conn):
    tid = funnel.create_wish(
        conn, title="Dunkles Theme", body="b", created_by="family",
    )
    task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.created_by == "family"


def test_create_wish_rejects_non_funnel_author(conn):
    with pytest.raises(ValueError):
        funnel.create_wish(conn, title="x", body="b", created_by="dashboard")


def test_create_wish_dedupes_same_wish(conn):
    a = funnel.create_wish(conn, title="Gleicher Wunsch", body="b", created_by="family")
    b = funnel.create_wish(conn, title="gleicher   WUNSCH", body="b", created_by="family")
    assert a == b
    assert len(funnel.open_proposals(conn)) == 1


def test_create_wish_cap_guard(conn):
    for i in range(funnel.FUNNEL_CAP):
        assert funnel.create_wish(
            conn, title=f"wunsch {i}", body="b", created_by="fo-gap-audit",
        ) is not None
    assert funnel.create_wish(
        conn, title="einer zu viel", body="b", created_by="fo-gap-audit",
    ) is None
    assert len(funnel.open_proposals(conn)) == funnel.FUNNEL_CAP


def test_open_proposals_ignores_other_sources_and_statuses(conn):
    funnel.create_wish(conn, title="funnel offen", body="b", created_by="discord-idee")
    kb.create_task(conn, title="normal triage", created_by="dashboard", triage=True)
    accepted_id = funnel.create_wish(conn, title="funnel angenommen", body="b", created_by="family")
    with kb.write_txn(conn):  # Annahme (status→ready) — Filter zählt nur triage
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (accepted_id,))
    titles = [p["title"] for p in funnel.open_proposals(conn)]
    assert titles == ["funnel offen"]


# --- Auto-Archiv (T4) --------------------------------------------------------


def test_archive_stale_archives_only_old_proposals(conn):
    now = int(time.time())
    old_id = funnel.create_wish(conn, title="alter wunsch", body="b", created_by="family")
    fresh_id = funnel.create_wish(conn, title="frischer wunsch", body="b", created_by="family")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (now - 31 * 86400, old_id),
        )

    archived = funnel.archive_stale(conn, now=now)
    assert [p["id"] for p in archived] == [old_id]
    assert kb.get_task(conn, old_id).status == "archived"
    assert kb.get_task(conn, fresh_id).status == "triage"
    # Idempotent: zweiter Lauf archiviert nichts mehr.
    assert funnel.archive_stale(conn, now=now) == []

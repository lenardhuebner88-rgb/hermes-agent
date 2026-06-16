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


# --- Auto-Decomposer-Exemption (Contract: nichts startet ohne Operator-Tap) --


def test_auto_paths_skip_funnel_proposals(conn):
    from hermes_cli import kanban_decompose, kanban_specify

    wish_id = funnel.create_wish(
        conn, title="funnel wartet auf piet", body="b", created_by="fo-gap-audit",
    )
    normal_id = kb.create_task(
        conn, title="normale idee", created_by="dashboard", triage=True,
    )

    for ids in (kanban_decompose.list_triage_ids(), kanban_specify.list_triage_ids()):
        assert normal_id in ids
        assert wish_id not in ids


# --- Freigabe-Pfad: list_drafts + approve_draft -------------------------------


def _make_done_draft(conn, *, created_by="family", title="wunsch mit draft",
                     draft="# Spec-Draft\n" + "x" * 150):
    tid = funnel.create_wish(conn, title=title, body="b", created_by=created_by)
    if draft:
        kb.add_comment(conn, tid, author="coder-claude", body=draft)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), tid),
        )
    return tid


def test_list_drafts_returns_done_funnel_roots_with_excerpt(conn):
    tid = _make_done_draft(conn)
    funnel.create_wish(conn, title="noch offen", body="b", created_by="family")
    kb.create_task(conn, title="normal done", created_by="dashboard")

    drafts = funnel.list_drafts(conn)
    assert [d["id"] for d in drafts] == [tid]
    assert drafts[0]["draft_excerpt"].startswith("# Spec-Draft")


def test_list_drafts_skips_blocked_comments_for_excerpt(conn):
    tid = _make_done_draft(conn, draft="echter draft " + "y" * 150)
    kb.add_comment(conn, tid, author="default", body="BLOCKED: " + "z" * 200)
    (draft,) = funnel.list_drafts(conn)
    assert draft["draft_excerpt"].startswith("echter draft")


def test_approve_draft_creates_linked_parked_child_without_contract(conn):
    tid = _make_done_draft(conn, created_by="discord-idee")
    new_id = funnel.approve_draft(conn, tid)

    child = kb.get_task(conn, new_id)
    assert child.status == "blocked"
    assert child.created_by == "discord-idee"  # Bilanz zählt die Kette einmal
    assert child.assignee == "coder-claude"    # Fallback, Wunsch hatte keinen
    assert child.kind == "code"
    assert child.title.startswith("Umsetzen:")
    assert "Spec-Draft" in (child.body or "")
    events = kb.list_events(conn, new_id)
    kinds = [e.kind for e in events]
    assert "needs_contract_blocked" in kinds
    link = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
        (tid, new_id),
    ).fetchone()
    assert link is not None
    # Root hat jetzt ein Kind -> fällt aus der Freigabe-Liste.
    assert funnel.list_drafts(conn) == []


def test_approve_draft_rejects_double_and_wrong_state(conn):
    tid = _make_done_draft(conn)
    funnel.approve_draft(conn, tid)
    with pytest.raises(ValueError, match="bereits freigegeben"):
        funnel.approve_draft(conn, tid)

    open_id = funnel.create_wish(conn, title="offen", body="b", created_by="family")
    with pytest.raises(ValueError, match="nicht fertig"):
        funnel.approve_draft(conn, open_id)

    normal = kb.create_task(conn, title="kein funnel", created_by="dashboard")
    kb.complete_task(conn, normal, summary="x")
    with pytest.raises(ValueError, match="kein Funnel-Vorschlag"):
        funnel.approve_draft(conn, normal)


def test_dismiss_draft_archives_with_comment(conn):
    tid = _make_done_draft(conn)
    funnel.dismiss_draft(conn, tid)
    assert kb.get_task(conn, tid).status == "archived"
    last = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert "Verworfen" in last["body"]
    assert funnel.list_drafts(conn) == []


def test_dismiss_draft_rejects_already_approved(conn):
    tid = _make_done_draft(conn)
    funnel.approve_draft(conn, tid)
    with pytest.raises(ValueError, match="bereits freigegeben"):
        funnel.dismiss_draft(conn, tid)


# --- Loop-Guard: Build-Kinder dürfen nie wieder zu Drafts werden ---------------
# Regression 2026-06-11: Build-Kinder erben created_by (Wert-Bilanz) und haben
# selbst keine Kinder — sobald eines done war, tauchte es erneut in der
# Freigabe-Queue auf. Jede Freigabe stapelte ein weiteres "Umsetzen: " auf den
# Titel (t_8e26e103 → t_91188cfe → t_2fd31dd7 → t_e7cd8d07).


def _complete(conn, task_id):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), task_id),
        )


def test_list_drafts_excludes_completed_build_children(conn):
    tid = _make_done_draft(conn)
    child_id = funnel.approve_draft(conn, tid)
    kb.add_comment(conn, child_id, author="coder-claude", body="Ergebnis " + "r" * 150)
    _complete(conn, child_id)

    # Weder der freigegebene Root (hat Kind) noch das fertige Build-Kind
    # (hat Eltern) sind freigabefähig — die Queue bleibt leer.
    assert funnel.list_drafts(conn) == []


def test_approve_draft_rejects_completed_build_child(conn):
    tid = _make_done_draft(conn)
    child_id = funnel.approve_draft(conn, tid)
    _complete(conn, child_id)

    with pytest.raises(ValueError, match="kein Funnel-Root"):
        funnel.approve_draft(conn, child_id)
    # Es darf kein Enkel "Umsetzen: Umsetzen: …" entstanden sein.
    grandchild = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ?", (child_id,),
    ).fetchone()
    assert grandchild is None


def test_dismiss_draft_rejects_completed_build_child(conn):
    tid = _make_done_draft(conn)
    child_id = funnel.approve_draft(conn, tid)
    _complete(conn, child_id)

    with pytest.raises(ValueError, match="kein Funnel-Root"):
        funnel.dismiss_draft(conn, child_id)
    assert kb.get_task(conn, child_id).status == "done"  # nicht archiviert


def test_approve_draft_title_prefix_is_idempotent(conn):
    # Familie tippt den Wunsch selbst schon als "Umsetzen: …" in die HermesBar.
    tid = _make_done_draft(conn, title="Umsetzen: Dunkles Theme")
    child = kb.get_task(conn, funnel.approve_draft(conn, tid))
    assert child.title == "Umsetzen: Dunkles Theme"
    assert not child.title.startswith("Umsetzen: Umsetzen:")


def test_save_draft_edit_becomes_canonical_build_text(conn):
    tid = _make_done_draft(conn)

    edited = "# Finale Operator-Version\n\nBitte genau diesen Scope bauen. " + "e" * 150
    refreshed = funnel.save_draft_edit(
        conn,
        tid,
        draft_text=edited,
        operator_note="Mobile zuerst, keine Extra-Features.",
    )

    assert refreshed["id"] == tid
    assert refreshed["operator_edited"] is True
    assert refreshed["draft_text"].startswith("# Operator-edited PlanSpec")
    assert edited in refreshed["draft_text"]
    assert "Operator-Input:" in refreshed["draft_text"]
    assert "Mobile zuerst" in refreshed["draft_text"]

    child = kb.get_task(conn, funnel.approve_draft(conn, tid))
    assert "# Operator-edited PlanSpec" in (child.body or "")
    assert "Bitte genau diesen Scope bauen" in (child.body or "")
    assert "Mobile zuerst" in (child.body or "")


def test_save_draft_edit_rejects_empty_and_already_approved(conn):
    tid = _make_done_draft(conn)

    with pytest.raises(ValueError, match="draft_text"):
        funnel.save_draft_edit(conn, tid, draft_text="   ")

    funnel.approve_draft(conn, tid)
    with pytest.raises(ValueError, match="bereits freigegeben"):
        funnel.save_draft_edit(conn, tid, draft_text="# Zu spät\n" + "z" * 150)


def test_request_revision_requeues_unlinked_root_and_archives_old(conn):
    tid = _make_done_draft(conn, title="PlanSpec verbessern")

    new_id = funnel.request_revision(
        conn,
        tid,
        draft_text="# Zwischenstand\n" + "a" * 150,
        operator_note="Bitte ACs explizit aufnehmen.",
    )

    old = kb.get_task(conn, tid)
    revised = kb.get_task(conn, new_id)
    assert old.status == "archived"
    assert revised.status == "ready"
    assert revised.created_by == old.created_by
    assert revised.title.startswith("Überarbeiten:")
    assert "Bitte ACs explizit aufnehmen" in (revised.body or "")
    assert conn.execute("SELECT 1 FROM task_links WHERE child_id = ?", (new_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM task_links WHERE parent_id = ?", (tid,)).fetchone() is None
    assert funnel.list_drafts(conn) == []

    kb.add_comment(conn, new_id, author="coder-claude", body="# Revised PlanSpec\n" + "r" * 150)
    _complete(conn, new_id)
    drafts = funnel.list_drafts(conn)
    assert [d["id"] for d in drafts] == [new_id]
    assert drafts[0]["draft_text"].startswith("# Revised PlanSpec")


# --- Spend-Disclosure-Gate --------------------------------------------------

_SPEND_DRAFT = (
    "# Benchmark-Spec\n\n"
    "Vergleiche anthropic/claude-fable-5 vs openai/gpt-5.5 via openrouter. "
    + "x" * 150
)

_SPEND_DRAFT_WITH_DISCLOSURE = (
    "# Benchmark-Spec\n\n"
    "Vergleiche anthropic/claude-fable-5 vs openai/gpt-5.5 via openrouter.\n\n"
    "Kosten: ~2€, Provider/Modelle: deepseek via openrouter\n\n"
    + "x" * 150
)

_NORMAL_DRAFT = "# Normale Spec\n\nKeine externen Provider. " + "x" * 150


def _make_done_draft_body(conn, *, draft: str, title: str = "wunsch") -> str:
    """Hilfsfunktion: Funnel-Root mit beliebigem Draft-Kommentar."""
    tid = funnel.create_wish(conn, title=title, body="b", created_by="family")
    kb.add_comment(conn, tid, author="coder-claude", body=draft)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), tid),
        )
    return tid


def test_spend_disclosure_missing_detects_signal_without_disclosure():
    assert funnel.spend_disclosure_missing("Benchmark openrouter", "Vergleich gpt-5.5") is True


def test_spend_disclosure_missing_false_when_disclosure_present():
    assert funnel.spend_disclosure_missing(
        "Benchmark openrouter",
        "Vergleich gpt-5.5\n\nKosten: ~1€",
    ) is False


def test_spend_disclosure_missing_false_without_signal():
    assert funnel.spend_disclosure_missing("Normaler Task", "kein Provider hier") is False


def test_approve_blocks_spend_draft_without_disclosure(conn):
    tid = _make_done_draft_body(conn, draft=_SPEND_DRAFT, title="Benchmark openrouter")
    with pytest.raises(ValueError, match="Kosten"):
        funnel.approve_draft(conn, tid)
    # Task bleibt unfreigegeben — kein Build-Kind darf entstehen.
    assert conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ?", (tid,),
    ).fetchone() is None


def test_approve_passes_spend_draft_with_disclosure(conn):
    tid = _make_done_draft_body(conn, draft=_SPEND_DRAFT_WITH_DISCLOSURE,
                                title="Benchmark mit Kosten-Abschnitt")
    new_id = funnel.approve_draft(conn, tid)
    assert new_id is not None
    assert kb.get_task(conn, new_id).status == "blocked"


def test_approve_passes_normal_draft_without_signal(conn):
    tid = _make_done_draft_body(conn, draft=_NORMAL_DRAFT)
    new_id = funnel.approve_draft(conn, tid)
    assert new_id is not None


def test_draft_dict_spend_alert_true_for_spend_signal_without_disclosure(conn):
    tid = _make_done_draft_body(conn, draft=_SPEND_DRAFT, title="Benchmark openrouter")
    task = kb.get_task(conn, tid)
    d = funnel.draft_dict(conn, task)
    assert d["spend_alert"] is True


def test_draft_dict_spend_alert_false_with_disclosure(conn):
    tid = _make_done_draft_body(conn, draft=_SPEND_DRAFT_WITH_DISCLOSURE,
                                title="Benchmark mit Disclosure")
    task = kb.get_task(conn, tid)
    d = funnel.draft_dict(conn, task)
    assert d["spend_alert"] is False


def test_draft_dict_spend_alert_false_without_signal(conn):
    tid = _make_done_draft_body(conn, draft=_NORMAL_DRAFT)
    task = kb.get_task(conn, tid)
    d = funnel.draft_dict(conn, task)
    assert d["spend_alert"] is False


def test_cost_disclosure_accepts_markdown_formats():
    """Insbesondere das vom Specifier-Prompt vorgeschriebene Format."""
    for line in (
        "**Kosten & Provider:** deepseek via openrouter, ~2 EUR",
        "## Kosten & Provider:",
        "- Kosten: ~2 EUR",
        "**Kosten:** ~2 EUR",
        "Budget: 5 EUR",
    ):
        assert funnel.spend_disclosure_missing(
            "Benchmark openrouter", f"Spec…\n{line}\n",
        ) is False, line


def test_cost_disclosure_rejects_kostenlos_and_prose():
    for text in (
        "Kostenlos: alles lokal",
        "Das kostet nichts, openrouter wird erwähnt",
    ):
        assert funnel.spend_disclosure_missing("Benchmark openrouter", text) is True, text


def test_short_cost_comment_unblocks_approve_and_keeps_spec(conn):
    """Heilungsweg aus der Fehlermeldung: kurzer Kommentar (<120 Zeichen)
    erfüllt das Gate, ersetzt aber den kanonischen Draft NICHT — das
    Build-Kind bekommt weiterhin die Spec, nicht die Kosten-Zeile."""
    tid = _make_done_draft_body(conn, draft=_SPEND_DRAFT, title="Benchmark openrouter")
    with pytest.raises(ValueError, match="Kosten"):
        funnel.approve_draft(conn, tid)

    note = "Kosten: ~2 € — Provider/Modelle: deepseek via openrouter"
    assert len(note) < 120
    kb.add_comment(conn, tid, author="operator", body=note)

    assert funnel.draft_text(conn, tid).startswith("# Benchmark-Spec")
    task = kb.get_task(conn, tid)
    assert funnel.draft_dict(conn, task)["spend_alert"] is False

    new_id = funnel.approve_draft(conn, tid)
    child = kb.get_task(conn, new_id)
    assert "# Benchmark-Spec" in (child.body or "")
    assert note not in (child.body or "")

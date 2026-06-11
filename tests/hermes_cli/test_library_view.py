"""Bibliothek (Programm 3 Phase D/E): Adapter, Redaction, Traversal-Schutz."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import library_view as lv


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (Hausmuster)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
    lv._cron_parse_cache.clear()  # In-Process-Caches nie zwischen Tests teilen
    lv._cron_dir_cache.clear()
    kb.init_db()
    return home


def _write_cron_store(store_dir: Path, *, job_id: str, name: str,
                      filename: str, response: str) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "jobs.json").write_text(json.dumps({
        "jobs": [{
            "id": job_id, "name": name, "enabled": True,
            "prompt": "GEHEIM: dieser Prompt darf nie ausgeliefert werden",
            "schedule": {"kind": "cron", "expr": "30 7 * * *", "display": "30 7 * * *"},
        }],
    }), encoding="utf-8")
    out = store_dir / "output" / job_id
    out.mkdir(parents=True)
    (out / filename).write_text(
        f"# Cron Job: {name}\n\n**Job ID:** {job_id}\n\n## Prompt\n\n"
        f"GEHEIM: dieser Prompt darf nie ausgeliefert werden\n\n"
        f"## Eingebettetes Heading im Prompt\n\nnoch geheim\n\n"
        f"## Response\n\n{response}\n",
        encoding="utf-8",
    )


def test_extract_response_ignores_headings_inside_prompt():
    md = "## Prompt\n\nfoo\n\n## Unterpunkt\nbar\n\n## Response\n\nDer Report."
    assert lv._extract_response(md) == "Der Report."
    assert lv._extract_response("## Prompt\nnur prompt") is None


def test_extract_response_prompt_containing_literal_response_line_never_leaks():
    """Redaction-Härtung: Enthält der PROMPT selbst eine wörtliche
    ``## Response``-Zeile (z.B. weil der Job-Prompt das Output-Format
    dokumentiert), darf kein Prompt-Text ausgeliefert werden — es zählt
    das letzte Vorkommen (der echte Response-Teil steht am Dateiende)."""
    md = (
        "## Prompt\n\nGEHEIM-A\n\n## Response\n\nGEHEIM-B (immer noch Prompt: "
        "so dokumentiert der Job sein eigenes Output-Format)\n\n"
        "## Response\n\nDer echte Report."
    )
    out = lv._extract_response(md)
    assert out == "Der echte Report."
    assert "GEHEIM" not in out


def test_categorize_job_explicit_hint_and_fallback():
    assert lv._categorize_job("342d9529bf9c", "irgendwas") == "news"  # explizit (WM)
    assert lv._categorize_job("ffffffffffff", "Breaking Watch KI") == "news"
    assert lv._categorize_job("ffffffffffff", "Repo Audit nightly") == "wartung"
    assert lv._categorize_job("ffffffffffff", "Morning Digest") == "briefings"


def test_cron_items_multi_store_with_redacted_response(kanban_home):
    """Haupt-Store UND Profil-Store werden gelesen; Body = nur Response-Teil
    (Prompt bleibt draußen); WM-Job landet als Serie unter News."""
    _write_cron_store(
        kanban_home / "cron", job_id="16dd6ac01fc0", name="Morning Digest",
        filename="2026-06-10_07-31-09.md", response="Haupt-Digest-Inhalt.",
    )
    _write_cron_store(
        kanban_home / "profiles" / "research" / "cron",
        job_id="342d9529bf9c", name="WM 2026 Deutschland Morgenbrief",
        filename="2026-06-10_07-30-37.md", response="WM-Morgenbrief-Inhalt.",
    )
    items = lv._collect_cron_items(with_bodies=True)
    assert len(items) == 2
    by_series = {i.series: i for i in items}
    wm = by_series["WM 2026 Deutschland Morgenbrief"]
    assert wm.category == "news"
    assert wm.series_id == "profile:research/342d9529bf9c"
    assert "WM-Morgenbrief-Inhalt." in wm.body_md
    assert "GEHEIM" not in wm.body_md  # Redaction: Prompt nie ausliefern
    main = by_series["Morning Digest"]
    assert main.category == "briefings"
    # Detail-Pfad liefert dasselbe Item über die strenge ID-Auflösung
    detail = lv._get_item(wm.id)
    assert detail is not None and "WM-Morgenbrief-Inhalt." in detail.body_md
    assert "GEHEIM" not in detail.body_md


def test_cron_item_id_traversal_is_rejected(kanban_home):
    _write_cron_store(
        kanban_home / "cron", job_id="16dd6ac01fc0", name="Digest",
        filename="2026-06-10_07-31-09.md", response="x",
    )
    with pytest.raises(ValueError):
        lv._get_item("cron::main::16dd6ac01fc0::../../jobs.json")
    with pytest.raises(ValueError):
        lv._get_item("cron::main::../secrets::2026-06-10_07-31-09.md")
    with pytest.raises(ValueError):
        lv._get_item("cron::../../etc::16dd6ac01fc0::2026-06-10_07-31-09.md")
    with pytest.raises(ValueError):
        lv._get_item("deliverable::t_x::../../../etc/passwd.md")


def test_research_adapter_uses_last_comment(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="Wann beginnt die WM?", tenant="research")
        # Frage ohne Antwort erscheint NICHT in der Bibliothek
        assert lv._collect_research_items(with_bodies=True) == []
        kb.add_comment(conn, t, "research", "Zwischenstand")
        kb.add_comment(conn, t, "research", "## Antwort\nAm 11. Juni 2026.")
    items = lv._collect_research_items(with_bodies=True)
    assert len(items) == 1
    assert items[0].category == "recherchen"
    assert "Am 11. Juni 2026." in items[0].body_md
    detail = lv._get_item(f"research::{t}")
    assert detail is not None
    assert "Frage:" in detail.body_md and "Am 11. Juni 2026." in detail.body_md


def test_list_items_search_and_category_filter(kanban_home):
    _write_cron_store(
        kanban_home / "cron", job_id="16dd6ac01fc0", name="Morning Digest",
        filename="2026-06-10_07-31-09.md",
        response="Heute: Spezialwort Quokkafund im Digest.",
    )
    listing = lv._list_items(None, None, 10)
    assert listing["count"] == 1
    assert listing["items"][0]["preview"].startswith("Heute:")
    assert "body_md" not in listing["items"][0]  # Liste bleibt schlank
    # Suche über den Body findet den Begriff (case-insensitive)
    hit = lv._list_items(None, "quokkafund", 10)
    assert hit["count"] == 1
    miss = lv._list_items(None, "nichtvorhanden", 10)
    assert miss["count"] == 0
    # Kategorie-Filter
    assert lv._list_items("news", None, 10)["count"] == 0
    assert lv._list_items("briefings", None, 10)["count"] == 1


def test_cron_collector_cache_and_per_job_cap(kanban_home):
    """Härtung (b): mtime-Cache liefert identische Ergebnisse, invalidiert
    bei Datei-Änderung, und pro Job werden nur die neuesten
    ``_MAX_OUTPUTS_PER_JOB`` Ausgaben gelesen (Haupt-Store hält >100k)."""
    store = kanban_home / "cron"
    _write_cron_store(
        store, job_id="16dd6ac01fc0", name="Morning Digest",
        filename="2026-06-10_07-31-09.md", response="Erste Ausgabe.",
    )
    out_dir = store / "output" / "16dd6ac01fc0"
    first = lv._collect_cron_items(with_bodies=True)
    assert [i.body_md for i in first] == ["Erste Ausgabe."]
    # 2. Lauf = Cache-Hit: gleiche Sicht, auch ohne Bodies kein Body-Leak
    again = lv._collect_cron_items(with_bodies=False)
    assert [i.preview for i in again] == [i.preview for i in first]
    assert again[0].body_md is None
    # Datei-Änderung (mtime/size) invalidiert den Eintrag
    target = out_dir / "2026-06-10_07-31-09.md"
    target.write_text(
        "## Prompt\n\nGEHEIM\n\n## Response\n\nKorrigierte Ausgabe, länger.\n",
        encoding="utf-8",
    )
    updated = lv._collect_cron_items(with_bodies=True)
    assert [i.body_md for i in updated] == ["Korrigierte Ausgabe, länger."]
    # Cap: 45 Ausgaben → nur die 40 neuesten (lexikalisch = zeitlich) bleiben
    for minute in range(45):
        (out_dir / f"2026-06-11_07-{minute:02d}-00.md").write_text(
            f"## Response\n\nAusgabe {minute}.\n", encoding="utf-8",
        )
    capped = lv._collect_cron_items(with_bodies=False)
    assert len(capped) == lv._MAX_OUTPUTS_PER_JOB
    names = sorted(i.id.rsplit("::", 1)[1] for i in capped)
    assert names[0] == "2026-06-11_07-05-00.md"  # die 5 ältesten + 10-06 fielen raus
    assert "2026-06-10_07-31-09.md" not in set(names)


def test_deliverable_adapter_lists_markdown(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="Build X")
        kb.complete_task(conn, t, summary="done")
    report_dir = kanban_home / "reports" / "by-task" / t
    report_dir.mkdir(parents=True)
    (report_dir / "RESULT.md").write_text("# Ergebnis\nFertig.", encoding="utf-8")
    (report_dir / "data.bin").write_bytes(b"\x00\x01")  # kein Markdown → ignoriert
    items = lv._collect_deliverable_items(with_bodies=True)
    assert len(items) == 1
    assert items[0].category == "arbeit"
    assert items[0].title == "Build X"
    assert "Fertig." in items[0].body_md
    detail = lv._get_item(items[0].id)
    assert detail is not None and "Fertig." in detail.body_md

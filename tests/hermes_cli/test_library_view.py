"""Bibliothek (Programm 3 Phase D/E): Adapter, Redaction, Traversal-Schutz."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import library_view as lv

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
    lv._receipt_parse_cache.clear()
    lv._receipt_dir_cache.clear()
    kb.init_db()
    return home


def _write_cron_store(
    store_dir: Path,
    *,
    job_id: str,
    name: str,
    filename: str,
    response: str,
    prompt: str = "GEHEIM: dieser Prompt darf nie ausgeliefert werden",
    script: str | None = None,
) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "jobs.json").write_text(json.dumps({
        "jobs": [{
            "id": job_id, "name": name, "enabled": True,
            "prompt": prompt,
            "script": script,
            "schedule": {"kind": "cron", "expr": "30 7 * * *", "display": "30 7 * * *"},
        }],
    }), encoding="utf-8")
    out = store_dir / "output" / job_id
    out.mkdir(parents=True)
    (out / filename).write_text(
        f"# Cron Job: {name}\n\n**Job ID:** {job_id}\n\n## Prompt\n\n"
        f"{prompt}\n\n"
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
    # Familien-Morgenbrief (fo-brain-Store) ist explizit gemappt und die
    # Kategorie am category-Param validierbar
    assert lv._categorize_job("e28b8cd87809", "Familien-Morgenbrief") == "familie"
    assert "familie" in lv.CATEGORIES


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


def test_structured_ki_brief_parses_real_response_fixture():
    raw = (FIXTURES_DIR / "ki-modell-brief-2026-07-09.md").read_text(encoding="utf-8")
    body = lv._extract_response(raw)
    assert body is not None

    parsed = lv._parse_structured_model_brief("92adf20dd9bd", body, 1_752_087_600)
    assert parsed is not None
    assert parsed["run_kind"] == "abend"
    assert parsed["top_story"].startswith("OpenAI hat GPT-5.6")
    assert len(parsed["sources"]) == 2  # duplicate URL removed
    assert {item["title"] for item in parsed["model_news"]} == {
        "OpenAI - GPT-5.6 (Sol/Terra/Luna)",
        "Meta - Muse Spark 1.1",
    }
    assert all(item["source_url"].startswith("https://") for item in parsed["model_news"])
    assert len(parsed["watchlist_delta"]) == 1


def test_structured_ki_brief_is_attached_to_list_and_detail(kanban_home):
    response = lv._extract_response(
        (FIXTURES_DIR / "ki-modell-brief-2026-07-09.md").read_text(encoding="utf-8")
    )
    assert response is not None
    store = kanban_home / "profiles" / "research" / "cron"
    _write_cron_store(
        store,
        job_id="92adf20dd9bd",
        name="KI Modell-Brief (Abend)",
        filename="2026-07-09_20-03-53.md",
        response=response,
        script="ki-modell-brief.py",
    )

    listed = lv._list_items("news", None, 10)["items"]
    assert len(listed) == 1
    assert listed[0]["structured"] is True
    assert listed[0]["structured_brief"]["run_kind"] == "abend"
    assert "body_md" not in listed[0]

    detail = lv._get_item(listed[0]["id"])
    assert detail is not None
    payload = detail.as_dict(with_body=True)
    assert payload["structured"] is True
    assert payload["structured_brief"]["model_news"]
    assert payload["body_md"].startswith("**🧠 KI-Modell-Brief")


def test_non_model_cron_keeps_legacy_item_shape(kanban_home):
    _write_cron_store(
        kanban_home / "cron",
        job_id="16dd6ac01fc0",
        name="Morning Digest",
        filename="2026-07-09_08-00-00.md",
        response="**Das Wichtigste zuerst**\n- Legacy bleibt Legacy.\n\n**Quellen**\n- https://example.com",
    )
    item = lv._collect_cron_items(with_bodies=False)[0].as_dict(with_body=False)
    assert "structured" not in item
    assert "structured_brief" not in item


def test_trivial_w_cron_outputs_are_filtered(kanban_home):
    """Throwaway test jobs must not flood Briefings.

    Live regression: a job named "w" with prompt "echo hi" ran every five
    minutes and filled the Lesesaal with "hi" reports.
    """
    _write_cron_store(
        kanban_home / "cron",
        job_id="4c2f1fa423d2",
        name="w",
        filename="2026-06-30_11-01-12.md",
        response="hi",
        prompt="echo hi",
    )
    assert lv._collect_cron_items(with_bodies=True) == []
    assert lv._list_items("briefings", "w", 10)["count"] == 0
    assert lv._get_item("cron::main::4c2f1fa423d2::2026-06-30_11-01-12.md") is None


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


def test_research_detail_returns_none_for_empty_last_comment(kanban_home):
    """Detail-Pfad muss leere letzte Kommentare wie _collect_research_items
    behandeln — sonst liefert er ein Item mit leerem/partial body zurück.

    ``add_comment`` lehnt leere Bodies ab, aber ein direkter DB-Insert (oder
    zukünftige Änderungen) können sie erzeugen — der Lesesaal soll fail-soft
    bleiben."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="Frage ohne Antwort?", tenant="research")
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (t, "research", "", 1),
        )
        conn.commit()
    assert lv._collect_research_items(with_bodies=True) == []
    assert lv._get_item(f"research::{t}") is None


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


def test_list_items_offset_pagination_and_has_more(kanban_home):
    """S6 (Bibliothek-Lesesaal, "Mehr laden"): ``offset`` paginiert über die
    bereits sortierte (neueste-zuerst) Liste, ``has_more`` zeigt an, ob nach
    der aktuellen Seite noch weitere Treffer folgen."""
    store = kanban_home / "cron"
    store.mkdir(parents=True, exist_ok=True)
    jobs = []
    for n in range(5):
        job_id = f"{n:012x}"
        jobs.append({
            "id": job_id, "name": f"Job {n}", "enabled": True,
            "prompt": "GEHEIM", "script": None,
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "display": "0 7 * * *"},
        })
        out = store / "output" / job_id
        out.mkdir(parents=True)
        (out / f"2026-06-{10 + n:02d}_07-00-00.md").write_text(
            f"## Response\n\nAusgabe {n}.\n", encoding="utf-8",
        )
    (store / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    first_page = lv._list_items(None, None, 2, offset=0)
    assert first_page["count"] == 5
    assert len(first_page["items"]) == 2
    assert first_page["has_more"] is True
    # Neueste zuerst (Job 4 wurde zuletzt geschrieben → höchstes Datum).
    assert [i["series"] for i in first_page["items"]] == ["Job 4", "Job 3"]

    second_page = lv._list_items(None, None, 2, offset=2)
    assert second_page["has_more"] is True
    assert [i["series"] for i in second_page["items"]] == ["Job 2", "Job 1"]
    # Seiten überschneiden sich nicht — Ids aus Seite 1 und 2 sind disjunkt.
    first_ids = {i["id"] for i in first_page["items"]}
    second_ids = {i["id"] for i in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)

    last_page = lv._list_items(None, None, 2, offset=4)
    assert last_page["has_more"] is False
    assert [i["series"] for i in last_page["items"]] == ["Job 0"]

    beyond_end = lv._list_items(None, None, 2, offset=10)
    assert beyond_end["items"] == []
    assert beyond_end["has_more"] is False
    assert beyond_end["count"] == 5


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


def test_silent_outputs_are_filtered_fresh_and_from_warm_cache(kanban_home):
    """Entrauschung: [SILENT]-Ausgaben (Selbstauskunft "nichts Neues")
    erscheinen nie im Lesesaal — weder beim Frisch-Parse noch über den
    Cache-Hit-Pfad (Regression: bestehende positive Cache-Einträge müssen
    NACH dem Cache-Read gefiltert werden)."""
    store = kanban_home / "cron"
    _write_cron_store(
        store, job_id="16dd6ac01fc0", name="Evening Kanban Review",
        filename="2026-06-10_21-00-00.md", response="[SILENT]",
    )
    out_dir = store / "output" / "16dd6ac01fc0"
    (out_dir / "2026-06-11_21-00-00.md").write_text(
        "## Response\n\nEchter Abend-Report.\n", encoding="utf-8",
    )
    fresh = lv._collect_cron_items(with_bodies=True)
    assert [i.body_md for i in fresh] == ["Echter Abend-Report."]
    # Warm-Cache-Lauf: SILENT bleibt draußen, der echte Report drin
    warm = lv._collect_cron_items(with_bodies=False)
    assert [i.preview for i in warm] == ["Echter Abend-Report."]
    # Regression Hit-Pfad: ein VOR dem Filter gecachtes SILENT-Item (z.B. aus
    # einer Prozess-Laufzeit vor dem Deploy) darf nicht ausgeliefert werden.
    silent_path = str(out_dir / "2026-06-10_21-00-00.md")
    assert silent_path in lv._cron_parse_cache
    assert lv._cron_parse_cache[silent_path][3] is not None  # positiv gecacht
    # Markervarianten bleiben toleriert (uppercased-Check wie Delivery-Skip)
    (out_dir / "2026-06-12_21-00-00.md").write_text(
        "## Response\n\nKein Update — [silent]\n", encoding="utf-8",
    )
    mixed = lv._collect_cron_items(with_bodies=False)
    assert [i.preview for i in mixed] == ["Echter Abend-Report."]


def test_silent_cron_detail_path_returns_none(kanban_home):
    """Der Detail-Pfad muss [SILENT]-Outputs genauso ablehnen wie die Liste —
    sonst wäre ein direkt erratenes ID ein Umweg um die Redaction-Disziplin."""
    store = kanban_home / "cron"
    _write_cron_store(
        store, job_id="16dd6ac01fc0", name="Evening Kanban Review",
        filename="2026-06-10_21-00-00.md", response="[SILENT]",
    )
    (store / "output" / "16dd6ac01fc0" / "2026-06-11_21-00-00.md").write_text(
        "## Response\n\nEchter Abend-Report.\n", encoding="utf-8",
    )
    items = lv._collect_cron_items(with_bodies=True)
    assert len(items) == 1
    assert "Echter Abend-Report." in items[0].body_md
    assert lv._get_item("cron::main::16dd6ac01fc0::2026-06-10_21-00-00.md") is None
    # Nicht-silente Ausgabe desselben Jobs bleibt per Detail erreichbar.
    detail = lv._get_item("cron::main::16dd6ac01fc0::2026-06-11_21-00-00.md")
    assert detail is not None
    assert "Echter Abend-Report." in detail.body_md


def test_wartung_items_stay_listed(kanban_home):
    """Der SILENT-Filter ist kein Kategorie-Filter: echte wartung-Ausgaben
    bleiben im Lesesaal gelistet (nur das Badge ignoriert sie)."""
    _write_cron_store(
        kanban_home / "cron", job_id="16dd6ac01fc0", name="Repo Audit nightly",
        filename="2026-06-10_03-00-00.md", response="Audit-Befund: alles ok.",
    )
    listing = lv._list_items("wartung", None, 10)
    assert listing["count"] == 1
    assert listing["items"][0]["category"] == "wartung"


def _write_receipt(tmp_home: Path, agent: str, filename: str, *,
                   frontmatter: str = "", body: str = "# Receipt\nInhalt.") -> Path:
    receipts = tmp_home / "vault" / "03-Agents" / agent / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    target = receipts / filename
    target.write_text(
        (f"---\n{frontmatter}\n---\n{body}\n" if frontmatter else f"{body}\n"),
        encoding="utf-8",
    )
    return target


def test_receipt_adapter_frontmatter_meta_and_failsoft(kanban_home, tmp_path):
    """Receipts: Frontmatter wird abgetrennt und als Meta-Zeile gerendert;
    frontmatterlose Dateien erscheinen fail-soft (Titel = H1 → Dateiname)."""
    _write_receipt(
        tmp_path, "Claude-Code", "haertung-receipt.md",
        frontmatter='agent: claude-code\nstatus: done\ndate: 2026-06-11\ntask: "Härtungs-Lauf"',
        body="# Receipt — Härtungs-Lauf\n\nStep-Ledger …",
    )
    _write_receipt(
        tmp_path, "Codex", "2026-05-04_ohne-frontmatter.md",
        body="Nur Fließtext ohne Heading.",
    )
    items = lv._collect_receipt_items(with_bodies=True)
    assert {i.series for i in items} == {"Claude-Code", "Codex"}
    cc = next(i for i in items if i.series == "Claude-Code")
    assert cc.category == "receipts"
    assert cc.title == "Receipt — Härtungs-Lauf"
    assert cc.body_md.startswith("> **status:** done · **task:** Härtungs-Lauf · **date:** 2026-06-11")
    assert "agent: claude-code" not in cc.body_md  # Frontmatter nicht roh im Body
    cx = next(i for i in items if i.series == "Codex")
    assert cx.title == "2026-05-04_ohne-frontmatter"  # Fallback: Dateiname
    assert "Fließtext" in cx.body_md and not cx.body_md.startswith(">")
    # Detail-Pfad über die strenge ID-Auflösung
    detail = lv._get_item(cc.id)
    assert detail is not None and "Step-Ledger" in detail.body_md


def test_receipt_adapter_traversal_and_extension_guards(kanban_home, tmp_path):
    _write_receipt(tmp_path, "Hermes", "echt.md")
    secret = tmp_path / "vault" / "03-Agents" / "Hermes" / "geheim.md"
    secret.write_text("# nicht im Regal", encoding="utf-8")
    receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    (receipts / "link.md").symlink_to(secret)
    (receipts / "inner_link.md").symlink_to(receipts / "echt.md")
    (receipts / "notiz.txt").write_text("kein markdown", encoding="utf-8")
    items = lv._collect_receipt_items(with_bodies=False)
    assert [i.id for i in items] == ["receipt::Hermes::echt.md"]  # Symlink + .txt draußen
    with pytest.raises(ValueError):
        lv._get_item("receipt::Hermes::../geheim.md")
    with pytest.raises(ValueError):
        lv._get_item("receipt::../00-Canon::echt.md")
    with pytest.raises(ValueError):
        lv._get_item("receipt::Hermes::echt.txt")
    # Symlinks are rejected by the detail path regardless of where they point.
    assert lv._get_item("receipt::Hermes::link.md") is None
    assert lv._get_item("receipt::Hermes::inner_link.md") is None


def test_receipt_adapter_cap_and_cache(kanban_home, tmp_path):
    """Newest-200-Cap für flache Agent-Receipts (mtime-Reihenfolge) + Cache-Hit liefert
    identische Items."""
    import os
    receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    for n in range(250):
        p = _write_receipt(tmp_path, "Hermes", f"receipt-{n:03d}.md",
                           body=f"# R{n}\nInhalt {n}.")
        os.utime(p, (1_700_000_000 + n, 1_700_000_000 + n))
    os.utime(receipts, (1_700_000_300, 1_700_000_300))
    first = lv._collect_receipt_items(with_bodies=False)
    assert len(first) == lv._MAX_RECEIPTS_FLAT
    names = {i.id.rsplit("::", 1)[1] for i in first}
    assert "receipt-249.md" in names and "receipt-049.md" not in names  # 50 älteste raus
    warm = lv._collect_receipt_items(with_bodies=True)
    assert {i.id for i in warm} == {i.id for i in first}
    assert all(i.body_md for i in warm)
    # Parse-Cache ist gefüllt und liefert beim Hit dasselbe Item-Objekt
    sample = str(receipts / "receipt-249.md")
    assert lv._receipt_parse_cache[sample][2] is not None


def test_library_view_receipts_subdirs(kanban_home, tmp_path):
    """Receipts: flach + auto/ + mother/ haben je ein eigenes Cap und
    Subdir-mtime invalidiert den Dir-Cache auch bei unverändertem Parent."""
    import os

    receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    auto = receipts / "auto"
    mother = receipts / "mother"
    auto.mkdir(parents=True)
    mother.mkdir()

    for n in range(3):
        p = _write_receipt(tmp_path, "Hermes", f"flat-{n}.md", body=f"# Flat {n}\n")
        os.utime(p, (1_700_000_000 + n, 1_700_000_000 + n))
    for n in range(3):
        p = auto / f"auto-{n}.md"
        p.write_text(f"# Auto {n}\n", encoding="utf-8")
        os.utime(p, (1_700_000_100 + n, 1_700_000_100 + n))
    for n in range(2):
        p = mother / f"mother-{n}.md"
        p.write_text(f"# Mother {n}\n", encoding="utf-8")
        os.utime(p, (1_700_000_200 + n, 1_700_000_200 + n))

    os.utime(receipts, (1_700_001_000, 1_700_001_000))
    os.utime(auto, (1_700_001_100, 1_700_001_100))
    os.utime(mother, (1_700_001_200, 1_700_001_200))

    names = lv._newest_receipt_names(receipts)
    assert len(names) == 8
    assert {n for n in names if "/" not in n} == {f"flat-{n}.md" for n in range(3)}
    assert {n for n in names if n.startswith("auto/")} == {f"auto/auto-{n}.md" for n in range(3)}
    assert {n for n in names if n.startswith("mother/")} == {f"mother/mother-{n}.md" for n in range(2)}

    items = lv._collect_receipt_items(with_bodies=False)
    assert {i.title for i in items} == {
        "Flat 0", "Flat 1", "Flat 2",
        "Auto 0", "Auto 1", "Auto 2",
        "Mother 0", "Mother 1",
    }
    auto_item = next(i for i in items if i.title == "Auto 2")
    assert auto_item.id == "receipt::Hermes::auto/auto-2.md"
    assert auto_item.source_ref == "receipt:Hermes/auto/auto-2.md"
    auto_detail = lv._get_item(auto_item.id)
    assert auto_detail is not None and auto_detail.title == "Auto 2"
    mother_item = next(i for i in items if i.title == "Mother 1")
    assert mother_item.id == "receipt::Hermes::mother/mother-1.md"
    mother_detail = lv._get_item(mother_item.id)
    assert mother_detail is not None and mother_detail.title == "Mother 1"
    # "other/" is now a valid subdir name (dynamic scanning), but the file
    # doesn't exist -> returns None (not ValueError).
    assert lv._get_item("receipt::Hermes::other/mother-1.md") is None
    with pytest.raises(ValueError):
        lv._get_item("receipt::Hermes::mother/nested/mother-1.md")

    lv._receipt_dir_cache.clear()
    for n in range(210):
        p = _write_receipt(tmp_path, "Hermes", f"cap-flat-{n:03d}.md", body=f"# Cap Flat {n}\n")
        os.utime(p, (1_700_010_000 + n, 1_700_010_000 + n))
        p = auto / f"cap-auto-{n:03d}.md"
        p.write_text(f"# Cap Auto {n}\n", encoding="utf-8")
        os.utime(p, (1_700_020_000 + n, 1_700_020_000 + n))
    os.utime(receipts, (1_700_021_000, 1_700_021_000))
    os.utime(auto, (1_700_021_100, 1_700_021_100))
    capped = lv._newest_receipt_names(receipts)
    assert sum(1 for n in capped if "/" not in n) == lv._MAX_RECEIPTS_FLAT
    assert sum(1 for n in capped if n.startswith("auto/")) == lv._MAX_RECEIPTS_PER_SUBDIR
    assert "cap-flat-209.md" in capped and "cap-flat-009.md" not in capped
    assert "auto/cap-auto-209.md" in capped and "auto/cap-auto-009.md" not in capped

    cached = lv._newest_receipt_names(receipts)
    assert cached == capped
    new_auto = auto / "new-auto.md"
    new_auto.write_text("# New Auto\n", encoding="utf-8")
    os.utime(new_auto, (1_700_030_000, 1_700_030_000))
    os.utime(auto, (1_700_030_100, 1_700_030_100))
    os.utime(receipts, (1_700_021_000, 1_700_021_000))
    refreshed = lv._newest_receipt_names(receipts)
    assert "auto/new-auto.md" in refreshed


def test_library_view_receipt_detail_rejects_symlinked_allowlisted_subdir(kanban_home, tmp_path):
    receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    real_auto = receipts / "real-auto"
    real_auto.mkdir(parents=True)
    (real_auto / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
    (receipts / "auto").symlink_to(real_auto, target_is_directory=True)

    # The symlinked "auto/" subdir is excluded (symlink exclusion policy).
    # The real "real-auto/" subdir IS scanned (it's a legitimate non-symlinked dir).
    names = lv._newest_receipt_names(receipts)
    assert not any(n.startswith("auto/") for n in names),         "symlinked auto/ must be excluded"
    assert any("real-auto/hidden.md" in n for n in names),         "real non-symlinked subdir real-auto/ is scanned"
    # Detail read via the symlinked path is still blocked.
    assert lv._get_item("receipt::Hermes::auto/hidden.md") is None


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
    assert items[0].category == "receipts"
    assert items[0].series == "Arbeitsergebnisse"
    assert items[0].title == "Build X"
    assert "Fertig." in items[0].body_md
    detail = lv._get_item(items[0].id)
    assert detail is not None and "Fertig." in detail.body_md


def test_lesesaal_merges_real_format_deliverables_and_agent_receipts(
    kanban_home, tmp_path,
):
    """Realformat-Regressionspfad: by-task-Report und Vault-Receipt werden
    ueber den echten Lesesaal-Aggregator in genau eine Kategorie gemergt.

    Die Herkunft bleibt als Serie erhalten: Arbeitsergebnis versus Agent.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Kategorien-Merge")
        kb.complete_task(conn, task_id, summary="done")

    report_dir = kanban_home / "reports" / "by-task" / task_id
    report_dir.mkdir(parents=True)
    (report_dir / "RESULT.md").write_text(
        "# Kategorien-Merge\n\nBackend und Frontend sind belegt.\n",
        encoding="utf-8",
    )
    _write_receipt(
        tmp_path,
        "Codex",
        "2026-07-22-kategorien-merge-receipt.md",
        frontmatter=(
            "agent: codex\n"
            "status: done\n"
            "date: 2026-07-22\n"
            f"task: {task_id}"
        ),
        body="# P8a Receipt\n\nEchte Receipt-Struktur mit Frontmatter.",
    )

    listing = lv._list_items(category=None, q=None, limit=200)
    by_id = {item["id"]: item for item in listing["items"]}
    deliverable = by_id[f"deliverable::{task_id}::RESULT.md"]
    receipt = by_id[
        "receipt::Codex::2026-07-22-kategorien-merge-receipt.md"
    ]

    assert listing["categories"].count("receipts") == 1
    assert "arbeit" not in listing["categories"]
    assert {deliverable["category"], receipt["category"]} == {"receipts"}
    assert deliverable["series"] == "Arbeitsergebnisse"
    assert deliverable["source_ref"] == f"task:{task_id}/RESULT.md"
    assert receipt["series"] == "Codex"
    assert receipt["source_ref"].startswith("receipt:Codex/")


def test_deliverable_adapter_caps_newest_markdown(kanban_home):
    """Wenn ein Task mehr als _DELIVERABLE_MAX_PER_TASK Markdown-Dateien hat,
    müssen die neuesten (mtime) behalten werden — nicht die alphabetisch ersten."""
    import os
    with kb.connect() as conn:
        t = kb.create_task(conn, title="Build X")
        kb.complete_task(conn, t, summary="done")
    report_dir = kanban_home / "reports" / "by-task" / t
    report_dir.mkdir(parents=True)
    for n in range(5):
        p = report_dir / f"report-{n:02d}.md"
        p.write_text(f"# Report {n}\nInhalt {n}.", encoding="utf-8")
        os.utime(p, (1_700_000_000 + n, 1_700_000_000 + n))
    items = lv._collect_deliverable_items(with_bodies=True)
    assert len(items) == lv._DELIVERABLE_MAX_PER_TASK
    names = {i.id.rsplit("::", 1)[1] for i in items}
    assert {"report-02.md", "report-03.md", "report-04.md"} == names


def test_library_deliverable_artifacts_scan(kanban_home, tmp_path):
    vault = tmp_path / "vault"
    deliverables = vault / "03-Agents" / "Hermes" / "deliverables"
    deliverables.mkdir(parents=True)
    valid = deliverables / "test.md"
    valid.write_text("# Vault Deliverable\nSichtbar.", encoding="utf-8")
    tmp_md = tmp_path / "test.md"
    tmp_md.write_text("# Non-Vault\nIgnorieren.", encoding="utf-8")

    many = []
    for n in range(10):
        target = deliverables / f"cap-{n}.md"
        target.write_text(f"# Cap {n}\n", encoding="utf-8")
        many.append(str(target))

    receipt_dir = vault / "03-Agents" / "Hermes" / "receipts" / "auto"
    receipt_dir.mkdir(parents=True)
    receipt_artifact = receipt_dir / "t_foo.md"
    receipt_artifact.write_text("# Already a Receipt\n", encoding="utf-8")

    with kb.connect() as conn:
        t_valid = kb.create_task(conn, title="Artifact Task")
        kb.complete_task(conn, t_valid, summary="done", metadata={
            "artifacts": [str(valid), "/etc/passwd", str(tmp_md)],
        })
        t_many = kb.create_task(conn, title="Many Artifacts")
        kb.complete_task(conn, t_many, summary="done", metadata={"artifacts": many})
        t_receipt = kb.create_task(conn, title="Receipt Duplicate")
        kb.complete_task(conn, t_receipt, summary="done", metadata={
            "artifacts": [str(receipt_artifact)],
        })
        t_none = kb.create_task(conn, title="No Metadata")
        kb.complete_task(conn, t_none, summary="done")
        t_bad = kb.create_task(conn, title="Bad Metadata")
        kb.complete_task(conn, t_bad, summary="done", metadata={"artifacts": [str(valid)]})
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE task_id = ?",
            ("not json artifacts", t_bad),
        )

    items = lv._collect_deliverable_items(with_bodies=True)
    ids = {i.id for i in items}
    assert f"deliverable::{t_valid}::test.md" in ids
    assert all("passwd" not in i.id for i in items)
    valid_item = next(i for i in items if i.id == f"deliverable::{t_valid}::test.md")
    assert valid_item.body_md is not None and valid_item.body_md.endswith("Sichtbar.")
    detail = lv._get_item(f"deliverable::{t_valid}::test.md")
    assert detail is not None and detail.body_md is not None
    assert detail.body_md.endswith("Sichtbar.")
    assert sum(i.id.startswith(f"deliverable::{t_many}::") for i in items) == 3
    assert f"deliverable::{t_receipt}::t_foo.md" not in ids
    assert all(not i.id.startswith(f"deliverable::{t_none}::") for i in items)
    assert all(not i.id.startswith(f"deliverable::{t_bad}::") for i in items)


def test_deliverable_artifact_read_by_name_scans_all_artifacts(kanban_home, tmp_path):
    """Detail-Pfad für Artifact-Deliverables darf nicht nach den ersten
    `_DELIVERABLE_MAX_PER_TASK` Einträgen aufhören — ein Name jenseits der
    Listen-Cap muss auffindbar sein."""
    vault = tmp_path / "vault"
    deliverables = vault / "03-Agents" / "Hermes" / "deliverables"
    deliverables.mkdir(parents=True)

    artifacts: list[str] = []
    for n in range(5):
        target = deliverables / f"cap-{n}.md"
        target.write_text(f"# Cap {n}\nInhalt {n}.", encoding="utf-8")
        artifacts.append(str(target))

    with kb.connect() as conn:
        t = kb.create_task(conn, title="Artifact Beyond Cap")
        kb.complete_task(conn, t, summary="done", metadata={"artifacts": artifacts})

    detail = lv._get_item(f"deliverable::{t}::cap-4.md")
    assert detail is not None and detail.body_md is not None
    assert "Cap 4" in detail.body_md


# ---------------------------------------------------------------------------
# SLICE 1: receipt indexer scans all subdirs (not just auto/mother)
# ---------------------------------------------------------------------------

def test_receipt_scan_includes_all_subdirs(tmp_path, monkeypatch):
    """Regression: the receipt indexer must scan every immediate subdir under
    <agent>/receipts/, not just a fixed allowlist. This test mirrors the live
    on-disk layout: <agent>/receipts/<subdir>/<YYYY-MM-DD-...>.md."""
    # Set up isolated home
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    lv._receipt_parse_cache.clear()
    lv._receipt_dir_cache.clear()

    # Mirror live layout: ~/vault/03-Agents/Hermes/receipts/<subdir>/*.md
    agent_receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    agent_receipts.mkdir(parents=True)

    # Traditional subdirs (auto, mother) — must still work
    (agent_receipts / "auto").mkdir()
    (agent_receipts / "auto" / "2026-07-20-auto-receipt.md").write_text(
        "# Auto Receipt\n\nBody.\n", encoding="utf-8"
    )
    (agent_receipts / "mother").mkdir()
    (agent_receipts / "mother" / "2026-07-19-mother-receipt.md").write_text(
        "# Mother Receipt\n\nBody.\n", encoding="utf-8"
    )

    # New subdirs that were previously invisible — must now be scanned
    (agent_receipts / "rca").mkdir()
    (agent_receipts / "rca" / "2026-07-18-rca-analysis.md").write_text(
        "# RCA Analysis\n\nRoot cause.\n", encoding="utf-8"
    )
    (agent_receipts / "_inbox").mkdir()
    (agent_receipts / "_inbox" / "2026-07-17-inbox-item.md").write_text(
        "# Inbox Item\n\nPending.\n", encoding="utf-8"
    )
    (agent_receipts / "terminal-tab-redesign-2026-07-09").mkdir()
    (agent_receipts / "terminal-tab-redesign-2026-07-09" / "2026-07-09-task-receipt.md").write_text(
        "# Terminal Redesign\n\nTask doc.\n", encoding="utf-8"
    )

    # Symlinked subdir — must be EXCLUDED (security policy)
    real_dir = tmp_path / "real_receipts_dir"
    real_dir.mkdir()
    (real_dir / "2026-07-16-symlinked.md").write_text(
        "# Symlinked\n\nShould not appear.\n", encoding="utf-8"
    )
    (agent_receipts / "symlinked-dir").symlink_to(real_dir)

    # Empty subdir — must not add noise (no .md files)
    (agent_receipts / "empty-subdir").mkdir()

    # Scan
    names = lv._newest_receipt_names(agent_receipts)

    # Assert: receipts from all real subdirs are included
    assert any("rca/2026-07-18-rca-analysis.md" in n for n in names), \
        "rca/ subdir should be scanned"
    assert any("_inbox/2026-07-17-inbox-item.md" in n for n in names), \
        "_inbox/ subdir should be scanned"
    assert any("terminal-tab-redesign-2026-07-09/2026-07-09-task-receipt.md" in n for n in names), \
        "per-task subdir should be scanned"
    assert any("auto/2026-07-20-auto-receipt.md" in n for n in names), \
        "traditional auto/ subdir should still work"
    assert any("mother/2026-07-19-mother-receipt.md" in n for n in names), \
        "traditional mother/ subdir should still work"

    # Assert: symlinked subdir is EXCLUDED
    assert not any("symlinked-dir" in n for n in names), \
        "symlinked subdir must be excluded"


def test_receipt_scan_old_allowlist_would_have_missed_rca(tmp_path, monkeypatch):
    """Prove the OLD fixed-allowlist behavior would have missed rca/ subdir.
    This is a regression-intent test: if someone reverts to a fixed allowlist,
    this test will fail."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    lv._receipt_parse_cache.clear()
    lv._receipt_dir_cache.clear()

    agent_receipts = tmp_path / "vault" / "03-Agents" / "TestAgent" / "receipts"
    agent_receipts.mkdir(parents=True)
    (agent_receipts / "rca").mkdir()
    (agent_receipts / "rca" / "2026-07-20-critical-rca.md").write_text(
        "# Critical RCA\n\nImportant finding.\n", encoding="utf-8"
    )

    names = lv._newest_receipt_names(agent_receipts)

    # The OLD behavior (only auto/mother) would have returned [] for this tree.
    # The NEW behavior must include the rca/ receipt.
    assert any("rca/2026-07-20-critical-rca.md" in n for n in names), \
        "rca/ must be scanned (old allowlist would have missed it)"


def test_receipt_scan_respects_per_subdir_cap(tmp_path, monkeypatch):
    """Ensure the per-subdir cap is applied correctly (200 files max per subdir)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    lv._receipt_parse_cache.clear()
    lv._receipt_dir_cache.clear()

    agent_receipts = tmp_path / "vault" / "03-Agents" / "CapTest" / "receipts"
    agent_receipts.mkdir(parents=True)
    (agent_receipts / "large-subdir").mkdir()

    # Create 250 receipts (exceeds the 200 cap)
    for i in range(250):
        (agent_receipts / "large-subdir" / f"2026-07-{i+1:02d}-receipt.md").write_text(
            f"# Receipt {i}\n\nBody.\n", encoding="utf-8"
        )

    names = lv._newest_receipt_names(agent_receipts)

    # Count how many are from large-subdir
    subdir_count = sum(1 for n in names if n.startswith("large-subdir/"))
    assert subdir_count == 200, \
        f"per-subdir cap should limit to 200, got {subdir_count}"


def test_receipt_full_pipeline_includes_newly_scanned_subdirs(tmp_path, monkeypatch):
    """Full-pipeline regression: receipts from rca/, _inbox/ and per-task
    subdirs must survive through _collect_receipt_items (not just the raw
    file walk) AND be readable via the detail path (_get_item), proving the
    _valid_receipt_relpath guard at the detail-read layer is also relaxed."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
    lv._receipt_parse_cache.clear()
    lv._receipt_dir_cache.clear()

    agent_receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    agent_receipts.mkdir(parents=True)

    # Per-task subdir (previously invisible under the old allowlist)
    task_dir = agent_receipts / "terminal-tab-redesign-2026-07-09"
    task_dir.mkdir()
    (task_dir / "2026-07-09-task-doc.md").write_text(
        "# Terminal Tab Redesign\n\nTask body text.\n", encoding="utf-8"
    )

    # rca/ subdir
    rca_dir = agent_receipts / "rca"
    rca_dir.mkdir()
    (rca_dir / "2026-07-18-root-cause.md").write_text(
        "# Root Cause Analysis\n\nRCA body.\n", encoding="utf-8"
    )

    # _inbox/ subdir
    inbox_dir = agent_receipts / "_inbox"
    inbox_dir.mkdir()
    (inbox_dir / "2026-07-17-pending.md").write_text(
        "# Pending Inbox\n\nInbox body.\n", encoding="utf-8"
    )

    # Symlinked subdir — must be excluded at every layer
    real_outside = tmp_path / "outside"
    real_outside.mkdir()
    (real_outside / "2026-07-16-leaked.md").write_text(
        "# Leaked\n\nShould never appear.\n", encoding="utf-8"
    )
    (agent_receipts / "symlinked-outside").symlink_to(real_outside)

    # --- FULL COLLECTOR ---
    items = lv._collect_receipt_items(with_bodies=True)
    titles = {i.title for i in items}
    ids = {i.id for i in items}

    # Assert: receipts from newly-visible subdirs appear in the final output
    assert "Terminal Tab Redesign" in titles, \
        "per-task subdir receipt must survive the full pipeline"
    assert "Root Cause Analysis" in titles, \
        "rca/ receipt must survive the full pipeline"
    assert "Pending Inbox" in titles, \
        "_inbox/ receipt must survive the full pipeline"

    # Assert: category is 'receipts' (existing convention, not a new bucket)
    for item in items:
        assert item.category == "receipts"

    # Assert: symlinked subdir is excluded
    assert "Leaked" not in titles
    assert not any("symlinked-outside" in i.id for i in items)

    # --- DETAIL READ PATH (_get_item via _valid_receipt_relpath) ---
    # This is the guard the coordinator flagged: if _valid_receipt_relpath
    # still checked against the old fixed allowlist, these would return None.
    task_detail = lv._get_item(
        "receipt::Hermes::terminal-tab-redesign-2026-07-09/2026-07-09-task-doc.md"
    )
    assert task_detail is not None, \
        "detail read must accept per-task subdir (not just auto/mother)"
    assert task_detail.title == "Terminal Tab Redesign"

    rca_detail = lv._get_item("receipt::Hermes::rca/2026-07-18-root-cause.md")
    assert rca_detail is not None, \
        "detail read must accept rca/ subdir"
    assert rca_detail.title == "Root Cause Analysis"

    inbox_detail = lv._get_item("receipt::Hermes::_inbox/2026-07-17-pending.md")
    assert inbox_detail is not None, \
        "detail read must accept _inbox/ subdir"

    # Symlinked subdir detail read still blocked
    symlink_detail = lv._get_item(
        "receipt::Hermes::symlinked-outside/2026-07-16-leaked.md"
    )
    assert symlink_detail is None, \
        "symlinked subdir must remain blocked at detail layer"

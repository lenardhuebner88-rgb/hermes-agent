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
    lv._receipt_parse_cache.clear()
    lv._receipt_dir_cache.clear()
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
    (receipts / "notiz.txt").write_text("kein markdown", encoding="utf-8")
    items = lv._collect_receipt_items(with_bodies=False)
    assert [i.id for i in items] == ["receipt::Hermes::echt.md"]  # Symlink + .txt draußen
    with pytest.raises(ValueError):
        lv._get_item("receipt::Hermes::../geheim.md")
    with pytest.raises(ValueError):
        lv._get_item("receipt::../00-Canon::echt.md")
    with pytest.raises(ValueError):
        lv._get_item("receipt::Hermes::echt.txt")
    with pytest.raises(ValueError):
        lv._get_item("receipt::Hermes::link.md")  # Symlink-Ziel liegt außerhalb


def test_receipt_adapter_cap_and_cache(kanban_home, tmp_path):
    """Newest-40-Cap pro Agent (mtime-Reihenfolge) + Cache-Hit liefert
    identische Items."""
    import os
    receipts = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts"
    for n in range(45):
        p = _write_receipt(tmp_path, "Hermes", f"receipt-{n:02d}.md",
                           body=f"# R{n}\nInhalt {n}.")
        os.utime(p, (1_700_000_000 + n, 1_700_000_000 + n))
    os.utime(receipts, (1_700_000_100, 1_700_000_100))
    first = lv._collect_receipt_items(with_bodies=False)
    assert len(first) == lv._MAX_RECEIPTS_PER_AGENT
    names = {i.id.rsplit("::", 1)[1] for i in first}
    assert "receipt-44.md" in names and "receipt-04.md" not in names  # 5 älteste raus
    warm = lv._collect_receipt_items(with_bodies=True)
    assert {i.id for i in warm} == {i.id for i in first}
    assert all(i.body_md for i in warm)
    # Parse-Cache ist gefüllt und liefert beim Hit dasselbe Item-Objekt
    sample = str(receipts / "receipt-44.md")
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

    lv._receipt_dir_cache.clear()
    for n in range(50):
        p = _write_receipt(tmp_path, "Hermes", f"cap-flat-{n:02d}.md", body=f"# Cap Flat {n}\n")
        os.utime(p, (1_700_010_000 + n, 1_700_010_000 + n))
        p = auto / f"cap-auto-{n:02d}.md"
        p.write_text(f"# Cap Auto {n}\n", encoding="utf-8")
        os.utime(p, (1_700_020_000 + n, 1_700_020_000 + n))
    os.utime(receipts, (1_700_021_000, 1_700_021_000))
    os.utime(auto, (1_700_021_100, 1_700_021_100))
    capped = lv._newest_receipt_names(receipts)
    assert sum(1 for n in capped if "/" not in n) == lv._MAX_RECEIPTS_FLAT
    assert sum(1 for n in capped if n.startswith("auto/")) == lv._MAX_RECEIPTS_PER_SUBDIR
    assert "cap-flat-49.md" in capped and "cap-flat-09.md" not in capped
    assert "auto/cap-auto-49.md" in capped and "auto/cap-auto-09.md" not in capped

    cached = lv._newest_receipt_names(receipts)
    assert cached == capped
    new_auto = auto / "new-auto.md"
    new_auto.write_text("# New Auto\n", encoding="utf-8")
    os.utime(new_auto, (1_700_030_000, 1_700_030_000))
    os.utime(auto, (1_700_030_100, 1_700_030_100))
    os.utime(receipts, (1_700_021_000, 1_700_021_000))
    refreshed = lv._newest_receipt_names(receipts)
    assert "auto/new-auto.md" in refreshed


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

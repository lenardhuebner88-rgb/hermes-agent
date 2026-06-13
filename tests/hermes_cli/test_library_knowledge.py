"""Bibliothek → Wissen/Kanon (Nachschlagewerk): Registry, Adapter, TOC-Zählung,
Traversal-Schutz, Suche."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import library_knowledge as kn


@pytest.fixture
def kb_home(tmp_path, monkeypatch):
    """Isolierter $HOME mit Canon-, Orchestrierungs-, Skill- und Rollen-Quellen."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    canon = tmp_path / "vault" / "00-Canon"
    canon.mkdir(parents=True)
    (canon / "_index.md").write_text("# Canon-Index\n\nEinstieg.\n", encoding="utf-8")
    (canon / "infra-topology.md").write_text(
        "# Topologie\n\nPort 9119 ist das Dashboard.\n\n## Ports\n\n- 9119\n",
        encoding="utf-8",
    )
    (canon / "conventions-gates.md").write_text("# Gates\n\nVor Deploy.\n", encoding="utf-8")
    (canon / "agent-roster.md").write_text("# Roster\n\nHermes, Claude, Codex.\n", encoding="utf-8")
    (canon / "projects-map.md").write_text("# Projekte\n\nFamily Organizer.\n", encoding="utf-8")
    (canon / "memory-architecture.md").write_text("# Memory\n\nScopes.\n", encoding="utf-8")

    orch = tmp_path / "orchestration"
    orch.mkdir(parents=True)
    (orch / "CLAUDE.md").write_text("# Verfassung\n\nTriage zuerst.\n", encoding="utf-8")
    (orch / "PLAYBOOK.md").write_text("# Playbook\n\n## Lektion 1\n\nDeploy grün.\n", encoding="utf-8")

    skills = tmp_path / ".claude" / "skills"
    (skills / "orchestrate").mkdir(parents=True)
    (skills / "orchestrate" / "SKILL.md").write_text(
        "---\n"
        "name: orchestrate\n"
        "description: Use when a build task is big enough to delegate. Claude stays the orchestrator.\n"
        "---\n\n"
        "# orchestrate\n\nDelegieren statt selbst tippen.\n",
        encoding="utf-8",
    )
    # Skill mit YAML-Block-Scalar-Description (>-) über mehrere Zeilen.
    (skills / "folded").mkdir(parents=True)
    (skills / "folded" / "SKILL.md").write_text(
        "---\n"
        "name: folded\n"
        "description: >-\n"
        "  Erste Zeile der gefalteten Beschreibung läuft weiter\n"
        "  in einer zweiten Zeile. Zweiter Satz hier.\n"
        "---\n\n"
        "# folded\n\nKörper.\n",
        encoding="utf-8",
    )
    # Skill ohne Frontmatter → Auto-Summary aus erstem Absatz.
    (skills / "plain").mkdir(parents=True)
    (skills / "plain" / "SKILL.md").write_text(
        "# Plain Skill\n\nDas hier ist der erste echte Absatz als Kurzbeschreibung.\n",
        encoding="utf-8",
    )

    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "auditor.md").write_text(
        "---\n"
        "name: auditor\n"
        "description: Read-only Code-Sichtung und Audit. Gibt nur die Konklusion zurück.\n"
        "model: sonnet\n"
        "---\n\n"
        "# auditor\n\nLesen, nicht schreiben.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_catalog_has_four_collections_in_order(kb_home):
    out = kn.list_knowledge()
    ids = [c["id"] for c in out["collections"]]
    assert ids == ["kanon", "orchestrierung", "skills", "rollen"]
    assert out["query"] == ""
    assert out["now"] > 0


def test_canon_docs_present_with_curated_summary_and_heading_count(kb_home):
    out = kn.list_knowledge()
    kanon = next(c for c in out["collections"] if c["id"] == "kanon")
    by_id = {d["id"]: d for d in kanon["docs"]}
    assert "kb::doc::canon-infra-topology" in by_id
    topo = by_id["kb::doc::canon-infra-topology"]
    assert topo["title"] == "Infrastruktur & Topologie"
    assert "Ports" in topo["summary"] or "verdrahtet" in topo["summary"]
    assert topo["source_ref"] == "vault/00-Canon/infra-topology.md"
    assert topo["heading_count"] == 2  # "# Topologie" + "## Ports"
    assert topo["updated_ts"] > 0
    # Karten tragen nie den Body.
    assert "body_md" not in topo


def test_skill_scanned_with_frontmatter_title_and_summary(kb_home):
    out = kn.list_knowledge()
    skills = next(c for c in out["collections"] if c["id"] == "skills")
    by_id = {d["id"]: d for d in skills["docs"]}
    assert "kb::skill::orchestrate" in by_id
    orch = by_id["kb::skill::orchestrate"]
    assert orch["title"] == "orchestrate"
    assert orch["summary"].startswith("Use when a build task is big enough to delegate.")
    assert "skill" in orch["tags"]


def test_folded_block_scalar_description_is_flattened(kb_home):
    # Regression: `description: >-` über mehrere Zeilen darf nicht ">-" werden.
    out = kn.list_knowledge()
    skills = next(c for c in out["collections"] if c["id"] == "skills")
    folded = next(d for d in skills["docs"] if d["id"] == "kb::skill::folded")
    assert folded["summary"].startswith("Erste Zeile der gefalteten Beschreibung läuft weiter")
    assert ">-" not in folded["summary"]
    assert "\n" not in folded["summary"]


def test_skill_without_frontmatter_uses_first_paragraph(kb_home):
    out = kn.list_knowledge()
    skills = next(c for c in out["collections"] if c["id"] == "skills")
    plain = next(d for d in skills["docs"] if d["id"] == "kb::skill::plain")
    assert plain["summary"] == "Das hier ist der erste echte Absatz als Kurzbeschreibung."


def test_role_scanned_with_model_tag(kb_home):
    out = kn.list_knowledge()
    rollen = next(c for c in out["collections"] if c["id"] == "rollen")
    auditor = next(d for d in rollen["docs"] if d["id"] == "kb::role::auditor")
    assert auditor["title"] == "auditor"
    assert "rolle" in auditor["tags"]
    assert "sonnet" in auditor["tags"]


def test_read_static_doc_returns_body(kb_home):
    doc = kn.read_knowledge_doc("kb::doc::canon-infra-topology")
    assert doc is not None
    assert doc["id"] == "kb::doc::canon-infra-topology"
    assert "Port 9119 ist das Dashboard." in doc["body_md"]


def test_read_skill_strips_frontmatter(kb_home):
    doc = kn.read_knowledge_doc("kb::skill::orchestrate")
    assert doc is not None
    # Body beginnt mit der H1, das Frontmatter ist abgetrennt.
    assert doc["body_md"].lstrip().startswith("# orchestrate")
    assert "description:" not in doc["body_md"]


def test_unknown_static_doc_raises(kb_home):
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::doc::does-not-exist")


def test_missing_skill_returns_none(kb_home):
    # Slug gültig, aber kein Verzeichnis → None (→ 404), kein Traversal.
    assert kn.read_knowledge_doc("kb::skill::nonexistent") is None


def test_malformed_id_raises(kb_home):
    for bad in ("garbage", "kb::doc", "x::doc::canon-index", "kb::weird::canon-index"):
        with pytest.raises(ValueError):
            kn.read_knowledge_doc(bad)


def test_traversal_slug_rejected(kb_home):
    # Punkte/Slashes scheitern schon am Slug-Regex → ValueError, nie ein Read.
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::skill::../../../etc/passwd")
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::role::..")


def test_search_filters_and_drops_empty_collections(kb_home):
    out = kn.list_knowledge(q="9119")
    ids = [c["id"] for c in out["collections"]]
    assert ids == ["kanon"]  # nur die Topologie matcht
    assert out["count"] == 1
    docs = out["collections"][0]["docs"]
    assert [d["id"] for d in docs] == ["kb::doc::canon-infra-topology"]


def test_search_matches_skill_body(kb_home):
    # "tippen" steht nur im orchestrate-Skill-Body, in keiner kuratierten Summary.
    out = kn.list_knowledge(q="tippen")
    ids = [c["id"] for c in out["collections"]]
    assert ids == ["skills"]


def test_heading_count_ignores_fenced_code(kb_home):
    canon = kb_home / "vault" / "00-Canon"
    (canon / "_index.md").write_text(
        "# Echt\n\n```\n# nur code\n## auch code\n```\n\n## Zweiter\n",
        encoding="utf-8",
    )
    out = kn.list_knowledge()
    kanon = next(c for c in out["collections"] if c["id"] == "kanon")
    idx = next(d for d in kanon["docs"] if d["id"] == "kb::doc::canon-index")
    assert idx["heading_count"] == 2  # "# Echt" + "## Zweiter", Code ignoriert

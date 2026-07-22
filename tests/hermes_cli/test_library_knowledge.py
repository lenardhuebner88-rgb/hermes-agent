"""Bibliothek → Wissen/Kanon (Nachschlagewerk): Registry, Adapter, TOC-Zählung,
Traversal-Schutz, Suche."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import library_knowledge as kn


@pytest.fixture
def kb_home(tmp_path, monkeypatch):
    """Isolierter $HOME mit Canon-, Orchestrierungs-, Skill-, Rollen- und Plan-Quellen."""
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
    orch_docs = orch / "docs"
    orch_docs.mkdir()
    (orch_docs / "LOOP_ENGINEERING.md").write_text("# Loop Engineering\n\nAnleitung.\n", encoding="utf-8")
    (orch_docs / "LOOP_ENGINEERING_PROMPTS.md").write_text("# Ops-Loops\n\nDrei Prompts.\n", encoding="utf-8")
    (orch_docs / "LOOP_ENGINEERING_BUILD_KIT.md").write_text(
        "# Build-Baukasten\n\nGenerative Loops.\n", encoding="utf-8"
    )

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
    # Skill mit leerem name-Feld → Fallback auf Slug.
    (skills / "empty-name").mkdir(parents=True)
    (skills / "empty-name" / "SKILL.md").write_text(
        "---\n"
        "name: \"\"\n"
        "description: Leerer Name muss auf Slug zurückfallen.\n"
        "---\n\n"
        "# empty-name\n\nBody.\n",
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
    # Rolle mit leerem name-Feld → Fallback auf Slug.
    (agents / "empty-name.md").write_text(
        "---\n"
        "name: \"\"\n"
        "description: Leerer Name muss auf Slug zurückfallen.\n"
        "---\n\n"
        "# empty-name\n\nBody.\n",
        encoding="utf-8",
    )

    wiki = tmp_path / "llm-wiki" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "entities").mkdir()
    (wiki / "queries").mkdir()
    (wiki / "sources").mkdir()
    (wiki / "lint").mkdir()
    (wiki / "overview.md").write_text(
        "---\n"
        "title: \"LLM-Wiki Überblick\"\n"
        "type: overview\n"
        "tags:\n"
        "  - llm-wiki\n"
        "  - einstieg\n"
        "---\n\n"
        "# LLM-Wiki Überblick\n\nDas Wiki sammelt agentisches Referenzwissen.\n",
        encoding="utf-8",
    )
    (wiki / "concepts" / "ingest-query-lint.md").write_text(
        "---\n"
        "title: \"Ingest Query Lint\"\n"
        "type: concept\n"
        "tags:\n"
        "  - workflow\n"
        "  - lint\n"
        "---\n\n"
        "# Ingest Query Lint\n\nDer ingest workflow prüft Quellen, Links und Queries.\n",
        encoding="utf-8",
    )
    (wiki / "entities" / "qmd.md").write_text("# qmd\n\nLokale Markdown-Suche.\n", encoding="utf-8")
    (wiki / "queries" / "what-is-the-ingest-workflow.md").write_text(
        "# What is the ingest workflow?\n\nAntwort mit Quellen.\n", encoding="utf-8"
    )
    (wiki / "sources" / "karpathy-llm-wiki-pattern.md").write_text(
        "# Karpathy LLM Wiki Pattern\n\nQuelle.\n", encoding="utf-8"
    )
    (wiki / "lint" / "auto-health-check.md").write_text("# Auto Health Check\n\nOK.\n", encoding="utf-8")

    # models/ — cron-gepflegter Bereich (model-return-watch, siehe echte
    # ~/llm-wiki/wiki/models/*.md): Live-Katalog + Append-only-Discovery-Log.
    (wiki / "models").mkdir()
    (wiki / "models" / "model-landscape.md").write_text(
        "---\n"
        "title: \"LLM Model Landscape\"\n"
        "type: entity\n"
        "tags:\n"
        "  - llm-wiki\n"
        "  - models\n"
        "---\n\n"
        "# LLM Model Landscape\n\n"
        "## anthropic\n\n"
        "| Modell-ID | Erstellt | Kontext | Prompt/Completion pro 1M |\n"
        "|---|---|---|---|\n"
        "| `anthropic/claude-sonnet-5` | 2026-06-30 | 1M | $2.00 / $10.00 |\n",
        encoding="utf-8",
    )
    # Zeilenformat 1:1 aus der echten Datei übernommen (siehe
    # ~/llm-wiki/wiki/models/model-log.md Kopfzeile); die Datenzeilen selbst
    # sind synthetisch, da das echte Log am 2026-07-02 noch leer ist.
    (wiki / "models" / "model-log.md").write_text(
        "---\n"
        "title: \"LLM Model Discovery Log\"\n"
        "type: entity\n"
        "tags:\n"
        "  - llm-wiki\n"
        "  - models\n"
        "---\n\n"
        "# LLM Model Discovery Log\n\n"
        "_Append-only. Pro neu entdecktem Modell der Watch-Provider "
        "(anthropic/, openai/, google/) eine Zeile._\n\n"
        "_Format: `- YYYY-MM-DD \\`model-id\\` (context Xk, $Y/$Z per 1M)`_\n\n"
        "- 2026-06-30 `anthropic/claude-sonnet-5` (context 1M, $2.00/$10.00 per 1M)\n"
        "- 2026-07-01 `google/gemini-3.5-flash` (context 1M, $1.50/$9.00 per 1M)\n"
        "- 2026-07-02 `x-ai/grok-build-0.1` (context 256k, $1.00/$2.00 per 1M)\n",
        encoding="utf-8",
    )
    # Synthetische dritte models/-Seite OHNE deklarierten `type:` — testet den
    # neuen Verzeichnis-Fallback (die zwei echten Dateien oben deklarieren
    # `type: entity` und bleiben dadurch unverändert als Entitäten klassifiziert).
    (wiki / "models" / "model-notes.md").write_text(
        "# Model Notes\n\nFreitext ohne Frontmatter.\n", encoding="utf-8"
    )

    plans = tmp_path / "vault" / "03-Agents" / "Hermes" / "plans"
    plans.mkdir(parents=True)
    (plans / "dashboard-refresh.md").write_text(
        "---\n"
        "title: \"Dashboard Refresh\"\n"
        "created: 2026-07-01\n"
        "owner: Hermes\n"
        "type: implementation\n"
        "status: active\n"
        "summary: Additive UI refresh plan.\n"
        "tags:\n"
        "  - dashboard\n"
        "  - slice-b\n"
        "---\n\n"
        "# Dashboard Refresh\n\nPlan body mentions async widgets.\n\n## Steps\n\n- Build.\n",
        encoding="utf-8",
    )
    (plans / "nested").mkdir()
    (plans / "nested" / "fallback.md").write_text(
        "# Fallback Plan\n\nFirst paragraph becomes the summary.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_catalog_has_collections_in_order(kb_home):
    out = kn.list_knowledge()
    ids = [c["id"] for c in out["collections"]]
    assert ids == ["kanon", "orchestrierung", "skills", "rollen", "llm-wiki", "vault-plans"]
    assert out["query"] == ""
    assert out["now"] > 0


def test_vault_plans_are_scanned_with_frontmatter(kb_home):
    out = kn.list_knowledge()
    plans = next(c for c in out["collections"] if c["id"] == "vault-plans")
    by_id = {d["id"]: d for d in plans["docs"]}
    assert list(by_id) == [
        "kb::plan::Hermes/plans/dashboard-refresh.md",
        "kb::plan::Hermes/plans/nested/fallback.md",
    ]
    dashboard = by_id["kb::plan::Hermes/plans/dashboard-refresh.md"]
    assert dashboard["title"] == "Dashboard Refresh"
    assert dashboard["summary"] == "Additive UI refresh plan."
    assert dashboard["created"] == "2026-07-01"
    assert dashboard["owner"] == "Hermes"
    assert dashboard["type"] == "implementation"
    assert dashboard["status"] == "active"
    assert dashboard["source_ref"] == "vault/03-Agents/Hermes/plans/dashboard-refresh.md"
    assert "vault-plan" in dashboard["tags"]
    assert "type:implementation" in dashboard["tags"]
    assert "status:active" in dashboard["tags"]
    assert "dashboard" in dashboard["tags"]
    assert dashboard["heading_count"] == 2
    fallback = by_id["kb::plan::Hermes/plans/nested/fallback.md"]
    assert fallback["title"] == "Fallback Plan"
    assert fallback["summary"] == "First paragraph becomes the summary."


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


def test_orchestration_loop_docs_present(kb_home):
    # Loop-Engineering-Baukasten lebt im Orchestrierungs-Regal (feste Registry).
    out = kn.list_knowledge()
    orch = next(c for c in out["collections"] if c["id"] == "orchestrierung")
    by_id = {d["id"]: d for d in orch["docs"]}
    assert "kb::doc::orch-loop-build-kit" in by_id
    kit = by_id["kb::doc::orch-loop-build-kit"]
    assert kit["title"] == "Loop Engineering — Build-Baukasten"
    assert kit["source_ref"] == "orchestration/docs/LOOP_ENGINEERING_BUILD_KIT.md"
    assert kit["updated_ts"] > 0
    # Lesbar: der Body kommt über read_knowledge_doc, nicht über die Karte.
    assert "body_md" not in kit
    doc = kn.read_knowledge_doc("kb::doc::orch-loop-build-kit")
    assert doc is not None and "Generative Loops." in doc["body_md"]


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


def test_skill_with_empty_name_falls_back_to_slug(kb_home):
    out = kn.list_knowledge()
    skills = next(c for c in out["collections"] if c["id"] == "skills")
    empty = next(d for d in skills["docs"] if d["id"] == "kb::skill::empty-name")
    assert empty["title"] == "empty-name"


def test_role_with_empty_name_falls_back_to_slug(kb_home):
    out = kn.list_knowledge()
    rollen = next(c for c in out["collections"] if c["id"] == "rollen")
    empty = next(d for d in rollen["docs"] if d["id"] == "kb::role::empty-name")
    assert empty["title"] == "empty-name"


def test_llm_wiki_pages_are_scanned_with_frontmatter_tags(kb_home):
    out = kn.list_knowledge()
    wiki = next(c for c in out["collections"] if c["id"] == "llm-wiki")
    by_id = {d["id"]: d for d in wiki["docs"]}
    assert list(by_id)[:2] == [
        "kb::llm::overview.md",
        "kb::llm::concepts/ingest-query-lint.md",
    ]
    concept = by_id["kb::llm::concepts/ingest-query-lint.md"]
    assert concept["title"] == "Ingest Query Lint"
    assert concept["source_ref"] == "llm-wiki/concepts/ingest-query-lint.md"
    assert "llm-wiki" in concept["tags"]
    assert "type:concept" in concept["tags"]
    assert "workflow" in concept["tags"]
    assert concept["heading_count"] == 1
    assert "body_md" not in concept
    assert "type:entity" in by_id["kb::llm::entities/qmd.md"]["tags"]
    assert "type:query" in by_id["kb::llm::queries/what-is-the-ingest-workflow.md"]["tags"]


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


def test_read_llm_wiki_doc_strips_frontmatter(kb_home):
    doc = kn.read_knowledge_doc("kb::llm::concepts/ingest-query-lint.md")
    assert doc is not None
    assert doc["id"] == "kb::llm::concepts/ingest-query-lint.md"
    assert doc["body_md"].lstrip().startswith("# Ingest Query Lint")
    assert "type: concept" not in doc["body_md"]


def test_read_vault_plan_strips_frontmatter(kb_home):
    doc = kn.read_knowledge_doc("kb::plan::Hermes/plans/dashboard-refresh.md")
    assert doc is not None
    assert doc["id"] == "kb::plan::Hermes/plans/dashboard-refresh.md"
    assert doc["created"] == "2026-07-01"
    assert doc["owner"] == "Hermes"
    assert doc["type"] == "implementation"
    assert doc["status"] == "active"
    assert doc["body_md"].lstrip().startswith("# Dashboard Refresh")
    assert "status: active" not in doc["body_md"]


def test_malformed_vault_plan_frontmatter_falls_back_and_is_still_listed(kb_home, caplog):
    # Regression: kaputtes YAML durfte den Plan früher komplett aus dem Regal
    # kippen. Jetzt: Doc bleibt gelistet (Metadaten leer, Titel fällt über
    # first-heading zurück), die Warnung bleibt aber erhalten.
    broken = kb_home / "vault" / "03-Agents" / "Hermes" / "plans" / "broken.md"
    broken.write_text("---\ntitle: [oops\n---\n\n# Broken\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="hermes_cli.library_knowledge"):
        out = kn.list_knowledge()

    plans = next(c for c in out["collections"] if c["id"] == "vault-plans")
    by_id = {d["id"]: d for d in plans["docs"]}
    assert "kb::plan::Hermes/plans/broken.md" in by_id
    broken_doc = by_id["kb::plan::Hermes/plans/broken.md"]
    assert broken_doc["title"] == "Broken"  # first-heading-Fallback
    assert "created" not in broken_doc  # leere Metadaten, kein Platzhalter
    assert "owner" not in broken_doc
    assert "malformed frontmatter" in caplog.text
    assert "Hermes/plans/broken.md" in caplog.text

    doc = kn.read_knowledge_doc("kb::plan::Hermes/plans/broken.md")
    assert doc is not None
    assert doc["body_md"].lstrip().startswith("# Broken")


def test_malformed_vault_plan_frontmatter_without_heading_falls_back_to_filename_title(kb_home):
    # Ohne Heading UND ohne brauchbares Frontmatter bleibt nur der Dateiname.
    broken = kb_home / "vault" / "03-Agents" / "Hermes" / "plans" / "no-heading-broken.md"
    broken.write_text("---\ntitle: [oops\n---\n\nJust body text, no heading.\n", encoding="utf-8")

    out = kn.list_knowledge()
    plans = next(c for c in out["collections"] if c["id"] == "vault-plans")
    by_id = {d["id"]: d for d in plans["docs"]}
    assert by_id["kb::plan::Hermes/plans/no-heading-broken.md"]["title"] == "No Heading Broken"


def test_unknown_static_doc_raises(kb_home):
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::doc::does-not-exist")


def test_missing_skill_returns_none(kb_home):
    # Slug gültig, aber kein Verzeichnis → None (→ 404), kein Traversal.
    assert kn.read_knowledge_doc("kb::skill::nonexistent") is None


def test_missing_llm_wiki_doc_returns_none(kb_home):
    assert kn.read_knowledge_doc("kb::llm::concepts/does-not-exist.md") is None


def test_missing_vault_plan_returns_none(kb_home):
    assert kn.read_knowledge_doc("kb::plan::Hermes/plans/does-not-exist.md") is None


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
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::llm::../raw/secret.md")
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::llm::concepts/../../secret.md")
    with pytest.raises(ValueError):
        kn.read_knowledge_doc("kb::plan::Hermes/plans/../../secret.md")


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


def test_search_matches_llm_wiki_body(kb_home):
    out = kn.list_knowledge(q="queries")
    ids = [c["id"] for c in out["collections"]]
    assert ids == ["llm-wiki"]
    docs = out["collections"][0]["docs"]
    assert [d["id"] for d in docs] == ["kb::llm::concepts/ingest-query-lint.md"]


def test_search_matches_vault_plan_body(kb_home):
    out = kn.list_knowledge(q="widgets")
    ids = [c["id"] for c in out["collections"]]
    assert ids == ["vault-plans"]
    docs = out["collections"][0]["docs"]
    assert [d["id"] for d in docs] == ["kb::plan::Hermes/plans/dashboard-refresh.md"]


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


def test_models_dir_infers_model_type_when_undeclared(kb_home):
    # Premise-Check: die echten model-landscape.md/model-log.md deklarieren
    # `type: entity` im Frontmatter — das gewinnt unverändert gegen die
    # Verzeichnis-Inferenz (bestehendes Präzedenz-Verhalten, hier nicht
    # angetastet). Der neue Verzeichnis-Fallback greift nur, wenn KEIN `type:`
    # deklariert ist (model-notes.md).
    out = kn.list_knowledge()
    wiki = next(c for c in out["collections"] if c["id"] == "llm-wiki")
    by_id = {d["id"]: d for d in wiki["docs"]}
    assert "type:entity" in by_id["kb::llm::models/model-landscape.md"]["tags"]
    assert "type:entity" in by_id["kb::llm::models/model-log.md"]["tags"]
    assert "type:model" in by_id["kb::llm::models/model-notes.md"]["tags"]
    # Rangfolge: nach sources/entities/concepts, vor lint (siehe echte
    # ~/llm-wiki/wiki-Struktur: concepts, entities, queries, sources, lint).
    ids = list(by_id)
    assert ids.index("kb::llm::models/model-landscape.md") > ids.index("kb::llm::sources/karpathy-llm-wiki-pattern.md")
    assert ids.index("kb::llm::models/model-landscape.md") < ids.index("kb::llm::lint/auto-health-check.md")


def test_read_model_landscape_doc_returns_body(kb_home):
    doc = kn.read_knowledge_doc("kb::llm::models/model-landscape.md")
    assert doc is not None
    assert doc["body_md"].lstrip().startswith("# LLM Model Landscape")
    assert "anthropic/claude-sonnet-5" in doc["body_md"]


def test_model_log_pulse_parses_last_three_entries_newest_first(kb_home):
    # Zeilenformat exakt aus der echten Datei (~/llm-wiki/wiki/models/model-log.md
    # Kopfzeile: "- YYYY-MM-DD `model-id` (context Xk, $Y/$Z per 1M)").
    pulse = kn._model_log_pulse()
    assert pulse == [
        {"date": "2026-07-02", "model": "x-ai/grok-build-0.1", "detail": "context 256k, $1.00/$2.00 per 1M"},
        {"date": "2026-07-01", "model": "google/gemini-3.5-flash", "detail": "context 1M, $1.50/$9.00 per 1M"},
        {"date": "2026-06-30", "model": "anthropic/claude-sonnet-5", "detail": "context 1M, $2.00/$10.00 per 1M"},
    ]


def test_model_log_pulse_missing_file_returns_empty(kb_home):
    (kb_home / "llm-wiki" / "wiki" / "models" / "model-log.md").unlink()
    assert kn._model_log_pulse() == []


def test_catalog_exposes_per_collection_freshness_metadata(kb_home):
    out = kn.list_knowledge()
    kanon = next(c for c in out["collections"] if c["id"] == "kanon")
    assert kanon["doc_count"] == len(kanon["docs"])
    assert kanon["updated_ts"] > 0
    assert "pulse" not in kanon  # nur llm-wiki bekommt den Puls-Strip

    wiki = next(c for c in out["collections"] if c["id"] == "llm-wiki")
    assert wiki["doc_count"] == len(wiki["docs"])
    assert wiki["updated_ts"] > 0
    assert wiki["pulse"][0]["model"] == "x-ai/grok-build-0.1"
    assert len(wiki["pulse"]) == 3


# ---------------------------------------------------------------------------
# SLICE 2b: wiki/reports and wiki/prompting get proper inferred types
# ---------------------------------------------------------------------------

def test_llm_wiki_type_infers_report_for_reports_dir(kb_home):
    """Paths under wiki/reports/ should infer type 'report' (KI-Lageberichte)."""
    assert kn._llm_wiki_type("reports/2026-07-20-daily.md", {}) == "report"
    assert kn._llm_wiki_type("reports/weekly-summary.md", {}) == "report"


def test_llm_wiki_type_infers_guide_for_prompting_dir(kb_home):
    """Paths under wiki/prompting/ should infer type 'guide' (prompting guides)."""
    assert kn._llm_wiki_type("prompting/gpt5-codex.md", {}) == "guide"
    assert kn._llm_wiki_type("prompting/claude-opus.md", {}) == "guide"


def test_llm_wiki_type_preserves_existing_inferences(kb_home):
    """Ensure the new entries don't break existing type inference."""
    assert kn._llm_wiki_type("concepts/some-concept.md", {}) == "concept"
    assert kn._llm_wiki_type("entities/some-entity.md", {}) == "entity"
    assert kn._llm_wiki_type("models/model-landscape.md", {}) == "model"
    assert kn._llm_wiki_type("sources/some-source.md", {}) == "source"


def test_llm_wiki_type_declared_type_still_wins(kb_home):
    """A declared type: in frontmatter always wins over directory inference."""
    assert kn._llm_wiki_type("reports/special.md", {"type": "analysis"}) == "analysis"
    assert kn._llm_wiki_type("prompting/custom.md", {"type": "tutorial"}) == "tutorial"

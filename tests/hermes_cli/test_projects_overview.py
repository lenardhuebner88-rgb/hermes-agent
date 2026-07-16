from __future__ import annotations

from pathlib import Path

from hermes_cli.projects_overview import load_projects_registry

# Verbatim copy of the REAL ~/.hermes/projects.yaml content (2026-07-16) so the
# "valid" test exercises the exact on-disk format, not a synthetic simplification.
_REAL_PROJECTS_YAML = """\
# Projekt-Registry für den /control "Projekte"-Tab (Leitstand).
# Runtime-Config — NICHT im Repo. Gelesen von hermes_cli/projects_overview.py.
# Felder pro Projekt:
#   slug            eindeutiger Kurzname (Pflicht)
#   name            Anzeigename (Pflicht)
#   repo_path       Git-Checkout (Pflicht)
#   kanban_project  Board-Slug in ~/.hermes/projects.db ('default' = Hermes-Board) oder null
#   loop_packs      Loop-Pack-Namen unter ~/.hermes/loops/
#   links           [{label, url}] — optional
#   parent          slug des Elternprojekts (Unterprojekt) — optional
#   path_filters    Pfad-Präfixe/Dateien im Eltern-Repo, die zu diesem Unterprojekt gehören — optional
projects:
  - slug: hermes-infra
    name: Hermes Infra
    repo_path: /home/piet/.hermes/hermes-agent
    kanban_project: default
    loop_packs:
      - builder-reviewer
      - dashboard-experience
      - dashboard-polish
      - doc-sweep
      - error-sweep
      - loop-schmiede
      - loops-date-audit
      - test-stabiliser
      - xai-hard-gate
    links:
      - label: Control-Dashboard
        url: /control
  - slug: diktat
    name: Diktat
    repo_path: /home/piet/.hermes/hermes-agent
    parent: hermes-infra
    path_filters:
      - android/hermes-dictate
      - web/src/control/views/DiktatView.tsx
    kanban_project: null
    loop_packs: []
  - slug: health-track
    name: Health Track
    repo_path: /home/piet/projects/health-track
    kanban_project: health-track
    loop_packs:
      - health-track-ux
      - ht-defect-hunt
      - ht-perf
      - ht-ux-polish
  - slug: family-organizer
    name: Family Organizer
    repo_path: /home/piet/projects/family-organizer
    kanban_project: null
    loop_packs: []
  - slug: oma-galerie
    name: Oma-Galerie
    repo_path: /home/piet/projects/oma-galerie
    kanban_project: null
    loop_packs: []
  - slug: llm-wiki
    name: LLM-Wiki
    repo_path: /home/piet/llm-wiki
    kanban_project: null
    loop_packs: []
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "projects.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_real_format_parses_all_entries(tmp_path: Path) -> None:
    path = _write(tmp_path, _REAL_PROJECTS_YAML)

    result = load_projects_registry(path)

    assert result.errors == []
    assert [p.slug for p in result.projects] == [
        "hermes-infra",
        "diktat",
        "health-track",
        "family-organizer",
        "oma-galerie",
        "llm-wiki",
    ]

    hermes_infra = next(p for p in result.projects if p.slug == "hermes-infra")
    assert "builder-reviewer" in hermes_infra.loop_packs
    assert hermes_infra.kanban_project == "default"
    assert len(hermes_infra.links) == 1
    assert hermes_infra.links[0].label == "Control-Dashboard"
    assert hermes_infra.links[0].url == "/control"

    diktat = next(p for p in result.projects if p.slug == "diktat")
    assert diktat.parent == "hermes-infra"
    assert diktat.path_filters == [
        "android/hermes-dictate",
        "web/src/control/views/DiktatView.tsx",
    ]
    assert diktat.kanban_project is None

    health_track = next(p for p in result.projects if p.slug == "health-track")
    assert health_track.kanban_project == "health-track"


def test_missing_file_returns_empty_no_error(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.yaml"

    result = load_projects_registry(path)

    assert result.projects == []
    assert result.errors == []


def test_broken_yaml_returns_error_not_exception(tmp_path: Path) -> None:
    path = _write(tmp_path, "projects: [unclosed")

    result = load_projects_registry(path)

    assert result.projects == []
    assert len(result.errors) == 1


def test_top_level_plain_list_returns_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "- slug: foo\n  name: Foo\n  repo_path: /tmp/foo\n")

    result = load_projects_registry(path)

    assert result.projects == []
    assert len(result.errors) == 1


def test_top_level_projects_wrong_type_returns_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "projects: nope\n")

    result = load_projects_registry(path)

    assert result.projects == []
    assert len(result.errors) == 1


def test_invalid_entry_skipped_valid_entries_survive(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
projects:
  - slug: good-one
    name: Good One
    repo_path: /tmp/good-one
  - slug: broken
    name: Broken Entry
""",
    )

    result = load_projects_registry(path)

    assert [p.slug for p in result.projects] == ["good-one"]
    assert len(result.errors) == 1
    assert "broken" in result.errors[0]


def test_duplicate_slug_first_wins(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
projects:
  - slug: dup
    name: First
    repo_path: /tmp/first
  - slug: dup
    name: Second
    repo_path: /tmp/second
""",
    )

    result = load_projects_registry(path)

    assert [p.name for p in result.projects] == ["First"]
    assert len(result.errors) == 1
    assert "dup" in result.errors[0]

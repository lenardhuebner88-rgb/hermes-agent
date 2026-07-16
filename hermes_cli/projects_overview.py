"""Projekt-Registry für den /control "Projekte"-Tab (Leitstand).

Diese erste Slice liest ausschließlich die Registry ein (keine FastAPI-Routen —
die kommen in einer späteren Stage; das Modul ist so strukturiert, dass Routen
später ohne Umbau ergänzt werden können).

Config-Vertrag (Runtime-Datei, NICHT im Repo): ``~/.hermes/projects.yaml``.
Top-Level-Mapping mit einem Key ``projects:`` (Liste von Einträgen). Pro
Eintrag:

    slug            eindeutiger Kurzname (Pflicht)
    name            Anzeigename (Pflicht)
    repo_path       Git-Checkout (Pflicht)
    kanban_project  Board-Slug in ~/.hermes/projects.db oder null (optional)
    loop_packs      Liste von Loop-Pack-Namen unter ~/.hermes/loops/ (optional)
    links           Liste von {label, url} (optional)
    parent          Slug des Elternprojekts, für Unterprojekte (optional)
    path_filters    Pfad-Präfixe/Dateien im Eltern-Repo (optional)

Fehlt die Datei, ist das der dokumentierte No-Config-Default: leere Projektliste,
keine Fehler. Ist die Datei vorhanden aber kaputt/falsch geformt, wird das als
Fehlerstring gemeldet statt eine Exception zu werfen — einzelne kaputte
Einträge werden übersprungen (mit Fehlerstring), gültige Einträge bleiben
erhalten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.config import get_hermes_home


@dataclass
class ProjectLink:
    """Ein Link-Eintrag ({label, url}) für ein Projekt."""

    label: str
    url: str


@dataclass
class ProjectEntry:
    """Ein Projekt-Registry-Eintrag aus ``projects.yaml``."""

    slug: str
    name: str
    repo_path: str
    kanban_project: str | None = None
    loop_packs: list[str] = field(default_factory=list)
    links: list[ProjectLink] = field(default_factory=list)
    parent: str | None = None
    path_filters: list[str] = field(default_factory=list)


@dataclass
class ProjectsRegistry:
    """Ergebnis von :func:`load_projects_registry`."""

    projects: list[ProjectEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _default_registry_path(home: Path | None) -> Path:
    return (home if home is not None else get_hermes_home()) / "projects.yaml"


def _parse_links(slug: str, raw: Any, errors: list[str]) -> list[ProjectLink] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"project '{slug}': 'links' must be a list")
        return None
    links: list[ProjectLink] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"project '{slug}': links[{i}] must be a mapping")
            return None
        label = item.get("label")
        url = item.get("url")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"project '{slug}': links[{i}] missing 'label'")
            return None
        if not isinstance(url, str) or not url.strip():
            errors.append(f"project '{slug}': links[{i}] missing 'url'")
            return None
        links.append(ProjectLink(label=label, url=url))
    return links


def _parse_str_list(slug: str, field_name: str, raw: Any, errors: list[str]) -> list[str] | None:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        errors.append(f"project '{slug}': '{field_name}' must be a list of strings")
        return None
    return list(raw)


def _parse_entry(index: int, raw: Any, errors: list[str]) -> ProjectEntry | None:
    label = f"index {index}"
    if not isinstance(raw, dict):
        errors.append(f"project at {label}: entry must be a mapping")
        return None

    slug = raw.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        errors.append(f"project at {label}: missing or empty 'slug'")
        return None
    label = f"'{slug}'"

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"project {label}: missing or empty 'name'")
        return None

    repo_path = raw.get("repo_path")
    if not isinstance(repo_path, str) or not repo_path.strip():
        errors.append(f"project {label}: missing or empty 'repo_path'")
        return None

    kanban_project = raw.get("kanban_project")
    if kanban_project is not None and not isinstance(kanban_project, str):
        errors.append(f"project {label}: 'kanban_project' must be a string or null")
        return None

    parent = raw.get("parent")
    if parent is not None and not isinstance(parent, str):
        errors.append(f"project {label}: 'parent' must be a string")
        return None

    loop_packs = _parse_str_list(slug, "loop_packs", raw.get("loop_packs"), errors)
    if loop_packs is None:
        return None

    path_filters = _parse_str_list(slug, "path_filters", raw.get("path_filters"), errors)
    if path_filters is None:
        return None

    links = _parse_links(slug, raw.get("links"), errors)
    if links is None:
        return None

    return ProjectEntry(
        slug=slug,
        name=name,
        repo_path=repo_path,
        kanban_project=kanban_project,
        loop_packs=loop_packs,
        links=links,
        parent=parent,
        path_filters=path_filters,
    )


def load_projects_registry(
    path: Path | None = None, *, home: Path | None = None
) -> ProjectsRegistry:
    """Lädt und validiert ``projects.yaml``.

    ``path`` überschreibt den Dateipfad direkt (für Tests); ``home`` überschreibt
    nur das Basisverzeichnis (Standard: :func:`get_hermes_home`). Wirft nie eine
    Exception — Fehler landen als Strings in ``ProjectsRegistry.errors``.
    """

    registry_path = path if path is not None else _default_registry_path(home)

    if not registry_path.exists():
        return ProjectsRegistry(projects=[], errors=[])

    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ProjectsRegistry(projects=[], errors=[f"could not read {registry_path}: {exc}"])

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ProjectsRegistry(projects=[], errors=[f"invalid YAML in {registry_path}: {exc}"])

    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
        return ProjectsRegistry(
            projects=[],
            errors=[
                f"{registry_path}: top-level document must be a mapping with a 'projects' list"
            ],
        )

    errors: list[str] = []
    projects: list[ProjectEntry] = []
    seen_slugs: set[str] = set()

    for index, raw_entry in enumerate(raw["projects"]):
        entry = _parse_entry(index, raw_entry, errors)
        if entry is None:
            continue
        if entry.slug in seen_slugs:
            errors.append(f"project '{entry.slug}': duplicate slug, keeping first occurrence")
            continue
        seen_slugs.add(entry.slug)
        projects.append(entry)

    return ProjectsRegistry(projects=projects, errors=errors)

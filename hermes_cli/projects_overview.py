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

import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from hermes_cli import control_loops, projects_db
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


# ---------------------------------------------------------------------------
# Stage 2 — read-only per-project card data (/api/projects)
# ---------------------------------------------------------------------------
#
# Every source below (git, kanban, loops) is isolated: an exception/timeout in
# one source degrades that one field to ``None`` plus a short ``errors[]``
# entry (prefixed ``git:``/``kanban:``/``loops:``) — it never takes down the
# whole payload. The route handler wraps everything once more so the endpoint
# always answers with JSON, never a 500.

_GIT_LOG_TIMEOUT_SECONDS = 3
_KANBAN_DONE_WINDOW_SECONDS = 7 * 24 * 3600
# Wire instants must carry an explicit zone (``Z`` or ``+HH:MM``) to be treated
# as unambiguous. Real loop heartbeat.json files mix zoned and naive
# timestamps (naive ones are local-clock artifacts); naive strings are dropped
# rather than silently misinterpreted as UTC.
_ISO_WITH_ZONE_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _project_last_commit(
    entry: ProjectEntry, *, now: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Runs ``git log -1`` for ``entry`` and returns ``(last_commit, error)``.

    ``path_filters`` (subproject slices of a parent repo) are appended as
    pathspecs after ``--`` so the subproject's own last touching commit is
    reported instead of the parent repo's overall HEAD.
    """
    cmd = [
        "git",
        "-C",
        entry.repo_path,
        "log",
        "-1",
        "--abbrev=9",
        "--format=%h\x1f%s\x1f%ct",
    ]
    if entry.path_filters:
        cmd.append("--")
        cmd.extend(entry.path_filters)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git log failed").strip()
        return None, f"git: {detail or 'git log failed'}"

    line = result.stdout.strip()
    if not line:
        # Repo exists, just has no commits (yet) matching the filters — not
        # an error, simply nothing to report.
        return None, None

    parts = line.split("\x1f")
    if len(parts) != 3:
        return None, "git: unexpected `git log` output"
    commit_hash, message, committed_at_raw = parts
    try:
        committed_at = int(committed_at_raw)
    except ValueError:
        return None, "git: unparsable commit timestamp"

    return {
        "hash": commit_hash,
        "message": message,
        "committed_at": committed_at,
        "age_seconds": max(0, now - committed_at),
    }, None


def _open_sqlite_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_kanban_project_id(
    board_slug: str, projects_db_path: Path
) -> tuple[str | None, str | None]:
    """Resolve a registry ``kanban_project`` (board slug) to a project row id.

    Not-found is not itself an error for the ``default`` board — legacy tasks
    predate ``project_id`` scoping entirely and are counted via ``IS NULL``
    regardless of whether a ``projects.db`` row binds to it.
    """
    try:
        conn = _open_sqlite_ro(projects_db_path)
    except sqlite3.DatabaseError as exc:
        if board_slug == "default":
            return None, None
        return None, f"kanban: could not open projects.db: {exc}"
    try:
        rows = projects_db.list_projects(conn, include_archived=True)
    except sqlite3.DatabaseError as exc:
        return None, f"kanban: {exc}"
    finally:
        conn.close()

    for row in rows:
        if row.board_slug == board_slug:
            return row.id, None
    if board_slug == "default":
        return None, None
    return None, f"kanban: no project bound to board '{board_slug}'"


def _kanban_counts(
    entry: ProjectEntry, *, kanban_db_path: Path, projects_db_path: Path, now: int
) -> tuple[dict[str, int] | None, str | None]:
    if entry.kanban_project is None:
        return None, None

    board_slug = entry.kanban_project
    project_id, resolve_error = _resolve_kanban_project_id(board_slug, projects_db_path)
    if resolve_error is not None:
        return None, resolve_error

    if board_slug == "default":
        if project_id is not None:
            scope_clause = "(project_id IS NULL OR project_id = ?)"
            scope_params: tuple[Any, ...] = (project_id,)
        else:
            scope_clause = "project_id IS NULL"
            scope_params = ()
    else:
        scope_clause = "project_id = ?"
        scope_params = (project_id,)

    try:
        conn = _open_sqlite_ro(kanban_db_path)
    except sqlite3.DatabaseError as exc:
        return None, f"kanban: could not open kanban.db: {exc}"

    try:
        def _count(status_clause: str, extra_params: tuple[Any, ...] = ()) -> int:
            row = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {scope_clause} AND {status_clause}",
                scope_params + extra_params,
            ).fetchone()
            return int(row[0]) if row is not None else 0

        counts = {
            "open": _count("status IN ('triage', 'todo', 'scheduled', 'ready')"),
            "running": _count("status = 'running'"),
            "blocked": _count("status = 'blocked'"),
            "review": _count("status = 'review'"),
            "done_7d": _count(
                "completed_at IS NOT NULL AND completed_at >= ?",
                (now - _KANBAN_DONE_WINDOW_SECONDS,),
            ),
        }
    except sqlite3.DatabaseError as exc:
        return None, f"kanban: {exc}"
    finally:
        conn.close()

    return counts, None


def _parse_loop_iso_timestamp(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not _ISO_WITH_ZONE_RE.search(text):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp())


def _last_heartbeat_at(heartbeat: dict[str, Any] | None) -> int | None:
    if not heartbeat:
        return None
    candidates: list[int] = []

    current = heartbeat.get("current")
    if isinstance(current, dict):
        parsed = _parse_loop_iso_timestamp(current.get("started_at"))
        if parsed is not None:
            candidates.append(parsed)

    last = heartbeat.get("last")
    if isinstance(last, list):
        for item in last:
            if isinstance(item, dict):
                parsed = _parse_loop_iso_timestamp(item.get("at"))
                if parsed is not None:
                    candidates.append(parsed)

    return max(candidates) if candidates else None


def _loops_for_entry(
    entry: ProjectEntry, *, loops_state_root: Path | None
) -> tuple[dict[str, Any], list[str]]:
    root = loops_state_root if loops_state_root is not None else control_loops._state_root()
    errors: list[str] = []
    packs: list[dict[str, Any]] = []
    active = 0

    for name in entry.loop_packs:
        try:
            state = root / name
            running = control_loops._is_running(state)
            heartbeat = control_loops._heartbeat(state)
            heartbeat_at = _last_heartbeat_at(heartbeat)
        except Exception as exc:  # isolate: one bad pack never kills the list
            errors.append(f"loops: pack '{name}': {exc}")
            packs.append({"name": name, "running": False, "last_heartbeat_at": None})
            continue
        if running:
            active += 1
        packs.append({"name": name, "running": running, "last_heartbeat_at": heartbeat_at})

    return {"active": active, "packs": packs}, errors


def build_projects_payload(
    registry: ProjectsRegistry,
    *,
    kanban_db_path: Path | None = None,
    projects_db_path: Path | None = None,
    loops_state_root: Path | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Assemble the frozen ``/api/projects`` payload from a loaded registry.

    Pure-ish: all data sources are overridable so tests never touch the real
    ``~/.hermes`` state. Defaults mirror production: ``~/.hermes/kanban.db``,
    ``~/.hermes/projects.db``, and the loops runner's real state root.
    """
    resolved_now = now if now is not None else int(time.time())
    home = get_hermes_home()
    resolved_kanban_db_path = kanban_db_path if kanban_db_path is not None else home / "kanban.db"
    resolved_projects_db_path = (
        projects_db_path if projects_db_path is not None else home / "projects.db"
    )

    projects_payload: list[dict[str, Any]] = []
    for entry in registry.projects:
        errors: list[str] = []

        try:
            last_commit, git_error = _project_last_commit(entry, now=resolved_now)
        except Exception as exc:
            last_commit, git_error = None, f"git: {exc}"
        if git_error:
            errors.append(git_error)

        try:
            kanban, kanban_error = _kanban_counts(
                entry,
                kanban_db_path=resolved_kanban_db_path,
                projects_db_path=resolved_projects_db_path,
                now=resolved_now,
            )
        except Exception as exc:
            kanban, kanban_error = None, f"kanban: {exc}"
        if kanban_error:
            errors.append(kanban_error)

        try:
            loops, loop_errors = _loops_for_entry(entry, loops_state_root=loops_state_root)
        except Exception as exc:
            loops, loop_errors = {"active": 0, "packs": []}, [f"loops: {exc}"]
        errors.extend(loop_errors)

        projects_payload.append(
            {
                "slug": entry.slug,
                "name": entry.name,
                "repo_path": entry.repo_path,
                "parent": entry.parent,
                "links": [{"label": link.label, "url": link.url} for link in entry.links],
                "last_commit": last_commit,
                "kanban": kanban,
                "loops": loops,
                "errors": errors,
            }
        )

    return {
        "generated_at": resolved_now,
        "registry_errors": list(registry.errors),
        "projects": projects_payload,
    }


def register_projects_routes(app: FastAPI) -> None:
    """Register the read-only ``GET /api/projects`` route.

    Auth comes automatically from the dashboard's ``/api/*`` middleware —
    nothing to do here as long as the path stays out of the public whitelist.
    The handler is wrapped once more on top of :func:`build_projects_payload`'s
    own per-source isolation so a truly unexpected failure (e.g. the registry
    file itself becoming unreadable) still answers with JSON, never a 500.
    """

    @app.get("/api/projects")
    def get_projects() -> dict[str, Any]:
        try:
            registry = load_projects_registry()
            return build_projects_payload(registry)
        except Exception as exc:
            return {
                "generated_at": int(time.time()),
                "registry_errors": [f"projects: unexpected error: {exc}"],
                "projects": [],
            }

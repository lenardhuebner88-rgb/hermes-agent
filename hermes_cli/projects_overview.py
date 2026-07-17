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

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
            # Live operator-waiting tasks: block_kind across any non-terminal
            # status bucket. Terminal tasks (done/archived) keep their historic
            # block_kind, so they must be excluded — else stale archived rows
            # inflate the attention ampel to false-red.
            "needs_input": _count(
                "block_kind = 'needs_input' AND status NOT IN ('done', 'archived')"
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


# ---------------------------------------------------------------------------
# Stage 3 — "who is working on what right now" (/api/projects/agents)
# ---------------------------------------------------------------------------
#
# Four independent sources (tmux, coordination notes, kanban, loops). Each is
# wrapped in its own try/except at the top of :func:`build_agents_payload` —
# an exception in one source degrades to a single ``errors[]`` entry, the
# other three still deliver. Every seam a test needs to override is a plain
# module-level function called by name at call time (``get_hermes_home``,
# ``_default_coordination_dir``, ``control_loops._all_pack_names``, ...) so
# ``monkeypatch.setattr`` on the module attribute is enough — same pattern
# stage 2 already uses for ``get_hermes_home``.

_TMUX_TIMEOUT_SECONDS = 2
_TMUX_LIST_PANES_CMD = [
    "tmux",
    "list-panes",
    "-a",
    "-F",
    "#{session_name}|#{window_index}|#{window_name}|#{pane_current_command}|#{pane_current_path}",
]
_TMUX_LIST_SESSIONS_CMD = ["tmux", "list-sessions", "-F", "#{session_name}|#{session_created}"]
_TMUX_SHELL_COMMANDS = frozenset({"bash", "zsh", "sh", "fish", "dash"})
# Priority order matters: first kind whose name appears wins (see
# _classify_tmux_kind).
_TMUX_KIND_ORDER = ("claude", "codex", "kimi", "grok", "hermes")

_COORDINATION_KIND_VALUES = frozenset(
    {"claude", "codex", "kimi", "grok", "hermes", "kanban", "loop"}
)
_COORDINATION_FRONTMATTER_BYTES = 4096


def _default_coordination_dir() -> Path:
    return Path.home() / "vault" / "_agents" / "_coordination"


def _attribute_project(paths: list[str], registry: ProjectsRegistry) -> str | None:
    """Longest ``repo_path`` prefix match across ``paths`` wins.

    Boundary-aware: a path only matches a registry entry if it equals the
    entry's ``repo_path`` or starts with ``repo_path + "/"`` — so
    ``/home/piet`` never matches an entry rooted at
    ``/home/piet/.hermes/hermes-agent`` (the reverse would be a false
    positive). Ties (a parent project and a sub-project sharing one
    ``repo_path``) resolve to the entry WITHOUT ``parent`` set.
    """
    best_len = -1
    candidates: list[ProjectEntry] = []
    for raw_path in paths:
        if not raw_path:
            continue
        candidate = raw_path.rstrip("/")
        for entry in registry.projects:
            repo = entry.repo_path.rstrip("/")
            if not repo:
                continue
            if candidate == repo or candidate.startswith(repo + "/"):
                if len(repo) > best_len:
                    best_len = len(repo)
                    candidates = [entry]
                elif len(repo) == best_len:
                    candidates.append(entry)

    if not candidates:
        return None
    without_parent = [entry for entry in candidates if entry.parent is None]
    chosen = without_parent[0] if without_parent else candidates[0]
    return chosen.slug


def _run_tmux_command(cmd: list[str]) -> tuple[str | None, str | None]:
    """Returns ``(stdout, error)``.

    ``("", None)`` is the truthful "tmux server not running" state (not an
    error) — ``tmux list-panes -a`` needs a running server and fails with
    ``"no server running"`` on stderr when there is none.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return None, "tmux: tmux is not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"tmux: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "no server running" in stderr.lower():
            return "", None
        return None, f"tmux: {stderr or 'tmux command failed'}"
    return result.stdout, None


def _classify_tmux_kind(window_name: str, pane_command: str) -> str:
    window_lower = window_name.lower()
    for kind in _TMUX_KIND_ORDER:
        if kind in window_lower:
            return kind
    command_lower = pane_command.lower()
    for kind in _TMUX_KIND_ORDER:
        if kind in command_lower:
            return kind
    return "unknown"


def _tmux_agents(
    *,
    tmux_panes_text: str | None,
    tmux_sessions_text: str | None,
    registry: ProjectsRegistry,
) -> tuple[list[dict[str, Any]], list[str]]:
    if tmux_panes_text is not None:
        panes_text, panes_error = tmux_panes_text, None
    else:
        panes_text, panes_error = _run_tmux_command(_TMUX_LIST_PANES_CMD)
    if panes_error:
        return [], [panes_error]
    if not panes_text:
        return [], []

    if tmux_sessions_text is not None:
        sessions_text, sessions_error = tmux_sessions_text, None
    else:
        sessions_text, sessions_error = _run_tmux_command(_TMUX_LIST_SESSIONS_CMD)
    if sessions_error:
        return [], [sessions_error]

    session_created: dict[str, int] = {}
    for line in (sessions_text or "").splitlines():
        parts = line.split("|")
        if len(parts) != 2:
            continue
        session_name, created_raw = parts
        try:
            session_created[session_name] = int(created_raw)
        except ValueError:
            continue

    agents: list[dict[str, Any]] = []
    for line in panes_text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        session_name, window_index, window_name, pane_command, pane_path = parts
        if pane_command.strip().lower() in _TMUX_SHELL_COMMANDS:
            continue
        agents.append(
            {
                "kind": _classify_tmux_kind(window_name, pane_command),
                "label": f"{session_name}:{window_index} {window_name}",
                "task": None,
                "project": _attribute_project([pane_path], registry),
                "since": session_created.get(session_name),
                "source": "tmux",
            }
        )
    return agents, []


def _extract_frontmatter(text: str) -> str | None:
    """Text between the first two ``---`` lines, or ``None`` if malformed."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return None


def _parse_coordination_timestamp(value: Any) -> int | None:
    """Parses a frontmatter ``started``/``ended`` value.

    ``yaml.safe_load`` already turns an unquoted ISO instant into a
    ``datetime`` (PyYAML's implicit timestamp resolver) — handle both that
    and the plain-string case. Naive values (no explicit zone) are assumed to
    be local-clock artifacts, matching stage 2's loop-heartbeat handling.
    """
    dt: datetime | None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp())


def _strip_touching_annotation(raw: str) -> str:
    """Strips a trailing `` (neu)``-style annotation off a ``touching`` path."""
    idx = raw.find(" (")
    return raw[:idx] if idx != -1 else raw


def _parse_coordination_note(
    path: Path, registry: ProjectsRegistry
) -> dict[str, Any] | None:
    """Read + parse one coordination note into an agent dict, or ``None`` to skip.

    Pure per-file body used by :func:`_coordination_agents` (and unit-tested in
    isolation). Unreadable files, garbage frontmatter, closed notes, and notes
    missing required fields all return ``None`` — never raise into ``errors[]``.
    Workers only read their own file plus the shared read-only ``registry``.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(_COORDINATION_FRONTMATTER_BYTES)
    except OSError:
        # Unreadable single note: many legacy notes are dirty — skip
        # silently rather than spam errors[] per file.
        return None

    text = raw.decode("utf-8", errors="replace")
    frontmatter_text = _extract_frontmatter(text)
    if frontmatter_text is None:
        return None
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    agent_field = data.get("agent")
    started_raw = data.get("started")
    if not isinstance(agent_field, str) or not agent_field.strip() or started_raw is None:
        return None

    ended_val = data.get("ended")
    if ended_val not in (None, ""):
        return None  # note is closed

    kind = agent_field.strip().lower()
    if kind not in _COORDINATION_KIND_VALUES:
        kind = "unknown"

    task_raw = data.get("task")
    task = task_raw if isinstance(task_raw, str) else None

    touching_raw = data.get("touching")
    touching_paths: list[str] = []
    if isinstance(touching_raw, list):
        touching_paths = [
            _strip_touching_annotation(item) for item in touching_raw if isinstance(item, str)
        ]

    return {
        "kind": kind,
        "label": path.stem,
        "task": task,
        "project": _attribute_project(touching_paths, registry),
        "since": _parse_coordination_timestamp(started_raw),
        "source": "coordination",
    }


def _coordination_agents(
    coordination_dir: Path, *, registry: ProjectsRegistry
) -> tuple[list[dict[str, Any]], list[str]]:
    if not coordination_dir.exists():
        # No-config default: nothing to scan is not an error, mirroring the
        # missing-registry-file default elsewhere in this module.
        return [], []
    if not coordination_dir.is_dir():
        return [], [f"coordination: {coordination_dir} is not a directory"]

    try:
        note_paths = sorted(coordination_dir.glob("*.md"))
    except OSError as exc:
        return [], [f"coordination: {exc}"]

    if not note_paths:
        return [], []

    # I/O-bound per-file work: bounded thread pool. Collect by input order
    # (sorted glob), not completion order, so the agents list stays deterministic.
    max_workers = min(8, os.cpu_count() or 4)
    agents: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        # map preserves the order of note_paths.
        for parsed in pool.map(
            lambda path: _parse_coordination_note(path, registry), note_paths
        ):
            if parsed is not None:
                agents.append(parsed)

    return agents, []


def _kanban_running_agents(
    *, kanban_db_path: Path, projects_db_path: Path, registry: ProjectsRegistry
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        conn = _open_sqlite_ro(kanban_db_path)
    except sqlite3.DatabaseError as exc:
        return [], [f"kanban: could not open kanban.db: {exc}"]

    try:
        rows = conn.execute(
            "SELECT id, title, status, project_id, started_at, assignee FROM tasks "
            "WHERE status = 'running'"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        return [], [f"kanban: {exc}"]
    finally:
        conn.close()

    # project_id -> board_slug, reversing stage 2's board -> project_id
    # lookup. An unreadable/missing projects.db just means project_id-bound
    # tasks resolve to project=None — not a hard error (default-board tasks
    # with project_id NULL never need this map at all).
    board_slug_by_project_id: dict[str, str | None] = {}
    try:
        pconn = _open_sqlite_ro(projects_db_path)
        try:
            for row in projects_db.list_projects(pconn, include_archived=True):
                board_slug_by_project_id[row.id] = row.board_slug
        finally:
            pconn.close()
    except sqlite3.DatabaseError:
        pass

    default_slug = next(
        (entry.slug for entry in registry.projects if entry.kanban_project == "default"),
        None,
    )
    slug_by_board_slug = {
        entry.kanban_project: entry.slug
        for entry in registry.projects
        if entry.kanban_project and entry.kanban_project != "default"
    }

    agents: list[dict[str, Any]] = []
    for row in rows:
        project_id = row["project_id"]
        if project_id is None:
            project = default_slug
        else:
            board_slug = board_slug_by_project_id.get(project_id)
            if board_slug == "default":
                # Tasks explicitly bound to the default board's project row
                # (the common live case) belong to the default project too.
                project = default_slug
            else:
                project = slug_by_board_slug.get(board_slug) if board_slug else None
        agents.append(
            {
                "kind": "kanban",
                "label": row["id"],
                "task": row["title"],
                "project": project,
                "since": row["started_at"],
                "source": "kanban",
            }
        )
    return agents, []


def _loop_started_at(heartbeat: dict[str, Any] | None) -> int | None:
    if not heartbeat:
        return None
    current = heartbeat.get("current")
    if not isinstance(current, dict):
        return None
    return _parse_loop_iso_timestamp(current.get("started_at"))


def _loop_agents(
    *,
    registry: ProjectsRegistry,
    loops_state_root: Path | None,
    pack_names: list[str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    root = loops_state_root if loops_state_root is not None else control_loops._state_root()
    names = (
        pack_names
        if pack_names is not None
        else [name for name, _source in control_loops._all_pack_names()]
    )

    project_by_pack: dict[str, str] = {}
    for entry in registry.projects:
        for pack_name in entry.loop_packs:
            project_by_pack.setdefault(pack_name, entry.slug)

    agents: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in names:
        try:
            state = root / name
            running = control_loops._is_running(state)
            if not running:
                continue
            since = _loop_started_at(control_loops._heartbeat(state))
        except Exception as exc:  # isolate: one bad pack never kills the list
            errors.append(f"loops: pack '{name}': {exc}")
            continue
        agents.append(
            {
                "kind": "loop",
                "label": name,
                "task": None,
                "project": project_by_pack.get(name),
                "since": since,
                "source": "loop",
            }
        )
    return agents, errors


def build_agents_payload(
    registry: ProjectsRegistry,
    *,
    tmux_panes_text: str | None = None,
    tmux_sessions_text: str | None = None,
    coordination_dir: Path | None = None,
    kanban_db_path: Path | None = None,
    projects_db_path: Path | None = None,
    loops_state_root: Path | None = None,
    pack_names: list[str] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Assemble the frozen ``/api/projects/agents`` payload.

    Four independent sources — tmux panes, coordination notes, kanban
    running tasks, active loop packs — each isolated: an exception in one
    becomes a single ``errors[]`` entry, the others still deliver. ``None``
    for any keyword means "use the real source"; tests inject fixtures.
    """
    resolved_now = now if now is not None else int(time.time())
    home = get_hermes_home()
    resolved_kanban_db_path = kanban_db_path if kanban_db_path is not None else home / "kanban.db"
    resolved_projects_db_path = (
        projects_db_path if projects_db_path is not None else home / "projects.db"
    )
    resolved_coordination_dir = (
        coordination_dir if coordination_dir is not None else _default_coordination_dir()
    )

    agents: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        tmux_result = _tmux_agents(
            tmux_panes_text=tmux_panes_text,
            tmux_sessions_text=tmux_sessions_text,
            registry=registry,
        )
    except Exception as exc:
        tmux_result = [], [f"tmux: {exc}"]
    agents.extend(tmux_result[0])
    errors.extend(tmux_result[1])

    try:
        coordination_result = _coordination_agents(resolved_coordination_dir, registry=registry)
    except Exception as exc:
        coordination_result = [], [f"coordination: {exc}"]
    agents.extend(coordination_result[0])
    errors.extend(coordination_result[1])

    try:
        kanban_result = _kanban_running_agents(
            kanban_db_path=resolved_kanban_db_path,
            projects_db_path=resolved_projects_db_path,
            registry=registry,
        )
    except Exception as exc:
        kanban_result = [], [f"kanban: {exc}"]
    agents.extend(kanban_result[0])
    errors.extend(kanban_result[1])

    try:
        loop_result = _loop_agents(
            registry=registry,
            loops_state_root=loops_state_root,
            pack_names=pack_names,
        )
    except Exception as exc:
        loop_result = [], [f"loops: {exc}"]
    agents.extend(loop_result[0])
    errors.extend(loop_result[1])

    return {
        "generated_at": resolved_now,
        "errors": errors,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# Stage 6 — project drilldown (/api/projects/{slug})
# ---------------------------------------------------------------------------
#
# Read-only detail payload for one registry entry. Same isolation contract as
# stages 2/3: git / kanban / loops each degrade independently (empty list or
# null + ``errors[]``); the route never 500s. Unknown slug → 404 JSON body
# ``{"error": "unknown project", "slug": …}`` (not a 500).

_RECENT_COMMITS_LIMIT = 10
_KANBAN_TASKS_LIMIT = 25
_KANBAN_OPEN_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked")
_LEDGER_TAIL_BYTES = 4096


def _project_recent_commits(
    entry: ProjectEntry, *, now: int
) -> tuple[list[dict[str, Any]], str | None]:
    """``git log -n 10`` for ``entry`` → ``(commits, error)``.

    Same field-splitting and ``path_filters`` pathspecs as
    :func:`_project_last_commit`; empty repo / no matching commits → ``[]``
    without an error.
    """
    cmd = [
        "git",
        "-C",
        entry.repo_path,
        "log",
        f"-n{_RECENT_COMMITS_LIMIT}",
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
        return [], f"git: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git log failed").strip()
        return [], f"git: {detail or 'git log failed'}"

    commits: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) != 3:
            return [], "git: unexpected `git log` output"
        commit_hash, message, committed_at_raw = parts
        try:
            committed_at = int(committed_at_raw)
        except ValueError:
            return [], "git: unparsable commit timestamp"
        commits.append(
            {
                "hash": commit_hash,
                "message": message,
                "committed_at": committed_at,
                "age_seconds": max(0, now - committed_at),
            }
        )
    return commits, None


def _kanban_tasks(
    entry: ProjectEntry, *, kanban_db_path: Path, projects_db_path: Path, now: int
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Open + blocked tasks for the project's board, newest/priority order.

    Returns ``(None, None)`` when the registry entry has no ``kanban_project``
    (honest "no board" — not an error). Same board→project_id scoping as
    :func:`_kanban_counts`.
    """
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

    status_placeholders = ", ".join("?" for _ in _KANBAN_OPEN_STATUSES)
    try:
        conn = _open_sqlite_ro(kanban_db_path)
    except sqlite3.DatabaseError as exc:
        return None, f"kanban: could not open kanban.db: {exc}"

    try:
        rows = conn.execute(
            f"SELECT id, title, status, block_kind, priority, created_at "
            f"FROM tasks WHERE {scope_clause} AND status IN ({status_placeholders}) "
            f"ORDER BY priority DESC, created_at DESC "
            f"LIMIT {_KANBAN_TASKS_LIMIT}",
            scope_params + _KANBAN_OPEN_STATUSES,
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        return None, f"kanban: {exc}"
    finally:
        conn.close()

    tasks: list[dict[str, Any]] = []
    for row in rows:
        created_at = int(row["created_at"] or 0)
        block_kind = row["block_kind"]
        tasks.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "block_kind": block_kind if isinstance(block_kind, str) else None,
                "priority": int(row["priority"] or 0),
                "created_at": created_at,
                "age_seconds": max(0, now - created_at),
            }
        )
    return tasks, None


def _read_file_tail_text(path: Path, *, max_bytes: int = _LEDGER_TAIL_BYTES) -> str | None:
    """Read the last ``max_bytes`` of a text file; drop a partial first line."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, 2)
                data = fh.read()
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1 :]
            else:
                data = fh.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _loop_last_outcome(state_dir: Path) -> dict[str, Any] | None:
    """Most recent ``ledger.jsonl`` line that parses and carries a ``verdict``.

    Reads only the file tail (≈4 KB) so a huge ledger never lands fully in
    memory. Missing ledger → ``None`` (not an error).
    """
    ledger = state_dir / "ledger.jsonl"
    if not ledger.is_file():
        return None
    text = _read_file_tail_text(ledger)
    if not text:
        return None

    # Walk newest-first among the tail lines.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or "verdict" not in event:
            continue
        verdict = event.get("verdict")
        if not isinstance(verdict, str):
            continue
        phase = event.get("phase")
        reason = event.get("reason")
        plan = event.get("plan")
        return {
            "verdict": verdict,
            "phase": phase if isinstance(phase, str) else None,
            "reason": reason if isinstance(reason, str) else None,
            "plan": plan if isinstance(plan, str) else None,
            "ts": _parse_loop_iso_timestamp(event.get("ts")),
        }
    return None


def _detail_loops_for_entry(
    entry: ProjectEntry, *, loops_state_root: Path | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """One row per registry ``loop_pack`` with running + last ledger outcome."""
    root = loops_state_root if loops_state_root is not None else control_loops._state_root()
    errors: list[str] = []
    packs: list[dict[str, Any]] = []

    for name in entry.loop_packs:
        try:
            state = root / name
            running = control_loops._is_running(state)
            heartbeat = control_loops._heartbeat(state)
            heartbeat_at = _last_heartbeat_at(heartbeat)
            last_outcome = _loop_last_outcome(state)
        except Exception as exc:  # isolate: one bad pack never kills the list
            errors.append(f"loops: pack '{name}': {exc}")
            packs.append(
                {
                    "name": name,
                    "running": False,
                    "last_heartbeat_at": None,
                    "last_outcome": None,
                }
            )
            continue
        packs.append(
            {
                "name": name,
                "running": running,
                "last_heartbeat_at": heartbeat_at,
                "last_outcome": last_outcome,
            }
        )
    return packs, errors


def build_project_detail(
    entry: ProjectEntry,
    registry: ProjectsRegistry,
    *,
    kanban_db_path: Path | None = None,
    projects_db_path: Path | None = None,
    loops_state_root: Path | None = None,
    now: int | None = None,
    tmux_panes_text: str | None = None,
    tmux_sessions_text: str | None = None,
    coordination_dir: Path | None = None,
    pack_names: list[str] | None = None,
    agents_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the frozen ``GET /api/projects/{slug}`` detail payload.

    Pure-ish: all data sources are overridable so tests never touch real
    ``~/.hermes`` state. Agents are :func:`build_agents_payload` filtered to
    ``project == entry.slug`` (``project`` field dropped from each agent).

    Pass ``agents_payload`` (e.g. the route-level TTL-cached agents payload)
    to skip re-running discovery on every drilldown poll. Filtering always
    builds a new agent list and copies each kept agent dict — the shared
    cached structure is never mutated in place.
    """
    resolved_now = now if now is not None else int(time.time())
    home = get_hermes_home()
    resolved_kanban_db_path = kanban_db_path if kanban_db_path is not None else home / "kanban.db"
    resolved_projects_db_path = (
        projects_db_path if projects_db_path is not None else home / "projects.db"
    )

    errors: list[str] = []

    try:
        recent_commits, git_error = _project_recent_commits(entry, now=resolved_now)
    except Exception as exc:
        recent_commits, git_error = [], f"git: {exc}"
    if git_error:
        errors.append(git_error)

    try:
        kanban_tasks, kanban_error = _kanban_tasks(
            entry,
            kanban_db_path=resolved_kanban_db_path,
            projects_db_path=resolved_projects_db_path,
            now=resolved_now,
        )
    except Exception as exc:
        kanban_tasks, kanban_error = None, f"kanban: {exc}"
    if kanban_error:
        errors.append(kanban_error)

    try:
        loops, loop_errors = _detail_loops_for_entry(entry, loops_state_root=loops_state_root)
    except Exception as exc:
        loops, loop_errors = [], [f"loops: {exc}"]
    errors.extend(loop_errors)

    try:
        if agents_payload is not None:
            source_payload = agents_payload
        else:
            source_payload = build_agents_payload(
                registry,
                tmux_panes_text=tmux_panes_text,
                tmux_sessions_text=tmux_sessions_text,
                coordination_dir=coordination_dir,
                kanban_db_path=resolved_kanban_db_path,
                projects_db_path=resolved_projects_db_path,
                loops_state_root=loops_state_root,
                pack_names=pack_names,
                now=resolved_now,
            )
        # New list + new dicts per agent: never pop/mutate shared cache entries.
        agents = [
            {
                "kind": agent["kind"],
                "label": agent["label"],
                "task": agent.get("task"),
                "since": agent.get("since"),
                "source": agent["source"],
            }
            for agent in source_payload.get("agents", [])
            if agent.get("project") == entry.slug
        ]
        # Agents-source errors stay on the agents endpoint; detail only
        # surfaces git/kanban/loops isolation (agents list simply empty).
    except Exception as exc:
        agents = []
        errors.append(f"agents: {exc}")

    return {
        "generated_at": resolved_now,
        "slug": entry.slug,
        "name": entry.name,
        "repo_path": entry.repo_path,
        "parent": entry.parent,
        "links": [{"label": link.label, "url": link.url} for link in entry.links],
        "recent_commits": recent_commits,
        "kanban_tasks": kanban_tasks,
        "loops": loops,
        "agents": agents,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Stage 9 — short process-level TTL cache for route handlers
# ---------------------------------------------------------------------------
#
# Polling (agents ~12s, detail ~8s, grid) and multiple browser tabs must not
# re-pay the coordination-dir scan on every hit. Cache sits at the ROUTE
# boundary only — builders stay pure/injectable for tests. Values are treated
# as read-only; :meth:`_TtlMemo.get` returns a shallow copy of the top-level
# dict so accidental top-level mutation cannot bleed across requests.

# 10s (top of the "~5-10s" budget): the frontend polls agents ~12s and the
# drilldown ~8s, so a TTL below the poll interval would miss on every single
# poll and never actually serve from cache. 10s lets the 8s detail poll reuse
# the prior agents scan; staleness of "who is working / last commit" at ~10s is
# fine for a read-only leitstand.
_PROJECTS_CACHE_TTL_SECONDS = 10.0

# Injectable clock for tests (no wall-clock sleeps). Production: monotonic.
_clock: Callable[[], float] = time.monotonic


class _TtlMemo:
    """Thread-safe TTL memo: one entry per logical key, injectable clock."""

    def __init__(self, ttl: float = _PROJECTS_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            stored_at, value = item
            if _clock() - stored_at >= self._ttl:
                del self._store[key]
                return None
            # Top-level dict + top-level lists are copied so rebinding keys or
            # clearing/replacing a list on the returned value cannot bleed into
            # the store. Nested dicts (agent rows, project cards) stay shared
            # and must be treated as read-only (detail filtering always copies).
            return {
                k: (list(v) if isinstance(v, list) else v) for k, v in value.items()
            }

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._store[key] = (_clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_projects_cache = _TtlMemo(ttl=_PROJECTS_CACHE_TTL_SECONDS)


def _reset_projects_cache() -> None:
    """Test hook: drop all cached route payloads so suites stay isolated."""
    _projects_cache.clear()


def _cache_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a request-local view of a cached payload (top-level lists copied)."""
    return {k: (list(v) if isinstance(v, list) else v) for k, v in payload.items()}


def _cached_projects_payload() -> dict[str, Any]:
    """Route accessor for ``GET /api/projects`` (TTL 5s, key ``projects``)."""
    cached = _projects_cache.get("projects")
    if cached is not None:
        return cached
    registry = load_projects_registry()
    payload = build_projects_payload(registry)
    _projects_cache.set("projects", payload)
    return _cache_view(payload)


def _cached_agents_payload() -> dict[str, Any]:
    """Route accessor for ``GET /api/projects/agents`` (TTL 5s, key ``agents``).

    Also used by the detail route so drilldown reuses the same cached agents
    scan instead of re-running coordination discovery (~314ms cold path).
    """
    cached = _projects_cache.get("agents")
    if cached is not None:
        return cached
    registry = load_projects_registry()
    payload = build_agents_payload(registry)
    _projects_cache.set("agents", payload)
    return _cache_view(payload)


def register_projects_routes(app: FastAPI) -> None:
    """Register read-only ``GET /api/projects`` (+ agents + ``{slug}`` detail).

    Auth comes automatically from the dashboard's ``/api/*`` middleware —
    nothing to do here as long as the path stays out of the public whitelist.
    Each handler is wrapped once more on top of its builder's own per-source
    isolation so a truly unexpected failure (e.g. the registry file itself
    becoming unreadable) still answers with JSON, never a 500.

    Unknown slug on the detail route answers **404** with body
    ``{"error": "unknown project", "slug": <slug>}`` (JSON, never a 500).
    Static ``/api/projects/agents`` is registered before ``/{slug}`` so the
    path is never captured as a slug.

    ``get_projects`` / ``get_project_agents`` consult a short process-level
    TTL cache (~5s). ``get_project_detail`` reuses the cached agents payload
    (does not cache per-slug detail bodies).
    """

    @app.get("/api/projects")
    def get_projects() -> dict[str, Any]:
        try:
            return _cached_projects_payload()
        except Exception as exc:
            return {
                "generated_at": int(time.time()),
                "registry_errors": [f"projects: unexpected error: {exc}"],
                "projects": [],
            }

    @app.get("/api/projects/agents")
    def get_project_agents() -> dict[str, Any]:
        try:
            return _cached_agents_payload()
        except Exception as exc:
            return {
                "generated_at": int(time.time()),
                "errors": [f"agents: unexpected error: {exc}"],
                "agents": [],
            }

    @app.get("/api/projects/{slug}")
    def get_project_detail(slug: str) -> Any:
        try:
            registry = load_projects_registry()
            entry = next((p for p in registry.projects if p.slug == slug), None)
            if entry is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "unknown project", "slug": slug},
                )
            # Reuse the same TTL-cached agents scan the agents route serves —
            # do not re-pay coordination discovery on every drilldown poll.
            agents_payload = _cached_agents_payload()
            return build_project_detail(entry, registry, agents_payload=agents_payload)
        except Exception as exc:
            return {
                "generated_at": int(time.time()),
                "slug": slug,
                "name": "",
                "repo_path": "",
                "parent": None,
                "links": [],
                "recent_commits": [],
                "kanban_tasks": None,
                "loops": [],
                "agents": [],
                "errors": [f"projects: unexpected error: {exc}"],
            }

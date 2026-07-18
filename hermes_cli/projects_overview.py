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
import copy
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    session_profiles: list[str] = field(default_factory=list)
    session_sources: list[str] = field(default_factory=list)


@dataclass
class ProjectsRegistry:
    """Ergebnis von :func:`load_projects_registry`."""

    projects: list[ProjectEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# A slug becomes a URL path segment (GET /api/projects/{slug}) and a React key.
# Keep it URL-safe and reject segments that collide with sibling static routes
# (/api/projects/agents, /api/projects/sessions, /api/projects/commits — all
# registered before /{slug}).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_RESERVED_SLUGS = frozenset({"agents", "sessions", "commits", "receipts"})

_RECEIPTS_ROOT = Path.home() / "vault" / "03-Agents"
_RECEIPTS_HEAD_BYTES = 4 * 1024
_RECEIPTS_LIMIT = 30
_RECEIPT_CONTENT_LIMIT_BYTES = 200 * 1024


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
    slug = slug.strip()
    if not _SLUG_RE.match(slug):
        errors.append(
            f"project at {label}: slug {slug!r} must be URL-safe "
            "([a-z0-9] start, then [a-z0-9._-])"
        )
        return None
    if slug in _RESERVED_SLUGS:
        # A project slugged e.g. 'agents' would collide with the static route
        # GET /api/projects/agents (registered before /{slug}), making its
        # detail endpoint unreachable.
        errors.append(f"project at {label}: slug {slug!r} is reserved")
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

    session_profiles = _parse_str_list(
        slug, "session_profiles", raw.get("session_profiles"), errors
    )
    if session_profiles is None:
        return None

    session_sources = _parse_str_list(
        slug, "session_sources", raw.get("session_sources"), errors
    )
    if session_sources is None:
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
        session_profiles=session_profiles,
        session_sources=session_sources,
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
_COMMIT_LOOP_RE = re.compile(r"^loop\(([^)]+)\):(?:\s|$)")
_COMMIT_TASK_RE = re.compile(r"^(kanban|wip)\((t_[0-9a-f]{8})\):(?:\s|$)")
_COMMIT_MERGE_RE = re.compile(
    r"^kanban:\s+merge\s+kanban/(t_[0-9a-f]{8})(?:\s|$)"
)
_COMMIT_REVERT_RE = re.compile(r'^(?:Revert|Reapply)\s+"')
# Wire instants must carry an explicit zone (``Z`` or ``+HH:MM``) to be treated
# as unambiguous. Real loop heartbeat.json files mix zoned and naive
# timestamps (naive ones are local-clock artifacts); naive strings are dropped
# rather than silently misinterpreted as UTC.
_ISO_WITH_ZONE_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _git_log_commits(
    entry: ProjectEntry, *, limit: int, now: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Runs ``git log -n <limit>`` for ``entry`` → ``(commits, error)``.

    Shared by the card's last commit, the detail drilldown's recent commits
    and the cross-project commit feed — one format string, one parser. Each
    row carries ``hash``, ``message``, ``author`` (``%an``), ``committed_at``
    and ``age_seconds``.

    ``path_filters`` (subproject slices of a parent repo) are appended as
    pathspecs after ``--`` so the subproject's own touching commits are
    reported instead of the parent repo's overall HEAD. An existing repo with
    no matching commits yields ``([], None)`` — not an error.
    """
    cmd = [
        "git",
        "-C",
        entry.repo_path,
        "log",
        f"-n{limit}",
        "--abbrev=9",
        "--format=%h\x1f%s\x1f%ct\x1f%an",
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
        if len(parts) != 4:
            return [], "git: unexpected `git log` output"
        commit_hash, message, committed_at_raw, author = parts
        try:
            committed_at = int(committed_at_raw)
        except ValueError:
            return [], "git: unparsable commit timestamp"
        commits.append(
            {
                "hash": commit_hash,
                "message": message,
                "author": author,
                "committed_at": committed_at,
                "age_seconds": max(0, now - committed_at),
            }
        )
    return commits, None


def _annotate_commit_attribution(
    commits: list[dict[str, Any]], *, kanban_db_path: Path | None
) -> None:
    """Add best-effort commit provenance without changing existing fields.

    Subject prefixes provide the stable attribution kind and task/pack key.
    Task-backed commits are resolved in one read-only DB open and one batched
    query; an absent/old/broken Kanban DB leaves nullable fields empty and
    never removes the commit from its source payload.
    """
    task_attributions: list[dict[str, Any]] = []
    task_ids: set[str] = set()

    for commit in commits:
        message = commit.get("message")
        message = message if isinstance(message, str) else ""
        author = commit.get("author")
        author = author if isinstance(author, str) and author else None
        attribution: dict[str, Any] = {
            "kind": "direct",
            "pack": None,
            "task_id": None,
            "lane": None,
            "model": None,
            "label": author,
        }

        loop_match = _COMMIT_LOOP_RE.match(message)
        task_match = _COMMIT_TASK_RE.match(message)
        merge_match = _COMMIT_MERGE_RE.match(message)
        if loop_match is not None:
            pack = loop_match.group(1)
            attribution.update(kind="loop", pack=pack, label=pack)
        elif task_match is not None:
            task_id = task_match.group(2)
            attribution.update(
                kind=task_match.group(1), task_id=task_id, label=None
            )
            task_attributions.append(attribution)
            task_ids.add(task_id)
        elif merge_match is not None:
            task_id = merge_match.group(1)
            attribution.update(kind="merge", task_id=task_id, label=None)
            task_attributions.append(attribution)
            task_ids.add(task_id)
        elif _COMMIT_REVERT_RE.match(message) is not None:
            attribution.update(kind="revert", label=None)

        commit["attribution"] = attribution

    if not task_ids or kanban_db_path is None:
        return

    try:
        conn = _open_sqlite_ro(kanban_db_path)
    except sqlite3.DatabaseError:
        return

    ordered_task_ids = sorted(task_ids)
    placeholders = ", ".join("?" for _task_id in ordered_task_ids)
    try:
        rows = conn.execute(
            f"""
            WITH ranked_runs AS (
                SELECT task_id, active_model, requested_model,
                       ROW_NUMBER() OVER (
                           PARTITION BY task_id
                           ORDER BY started_at DESC, id DESC
                       ) AS run_rank
                FROM task_runs
                WHERE task_id IN ({placeholders})
            )
            SELECT tasks.id AS task_id, tasks.assignee AS lane,
                   ranked_runs.active_model, ranked_runs.requested_model
            FROM tasks
            LEFT JOIN ranked_runs
              ON ranked_runs.task_id = tasks.id AND ranked_runs.run_rank = 1
            WHERE tasks.id IN ({placeholders})
            """,
            (*ordered_task_ids, *ordered_task_ids),
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()

    resolved: dict[str, tuple[str | None, str | None]] = {}
    for row in rows:
        lane_raw = row["lane"]
        active_model_raw = row["active_model"]
        requested_model_raw = row["requested_model"]
        lane = (
            lane_raw.strip()
            if isinstance(lane_raw, str) and lane_raw.strip()
            else None
        )
        active_model = (
            active_model_raw.strip()
            if isinstance(active_model_raw, str) and active_model_raw.strip()
            else None
        )
        requested_model = (
            requested_model_raw.strip()
            if isinstance(requested_model_raw, str) and requested_model_raw.strip()
            else None
        )
        resolved[row["task_id"]] = (lane, active_model or requested_model)

    for attribution in task_attributions:
        lane, model = resolved.get(attribution["task_id"], (None, None))
        attribution["lane"] = lane
        attribution["model"] = model


def _project_last_commit(
    entry: ProjectEntry, *, now: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Returns ``(last_commit, error)`` — the newest row of the shared log
    helper, or ``(None, None)`` when the repo simply has no matching commits."""
    commits, error = _git_log_commits(entry, limit=1, now=now)
    if error is not None or not commits:
        return None, error
    return commits[0], None


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
        # A projects.db READ failure is a real degradation, even for the default
        # board: without it we cannot count tasks explicitly bound to the default
        # project row, so returning "no binding" would silently drop them.
        # Distinguish this from the readable-but-no-row case below (which
        # legitimately counts NULL-scoped legacy tasks only).
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
            # Karten-Attention-Ampel braucht den letzten Ledger-Verdict auch
            # in der Liste (bisher nur im Detail) — gleiche Shape, ~4KB-Tail.
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
        if running:
            active += 1
        packs.append(
            {
                "name": name,
                "running": running,
                "last_heartbeat_at": heartbeat_at,
                "last_outcome": last_outcome,
            }
        )

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
    last_commits_to_annotate: list[dict[str, Any]] = []
    for entry in registry.projects:
        errors: list[str] = []

        try:
            last_commit, git_error = _project_last_commit(entry, now=resolved_now)
            if last_commit is not None:
                last_commits_to_annotate.append(last_commit)
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
                # Board-Slug fürs Fleet-Deep-Link der Karten-Chips
                # (?board=<slug> — Frontend linkt nur wenn gesetzt).
                "kanban_project": entry.kanban_project,
                "links": [{"label": link.label, "url": link.url} for link in entry.links],
                "last_commit": last_commit,
                "kanban": kanban,
                "loops": loops,
                "errors": errors,
            }
        )

    _annotate_commit_attribution(
        last_commits_to_annotate, kanban_db_path=resolved_kanban_db_path
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
_TMUX_SCAN_CACHE_TTL_SECONDS = 3.0
_TMUX_LIST_PANES_CMD = [
    "tmux",
    "list-panes",
    "-a",
    "-F",
    "#{session_name}|#{window_index}|#{window_name}|#{pane_current_command}|#{pane_current_path}"
    "|#{@hermes_kind}|#{@hermes_workdir}|#{@hermes_task_id}|#{@hermes_session_id}",
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

_tmux_scan_clock: Callable[[], float] = time.monotonic
_tmux_scan_cache_lock = threading.Lock()
_tmux_scan_cache: dict[
    tuple[str, ...], tuple[float, tuple[str | None, str | None]]
] = {}


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
        # Collapse ``..`` / ``.`` segments before matching so a touching-path
        # like ``/home/piet/repo/../outside`` cannot be mis-attributed to a
        # project rooted at ``/home/piet/repo``.
        candidate = os.path.normpath(raw_path).rstrip("/")
        for entry in registry.projects:
            repo = os.path.normpath(entry.repo_path).rstrip("/")
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


def _cached_tmux_command(cmd: list[str]) -> tuple[str | None, str | None]:
    """Share one raw tmux subprocess result across concurrent payload builds."""
    key = tuple(cmd)
    with _tmux_scan_cache_lock:
        cached = _tmux_scan_cache.get(key)
        now = _tmux_scan_clock()
        if cached is not None and now - cached[0] < _TMUX_SCAN_CACHE_TTL_SECONDS:
            return cached[1]

        result = _run_tmux_command(cmd)
        _tmux_scan_cache[key] = (_tmux_scan_clock(), result)
        return result


def _reset_tmux_scan_cache() -> None:
    """Test hook: clear cached raw tmux subprocess results."""
    with _tmux_scan_cache_lock:
        _tmux_scan_cache.clear()


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
    kanban_db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if tmux_panes_text is not None:
        panes_text, panes_error = tmux_panes_text, None
    else:
        panes_text, panes_error = _cached_tmux_command(_TMUX_LIST_PANES_CMD)
    if panes_error:
        return [], [panes_error]
    if not panes_text:
        return [], []

    if tmux_sessions_text is not None:
        sessions_text, sessions_error = tmux_sessions_text, None
    else:
        sessions_text, sessions_error = _cached_tmux_command(_TMUX_LIST_SESSIONS_CMD)
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
    task_ids: set[str] = set()
    for line in panes_text.splitlines():
        parts = line.split("|")
        if len(parts) != 9:
            continue
        (
            session_name,
            window_index,
            window_name,
            pane_command,
            pane_path,
            hermes_kind,
            _hermes_workdir,
            hermes_task_id,
            hermes_session_id,
        ) = parts
        if pane_command.strip().lower() in _TMUX_SHELL_COMMANDS:
            continue
        kind = hermes_kind.strip() or _classify_tmux_kind(window_name, pane_command)
        task_id = hermes_task_id.strip() or None
        session_id = hermes_session_id.strip() or None
        if task_id is not None:
            task_ids.add(task_id)
        agents.append(
            {
                "kind": kind,
                "label": f"{session_name}:{window_index} {window_name}",
                "task": None,
                "session_id": session_id,
                "task_id": task_id,
                "project": _attribute_project([pane_path], registry),
                "since": session_created.get(session_name),
                "source": "tmux",
                # Structured kill target for the Projekte-Tab session rows:
                # POST /api/agent-terminals/terminate takes (session, window).
                # Frontend must NEVER re-parse these out of the display label.
                "tmux_session": session_name,
                "tmux_window": window_index,
                # Terminal deep-link target: /control/agent-terminals keys its
                # window list by #{window_name}, NOT by index — the index-based
                # tmux_window stays reserved for the terminate API.
                "tmux_window_name": window_name,
            }
        )

    if not task_ids or kanban_db_path is None:
        return agents, []

    try:
        conn = _open_sqlite_ro(kanban_db_path)
    except sqlite3.DatabaseError as exc:
        return agents, [f"kanban: could not open kanban.db: {exc}"]
    try:
        placeholders = ",".join("?" for _task_id in task_ids)
        rows = conn.execute(
            f"SELECT id, title FROM tasks WHERE id IN ({placeholders})",
            sorted(task_ids),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        return agents, [f"kanban: {exc}"]
    finally:
        conn.close()

    titles = {row["id"]: row["title"] for row in rows}
    for agent in agents:
        task_id = agent["task_id"]
        if task_id in titles:
            agent["task"] = titles[task_id]
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


_COORDINATION_KEY_LINE = re.compile(r"^[A-Za-z_][\w-]*:")


def _extract_lenient_frontmatter(text: str) -> str | None:
    """Extract the canonical leading block from a fence-less session note."""
    lines = text.splitlines()
    if not lines or not _COORDINATION_KEY_LINE.match(lines[0]):
        return None
    block: list[str] = []
    for line in lines:
        if _COORDINATION_KEY_LINE.match(line) or line.startswith("  - "):
            block.append(line)
            continue
        break
    return "\n".join(block) if block else None


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
        frontmatter_text = _extract_lenient_frontmatter(text)
    if frontmatter_text is None:
        return None
    try:
        data = yaml.safe_load(frontmatter_text)
    except Exception:
        # yaml.YAMLError for ordinary garbage, but a pathologically nested doc
        # can raise RecursionError (not a YAMLError). This runs inside a thread
        # pool via pool.map, which propagates the FIRST exception and would kill
        # the whole coordination scan — so one bad note must never escape here.
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

    # Live coordination notes carry an ``operator:`` field (who the agent works
    # for) — surface it so the tab can answer "für wen" beside "wer/woran".
    operator_raw = data.get("operator")
    operator = operator_raw.strip() if isinstance(operator_raw, str) and operator_raw.strip() else None

    session_raw = data.get("session")
    session_id = session_raw.strip() if isinstance(session_raw, str) and session_raw.strip() else None
    task_id_raw = data.get("task_id")
    task_id = (
        task_id_raw.strip()
        if isinstance(task_id_raw, str) and task_id_raw.strip()
        else None
    )

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
        "operator": operator,
        "session_id": session_id,
        "task_id": task_id,
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
    def _safe_parse(path: Path) -> dict[str, Any] | None:
        # Final guard: _parse_coordination_note already swallows read/parse
        # errors, but any unexpected failure in one worker must never propagate
        # out of the pool (it would abort the whole scan). Skip that note only.
        try:
            return _parse_coordination_note(path, registry)
        except Exception:
            return None

    max_workers = min(8, os.cpu_count() or 4)
    agents: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        # map preserves the order of note_paths.
        for parsed in pool.map(_safe_parse, note_paths):
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
                # Which lane/assignee the running task belongs to — the "wer"
                # in "wer arbeitet woran" (selected since stage 3, but never
                # surfaced until now).
                "assignee": row["assignee"] or None,
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
            kanban_db_path=resolved_kanban_db_path,
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

    Thin wrapper over :func:`_git_log_commits` (shared format/parser with the
    card's last commit and the cross-project feed).
    """
    return _git_log_commits(entry, limit=_RECENT_COMMITS_LIMIT, now=now)


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
    receipts_payload: dict[str, Any] | None = None,
    receipts_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble the frozen ``GET /api/projects/{slug}`` detail payload.

    Pure-ish: all data sources are overridable so tests never touch real
    ``~/.hermes`` state. Agents are :func:`build_agents_payload` filtered to
    ``project == entry.slug`` (``project`` field dropped from each agent).

    Pass ``agents_payload`` (e.g. the route-level TTL-cached agents payload)
    to skip re-running discovery on every drilldown poll. Filtering always
    builds a new agent list and copies each kept agent dict — the shared
    cached structure is never mutated in place. ``receipts_payload`` follows
    the same contract for the cross-agent receipt scan.
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
        _annotate_commit_attribution(
            recent_commits, kanban_db_path=resolved_kanban_db_path
        )
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
                "assignee": agent.get("assignee"),
                "operator": agent.get("operator"),
                "session_id": agent.get("session_id"),
                "task_id": agent.get("task_id"),
            }
            for agent in source_payload.get("agents", [])
            if agent.get("project") == entry.slug
        ]
        # Agents-source errors stay on the agents endpoint; detail only
        # surfaces git/kanban/loops isolation (agents list simply empty).
    except Exception as exc:
        agents = []
        errors.append(f"agents: {exc}")

    try:
        if receipts_payload is None:
            receipts_payload = build_receipts_payload(
                registry,
                receipts_root=receipts_root,
                now=resolved_now,
            )
        receipts = [
            dict(receipt)
            for receipt in receipts_payload.get("receipts", [])
            if receipt.get("project") == entry.slug
        ][:5]
    except Exception as exc:
        receipts = []
        errors.append(f"receipts: {exc}")

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
        "receipts": receipts,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Stage 10 — open sessions + spawn tree (/api/projects/sessions)
# ---------------------------------------------------------------------------
#
# Answers two operator questions the agents payload cannot:
#   * "welche Sessions sind noch nicht geschlossen?" — sessions.ended_at IS NULL
#   * "welchen Agent hat wer gespawnt?" — sessions.parent_session_id plus the
#     model_config markers ``_delegate_from`` (delegate subagents) and
#     ``_branched_from`` (/branch children), written by delegate_tool/branch.
# Read-only straight against state.db (same isolation contract as kanban.db
# above): a missing file is the no-config default, a locked/broken DB degrades
# to one ``errors[]`` entry — never a 500.

_SESSIONS_WINDOW_SECONDS = 36 * 3600
_SESSIONS_LIMIT = 150
# Same "active" definition as web_server's /api/sessions: open AND a message
# (or start) within the last 300s.
_SESSION_ACTIVE_SECONDS = 300
# Real-data lesson (2026-07-17): on a live host almost every session row keeps
# ``ended_at IS NULL`` forever (gateway/cli rows are rarely closed), so plain
# "open" buckets hundreds of zombie rows. ``stale_open`` marks the graveyard:
# open but no activity for 24h — the UI splits those out of the default view.
_SESSION_STALE_SECONDS = 24 * 3600
_ORPHANED_MARKER = "__orphaned__"

_SESSIONS_SQL = """
SELECT s.id, s.source, s.model, s.title, s.display_name,
       s.started_at, s.ended_at, s.end_reason,
       s.message_count, s.input_tokens, s.output_tokens,
       s.cwd, s.git_repo_root, s.parent_session_id, s.profile_name,
       CASE WHEN json_valid(s.origin_json)
            THEN json_extract(s.origin_json, '$.chat_name') END AS source_channel,
       json_extract(COALESCE(s.model_config, '{}'), '$._delegate_from') AS delegate_from,
       json_extract(COALESCE(s.model_config, '{}'), '$._branched_from') AS branched_from,
       (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) AS last_active
FROM sessions s
WHERE s.started_at >= ? OR s.ended_at IS NULL
ORDER BY (s.ended_at IS NULL) DESC, s.started_at DESC
LIMIT ?
"""

_SESSION_PARENTS_SQL = (
    "SELECT id, display_name, title, source, model, end_reason FROM sessions WHERE id IN ({})"
)


def _session_int(value: Any) -> int | None:
    """REAL/TEXT/None epoch column → plain int seconds (or None)."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _session_label_fields(display_name: Any, title: Any, session_id: str) -> str:
    for candidate in (display_name, title):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return session_id[:8]


def _attribute_session_project(
    paths: list[str],
    profile_name: Any,
    source: Any,
    source_channel: Any,
    registry: ProjectsRegistry,
) -> str | None:
    """Attribute a session by path, then profile, then source/channel mapping."""
    project = _attribute_project(paths, registry)
    if project is not None:
        return project

    if isinstance(profile_name, str):
        for entry in registry.projects:
            if profile_name in entry.session_profiles:
                return entry.slug

    if not isinstance(source, str):
        return None

    # Prefer the more specific channel mapping regardless of registry order;
    # only fall back to a source-only mapping when no channel entry matched.
    if isinstance(source_channel, str) and source_channel:
        source_with_channel = f"{source}:{source_channel}"
        for entry in registry.projects:
            if source_with_channel in entry.session_sources:
                return entry.slug
    for entry in registry.projects:
        if source in entry.session_sources:
            return entry.slug
    return None


def build_sessions_payload(
    registry: ProjectsRegistry,
    *,
    state_db_path: Path | None = None,
    tmux_panes_text: str | None = None,
    tmux_sessions_text: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Assemble the frozen ``/api/projects/sessions`` payload.

    Pure-ish: ``state_db_path``/``now`` are injectable so tests never touch the
    real ``~/.hermes/state.db``. Rows are open-first (unclosed sessions lead,
    then newest ``started_at``) so a long-open session can never fall victim
    to the row cap — the frontend derives the spawn tree from
    ``spawned_by_id`` regardless of flat order.
    """
    resolved_now = now if now is not None else int(time.time())
    home = get_hermes_home()
    resolved_state_db = state_db_path if state_db_path is not None else home / "state.db"

    if not resolved_state_db.exists():
        # No state.db yet (fresh profile / sandbox) — same no-config default
        # as a missing registry file: empty list, no error.
        return {"generated_at": resolved_now, "errors": [], "sessions": []}

    try:
        conn = _open_sqlite_ro(resolved_state_db)
    except sqlite3.DatabaseError as exc:
        return {
            "generated_at": resolved_now,
            "errors": [f"sessions: could not open state.db: {exc}"],
            "sessions": [],
        }

    try:
        rows = conn.execute(
            _SESSIONS_SQL,
            (resolved_now - _SESSIONS_WINDOW_SECONDS, _SESSIONS_LIMIT),
        ).fetchall()

        # Resolve spawn parents (labels + end_reason) in ONE extra query. The
        # parent itself may sit outside the 36h window (long-open CLI sessions)
        # and would otherwise render as a bare id.
        listed_ids = {row["id"] for row in rows}
        wanted_parent_ids: set[str] = set()
        for row in rows:
            for key in ("parent_session_id", "delegate_from", "branched_from"):
                value = row[key]
                if isinstance(value, str) and value and value != _ORPHANED_MARKER:
                    wanted_parent_ids.add(value)
        missing_parent_ids = sorted(wanted_parent_ids - listed_ids)
        parents: dict[str, dict[str, Any]] = {}
        if missing_parent_ids:
            placeholders = ", ".join("?" for _ in missing_parent_ids)
            for prow in conn.execute(
                _SESSION_PARENTS_SQL.format(placeholders), missing_parent_ids
            ).fetchall():
                parents[prow["id"]] = dict(prow)
    except sqlite3.DatabaseError as exc:
        return {
            "generated_at": resolved_now,
            "errors": [f"sessions: {exc}"],
            "sessions": [],
        }
    finally:
        conn.close()

    # Parent lookup across both result sets (window rows win — they carry the
    # freshest state; the extra query is only a label/end_reason fallback).
    row_by_id = {row["id"]: row for row in rows}

    tmux_by_session_id: dict[str, tuple[str, str, str]] = {}
    tmux_errors: list[str] = []
    try:
        tmux_agents, source_errors = _tmux_agents(
            tmux_panes_text=tmux_panes_text,
            tmux_sessions_text=tmux_sessions_text,
            registry=registry,
            kanban_db_path=None,
        )
        for agent in tmux_agents:
            hermes_session_id = agent.get("session_id")
            if not isinstance(hermes_session_id, str) or not hermes_session_id:
                continue
            tmux_by_session_id.setdefault(
                hermes_session_id,
                (
                    agent["tmux_session"],
                    agent["tmux_window"],
                    agent["tmux_window_name"],
                ),
            )
        tmux_errors.extend(
            f"sessions-tmux: {error.removeprefix('tmux:').strip()}"
            for error in source_errors
        )
    except Exception as exc:
        tmux_errors.append(f"sessions-tmux: {exc}")

    def _parent_info(parent_id: str) -> tuple[str | None, str | None]:
        """``(label, end_reason)`` for a spawn parent, best effort."""
        source_row = row_by_id.get(parent_id)
        if source_row is not None:
            return (
                _session_label_fields(
                    source_row["display_name"], source_row["title"], parent_id
                ),
                source_row["end_reason"] if isinstance(source_row["end_reason"], str) else None,
            )
        parent = parents.get(parent_id)
        if parent is not None:
            return (
                _session_label_fields(parent.get("display_name"), parent.get("title"), parent_id),
                parent.get("end_reason") if isinstance(parent.get("end_reason"), str) else None,
            )
        return None, None

    sessions: list[dict[str, Any]] = []
    for row in rows:
        session_id = row["id"]
        tmux_target = tmux_by_session_id.get(session_id)
        delegate_from = row["delegate_from"]
        branched_from = row["branched_from"]
        parent_session_id = row["parent_session_id"]

        spawn_kind: str | None = None
        spawned_by_id: str | None = None
        if isinstance(delegate_from, str) and delegate_from:
            spawn_kind = "delegate"
            spawned_by_id = None if delegate_from == _ORPHANED_MARKER else delegate_from
        elif isinstance(branched_from, str) and branched_from:
            spawn_kind = "branch"
            spawned_by_id = branched_from
        elif isinstance(parent_session_id, str) and parent_session_id:
            spawned_by_id = parent_session_id
            _label, parent_end_reason = _parent_info(parent_session_id)
            # A plain parent link with a compression-ended parent is a
            # continuation of the same conversation, not a spawned agent.
            spawn_kind = "compression" if parent_end_reason == "compression" else "child"

        spawned_by_label: str | None = None
        if spawned_by_id is not None:
            spawned_by_label, _unused = _parent_info(spawned_by_id)

        started_at = _session_int(row["started_at"])
        ended_at = _session_int(row["ended_at"])
        last_active = _session_int(row["last_active"])
        is_open = ended_at is None
        active_reference = last_active if last_active is not None else (started_at or 0)
        is_active = is_open and (resolved_now - active_reference) < _SESSION_ACTIVE_SECONDS
        stale_open = is_open and (resolved_now - active_reference) >= _SESSION_STALE_SECONDS

        input_tokens = row["input_tokens"] or 0
        output_tokens = row["output_tokens"] or 0

        attribution_paths = [
            value for value in (row["cwd"], row["git_repo_root"]) if isinstance(value, str)
        ]

        sessions.append(
            {
                "id": session_id,
                "label": _session_label_fields(row["display_name"], row["title"], session_id),
                "source": row["source"] if isinstance(row["source"], str) else "",
                "model": row["model"] if isinstance(row["model"], str) else None,
                "started_at": started_at,
                "ended_at": ended_at,
                "end_reason": row["end_reason"] if isinstance(row["end_reason"], str) else None,
                "is_open": is_open,
                "is_active": is_active,
                "stale_open": stale_open,
                "last_active": last_active,
                "message_count": int(row["message_count"] or 0),
                "tokens": int(input_tokens) + int(output_tokens),
                "project": _attribute_session_project(
                    attribution_paths,
                    row["profile_name"],
                    row["source"],
                    row["source_channel"],
                    registry,
                ),
                "spawn_kind": spawn_kind,
                "spawned_by_id": spawned_by_id,
                "spawned_by_label": spawned_by_label,
                "tmux_session": tmux_target[0] if tmux_target is not None else None,
                "tmux_window": tmux_target[1] if tmux_target is not None else None,
                "tmux_window_name": tmux_target[2] if tmux_target is not None else None,
            }
        )

    return {"generated_at": resolved_now, "errors": tmux_errors, "sessions": sessions}


# ---------------------------------------------------------------------------
# Stage 11 — cross-project commit feed (/api/projects/commits)
# ---------------------------------------------------------------------------
#
# One merged "Alle Commits" timeline across every registered project: N newest
# commits per repo (path_filters respected via the shared log helper), merged
# newest-first, capped. Each project's git read is isolated — one broken repo
# degrades to an ``errors[]`` entry, the rest of the feed still delivers.

_FEED_COMMITS_PER_PROJECT = 6
_FEED_COMMITS_LIMIT = 30


def build_commits_payload(
    registry: ProjectsRegistry,
    *,
    kanban_db_path: Path | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Assemble the frozen ``/api/projects/commits`` payload (newest-first)."""
    resolved_now = now if now is not None else int(time.time())
    home = get_hermes_home()
    resolved_kanban_db_path = (
        kanban_db_path if kanban_db_path is not None else home / "kanban.db"
    )

    feed: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in registry.projects:
        try:
            commits, git_error = _git_log_commits(
                entry, limit=_FEED_COMMITS_PER_PROJECT, now=resolved_now
            )
        except Exception as exc:
            commits, git_error = [], f"git: {exc}"
        if git_error:
            detail = git_error.removeprefix("git:").strip()
            errors.append(f"git: project '{entry.slug}': {detail}")
        for commit in commits:
            feed.append(
                {
                    "project": entry.slug,
                    "project_name": entry.name,
                    "hash": commit["hash"],
                    "message": commit["message"],
                    "author": commit["author"],
                    "committed_at": commit["committed_at"],
                    "age_seconds": commit["age_seconds"],
                    "_order": len(feed),  # stable tie-break: registry order
                }
            )

    feed.sort(key=lambda commit: (-commit["committed_at"], commit["_order"]))
    feed = feed[:_FEED_COMMITS_LIMIT]
    _annotate_commit_attribution(feed, kanban_db_path=resolved_kanban_db_path)
    for commit in feed:
        del commit["_order"]

    return {"generated_at": resolved_now, "errors": errors, "commits": feed}


# ---------------------------------------------------------------------------
# Stage 12 — cross-agent receipt feed (/api/projects/receipts)
# ---------------------------------------------------------------------------


def _receipt_title_and_excerpt(head: str, fallback_title: str) -> tuple[str, str | None]:
    """Extract display fields without requiring valid YAML frontmatter."""
    lines = head.splitlines()
    frontmatter_end: int | None = None
    if lines and lines[0].strip() == "---":
        frontmatter_end = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
        # An unterminated frontmatter block is not trustworthy content.
        if frontmatter_end is None:
            return fallback_title, None

    title = fallback_title
    for index, line in enumerate(lines):
        if frontmatter_end is not None and index <= frontmatter_end:
            continue
        if line.startswith("# ") and line[2:].strip():
            title = line[2:].strip()
            break

    for index, line in enumerate(lines):
        if frontmatter_end is not None and index <= frontmatter_end:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        return title, stripped[:200]
    return title, None


def _attribute_receipt_project(head: str, registry: ProjectsRegistry) -> str | None:
    """Choose the project with the longest matching registry identifier."""
    lowered = head.casefold()
    best_slug: str | None = None
    best_length = -1
    for entry in registry.projects:
        for candidate in (entry.repo_path, entry.slug, entry.name):
            needle = candidate.strip().casefold()
            if needle and needle in lowered and len(needle) > best_length:
                best_slug = entry.slug
                best_length = len(needle)
    return best_slug


def build_receipts_payload(
    registry: ProjectsRegistry,
    *,
    receipts_root: Path | None = None,
    now: int | float | None = None,
) -> dict[str, Any]:
    """Scan the newest Markdown receipts across every agent directory."""
    root = receipts_root if receipts_root is not None else _RECEIPTS_ROOT
    resolved_now = now if now is not None else time.time()
    candidates: list[tuple[float, str, Path]] = []

    try:
        agent_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        agent_dirs = []

    for agent_dir in agent_dirs:
        receipts_dir = agent_dir / "receipts"
        try:
            paths = list(receipts_dir.iterdir())
        except OSError:
            continue
        for path in paths:
            if path.suffix.lower() != ".md":
                continue
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime, agent_dir.name, path))
            except OSError:
                continue

    candidates.sort(key=lambda item: item[0], reverse=True)
    receipts: list[dict[str, Any]] = []
    for mtime, agent, path in candidates[:_RECEIPTS_LIMIT]:
        fallback_title = path.stem
        try:
            with path.open("rb") as handle:
                head = handle.read(_RECEIPTS_HEAD_BYTES).decode("utf-8", errors="replace")
            title, excerpt = _receipt_title_and_excerpt(head, fallback_title)
            project = _attribute_receipt_project(head, registry)
        except OSError:
            title, excerpt, project = fallback_title, None, None
        receipts.append(
            {
                "agent": agent,
                "filename": path.name,
                "title": title,
                "mtime": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "age_seconds": max(0, int(resolved_now - mtime)),
                "project": project,
                "excerpt": excerpt,
            }
        )

    return {"generated_at": int(resolved_now), "receipts": receipts}


def _unknown_receipt_response() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "unknown receipt"})


def _read_receipt_content(agent: str, filename: str) -> dict[str, Any] | None:
    """Validate and read one receipt, returning ``None`` for every violation."""
    if (
        not agent
        or Path(agent).name != agent
        or "/" in agent
        or "\\" in agent
        or not filename
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".md")
    ):
        return None

    try:
        root = _RECEIPTS_ROOT.resolve()
        agent_dir = root / agent
        if not agent_dir.is_dir():
            return None
        path = (agent_dir / "receipts" / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        stat = path.stat()
        with path.open("rb") as handle:
            raw = handle.read(_RECEIPT_CONTENT_LIMIT_BYTES + 1)
        truncated = len(raw) > _RECEIPT_CONTENT_LIMIT_BYTES
        markdown = raw[:_RECEIPT_CONTENT_LIMIT_BYTES].decode("utf-8", errors="replace")
        title, _excerpt = _receipt_title_and_excerpt(
            raw[:_RECEIPTS_HEAD_BYTES].decode("utf-8", errors="replace"),
            path.stem,
        )
    except OSError:
        return None

    return {
        "agent": agent,
        "filename": filename,
        "title": title,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "truncated": truncated,
        "markdown": markdown,
    }


# ---------------------------------------------------------------------------
# Stage 9 — short process-level TTL cache for route handlers
# ---------------------------------------------------------------------------
#
# Polling (agents ~12s, detail ~8s, grid) and multiple browser tabs must not
# re-pay the coordination-dir scan on every hit. Cache sits at the ROUTE
# boundary only — builders stay pure/injectable for tests. Values are treated
# as read-only; :meth:`_TtlMemo.get` returns a deep copy so no caller can mutate
# a shared nested structure (agent rows, project cards) and poison later hits.

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
            # Deep copy: the payload's nested agent rows / project cards are
            # otherwise shared with the store, so a caller mutating one (e.g.
            # popping a field off a cached agent dict) would poison later hits.
            return copy.deepcopy(value)

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._store[key] = (_clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_projects_cache = _TtlMemo(ttl=_PROJECTS_CACHE_TTL_SECONDS)
_receipts_cache = _TtlMemo(ttl=30.0)


def _reset_projects_cache() -> None:
    """Test hook: drop all cached route payloads so suites stay isolated."""
    _projects_cache.clear()
    _receipts_cache.clear()


def _cache_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a request-local deep copy so the freshly-stored payload the store
    holds can never be mutated through the value handed back on a cold miss."""
    return copy.deepcopy(payload)


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


def _cached_sessions_payload() -> dict[str, Any]:
    """Route accessor for ``GET /api/projects/sessions`` (key ``sessions``)."""
    cached = _projects_cache.get("sessions")
    if cached is not None:
        return cached
    registry = load_projects_registry()
    payload = build_sessions_payload(registry)
    _projects_cache.set("sessions", payload)
    return _cache_view(payload)


def _cached_commits_payload() -> dict[str, Any]:
    """Route accessor for ``GET /api/projects/commits`` (key ``commits``)."""
    cached = _projects_cache.get("commits")
    if cached is not None:
        return cached
    registry = load_projects_registry()
    payload = build_commits_payload(registry)
    _projects_cache.set("commits", payload)
    return _cache_view(payload)


def _cached_receipts_payload() -> dict[str, Any]:
    """Route/detail accessor for the 30-second receipt scan."""
    cached = _receipts_cache.get("receipts")
    if cached is not None:
        return cached
    registry = load_projects_registry()
    payload = build_receipts_payload(registry)
    _receipts_cache.set("receipts", payload)
    return _cache_view(payload)


def register_projects_routes(app: FastAPI) -> None:
    """Register the read-only ``/api/projects`` overview surfaces.

    Auth comes automatically from the dashboard's ``/api/*`` middleware —
    nothing to do here as long as the path stays out of the public whitelist.
    Each handler is wrapped once more on top of its builder's own per-source
    isolation so a truly unexpected failure (e.g. the registry file itself
    becoming unreadable) still answers with JSON, never a 500.

    Unknown slug on the detail route answers **404** with body
    ``{"error": "unknown project", "slug": <slug>}`` (JSON, never a 500).
    The static routes ``/api/projects/agents``, ``/api/projects/sessions`` and
    ``/api/projects/commits`` are registered before ``/{slug}`` so they are
    never captured as a slug (the matching names are also reserved in the
    registry, see ``_RESERVED_SLUGS``).

    ``get_projects`` / ``get_project_agents`` / ``get_project_sessions`` /
    ``get_project_commits`` consult a short process-level TTL cache (~10s).
    ``get_project_detail`` reuses the cached agents payload (does not cache
    per-slug detail bodies).
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

    @app.get("/api/projects/sessions")
    def get_project_sessions() -> dict[str, Any]:
        try:
            return _cached_sessions_payload()
        except Exception as exc:
            return {
                "generated_at": int(time.time()),
                "errors": [f"sessions: unexpected error: {exc}"],
                "sessions": [],
            }

    @app.get("/api/projects/commits")
    def get_project_commits() -> dict[str, Any]:
        try:
            return _cached_commits_payload()
        except Exception as exc:
            return {
                "generated_at": int(time.time()),
                "errors": [f"commits: unexpected error: {exc}"],
                "commits": [],
            }

    @app.get("/api/projects/receipts")
    def get_project_receipts() -> dict[str, Any]:
        try:
            return _cached_receipts_payload()
        except Exception:
            return {"generated_at": int(time.time()), "receipts": []}

    @app.get("/api/projects/receipts/{agent}/{filename:path}")
    def get_project_receipt(agent: str, filename: str) -> Any:
        receipt = _read_receipt_content(agent, filename)
        if receipt is None:
            return _unknown_receipt_response()
        return receipt

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
            receipts_payload = _cached_receipts_payload()
            return build_project_detail(
                entry,
                registry,
                agents_payload=agents_payload,
                receipts_payload=receipts_payload,
            )
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
                "receipts": [],
                "errors": [f"projects: unexpected error: {exc}"],
            }

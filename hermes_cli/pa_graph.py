"""Bounded read-only Estate graph for the Projekte-tab personal assistant.

The graph is deliberately assembled from independent local sources.  A source
failure is returned in ``errors`` and never aborts the other sources.  qmd's
local SQLite index is the preferred Vault source; a bounded Markdown walk is
used when that private index schema is unavailable.

Results are cached in-process for :data:`CACHE_TTL_SECONDS`.  The cache is
invalidated by TTL expiry, process restart, or an explicit
:func:`invalidate_graph_cache` call.  No source is written or re-indexed here.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import heapq
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote

from hermes_constants import get_hermes_home

SCHEMA_VERSION = "pa-graph/v1"
LAYOUT = "precomputed-viewbox-1280x820"
POLL_INTERVAL_SECONDS = 30
CACHE_TTL_SECONDS = 60

MAX_NODES = 500
MAX_EDGES = 1_500
VAULT_NODE_LIMIT = 240
QMD_METADATA_ROW_LIMIT = 20_000
VAULT_WALK_FILE_CAP = 5_000
VAULT_WALK_DEPTH_CAP = 8
KANBAN_PROJECT_LIMIT = 24
KANBAN_TASK_LIMIT = 120
KANBAN_LINK_LIMIT = 500
RECEIPT_LIMIT = 60
RECEIPT_SCAN_CAP = 5_000
MEMORY_LIMIT = 40
MEMORY_SCAN_CAP = 500
MAX_DOC_CHARS = 32_000
MAX_RECEIPT_HEAD_BYTES = 8_192

CLUSTERS: tuple[dict[str, str], ...] = (
    {"id": "canon", "label": "Canon", "color": "#38d8ff"},
    {"id": "projekte", "label": "Projekte", "color": "#3ddc97"},
    {"id": "agenten", "label": "Agenten", "color": "#ffb347"},
    {"id": "skills", "label": "Skills", "color": "#5b8cff"},
    {"id": "memories", "label": "Memories", "color": "#b78cff"},
    {"id": "receipts", "label": "Receipts", "color": "#ff7ab8"},
    {"id": "archiv", "label": "Archiv", "color": "#5a6f8f"},
)

_CLUSTER_CENTERS: dict[str, tuple[float, float]] = {
    "canon": (640.0, 310.0),
    "projekte": (500.0, 590.0),
    "agenten": (850.0, 285.0),
    "skills": (440.0, 255.0),
    "memories": (965.0, 450.0),
    "receipts": (820.0, 595.0),
    "archiv": (325.0, 450.0),
}

_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "data:", "file://")

_cache_lock = threading.Lock()
_cache_created_at = 0.0
_cache_payload: dict[str, Any] | None = None
_clock: Callable[[], float] = time.monotonic


@dataclass
class GraphPart:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


# Absolute filesystem paths must never ride out through public API errors[].
# Match multi-segment Unix paths and common home/root prefixes; leave short
# tokens like "/api" alone.
_ABS_FS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_+-])"
    r"(?:"
    r"/(?:home|Users|var|tmp|opt|etc|usr|root|mnt|media|data|private|Volumes)"
    r"(?:/[^/\s,\"'`;)\]}>]+)+"
    r"|/(?:[A-Za-z0-9._+-]+/){1,}[A-Za-z0-9._+-]+"
    r")"
)
_WIN_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_+-])"
    r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+"
)


def scrub_public_error_text(message: str) -> str:
    """Strip absolute filesystem paths from a public-facing error string."""
    text = str(message or "")
    text = _ABS_FS_PATH_RE.sub("<path>", text)
    text = _WIN_ABS_PATH_RE.sub("<path>", text)
    return text


def public_error_message(
    exc: BaseException | str, *, include_type: bool = False
) -> str:
    """Format an exception/string for API errors[] without leaking abs paths."""
    if isinstance(exc, BaseException):
        detail = str(exc).strip()
        name = type(exc).__name__
        if include_type:
            raw = f"{name}: {detail}" if detail else name
        else:
            raw = detail or name
    else:
        raw = str(exc).strip()
    return scrub_public_error_text(raw)[:500]


def _error(source: str, exc: BaseException | str) -> dict[str, str]:
    return {"source": source, "error": public_error_message(exc)}


def _vault_root() -> Path:
    configured = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    return Path(configured).expanduser() if configured else Path("/home/piet/vault")


def _qmd_index_path() -> Path:
    return Path.home() / ".cache" / "qmd" / "index.sqlite"


def _receipts_root() -> Path:
    return _vault_root() / "03-Agents"


def _memory_roots() -> tuple[tuple[str, Path], ...]:
    return (
        ("memsearch", Path.home() / ".memsearch" / "shared" / "memory"),
        ("hermes", get_hermes_home() / "memories"),
    )


def _projects_db_path() -> Path:
    return get_hermes_home() / "projects.db"


def _kanban_location() -> tuple[Path, str]:
    # Import only for canonical path/board resolution.  The DB itself is opened
    # below with SQLite mode=ro, so graph polling cannot initialize or migrate it.
    from hermes_cli import kanban_db

    return kanban_db.kanban_db_path(), kanban_db.get_current_board()


def _open_sqlite_ro(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalize_vault_path(value: str) -> str:
    text = unquote(str(value or "")).strip().replace("\\", "/")
    text = text.removeprefix("vault://").removeprefix("qmd://vault/")
    vault_prefix = str(_vault_root()).replace("\\", "/").rstrip("/") + "/"
    if text.casefold().startswith(vault_prefix.casefold()):
        text = text[len(vault_prefix) :]
    pieces = []
    for piece in text.strip("/").split("/"):
        normalized = re.sub(r"[\s_]+", "-", piece.strip().casefold())
        if normalized and normalized != ".":
            pieces.append(normalized)
    return "/".join(pieces)


def _cluster_for_vault_path(path: str) -> str:
    normalized = _normalize_vault_path(path)
    parts = tuple(part for part in normalized.split("/") if part)
    lowered = set(parts)
    if "receipts" in lowered:
        return "receipts"
    if any(part in {"skill", "skills"} for part in parts):
        return "skills"
    if any("memory" in part for part in parts):
        return "memories"
    top = parts[0] if parts else ""
    if top == "00-canon":
        return "canon"
    if top in {"03-projects", "04-sprints"}:
        return "projekte"
    if top in {"03-agents", "agents", "-agents"}:
        return "agenten"
    if top == "01-daily":
        return "memories"
    if top in {"08-backups", "09-archive", "10-kb"}:
        return "archiv"
    return "archiv"


def _vault_node(path: str, title: str, rank: int) -> dict[str, Any]:
    normalized = _normalize_vault_path(path)
    href = f"vault://{path.strip('/')}"
    return {
        "id": f"vault:{normalized}",
        "label": (title or Path(path).stem).strip() or Path(path).stem,
        "cluster": _cluster_for_vault_path(path),
        "kind": "doc",
        "weight": max(0.24, 0.72 - rank * 0.0015),
        "href": href,
        "ref": href,
    }


def _is_receipt_path(path: str) -> bool:
    return "/receipts/" in f"/{_normalize_vault_path(path)}/"


def _select_vault_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    # Keep every Estate cluster represented before recency fills the rest.
    per_cluster_floor = max(1, VAULT_NODE_LIMIT // (len(CLUSTERS) * 2))
    for cluster in (entry["id"] for entry in CLUSTERS):
        cluster_rows = [
            row for row in rows if _cluster_for_vault_path(row["path"]) == cluster
        ]
        for row in cluster_rows[:per_cluster_floor]:
            if row["path"] not in selected_paths:
                selected.append(row)
                selected_paths.add(row["path"])
    for row in rows:
        if len(selected) >= VAULT_NODE_LIMIT:
            break
        if row["path"] not in selected_paths:
            selected.append(row)
            selected_paths.add(row["path"])
    return selected[:VAULT_NODE_LIMIT]


def _link_targets(text: str) -> Iterable[tuple[str, str]]:
    for match in _WIKILINK_RE.finditer(text):
        yield "wikilink", match.group(1).split("|", 1)[0]
    for match in _MARKDOWN_LINK_RE.finditer(text):
        raw = match.group(1).strip()
        # Remove optional Markdown link title after the path.
        if " " in raw and not raw.startswith("<"):
            raw = raw.split(" ", 1)[0]
        yield "markdown-link", raw.strip("<>")


def _resolve_vault_target(
    source_path: str,
    raw_target: str,
    *,
    by_path: dict[str, str],
    by_stem: dict[str, set[str]],
) -> str | None:
    target = raw_target.strip().split("#", 1)[0].strip()
    if not target or target.casefold().startswith(_EXTERNAL_SCHEMES):
        return None
    normalized = _normalize_vault_path(target)
    if not normalized:
        return None
    if not normalized.endswith(".md"):
        normalized += ".md"
    source_parent = _normalize_vault_path(source_path).rsplit("/", 1)[0]
    candidates = [normalized]
    if source_parent:
        candidates.insert(0, f"{source_parent}/{normalized}")
    for candidate in candidates:
        if candidate in by_path:
            return by_path[candidate]
    stem = Path(normalized).stem
    matches = by_stem.get(stem, set())
    return next(iter(matches)) if len(matches) == 1 else None


def _vault_part_from_rows(rows: list[dict[str, Any]]) -> GraphPart:
    selected = _select_vault_rows(rows)
    nodes = [
        _vault_node(row["path"], row.get("title") or "", rank)
        for rank, row in enumerate(selected)
    ]
    by_path = {
        _normalize_vault_path(row["path"]): node["id"]
        for row, node in zip(selected, nodes, strict=True)
    }
    by_stem: dict[str, set[str]] = {}
    for normalized, node_id in by_path.items():
        by_stem.setdefault(Path(normalized).stem, set()).add(node_id)
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for row, node in zip(selected, nodes, strict=True):
        for kind, target in _link_targets(str(row.get("doc") or "")):
            target_id = _resolve_vault_target(
                row["path"], target, by_path=by_path, by_stem=by_stem
            )
            key = (node["id"], target_id or "", kind)
            if target_id and target_id != node["id"] and key not in seen_edges:
                edges.append({"from": node["id"], "to": target_id, "kind": kind})
                seen_edges.add(key)
    return GraphPart(nodes=nodes, edges=edges)


def _load_vault_from_qmd() -> GraphPart:
    with contextlib.closing(_open_sqlite_ro(_qmd_index_path())) as conn:
        if not {
            "collection",
            "path",
            "title",
            "hash",
            "active",
            "modified_at",
        } <= _table_columns(conn, "documents") or not {"hash", "doc"} <= _table_columns(
            conn, "content"
        ):
            raise RuntimeError("unsupported qmd index schema")
        metadata_rows = conn.execute(
            "SELECT d.path, d.title, d.modified_at, d.hash "
            "FROM documents d "
            "WHERE d.collection = 'vault' AND d.active = 1 "
            "ORDER BY d.modified_at DESC, d.path ASC LIMIT ?",
            (QMD_METADATA_ROW_LIMIT,),
        ).fetchall()
        metadata = [
            dict(row)
            for row in metadata_rows
            if str(row["path"]).lower().endswith(".md")
            and not _is_receipt_path(str(row["path"]))
        ]
        selected = _select_vault_rows(metadata)
        if not selected:
            raise RuntimeError("qmd vault collection is empty")
        hashes = {str(row["hash"]) for row in selected}
        placeholders = ",".join("?" for _ in hashes)
        content_rows = conn.execute(
            f"SELECT hash, substr(doc, 1, ?) AS doc FROM content "
            f"WHERE hash IN ({placeholders})",
            (MAX_DOC_CHARS, *hashes),
        ).fetchall()
    content_by_hash = {str(row["hash"]): str(row["doc"]) for row in content_rows}
    for row in selected:
        row["doc"] = content_by_hash.get(str(row["hash"]), "")
    return _vault_part_from_rows(selected)


def _walk_markdown_candidates(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    candidates: list[tuple[float, str, Path]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    scanned = 0
    while stack and scanned < VAULT_WALK_FILE_CAP:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        child_dirs: list[Path] = []
        for entry in entries:
            if scanned >= VAULT_WALK_FILE_CAP:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < VAULT_WALK_DEPTH_CAP and entry.name not in {
                        ".git",
                        ".obsidian",
                        "node_modules",
                    }:
                        child_dirs.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.lower().endswith(
                    ".md"
                ):
                    continue
                scanned += 1
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if _is_receipt_path(relative):
                    continue
                stat = entry.stat(follow_symlinks=False)
                item = (stat.st_mtime, relative, path)
                if len(candidates) < VAULT_NODE_LIMIT * 2:
                    heapq.heappush(candidates, item)
                elif item > candidates[0]:
                    heapq.heapreplace(candidates, item)
            except OSError:
                continue
        for child in reversed(child_dirs):
            stack.append((child, depth + 1))
    return [item[2] for item in sorted(candidates, reverse=True)]


def _load_vault_from_files() -> GraphPart:
    root = _vault_root()
    rows: list[dict[str, Any]] = []
    read_errors: list[dict[str, str]] = []
    for path in _walk_markdown_candidates(root):
        try:
            relative = path.relative_to(root).as_posix()
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                doc = handle.read(MAX_DOC_CHARS)
            heading = _HEADING_RE.search(doc)
            rows.append(
                {
                    "path": relative,
                    "title": heading.group(1).strip() if heading else path.stem,
                    "doc": doc,
                }
            )
        except (OSError, ValueError) as exc:
            if not read_errors:
                read_errors.append(_error("vault-fallback", exc))
    if not rows:
        raise RuntimeError("bounded Vault fallback found no readable Markdown notes")
    part = _vault_part_from_rows(rows)
    part.errors.extend(read_errors)
    return part


def _collect_vault() -> GraphPart:
    try:
        return _load_vault_from_qmd()
    except Exception as qmd_exc:
        try:
            fallback = _load_vault_from_files()
        except Exception as fallback_exc:
            raise RuntimeError(
                f"qmd unavailable ({qmd_exc}); Vault fallback failed ({fallback_exc})"
            ) from fallback_exc
        fallback.errors.insert(
            0,
            _error("qmd", f"{qmd_exc}; bounded Vault fallback active"),
        )
        return fallback


def _project_node(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    project_id = str(row["id"])
    label = str(row["name"] or row["slug"] or project_id)
    return {
        "id": f"project:{project_id}",
        "label": label,
        "cluster": "projekte",
        "kind": "project",
        "weight": 0.68,
        "href": "/control/projekte-klassisch",
        "ref": "/control/projekte-klassisch",
    }


def _collect_kanban() -> GraphPart:
    db_path, board_slug = _kanban_location()
    part = GraphPart()
    projects_by_id: dict[str, dict[str, Any]] = {}
    project_for_board: dict[str, str] = {}
    try:
        with contextlib.closing(_open_sqlite_ro(_projects_db_path())) as projects_conn:
            columns = _table_columns(projects_conn, "projects")
            required = {"id", "name", "slug", "created_at"}
            if not required <= columns:
                raise RuntimeError("unsupported projects.db schema")
            archived_clause = "WHERE archived = 0" if "archived" in columns else ""
            board_column = "board_slug" if "board_slug" in columns else "NULL AS board_slug"
            project_rows = projects_conn.execute(
                f"SELECT id, slug, name, {board_column}, created_at FROM projects "
                f"{archived_clause} ORDER BY created_at DESC LIMIT ?",
                (KANBAN_PROJECT_LIMIT,),
            ).fetchall()
        for row in project_rows:
            node = _project_node(row)
            projects_by_id[str(row["id"])] = node
            part.nodes.append(node)
            if row["board_slug"]:
                project_for_board[str(row["board_slug"])] = str(row["id"])
    except Exception as exc:
        part.errors.append(_error("projects", exc))

    with contextlib.closing(_open_sqlite_ro(db_path)) as conn:
        columns = _table_columns(conn, "tasks")
        required = {"id", "title", "status", "created_at"}
        if not required <= columns:
            raise RuntimeError("unsupported kanban tasks schema")
        recency_columns = [
            column
            for column in ("created_at", "started_at", "completed_at", "last_heartbeat_at")
            if column in columns
        ]
        recency_expr = "max(" + ",".join(
            f"coalesce({column}, 0)" for column in recency_columns
        ) + ")"
        project_select = "project_id" if "project_id" in columns else "NULL AS project_id"
        rows = conn.execute(
            f"SELECT id, title, status, {project_select}, {recency_expr} AS graph_updated "
            "FROM tasks ORDER BY graph_updated DESC, created_at DESC, id DESC LIMIT ?",
            (KANBAN_TASK_LIMIT,),
        ).fetchall()
        selected_task_ids: set[str] = set()
        default_project_id = project_for_board.get(board_slug)
        if rows and default_project_id is None and len(projects_by_id) < KANBAN_PROJECT_LIMIT:
            default_project_id = f"board:{board_slug}"
            placeholder = {
                "id": f"project:{default_project_id}",
                "label": board_slug,
                "cluster": "projekte",
                "kind": "project",
                "weight": 0.58,
                "href": "/control/projekte-klassisch",
                "ref": "/control/projekte-klassisch",
            }
            projects_by_id[default_project_id] = placeholder
            part.nodes.insert(0, placeholder)
        for rank, row in enumerate(rows):
            task_id = str(row["id"])
            selected_task_ids.add(task_id)
            href = f"/control/fleet?task={quote(task_id, safe='')}"
            part.nodes.append(
                {
                    "id": f"task:{task_id}",
                    "label": str(row["title"] or task_id),
                    "cluster": "projekte",
                    "kind": "task",
                    "weight": max(0.24, 0.55 - rank * 0.002),
                    "href": href,
                    "ref": href,
                }
            )
            project_id = str(row["project_id"]) if row["project_id"] else default_project_id
            if (
                project_id
                and project_id not in projects_by_id
                and len(projects_by_id) < KANBAN_PROJECT_LIMIT
            ):
                placeholder = {
                    "id": f"project:{project_id}",
                    "label": project_id,
                    "cluster": "projekte",
                    "kind": "project",
                    "weight": 0.58,
                    "href": "/control/projekte-klassisch",
                    "ref": "/control/projekte-klassisch",
                }
                projects_by_id[project_id] = placeholder
                part.nodes.insert(0, placeholder)
            if project_id:
                part.edges.append(
                    {
                        "from": f"project:{project_id}",
                        "to": f"task:{task_id}",
                        "kind": "project-task",
                    }
                )

        if selected_task_ids and {"parent_id", "child_id"} <= _table_columns(
            conn, "task_links"
        ):
            placeholders = ",".join("?" for _ in selected_task_ids)
            link_rows = conn.execute(
                f"SELECT parent_id, child_id FROM task_links "
                f"WHERE parent_id IN ({placeholders}) AND child_id IN ({placeholders}) "
                "LIMIT ?",
                (*selected_task_ids, *selected_task_ids, KANBAN_LINK_LIMIT),
            ).fetchall()
            for row in link_rows:
                part.edges.append(
                    {
                        "from": f"task:{row['parent_id']}",
                        "to": f"task:{row['child_id']}",
                        "kind": "task-link",
                    }
                )
    return part


def _receipt_title(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(MAX_RECEIPT_HEAD_BYTES).decode("utf-8", errors="replace")
        heading = _HEADING_RE.search(head)
        return heading.group(1).strip() if heading else path.stem
    except OSError:
        return path.stem


def _collect_receipts() -> GraphPart:
    root = _receipts_root()
    if not root.is_dir():
        raise FileNotFoundError(root)
    candidates: list[tuple[float, str, Path]] = []
    scanned = 0
    for agent_dir in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        receipts_dir = agent_dir / "receipts"
        if not receipts_dir.is_dir():
            continue
        try:
            entries = list(receipts_dir.iterdir())
        except OSError:
            continue
        for path in entries:
            if scanned >= RECEIPT_SCAN_CAP:
                break
            scanned += 1
            try:
                if path.is_file() and path.suffix.casefold() == ".md":
                    candidates.append((path.stat().st_mtime, agent_dir.name, path))
            except OSError:
                continue
    candidates.sort(reverse=True)
    part = GraphPart()
    agents: set[str] = set()
    for rank, (_, agent, path) in enumerate(candidates[:RECEIPT_LIMIT]):
        agent_slug = re.sub(r"[^a-z0-9]+", "-", agent.casefold()).strip("-") or "agent"
        agent_id = f"agent:{agent_slug}"
        if agent_id not in agents:
            part.nodes.append(
                {
                    "id": agent_id,
                    "label": agent,
                    "cluster": "agenten",
                    "kind": "agent",
                    "weight": 0.62,
                }
            )
            agents.add(agent_id)
        node_id = f"receipt:{agent_slug}/{path.name.casefold()}"
        href = (
            f"/api/projects/receipts/{quote(agent, safe='')}/"
            f"{quote(path.name, safe='')}"
        )
        part.nodes.append(
            {
                "id": node_id,
                "label": _receipt_title(path),
                "cluster": "receipts",
                "kind": "receipt",
                "weight": max(0.24, 0.55 - rank * 0.004),
                "href": href,
                "ref": href,
            }
        )
        part.edges.append({"from": agent_id, "to": node_id, "kind": "receipt"})
    return part


def _collect_memories() -> GraphPart:
    candidates: list[tuple[float, str, Path, Path]] = []
    errors: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    available_root = False
    scanned = 0
    for source_name, root in _memory_roots():
        if not root.is_dir():
            continue
        available_root = True
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            errors.append(_error("memories", exc))
            continue
        for path in entries:
            if scanned >= MEMORY_SCAN_CAP:
                break
            scanned += 1
            try:
                if not path.is_file() or path.suffix.casefold() != ".md":
                    continue
                key = str(path.resolve())
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                candidates.append((path.stat().st_mtime, source_name, path, root))
            except OSError:
                continue
    if not available_root:
        raise FileNotFoundError("no readable memsearch/Hermes memory root")
    candidates.sort(reverse=True)
    part = GraphPart(errors=errors)
    previous_id: str | None = None
    for rank, (_, source_name, path, root) in enumerate(candidates[:MEMORY_LIMIT]):
        relative = path.relative_to(root).as_posix()
        node_id = f"memory:{source_name}:{relative.casefold()}"
        href = f"memory://{source_name}/{relative}"
        part.nodes.append(
            {
                "id": node_id,
                "label": path.stem,
                "cluster": "memories",
                "kind": "memory",
                "weight": max(0.24, 0.58 - rank * 0.007),
                "href": href,
                "ref": href,
            }
        )
        if previous_id:
            part.edges.append(
                {"from": previous_id, "to": node_id, "kind": "previous-memory"}
            )
        previous_id = node_id
    return part


def _decorate_nodes(
    nodes: list[dict[str, Any]], edges: list[dict[str, str]]
) -> list[dict[str, Any]]:
    degree: dict[str, int] = {str(node["id"]): 0 for node in nodes}
    for edge in edges:
        degree[edge["from"]] = degree.get(edge["from"], 0) + 1
        degree[edge["to"]] = degree.get(edge["to"], 0) + 1
    max_degree = max(degree.values(), default=0)
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_cluster.setdefault(str(node["cluster"]), []).append(node)
    decorated: list[dict[str, Any]] = []
    for cluster, cluster_nodes in by_cluster.items():
        center_x, center_y = _CLUSTER_CENTERS.get(cluster, (640.0, 410.0))
        ordered = sorted(cluster_nodes, key=lambda node: str(node["id"]))
        count = len(ordered)
        for index, original in enumerate(ordered):
            node = dict(original)
            digest = hashlib.sha256(str(node["id"]).encode()).digest()
            phase = int.from_bytes(digest[:4], "big") / (2**32) * math.tau
            angle = phase + index * 2.399963229728653
            radius = 25.0 + 180.0 * math.sqrt((index + 1) / (count + 1))
            node_degree = degree.get(str(node["id"]), 0)
            degree_weight = (
                0.2 + 0.8 * math.sqrt(node_degree / max_degree) if max_degree else 0.2
            )
            node["weight"] = round(
                min(1.0, max(0.2, float(node.get("weight", 0.2)), degree_weight)), 3
            )
            node["x"] = round(center_x + math.cos(angle) * radius, 1)
            node["y"] = round(center_y + math.sin(angle) * radius, 1)
            node.setdefault("kind", "doc")
            node.setdefault("label", str(node["id"]))
            decorated.append(node)
    return decorated


def _build_uncached() -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    parts: list[GraphPart] = []
    for source, collector in (
        ("vault", _collect_vault),
        ("kanban", _collect_kanban),
        ("receipts", _collect_receipts),
        ("memories", _collect_memories),
    ):
        try:
            part = collector()
        except Exception as exc:
            errors.append(_error(source, exc))
            continue
        parts.append(part)
        errors.extend(part.errors)

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for part in parts:
        for node in part.nodes:
            node_id = str(node.get("id") or "")
            if not node_id or node_id in node_ids or len(nodes) >= MAX_NODES:
                continue
            nodes.append(node)
            node_ids.add(node_id)

    edges: list[dict[str, str]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for part in parts:
        for edge in part.edges:
            source_id = str(edge.get("from") or "")
            target_id = str(edge.get("to") or "")
            kind = str(edge.get("kind") or "link")
            key = (source_id, target_id, kind)
            if (
                source_id in node_ids
                and target_id in node_ids
                and source_id != target_id
                and key not in edge_keys
                and len(edges) < MAX_EDGES
            ):
                edges.append({"from": source_id, "to": target_id, "kind": kind})
                edge_keys.add(key)

    nodes = _decorate_nodes(nodes, edges)
    return {
        "schema": SCHEMA_VERSION,
        "source": "live",
        "layout": LAYOUT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refresh": {
            "interval_s": POLL_INTERVAL_SECONDS,
            "cache_ttl_s": CACHE_TTL_SECONDS,
            "invalidation": "ttl-or-process-restart",
            "on_error": "empty-live-data + frontend-mock-fallback",
        },
        "clusters": [dict(cluster) for cluster in CLUSTERS],
        "nodes": nodes,
        "edges": edges,
        "errors": errors,
    }


def invalidate_graph_cache() -> None:
    """Drop the in-process snapshot; the next request rebuilds every source."""
    global _cache_created_at, _cache_payload

    with _cache_lock:
        _cache_created_at = 0.0
        _cache_payload = None


def cached_graph() -> dict[str, Any] | None:
    """Return the cached graph snapshot, or ``None`` when nothing is cached.

    Read-only companion to :func:`build_graph`: callers that only want to
    *annotate* results against the graph (e.g. the B3 search's ``in_graph``
    flag and node connections) must never trigger a multi-second rebuild.
    """
    with _cache_lock:
        if _cache_payload is None or _clock() - _cache_created_at >= CACHE_TTL_SECONDS:
            return None
        return copy.deepcopy(_cache_payload)


def graph_node_ids() -> set[str]:
    """IDs of the currently cached graph nodes (empty when the cache is cold)."""
    payload = cached_graph()
    if not payload:
        return set()
    return {str(node["id"]) for node in payload.get("nodes", []) if node.get("id")}


def build_graph(*, force_refresh: bool = False) -> dict[str, Any]:
    """Return a deep-copied cached graph, rebuilding after the 60-second TTL."""
    global _cache_created_at, _cache_payload

    now = _clock()
    with _cache_lock:
        if (
            not force_refresh
            and _cache_payload is not None
            and now - _cache_created_at < CACHE_TTL_SECONDS
        ):
            return copy.deepcopy(_cache_payload)
        payload = _build_uncached()
        _cache_created_at = now
        _cache_payload = copy.deepcopy(payload)
        return copy.deepcopy(payload)

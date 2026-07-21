"""Jarvis "Gehirn-Suche" + Node-Preview (Slice B3).

Implements the normalized B3 API contract:

- ``GET /api/pa/search?q=…`` — unified search across the three brain sources
  (Vault via the qmd FTS5 index, Kanban tasks via LIKE, memory notes via a
  bounded root scan).  Every source runs behind its own timeout and error
  boundary: a single failing source still yields HTTP 200 with the remaining
  partial results plus a structured ``errors[]`` entry.  Results are
  deduplicated by node id, interleaved deterministically and cut to a global
  limit; responses are cached for a few seconds.
- ``GET /api/pa/node?id=…`` — node preview with body/metadata/connections for
  Vault notes, Kanban tasks and memory notes.  Unknown ids → 404;
  malformed / traversal / absolute-path ids → 400.  Neither absolute
  filesystem paths nor token-shaped secrets are ever emitted.

Node identity is **not** redefined here.  Every id comes from the same
``pa_graph`` helpers the graph itself uses (``_vault_node``, ``task:{id}``,
``memory:{source}:{rel}``), which is what makes ``search.items[].id`` line up
with ``pa_graph`` node ids.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from hermes_cli import pa_graph

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS = 15.0
CACHE_MAX_ENTRIES = 64

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 128
MAX_QUERY_TERMS = 8
DEFAULT_LIMIT = 12
MAX_LIMIT = 50

SOURCE_TIMEOUT_SECONDS = 5.0
# Per-source caps keep one chatty source from crowding the others out before
# the global limit is applied (DESIGN-BACKEND B3).
SOURCE_CAPS: dict[str, int] = {"vault": 12, "kanban": 8, "memory": 6}
SOURCE_ORDER = ("vault", "kanban", "memory")

MAX_SNIPPET_CHARS = 320
MAX_BODY_CHARS = 8_000
MAX_CONNECTIONS = 50
MAX_META_VALUE_CHARS = 256
MEMORY_HEAD_BYTES = 8_192
SNIPPET_CONTEXT_CHARS = 80

# Token-shaped secrets must never travel through a snippet, body or metadata.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}"
    r"|ghp_[A-Za-z0-9]{12,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_TERM_RE = re.compile(r"\w+", re.UNICODE)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

_clock = time.monotonic


# ---------------------------------------------------------------------------
# Indirections (kept as module-level functions so tests can redirect them)
# ---------------------------------------------------------------------------


def _vault_root() -> Path:
    return pa_graph._vault_root()


def _qmd_index_path() -> Path:
    return pa_graph._qmd_index_path()


def _kanban_location() -> tuple[Path, str]:
    return pa_graph._kanban_location()


def _memory_roots() -> tuple[tuple[str, Path], ...]:
    return pa_graph._memory_roots()


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text or "")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _snippet_text(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    """One-line, secret-free excerpt."""
    return _clip(re.sub(r"\s+", " ", _redact(text)).strip(), limit)


def _body_text(text: str) -> str:
    """Preview body — keeps line structure, drops secrets, bounded length."""
    return _clip(_redact(text), MAX_BODY_CHARS)


def _sanitize_meta(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = _snippet_text(value, MAX_META_VALUE_CHARS)
            if not value:
                continue
        out[str(key)] = value
    return out


# ---------------------------------------------------------------------------
# Query normalization
# ---------------------------------------------------------------------------


def _normalize_query(raw: str) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()[:MAX_QUERY_CHARS]


def _fts_match(query: str) -> str:
    """Tolerant FTS5 MATCH expression: quoted terms OR-joined."""
    terms = _TERM_RE.findall(query)[:MAX_QUERY_TERMS]
    return " OR ".join(f'"{term}"' for term in terms)


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _excerpt_around(text: str, needle: str) -> str:
    """±SNIPPET_CONTEXT_CHARS around the first case-insensitive match."""
    position = text.casefold().find(needle.casefold())
    if position < 0:
        return _snippet_text(text)
    start = max(0, position - SNIPPET_CONTEXT_CHARS)
    end = min(len(text), position + len(needle) + SNIPPET_CONTEXT_CHARS)
    excerpt = text[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt = excerpt + "…"
    return _snippet_text(excerpt)


def _rank_score(rank: int) -> float:
    """Per-source normalized score in (0, 1] — monotone in source relevance."""
    return round(1.0 / (1.0 + rank), 4)


# ---------------------------------------------------------------------------
# Per-source timeout (sqlite handles are opened inside the worker thread, so a
# hung source can never wedge the request handler)
# ---------------------------------------------------------------------------


class SourceTimeout(RuntimeError):
    pass


def _run_with_timeout(fn, timeout: float) -> Any:
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced through errors[]
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise SourceTimeout(f"source timed out after {timeout:.1f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# Source: Vault (qmd FTS5)
# ---------------------------------------------------------------------------

_VAULT_SQL = """
SELECT d.path                                       AS path,
       COALESCE(d.title, '')                        AS title,
       snippet(documents_fts, 2, '', '', '…', 24)   AS snippet,
       bm25(documents_fts, 2.0, 8.0, 1.0)           AS bm25
FROM documents_fts
JOIN documents d ON d.id = documents_fts.rowid
WHERE documents_fts MATCH ?
  AND d.collection = 'vault'
  AND d.active = 1
ORDER BY bm25
LIMIT ?
"""


def _search_vault(query: str, limit: int) -> list[dict[str, Any]]:
    match = _fts_match(query)
    if not match:
        return []
    with contextlib.closing(pa_graph._open_sqlite_ro(_qmd_index_path())) as conn:
        rows = conn.execute(_VAULT_SQL, (match, limit * 4)).fetchall()

    hits: list[dict[str, Any]] = []
    for row in rows:
        path = str(row["path"] or "")
        if not path.lower().endswith(".md") or pa_graph._is_receipt_path(path):
            continue
        # Identity + cluster come from the graph's own node factory.
        node = pa_graph._vault_node(path, str(row["title"] or ""), len(hits))
        hits.append(
            {
                "id": node["id"],
                "ref": str(node["ref"]),
                "title": str(node["label"]),
                "cluster": str(node["cluster"]),
                "snippet": _snippet_text(str(row["snippet"] or "")),
                "score": _rank_score(len(hits)),
                "kind": "doc",
                "source": "vault",
                "meta": {"bm25": round(float(row["bm25"] or 0.0), 4)},
            }
        )
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------------------
# Source: Kanban (LIKE — the board DB carries no FTS index)
# ---------------------------------------------------------------------------


def _search_kanban(query: str, limit: int) -> list[dict[str, Any]]:
    db_path, _board = _kanban_location()
    like = _like_pattern(query)
    with contextlib.closing(pa_graph._open_sqlite_ro(db_path)) as conn:
        columns = pa_graph._table_columns(conn, "tasks")
        if not {"id", "title"} <= columns:
            raise RuntimeError("unsupported kanban tasks schema")
        body_expr = "COALESCE(body, '')" if "body" in columns else "''"
        status_expr = "status" if "status" in columns else "NULL"
        recency = [
            column
            for column in ("created_at", "started_at", "completed_at", "last_heartbeat_at")
            if column in columns
        ]
        # SQLite's max() is the *aggregate* when called with a single argument
        # ("misuse of aggregate: max()" inside ORDER BY) and only the scalar
        # variant from two arguments on — so a board carrying exactly one of the
        # recency columns must order by that column directly.
        recency_exprs = [f"coalesce({column}, 0)" for column in recency]
        if len(recency_exprs) >= 2:
            order = "max(" + ", ".join(recency_exprs) + ") DESC, id DESC"
        elif recency_exprs:
            order = f"{recency_exprs[0]} DESC, id DESC"
        else:
            order = "id DESC"
        rows = conn.execute(
            f"SELECT id, title, {body_expr} AS body, {status_expr} AS status "
            f"FROM tasks WHERE title LIKE ? ESCAPE '\\' OR {body_expr} LIKE ? ESCAPE '\\' "
            f"ORDER BY {order} LIMIT ?",
            (like, like, limit),
        ).fetchall()

    hits: list[dict[str, Any]] = []
    for rank, row in enumerate(rows):
        task_id = str(row["id"])
        haystack = f"{row['title'] or ''}\n{row['body'] or ''}"
        hits.append(
            {
                # Same literal shape as pa_graph._collect_kanban.
                "id": f"task:{task_id}",
                "ref": f"/control/fleet?task={task_id}",
                "title": str(row["title"] or task_id),
                "cluster": "projekte",
                "snippet": _excerpt_around(haystack, query),
                "score": _rank_score(rank),
                "kind": "task",
                "source": "kanban",
                "meta": _sanitize_meta({"status": row["status"]}),
            }
        )
    return hits


# ---------------------------------------------------------------------------
# Source: memory notes (bounded scan of the same roots the graph walks)
# ---------------------------------------------------------------------------


def _search_memory(query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.casefold()
    hits: list[dict[str, Any]] = []
    scanned = 0
    available_root = False

    for source_name, root in _memory_roots():
        if not root.is_dir():
            continue
        available_root = True
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            continue
        for path in entries:
            if scanned >= pa_graph.MEMORY_SCAN_CAP or len(hits) >= limit:
                break
            try:
                if not path.is_file() or path.suffix.casefold() != ".md":
                    continue
                scanned += 1
                relative = path.relative_to(root).as_posix()
                with path.open("rb") as handle:
                    head = handle.read(MEMORY_HEAD_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            if needle not in relative.casefold() and needle not in head.casefold():
                continue
            hits.append(
                {
                    # Same literal shape as pa_graph._collect_memories.
                    "id": f"memory:{source_name}:{relative.casefold()}",
                    "ref": f"memory://{source_name}/{relative}",
                    "title": path.stem,
                    "cluster": "memories",
                    "snippet": _excerpt_around(head, query),
                    "score": _rank_score(len(hits)),
                    "kind": "memory",
                    "source": "memory",
                    "meta": {"root": source_name},
                }
            )

    if not available_root:
        raise FileNotFoundError("no readable memsearch/Hermes memory root")
    return hits


# Resolved by name at call time, not bound here: the fan-out must pick up a
# patched/replaced source function instead of a stale import-time reference.
_SOURCE_FUNCS: dict[str, str] = {
    "vault": "_search_vault",
    "kanban": "_search_kanban",
    "memory": "_search_memory",
}


def _source_fn(name: str):
    return globals()[_SOURCE_FUNCS[name]]


# ---------------------------------------------------------------------------
# Response caches
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_search_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_node_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def invalidate_search_cache() -> None:
    """Drop both response caches (used by tests and manual refreshes)."""
    with _cache_lock:
        _search_cache.clear()
        _node_cache.clear()


# Backwards-compatible alias used by earlier drafts of the B3 tests.
_reset_caches = invalidate_search_cache


def _cache_get(cache: dict, key: Any) -> dict[str, Any] | None:
    with _cache_lock:
        entry = cache.get(key)
        if entry is None:
            return None
        stored_at, payload = entry
        if _clock() - stored_at > CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None
        return copy_payload(payload)


def _cache_put(cache: dict, key: Any, payload: dict[str, Any]) -> None:
    with _cache_lock:
        if len(cache) >= CACHE_MAX_ENTRIES:
            oldest = min(cache.items(), key=lambda item: item[1][0])[0]
            cache.pop(oldest, None)
        cache[key] = (_clock(), copy_payload(payload))


def copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shallow-immutable copy: nested lists/dicts are rebuilt, scalars shared."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            out[key] = [dict(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            out[key] = dict(value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _interleave(by_source: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Round-robin across sources in a fixed order — fully deterministic."""
    out: list[dict[str, Any]] = []
    index = 0
    while len(out) < limit:
        progressed = False
        for name in SOURCE_ORDER:
            bucket = by_source.get(name) or []
            if index >= len(bucket):
                continue
            progressed = True
            out.append(bucket[index])
            if len(out) >= limit:
                break
        if not progressed:
            break
        index += 1
    return out


def _execute_search(query: str, limit: int) -> dict[str, Any]:
    started = _clock()
    errors: list[dict[str, str]] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()

    for name in SOURCE_ORDER:
        fn = _source_fn(name)
        cap = SOURCE_CAPS.get(name, limit)
        try:
            hits = _run_with_timeout(
                lambda fn=fn, cap=cap: fn(query, cap), SOURCE_TIMEOUT_SECONDS
            )
        except BaseException as exc:  # noqa: BLE001 — one source must not sink the request
            errors.append({"source": name, "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        bucket: list[dict[str, Any]] = []
        for hit in hits or []:
            node_id = str(hit.get("id") or "")
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            bucket.append(hit)
        by_source[name] = bucket

    graph_ids = pa_graph.graph_node_ids()
    items = [
        {
            "id": hit["id"],
            "ref": hit.get("ref", ""),
            "title": hit.get("title", ""),
            "cluster": hit.get("cluster", ""),
            "snippet": hit.get("snippet", ""),
            "score": hit.get("score", 0.0),
            "kind": hit.get("kind", ""),
            "source": hit.get("source", ""),
            "in_graph": hit["id"] in graph_ids,
            "meta": _sanitize_meta(hit.get("meta") or {}),
        }
        for hit in _interleave(by_source, limit)
    ]

    return {
        "query": query,
        "items": items,
        "took_ms": max(0, int((_clock() - started) * 1000)),
        "errors": errors,
    }


def search(query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    query = _normalize_query(query)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    if len(query) < MIN_QUERY_CHARS:
        return {"query": query, "items": [], "took_ms": 0, "errors": []}

    key = (query, limit)
    cached = _cache_get(_search_cache, key)
    if cached is not None:
        return cached
    payload = _execute_search(query, limit)
    _cache_put(_search_cache, key, payload)
    return payload


# ---------------------------------------------------------------------------
# Node preview — id validation and path safety
# ---------------------------------------------------------------------------


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="node not found")


def _check_relative(rel: str, *, what: str) -> str:
    """Reject absolute paths, home refs, traversal and NUL/newline injection."""
    if not rel or rel.startswith(("/", "~", "\\")):
        raise _bad_request(f"invalid {what}")
    if re.match(r"^[A-Za-z]:", rel):  # Windows-style absolute
        raise _bad_request(f"invalid {what}")
    if any(char in rel for char in ("\x00", "\n", "\r")):
        raise _bad_request(f"invalid {what}")
    if any(part == ".." for part in rel.replace("\\", "/").split("/")):
        raise _bad_request(f"invalid {what}")
    return rel


def _guarded_join(root: Path, rel: str) -> Path | None:
    """Join ``rel`` under ``root``, refusing anything that escapes it.

    Uses ``resolve()`` on both sides so symlinks pointing outside the root are
    rejected too, not just lexical ``..`` traversal.
    """
    try:
        root_real = root.resolve(strict=False)
        candidate = (root_real / rel).resolve(strict=False)
    except (OSError, ValueError):
        return None
    if candidate != root_real and root_real not in candidate.parents:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Node preview — connections from the cached graph
# ---------------------------------------------------------------------------


def _connections(node_id: str) -> list[dict[str, Any]]:
    payload = pa_graph.cached_graph()
    if not payload:
        return []
    by_id = {
        str(node["id"]): node for node in payload.get("nodes", []) if node.get("id")
    }
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in payload.get("edges", []):
        source_id, target_id = str(edge.get("from") or ""), str(edge.get("to") or "")
        if node_id == source_id:
            other, direction = target_id, "out"
        elif node_id == target_id:
            other, direction = source_id, "in"
        else:
            continue
        kind = str(edge.get("kind") or "link")
        existing = merged.get((other, kind))
        if existing is not None:
            if existing["direction"] != direction:
                existing["direction"] = "both"
            continue
        if len(merged) >= MAX_CONNECTIONS:
            break
        node = by_id.get(other) or {}
        merged[(other, kind)] = {
            "id": other,
            "title": str(node.get("label") or other),
            "cluster": str(node.get("cluster") or ""),
            "kind": kind,
            "direction": direction,
            "label": kind,
        }
    return list(merged.values())


# ---------------------------------------------------------------------------
# Node preview — per-source loaders
# ---------------------------------------------------------------------------


def _qmd_vault_paths() -> list[str]:
    """Vault-relative note paths known to qmd (bounded, best-effort)."""
    try:
        with contextlib.closing(pa_graph._open_sqlite_ro(_qmd_index_path())) as conn:
            rows = conn.execute(
                "SELECT path FROM documents WHERE collection = 'vault' AND active = 1 "
                "LIMIT ?",
                (pa_graph.QMD_METADATA_ROW_LIMIT,),
            ).fetchall()
    except Exception:
        return []
    return [str(row["path"] or "") for row in rows]


def _loose_vault_key(value: str) -> str:
    """Underscore-tolerant variant of the normalized vault path.

    qmd stores note paths with leading underscores stripped (``_agents/`` is
    indexed as ``agents/``), while :func:`pa_graph._normalize_vault_path` folds
    ``_`` into ``-`` (``_agents`` -> ``-agents``). Ids therefore come out of the
    index as ``vault:agents/...`` while the same file on disk normalizes to
    ``vault:-agents/...``, which makes every note under ``_agents``/
    ``_coordination`` unresolvable by an exact reverse lookup. Comparing on this
    key — leading dashes dropped per segment — bridges the two spellings without
    introducing a second id definition: the id itself stays pa_graph's.
    """
    return "/".join(
        segment.lstrip("-") for segment in pa_graph._normalize_vault_path(value).split("/")
    )


def _scan_vault_for(root: Path, normalized: str) -> Path | None:
    """Bounded walk resolving a normalized id back to its real file.

    ``_normalize_vault_path`` is lossy (case-folded, whitespace/underscores
    collapsed to dashes), so an exact path join can miss even though the note
    exists — e.g. ``Übungs-Notiz Größe.md``.
    """
    scanned = 0
    # Exact matches win outright; a loose (underscore-tolerant) hit is only
    # used when the whole walk produced no exact one.
    loose: Path | None = None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in sorted(dirnames) if name not in {".git", ".obsidian", "node_modules"}
        ]
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            scanned += 1
            if scanned > pa_graph.VAULT_WALK_FILE_CAP:
                return loose
            candidate = Path(dirpath) / name
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if pa_graph._normalize_vault_path(relative) == normalized:
                return candidate
            if _loose_vault_key(relative) == _loose_vault_key(normalized):
                loose = loose or candidate
    return loose


def _qmd_vault_note(normalized: str) -> tuple[str, str] | None:
    """Resolve a normalized id to ``(vault-relative path, raw text)`` via qmd.

    The index is the authoritative bridge between an id and its note: qmd rewrites
    paths when indexing (leading ``_`` stripped, ``_`` -> ``-``), so the id spelling
    frequently has no counterpart on disk. Reading the indexed content keeps node
    preview O(1)-ish instead of walking a 90k-file vault, and never touches the
    filesystem — so no path can escape the vault by construction.
    """
    target = _loose_vault_key(normalized)
    try:
        with contextlib.closing(pa_graph._open_sqlite_ro(_qmd_index_path())) as conn:
            rows = conn.execute(
                "SELECT d.path AS path, c.doc AS doc FROM documents d "
                "JOIN content c ON c.hash = d.hash "
                "WHERE d.collection = 'vault' AND d.active = 1 LIMIT ?",
                (pa_graph.QMD_METADATA_ROW_LIMIT,),
            ).fetchall()
    except Exception:
        return None
    for row in rows:
        indexed = str(row["path"] or "")
        if not indexed:
            continue
        if (
            pa_graph._normalize_vault_path(indexed) == normalized
            or _loose_vault_key(indexed) == target
        ):
            return indexed, str(row["doc"] or "")
    return None


def _resolve_vault_file(normalized: str, *, deep: bool = True) -> Path | None:
    """Map a normalized id back to a real file.

    ``deep`` enables the bounded full-vault walk, which is the only way to find
    notes qmd never indexed but costs a five-figure number of ``stat`` calls —
    callers with a cheaper fallback available should try ``deep=False`` first.
    """
    root = _vault_root()
    if not root.is_dir():
        return None
    direct = _guarded_join(root, normalized)
    if direct is not None and direct.is_file():
        return direct
    target = _loose_vault_key(normalized)
    for indexed in _qmd_vault_paths():
        if (
            pa_graph._normalize_vault_path(indexed) == normalized
            or _loose_vault_key(indexed) == target
        ):
            candidate = _guarded_join(root, indexed)
            if candidate is not None and candidate.is_file():
                return candidate
    if not deep:
        return None
    found = _scan_vault_for(root.resolve(strict=False), normalized)
    if found is None:
        return None
    # Re-run the escape guard on the walk result (symlinked trees).
    try:
        relative = found.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return None
    return _guarded_join(root, relative)


def _node_vault(rest: str, node_id: str) -> dict[str, Any]:
    _check_relative(rest, what="vault path")
    if not rest.lower().endswith(".md"):
        raise _bad_request("invalid vault path")
    # Cheap disk hit -> indexed copy -> bounded walk for never-indexed notes.
    path = _resolve_vault_file(rest, deep=False)
    indexed = _qmd_vault_note(rest) if path is None else None
    if path is None and indexed is None:
        path = _resolve_vault_file(rest, deep=True)
    if path is not None:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise _not_found() from None
        relative = path.relative_to(_vault_root().resolve(strict=False)).as_posix()
        stem = path.stem
    elif indexed is not None:
        relative, raw = indexed
        stem = Path(relative).stem
    else:
        raise _not_found()
    heading = pa_graph._HEADING_RE.search(raw)
    title = heading.group(1).strip() if heading else stem
    return {
        "id": node_id,
        "title": title,
        "cluster": pa_graph._cluster_for_vault_path(relative),
        "body": _body_text(raw),
        # Only vault-relative refs — never an absolute filesystem path.
        "metadata": _sanitize_meta({"ref": f"vault://{relative}", "path": relative}),
        "connections": _connections(node_id),
        "source": "vault",
    }


def _node_task(task_id: str, node_id: str) -> dict[str, Any]:
    if not _TASK_ID_RE.match(task_id):
        raise _bad_request("invalid task id")
    db_path, _board = _kanban_location()
    try:
        conn_ctx = pa_graph._open_sqlite_ro(db_path)
    except (FileNotFoundError, sqlite3.Error):
        raise _not_found() from None
    with contextlib.closing(conn_ctx) as conn:
        columns = pa_graph._table_columns(conn, "tasks")
        if not {"id", "title"} <= columns:
            raise _not_found()
        selected = [
            column
            for column in ("status", "assignee", "created_by", "priority", "created_at")
            if column in columns
        ]
        body_expr = "COALESCE(body, '')" if "body" in columns else "''"
        row = conn.execute(
            f"SELECT id, title, {body_expr} AS body"
            + ("".join(f", {column}" for column in selected))
            + " FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise _not_found()
    return {
        "id": node_id,
        "title": str(row["title"] or task_id),
        "cluster": "projekte",
        "body": _body_text(str(row["body"] or "")),
        "metadata": _sanitize_meta(
            {column: row[column] for column in selected}
            | {"ref": f"/control/fleet?task={task_id}"}
        ),
        "connections": _connections(node_id),
        "source": "kanban",
    }


def _node_memory(rest: str, node_id: str) -> dict[str, Any]:
    source_name, _, relative = rest.partition(":")
    if not source_name or not relative:
        raise _bad_request("invalid memory id")
    _check_relative(relative, what="memory path")
    root = dict(_memory_roots()).get(source_name)
    if root is None:
        raise _bad_request("unknown memory root")
    path = _guarded_join(root, relative)
    if path is None:
        raise _bad_request("invalid memory path")
    if not path.is_file():
        raise _not_found()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise _not_found() from None
    heading = pa_graph._HEADING_RE.search(raw)
    return {
        "id": node_id,
        "title": heading.group(1).strip() if heading else path.stem,
        "cluster": "memories",
        "body": _body_text(raw),
        "metadata": _sanitize_meta(
            {"ref": f"memory://{source_name}/{relative}", "root": source_name}
        ),
        "connections": _connections(node_id),
        "source": "memory",
    }


def node(node_id: str) -> dict[str, Any]:
    node_id = str(node_id or "").strip()
    if not node_id or len(node_id) > 512:
        raise _bad_request("invalid node id")
    if any(char in node_id for char in ("\x00", "\n", "\r")):
        raise _bad_request("invalid node id")

    cached = _cache_get(_node_cache, node_id)
    if cached is not None:
        return cached

    prefix, _, rest = node_id.partition(":")
    if prefix == "vault":
        payload = _node_vault(rest, node_id)
    elif prefix == "task":
        payload = _node_task(rest, node_id)
    elif prefix == "memory":
        payload = _node_memory(rest, node_id)
    else:
        raise _bad_request("invalid node id")

    _cache_put(_node_cache, node_id, payload)
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_pa_search_routes(app: FastAPI) -> None:
    """Attach the B3 brain-search + node-preview routes to the dashboard app."""

    @app.get("/api/pa/search")
    def pa_search_endpoint(  # pyright: ignore[reportUnusedFunction]
        q: str = Query(default=""),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        return search(q, limit)

    @app.get("/api/pa/node")
    def pa_node_endpoint(  # pyright: ignore[reportUnusedFunction]
        id: str = Query(default=""),  # noqa: A002 — contract-mandated query name
    ) -> dict[str, Any]:
        return node(id)


__all__ = [
    "register_pa_search_routes",
    "search",
    "node",
    "invalidate_search_cache",
]

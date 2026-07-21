"""Jarvis B3: brain search and node preview.

Implements the normalized API contract for /api/pa/search and /api/pa/node
on top of pa_graph's canonical node identities.

Sources:
  - vault: qmd index (documents_fts -> documents -> content)
  - kanban: LIKE over tasks.title/body (kanban.db has no FTS)
  - memory: bounded markdown scan of pa_graph memory roots

Error isolation: a single source failing must still return HTTP 200 with
partial results and a structured errors[] entry; no hangs, no 5xx.
"""
from __future__ import annotations

import copy
import re
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from hermes_constants import get_hermes_home
import hermes_cli.pa_graph as pa_graph

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_QUERY_MAX_LEN = 300
_CACHE_TTL_SECONDS = 15.0
_PER_SOURCE_LIMIT = 50
_SEARCH_LIMIT_MAX = 50
_NODE_BODY_MAX = 5000
_MEMORY_FILE_CAP = 500
_STATEMENT_TIMEOUT_MS = 2000
SOURCE_TIMEOUT_SECONDS = 5.0

_search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"«redacted:[^»]*»"),
)


def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


# ---------------------------------------------------------------------------
# Query normalization (mirror of pa_graph._normalize_query)
# ---------------------------------------------------------------------------

def _normalize_query(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = re.sub(r"[\x00-\x1F\x7F]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if len(normalized) > _QUERY_MAX_LEN:
        normalized = normalized[:_QUERY_MAX_LEN].rstrip()
    return normalized


def _title_from_rel_path(rel_path: str) -> str:
    stem = rel_path.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    return stem.replace("-", " ").replace("_", " ").strip().title()


# ---------------------------------------------------------------------------
# Path helpers — monkeypatchable names expected by the test fixture
# ---------------------------------------------------------------------------

def _vault_root() -> Path:
    return pa_graph._vault_root()


def _qmd_index_path() -> Path | None:
    return pa_graph._qmd_index_path()


def _kanban_location() -> tuple[Path, str]:
    return pa_graph._kanban_location()


def _memory_roots() -> list[tuple[str, Path, str]]:
    return pa_graph._memory_roots()


# ---------------------------------------------------------------------------
# Local path-safety helper (pa_graph has no _safe_rel_md; keep in sync with
# the contract used by pa_graph's vault loading)
# ---------------------------------------------------------------------------

def _safe_rel_md(path: str, root: Path) -> Path | None:
    """Return an absolute path inside *root* for a relative markdown path.

    Returns None for absolute paths, traversal escapes, or non-md files.
    """
    if not path:
        return None
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            return None
        normalized = Path(path).as_posix()
        if normalized.startswith("../") or "/../" in normalized or normalized == "..":
            return None
        resolved = (root / path).resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if resolved.suffix.lower() != ".md":
        return None
    return resolved


def _reverse_normalize_vault_path(normalized: str, vault_root: Path) -> str | None:
    """Map a normalized vault id path back to the on-disk relative path.

    The id is case-folded with dashes; the file on disk may have spaces and
    mixed case. We walk the vault and compare normalized forms.
    """
    if not normalized:
        return None
    try:
        for candidate in vault_root.rglob("*.md"):
            rel = candidate.relative_to(vault_root).as_posix()
            if pa_graph._normalize_vault_path(rel) == normalized:
                return rel
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Vault search (qmd FTS5)
# ---------------------------------------------------------------------------

def _search_vault(query: str, limit: int) -> list[dict[str, Any]]:
    db_path = _qmd_index_path()
    if db_path is None or not db_path.exists():
        raise FileNotFoundError("qmd index not found")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.25)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_STATEMENT_TIMEOUT_MS}")
        rows = conn.execute(
            """
            SELECT d.path, d.title, snippet(documents_fts, 2, '<b>', '</b>', '...', 32) AS snip
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            WHERE documents_fts MATCH ? AND d.active = 1 AND d.collection = 'vault'
            ORDER BY bm25(documents_fts)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for path, title, snippet in rows:
        node = pa_graph._vault_node(str(path), str(title or ""), rank=0)
        items.append(
            {
                "id": node["id"],
                "ref": str(path),
                "title": str(title or node["title"]),
                "cluster": node.get("cluster", ""),
                "snippet": _redact_secrets(str(snippet or "")),
                "score": 0,
                "kind": "vault",
                "source": "vault",
                "in_graph": False,
                "meta": {},
            }
        )
    return items


# ---------------------------------------------------------------------------
# Kanban search (LIKE, no FTS available)
# ---------------------------------------------------------------------------

def _search_kanban(query: str, limit: int) -> list[dict[str, Any]]:
    db_path, _slug = _kanban_location()
    if not db_path.exists():
        raise FileNotFoundError("kanban db not found")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.25)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_STATEMENT_TIMEOUT_MS}")
        like = f"%{query}%"
        rows = conn.execute(
            """
            SELECT id, title, body
            FROM tasks
            WHERE title LIKE ? OR body LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for task_id, title, body in rows:
        node_id = f"task:{task_id}"
        snippet = ""
        if body:
            idx = body.lower().find(query.lower())
            if idx >= 0:
                start = max(0, idx - 40)
                end = min(len(body), idx + len(query) + 80)
                snippet = body[start:end].replace("\n", " ")
        items.append(
            {
                "id": node_id,
                "ref": f"task:{task_id}",
                "title": str(title or ""),
                "cluster": "projekte",
                "snippet": _redact_secrets(snippet),
                "score": 0,
                "kind": "task",
                "source": "kanban",
                "in_graph": False,
                "meta": {"task_id": task_id},
            }
        )
    return items


# ---------------------------------------------------------------------------
# Memory search (bounded markdown scan)
# ---------------------------------------------------------------------------

def _search_memory(query: str, limit: int) -> list[dict[str, Any]]:
    roots = _memory_roots()
    if not roots:
        return []
    query_lower = query.lower()
    items: list[dict[str, Any]] = []
    scanned = 0
    for source, root, cluster in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if scanned >= _MEMORY_FILE_CAP:
                break
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            if query_lower not in text.lower():
                continue
            node_id = f"memory:{source}:{rel.casefold()}"
            idx = text.lower().find(query_lower)
            start = max(0, idx - 40)
            end = min(len(text), idx + len(query) + 80)
            snippet = text[start:end].replace("\n", " ")
            title = path.stem.replace("-", " ").replace("_", " ").strip().title()
            items.append(
                {
                    "id": node_id,
                    "ref": rel,
                    "title": title,
                    "cluster": cluster,
                    "snippet": _redact_secrets(snippet),
                    "score": 0,
                    "kind": "memory",
                    "source": "memory",
                    "in_graph": False,
                    "meta": {"source": source},
                }
            )
            if len(items) >= limit:
                break
        if len(items) >= limit or scanned >= _MEMORY_FILE_CAP:
            break
    return items


# ---------------------------------------------------------------------------
# Search orchestration with per-source timeout + error isolation
# ---------------------------------------------------------------------------

def invalidate_search_cache() -> None:
    with _cache_lock:
        _search_cache.clear()


def _run_source(
    name: str,
    fn: Any,
    query: str,
    limit: int,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, query, limit)
        try:
            return future.result(timeout=SOURCE_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            errors.append({"source": name, "error": "source timed out"})
            return []
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": name, "error": str(exc)})
            return []


def _annotate_in_graph(items: list[dict[str, Any]]) -> None:
    """Set in_graph from the current graph cache without triggering a rebuild."""
    try:
        payload = pa_graph._cache_payload
        if not payload:
            return
        graph_ids = {str(n["id"]) for n in payload.get("nodes", []) if n.get("id")}
        for item in items:
            item["in_graph"] = item["id"] in graph_ids
    except Exception:  # noqa: BLE001
        pass


def search(query: str, limit: int = 20) -> dict[str, Any]:
    query = _normalize_query(query)
    limit = max(1, min(int(limit), _SEARCH_LIMIT_MAX))
    cache_key = f"{query}\x00{limit}"
    now = time.monotonic()
    with _cache_lock:
        cached = _search_cache.get(cache_key)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    errors: list[dict[str, str]] = []
    all_items: list[dict[str, Any]] = []

    if len(query) < 2:
        payload = {"query": query, "items": [], "took_ms": 0, "errors": []}
        with _cache_lock:
            _search_cache[cache_key] = (now, payload)
        return copy.deepcopy(payload)

    started = time.monotonic()

    vault_items = _run_source("vault", _search_vault, query, _PER_SOURCE_LIMIT, errors)
    kanban_items = _run_source("kanban", _search_kanban, query, _PER_SOURCE_LIMIT, errors)
    memory_items = _run_source("memory", _search_memory, query, _PER_SOURCE_LIMIT, errors)

    # Dedupe by canonical id, preserve first-seen order
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in vault_items + kanban_items + memory_items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        deduped.append(item)

    # Deterministic interleave: round-robin across sources
    by_source: dict[str, list[dict[str, Any]]] = {"vault": [], "kanban": [], "memory": []}
    for item in deduped:
        by_source.setdefault(item["source"], []).append(item)
    result_items: list[dict[str, Any]] = []
    idx = 0
    while len(result_items) < limit:
        added = False
        for src in ("vault", "kanban", "memory"):
            if idx < len(by_source.get(src, [])):
                result_items.append(by_source[src][idx])
                added = True
                if len(result_items) >= limit:
                    break
        if not added:
            break
        idx += 1

    _annotate_in_graph(result_items)

    took_ms = int((time.monotonic() - started) * 1000)
    payload = {
        "query": query,
        "items": result_items,
        "took_ms": took_ms,
        "errors": errors,
    }
    with _cache_lock:
        _search_cache[cache_key] = (now, payload)
    return copy.deepcopy(payload)


# ---------------------------------------------------------------------------
# Node preview
# ---------------------------------------------------------------------------

def _node_connections(node_id: str) -> list[dict[str, Any]]:
    try:
        payload = pa_graph._cache_payload
        if not payload:
            return []
        connections: list[dict[str, Any]] = []
        for edge in payload.get("edges", []):
            source = edge.get("source")
            target = edge.get("target")
            if source != node_id and target != node_id:
                continue
            other = target if source == node_id else source
            direction = "out" if source == node_id else "in"
            other_meta = next(
                (n for n in payload.get("nodes", []) if n.get("id") == other), {}
            )
            connections.append(
                {
                    "id": other,
                    "title": other_meta.get("title", ""),
                    "cluster": other_meta.get("cluster", ""),
                    "kind": edge.get("kind", ""),
                    "direction": direction,
                    "label": edge.get("label", ""),
                }
            )
        return connections
    except Exception:  # noqa: BLE001
        return []


def _node_vault(normalized_path: str, node_id: str) -> dict[str, Any]:
    vault_root = _vault_root()
    # Reverse the lossy normalized id back to the on-disk relative path
    rel_path = _reverse_normalize_vault_path(normalized_path, vault_root)
    if rel_path is None:
        raise HTTPException(status_code=404, detail="vault node not found")
    abs_path = _safe_rel_md(rel_path, vault_root)
    if abs_path is None:
        raise HTTPException(status_code=400, detail="invalid or unsafe vault path")
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="vault node not found")
    # Reject symlink escapes: resolve must stay inside vault root
    try:
        resolved = abs_path.resolve()
        resolved.relative_to(vault_root.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="vault node not found")

    # Try qmd index first for content
    body = ""
    metadata: dict[str, Any] = {}
    db_path = _qmd_index_path()
    if db_path is not None and db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.25)
            try:
                conn.execute(f"PRAGMA busy_timeout = {_STATEMENT_TIMEOUT_MS}")
                row = conn.execute(
                    "SELECT d.title, c.doc FROM documents d JOIN content c ON c.hash = d.hash "
                    "WHERE d.collection = 'vault' AND d.path = ? AND d.active = 1",
                    (rel_path,),
                ).fetchone()
                if row:
                    metadata["title"] = row[0]
                    body = row[1] or ""
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    if not body:
        try:
            body = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=404, detail="vault node unreadable") from exc
    body = _redact_secrets(body[:_NODE_BODY_MAX])

    title = metadata.get("title") or _title_from_rel_path(rel_path)
    node = pa_graph._vault_node(rel_path, title, rank=0)
    return {
        "id": node_id,
        "title": node["title"],
        "cluster": node.get("cluster", ""),
        "body": body,
        "metadata": metadata,
        "connections": _node_connections(node_id),
        "source": "vault",
    }


def _node_kanban(task_id: str, node_id: str) -> dict[str, Any]:
    db_path, _slug = _kanban_location()
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="kanban node not found")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.25)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_STATEMENT_TIMEOUT_MS}")
            row = conn.execute(
                "SELECT id, title, body, status, assignee, created_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"kanban lookup failed: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="kanban node not found")
    task_id_db, title, body, status, assignee, created_at = row
    return {
        "id": node_id,
        "title": str(title or ""),
        "cluster": "projekte",
        "body": _redact_secrets(str(body or "")[:_NODE_BODY_MAX]),
        "metadata": {
            "task_id": task_id_db,
            "status": status,
            "assignee": assignee,
            "created_at": created_at,
        },
        "connections": _node_connections(node_id),
        "source": "kanban",
    }


def _node_memory(source: str, rel_path: str, node_id: str) -> dict[str, Any]:
    roots = _memory_roots()
    root: Path | None = None
    cluster = "memories"
    for src, candidate_root, candidate_cluster in roots:
        if src == source:
            root = candidate_root
            cluster = candidate_cluster
            break
    if root is None:
        raise HTTPException(status_code=400, detail="unknown memory source")
    abs_path = _safe_rel_md(rel_path, root)
    if abs_path is None:
        raise HTTPException(status_code=400, detail="invalid or unsafe memory path")
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="memory node not found")
    try:
        body = abs_path.read_text(encoding="utf-8", errors="replace")[:_NODE_BODY_MAX]
    except OSError as exc:
        raise HTTPException(status_code=404, detail="memory node unreadable") from exc
    title = Path(rel_path).stem.replace("-", " ").replace("_", " ").strip().title()
    return {
        "id": node_id,
        "title": title,
        "cluster": cluster,
        "body": _redact_secrets(body),
        "metadata": {"source": source, "rel_path": rel_path},
        "connections": _node_connections(node_id),
        "source": "memory",
    }


def node(node_id: str) -> dict[str, Any]:
    if not node_id:
        raise HTTPException(status_code=400, detail="missing node id")
    # Reject absolute paths and traversal up-front
    if node_id.startswith("/") or node_id.startswith(".."):
        raise HTTPException(status_code=400, detail="invalid node id")

    if node_id.startswith("vault:"):
        rest = node_id[len("vault:"):]
        # Reject traversal / absolute / tilde in vault paths
        if rest.startswith("/") or rest.startswith("~") or ".." in rest:
            raise HTTPException(status_code=400, detail="invalid vault node id")
        if not rest.endswith(".md"):
            raise HTTPException(status_code=400, detail="invalid vault node id")
        return _node_vault(rest, node_id)
    if node_id.startswith("task:"):
        rest = node_id[len("task:"):]
        if not rest or "/" in rest or "\\" in rest or rest.startswith("."):
            raise HTTPException(status_code=400, detail="invalid task node id")
        return _node_kanban(rest, node_id)
    if node_id.startswith("memory:"):
        rest = node_id[len("memory:"):]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="invalid memory node id")
        source, rel_path = parts
        if not source or not rel_path:
            raise HTTPException(status_code=400, detail="invalid memory node id")
        if rel_path.startswith("/") or ".." in rel_path:
            raise HTTPException(status_code=400, detail="invalid memory node id")
        return _node_memory(source, rel_path, node_id)
    raise HTTPException(status_code=400, detail="unknown node id format")


# ---------------------------------------------------------------------------
# FastAPI route registration
# ---------------------------------------------------------------------------

def register_pa_search_routes(app: FastAPI) -> None:
    @app.get("/api/pa/search")
    def pa_search_endpoint(
        q: str = Query("", description="Search query"),
        limit: int = Query(20, ge=1, le=_SEARCH_LIMIT_MAX),
    ) -> dict[str, Any]:
        return search(q, limit)

    @app.get("/api/pa/node")
    def pa_node_endpoint(id: str = Query(..., description="Node id")) -> dict[str, Any]:  # noqa: A002
        return node(id)

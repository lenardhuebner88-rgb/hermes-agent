"""Jarvis "Gehirn-Suche" + Node-Preview (Slice B3).

Implements the normalized API contract for:

- ``GET /api/pa/search?q=…`` — unified full-text search across the three
  brain sources (vault/qmd FTS5, kanban tasks, SessionDB memory FTS5),
  deterministic dedupe + global limit, per-source error isolation
  (a single failing source still yields HTTP 200 with partial results and
  a structured ``errors[]`` entry), and a short-TTL response cache.
- ``GET /api/pa/node?id=…`` — node preview with body/metadata/connections
  for vault notes, kanban tasks and session messages. Unknown IDs → 404;
  invalid/traversal/absolute-path IDs → 400; no absolute path leaks.

Node IDs are *exactly* the ``pa_graph`` node IDs (``vault:…``, ``task:…``,
``session:msg:…``) — this module never invents a parallel ID scheme.
"""

from __future__ import annotations

import math
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
SEARCH_MAX_LIMIT = 50
SOURCE_TIMEOUT_SECONDS = 5.0
MAX_SNIPPET = 320
MAX_BODY = 8192
MAX_CONNECTIONS = 50
MAX_META_ENTRIES = 50
MAX_META_VALUE = 256
_QMD_MIN_SCORE = 0.001

_VAULT_ROOT = Path("/home/piet/vault").expanduser().resolve(strict=False)
# Token-ish secrets must never leak through search snippets or node bodies.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+|password\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]*$")


def _clock() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# Path helpers (delegating to pa_graph where possible)
# ---------------------------------------------------------------------------


def _vault_root() -> Path:
    return _VAULT_ROOT


def _qmd_db_path() -> Path:
    return pa_graph._qmd_db_path()


def _kanban_location() -> tuple[Path, str]:
    return pa_graph._kanban_location()


def _safe_rel_md(path: str, root: Path) -> Path | None:
    """Same contract as pa_graph._safe_rel_md."""
    return pa_graph._safe_rel_md(path, root)


def _graph_node_ids() -> set[str]:
    try:
        graph = pa_graph.build_graph()
    except Exception:
        return set()
    return {str(n.get("id", "")) for n in graph.get("nodes", []) if n.get("id")}


def _scrub(text: str, limit: int) -> str:
    text = _SECRET_RE.sub("[redacted]", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _sanitize_meta(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if len(out) >= MAX_META_ENTRIES:
            break
        if value is None:
            continue
        if isinstance(value, str):
            value = _scrub(value, MAX_META_VALUE)
            if not value:
                continue
        out[str(key)] = value
    return out


# ---------------------------------------------------------------------------
# Query normalization
# ---------------------------------------------------------------------------


def _normalize_query(q: str) -> str:
    q = (q or "").strip()
    q = re.sub(r"\s+", " ", q)
    return q[:128]


def _fts_query(q: str) -> str:
    """Build a tolerant FTS5 MATCH query from user input (quoted OR terms)."""
    terms = re.findall(r"[\wäöüÄÖÜß]+", q, flags=re.UNICODE)
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms[:8])


# ---------------------------------------------------------------------------
# Per-source timeouts (threads — sqlite connections are created inside the
# worker thread, so a hung query never blocks the event loop forever)
# ---------------------------------------------------------------------------


class _SourceTimeout(RuntimeError):
    pass


def _run_with_timeout(fn, timeout: float):
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced via errors[]
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise _SourceTimeout(f"source timed out after {timeout:.1f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# Search sources — each returns a list of hit dicts (pre-normalization)
# ---------------------------------------------------------------------------


def _search_vault(query: str, limit: int) -> list[dict[str, Any]]:
    db_path = _qmd_db_path()
    if not db_path.is_file():
        raise FileNotFoundError(f"qmd index missing: {db_path.name}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    try:
        fts = _fts_query(query)
        rows = con.execute(
            """
            SELECT d.filepath AS filepath,
                   COALESCE(c.title, '') AS title,
                   COALESCE(c.doc, '') AS body,
                   bm25(documents_fts, 12.0, 6.0, 1.0) AS score
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            LEFT JOIN content c ON c.hash = d.hash
            WHERE documents_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
    finally:
        con.close()
    hits: list[dict[str, Any]] = []
    for row in rows:
        filepath = str(row["filepath"] or "")
        if not filepath or not filepath.lower().endswith(".md"):
            continue
        node_id = pa_graph._vault_node_id(filepath)
        if not node_id:
            continue
        hits.append(
            {
                "id": node_id,
                "ref": filepath,
                "title": str(row["title"] or "") or Path(filepath).stem,
                "snippet": _scrub(str(row["body"] or ""), MAX_SNIPPET),
                "score": -float(row["score"] or 0.0),
                "kind": "vault_note",
                "source": "vault",
            }
        )
    return hits


def _search_kanban(query: str, limit: int) -> list[dict[str, Any]]:
    db_path, board = _kanban_location()
    if not db_path.is_file():
        raise FileNotFoundError(f"kanban db missing: {db_path.name}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    try:
        like = f"%{query}%"
        rows = con.execute(
            """
            SELECT id, title, COALESCE(body, '') AS body, status
            FROM tasks
            WHERE board = ? AND (title LIKE ? OR body LIKE ?)
            ORDER BY priority DESC, id
            LIMIT ?
            """,
            (board, like, like, limit),
        ).fetchall()
    finally:
        con.close()
    hits: list[dict[str, Any]] = []
    for row in rows:
        body = str(row["body"] or "")
        hits.append(
            {
                "id": f"task:{row['id']}",
                "ref": f"kanban://tasks/{row['id']}",
                "title": str(row["title"] or row["id"]),
                "snippet": _scrub(body, MAX_SNIPPET),
                "score": 1.0,
                "kind": "kanban_task",
                "source": "kanban",
                "meta": {"status": row["status"]},
            }
        )
    return hits


def _SessionDB(profile: str | None = None):  # noqa: N802 — indirection for tests
    from hermes_state import SessionDB

    return SessionDB(profile)


def _search_memory(query: str, limit: int) -> list[dict[str, Any]]:
    with _SessionDB(None) as db:
        rows = db.search_messages(query, role_filter=None, limit=limit, offset=0)
    hits: list[dict[str, Any]] = []
    for row in rows:
        role = str(row.get("role", ""))
        if role not in pa_graph._CONTENT_ROLES:
            continue
        session_id = str(row.get("session_id", "") or "")
        profile = str(row.get("profile", "") or "")
        node_id = f"session:msg:{row.get('id')}:{session_id}:{profile}"
        content = str(row.get("content", "") or "")
        hits.append(
            {
                "id": node_id,
                "ref": f"session://{profile}/{session_id}#{row.get('id')}",
                "title": _scrub(content, 72) or "(message)",
                "snippet": _scrub(str(row.get("snippet") or content), MAX_SNIPPET),
                "score": 1.0,
                "kind": "session_message",
                "source": "memory",
                "meta": {"role": role, "session_id": session_id},
            }
        )
    return hits


_SOURCES = (
    ("vault", _search_vault),
    ("kanban", _search_kanban),
    ("memory", _search_memory),
)


# ---------------------------------------------------------------------------
# Search cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_search_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_node_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _reset_caches() -> None:
    with _cache_lock:
        _search_cache.clear()
        _node_cache.clear()


def _cache_get(cache: dict, key) -> dict[str, Any] | None:
    with _cache_lock:
        entry = cache.get(key)
        if entry is None:
            return None
        ts, payload = entry
        if _clock() - ts > CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None
        return dict(payload)


def _cache_put(cache: dict, key, payload: dict[str, Any]) -> None:
    with _cache_lock:
        if len(cache) >= CACHE_MAX_ENTRIES:
            oldest = min(cache.items(), key=lambda kv: kv[1][0])[0]
            cache.pop(oldest, None)
        cache[key] = (_clock(), dict(payload))


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------


def _execute_search(query: str, limit: int) -> dict[str, Any]:
    start = _clock()
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    per_source = max(1, min(limit * 3, SEARCH_MAX_LIMIT * 3))

    for name, fn in _SOURCES:
        try:
            hits = _run_with_timeout(lambda fn=fn: fn(query, per_source), SOURCE_TIMEOUT_SECONDS)
        except BaseException as exc:  # noqa: BLE001 — isolated per source
            errors.append({"source": name, "error": f"{type(exc).__name__}: {exc}"[:240]})
            continue
        for hit in hits or []:
            hid = hit.get("id")
            if not hid or hid in seen:
                continue
            seen.add(hid)
            items.append(hit)

    # Deterministic global ordering: score desc, then id.
    items.sort(key=lambda h: (-float(h.get("score", 0.0)), str(h.get("id", ""))))
    items = items[:limit]

    graph_ids = _graph_node_ids()
    out_items: list[dict[str, Any]] = []
    for hit in items:
        out_items.append(
            {
                "id": hit["id"],
                "ref": hit.get("ref", ""),
                "title": hit.get("title", ""),
                "cluster": hit.get("source", ""),
                "snippet": hit.get("snippet", ""),
                "score": round(float(hit.get("score", 0.0)), 4),
                "kind": hit.get("kind", ""),
                "source": hit.get("source", ""),
                "in_graph": hit["id"] in graph_ids,
                "meta": _sanitize_meta(hit.get("meta") or {}),
            }
        )

    return {
        "query": query,
        "items": out_items,
        "took_ms": int((_clock() - start) * 1000),
        "errors": errors,
    }


def _search(query: str, limit: int) -> dict[str, Any]:
    query = _normalize_query(query)
    limit = max(1, min(int(limit or 12), SEARCH_MAX_LIMIT))
    key = (query, limit)
    cached = _cache_get(_search_cache, key)
    if cached is not None:
        return cached
    payload = _execute_search(query, limit)
    _cache_put(_search_cache, key, payload)
    return dict(payload)


# ---------------------------------------------------------------------------
# Node preview
# ---------------------------------------------------------------------------


def _validate_node_id(node_id: str) -> None:
    if not node_id or len(node_id) > 512:
        raise HTTPException(status_code=400, detail="invalid node id")
    if "\x00" in node_id or "\n" in node_id or "\r" in node_id:
        raise HTTPException(status_code=400, detail="invalid node id")
    if node_id.startswith("vault:"):
        rest = node_id[len("vault:"):]
        if rest.startswith("/") or rest.startswith("~"):
            raise HTTPException(status_code=400, detail="invalid vault path")
        parts = Path(rest).parts
        if any(part == ".." for part in parts):
            raise HTTPException(status_code=400, detail="invalid vault path")
    elif node_id.startswith(("task:", "session:")):
        if not _ID_SAFE_RE.match(node_id):
            raise HTTPException(status_code=400, detail="invalid node id")
    else:
        raise HTTPException(status_code=400, detail="invalid node id")


def _connections(node_id: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Build connection stubs for F3 — resolved from the live pa_graph edges."""
    out: list[dict[str, Any]] = []
    try:
        graph = pa_graph.build_graph()
    except Exception:
        return out
    by_id = {str(n.get("id", "")): n for n in graph.get("nodes", []) if n.get("id")}
    for edge in graph.get("edges", []):
        src, dst = str(edge.get("source", "")), str(edge.get("target", ""))
        if src != node_id and dst != node_id:
            continue
        other = dst if src == node_id else src
        direction = "out" if src == node_id else "in"
        other_node = by_id.get(other, {})
        out.append(
            {
                "id": other,
                "title": str(other_node.get("title", "") or other),
                "cluster": str(other_node.get("cluster", "")),
                "kind": str(edge.get("kind", "")),
                "direction": direction,
                "label": str(edge.get("kind", "")),
            }
        )
        if len(out) >= MAX_CONNECTIONS:
            break
    return out


def _node_vault(rest: str, node_id: str) -> dict[str, Any]:
    if not rest.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="invalid vault path")
    root = _vault_root()
    path = _safe_rel_md(rest, root)
    if path is None:
        raise HTTPException(status_code=400, detail="invalid vault path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="node not found")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise HTTPException(status_code=404, detail="node not found") from None
    # Title: first markdown heading, else stem — mirrors pa_graph's display title.
    title = path.stem
    body_lines = raw.splitlines()
    for line in body_lines[:200]:
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            title = m.group(1).strip()
            break
    body = _scrub(raw, MAX_BODY)
    outgoing = _outgoing_links(raw, rest, root)
    connections = _connections(node_id, {})
    known = {c["id"] for c in connections}
    for target_rel in outgoing:
        tid = pa_graph._vault_node_id(target_rel)
        if not tid or tid == node_id or tid in known:
            continue
        connections.append(
            {
                "id": tid,
                "title": Path(target_rel).stem,
                "cluster": "vault",
                "kind": "links_to",
                "direction": "out",
                "label": "links_to",
            }
        )
        known.add(tid)
        if len(connections) >= MAX_CONNECTIONS:
            break
    return {
        "id": node_id,
        "title": title,
        "cluster": "vault",
        "body": body,
        "metadata": _sanitize_meta({"ref": rest}),
        "connections": connections,
        "source": "vault",
    }


def _outgoing_links(text: str, rel: str, root: Path) -> list[str]:
    """Resolve [[wiki]] + markdown links the same way pa_graph does."""
    cur_dir = Path(rel).parent
    out: list[str] = []
    for link in pa_graph._WIKILINK_RE.findall(text):
        resolved = pa_graph._resolve_wikilink(link, cur_dir, root)
        if resolved and resolved not in out:
            out.append(resolved)
    for link in pa_graph._MDLINK_RE.findall(text):
        resolved = pa_graph._resolve_rel_link(link, cur_dir)
        if resolved and resolved not in out:
            out.append(resolved)
    return out


def _node_task(task_id: str, node_id: str) -> dict[str, Any]:
    db_path, board = _kanban_location()
    row = None
    if db_path.is_file():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT id, title, COALESCE(body, '') AS body, status, assignee, priority, "
                "created_at, completed_at FROM tasks WHERE id = ? AND board = ?",
                (task_id, board),
            ).fetchone()
        finally:
            con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="node not found")
    meta = _sanitize_meta(
        {
            "status": row["status"],
            "assignee": row["assignee"],
            "priority": row["priority"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "ref": f"kanban://tasks/{row['id']}",
        }
    )
    return {
        "id": node_id,
        "title": str(row["title"] or row["id"]),
        "cluster": "kanban",
        "body": _scrub(str(row["body"] or ""), MAX_BODY),
        "metadata": meta,
        "connections": _connections(node_id, {}),
        "source": "kanban",
    }


def _node_session(node_id: str) -> dict[str, Any]:
    # session:msg:<rowid>:<session_id>:<profile>
    parts = node_id.split(":")
    if len(parts) < 5 or parts[1] != "msg":
        raise HTTPException(status_code=400, detail="invalid node id")
    try:
        rowid = int(parts[2])
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid node id") from None
    session_id, profile = parts[3], parts[4]
    with _SessionDB(None) as db:
        db_path = getattr(db, "db_path", None)
    if not db_path or not Path(db_path).is_file():
        raise HTTPException(status_code=404, detail="node not found")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id, role, content, tool_name, ts FROM messages WHERE id = ?",
            (rowid,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="node not found")
    content = str(row["content"] or "")
    return {
        "id": node_id,
        "title": _scrub(content, 72) or "(message)",
        "cluster": "sessions",
        "body": _scrub(content, MAX_BODY),
        "metadata": _sanitize_meta(
            {"role": row["role"], "session_id": session_id, "ts": row["ts"]}
        ),
        "connections": _connections(node_id, {}),
        "source": "memory",
    }


def _node(node_id: str) -> dict[str, Any]:
    node_id = (node_id or "").strip()
    _validate_node_id(node_id)
    cached = _cache_get(_node_cache, node_id)
    if cached is not None:
        return cached
    if node_id.startswith("vault:"):
        payload = _node_vault(node_id[len("vault:"):], node_id)
    elif node_id.startswith("task:"):
        payload = _node_task(node_id[len("task:"):], node_id)
    elif node_id.startswith("session:"):
        payload = _node_session(node_id)
    else:  # pragma: no cover — guarded by _validate_node_id
        raise HTTPException(status_code=400, detail="invalid node id")
    _cache_put(_node_cache, node_id, payload)
    return dict(payload)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_pa_search_routes(app: FastAPI) -> None:
    """Attach the B3 search/preview routes to the dashboard FastAPI app."""

    @app.get("/api/pa/search")
    def pa_search_endpoint(
        q: str = Query(default=""),
        limit: int = Query(default=12, ge=1, le=SEARCH_MAX_LIMIT),
    ) -> dict[str, Any]:
        query = _normalize_query(q)
        if not query:
            return {"query": query, "items": [], "took_ms": 0, "errors": []}
        return _search(query, limit)

    @app.get("/api/pa/node")
    def pa_node_endpoint(id: str = Query(default="")) -> dict[str, Any]:  # noqa: A002
        return _node(id)


__all__ = [
    "register_pa_search_routes",
    "_reset_caches",
    "_search",
    "_node",
    "_search_vault",
    "_search_kanban",
    "_search_memory",
    "_vault_root",
    "_qmd_db_path",
    "_kanban_location",
]

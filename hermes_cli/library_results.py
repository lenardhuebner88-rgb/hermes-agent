"""Bibliothek (/control) — Ergebnisse: abgeschlossene Kanban-Tasks als
menschen- UND LLM-lesbare Ergebnis-Digests.

Quelle ist ausschließlich das bestehende Kanban-Board (``tasks``/
``task_runs``) — kein neuer Store, kein Schreibzugriff. Öffnet die DB
IMMER read-only (``mode=ro``); die Bibliothek darf das Board nie
mutieren. Hausmuster wie ``library_view.py``:
``register_library_results_routes(app)`` unter ``/api/`` (erbt das
Session-Gate der Middleware, nie in ``PUBLIC_API_PATHS``).

Ein Task zählt als Ergebnis, sobald ``status='done'`` und ``result`` nicht
leer ist. Verdict/Outcome/Cost/Profile werden vom JEWEILS LETZTEN
``task_runs``-Eintrag des Tasks übernommen (per-Task-Denormalisierung,
ein einziger JOIN) — der letzte Run trägt oft kein Verdict mehr (die
finale "Operator/Coder"-Zeile schließt nur ab), das ist reales Verhalten,
kein Bug.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PREVIEW_CHARS = 280
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_DELIVERABLE_MAX_PER_TASK = 3

_LIST_BASE_SQL = """
    FROM tasks t
    LEFT JOIN task_runs lr ON lr.id = (
        SELECT id FROM task_runs WHERE task_id = t.id ORDER BY id DESC LIMIT 1
    )
    WHERE t.status = 'done' AND t.result IS NOT NULL AND t.result != ''
"""

_LIST_SELECT_SQL = """
    SELECT t.id AS id, t.title AS title, t.kind AS kind, t.completed_at AS completed_at,
           t.result AS result, lr.profile AS profile, lr.verdict AS verdict,
           lr.outcome AS outcome, lr.cost_usd AS cost_usd,
           (SELECT COUNT(*) FROM task_runs WHERE task_id = t.id) AS run_count
""" + _LIST_BASE_SQL


def _db_path() -> Path:
    from hermes_cli import kanban_db
    return kanban_db.kanban_db_path()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(int(ts), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _truncate_word_boundary(text: str, limit: int) -> str:
    """Flatten whitespace, then cut at ``limit`` chars without splitting a word."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut


def _row_to_item(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    result = row["result"] or ""
    item: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "profile": row["profile"],
        "completed_at": _iso(row["completed_at"]),
        "result_summary": _truncate_word_boundary(result, _PREVIEW_CHARS),
        "verdict": row["verdict"],
        "outcome": row["outcome"],
        "cost_usd": row["cost_usd"],
        "run_count": row["run_count"],
    }
    if full:
        item["result_md"] = result
    return item


def _build_filters(
    *,
    kind: Optional[str],
    profile: Optional[str],
    verdict: Optional[str],
    q: Optional[str],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("t.kind = ?")
        params.append(kind)
    if profile:
        clauses.append("lr.profile = ?")
        params.append(profile)
    if verdict:
        clauses.append("lr.verdict = ?")
        params.append(verdict)
    if q:
        clauses.append("(t.title LIKE ? OR t.result LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle])
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    return extra, params


def _query_results(
    *,
    kind: Optional[str],
    profile: Optional[str],
    verdict: Optional[str],
    q: Optional[str],
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    extra, params = _build_filters(kind=kind, profile=profile, verdict=verdict, q=q)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) {_LIST_BASE_SQL}{extra}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"{_LIST_SELECT_SQL}{extra} ORDER BY t.completed_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return rows, int(total)
    finally:
        conn.close()


def _get_result_item(task_id: str) -> Optional[dict[str, Any]]:
    if not _TASK_ID_RE.match(task_id):
        raise ValueError("invalid task id")
    conn = _connect()
    try:
        row = conn.execute(
            f"{_LIST_SELECT_SQL} AND t.id = ?",
            [task_id],
        ).fetchone()
        if row is None:
            return None
        runs = conn.execute(
            "SELECT started_at, outcome, verdict, cost_usd, input_tokens, "
            "output_tokens, summary FROM task_runs WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        artifacts = _artifacts_for_task(conn, task_id, row["title"])
    finally:
        conn.close()
    item = _row_to_item(row, full=True)
    item["runs"] = [
        {
            "started": _iso(r["started_at"]),
            "outcome": r["outcome"],
            "verdict": r["verdict"],
            "cost_usd": r["cost_usd"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "summary": r["summary"],
        }
        for r in runs
    ]
    item["artifacts"] = artifacts
    return item


# ---------------------------------------------------------------------------
# Artefakt-Links (deep-link auf den Lesesaal-Deliverable-Adapter,
# library_view.py). Läuft off der eigenen read-only Verbindung/Filesystem
# statt library_view._collect_deliverable_items() zu reimportieren, damit
# kein Schreib-fähiger kanban_db.connect() ins Spiel kommt.
# ---------------------------------------------------------------------------

def _reports_dir_for_task(task_id: str) -> Path:
    from hermes_cli import kanban_db
    return kanban_db.kanban_home() / "reports" / "by-task" / task_id


def _validated_artifact_path(art_path: str, *, vault_root: Path) -> Optional[Path]:
    p = Path(art_path).expanduser()
    if not p.is_absolute() or p.suffix != ".md":
        return None
    try:
        p_resolved = p.resolve()
        p_resolved.relative_to(vault_root)
    except (OSError, ValueError):
        return None
    return p_resolved


def _artifacts_for_task(
    conn: sqlite3.Connection, task_id: str, task_title: str
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    task_dir = _reports_dir_for_task(task_id)
    if task_dir.is_dir():
        md_files = sorted(
            task_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True,
        )[:_DELIVERABLE_MAX_PER_TASK]
        for md_file in md_files:
            rel = md_file.relative_to(task_dir).as_posix()
            artifacts.append({
                "title": task_title if rel == "RESULT.md" else f"{task_title} · {rel}",
                "id": f"deliverable::{task_id}::{rel}",
                "category": "arbeit",
            })
    seen = {a["id"] for a in artifacts}
    vault_root = (Path.home() / "vault").resolve()
    rows = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL "
        "AND metadata != '' AND metadata LIKE '%artifacts%' ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        if len(artifacts) >= _DELIVERABLE_MAX_PER_TASK * 2:
            break
        try:
            md = json.loads(row["metadata"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        arts = md.get("artifacts", []) if isinstance(md, dict) else []
        if not isinstance(arts, list):
            continue
        for art_path in arts:
            if not isinstance(art_path, str):
                continue
            p_resolved = _validated_artifact_path(art_path, vault_root=vault_root)
            if p_resolved is None or not p_resolved.is_file():
                continue
            aid = f"deliverable::{task_id}::{p_resolved.name}"
            if aid in seen:
                continue
            seen.add(aid)
            artifacts.append({
                "title": f"{task_title} - {p_resolved.name}",
                "id": aid,
                "category": "arbeit",
            })
    return artifacts


# ---------------------------------------------------------------------------
# LLM-Digest (format=md) — stabile, HTML-freie Markdown-Ausgabe.
# ---------------------------------------------------------------------------

def _render_md_digest(rows: list[sqlite3.Row], conn: sqlite3.Connection) -> str:
    blocks: list[str] = []
    for row in rows:
        item = _row_to_item(row, full=True)
        cost = item["cost_usd"]
        cost_display = f"${cost:.4f}" if isinstance(cost, (int, float)) else "-"
        meta_line = " · ".join([
            item["kind"] or "-",
            item["profile"] or "-",
            item["completed_at"] or "-",
            item["verdict"] or "-",
            cost_display,
        ])
        artifacts = _artifacts_for_task(conn, item["id"], item["title"])
        block = f"## {item['title']} — {item['id']}\n{meta_line}\n\n{item['result_md']}"
        if artifacts:
            links = "\n".join(f"- [{a['title']}]({a['id']})" for a in artifacts)
            block += f"\n\nArtefakte:\n{links}"
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def _render_md_for_query(
    *,
    kind: Optional[str],
    profile: Optional[str],
    verdict: Optional[str],
    q: Optional[str],
    limit: int,
    offset: int,
) -> str:
    extra, params = _build_filters(kind=kind, profile=profile, verdict=verdict, q=q)
    conn = _connect()
    try:
        rows = conn.execute(
            f"{_LIST_SELECT_SQL}{extra} ORDER BY t.completed_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return _render_md_digest(rows, conn)
    finally:
        conn.close()


def register_library_results_routes(app: Any) -> None:
    """Ergebnisse-Routen. Unter /api/ → Session-Gate der Middleware;
    bewusst NIE in PUBLIC_API_PATHS."""
    from fastapi import HTTPException, Query
    from fastapi.responses import PlainTextResponse

    @app.get("/api/library/results")
    async def library_results(  # type: ignore[unused-variable]
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        kind: Optional[str] = Query(None, max_length=50),
        profile: Optional[str] = Query(None, max_length=50),
        verdict: Optional[str] = Query(None, max_length=50),
        q: Optional[str] = Query(None, max_length=200),
        format: Optional[str] = Query(None, max_length=10),  # noqa: A002
    ):
        if format == "md":
            md = await asyncio.to_thread(
                _render_md_for_query,
                kind=kind, profile=profile, verdict=verdict, q=q,
                limit=limit, offset=offset,
            )
            return PlainTextResponse(md, media_type="text/markdown")
        rows, total = await asyncio.to_thread(
            _query_results,
            kind=kind, profile=profile, verdict=verdict, q=q,
            limit=limit, offset=offset,
        )
        return {
            "items": [_row_to_item(r, full=False) for r in rows],
            "total": total,
        }

    @app.get("/api/library/results/item")
    async def library_results_item(  # type: ignore[unused-variable]
        id: str = Query(..., max_length=100),  # noqa: A002
    ):
        try:
            item = await asyncio.to_thread(_get_result_item, id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if item is None:
            raise HTTPException(status_code=404, detail="result not found")
        return item

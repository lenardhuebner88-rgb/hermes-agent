"""Demand-Funnel-Helfer: Cap, Dedupe-Keys, Alter und Auto-Archiv.

Vorschläge aus den Funnel-Quellen (HermesBar/Familie, Discord ``idee:``,
fo-gap-audit) sind normale Kanban-Tasks in ``triage`` mit einem der
:data:`hermes_cli.kanban_db.FUNNEL_CREATED_BY`-Autoren. Dieses Modul bündelt
die Steuer-Logik, die laut Planspec in den **Code** gehört (nie nur in
Prompts): Cap 15 offene Vorschläge, Auto-Archiv nach 30 Tagen, stabile
Dedupe-Keys. Die fo-brain Cron-Scripts (fo-gap-audit, Sonntags-Digest im
Morgenbrief) importieren es auf dem venv-Python.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import List, Optional

from hermes_cli import kanban_db as kb

FUNNEL_CAP = 15
MAX_AGE_DAYS = 30

_KEY_LIMIT = 120


def wish_key(text: str) -> str:
    """Stabiler Dedupe-Key: ``wish:`` + lowercase, Whitespace kollabiert."""
    norm = re.sub(r"\s+", " ", text or "").strip().lower()[:_KEY_LIMIT]
    return f"wish:{norm}"


def open_proposals(conn: sqlite3.Connection) -> List[dict]:
    """Offene Funnel-Vorschläge (status=triage), älteste zuerst."""
    placeholders = ",".join("?" for _ in kb.FUNNEL_CREATED_BY)
    rows = conn.execute(
        "SELECT id, title, created_by, created_at FROM tasks "
        f"WHERE status = 'triage' AND created_by IN ({placeholders}) "
        "ORDER BY created_at ASC",
        kb.FUNNEL_CREATED_BY,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def cap_reached(conn: sqlite3.Connection, *, cap: int = FUNNEL_CAP) -> bool:
    return len(open_proposals(conn)) >= cap


def stale_proposals(
    conn: sqlite3.Connection,
    *,
    max_age_days: int = MAX_AGE_DAYS,
    now: Optional[int] = None,
) -> List[dict]:
    """Offene Vorschläge, die älter als ``max_age_days`` sind."""
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max_age_days * 86400
    return [
        p for p in open_proposals(conn)
        if p["created_at"] is not None and int(p["created_at"]) <= cutoff
    ]


def archive_stale(
    conn: sqlite3.Connection,
    *,
    max_age_days: int = MAX_AGE_DAYS,
    now: Optional[int] = None,
) -> List[dict]:
    """Auto-Archiv: alte offene Vorschläge archivieren, archivierte zurückgeben."""
    archived: List[dict] = []
    for p in stale_proposals(conn, max_age_days=max_age_days, now=now):
        if kb.archive_task(conn, p["id"]):
            archived.append(p)
    return archived


# --- Freigabe-Pfad: fertiger Draft → Build-Task (Operator-Klick) -----------

DRAFT_WINDOW_DAYS = 30
_DRAFT_EXCERPT_MIN = 120
_DRAFT_EXCERPT_MAX = 1500
_BUILD_TITLE_LIMIT = 80

APPROVE_BODY_TEMPLATE = (
    "Freigegebener Funnel-Draft — jetzt umsetzen.\n\n"
    "Ursprünglicher Wunsch: „{title}“ ({task_id}, Quelle: {created_by}).\n\n"
    "Draft (letzter Stand aus dem Ursprungs-Task):\n{excerpt}\n\n"
    "Anweisungen: Setze GENAU den freigegebenen Draft um — Referenzen stehen "
    "im Ursprungs-Task (Kommentare) bzw. im dort genannten Backlog-Item. "
    "Gates der Ziel-Lane fahren (Tests/Build); Push/Deploy nur bei grün gemäß "
    "Lane-Governance. Bei unklarem Scope: blocken statt raten."
)


def _draft_excerpt(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Jüngster substanzieller Kommentar (= der Draft) eines Funnel-Tasks."""
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for r in rows:
        body = (r["body"] or "").strip()
        if len(body) >= _DRAFT_EXCERPT_MIN and not body.startswith("BLOCKED:"):
            return body[:_DRAFT_EXCERPT_MAX]
    return None


def list_drafts(
    conn: sqlite3.Connection,
    *,
    days: int = DRAFT_WINDOW_DAYS,
    now: Optional[int] = None,
) -> List[dict]:
    """Fertige Funnel-Roots ohne Build-Kind — sie warten auf die Freigabe.

    Nach der Freigabe hat der Root ein verlinktes Build-Kind und fällt aus
    dieser Liste; die Kette übernimmt das Flow-Board.
    """
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max(1, int(days)) * 86400
    placeholders = ",".join("?" for _ in kb.FUNNEL_CREATED_BY)
    rows = conn.execute(
        "SELECT id, title, created_by, assignee, completed_at FROM tasks "
        f"WHERE created_by IN ({placeholders}) AND status = 'done' "
        "AND completed_at IS NOT NULL AND completed_at >= ? "
        "AND id NOT IN (SELECT DISTINCT parent_id FROM task_links) "
        "ORDER BY completed_at DESC",
        (*kb.FUNNEL_CREATED_BY, cutoff),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_by": r["created_by"],
            "assignee": r["assignee"],
            "completed_at": r["completed_at"],
            "draft_excerpt": _draft_excerpt(conn, r["id"]),
        }
        for r in rows
    ]


def approve_draft(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    assignee_fallback: str = "coder-claude",
) -> str:
    """Freigabe: Build-Task als verlinktes Kind des Draft-Roots anlegen.

    Das Kind erbt ``created_by`` (die Wert-Bilanz zählt die Kette einmal als
    nutzer — der Root wird interior, das Kind der neue Sink) und startet als
    ``ready`` (Parent ist done) — der Dispatcher übernimmt. Raises ValueError
    mit Begründung, wenn der Task kein freigabefähiger Draft ist.
    """
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} nicht gefunden")
    if (task.created_by or "") not in kb.FUNNEL_CREATED_BY:
        raise ValueError(f"{task_id} ist kein Funnel-Vorschlag (created_by={task.created_by!r})")
    if task.status != "done":
        raise ValueError(f"{task_id} ist nicht fertig (status={task.status}) — erst der fertige Draft wird freigegeben")
    has_child = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? LIMIT 1", (task_id,),
    ).fetchone()
    if has_child:
        raise ValueError(f"{task_id} wurde bereits freigegeben (Build-Kind existiert)")

    title = f"Umsetzen: {task.title}"
    if len(title) > _BUILD_TITLE_LIMIT:
        title = title[: _BUILD_TITLE_LIMIT - 1].rstrip() + "…"
    excerpt = _draft_excerpt(conn, task_id) or (
        "(kein Draft-Kommentar gefunden — Referenzen im Ursprungs-Task prüfen)"
    )
    return kb.create_task(
        conn,
        title=title,
        body=APPROVE_BODY_TEMPLATE.format(
            title=task.title, task_id=task_id,
            created_by=task.created_by, excerpt=excerpt,
        ),
        created_by=task.created_by,
        assignee=task.assignee or assignee_fallback,
        parents=(task_id,),
    )


def dismiss_draft(conn: sqlite3.Connection, task_id: str) -> None:
    """Verwerfen: Draft-Root archivieren (Operator-Entscheid, kein Build).

    Gleiche Gültigkeitsregeln wie :func:`approve_draft` — nur Einträge der
    Freigabe-Queue (fertiger Funnel-Root ohne Build-Kind) sind verwerfbar.
    """
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} nicht gefunden")
    if (task.created_by or "") not in kb.FUNNEL_CREATED_BY:
        raise ValueError(f"{task_id} ist kein Funnel-Vorschlag (created_by={task.created_by!r})")
    if task.status != "done":
        raise ValueError(f"{task_id} ist nicht in der Freigabe-Queue (status={task.status})")
    has_child = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? LIMIT 1", (task_id,),
    ).fetchone()
    if has_child:
        raise ValueError(f"{task_id} wurde bereits freigegeben (Build-Kind existiert)")
    kb.add_comment(conn, task_id, "operator",
                   "Verworfen über die Funnel-Freigabe-Queue — kein Build.")
    kb.archive_task(conn, task_id)


def create_wish(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    created_by: str,
    key: Optional[str] = None,
    cap: int = FUNNEL_CAP,
    assignee: Optional[str] = None,
) -> Optional[str]:
    """Funnel-Vorschlag anlegen (triage, dedupe). None, wenn das Cap greift.

    Der Cap-Guard sitzt hier im Code — ein voller Trichter lehnt neue
    Vorschläge ab, statt das Board zu fluten.
    """
    if created_by not in kb.FUNNEL_CREATED_BY:
        raise ValueError(f"created_by must be one of {kb.FUNNEL_CREATED_BY}")
    if cap_reached(conn, cap=cap):
        return None
    return kb.create_task(
        conn,
        title=title,
        body=body,
        created_by=created_by,
        assignee=assignee,
        triage=True,
        idempotency_key=key or wish_key(title),
    )

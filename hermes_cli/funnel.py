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


def create_wish(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    created_by: str,
    key: Optional[str] = None,
    cap: int = FUNNEL_CAP,
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
        triage=True,
        idempotency_key=key or wish_key(title),
    )

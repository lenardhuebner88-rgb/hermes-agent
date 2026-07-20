"""Persistent one-shot reminders for the Jarvis personal assistant."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from secrets import token_hex
from typing import Any

from hermes_cli.pa_chat import PAStore


def _utc_iso(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Reminder-Zeit braucht eine Zeitzone")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def create_reminder(
    *,
    due_at_utc: str,
    title: str,
    body: str = "",
    store: PAStore | None = None,
) -> str:
    """Create one pending reminder and return its opaque id."""
    reminder_store = store or PAStore()
    reminder_store.ensure_schema()
    reminder_id = f"rem_{token_hex(12)}"
    created_at = _utc_iso(datetime.now(tz=timezone.utc))
    with reminder_store.connect() as conn:
        conn.execute(
            "INSERT INTO reminders(id, due_at_utc, title, body, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (reminder_id, _utc_iso(due_at_utc), title, body, created_at),
        )
    return reminder_id


def due_reminders(
    now_utc: datetime | str,
    *,
    store: PAStore | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return pending reminders due at or before ``now_utc``."""
    reminder_store = store or PAStore()
    if conn is None:
        reminder_store.ensure_schema()
    query = (
        "SELECT id, due_at_utc, title, body, status, created_at, fired_at "
        "FROM reminders WHERE status='pending' AND due_at_utc <= ? "
        "ORDER BY due_at_utc, id"
    )
    params = (_utc_iso(now_utc),)
    if conn is not None:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    with reminder_store.connect() as own_conn:
        return [dict(row) for row in own_conn.execute(query, params).fetchall()]


def mark_fired(
    reminder_id: str,
    fired_at: datetime | str,
    *,
    store: PAStore | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Mark one pending reminder fired; return whether this call won."""
    reminder_store = store or PAStore()
    if conn is None:
        reminder_store.ensure_schema()
    params = (_utc_iso(fired_at), reminder_id)
    query = (
        "UPDATE reminders SET status='fired', fired_at=? "
        "WHERE id=? AND status='pending'"
    )
    if conn is not None:
        return int(conn.execute(query, params).rowcount or 0) == 1
    with reminder_store.connect() as own_conn:
        return int(own_conn.execute(query, params).rowcount or 0) == 1

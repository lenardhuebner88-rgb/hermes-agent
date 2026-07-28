"""Shared status policy for bounded chain-fixer cards.

This fork-owned module keeps fixer lifecycle semantics out of the upstream-
touched Kanban modules.  Imports of ``kanban_db`` stay inside functions so the
task-state and worktree modules can import these helpers without a cycle.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

_log = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"done", "archived", "failed", "cancelled"})
CONFLICT_FIXER_FAILED_EVENT = "conflict_fixer_failed"


def _payload_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def blocked_event_kind(raw_payload: object, *, fallback: object = None) -> object:
    """Read a structured block kind without making legacy payloads fatal."""
    return _payload_dict(raw_payload).get("kind") or fallback


def is_settled_fixer_card(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether a bounded fixer card cannot make further progress."""
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_lane_fixer import is_lane_scope_fixer_task

    if not (
        kb._is_conflict_fixer_task(conn, task_id)
        or is_lane_scope_fixer_task(conn, task_id)
    ):
        return False

    status = str(row["status"] or "")
    if status in _TERMINAL_STATUSES:
        return True
    if status != "blocked":
        return False

    event_ids = {
        event["kind"]: int(event["id"])
        for event in conn.execute(
            "SELECT kind, MAX(id) AS id FROM task_events "
            "WHERE task_id = ? AND kind IN ('claimed', 'gave_up') "
            "GROUP BY kind",
            (task_id,),
        ).fetchall()
    }
    claimed_id = event_ids.get("claimed")
    gave_up_id = event_ids.get("gave_up")
    return (
        claimed_id is not None
        and gave_up_id is not None
        and gave_up_id > claimed_id
    )


def _matching_conflict_fixer_attempts(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    conflict_fingerprint: str,
) -> int:
    from hermes_cli import kanban_db as kb

    child_ids: set[str] = set()
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE kind = ? AND json_extract(payload, '$.root_id') = ?",
        (kb.CONFLICT_FIXER_DISPATCHED_EVENT, root_id),
    ).fetchall()
    for row in rows:
        payload = _payload_dict(row["payload"])
        if kb._conflict_event_fingerprint(payload) != conflict_fingerprint:
            continue
        child_id = str(payload.get("child_id") or "").strip()
        if child_id:
            child_ids.add(child_id)
    return len(child_ids)


def _failure_event_exists(
    conn: sqlite3.Connection,
    *,
    parent_id: str,
    conflict_fingerprint: str,
    attempt: int,
) -> bool:
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC",
        (parent_id, CONFLICT_FIXER_FAILED_EVENT),
    ).fetchall()
    for row in rows:
        payload = _payload_dict(row["payload"])
        try:
            payload_attempt = int(payload.get("attempt"))
        except (TypeError, ValueError):
            continue
        if (
            str(payload.get("parent_id") or "") == parent_id
            and str(payload.get("conflict_fingerprint") or "")
            == conflict_fingerprint
            and payload_attempt == attempt
        ):
            return True
    return False


def on_fixer_card_failed(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    blocked: bool,
) -> bool:
    """Publish one chain-directed failure signal for a conflict fixer."""
    from hermes_cli import kanban_db as kb

    try:
        if not kb._is_conflict_fixer_task(conn, task_id):
            return False
        marker = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'conflict_fixer_for' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if marker is None:
            return False
        marker_payload = _payload_dict(marker["payload"])
        parent_id = str(marker_payload.get("parent_id") or "").strip()
        root_id = str(marker_payload.get("root_id") or "").strip()
        conflict_fingerprint = str(
            marker_payload.get("conflict_fingerprint") or ""
        ).strip()
        try:
            attempt = int(marker_payload.get("attempt"))
        except (TypeError, ValueError):
            return False
        if not parent_id or not root_id or not conflict_fingerprint or attempt < 1:
            return False

        payload = {
            "child_id": task_id,
            "parent_id": parent_id,
            "root_id": root_id,
            "attempt": attempt,
            "outcome": outcome,
            "blocked": bool(blocked),
            "conflict_fingerprint": conflict_fingerprint,
        }
        wrote_event = False
        with kb.write_txn(conn):
            if not _failure_event_exists(
                conn,
                parent_id=parent_id,
                conflict_fingerprint=conflict_fingerprint,
                attempt=attempt,
            ):
                kb._append_event(
                    conn,
                    task_id,
                    CONFLICT_FIXER_FAILED_EVENT,
                    payload,
                )
                kb._append_event(
                    conn,
                    parent_id,
                    CONFLICT_FIXER_FAILED_EVENT,
                    payload,
                )
                wrote_event = True

        attempts = _matching_conflict_fixer_attempts(
            conn,
            root_id=root_id,
            conflict_fingerprint=conflict_fingerprint,
        )
        escalated = False
        if attempts >= kb.CONFLICT_FIXER_MAX_ATTEMPTS:
            parent = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if parent is not None:
                escalated = kb._park_stall_once(
                    conn,
                    parent,
                    stall_class=kb.INTEGRATION_PARKED_STALL_CLASS,
                    reason="conflict fixer budget exhausted",
                    evidence={
                        "attempts": attempts,
                        "fixer_exhausted": True,
                        "via": "fixer_failure",
                    },
                    now=int(time.time()),
                )
        return wrote_event or escalated
    except Exception:
        _log.warning(
            "could not publish conflict-fixer failure for %s",
            task_id,
            exc_info=True,
        )
        return False


def is_integration_park(*, reason: object, block_kind: object) -> bool:
    """Recognize current structured parks and legacy reason-only parks."""
    return block_kind == "integration" or str(reason or "").startswith(
        "integration parked:"
    )

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
LANE_SCOPE_FIXER_FAILED_EVENT = "lane_scope_fixer_failed"
FIXER_FAILURE_PUBLISH_FAILED_EVENT = "fixer_failure_publish_failed"


def _payload_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def blocked_event_kind(raw_payload: object, *, fallback: object = None) -> object:
    """Read the block origin from its event without making legacy payloads fatal.

    ``tasks.block_kind`` can be rewritten by a later stall sweep without a new
    ``blocked`` event.  It is not evidence of this park episode's origin and
    therefore must not override an untyped, legacy event.
    """
    kind = _payload_dict(raw_payload).get("kind")
    # ``block_task(..., kind=None)`` synthesizes ``needs_input`` or
    # ``transient`` for legacy reason-only parks. Those are generic retry/UI
    # states rather than typed park origins, so preserve the reason fallback.
    return None if kind in {"needs_input", "transient"} else kind


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
    # A blocked fixer can no longer make progress, independently of why it was
    # blocked. Its operator hold remains untouched; this only releases the
    # one-at-a-time fixer guard so a separately routed replacement may proceed.
    return status == "blocked"


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


def _lane_failure_event_exists(
    conn: sqlite3.Connection,
    *,
    parent_id: str,
    fingerprint: str,
    attempt: int,
) -> bool:
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC",
        (parent_id, LANE_SCOPE_FIXER_FAILED_EVENT),
    ).fetchall()
    for row in rows:
        payload = _payload_dict(row["payload"])
        try:
            payload_attempt = int(payload.get("attempt"))
        except (TypeError, ValueError):
            continue
        if (
            str(payload.get("parent_id") or "") == parent_id
            and str(payload.get("fingerprint") or "") == fingerprint
            and payload_attempt == attempt
        ):
            return True
    return False


def _record_failure_publish_error(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    exc: Exception,
) -> bool:
    """Leave a durable receipt when a fixer wake path itself fails."""
    from hermes_cli import kanban_db as kb

    payload = {
        "task_id": task_id,
        "source": "on_fixer_card_failed",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, FIXER_FAILURE_PUBLISH_FAILED_EVENT, payload)
    return True


def _on_lane_scope_fixer_failed(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    blocked: bool,
) -> bool:
    """Record and wake the bounded replacement path for a lane-scope fixer."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_lane_fixer import (
        LANE_FIXER_FOR_EVENT,
        maybe_route_lane_scope_fixer,
    )

    marker = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, LANE_FIXER_FOR_EVENT),
    ).fetchone()
    if marker is None:
        return False
    marker_payload = _payload_dict(marker["payload"])
    parent_id = str(marker_payload.get("parent_id") or "").strip()
    fingerprint = str(marker_payload.get("fingerprint") or "").strip()
    expected_lane = str(marker_payload.get("expected_lane") or "").strip()
    paths = marker_payload.get("violating_paths")
    violating_paths = [str(path) for path in paths] if isinstance(paths, list) else []
    try:
        attempt = int(marker_payload.get("attempt"))
    except (TypeError, ValueError):
        return False
    if not parent_id or not fingerprint or not expected_lane or attempt < 1:
        return False

    payload = {
        "child_id": task_id,
        "parent_id": parent_id,
        "fingerprint": fingerprint,
        "attempt": attempt,
        "outcome": outcome,
        "blocked": bool(blocked),
    }
    wrote_event = False
    with kb.write_txn(conn):
        if not _lane_failure_event_exists(
            conn,
            parent_id=parent_id,
            fingerprint=fingerprint,
            attempt=attempt,
        ):
            kb._append_event(conn, task_id, LANE_SCOPE_FIXER_FAILED_EVENT, payload)
            kb._append_event(conn, parent_id, LANE_SCOPE_FIXER_FAILED_EVENT, payload)
            wrote_event = True

    parent = conn.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone()
    replacement_id = None
    if parent is not None:
        replacement_id = maybe_route_lane_scope_fixer(
            conn,
            parent,
            violating_paths=violating_paths,
            expected_lane=expected_lane,
        )
    return wrote_event or replacement_id is not None


def on_fixer_card_failed(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    blocked: bool,
) -> bool:
    """Publish and wake bounded fixer failure paths without swallowing errors."""
    from hermes_cli import kanban_db as kb

    try:
        from hermes_cli.kanban_lane_fixer import is_lane_scope_fixer_task

        if is_lane_scope_fixer_task(conn, task_id):
            return _on_lane_scope_fixer_failed(
                conn,
                task_id,
                outcome=outcome,
                blocked=blocked,
            )
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
    except Exception as exc:
        try:
            return _record_failure_publish_error(conn, task_id=task_id, exc=exc)
        except Exception:
            _log.exception(
                "could not record fixer wake-path failure for %s", task_id
            )
            raise


def is_integration_park(*, reason: object, block_kind: object) -> bool:
    """Recognize typed integration parks and legacy reason-only parks.

    A typed origin on a blocked event is authoritative. Only untyped legacy
    events fall back to their reason prefix.
    """
    if isinstance(block_kind, str) and block_kind:
        return block_kind == "integration"
    return str(reason or "").startswith("integration parked:")

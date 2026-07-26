"""Bounded handoff for lane-scope parks.

A lane-scope gate must keep its attribution verdict intact, but the corrective
work belongs to the lane that owns the violated paths.  This module mirrors the
conflict-park fixer policy without adding fork logic to upstream-owned
``kanban_db.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt

_log = logging.getLogger(__name__)

LANE_FIXER_IDEM_PREFIX = "lane-scope-fixer:"
LANE_FIXER_DISPATCHED_EVENT = "lane_scope_fixer_dispatched"
LANE_FIXER_FOR_EVENT = "lane_scope_fixer_for"


def _lane_fingerprint(violating_paths: Iterable[str], expected_lane: str) -> str:
    canonical = {
        "expected_lane": str(expected_lane),
        "violating_paths": sorted({str(path) for path in violating_paths if str(path)}),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _event_payloads(
    conn: sqlite3.Connection, parent_id: str, fingerprint: str
) -> list[tuple[int, dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT created_at, payload FROM task_events
        WHERE task_id = ? AND kind = ?
        ORDER BY id ASC
        """,
        (parent_id, LANE_FIXER_DISPATCHED_EVENT),
    ).fetchall()
    matched: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("fingerprint") == fingerprint:
            matched.append((int(row["created_at"] or 0), payload))
    return matched


def _is_open_task(conn: sqlite3.Connection, task_id: object) -> bool:
    if not task_id:
        return False
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
    return row is not None and row["status"] not in {"done", "archived", "failed", "cancelled"}


def is_lane_scope_fixer_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether *task_id* is a lane fixer, based on its idempotency key."""
    row = conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return bool(row and str(row["idempotency_key"] or "").startswith(LANE_FIXER_IDEM_PREFIX))


def _fixer_body(
    *,
    parent_id: str,
    branch: str,
    violating_paths: list[str],
    expected_lane: str,
    attempt: int,
) -> str:
    paths = "\n".join(f"- `{path}`" for path in violating_paths)
    return (
        f"## Lane-scope fixer for `{parent_id}`\n\n"
        f"The parent task was parked because its attributed diff includes paths owned by "
        f"lane `{expected_lane}`. Work on the existing chain branch `{branch}`.\n\n"
        "The work is **already committed** on this branch. Take it over and complete it "
        "in the correct lane; do not rebuild it and do not revert it.\n\n"
        f"Violating paths (verbatim):\n{paths}\n\n"
        f"Attempt {attempt}/{kb.CONFLICT_FIXER_MAX_ATTEMPTS}."
    )


def _escalate_exhausted(
    conn: sqlite3.Connection,
    parent: sqlite3.Row,
    *,
    fingerprint: str,
    attempts: int,
    now: int,
) -> None:
    escalated = kb._park_stall_once(
        conn,
        parent,
        stall_class=kb.INTEGRATION_PARKED_STALL_CLASS,
        reason="lane-scope fixer budget exhausted",
        evidence={
            "route": "lane_scope_fixer",
            "fingerprint": fingerprint,
            "attempts": attempts,
            "limit": kb.CONFLICT_FIXER_MAX_ATTEMPTS,
        },
        now=now,
    )
    if not escalated and not kb._has_operator_escalation(conn, parent["id"]):
        # The lane gate calls us before its caller performs the final blocked
        # transition.  Preserve the conflict-fixer escalation semantics even
        # in that short running/ready window.
        kb._emit_operator_escalation(
            conn,
            str(parent["id"]),
            None,
            "lane_scope_fixer_budget_exhausted",
            "lane-scope fixer budget exhausted",
        )


def maybe_route_lane_scope_fixer(
    conn: sqlite3.Connection,
    parent: sqlite3.Row,
    *,
    violating_paths: Iterable[str],
    expected_lane: str,
    now: Optional[int] = None,
) -> Optional[str]:
    """Create one budgeted fixer for a lane-scope park, or return ``None``.

    Routing is fail-soft: callers intentionally ignore this return value so a
    failure here can never prevent the original lane-scope park.
    """
    parent_id = str(parent["id"])
    if is_lane_scope_fixer_task(conn, parent_id):
        return None

    paths = sorted({str(path) for path in violating_paths if str(path)})
    if not paths or not expected_lane:
        return None
    now = int(time.time()) if now is None else int(now)
    fingerprint = _lane_fingerprint(paths, expected_lane)
    dispatched = _event_payloads(conn, parent_id, fingerprint)
    if any(_is_open_task(conn, payload.get("child_id")) for _, payload in dispatched):
        return None

    attempts = len(dispatched)
    if attempts >= kb.CONFLICT_FIXER_MAX_ATTEMPTS:
        _escalate_exhausted(
            conn, parent, fingerprint=fingerprint, attempts=attempts, now=now
        )
        return None
    if dispatched and now - dispatched[-1][0] < kb.CONFLICT_FIXER_BACKOFF_SECONDS:
        return None

    provisioned = kwt.split_provisioned_path(parent["workspace_path"])
    if provisioned is None:
        return None
    _, root_id, _ = provisioned
    branch = kwt.chain_branch(root_id)
    attempt = attempts + 1
    idempotency_key = f"{LANE_FIXER_IDEM_PREFIX}{parent_id}:{fingerprint}:{attempt}"
    try:
        child_id = kb.create_task(
            conn,
            title=f"Lane-scope fixer for {parent_id}",
            body=_fixer_body(
                parent_id=parent_id,
                branch=branch,
                violating_paths=paths,
                expected_lane=expected_lane,
                attempt=attempt,
            ),
            assignee=expected_lane,
            created_by="lane-scope-fixer",
            workspace_kind="dir",
            workspace_path=str(parent["workspace_path"]),
            tenant=parent["tenant"],
            priority=int(parent["priority"] or 0),
            idempotency_key=idempotency_key,
            max_runtime_seconds=kb.CONFLICT_FIXER_MAX_RUNTIME_SECONDS,
            max_retries=1,
            kind="code",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                LANE_FIXER_DISPATCHED_EVENT,
                {
                    "parent_id": parent_id,
                    "child_id": child_id,
                    "fingerprint": fingerprint,
                    "attempt": attempt,
                    "limit": kb.CONFLICT_FIXER_MAX_ATTEMPTS,
                    "expected_lane": expected_lane,
                    "violating_paths": paths,
                },
            )
            kb._append_event(
                conn,
                child_id,
                LANE_FIXER_FOR_EVENT,
                {
                    "parent_id": parent_id,
                    "fingerprint": fingerprint,
                    "attempt": attempt,
                },
            )
    except Exception:
        _log.warning("lane-scope fixer creation failed for %s", parent_id, exc_info=True)
        return None
    return child_id

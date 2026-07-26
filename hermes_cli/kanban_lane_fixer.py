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
LANE_FIXER_PARENT_RESUMED_EVENT = "lane_scope_fixer_parent_resumed"


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


def allowlisted_paths_for_parent(
    conn: sqlite3.Connection, parent_id: str
) -> set[str]:
    """Paths a completed lane-scope fixer child already owns for *parent_id*.

    Used by ``_enforce_lane_scope_on_complete`` so a resumed parent is not
    re-parked for the same paths the fixer was dispatched to take over.
    """
    rows = conn.execute(
        """
        SELECT payload FROM task_events
        WHERE task_id = ? AND kind = ?
        ORDER BY id ASC
        """,
        (parent_id, LANE_FIXER_DISPATCHED_EVENT),
    ).fetchall()
    allow: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        child_id = str(payload.get("child_id") or "").strip()
        if not child_id:
            continue
        child = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        if child is None or child["status"] != "done":
            continue
        for path in payload.get("violating_paths") or []:
            text = str(path).strip()
            if text:
                allow.add(text)
    return allow


def resume_parent_for_completed_lane_fixer(
    conn: sqlite3.Connection,
    child_id: str,
) -> bool:
    """CAS-resume the lane-scope park this successful fixer still owns.

    Shape mirrors ``kanban_db._resume_parent_for_completed_conflict_fixer`` but
    lives here (fork-owned) and matches on ``lane_scope_fixer_for`` events.
    """
    with kb.write_txn(conn):
        marker = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = ? ORDER BY id DESC LIMIT 1",
            (child_id, LANE_FIXER_FOR_EVENT),
        ).fetchone()
        if marker is None:
            return False
        try:
            payload = json.loads(marker["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        parent_id = str(payload.get("parent_id") or "").strip()
        expected_fingerprint = str(payload.get("fingerprint") or "").strip()
        if not parent_id or not expected_fingerprint:
            return False

        parent = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
        current_reason = ""
        if blocked is not None:
            try:
                blocked_payload = json.loads(blocked["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                blocked_payload = {}
            if isinstance(blocked_payload, dict):
                current_reason = str(blocked_payload.get("reason") or "")
        if (
            parent is None
            or parent["status"] != "blocked"
            or not current_reason.startswith("integration parked:")
            or "lane-scope" not in current_reason.lower()
        ):
            return False

        # Fingerprint still matches the dispatch that created this fixer.
        dispatched = conn.execute(
            """
            SELECT payload FROM task_events
            WHERE task_id = ? AND kind = ?
            ORDER BY id DESC
            """,
            (parent_id, LANE_FIXER_DISPATCHED_EVENT),
        ).fetchall()
        fingerprint_ok = False
        for row in dispatched:
            try:
                d_payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                str(d_payload.get("child_id") or "") == child_id
                and str(d_payload.get("fingerprint") or "") == expected_fingerprint
            ):
                fingerprint_ok = True
                break
        if not fingerprint_ok:
            return False

        undone_parent = conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (parent_id,),
        ).fetchone()
        new_status = "todo" if undone_parent else "ready"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, block_kind = NULL, "
            "block_recurrences = 0 WHERE id = ? AND status = 'blocked'",
            (new_status, parent_id),
        )
        if cur.rowcount != 1:
            return False
        kb._append_event(
            conn,
            parent_id,
            LANE_FIXER_PARENT_RESUMED_EVENT,
            {
                "child_id": child_id,
                "fingerprint": expected_fingerprint,
                "status": new_status,
            },
        )
        kb._append_event(
            conn,
            parent_id,
            "unblocked",
            {"status": new_status, "source": "lane_scope_fixer_completion"},
        )
        return True


def handle_task_completed(task_id: str, **kwargs: object) -> None:
    """Lifecycle observer: release a parked parent when its lane fixer finishes."""
    board = kwargs.get("board")
    board_slug = str(board) if board else None
    try:
        with kb.connect_closing(board=board_slug) as conn:
            resume_parent_for_completed_lane_fixer(conn, task_id)
    except Exception:
        _log.exception(
            "lane-scope fixer parent resume failed for %s", task_id,
        )


def register_lifecycle_hooks() -> None:
    """Register the lane-scope fixer resume observer (idempotent)."""
    from hermes_cli.plugins import register_hook_once

    register_hook_once("kanban_task_completed", handle_task_completed)


def ensure_lifecycle_hooks_registered() -> None:
    """Best-effort self-registration without editing ``kanban_db``/lifecycle.py.

    ``register_hook_once`` is already idempotent and re-registers after a
    plugin-manager force-reset, so callers may invoke this freely.
    """
    try:
        register_lifecycle_hooks()
    except Exception:
        _log.debug("lane-scope fixer lifecycle registration failed", exc_info=True)


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
    ensure_lifecycle_hooks_registered()
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
                    "expected_lane": expected_lane,
                    "violating_paths": paths,
                },
            )
    except Exception:
        _log.warning("lane-scope fixer creation failed for %s", parent_id, exc_info=True)
        return None
    return child_id

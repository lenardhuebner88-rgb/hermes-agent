"""Fork-owned classification of *how* a worker process ended.

Two questions live here, both of which the upstream-owned call sites used to
answer inline:

1. A worker PID is gone — was it killed *on purpose*? Archiving a task with a
   live worker and a manual reclaim both SIGKILL the process after recording a
   ``reclaim_deferred`` marker on the current run. Without this distinction the
   crash reaper reads the resulting SIGKILL as worker misconduct: the run is
   stamped ``crashed``, a ``protocol_violation`` event is written and the
   failure streak advances — for a termination the board itself requested.
2. A single-query worker finished — which process exit code does it owe the
   dispatcher? A provider quota wall is not a task failure, so it must exit
   with the rate-limit sentinel instead of a generic ``1``; the reap classifier
   then requeues the task without counting a failure.

Fork-owned per ``docs/refactor/UPSTREAM-STRATEGY.md``: the call sites in
``hermes_cli/kanban_db.py`` and ``cli.py`` (both upstream-owned) stay a handful
of delegating lines, so an upstream sync never has to merge this logic.

Restored piece of ``d1a40e0617``, lost with the revert ``88377432aa``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

# Reclaim reasons whose SIGKILL is a board decision, not a worker fault. Both
# are written by the reclaim path in kanban_db.py before it signals the PID.
INTENTIONAL_SIGKILL_REASONS = frozenset(
    {"archive_worker_alive", "manual_reclaim_worker_alive"}
)

# Event kind AND run outcome/status for an intentional external termination.
# Deliberately distinct from ``crashed`` and ``rate_limited`` so board history
# does not show a phantom crash for a kill the operator asked for.
EXTERNAL_TERMINATION_EVENT = "externally_terminated"
EXTERNAL_TERMINATION_OUTCOME = "externally_terminated"

# Chat failures that mean "the account hit a wall", not "the task is broken".
RATE_LIMIT_FAILURE_REASONS = frozenset({"rate_limit", "billing"})


def _payload_dict(raw: Any) -> Optional[dict]:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def intentional_sigkill_marker(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
) -> Optional[dict]:
    """Return the current run's audited archive/reclaim SIGKILL marker.

    Scoped to ``run_id`` on purpose: a marker from an earlier run must not
    excuse a later, genuine crash of the same task.
    """
    if run_id is None:
        return None
    row = conn.execute(
        """SELECT payload FROM task_events
           WHERE task_id=? AND run_id=? AND kind='reclaim_deferred'
           ORDER BY id DESC LIMIT 1""",
        (task_id, run_id),
    ).fetchone()
    if row is None:
        return None
    payload = _payload_dict(row["payload"])
    if payload is None:
        return None
    termination = payload.get("termination")
    if (
        payload.get("reason") not in INTENTIONAL_SIGKILL_REASONS
        or not isinstance(termination, dict)
        or termination.get("sigkill") is not True
    ):
        return None
    return payload


def classify_external_termination(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
    *,
    pid: int,
    claimer: Any,
    exit_kind: str,
    exit_code: Optional[int],
) -> Optional[dict]:
    """Describe an intentional termination, or ``None`` for a real crash.

    The returned mapping carries everything the reaper needs to record the
    run: ``error_text``, ``event_kind`` and ``event_payload``.
    """
    marker = intentional_sigkill_marker(conn, task_id, run_id)
    if marker is None:
        return None
    reason = marker["reason"]
    return {
        "reason": reason,
        "error_text": f"pid {pid} intentionally terminated by {reason}",
        "event_kind": EXTERNAL_TERMINATION_EVENT,
        "event_payload": {
            "pid": pid,
            "claimer": claimer,
            "reason": reason,
            "signal": "SIGKILL",
            "exit_kind": exit_kind,
            "exit_code": exit_code,
            "requested_termination": marker["termination"],
        },
    }


def single_query_exit_code(chat_result: Any) -> int:
    """Process exit code a finished single-query run owes its caller.

    ``0`` when the conversation did not fail. ``1`` for an ordinary failure.
    The kanban rate-limit sentinel only for a worker run (``HERMES_KANBAN_TASK``
    set) that failed on a quota/billing wall — the dispatcher's reap classifier
    maps that code to a requeue that does not count a failure, so a multi-hour
    quota window cannot trip the circuit breaker.
    """
    if not isinstance(chat_result, dict) or not chat_result.get("failed"):
        return 0
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return 1
    if chat_result.get("failure_reason") not in RATE_LIMIT_FAILURE_REASONS:
        return 1
    try:
        # Local import: kanban_db imports this module, so a module-level
        # import would close an import cycle.
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
    except Exception:
        return 1
    return int(KANBAN_RATE_LIMIT_EXIT_CODE)

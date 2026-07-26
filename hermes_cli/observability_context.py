"""Fail-open Kanban correlation dimensions for observability hook callers."""

from __future__ import annotations

import os
from typing import Optional


def _normalized(value: object) -> Optional[str]:
    """Return a stripped non-empty string, or ``None`` for absent values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _resolve_chain_and_lane(task_id: str) -> tuple[Optional[str], Optional[str]]:
    """Read the stable Kanban chain key and worker lane without mutating state."""
    try:
        from hermes_cli.kanban_db import connect, get_task
        from hermes_cli.kanban_worktrees import chain_root_id

        conn = connect(busy_timeout_ms=100)
        try:
            task = get_task(conn, task_id)
            return chain_root_id(conn, task_id), getattr(task, "assignee", None)
        finally:
            conn.close()
    except Exception:
        return None, None


def resolve_observability_context() -> dict[str, str]:
    """Return normalized worker dimensions, or an empty dict for normal agents.

    Hook emission must never affect an agent request, so unavailable Kanban state
    deliberately degrades to the task id and active worker profile when present.
    """
    task_id = _normalized(os.environ.get("HERMES_KANBAN_TASK"))
    if task_id is None:
        return {}

    chain_id, lane = _resolve_chain_and_lane(task_id)
    result = {"chain_id": _normalized(chain_id) or task_id}
    task_run_id = _normalized(os.environ.get("HERMES_KANBAN_RUN_ID"))
    if task_run_id is not None:
        result["task_run_id"] = task_run_id
    normalized_lane = _normalized(lane) or _normalized(os.environ.get("HERMES_PROFILE"))
    if normalized_lane is not None:
        result["lane"] = normalized_lane
    return result

"""Fail-open Kanban correlation dimensions for observability hook callers."""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote


def _normalized(value: object) -> Optional[str]:
    """Return a stripped non-empty string, or ``None`` for absent values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


@lru_cache(maxsize=64)
def _read_worker_dimensions(
    db_path: str,
    task_id: str,
) -> tuple[Optional[str], Optional[str]]:
    """Validate one worker task through a cached, read-only board lookup."""
    path = Path(db_path)
    if not path.is_file():
        return None, None
    connection: sqlite3.Connection | None = None
    try:
        from hermes_cli.kanban_worktrees import chain_root_id

        connection = sqlite3.connect(
            f"file:{quote(str(path.resolve()), safe='/')}?mode=ro",
            uri=True,
            timeout=0.1,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        task = connection.execute(
            "SELECT assignee FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            return None, None
        return chain_root_id(connection, task_id), _normalized(task["assignee"])
    except Exception:
        # Instrumentation is fail-open; malformed/old board schemas must not
        # affect the model request that triggered this lookup.
        return None, None
    finally:
        if connection is not None:
            connection.close()


def _resolve_chain_and_lane(task_id: str) -> tuple[Optional[str], Optional[str]]:
    """Read a real task's stable chain key and lane without board writes."""
    try:
        from hermes_cli.kanban_db import kanban_db_path

        path = kanban_db_path()
        return _read_worker_dimensions(str(path.resolve(strict=False)), task_id)
    except (OSError, ValueError):
        return None, None


def resolve_observability_context() -> dict[str, str]:
    """Return normalized worker dimensions, or an empty dict for normal agents.

    Hook emission must never affect an agent request. A missing or pseudo task
    therefore returns no worker dimensions instead of fabricating an exact link.
    """
    task_id = _normalized(os.environ.get("HERMES_KANBAN_TASK"))
    if task_id is None:
        return {}

    chain_id, lane = _resolve_chain_and_lane(task_id)
    normalized_chain = _normalized(chain_id)
    if normalized_chain is None:
        return {}
    result = {
        "kanban_task_id": task_id,
        "chain_id": normalized_chain,
    }
    task_run_id = _normalized(os.environ.get("HERMES_KANBAN_RUN_ID"))
    if task_run_id is not None:
        result["task_run_id"] = task_run_id
    normalized_lane = _normalized(lane) or _normalized(os.environ.get("HERMES_PROFILE"))
    if normalized_lane is not None:
        result["lane"] = normalized_lane
    return result

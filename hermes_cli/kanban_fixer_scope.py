"""Parent-slice scope guard for bounded conflict-fixer tasks.

This fork-owned completion edge deliberately keeps its imports of the upstream-
owned ``kanban_db`` module inside the public entry point.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any


_log = logging.getLogger(__name__)


def _payload_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    payload = json.loads(raw or "{}")
    return payload if isinstance(payload, dict) else {}


def _scope_contract(raw: object) -> dict[str, object] | None:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return None
    parsed = json.loads(str(raw))
    return parsed if isinstance(parsed, dict) else None


def _oldest_materialized_run_head(
    conn: sqlite3.Connection,
    task_id: str,
) -> str | None:
    row = conn.execute(
        "SELECT pre_run_commit_sha FROM task_runs "
        "WHERE task_id = ? AND pre_run_commit_sha IS NOT NULL "
        "AND pre_run_commit_sha != '' AND workspace_materialized = 1 "
        "ORDER BY id ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    head = str(row["pre_run_commit_sha"] or "").strip()
    return head or None


def _diff_paths(kwt, worktree: Path, diff_spec: str) -> list[str]:
    return sorted(
        {
            path.strip()
            for path in kwt._git(
                worktree, "diff", "--name-only", diff_spec,
            ).splitlines()
            if path.strip()
        }
    )


def enforce_on_complete(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any] | None:
    """Return a parked outcome when a conflict fixer leaves its parent scope.

    Missing scope or attribution evidence is intentionally fail-open. Any
    unexpected DB/git failure also skips the guard: this safety edge must not
    become a new way to deadlock an integration chain.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_worktrees as kwt

        if not kb._is_conflict_fixer_task(conn, task_id):
            return None
        marker = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'conflict_fixer_for' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if marker is None:
            return None
        marker_payload = _payload_dict(marker["payload"])
        parent_id = str(marker_payload.get("parent_id") or "").strip()
        if not parent_id:
            return None

        parent = conn.execute(
            "SELECT body, scope_contract FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            return None
        allowed_paths = kwt._task_scope_paths(
            parent["body"],
            _scope_contract(parent["scope_contract"]),
        )
        if not allowed_paths:
            return None

        fixer = conn.execute(
            "SELECT workspace_path, branch_name FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if fixer is None or not fixer["workspace_path"]:
            return None
        fixer_pre_run = _oldest_materialized_run_head(conn, task_id)
        if fixer_pre_run is None:
            return None

        worktree = Path(str(fixer["workspace_path"])).resolve()
        changed_files = _diff_paths(kwt, worktree, f"{fixer_pre_run}..HEAD")

        parent_pre_run = _oldest_materialized_run_head(conn, parent_id)
        if parent_pre_run is not None:
            parent_touched_paths = _diff_paths(
                kwt,
                worktree,
                f"{parent_pre_run}..{fixer_pre_run}",
            )
        else:
            recorded_parent_paths = kwt._lane_scope_recorded_task_commit_paths(
                conn,
                parent_id,
                worktree,
                completion_metadata=None,
            )
            parent_touched_paths = (
                sorted(recorded_parent_paths)
                if recorded_parent_paths is not None
                else []
            )

        violating_paths = [
            path
            for path in changed_files
            if not kwt._path_is_under(path, allowed_paths)
            and path not in parent_touched_paths
            and kwt._classify_dirty_paths([path])
            != kwt.PRESERVABLE_ARTIFACTS_CLASS
        ]
        if not violating_paths:
            return None

        allowed_text = ", ".join(allowed_paths)
        violating_text = ", ".join(violating_paths)
        reason = (
            f"conflict-fixer scope violation for parent {parent_id}: "
            f"out-of-scope files: {violating_text}. Allowed paths: "
            f"{allowed_text}. Resolve the conflict within the parent slice "
            "scope; revert out-of-scope changes before completing the fixer."
        )
        payload = {
            "class": "fixer_scope",
            "gate": "fixer_scope",
            "command": "conflict-fixer-parent-scope-check",
            "returncode": 1,
            "output_tail": reason,
            "parent_id": parent_id,
            "root_id": marker_payload.get("root_id"),
            "allowed_paths": allowed_paths,
            "parent_touched_paths": parent_touched_paths,
            "violating_paths": violating_paths,
            "changed_files": changed_files,
            "branch": fixer["branch_name"],
        }
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                kwt.LANE_SCOPE_BLOCKED_EVENT,
                payload,
            )
        return {
            "action": "parked",
            "reason": reason,
            "fixer_scope": payload,
        }
    except Exception:
        _log.warning(
            "conflict-fixer parent-scope guard failed open for task %s",
            task_id,
            exc_info=True,
        )
        return None

"""Chain-slice scope guard for bounded conflict-fixer tasks.

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


def _attributed_task_paths(
    conn: sqlite3.Connection,
    task_id: str,
    worktree: Path,
    kwt,
    *,
    completion_metadata: dict[str, Any] | None,
    candidate_ref: str | None,
) -> list[str] | None:
    pre_run = _oldest_materialized_run_head(conn, task_id)
    if pre_run is None or candidate_ref is None:
        return None
    try:
        kwt._git(
            worktree,
            "rev-parse",
            "--verify",
            f"{pre_run}^{{commit}}",
        )
        kwt._git(
            worktree,
            "rev-parse",
            "--verify",
            f"{candidate_ref}^{{commit}}",
        )
        stamp_is_usable = (
            kwt._branch_is_ancestor(worktree, pre_run, "HEAD")
            and kwt._branch_is_ancestor(worktree, candidate_ref, "HEAD")
            and kwt._branch_is_ancestor(worktree, pre_run, candidate_ref)
        )
    except Exception:
        stamp_is_usable = False
    if stamp_is_usable:
        try:
            paths = _diff_paths(
                kwt, worktree, f"{pre_run}..{candidate_ref}",
            )
        except Exception:
            return None
        _log.info(
            "conflict-fixer scope: task %s attributed via ancestor stamp "
            "%s..%s: %s",
            task_id,
            pre_run,
            candidate_ref,
            ", ".join(paths),
        )
        return paths

    recorded = kwt._lane_scope_recorded_task_commit_paths(
        conn,
        task_id,
        worktree,
        completion_metadata=completion_metadata,
    )
    # ``None`` is the helper's Unknown answer (no receipt, or only
    # unattributable ones such as a merge commit); it never returns an empty
    # set, so there is no local empty-set case left to re-interpret here.
    if recorded:
        paths = sorted(recorded)
        _log.info(
            "conflict-fixer scope: task %s attributed via commit receipts: %s",
            task_id,
            ", ".join(paths),
        )
        return paths
    return None


def _target_net_paths(
    conn: sqlite3.Connection,
    root_id: str,
    worktree: Path,
    kwt,
) -> tuple[set[str], str] | None:
    """Paths the fixer branch still contributes on top of its merge target.

    *root_id* must be the chain root recorded on the ``conflict_fixer_for``
    marker, never ``chain_root_id`` of the fixer itself: a fixer is attached
    to its chain by event only -- it has neither a ``task_links`` row nor a
    ``decomposed`` event -- so ``chain_root_id`` returns the fixer's own id,
    for which ``worktree_provisioned`` never exists.

    There is deliberately no ``"main"`` fallback. On a chain whose target is
    e.g. ``master`` an invented target measures against the wrong merge base,
    pulls foreign target history into the net set and can only manufacture
    parks; where ``main`` does not exist at all the diff throws and the guard
    goes silently dead. An unresolvable target is uncertainty, so it skips.
    """
    if not root_id:
        return None
    merge_target = kwt.frozen_merge_target(conn, root_id)
    if not merge_target:
        return None
    try:
        paths = set(_diff_paths(kwt, worktree, f"{merge_target}...HEAD"))
    except Exception:
        return None
    return paths, merge_target


def enforce_on_complete(
    conn: sqlite3.Connection,
    task_id: str,
    completion_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a parked outcome when a conflict fixer leaves its parent scope.

    Missing scope or attribution evidence is intentionally fail-open. Any
    unexpected DB/git failure also skips the guard: this safety edge must not
    become a new way to deadlock an integration chain.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_diagnostics import _linked_chain_member_ids
        from hermes_cli import kanban_worktrees as kwt

        if not kb._is_conflict_fixer_task(conn, task_id):
            return None
        marker = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'conflict_fixer_for' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if marker is None:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 1 "
                "failed (parent task could not be resolved)",
                task_id,
            )
            return None
        marker_payload = _payload_dict(marker["payload"])
        parent_id = str(marker_payload.get("parent_id") or "").strip()
        if not parent_id:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 1 "
                "failed (parent task could not be resolved)",
                task_id,
            )
            return None

        parent = conn.execute(
            "SELECT body, scope_contract FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 1 "
                "failed (parent task %s could not be resolved)",
                task_id,
                parent_id,
            )
            return None
        try:
            _root_id, member_ids = _linked_chain_member_ids(conn, parent_id)
            placeholders = ", ".join("?" for _ in member_ids)
            member_rows = conn.execute(
                "SELECT id, body, scope_contract FROM tasks "
                f"WHERE id IN ({placeholders})",
                tuple(sorted(member_ids)),
            ).fetchall()
            if len(member_rows) != len(member_ids):
                raise LookupError("chain member row is missing")
            allowed = set()
            for member in member_rows:
                member_scope = kwt._task_scope_paths(
                    member["body"],
                    _scope_contract(member["scope_contract"]),
                )
                if member_scope:
                    allowed.update(member_scope)
            allowed_paths = sorted(allowed)
        except Exception:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 1 "
                "failed (chain scope for parent task %s could not be resolved)",
                task_id,
                parent_id,
            )
            return None
        if not allowed_paths:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 1 "
                "failed (chain containing parent task %s declares no scope)",
                task_id,
                parent_id,
            )
            return None

        fixer = conn.execute(
            "SELECT workspace_path, branch_name FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if fixer is None or not fixer["workspace_path"]:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 2 "
                "failed (fixer workspace is unavailable)",
                task_id,
            )
            return None

        worktree = Path(str(fixer["workspace_path"])).resolve()
        changed_files = _attributed_task_paths(
            conn,
            task_id,
            worktree,
            kwt,
            completion_metadata=completion_metadata,
            candidate_ref="HEAD",
        )
        if changed_files is None:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 2 "
                "failed (fixer changes are not cleanly attributable)",
                task_id,
            )
            return None

        root_id = str(marker_payload.get("root_id") or "").strip()
        net_result = _target_net_paths(conn, root_id, worktree, kwt)
        if net_result is None:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 2 "
                "failed (merge target of chain root %r is unavailable)",
                task_id,
                root_id,
            )
            return None
        net_paths, merge_target = net_result
        changed_files = sorted(set(changed_files) & net_paths)

        parent_touched_paths = _attributed_task_paths(
            conn,
            parent_id,
            worktree,
            kwt,
            completion_metadata=None,
            candidate_ref=_oldest_materialized_run_head(conn, task_id),
        )
        if parent_touched_paths is None:
            _log.info(
                "conflict-fixer scope guard skipped for task %s: condition 3 "
                "failed (parent task %s touched paths are not cleanly "
                "attributable)",
                task_id,
                parent_id,
            )
            return None
        parent_touched_paths = sorted(set(parent_touched_paths) & net_paths)

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
            "root_id": root_id,
            "allowed_paths": allowed_paths,
            "parent_touched_paths": parent_touched_paths,
            "violating_paths": violating_paths,
            "changed_files": changed_files,
            "branch": fixer["branch_name"],
            "merge_target": merge_target,
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

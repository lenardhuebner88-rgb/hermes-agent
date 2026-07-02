"""Policy helpers for one Hermes Kanban dispatcher tick."""

from __future__ import annotations

import sqlite3
import time
from typing import Callable, Iterable, Optional


def positive_int(value) -> Optional[int]:
    """Return ``value`` as a positive int, excluding bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def positive_number(value) -> Optional[float]:
    """Return ``value`` as a positive number, excluding bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def profile_running_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count currently running tasks per assignee."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT assignee, COUNT(*) AS n FROM tasks "
        "WHERE status = 'running' AND assignee IS NOT NULL "
        "GROUP BY assignee"
    ):
        counts[row["assignee"]] = int(row["n"])
    return counts


def repo_inflight_counts(
    conn: sqlite3.Connection,
    repo_root_for_row: Callable[[str, str], Optional[str]],
) -> dict[str, int]:
    """Count non-terminal repo slots that hold serialization capacity."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks "
        "WHERE status NOT IN ('done', 'archived', 'ready', 'scheduled')"
    ):
        repo_root = repo_root_for_row(row["workspace_kind"], row["workspace_path"])
        if repo_root:
            counts[repo_root] = counts.get(repo_root, 0) + 1
    return counts


def chain_worktree_inflight_counts(
    conn: sqlite3.Connection,
    chain_root_for_task: Callable[[str], Optional[str]],
) -> dict[str, int]:
    """Count in-flight ``dir`` tasks per chain root.

    Used by the chain-worktree-serialization guard to enforce at-most-one
    running ``dir`` sibling per chain (Befund 4, 2026-07-02).  Only
    ``workspace_kind='dir'`` tasks participate — scratch/worktree tasks share
    neither the same provisioned git worktree nor the same conflict surface.

    In-flight definition: ``status NOT IN ('done', 'archived', 'todo', 'ready',
    'scheduled')``.  Excludes ``todo`` (task is waiting on a predecessor, not
    occupying a worker slot) and the other non-running terminal/parked states.
    Includes ``running``, ``review``, and ``blocked`` — a ``review``-state task
    is running gates inside the worktree; a ``blocked`` task's auto-retry will
    re-enter the same worktree.  Using a different set from
    :func:`repo_inflight_counts` (which includes ``todo``) is intentional: the
    repo guard's semantics require counting every non-terminal branch holder,
    whereas the chain guard only needs to hold back a NEW dispatch while a
    worktree worker is actually active or gating.
    """
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT id FROM tasks "
        "WHERE workspace_kind = 'dir' "
        "AND status NOT IN ('done', 'archived', 'todo', 'ready', 'scheduled')"
    ):
        root = chain_root_for_task(row["id"])
        if root is not None:
            counts[root] = counts.get(root, 0) + 1
    return counts


def capped_profiles_for_window(
    conn: sqlite3.Connection,
    *,
    token_cap: Optional[int],
    cost_cap: Optional[float],
    now: Optional[int] = None,
) -> tuple[set[str], bool]:
    """Return profiles over token cap and whether board-wide cost is over cap."""
    budget_capped_profiles: set[str] = set()
    global_cost_exceeded = False
    window_start = int(now if now is not None else time.time()) - 86400
    if token_cap is not None:
        for row in conn.execute(
            "SELECT profile, "
            "COALESCE(SUM(COALESCE(input_tokens, 0) + "
            "              COALESCE(output_tokens, 0)), 0) AS tok "
            "FROM task_runs WHERE started_at >= ? AND profile IS NOT NULL "
            "GROUP BY profile",
            (window_start,),
        ):
            if int(row["tok"]) >= token_cap:
                budget_capped_profiles.add(row["profile"])
    if cost_cap is not None:
        total_cost = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) "
            "FROM task_runs WHERE started_at >= ?",
            (window_start,),
        ).fetchone()[0]
        if float(total_cost or 0) >= cost_cap:
            global_cost_exceeded = True
    return budget_capped_profiles, global_cost_exceeded


def per_task_input_usage(
    conn: sqlite3.Connection,
    ready_ids: Iterable[str],
) -> dict[str, tuple[int, int]]:
    """Return cumulative input-token usage and run count for ready tasks."""
    usage: dict[str, tuple[int, int]] = {}
    ids = list(ready_ids)
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            "SELECT task_id, "
            "COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS tok, "
            "COUNT(*) AS n FROM task_runs "
            f"WHERE task_id IN ({placeholders}) GROUP BY task_id",
            chunk,
        ):
            usage[row["task_id"]] = (
                int(row["tok"] or 0),
                int(row["n"] or 0),
            )
    return usage

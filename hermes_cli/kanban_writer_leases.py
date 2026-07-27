"""Fork-owned helpers for Kanban worktree writer leases."""

from __future__ import annotations

import json
import sqlite3


def should_log_release_deferred(
    conn: sqlite3.Connection,
    task_id: str,
    worktree_key: str,
    candidate_sha: str | None,
    observed_sha: str | None,
) -> bool:
    """Return whether the latest deferred release differs from this state."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, "worktree_writer_release_deferred"),
    ).fetchone()
    if row is None:
        return True
    try:
        previous = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return True
    return not (
        isinstance(previous, dict)
        and previous.get("worktree_key") == worktree_key
        and "candidate_sha" in previous
        and previous["candidate_sha"] == candidate_sha
        and "observed_sha" in previous
        and previous["observed_sha"] == observed_sha
    )

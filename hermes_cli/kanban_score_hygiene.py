"""Fork-owned exclusions for known synthetic Kanban score fixtures.

The upstream run-metric writer intentionally mirrors every ended run. Two
archived ``retry-heavy`` fixtures predate that contract and would recreate
2,000 misleading attempt-index scores after the one-time cleanup. Keep the
exception at the fork edge: existing scores are never changed here, and the
upstream-owned writer remains untouched.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from hermes_cli import kanban_db as kb


SYNTHETIC_RETRY_HEAVY_TASK_IDS: Final[tuple[str, str]] = (
    "t_bbb65f0e",
    "t_8ec520d3",
)


def backfill_run_metric_scores_without_retry_fixtures(
    conn: sqlite3.Connection,
) -> int:
    """Backfill ended real runs while quarantining two archived fixtures.

    The private upstream row writer is deliberately reused so duration, token,
    cost, and attempt semantics cannot drift. Only the historical row selection
    is fork-specific.
    """

    rows = conn.execute(
        "SELECT id, task_id, COALESCE(ended_at, started_at) AS at "
        "FROM task_runs "
        "WHERE ended_at IS NOT NULL AND task_id NOT IN (?, ?)",
        SYNTHETIC_RETRY_HEAVY_TASK_IDS,
    ).fetchall()
    inserted = 0
    with kb.write_txn(conn):
        for row in rows:
            inserted += kb._record_run_metric_scores(  # noqa: SLF001
                conn,
                row["id"],
                row["task_id"],
                created_at=row["at"],
            )
    return inserted

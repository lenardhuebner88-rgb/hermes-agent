"""Fork-owned exclusions for known synthetic Kanban metric fixtures.

The exclusion is centralized here so every aggregation and backfill shares the
same testable predicate. Existing scores are never changed or deleted.
"""

from __future__ import annotations

import sqlite3
from typing import Final


SYNTHETIC_RETRY_HEAVY_TASK_IDS: Final[tuple[str, str]] = (
    "t_bbb65f0e",
    "t_8ec520d3",
)
SYNTHETIC_METRIC_PROFILE: Final[str] = "w"
SYNTHETIC_METRIC_TASK_TITLES: Final[tuple[str, str]] = (
    "time-travel",
    "retry-heavy",
)
METRIC_SCORES_RELATION: Final[str] = "metric_scores_without_test_fixtures"


def is_metric_test_fixture(
    task_id: object,
    profile: object = None,
    task_title: object = None,
) -> bool:
    """Identify the named historical metric fixtures.

    The stable task IDs quarantine the two archived retry-heavy series. The
    profile/title pair also catches equivalent fixture rows in copied test
    databases where task IDs were regenerated.
    """
    normalized_task_id = str(task_id or "").strip()
    normalized_profile = str(profile or "").strip()
    normalized_title = str(task_title or "").strip().lower()
    return (
        normalized_task_id in SYNTHETIC_RETRY_HEAVY_TASK_IDS
        or (
            normalized_profile == SYNTHETIC_METRIC_PROFILE
            and normalized_title in SYNTHETIC_METRIC_TASK_TITLES
        )
    )


def metric_scores_relation(conn: sqlite3.Connection) -> str:
    """Return a connection-local filtered scores view for all aggregations."""
    conn.create_function(
        "is_metric_test_fixture",
        3,
        is_metric_test_fixture,
        deterministic=True,
    )
    conn.execute(
        f"""
        CREATE TEMP VIEW IF NOT EXISTS {METRIC_SCORES_RELATION} AS
        SELECT s.*
        FROM main.scores AS s
        LEFT JOIN main.task_runs AS fixture_run ON fixture_run.id = s.run_id
        LEFT JOIN main.tasks AS fixture_task ON fixture_task.id = s.task_id
        WHERE NOT is_metric_test_fixture(
            s.task_id,
            COALESCE(fixture_run.profile, fixture_task.assignee),
            fixture_task.title
        )
        """
    )
    return METRIC_SCORES_RELATION


def backfill_run_metric_scores_without_retry_fixtures(
    conn: sqlite3.Connection,
) -> int:
    """Backfill ended real runs while quarantining two archived fixtures.

    The private upstream row writer is deliberately reused so duration, token,
    cost, and attempt semantics cannot drift. Only the historical row selection
    is fork-specific.
    """

    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT task_runs.id, task_runs.task_id, "
        "COALESCE(task_runs.ended_at, task_runs.started_at) AS at, "
        "task_runs.profile, tasks.title "
        "FROM task_runs JOIN tasks ON tasks.id = task_runs.task_id "
        "WHERE task_runs.ended_at IS NOT NULL"
    ).fetchall()
    inserted = 0
    with kb.write_txn(conn):
        for row in rows:
            if is_metric_test_fixture(
                row["task_id"],
                row["profile"],
                row["title"],
            ):
                continue
            inserted += kb._record_run_metric_scores(  # noqa: SLF001
                conn,
                row["id"],
                row["task_id"],
                created_at=row["at"],
            )
    return inserted

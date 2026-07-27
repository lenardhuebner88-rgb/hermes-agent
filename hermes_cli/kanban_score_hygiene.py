"""Fork-owned classification for Kanban metric runs and score rows.

Every run remains visible.  Synthetic fixtures and telemetry-empty attempts
are labelled instead of being silently filtered out of metric aggregations.
"""

from __future__ import annotations

import sqlite3
from typing import Final


SYNTHETIC_RETRY_HEAVY_TASK_IDS: Final[tuple[str, str]] = (
    "t_bbb65f0e",
    "t_8ec520d3",
)
SYNTHETIC_METRIC_PROFILE: Final[str] = "w"
RUN_CLASS_PRODUCTIVE: Final[str] = "produktiv"
RUN_CLASS_FIXTURE: Final[str] = "fixture"
RUN_CLASS_NEVER_RAN: Final[str] = "nie_gelaufen"
RUN_CLASSES: Final[tuple[str, str, str]] = (
    RUN_CLASS_PRODUCTIVE,
    RUN_CLASS_FIXTURE,
    RUN_CLASS_NEVER_RAN,
)
RUN_CLASSIFICATION_RELATION: Final[str] = "metric_run_classification"
METRIC_SCORES_RELATION: Final[str] = "metric_scores_classified"


def classify_metric_run(
    task_id: object,
    profile: object = None,
    *,
    active_model: object = None,
    input_tokens: object = None,
    output_tokens: object = None,
    cost_usd: object = None,
    started_at: object = None,
    ended_at: object = None,
) -> str:
    """Return exactly one ordered class for a run.

    Fixture wins over the telemetry-empty predicate because the two historical
    populations overlap heavily.  A missing profile is deliberately neutral.
    """
    normalized_task_id = str(task_id or "").strip()
    normalized_profile = str(profile or "").strip()
    if (
        normalized_profile == SYNTHETIC_METRIC_PROFILE
        or normalized_task_id in SYNTHETIC_RETRY_HEAVY_TASK_IDS
    ):
        return RUN_CLASS_FIXTURE

    telemetry_empty = all(
        value is None
        for value in (active_model, input_tokens, output_tokens, cost_usd)
    )
    try:
        zero_or_missing_duration = (
            started_at is None
            or ended_at is None
            or float(ended_at) <= float(started_at)
        )
    except (TypeError, ValueError):
        zero_or_missing_duration = False
    if telemetry_empty and zero_or_missing_duration:
        return RUN_CLASS_NEVER_RAN
    return RUN_CLASS_PRODUCTIVE


def is_metric_test_fixture(
    task_id: object,
    profile: object = None,
    task_title: object = None,
) -> bool:
    """Compatibility predicate; task titles are no longer a class criterion."""
    del task_title
    return (
        classify_metric_run(task_id, profile=profile)
        == RUN_CLASS_FIXTURE
    )


def metric_run_classification_relation(conn: sqlite3.Connection) -> str:
    """Create the connection-local, exhaustive run classification view."""
    fixture_ids = ", ".join(
        f"'{task_id}'" for task_id in SYNTHETIC_RETRY_HEAVY_TASK_IDS
    )
    conn.execute(
        f"""
        CREATE TEMP VIEW IF NOT EXISTS {RUN_CLASSIFICATION_RELATION} AS
        SELECT task_runs.*,
               CASE
                   WHEN COALESCE(task_runs.profile, '') =
                            '{SYNTHETIC_METRIC_PROFILE}'
                        OR task_runs.task_id IN ({fixture_ids})
                       THEN '{RUN_CLASS_FIXTURE}'
                   WHEN task_runs.active_model IS NULL
                        AND task_runs.input_tokens IS NULL
                        AND task_runs.output_tokens IS NULL
                        AND task_runs.cost_usd IS NULL
                        AND (
                            task_runs.ended_at IS NULL
                            OR task_runs.started_at IS NULL
                            OR task_runs.ended_at <= task_runs.started_at
                        )
                       THEN '{RUN_CLASS_NEVER_RAN}'
                   ELSE '{RUN_CLASS_PRODUCTIVE}'
               END AS run_class
        FROM main.task_runs
        """
    )
    return RUN_CLASSIFICATION_RELATION


def metric_run_class_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count every run once and retain zeroes for absent classes."""
    relation = metric_run_classification_relation(conn)
    counts = {run_class: 0 for run_class in RUN_CLASSES}
    for row in conn.execute(
        f"SELECT run_class, COUNT(*) AS count FROM {relation} "
        "GROUP BY run_class"
    ):
        counts[str(row["run_class"])] = int(row["count"])
    return counts


def metric_scores_relation(conn: sqlite3.Connection) -> str:
    """Return all score rows with their run class attached."""
    runs = metric_run_classification_relation(conn)
    fixture_ids = ", ".join(
        f"'{task_id}'" for task_id in SYNTHETIC_RETRY_HEAVY_TASK_IDS
    )
    conn.execute(
        f"""
        CREATE TEMP VIEW IF NOT EXISTS {METRIC_SCORES_RELATION} AS
        SELECT s.*,
               COALESCE(
                   classified_run.run_class,
                   CASE
                       WHEN COALESCE(score_task.assignee, '') =
                                '{SYNTHETIC_METRIC_PROFILE}'
                            OR s.task_id IN ({fixture_ids})
                           THEN '{RUN_CLASS_FIXTURE}'
                       ELSE '{RUN_CLASS_PRODUCTIVE}'
                   END
               ) AS run_class
        FROM main.scores AS s
        LEFT JOIN {runs} AS classified_run
               ON classified_run.id = s.run_id
        LEFT JOIN main.tasks AS score_task ON score_task.id = s.task_id
        """
    )
    return METRIC_SCORES_RELATION


def backfill_run_metric_scores_with_classification(
    conn: sqlite3.Connection,
) -> int:
    """Backfill all ended runs; classification stays an analytics dimension."""
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT task_runs.id, task_runs.task_id, "
        "COALESCE(task_runs.ended_at, task_runs.started_at) AS at "
        "FROM task_runs WHERE task_runs.ended_at IS NOT NULL"
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


def backfill_run_metric_scores_without_retry_fixtures(
    conn: sqlite3.Connection,
) -> int:
    """Compatibility entrypoint: fixtures are now included and classified."""
    return backfill_run_metric_scores_with_classification(conn)

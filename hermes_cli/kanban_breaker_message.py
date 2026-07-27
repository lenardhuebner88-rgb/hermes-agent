"""Operator-facing circuit-breaker messages for the Hermes fork."""

from __future__ import annotations

import sqlite3


def breaker_trip_prefix(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    failures: int,
    trigger_outcome: str,
    effective_limit: int,
) -> str:
    """Describe the observed failure kinds without changing breaker policy."""
    prior_count = max(0, int(failures) - 1)
    rows = conn.execute(
        """
        SELECT outcome
          FROM task_runs
         WHERE task_id = ?
           AND ended_at IS NOT NULL
           AND outcome IS NOT NULL
         ORDER BY started_at DESC, id DESC
         LIMIT ?
        """,
        (task_id, prior_count),
    ).fetchall()
    streak_outcomes = [
        str(row[0]) for row in reversed(rows) if row[0] is not None
    ]
    streak_outcomes.append(str(trigger_outcome))

    if len(streak_outcomes) == int(failures) and all(
        item == trigger_outcome for item in streak_outcomes
    ):
        return (
            f"circuit breaker tripped after {failures} consecutive "
            f"{trigger_outcome} failures (limit {effective_limit}): "
        )

    observed_kinds = ", ".join(dict.fromkeys(streak_outcomes))
    return (
        f"circuit breaker tripped after {failures} failures "
        f"(limit {effective_limit}; triggered by {trigger_outcome}; "
        f"streak outcomes: {observed_kinds}): "
    )

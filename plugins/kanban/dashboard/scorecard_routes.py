"""Read-only scorecard aggregation route."""
from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
import time
from typing import Optional

from fastapi import Query

from hermes_cli.kanban_score_hygiene import (
    metric_run_class_counts,
    metric_scores_relation,
)
from plugins.kanban.dashboard.langfuse_read_model import (
    get_langfuse_snapshot as _get_langfuse_snapshot,
)


_NUMERIC_MATERIALIZED_SCORE_NAMES = (
    "run_cost_effective_usd",
    "run_cost_usd",
    "run_duration_seconds",
    "run_tokens_total",
    "run_attempt_index",
    "review_iterations_to_approval",
)
_CATEGORICAL_MATERIALIZED_SCORE_NAMES = (
    "run_outcome_kind",
)
_RUN_OUTCOME_KIND_LABELS = {
    1.0: "completed",
    2.0: "blocked",
    3.0: "iteration_budget_exhausted",
    4.0: "spawn_failed",
    5.0: "gave_up",
    6.0: "crashed",
    7.0: "reclaimed",
    8.0: "scheduled",
    9.0: "spawn_retry",
    10.0: "stale",
    11.0: "timed_out",
    12.0: "operator_review_required",
}


def _score_rows(
    conn: sqlite3.Connection,
    *,
    since: int | None = None,
) -> list[sqlite3.Row]:
    scores = metric_scores_relation(conn)
    where = "WHERE s.name = 'review_verdict'"
    params: tuple[object, ...] = ()
    if since is not None:
        where += " AND s.created_at >= ?"
        params = (since,)
    return conn.execute(
        "SELECT s.name, s.value, s.created_at, COALESCE(r.profile, 'unknown') AS profile, "
        "COALESCE(r.active_model, r.requested_model, 'unknown') AS model "
        f"FROM {scores} s LEFT JOIN task_runs r ON r.id = s.run_id "
        f"{where} ORDER BY s.created_at",
        params,
    ).fetchall()


def _rate(rows: list[sqlite3.Row]) -> dict[str, object]:
    total = len(rows)
    approved = sum(1 for row in rows if float(row['value'] or 0) == 1.0)
    return {"runs": total, "approved": approved, "approval_rate": approved / total if total else None}


def _run_outcome_kind_label(value: object) -> str:
    """Make legacy numeric outcome scores readable without dropping unknown codes."""
    raw_value = str(value)
    try:
        code = float(raw_value)
    except (TypeError, ValueError):
        return raw_value
    return _RUN_OUTCOME_KIND_LABELS.get(code, f"unknown_outcome_code:{raw_value}")


def _materialized_scores(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Aggregate the score-materialization signals without hiding thin samples.

    Numeric signals include distribution bounds and total alongside their
    arithmetic mean. Categorical signals are value-frequency maps so values
    such as task states are never coerced into misleading numbers. Every
    supported name is present even if it has no rows yet.
    """
    scores: dict[str, dict[str, object]] = {
        name: {"value": None, "min": None, "max": None, "sum": None, "count": 0}
        for name in _NUMERIC_MATERIALIZED_SCORE_NAMES
    }
    scores.update({name: {"value": None, "count": 0} for name in _CATEGORICAL_MATERIALIZED_SCORE_NAMES})
    score_relation = metric_scores_relation(conn)
    numeric_placeholders = ", ".join("?" for _ in _NUMERIC_MATERIALIZED_SCORE_NAMES)
    numeric_rows = conn.execute(
        f"SELECT name, AVG(CAST(value AS REAL)) AS value, "
        f"MIN(CAST(value AS REAL)) AS minimum, MAX(CAST(value AS REAL)) AS maximum, "
        f"SUM(CAST(value AS REAL)) AS total, COUNT(*) AS count "
        f"FROM {score_relation} WHERE name IN ({numeric_placeholders}) GROUP BY name",
        _NUMERIC_MATERIALIZED_SCORE_NAMES,
    ).fetchall()
    for row in numeric_rows:
        scores[str(row["name"])] = {
            "value": float(row["value"]) if row["value"] is not None else None,
            "min": float(row["minimum"]) if row["minimum"] is not None else None,
            "max": float(row["maximum"]) if row["maximum"] is not None else None,
            "sum": float(row["total"]) if row["total"] is not None else None,
            "count": int(row["count"]),
        }

    categorical_placeholders = ", ".join("?" for _ in _CATEGORICAL_MATERIALIZED_SCORE_NAMES)
    categorical_rows = conn.execute(
        f"SELECT name, value, COUNT(*) AS count FROM {score_relation} "
        f"WHERE name IN ({categorical_placeholders}) "
        f"GROUP BY name, value ORDER BY name, value",
        _CATEGORICAL_MATERIALIZED_SCORE_NAMES,
    ).fetchall()
    for row in categorical_rows:
        name = str(row["name"])
        entry = scores[name]
        values = entry["value"]
        if values is None:
            values = {}
            entry["value"] = values
        assert isinstance(values, dict)
        label = _run_outcome_kind_label(row["value"]) if name == "run_outcome_kind" else str(row["value"])
        values[label] = int(values.get(label, 0)) + int(row["count"])
        entry["count"] = int(entry["count"]) + int(row["count"])
    return scores


def _quality_snapshot(
    conn: sqlite3.Connection,
    *,
    days: int,
    now: int,
) -> dict[str, object]:
    cutoff = now - (days * 86400)
    rows = _score_rows(conn, since=cutoff)
    by_profile: dict[str, list[sqlite3.Row]] = {}
    by_model: dict[str, list[sqlite3.Row]] = {}
    verdicts = {"approved": 0, "rejected": 0}
    daily: dict[str, dict[str, int]] = {}
    for row in rows:
        by_profile.setdefault(str(row["profile"]), []).append(row)
        by_model.setdefault(str(row["model"]), []).append(row)
        verdicts[
            "approved" if float(row["value"] or 0) == 1.0 else "rejected"
        ] += 1
        day = dt.datetime.fromtimestamp(
            int(row["created_at"]),
            tz=dt.timezone.utc,
        ).date().isoformat()
        bucket = daily.setdefault(
            day,
            {"approved": 0, "rejected": 0},
        )
        bucket[
            "approved" if float(row["value"] or 0) == 1.0 else "rejected"
        ] += 1

    score_relation = metric_scores_relation(conn)
    score_rows = conn.execute(
        f"SELECT name, value FROM {score_relation} "
        "WHERE created_at >= ? AND name IN ("
        "'run_outcome_kind', 'review_iterations_to_approval')",
        (cutoff,),
    ).fetchall()
    outcomes: dict[str, int] = {}
    iterations: list[float] = []
    for row in score_rows:
        name = str(row["name"])
        if name == "run_outcome_kind":
            label = _run_outcome_kind_label(row["value"])
            outcomes[label] = outcomes.get(label, 0) + 1
        else:
            try:
                value = float(row["value"])
            except (TypeError, ValueError):
                continue
            if name == "review_iterations_to_approval":
                iterations.append(value)

    total_runs = int(
        conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE started_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    )
    group = lambda data: [
        dict(name=name, **_rate(items))
        for name, items in sorted(data.items())
    ]
    iteration_distribution: dict[str, int] = {}
    for value in iterations:
        key = str(max(0, int(round(value))))
        iteration_distribution[key] = iteration_distribution.get(key, 0) + 1
    return {
        "window_days": days,
        "overall": _rate(rows),
        "verdicts": verdicts,
        "outcomes": dict(
            sorted(outcomes.items(), key=lambda item: (-item[1], item[0]))
        ),
        "profiles": group(by_profile),
        "models": group(by_model),
        "daily_verdicts": [
            {"date": date, **values}
            for date, values in sorted(daily.items())
        ],
        "review_iterations": {
            "average": statistics.fmean(iterations) if iterations else None,
            "count": len(iterations),
            "distribution": iteration_distribution,
        },
        "coverage": {
            "runs": total_runs,
            "review_verdicts": len(rows),
            "run_outcomes": sum(outcomes.values()),
            "review_iterations": len(iterations),
        },
        "source": "kanban.metric_scores",
        "captured_at": now,
    }


@scorecard_routes.get("/scorecard")
def get_scorecard(
    board: Optional[str] = Query(
        None,
        description="Kanban board slug (omit for current)",
    ),
    days: int = Query(7, ge=1, le=30),
):
    """Aggregate review verdict scores by profile/model and ISO week."""
    now = int(time.time())
    conn = _conn(board)
    try:
        rows = _score_rows(conn)
        materialized_scores = _materialized_scores(conn)
        run_classes = metric_run_class_counts(conn)
        quality = _quality_snapshot(conn, days=days, now=now)
    finally:
        conn.close()
    by_profile: dict[str, list[sqlite3.Row]] = {}
    by_model: dict[str, list[sqlite3.Row]] = {}
    by_verdict = {"approved": 0, "rejected": 0}
    by_week: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in rows:
        by_profile.setdefault(str(row['profile']), []).append(row)
        by_model.setdefault(str(row['model']), []).append(row)
        by_verdict['approved' if float(row['value'] or 0) == 1.0 else 'rejected'] += 1
        date = dt.datetime.fromtimestamp(int(row['created_at']), tz=dt.timezone.utc).date()
        iso = date.isocalendar()
        by_week.setdefault((iso.year, iso.week), []).append(row)
    group = lambda data: [dict(name=name, **_rate(items)) for name, items in sorted(data.items())]
    # Scorecard owns quality, not remote observability refreshes. It may expose
    # cached health from Statistik but must never block on Langfuse I/O.
    langfuse = _get_langfuse_snapshot(days=days, allow_refresh=False)
    return {
        "contract_version": "hermes-scorecard.v2",
        "overall": _rate(rows),
        "run_classes": run_classes,
        "verdicts": by_verdict,
        "profiles": group(by_profile),
        "models": group(by_model),
        "weeks": [dict(year=year, week=week, **_rate(items)) for (year, week), items in sorted(by_week.items())],
        "materialized_scores": materialized_scores,
        "quality": quality,
        # Health only: throughput, cost, tokens and observation latency remain
        # exclusive to /control/statistik.
        "observability": {
            key: langfuse.get(key)
            for key in (
                "available",
                "state",
                "reason",
                "captured_at",
                "cache",
            )
        },
        "checked_at": now,
    }


__all__ = ["get_scorecard"]

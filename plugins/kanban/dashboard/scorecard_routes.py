"""Read-only scorecard aggregation route."""
from __future__ import annotations

import datetime as dt


_NUMERIC_MATERIALIZED_SCORE_NAMES = (
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


def _score_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.name, s.value, s.created_at, COALESCE(r.profile, 'unknown') AS profile, "
        "COALESCE(r.active_model, r.requested_model, 'unknown') AS model "
        "FROM scores s LEFT JOIN task_runs r ON r.id = s.run_id "
        "WHERE s.name = 'review_verdict' ORDER BY s.created_at"
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
    numeric_placeholders = ", ".join("?" for _ in _NUMERIC_MATERIALIZED_SCORE_NAMES)
    numeric_rows = conn.execute(
        f"SELECT name, AVG(CAST(value AS REAL)) AS value, "
        f"MIN(CAST(value AS REAL)) AS minimum, MAX(CAST(value AS REAL)) AS maximum, "
        f"SUM(CAST(value AS REAL)) AS total, COUNT(*) AS count "
        f"FROM scores WHERE name IN ({numeric_placeholders}) GROUP BY name",
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
        f"SELECT name, value, COUNT(*) AS count FROM scores "
        f"WHERE name IN ({categorical_placeholders}) GROUP BY name, value ORDER BY name, value",
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


@scorecard_routes.get("/scorecard")
def get_scorecard(board: Optional[str] = Query(None, description="Kanban board slug (omit for current)")):
    """Aggregate review verdict scores by profile/model and ISO week."""
    conn = _conn(board)
    try:
        rows = _score_rows(conn)
        materialized_scores = _materialized_scores(conn)
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
    return {
        "overall": _rate(rows),
        "verdicts": by_verdict,
        "profiles": group(by_profile),
        "models": group(by_model),
        "weeks": [dict(year=year, week=week, **_rate(items)) for (year, week), items in sorted(by_week.items())],
        "materialized_scores": materialized_scores,
        "checked_at": int(time.time()),
    }


__all__ = ["get_scorecard"]

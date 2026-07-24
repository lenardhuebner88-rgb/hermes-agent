"""Read-only scorecard aggregation route."""
from __future__ import annotations

import datetime as dt


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


@core_routes.get("/scorecard")
def get_scorecard(board: Optional[str] = Query(None, description="Kanban board slug (omit for current)")):
    """Aggregate review verdict scores by profile/model and ISO week."""
    conn = _connect(board)
    try:
        rows = _score_rows(conn)
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
        "checked_at": int(time.time()),
    }


__all__ = ["get_scorecard"]

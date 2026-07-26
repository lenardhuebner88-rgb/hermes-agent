"""Fork-owned weekly Kanban score digest and deterministic findings.

The digest reads only the local ``kanban.db`` score store.  No Langfuse
requests are made on the CLI/render path.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import time
from typing import Any, Optional


_EUR_PER_USD = 0.92
_FINDING_SOURCE = "kanban.db/scores"
_MAX_FINDINGS = 5


def _nearest_rank_percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    index = int(round((pct / 100.0) * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, index))]


def _display_task_id(task_id: Any) -> str:
    value = str(task_id or "?")
    return value if len(value) <= 48 else value[:45] + "..."


def _impact_eur(cost_usd: float, multiplier: float = 1.0) -> float:
    """Keep the EUR estimate deterministic and explicit about its basis."""
    return round(max(0.0, cost_usd * multiplier * _EUR_PER_USD), 2)


def _window_costs(conn: sqlite3.Connection, start_epoch: int) -> dict[int, float]:
    return {
        int(row["run_id"]): float(row["cost"] or 0.0)
        for row in conn.execute(
            "SELECT run_id, MAX(CAST(value AS REAL)) AS cost FROM scores "
            "WHERE name = 'run_cost_usd' AND run_id IS NOT NULL AND created_at >= ? "
            "GROUP BY run_id",
            (start_epoch,),
        )
    }


def _top_findings(
    conn: sqlite3.Connection,
    *,
    start_epoch: int,
    current_week_epoch: int,
) -> list[dict[str, Any]]:
    """Return at most five local, stable, cost-basis findings.

    ``impact_eur`` is an estimate calculated from persisted ``run_cost_usd``
    score rows and a fixed documented 0.92 USD→EUR conversion.  For cache and
    queue findings it is a cost-at-risk estimate, not a claim of causal loss.
    """
    costs = _window_costs(conn, start_epoch)
    findings: list[dict[str, Any]] = []

    for row in conn.execute(
        "SELECT task_id, run_id, MAX(CAST(value AS REAL)) AS attempts "
        "FROM scores WHERE name = 'task_runs_to_done' AND created_at >= ? "
        "GROUP BY task_id, run_id HAVING attempts > 1 "
        "ORDER BY attempts DESC, task_id ASC, run_id ASC",
        (start_epoch,),
    ):
        attempts = float(row["attempts"])
        retries = max(0.0, attempts - 1.0)
        task_id = _display_task_id(row["task_id"])
        cost = costs.get(int(row["run_id"]) if row["run_id"] is not None else -1, 0.0)
        findings.append(
            {
                "kind": "attempt_hotspot",
                "title": f"Attempt-Hotspot: {task_id}",
                "summary": f"{int(attempts)} Läufe bis done; {int(retries)} Wiederholung(en)",
                "impact_eur": _impact_eur(cost, retries),
                "data_source": _FINDING_SOURCE,
                "severity": retries,
                "subject": task_id,
            }
        )

    total_tasks = int(
        conn.execute(
            "SELECT COUNT(DISTINCT task_id) AS n FROM scores WHERE created_at >= ?",
            (start_epoch,),
        ).fetchone()["n"]
        or 0
    )
    veto_rows = conn.execute(
        "SELECT task_id, run_id FROM scores "
        "WHERE name = 'operator_veto' AND CAST(value AS REAL) > 0 AND created_at >= ? "
        "ORDER BY task_id ASC, run_id ASC",
        (start_epoch,),
    ).fetchall()
    if veto_rows:
        veto_count = len({str(row["task_id"]) for row in veto_rows})
        rate = veto_count / total_tasks if total_tasks else 0.0
        cost_at_risk = sum(
            costs.get(int(row["run_id"]) if row["run_id"] is not None else -1, 0.0)
            for row in veto_rows
        )
        findings.append(
            {
                "kind": "veto_rate",
                "title": "Veto-Rate-Ausreißer",
                "summary": f"{veto_count}/{total_tasks} beobachtete Tasks ({rate:.1%})",
                "impact_eur": _impact_eur(cost_at_risk),
                "data_source": _FINDING_SOURCE,
                "severity": rate,
                "subject": "veto",
            }
        )

    prior_cache = conn.execute(
        "SELECT AVG(CAST(value AS REAL)) AS ratio FROM scores "
        "WHERE name = 'cache_hit_ratio' AND created_at >= ? AND created_at < ?",
        (start_epoch, current_week_epoch),
    ).fetchone()["ratio"]
    current_cache = conn.execute(
        "SELECT AVG(CAST(value AS REAL)) AS ratio FROM scores "
        "WHERE name = 'cache_hit_ratio' AND created_at >= ?",
        (current_week_epoch,),
    ).fetchone()["ratio"]
    if prior_cache is not None and current_cache is not None:
        drop = float(prior_cache) - float(current_cache)
        if drop > 0:
            current_cache_cost = sum(
                costs.get(int(row["run_id"]), 0.0)
                for row in conn.execute(
                    "SELECT DISTINCT run_id FROM scores "
                    "WHERE name = 'cache_hit_ratio' AND run_id IS NOT NULL AND created_at >= ?",
                    (current_week_epoch,),
                )
            )
            findings.append(
                {
                    "kind": "cache_hit_drop",
                    "title": "Cache-Hit-Einbruch",
                    "summary": f"{float(prior_cache):.1%} → {float(current_cache):.1%}",
                    "impact_eur": _impact_eur(current_cache_cost, drop),
                    "data_source": _FINDING_SOURCE,
                    "severity": drop,
                    "subject": "cache",
                }
            )

    queue_rows = conn.execute(
        "SELECT run_id, CAST(value AS REAL) AS seconds FROM scores "
        "WHERE name = 'queue_latency_seconds' AND created_at >= ? AND value IS NOT NULL "
        "ORDER BY seconds ASC, run_id ASC",
        (start_epoch,),
    ).fetchall()
    queue_values = [float(row["seconds"]) for row in queue_rows]
    p95 = _nearest_rank_percentile(queue_values, 95)
    if p95 is not None:
        cost_at_risk = sum(
            costs.get(int(row["run_id"]) if row["run_id"] is not None else -1, 0.0)
            for row in queue_rows
            if float(row["seconds"]) >= p95
        )
        findings.append(
            {
                "kind": "queue_p95",
                "title": "Queue-p95-Ausreißer",
                "summary": f"p95 {p95:.0f}s ({len(queue_values)} Messung(en))",
                "impact_eur": _impact_eur(cost_at_risk),
                "data_source": _FINDING_SOURCE,
                "severity": p95,
                "subject": "queue",
            }
        )

    findings.sort(
        key=lambda item: (-item["impact_eur"], -item["severity"], item["kind"], item["subject"])
    )
    for item in findings:
        item.pop("severity")
        item.pop("subject")
    return findings[:_MAX_FINDINGS]


def scores_digest(
    conn: sqlite3.Connection, *, weeks: int = 4, now: Optional[int] = None
) -> dict[str, Any]:
    """Build the local weekly digest and its deterministic top findings."""
    weeks = max(1, int(weeks))
    generated_at = int(now or time.time())
    now_dt = _dt.datetime.fromtimestamp(generated_at, tz=_dt.timezone.utc)
    current_monday = now_dt.date() - _dt.timedelta(days=now_dt.weekday())
    week_starts = [
        current_monday - _dt.timedelta(weeks=offset)
        for offset in range(weeks - 1, -1, -1)
    ]
    start_epoch = int(
        _dt.datetime.combine(week_starts[0], _dt.time.min, tzinfo=_dt.timezone.utc).timestamp()
    )
    current_week_epoch = int(
        _dt.datetime.combine(current_monday, _dt.time.min, tzinfo=_dt.timezone.utc).timestamp()
    )

    overall = conn.execute(
        "SELECT COUNT(*) AS rows_total, "
        "COALESCE(SUM(CASE WHEN value = 1.0 THEN 1 ELSE 0 END), 0) AS approved_rows "
        "FROM scores WHERE name = 'review_verdict'"
    ).fetchone()
    rows_total = int(overall["rows_total"])
    approved_rows = int(overall["approved_rows"])
    approval_rate = approved_rows / rows_total if rows_total else None

    weekly_counts: dict[_dt.date, tuple[int, int]] = {}
    for row in conn.execute(
        "SELECT created_at, value FROM scores "
        "WHERE name = 'review_verdict' AND created_at >= ? ORDER BY created_at",
        (start_epoch,),
    ):
        day = _dt.datetime.fromtimestamp(int(row["created_at"]), tz=_dt.timezone.utc).date()
        monday = day - _dt.timedelta(days=day.weekday())
        if monday in week_starts:
            total, approved = weekly_counts.get(monday, (0, 0))
            weekly_counts[monday] = (total + 1, approved + int(row["value"] == 1.0))

    weekly: list[dict[str, Any]] = []
    for monday in week_starts:
        total, approved = weekly_counts.get(monday, (0, 0))
        iso_year, iso_week, _ = monday.isocalendar()
        weekly.append(
            {
                "year": iso_year,
                "week": iso_week,
                "rows_total": total,
                "approved_rows": approved,
                "approval_rate": approved / total if total else None,
            }
        )

    rates_with_data = [week for week in weekly if week["approval_rate"] is not None]
    wow_delta = (
        rates_with_data[-1]["approval_rate"] - rates_with_data[-2]["approval_rate"]
        if len(rates_with_data) >= 2
        else None
    )

    by_profile: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT COALESCE(tr.profile, '?') AS profile, COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN s.value = 1.0 THEN 1 ELSE 0 END), 0) AS approved "
        "FROM scores s JOIN task_runs tr ON s.run_id = tr.id "
        "WHERE s.name = 'review_verdict' GROUP BY tr.profile ORDER BY total DESC"
    ):
        total = int(row["total"])
        approved = int(row["approved"])
        by_profile[str(row["profile"])] = {
            "total": total,
            "approved": approved,
            "approval_rate": approved / total if total else None,
        }

    by_model: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT COALESCE(tr.active_model, '?') AS model, COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN s.value = 1.0 THEN 1 ELSE 0 END), 0) AS approved "
        "FROM scores s JOIN task_runs tr ON s.run_id = tr.id "
        "WHERE s.name = 'review_verdict' GROUP BY tr.active_model ORDER BY total DESC"
    ):
        total = int(row["total"])
        approved = int(row["approved"])
        by_model[str(row["model"])] = {
            "total": total,
            "approved": approved,
            "approval_rate": approved / total if total else None,
        }

    cost_duration: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT s.run_id, tr.task_id, "
        "MAX(CASE WHEN s.name = 'run_cost_usd' THEN s.value END) AS cost, "
        "MAX(CASE WHEN s.name = 'run_duration_seconds' THEN s.value END) AS duration "
        "FROM scores s JOIN task_runs tr ON tr.id = s.run_id "
        "WHERE s.run_id IN (SELECT run_id FROM scores WHERE name = 'review_verdict' AND value = 1.0) "
        "AND s.name IN ('run_cost_usd', 'run_duration_seconds') "
        "GROUP BY s.run_id ORDER BY s.run_id DESC LIMIT 20"
    ):
        cost_duration.append(
            {
                "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
                "task_id": str(row["task_id"]) if row["task_id"] is not None else None,
                "cost_usd": float(row["cost"]) if row["cost"] is not None else None,
                "duration_seconds": float(row["duration"]) if row["duration"] is not None else None,
            }
        )

    has_metrics = conn.execute(
        "SELECT 1 FROM scores WHERE name IN ('run_cost_usd', 'run_duration_seconds') LIMIT 1"
    ).fetchone() is not None
    approved_run_metric_coverage = any(
        row.get("cost_usd") is not None or row.get("duration_seconds") is not None
        for row in cost_duration
    )

    retry_hotspots = [
        {"task_id": str(row["task_id"]), "rejections": int(row["rejections"])}
        for row in conn.execute(
            "SELECT task_id, COUNT(*) AS rejections FROM scores "
            "WHERE name = 'review_verdict' AND value = 0.0 AND created_at >= ? "
            "GROUP BY task_id ORDER BY rejections DESC LIMIT 3",
            (start_epoch,),
        )
    ]

    return {
        "generated_at": generated_at,
        "weeks_requested": weeks,
        "approval_rate": approval_rate,
        "approved_rows": approved_rows,
        "rows_total": rows_total,
        "wow_delta": wow_delta,
        "weekly": weekly,
        "by_profile": by_profile,
        "by_model": by_model,
        "cost_duration_per_approved_run": cost_duration,
        "has_metric_scores": has_metrics,
        "approved_run_metric_coverage": approved_run_metric_coverage,
        "retry_hotspots": retry_hotspots,
        "top_findings": _top_findings(
            conn, start_epoch=start_epoch, current_week_epoch=current_week_epoch
        ),
    }

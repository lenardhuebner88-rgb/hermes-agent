"""Read-only per-agent task-run history for the Fleet dashboard."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from hermes_cli.config import get_hermes_home


_ERROR_STATUSES = ("crashed", "timed_out", "failed")
_ERROR_OUTCOMES = (
    "crashed",
    "timed_out",
    "spawn_failed",
    "gave_up",
    "iteration_budget_exhausted",
)


def _open_sqlite_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _clamp_days(days: int) -> int:
    return min(90, max(1, days))


def _rate(errors: int, runs: int) -> float:
    return round(errors / runs, 3) if runs else 0.0


def _task_runs_has_cost(conn: sqlite3.Connection) -> bool:
    return any(row["name"] == "cost_usd" for row in conn.execute("PRAGMA table_info(task_runs)"))


def build_agent_history(
    *,
    db_path: Path | None = None,
    days: int = 30,
    now: int | None = None,
) -> dict[str, Any]:
    """Aggregate ``task_runs`` by profile and local calendar day."""

    selected_days = _clamp_days(days)
    generated_at = int(time.time() if now is None else now)
    selected_db = db_path if db_path is not None else get_hermes_home() / "kanban.db"

    with contextlib.closing(_open_sqlite_ro(selected_db)) as conn:
        cost_sql = (
            "COALESCE(SUM(cost_usd), 0.0)"
            if _task_runs_has_cost(conn)
            else "0.0"
        )
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(profile, 'unbekannt') AS profile,
                date(started_at, 'unixepoch', 'localtime') AS day,
                COUNT(*) AS runs,
                SUM(
                    CASE
                        WHEN status IN ({", ".join("?" for _ in _ERROR_STATUSES)})
                          OR outcome IN ({", ".join("?" for _ in _ERROR_OUTCOMES)})
                        THEN 1
                        ELSE 0
                    END
                ) AS errors,
                {cost_sql} AS cost_usd
            FROM task_runs
            WHERE date(started_at, 'unixepoch', 'localtime')
                  BETWEEN date(?, 'unixepoch', 'localtime', ?)
                      AND date(?, 'unixepoch', 'localtime')
            GROUP BY COALESCE(profile, 'unbekannt'), day
            ORDER BY profile COLLATE NOCASE, day
            """,
            (
                *_ERROR_STATUSES,
                *_ERROR_OUTCOMES,
                generated_at,
                f"-{selected_days - 1} days",
                generated_at,
            ),
        ).fetchall()

    agents_by_profile: dict[str, dict[str, Any]] = {}
    for row in rows:
        profile = str(row["profile"])
        runs = int(row["runs"])
        errors = int(row["errors"])
        cost_usd = round(float(row["cost_usd"] or 0.0), 6)
        agent = agents_by_profile.setdefault(
            profile,
            {
                "profile": profile,
                "series": [],
                "totals": {
                    "runs": 0,
                    "errors": 0,
                    "error_rate": 0.0,
                    "cost_usd": 0.0,
                },
            },
        )
        agent["series"].append(
            {
                "date": row["day"],
                "runs": runs,
                "errors": errors,
                "error_rate": _rate(errors, runs),
                "cost_usd": cost_usd,
            }
        )
        totals = agent["totals"]
        totals["runs"] += runs
        totals["errors"] += errors
        totals["cost_usd"] = round(totals["cost_usd"] + cost_usd, 6)

    for agent in agents_by_profile.values():
        totals = agent["totals"]
        totals["error_rate"] = _rate(totals["errors"], totals["runs"])

    return {
        "days": selected_days,
        "generated_at": generated_at,
        "agents": list(agents_by_profile.values()),
    }


def register_fleet_history_routes(app: FastAPI) -> None:
    """Register the read-only Fleet agent-history endpoint."""

    @app.get("/api/fleet/agent-history")
    def get_fleet_agent_history(days: int = 30) -> dict[str, Any]:
        return build_agent_history(days=days)

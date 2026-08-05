from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import fleet_history


NOW = int(datetime(2026, 8, 4, 12, tzinfo=timezone.utc).timestamp())


def _timestamp(day: int, hour: int = 12) -> int:
    return int(datetime(2026, 8, day, hour, tzinfo=timezone.utc).timestamp())


def _create_task_runs_db(path: Path, *, with_cost: bool = True) -> None:
    cost_column = ", cost_usd REAL" if with_cost else ""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE task_runs (
                profile TEXT,
                status TEXT,
                started_at INTEGER,
                ended_at INTEGER,
                outcome TEXT
                %s
            )
            """ % cost_column
        )


def _insert_run(
    path: Path,
    *,
    profile: str | None,
    status: str,
    started_at: int,
    outcome: str | None,
    cost_usd: float | None = None,
    with_cost: bool = True,
) -> None:
    columns = "profile, status, started_at, ended_at, outcome"
    values: tuple[object, ...] = (
        profile,
        status,
        started_at,
        started_at + 60,
        outcome,
    )
    if with_cost:
        columns += ", cost_usd"
        values += (cost_usd,)
    placeholders = ", ".join("?" for _ in values)
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"INSERT INTO task_runs ({columns}) VALUES ({placeholders})",
            values,
        )


def test_build_agent_history_aggregates_profile_day_errors_and_costs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _create_task_runs_db(db_path)

    rows = [
        ("coder", "done", 2, None, 0.10),
        ("coder", "failed", 3, None, 0.20),
        ("coder", "done", 3, "gave_up", None),
        ("coder", "crashed", 3, "crashed", 0.12),
        ("reviewer", "done", 4, None, 0.30),
        (None, "timed_out", 4, None, 0.40),
        ("coder", "failed", 1, None, 99.0),
    ]
    for profile, status, day, outcome, cost in rows:
        _insert_run(
            db_path,
            profile=profile,
            status=status,
            started_at=_timestamp(day),
            outcome=outcome,
            cost_usd=cost,
        )

    payload = fleet_history.build_agent_history(db_path=db_path, days=3, now=NOW)

    assert payload["days"] == 3
    assert payload["generated_at"] == NOW
    agents = {agent["profile"]: agent for agent in payload["agents"]}
    assert set(agents) == {"coder", "reviewer", "unbekannt"}
    assert agents["coder"] == {
        "profile": "coder",
        "series": [
            {
                "date": "2026-08-02",
                "runs": 1,
                "errors": 0,
                "error_rate": 0.0,
                "cost_usd": pytest.approx(0.10),
            },
            {
                "date": "2026-08-03",
                "runs": 3,
                "errors": 3,
                "error_rate": 1.0,
                "cost_usd": pytest.approx(0.32),
            },
        ],
        "totals": {
            "runs": 4,
            "errors": 3,
            "error_rate": 0.75,
            "cost_usd": pytest.approx(0.42),
        },
    }
    assert agents["unbekannt"]["series"][0]["errors"] == 1
    assert agents["reviewer"]["series"][0]["cost_usd"] == pytest.approx(0.30)


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (999, 90)])
def test_build_agent_history_clamps_days(
    tmp_path: Path, requested: int, expected: int
) -> None:
    db_path = tmp_path / "kanban.db"
    _create_task_runs_db(db_path)

    payload = fleet_history.build_agent_history(
        db_path=db_path,
        days=requested,
        now=NOW,
    )

    assert payload["days"] == expected


def test_build_agent_history_supports_legacy_db_without_cost_column(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-kanban.db"
    _create_task_runs_db(db_path, with_cost=False)
    _insert_run(
        db_path,
        profile="coder",
        status="done",
        started_at=_timestamp(4),
        outcome=None,
        with_cost=False,
    )

    payload = fleet_history.build_agent_history(db_path=db_path, days=30, now=NOW)

    assert payload["agents"][0]["series"][0]["cost_usd"] == 0.0
    assert payload["agents"][0]["totals"]["cost_usd"] == 0.0


def test_agent_history_route_uses_hermes_home_and_clamps_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "kanban.db"
    _create_task_runs_db(db_path)
    _insert_run(
        db_path,
        profile="coder",
        status="failed",
        started_at=_timestamp(4),
        outcome=None,
        cost_usd=0.25,
    )
    monkeypatch.setattr(fleet_history, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_history.time, "time", lambda: NOW)
    app = FastAPI()
    fleet_history.register_fleet_history_routes(app)

    response = TestClient(app).get("/api/fleet/agent-history?days=120")

    assert response.status_code == 200
    assert response.json() == {
        "days": 90,
        "generated_at": NOW,
        "agents": [
            {
                "profile": "coder",
                "series": [
                    {
                        "date": "2026-08-04",
                        "runs": 1,
                        "errors": 1,
                        "error_rate": 1.0,
                        "cost_usd": 0.25,
                    }
                ],
                "totals": {
                    "runs": 1,
                    "errors": 1,
                    "error_rate": 1.0,
                    "cost_usd": 0.25,
                },
            }
        ],
    }


def test_web_server_registers_agent_history_route() -> None:
    from hermes_cli import web_server

    paths = {getattr(route, "path", None) for route in web_server.app.routes}
    assert "/api/fleet/agent-history" in paths

"""HTTP coverage for the read-only Kanban scorecard route."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("scorecard_routes_test_plugin", plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _insert_score(conn, *, task_id, run_id, name, value, value_type="numeric"):
    conn.execute(
        "INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'test', 1)",
        (run_id, task_id, name, value, value_type),
    )


def _make_run(conn, *, profile="coder", model="test-model", title="scorecard test"):
    task_id = kb.create_task(conn, title=title, assignee=profile)
    run_id = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, active_model) "
        "VALUES (?, ?, 'done', 1, ?)",
        (task_id, profile, model),
    ).lastrowid
    return task_id, run_id


def test_scorecard_aggregates_populated_numeric_scores_and_preserves_review_verdicts(client):
    with kb.connect_closing() as conn:
        task_a, run_a = _make_run(conn, profile="coder", model="model-a")
        task_b, run_b = _make_run(conn, profile="reviewer", model="model-b")
        _insert_score(conn, task_id=task_a, run_id=run_a, name="review_verdict", value=1.0, value_type="binary")
        _insert_score(conn, task_id=task_b, run_id=run_b, name="review_verdict", value=0.0, value_type="binary")
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_cost_usd", value=1.5)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="run_cost_usd", value=2.5)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_cost_effective_usd", value=11.5)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="run_cost_effective_usd", value=22.5)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_duration_seconds", value=3.0)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="run_duration_seconds", value=9.0)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_tokens_total", value=100.0)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="run_tokens_total", value=300.0)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_attempt_index", value=1.0)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="run_attempt_index", value=3.0)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="review_iterations_to_approval", value=0.0)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="review_iterations_to_approval", value=2.0)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_outcome_kind", value="completed", value_type="categorical")

    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"] == {"runs": 2, "approved": 1, "approval_rate": 0.5}
    assert payload["verdicts"] == {"approved": 1, "rejected": 1}
    scores = payload["materialized_scores"]
    assert scores["run_cost_usd"] == {"value": 2.0, "min": 1.5, "max": 2.5, "sum": 4.0, "count": 2}
    assert scores["run_cost_effective_usd"] == {
        "value": 17.0, "min": 11.5, "max": 22.5, "sum": 34.0, "count": 2,
    }
    assert scores["run_duration_seconds"] == {"value": 6.0, "min": 3.0, "max": 9.0, "sum": 12.0, "count": 2}
    assert scores["run_tokens_total"] == {"value": 200.0, "min": 100.0, "max": 300.0, "sum": 400.0, "count": 2}
    assert scores["run_attempt_index"] == {"value": 2.0, "min": 1.0, "max": 3.0, "sum": 4.0, "count": 2}
    assert scores["review_iterations_to_approval"] == {"value": 1.0, "min": 0.0, "max": 2.0, "sum": 2.0, "count": 2}
    assert scores["run_outcome_kind"] == {"value": {"completed": 1}, "count": 1}


def test_scorecard_returns_explicit_empty_materialized_score_entries(client):
    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    assert response.json()["materialized_scores"] == {
        "run_cost_effective_usd": {"value": None, "min": None, "max": None, "sum": None, "count": 0},
        "run_cost_usd": {"value": None, "min": None, "max": None, "sum": None, "count": 0},
        "run_duration_seconds": {"value": None, "min": None, "max": None, "sum": None, "count": 0},
        "run_tokens_total": {"value": None, "min": None, "max": None, "sum": None, "count": 0},
        "run_attempt_index": {"value": None, "min": None, "max": None, "sum": None, "count": 0},
        "review_iterations_to_approval": {"value": None, "min": None, "max": None, "sum": None, "count": 0},
        "run_outcome_kind": {"value": None, "count": 0},
    }


def test_scorecard_keeps_fixture_metrics_and_reports_run_classes(client):
    with kb.connect_closing() as conn:
        real_task, real_run = _make_run(conn)
        fixture_task, fixture_run = _make_run(
            conn,
            profile="w",
            title="time-travel",
        )
        _insert_score(
            conn,
            task_id=real_task,
            run_id=real_run,
            name="review_verdict",
            value=1.0,
            value_type="binary",
        )
        _insert_score(
            conn,
            task_id=real_task,
            run_id=real_run,
            name="run_duration_seconds",
            value=222.0,
        )
        _insert_score(
            conn,
            task_id=fixture_task,
            run_id=fixture_run,
            name="review_verdict",
            value=0.0,
            value_type="binary",
        )
        _insert_score(
            conn,
            task_id=fixture_task,
            run_id=fixture_run,
            name="run_duration_seconds",
            value=0.0,
        )

    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"] == {
        "runs": 2,
        "approved": 1,
        "approval_rate": 0.5,
    }
    assert payload["run_classes"] == {
        "produktiv": 1,
        "fixture": 1,
        "nie_gelaufen": 0,
    }
    assert payload["materialized_scores"]["run_duration_seconds"] == {
        "value": 111.0,
        "min": 0.0,
        "max": 222.0,
        "sum": 222.0,
        "count": 2,
    }
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 4


def test_scorecard_merges_numeric_and_text_run_outcome_kinds(client):
    with kb.connect_closing() as conn:
        task_a, run_a = _make_run(conn)
        task_b, run_b = _make_run(conn)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_outcome_kind", value=1.0)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="run_outcome_kind", value="completed", value_type="categorical")

    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    assert response.json()["materialized_scores"]["run_outcome_kind"] == {
        "value": {"completed": 2}, "count": 2,
    }


def test_scorecard_preserves_unknown_numeric_run_outcome_kind(client):
    with kb.connect_closing() as conn:
        task_id, run_id = _make_run(conn)
        _insert_score(conn, task_id=task_id, run_id=run_id, name="run_outcome_kind", value=99.0)

    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    assert response.json()["materialized_scores"]["run_outcome_kind"] == {
        "value": {"unknown_outcome_code:99.0": 1}, "count": 1,
    }


def test_scorecard_request_does_not_modify_sqlite_database(client, kanban_home):
    db_path = kanban_home / "kanban.db"
    observer = sqlite3.connect(db_path)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        response = client.get("/api/plugins/kanban/scorecard")
        after = observer.execute("PRAGMA data_version").fetchone()[0]
    finally:
        observer.close()

    assert response.status_code == 200
    assert after == before

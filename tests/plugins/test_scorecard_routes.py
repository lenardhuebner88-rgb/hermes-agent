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


def _make_run(conn, *, profile="coder", model="test-model"):
    task_id = kb.create_task(conn, title="scorecard test", assignee=profile)
    run_id = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, active_model) "
        "VALUES (?, ?, 'done', 1, ?)",
        (task_id, profile, model),
    ).lastrowid
    return task_id, run_id


def test_scorecard_aggregates_materialized_scores_and_preserves_review_verdicts(client):
    with kb.connect_closing() as conn:
        task_a, run_a = _make_run(conn, profile="coder", model="model-a")
        task_b, run_b = _make_run(conn, profile="reviewer", model="model-b")
        _insert_score(conn, task_id=task_a, run_id=run_a, name="review_verdict", value=1.0, value_type="binary")
        _insert_score(conn, task_id=task_b, run_id=run_b, name="review_verdict", value=0.0, value_type="binary")
        _insert_score(conn, task_id=task_a, run_id=run_a, name="cache_hit_ratio", value=0.2)
        _insert_score(conn, task_id=task_b, run_id=run_b, name="cache_hit_ratio", value=0.4)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="queue_latency_seconds", value=3.0)
        _insert_score(conn, task_id=task_a, run_id=run_a, name="operator_veto", value=1.0, value_type="binary")
        _insert_score(conn, task_id=task_a, run_id=run_a, name="task_outcome", value="done", value_type="categorical")
        _insert_score(conn, task_id=task_b, run_id=run_b, name="task_outcome", value="blocked", value_type="categorical")
        _insert_score(conn, task_id=task_a, run_id=run_a, name="run_outcome_kind", value="completed", value_type="categorical")

    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"] == {"runs": 2, "approved": 1, "approval_rate": 0.5}
    assert payload["verdicts"] == {"approved": 1, "rejected": 1}
    scores = payload["materialized_scores"]
    assert scores["cache_hit_ratio"] == {
        "value": pytest.approx(0.3), "count": 2,
    }
    assert scores["queue_latency_seconds"] == {"value": 3.0, "count": 1}
    assert scores["operator_veto"] == {"value": 1.0, "count": 1}
    assert scores["task_outcome"] == {
        "value": {"blocked": 1, "done": 1}, "count": 2,
    }
    assert scores["run_outcome_kind"] == {"value": {"completed": 1}, "count": 1}


def test_scorecard_returns_explicit_empty_materialized_score_entries(client):
    response = client.get("/api/plugins/kanban/scorecard")

    assert response.status_code == 200
    assert response.json()["materialized_scores"] == {
        "cache_hit_ratio": {"value": None, "count": 0},
        "queue_latency_seconds": {"value": None, "count": 0},
        "operator_veto": {"value": None, "count": 0},
        "task_outcome": {"value": None, "count": 0},
        "run_outcome_kind": {"value": None, "count": 0},
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

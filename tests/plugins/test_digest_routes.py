"""HTTP coverage for the read-only Kanban weekly-digest route."""
from __future__ import annotations

import importlib.util
import socket
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scores_digest as digest


_FIXED_NOW = 1_784_563_200


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("digest_routes_test_plugin", plugin_file)
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


def _make_run(conn, *, profile="coder", model="test-model"):
    task_id = kb.create_task(conn, title="digest test", assignee=profile)
    run_id = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, active_model) "
        "VALUES (?, ?, 'done', ?, ?)",
        (task_id, profile, _FIXED_NOW, model),
    ).lastrowid
    return task_id, run_id


def _insert_score(conn, *, task_id, run_id, name, value, value_type="numeric"):
    conn.execute(
        "INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'test', ?)",
        (run_id, task_id, name, value, value_type, _FIXED_NOW),
    )


def test_digest_endpoint_matches_existing_calculation(client, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, run_id = _make_run(conn)
        _insert_score(
            conn,
            task_id=task_id,
            run_id=run_id,
            name="task_runs_to_done",
            value=3.0,
        )
        _insert_score(
            conn,
            task_id=task_id,
            run_id=run_id,
            name="queue_latency_seconds",
            value=3600.0,
        )
        _insert_score(
            conn,
            task_id=task_id,
            run_id=run_id,
            name="run_cost_usd",
            value=2.0,
        )
        expected = digest.scores_digest(conn, weeks=2, now=_FIXED_NOW)

    monkeypatch.setattr(digest.time, "time", lambda: _FIXED_NOW)
    response = client.get("/api/plugins/kanban/scores/digest?weeks=2")

    assert response.status_code == 200
    assert response.json() == expected
    assert response.json()["top_findings"]


def test_digest_endpoint_returns_explicit_empty_findings(client):
    response = client.get("/api/plugins/kanban/scores/digest")

    assert response.status_code == 200
    assert response.json()["top_findings"] == []


def test_digest_endpoint_is_read_only_and_does_not_use_network(client, kanban_home, monkeypatch):
    db_path = kanban_home / "kanban.db"
    observer = sqlite3.connect(db_path)

    def fail_network(*args, **kwargs):
        raise AssertionError("digest endpoint must not make network requests")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        response = client.get("/api/plugins/kanban/scores/digest")
        after = observer.execute("PRAGMA data_version").fetchone()[0]
    finally:
        observer.close()

    assert response.status_code == 200
    assert after == before

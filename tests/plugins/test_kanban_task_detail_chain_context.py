"""Contract tests for additive chain context in task detail responses."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    kb.init_db(db_path=home / "kanban.db")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_detail_chain_context_test", plugin_file
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kanban")
    with module._board_cache_lock:
        module._board_cache.clear()
    return TestClient(app)


def test_task_detail_includes_chain_context_and_adjacent_stations(client):
    first = client.post(
        "/api/plugins/kanban/tasks", json={"title": "First station"}
    ).json()["task"]
    middle = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Middle station", "parents": [first["id"]]},
    ).json()["task"]
    last = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Last station", "parents": [middle["id"]]},
    ).json()["task"]
    standalone = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Standalone"}
    ).json()["task"]

    first_context = client.get(
        f"/api/plugins/kanban/tasks/{first['id']}"
    ).json()["task"]["chain_context"]
    middle_context = client.get(
        f"/api/plugins/kanban/tasks/{middle['id']}"
    ).json()["task"]["chain_context"]
    last_context = client.get(
        f"/api/plugins/kanban/tasks/{last['id']}"
    ).json()["task"]["chain_context"]

    assert first_context == {
        "root_id": last["id"],
        "chain_identifier": "First station",
        "chain_state": "laeuft",
        "position": 1,
        "total": 3,
        "previous_station": None,
        "next_station": {"id": middle["id"], "title": "Middle station"},
    }
    assert middle_context == {
        "root_id": last["id"],
        "chain_identifier": "First station",
        "chain_state": "laeuft",
        "position": 2,
        "total": 3,
        "previous_station": {"id": first["id"], "title": "First station"},
        "next_station": {"id": last["id"], "title": "Last station"},
    }
    assert last_context == {
        "root_id": last["id"],
        "chain_identifier": "First station",
        "chain_state": "laeuft",
        "position": 3,
        "total": 3,
        "previous_station": {"id": middle["id"], "title": "Middle station"},
        "next_station": None,
    }
    assert "chain_context" not in client.get(
        f"/api/plugins/kanban/tasks/{standalone['id']}"
    ).json()["task"]

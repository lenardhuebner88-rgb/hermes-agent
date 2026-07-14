"""Dispatch-cap parity for the dashboard's mutating dispatcher surfaces."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db


_MODULE_NAME = "hermes_dashboard_plugin_kanban_dispatch_caps_test"


@pytest.fixture
def plugin_client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        """\
kanban:
  default_assignee: coder
  max_in_progress: 2
  max_spawn: 5
  max_in_progress_per_profile: 4
  max_concurrent_per_repo: 3
  serialize_by_repo: false
""",
        encoding="utf-8",
    )

    db_path = kanban_db.kanban_db_path(board="default")
    kanban_db._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kanban_db.init_db()

    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    sys.modules.pop(_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, plugin_file)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = plugin
    spec.loader.exec_module(plugin)

    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")
    try:
        with TestClient(app) as client:
            yield plugin, client
    finally:
        sys.modules.pop(_MODULE_NAME, None)


def _dispatch_spy(monkeypatch, plugin):
    calls = []

    def fake_dispatch_once(conn, **kwargs):
        calls.append(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(plugin.kanban_db, "dispatch_once", fake_dispatch_once)
    return calls


def _assert_config_caps(kwargs, *, max_spawn):
    assert kwargs["default_assignee"] == "coder"
    assert kwargs["max_in_progress"] == 2
    assert kwargs["max_spawn"] == max_spawn
    assert kwargs["max_in_progress_per_profile"] == 4
    assert kwargs["max_concurrent_per_repo"] == 3
    assert kwargs["serialize_by_repo"] is False


def test_dispatch_endpoint_passes_config_caps_with_max_override(
    plugin_client, monkeypatch
):
    plugin, client = plugin_client
    calls = _dispatch_spy(monkeypatch, plugin)

    response = client.post(
        "/api/plugins/kanban/dispatch?dry_run=true&max=7"
    )

    assert response.status_code == 200
    assert len(calls) == 1
    _assert_config_caps(calls[0], max_spawn=7)
    assert calls[0]["dry_run"] is True


@pytest.mark.parametrize("max_n", [0, 33])
def test_dispatch_endpoint_bounds_max(plugin_client, monkeypatch, max_n):
    plugin, client = plugin_client
    calls = _dispatch_spy(monkeypatch, plugin)

    response = client.post(f"/api/plugins/kanban/dispatch?max={max_n}")

    assert response.status_code == 422
    assert calls == []


def test_worker_dispatch_action_passes_config_caps(plugin_client, monkeypatch):
    plugin, client = plugin_client
    calls = _dispatch_spy(monkeypatch, plugin)

    response = client.post(
        "/api/plugins/kanban/workers/0/action",
        json={"action": "dispatch", "confirm": True},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(calls) == 1
    _assert_config_caps(calls[0], max_spawn=5)


def test_worker_restart_passes_config_caps(plugin_client, monkeypatch):
    plugin, client = plugin_client
    calls = _dispatch_spy(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.kanban_db,
        "get_run",
        lambda conn, run_id: SimpleNamespace(task_id="t_restart"),
    )
    monkeypatch.setattr(
        plugin.kanban_db,
        "reclaim_task",
        lambda conn, task_id, reason: True,
    )

    response = client.post(
        "/api/plugins/kanban/workers/17/action",
        json={"action": "restart", "confirm": True},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(calls) == 1
    _assert_config_caps(calls[0], max_spawn=5)

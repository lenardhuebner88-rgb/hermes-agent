"""API test for GET /release-status (read-only auto-release status).

Uses the same bare-router TestClient harness as
test_kanban_disposition_endpoints.py.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_release_status_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix=PREFIX)
    return TestClient(app)


def test_release_status_default_config(client):
    """Default config.yaml has no ``release`` block → autonomous stays False."""
    resp = client.get(f"{PREFIX}/release-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["autonomous"] is False
    assert isinstance(body["recent"], list)
    assert isinstance(body["anchors"], list)


def test_release_status_surfaces_recent_auto_release_event(client):
    """A recorded ``auto_release`` timeline event appears in ``recent``."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Fake auto-released task")
        kb.add_event(conn, task_id, "auto_release", {"deployed": True, "anchor": "release/pre-deploy/20260705T000000"})

    resp = client.get(f"{PREFIX}/release-status")
    assert resp.status_code == 200
    body = resp.json()
    task_ids = [row["task_id"] for row in body["recent"]]
    assert task_id in task_ids
    matching = next(row for row in body["recent"] if row["task_id"] == task_id)
    assert matching["task_title"] == "Fake auto-released task"
    assert matching["payload"]["deployed"] is True

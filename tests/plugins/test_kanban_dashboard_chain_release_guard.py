"""Regression coverage for fail-closed starts of held PlanSpec chains."""

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
        "hermes_dashboard_plugin_chain_release_guard_test", plugin_file,
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


def test_held_chain_member_cannot_be_started_individually(client):
    """Only the chain-wide release path may clear an operator-held chain."""
    with kb.connect() as conn:
        root_id = kb.create_task(conn, title="held root", assignee="coder")
        child_one_id = kb.create_task(conn, title="held child one", assignee="coder")
        child_two_id = kb.create_task(conn, title="held child two", assignee="coder")
        # A decomposed PlanSpec root waits on its build children.  This is the
        # real topology traversed by release_freigabe_hold.
        kb.link_tasks(conn, parent_id=child_one_id, child_id=root_id)
        kb.link_tasks(conn, parent_id=child_two_id, child_id=root_id)
        free_id = kb.create_task(conn, title="free task", assignee="coder")
        conn.execute(
            "UPDATE tasks SET status = 'scheduled', freigabe = 'operator' "
            "WHERE id = ?",
            (root_id,),
        )
        conn.execute(
            "UPDATE tasks SET status = 'scheduled' WHERE id IN (?, ?, ?)",
            (child_one_id, child_two_id, free_id),
        )
        conn.commit()

    held = client.patch(f"{PREFIX}/tasks/{child_one_id}", json={"status": "ready"})
    assert held.status_code == 409
    assert root_id in held.json()["detail"]
    assert "Freigabe der ganzen Kette" in held.json()["detail"]

    with kb.connect() as conn:
        child_one = kb.get_task(conn, child_one_id)
        root = kb.get_task(conn, root_id)
        child_two = kb.get_task(conn, child_two_id)
        assert child_one is not None and child_one.status == "scheduled"
        assert root is not None and root.status == "scheduled"
        assert child_two is not None and child_two.status == "scheduled"

    free = client.patch(f"{PREFIX}/tasks/{free_id}", json={"status": "ready"})
    assert free.status_code == 200
    with kb.connect() as conn:
        free_task = kb.get_task(conn, free_id)
        assert free_task is not None and free_task.status == "ready"
        assert kb.release_freigabe_hold(conn, root_id)

    released = client.patch(f"{PREFIX}/tasks/{child_one_id}", json={"status": "ready"})
    assert released.status_code == 200

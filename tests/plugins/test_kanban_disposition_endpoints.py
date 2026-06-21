"""API tests for disposition-item lifecycle endpoints (FRD Phase 3b).

Uses the same bare-router TestClient harness as test_kanban_strategist_endpoint.py.

Covers:
  GET  /disposition-items              — list open items (+ status=all)
  POST /disposition-items/{id}/accept  — accept, 404 on missing
  POST /disposition-items/{id}/dismiss — dismiss + reason, 404 on missing
  POST /disposition-items/{id}/create-fix-task — fix-task, 409 on non-open, 404 on missing
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
        "hermes_dashboard_plugin_kanban_disposition_test", plugin_file,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_open_item(source_task_id: str = "t_test") -> str:
    """Insert a disposition item and return its id."""
    with kb.connect() as conn:
        return kb.insert_disposition_item(
            conn,
            source_task_id=source_task_id,
            typ="risk",
            disposition="delegate",
            next_action="Validate input at boundary",
            severity="real-risk",
            evidence="src/foo.py:10",
        )


def _make_source_task() -> str:
    """Create a done source task and return its id."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Source task for disposition")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    return tid


def _item_status(item_id: str) -> str:
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status FROM disposition_items WHERE id=?", (item_id,)
        ).fetchone()
    return row["status"]


# ===========================================================================
# GET /disposition-items
# ===========================================================================


def test_list_returns_open_items_by_default(client):
    """GET /disposition-items returns open items (default status=open)."""
    iid = _make_open_item()
    r = client.get(f"{PREFIX}/disposition-items")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "items" in data
    ids = [item["id"] for item in data["items"]]
    assert iid in ids


def test_list_excludes_dismissed_when_status_open(client):
    """Default status=open excludes dismissed items."""
    iid = _make_open_item()
    # Dismiss via the API
    client.post(f"{PREFIX}/disposition-items/{iid}/dismiss", json={"reason": ""})
    r = client.get(f"{PREFIX}/disposition-items")
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert iid not in ids


def test_list_status_all_returns_every_item(client):
    """status=all returns items regardless of lifecycle state."""
    tid = _make_source_task()
    iid = _make_open_item(source_task_id=tid)
    client.post(f"{PREFIX}/disposition-items/{iid}/dismiss", json={"reason": ""})

    r = client.get(f"{PREFIX}/disposition-items", params={"status": "all"})
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert iid in ids


# ===========================================================================
# POST /disposition-items/{id}/accept
# ===========================================================================


def test_accept_sets_status_accepted(client):
    """POST accept → item status becomes accepted."""
    iid = _make_open_item()
    r = client.post(f"{PREFIX}/disposition-items/{iid}/accept")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["item"]["status"] == "accepted"
    assert data["item"]["id"] == iid


def test_accept_nonexistent_item_returns_404(client):
    """POST accept on unknown id → 404."""
    r = client.post(f"{PREFIX}/disposition-items/di_nope/accept")
    assert r.status_code == 404


# ===========================================================================
# POST /disposition-items/{id}/dismiss
# ===========================================================================


def test_dismiss_sets_status_dismissed(client):
    """POST dismiss → item status becomes dismissed."""
    iid = _make_open_item()
    r = client.post(f"{PREFIX}/disposition-items/{iid}/dismiss", json={"reason": ""})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["status"] == "dismissed"


def test_dismiss_with_reason(client):
    """POST dismiss with reason → 200, reason accepted."""
    tid = _make_source_task()
    iid = _make_open_item(source_task_id=tid)
    r = client.post(
        f"{PREFIX}/disposition-items/{iid}/dismiss",
        json={"reason": "Not relevant anymore"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["item"]["status"] == "dismissed"


def test_dismiss_nonexistent_item_returns_404(client):
    """POST dismiss on unknown id → 404."""
    r = client.post(f"{PREFIX}/disposition-items/di_nope/dismiss", json={"reason": ""})
    assert r.status_code == 404


# ===========================================================================
# POST /disposition-items/{id}/create-fix-task
# ===========================================================================


def test_create_fix_task_returns_triage_task(client):
    """POST create-fix-task → fix_task in triage, item in task_created."""
    iid = _make_open_item()
    r = client.post(f"{PREFIX}/disposition-items/{iid}/create-fix-task")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "fix_task" in data
    assert "item" in data
    assert data["fix_task"]["status"] == "triage"
    assert data["item"]["status"] == "task_created"
    assert data["item"]["id"] == iid


def test_create_fix_task_on_non_open_item_returns_409(client):
    """POST create-fix-task on dismissed item → 409."""
    iid = _make_open_item()
    client.post(f"{PREFIX}/disposition-items/{iid}/dismiss", json={"reason": ""})

    r = client.post(f"{PREFIX}/disposition-items/{iid}/create-fix-task")
    assert r.status_code == 409


def test_create_fix_task_nonexistent_item_returns_404(client):
    """POST create-fix-task on unknown id → 404."""
    r = client.post(f"{PREFIX}/disposition-items/di_nope/create-fix-task")
    assert r.status_code == 404

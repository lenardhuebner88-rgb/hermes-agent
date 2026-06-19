"""Tests for the Strategist surface REST endpoints (G1).

Mirrors the bare-router harness of test_kanban_dashboard_plugin.py: the kanban
plugin router is attached to a stand-alone FastAPI app mounted at the real
prefix so we exercise the actual REST surface in isolation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import strategist_surface as ss

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_strategist_test", plugin_file,
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


def _make_held_chain(*, annotate: bool = True) -> tuple[str, str]:
    """Create a held freigabe:operator root + one held child (decompose link
    direction). Returns (root_id, child_id)."""
    body = "Strategist lever proposal."
    if annotate:
        body += "\n\n" + ss.format_annotation(
            target_metric="Autonomie-% 62 → 75",
            roi="hoch",
            counter_metric="Fehl-Eskalations-Rate < 5%",
        )
    with kb.connect() as conn:
        root_id = kb.create_task(conn, title="Lever", body=body, assignee="coder-claude")
        child_id = kb.create_task(conn, title="Build lever", assignee="coder-claude")
        kb.link_tasks(conn, parent_id=child_id, child_id=root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
                (root_id,),
            )
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (child_id,))
    return root_id, child_id


def _status(task_id: str) -> str:
    with kb.connect() as conn:
        return conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["status"]


# ---------------------------------------------------------------------------
# GET /strategist/proposals
# ---------------------------------------------------------------------------


def test_list_returns_held_proposals_with_annotations(client):
    root_id, _ = _make_held_chain(annotate=True)
    r = client.get(f"{PREFIX}/strategist/proposals")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    p = data["proposals"][0]
    assert p["id"] == root_id
    assert p["target_metric"] == "Autonomie-% 62 → 75"
    assert p["roi"] == "hoch"
    assert p["counter_metric"] == "Fehl-Eskalations-Rate < 5%"
    assert p["subtask_count"] == 1
    # No snapshot written yet → null, but the key is present as triage context.
    assert "metrics" in data and data["metrics"] is None


def test_list_includes_metric_snapshot_when_present(client):
    _make_held_chain()
    path = ss.vision_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"autonomy_pct": 71, "green_gate_streak": 3}), encoding="utf-8")
    r = client.get(f"{PREFIX}/strategist/proposals")
    assert r.status_code == 200
    assert r.json()["metrics"] == {"autonomy_pct": 71, "green_gate_streak": 3}


def test_list_empty_when_no_held_proposals(client):
    r = client.get(f"{PREFIX}/strategist/proposals")
    assert r.status_code == 200
    data = r.json()
    assert data["proposals"] == []
    assert data["count"] == 0
    assert data["metrics"] is None
    assert isinstance(data["checked_at"], int)


def test_list_emits_weak_etag_and_304_roundtrip(client):
    _make_held_chain()
    r1 = client.get(f"{PREFIX}/strategist/proposals")
    etag = r1.headers.get("etag")
    assert etag and etag.startswith('W/"')
    assert r1.headers.get("cache-control") == "private, no-cache"
    r2 = client.get(f"{PREFIX}/strategist/proposals", headers={"If-None-Match": etag})
    assert r2.status_code == 304


# ---------------------------------------------------------------------------
# POST approve / veto
# ---------------------------------------------------------------------------


def test_approve_releases_held_chain(client):
    root_id, child_id = _make_held_chain()
    assert _status(root_id) == "scheduled"
    r = client.post(f"{PREFIX}/strategist/proposals/{root_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["released"] is True
    # Root left the hold; child is promoted out of 'scheduled'.
    assert _status(root_id) == "todo"
    assert _status(child_id) != "scheduled"


def test_veto_archives_root_and_children(client):
    root_id, child_id = _make_held_chain()
    r = client.post(f"{PREFIX}/strategist/proposals/{root_id}/veto")
    assert r.status_code == 200, r.text
    assert r.json()["vetoed"] is True
    assert _status(root_id) == "archived"
    assert _status(child_id) == "archived"
    # Vetoed proposal no longer appears in the list.
    listing = client.get(f"{PREFIX}/strategist/proposals").json()
    assert listing["count"] == 0


def test_approve_non_root_child_is_rejected(client):
    _root_id, child_id = _make_held_chain()
    # The held BUILD child carries no freigabe → not a proposal.
    r = client.post(f"{PREFIX}/strategist/proposals/{child_id}/approve")
    assert r.status_code == 409


def test_veto_non_root_child_is_rejected(client):
    _root_id, child_id = _make_held_chain()
    r = client.post(f"{PREFIX}/strategist/proposals/{child_id}/veto")
    assert r.status_code == 409
    # The child stays held — the guard refused to touch it.
    assert _status(child_id) == "scheduled"


def test_approve_unknown_task_is_rejected(client):
    r = client.post(f"{PREFIX}/strategist/proposals/t_does_not_exist/approve")
    assert r.status_code == 409


def test_veto_already_released_chain_is_rejected(client):
    root_id, _ = _make_held_chain()
    assert client.post(f"{PREFIX}/strategist/proposals/{root_id}/approve").status_code == 200
    # Once released (todo, building) a veto must NOT silently tear it down.
    r = client.post(f"{PREFIX}/strategist/proposals/{root_id}/veto")
    assert r.status_code == 409
    assert _status(root_id) == "todo"

"""Fork-side coverage for the per-task model/provider override HTTP surface.

Why this file exists next to upstream's ``test_kanban_model_override.py``:

Upstream's five dashboard-API tests build their tasks with
``assignee="worker"``. The fork validates the assignee against the on-disk
Hermes profiles and rejects unknown ones ("Assignee 'worker' is not
spawnable: no on-disk Hermes profile"), so those five fail at the *create*
step before they ever exercise the override. That is a deliberate fork
guard, not a defect in the override work — and editing upstream's test file
to accommodate it would add exactly the merge burden this workstream exists
to remove.

So the behaviour is asserted here instead, under the fork's own contract
(``assignee="default"``). Same five flows, same assertions. If upstream's
five ever go green, this file is redundant and can be deleted.

Fixtures are reused from upstream's module so the two stay in lockstep:
one board setup, one router loader, no drift.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_UPSTREAM_TESTS = (
    Path(__file__).resolve().parent / "test_kanban_model_override.py"
)
_spec = importlib.util.spec_from_file_location(
    "_kanban_model_override_upstream", _UPSTREAM_TESTS
)
_up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_up)

# Re-export so pytest resolves them as fixtures in this module's scope.
kanban_home = _up.kanban_home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_up._load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _create(client, **kwargs):
    """Create via the API with an assignee the fork's guard accepts."""
    body = {"title": "task", "assignee": "default"}
    body.update(kwargs)
    r = client.post("/api/plugins/kanban/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task"]


def test_patch_sets_model_and_provider_override(client):
    task = _create(client)
    r = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"model_override": "gpt-5.6-sol", "provider_override": "openai"},
    )
    assert r.status_code == 200, r.text
    updated = r.json()["task"]
    assert updated["model_override"] == "gpt-5.6-sol"
    assert updated["provider_override"] == "openai"


def test_patch_clear_flag_clears_both(client):
    task = _create(
        client, model_override="gpt-5.6-sol", provider_override="openai"
    )
    assert task["model_override"] == "gpt-5.6-sol"
    r = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"clear_model_override": True},
    )
    assert r.status_code == 200, r.text
    updated = r.json()["task"]
    assert updated["model_override"] is None
    assert updated["provider_override"] is None


def test_patch_provider_without_model_is_400(client):
    task = _create(client)
    r = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"model_override": "", "provider_override": "openai"},
    )
    assert r.status_code == 400, r.text


def test_create_carries_model_and_provider(client):
    task = _create(
        client, model_override="qwen-max", provider_override="openrouter"
    )
    assert task["model_override"] == "qwen-max"
    assert task["provider_override"] == "openrouter"


def test_bulk_applies_override_to_every_id(client):
    a = _create(client)
    b = _create(client)
    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "model_override": "glm-5",
            "provider_override": "nous",
        },
    )
    assert r.status_code == 200, r.text
    assert all(e["ok"] for e in r.json()["results"]), r.text
    for tid in (a["id"], b["id"]):
        got = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert got["model_override"] == "glm-5"
        assert got["provider_override"] == "nous"

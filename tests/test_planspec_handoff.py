"""ATH-S5: terminal-selection → PlanSpec/Kanban handoff backend.

Covers the materialise-then-reuse pipeline:
  * ``hermes_cli.terminal_handoff`` slug + draft-write helpers
  * ``POST /api/plugins/kanban/planspecs/validate``  → reuses the EXISTING
    deterministic validator; a non-binding default draft comes back invalid
    (the AC-3/AC-7 "validation failure" path), a structurally binding draft
    parses past the binding gate.
  * ``POST /api/plugins/kanban/planspecs/ingest-draft`` → delegates to the
    EXISTING ``ingest_planspec`` (mocked here); blocks surface as 400 findings.

Nothing here dispatches: ingest produces a held chain, never a spawn.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import planspecs, terminal_handoff


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[1]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    mod_name = "hermes_dashboard_plugin_kanban_handoff_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name].router
    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def plans_root(tmp_path, monkeypatch):
    """Redirect the whole PlanSpec plans root at a tmp dir."""
    root = tmp_path / "03-Agents"
    root.mkdir()
    monkeypatch.setattr(planspecs, "DEFAULT_PLANS_ROOT", root)
    return root


@pytest.fixture
def client(plans_root):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


_BINDING_DRAFT = """\
---
title: "Handoff binding draft"
type: planspec
agent: Hermes
status: draft
freigabe: operator
live_test_depth: smoke
topic: "Handoff binding draft"
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "Do the captured thing"
      lane: coder
      deps: []
---

## Kontext (aus Terminal)

captured terminal text goes here
"""

_NON_BINDING_DRAFT = """\
---
title: "Handoff freeform draft"
type: planspec
agent: Hermes
status: draft
freigabe: operator
live_test_depth: smoke
topic: "Handoff freeform draft"
taskgraph_hints:
  binding: false
---

## Kontext (aus Terminal)

just some captured text, no structure yet
"""


# ---------------------------------------------------------------------------
# terminal_handoff helpers
# ---------------------------------------------------------------------------

def test_slugify_sanitises_and_never_empty():
    assert terminal_handoff.slugify("Fix the Login Bug!!") == "fix-the-login-bug"
    assert terminal_handoff.slugify("   ") == "draft"
    assert terminal_handoff.slugify("", fallback="x") == "x"
    # Bounded length, no trailing dashes.
    long = terminal_handoff.slugify("a" * 200)
    assert len(long) <= 60 and not long.endswith("-")


def test_write_handoff_draft_lands_under_plans_root(plans_root):
    path = terminal_handoff.write_handoff_draft(
        "hello", slug="my draft", plans_root=plans_root
    )
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "hello"
    assert path.suffix == ".md"
    # Lives in the dedicated handoff subdir, under the plans root.
    assert terminal_handoff.HANDOFF_SUBDIR.as_posix() in path.as_posix()
    assert plans_root in path.parents


def test_write_handoff_draft_is_idempotent_per_slug(plans_root):
    p1 = terminal_handoff.write_handoff_draft("v1", slug="dupe", plans_root=plans_root)
    p2 = terminal_handoff.write_handoff_draft("v2", slug="dupe", plans_root=plans_root)
    assert p1 == p2
    assert p2.read_text(encoding="utf-8") == "v2"
    assert len(list(p2.parent.glob("*.md"))) == 1


# ---------------------------------------------------------------------------
# POST /planspecs/validate — reuses the existing deterministic validator
# ---------------------------------------------------------------------------

def test_validate_route_flags_non_binding_draft(client, plans_root):
    """AC-7 validation-failure: the freeform default (binding:false) is invalid."""
    resp = client.post(
        "/api/plugins/kanban/planspecs/validate",
        json={"content": _NON_BINDING_DRAFT, "slug": "freeform"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["disposition"] == "invalid"
    assert data["would_block"] is True
    assert any("binding" in f for f in data["findings"])
    # The draft was materialised under the (tmp) plans root, nothing else.
    assert (plans_root / terminal_handoff.HANDOFF_SUBDIR / "freeform.md").is_file()


def test_validate_route_accepts_structurally_binding_draft(client):
    """AC-3/AC-4: a binding draft parses past the binding gate via the real
    validator (disposition may be clean/warn/block, but never the structural
    'invalid')."""
    resp = client.post(
        "/api/plugins/kanban/planspecs/validate",
        json={"content": _BINDING_DRAFT, "slug": "binding"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["disposition"] in {"clean", "warn", "block"}
    assert not any("taskgraph_hints.binding must be true" in f for f in data["findings"])
    assert data["freigabe"] == "operator"


# ---------------------------------------------------------------------------
# POST /planspecs/ingest-draft — delegates to the existing ingest path
# ---------------------------------------------------------------------------

def test_ingest_draft_route_success_mocked(client, plans_root):
    """AC-7 successful ingest (mocked): the route materialises the draft then
    hands a real path to the EXISTING ingest_planspec; no DB writes of its own."""
    fake = {
        "ok": True,
        "already_ingested": False,
        "path": "x",
        "root_task_id": "t_root123",
        "child_ids": ["t_c1", "t_c2"],
        "freigabe": "operator",
        "live_test_depth": "smoke",
        "subtask_count": 2,
    }
    with patch.object(planspecs, "ingest_planspec", return_value=fake) as m:
        resp = client.post(
            "/api/plugins/kanban/planspecs/ingest-draft",
            json={"content": _BINDING_DRAFT, "slug": "ing", "author": "dashboard"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["root_task_id"] == "t_root123"
    assert body["child_ids"] == ["t_c1", "t_c2"]
    # ingest_planspec was called with the materialised path under the tmp root.
    called_path = Path(m.call_args.args[0])
    assert called_path.is_file()
    assert plans_root in called_path.parents
    assert m.call_args.kwargs.get("author") == "dashboard"


def test_ingest_draft_route_blocked_returns_400(client):
    """A blocking spec surfaces as 400 with findings (same contract as ingest)."""
    blocked = planspecs.PlanSpecBlocked(["live_test_depth must be set", "freigabe is required"])
    with patch.object(planspecs, "ingest_planspec", side_effect=blocked):
        resp = client.post(
            "/api/plugins/kanban/planspecs/ingest-draft",
            json={"content": _BINDING_DRAFT},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["findings"] == [
        "live_test_depth must be set",
        "freigabe is required",
    ]

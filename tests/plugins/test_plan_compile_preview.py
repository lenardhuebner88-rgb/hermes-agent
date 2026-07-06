from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from hermes_cli import kanban_db as kb


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    mod_name = "hermes_dashboard_plugin_plan_compile_preview_test"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin_module(tmp_path, monkeypatch):
    from hermes_cli import profiles

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb.init_db()
    return _load_plugin_module()


def test_compile_preview_endpoint(plugin_module):
    prose = """# Preview Plan
**Goal:** Compile a prose plan in the dashboard.

## Slice: Parse text
- done-when: Parser yields a ProsePlan.

## Slice: Fill gaps
"""

    assert "/planspecs/compile-preview" in {
        route.path for route in plugin_module.router.routes
    }

    payload = plugin_module.compile_planspec_preview(
        plugin_module.PlanSpecCompilePreviewBody(prose=prose)
    )

    assert payload["ok"] is True
    assert [child["title"] for child in payload["children"]] == ["Parse text", "Fill gaps"]
    assert payload["children"][1]["parents"] == [0]
    assert any("lane missing" in item for item in payload["repairs"])
    assert any("ambiguous slice" in item for item in payload["warnings"])


def test_ingest_prose_endpoint(plugin_module):
    prose = """# Ingest Plan
**Goal:** Create a held kanban chain.

## Slice: Ship composer
- done-when: Chain exists.
"""

    assert "/planspecs/ingest-prose" in {
        route.path for route in plugin_module.router.routes
    }

    payload = plugin_module.ingest_prose_planspec(
        plugin_module.PlanSpecProseIngestBody(prose=prose, author="pytest"),
        board=None,
    )

    assert payload["ok"] is True
    assert payload["subtask_count"] == 1
    assert payload["freigabe"] == "operator"
    assert payload["initial_child_status"] == "scheduled"
    source_path = Path(payload["path"])
    assert source_path.name.startswith("dashboard-prose-")
    assert source_path.read_text(encoding="utf-8") == prose

    with kb.connect_closing() as conn:
        root = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id = ?",
            (payload["root_task_id"],),
        ).fetchone()
        child = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (payload["child_ids"][0],),
        ).fetchone()

    assert root["status"] == "scheduled"
    assert root["freigabe"] == "operator"
    assert child["status"] == "scheduled"


def test_ingest_prose_endpoint_sofort_preserves_dispatchable_behavior(plugin_module):
    prose = """# Ingest Plan Sofort
**Goal:** Create the pre-existing immediate kanban chain.

## Slice: Ship immediately
- done-when: Chain can run without operator approval.
"""

    payload = plugin_module.ingest_prose_planspec(
        plugin_module.PlanSpecProseIngestBody(
            prose=prose,
            author="pytest",
            freigabe="sofort",
        ),
        board=None,
    )

    assert payload["ok"] is True
    assert payload["freigabe"] == "complete"
    assert payload["initial_child_status"] == "todo"

    with kb.connect_closing() as conn:
        root = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id = ?",
            (payload["root_task_id"],),
        ).fetchone()
        child = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (payload["child_ids"][0],),
        ).fetchone()

    assert root["status"] == "todo"
    assert root["freigabe"] == "complete"
    assert child["status"] == "todo"


def _prose_list(plugin_module):
    """Call the real ``GET /planspecs`` handler wired to the dashboard prose dir.

    Mirrors the endpoint's own wiring (``prose_plans_root=get_hermes_home() /
    "dashboard" / "prose-plans"``) instead of reinventing it — a regression in
    the endpoint's own glue would otherwise go undetected by this test.
    """
    from hermes_constants import get_hermes_home

    return plugin_module.list_planspecs(
        scope="open",
        valid=None,
        limit=None,
        q=None,
        board=None,
    ), get_hermes_home() / "dashboard" / "prose-plans"


def test_prose_ingest_operator_hold_is_pending_and_approve_clears_it(plugin_module):
    """E2E: an operator-held prose ingest must surface in scope=open — the bug
    fixed here — and disappear once approved.

    Red on 63eb04fe1: ``list_planspecs`` never globbed the dashboard prose
    dir, so ``planspecs["planspecs"]`` never contained this record at all —
    the first assertion (finding it) raised ``StopIteration``.
    """
    prose = """# Ingest Plan Pending
**Goal:** Create a held kanban chain the operator can release from the UI.

## Slice: Ship composer
- done-when: Chain exists.
"""
    ingest = plugin_module.ingest_prose_planspec(
        plugin_module.PlanSpecProseIngestBody(prose=prose, author="pytest"),
        board=None,
    )
    assert ingest["freigabe"] == "operator"

    listing, _ = _prose_list(plugin_module)
    record = next((r for r in listing["planspecs"] if r["path"] == ingest["path"]), None)
    assert record is not None, "operator-held prose ingest must appear in scope=open"

    # What the frontend's planSpecAwaitsPlanAction(record) needs to classify
    # this as pending (fleetHub.ts: freigabe==='operator' && state in
    # {'queued','not_ingested'}), plus the fields PlanTab/buildApproveRequest
    # read off the record to build the approve POST body.
    assert record["open"] is True
    assert record["freigabe"] == "operator"
    assert record["kanban_state"] in {"queued", "not_ingested"}
    assert record["kanban_root_task_id"] == ingest["root_task_id"]
    assert record["topic"] == "Ingest Plan Pending"

    approve = plugin_module.approve_planspec(
        plugin_module.PlanSpecApproveBody(root_task_id=ingest["root_task_id"]),
        board=None,
    )
    assert approve["released"] is True

    with kb.connect_closing() as conn:
        root = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id = ?",
            (ingest["root_task_id"],),
        ).fetchone()
    assert root["status"] == "todo"
    assert root["freigabe"] == "operator"

    listing_after, _ = _prose_list(plugin_module)
    assert all(r["path"] != ingest["path"] for r in listing_after["planspecs"]), (
        "released prose chain must not linger in the pending (scope=open) list"
    )


# ---------------------------------------------------------------------------
# GET /planspecs/detail on a dashboard prose-plan source
#
# Red on the pre-fix tree: the detail endpoint called parse_binding_planspec
# directly, which resolves ``path`` against DEFAULT_PLANS_ROOT (the vault) —
# a dashboard prose file lives under get_hermes_home()/dashboard/prose-plans/
# instead, so ``_is_relative_to`` failed and every selection 400'd with
# "planspec path must be under the allowed plans directory", never reaching
# parse_prose_plan. Fixed by trying parse_prose_plan_detail first.
# ---------------------------------------------------------------------------


def test_planspec_detail_endpoint_on_ingested_prose_path_returns_200(plugin_module):
    prose = """# Detail Plan
**Goal:** Show the raw prose in the Volltext drawer.

## Slice: Render markdown
- lane: coder
- done-when: Drawer shows the raw markdown text.
"""
    ingest = plugin_module.ingest_prose_planspec(
        plugin_module.PlanSpecProseIngestBody(prose=prose, author="pytest"),
        board=None,
    )
    source_path = ingest["path"]

    payload = plugin_module.get_planspec_detail(path=source_path)

    assert payload["goal"] == "Show the raw prose in the Volltext drawer."
    assert payload["prose_plan"] is True
    assert payload["full_text"] == prose
    assert [s["title"] for s in payload["subtasks"]] == ["Render markdown"]
    # Binding-only fields stay empty/falsy — no frontmatter to source them from.
    assert payload["acceptance_criteria"] == []
    assert payload["anti_scope"] == []


def test_planspec_detail_endpoint_path_outside_both_roots_still_blocks(plugin_module):
    """Containment regression: a path under NEITHER the vault root NOR the
    dashboard prose-plans dir must still 400, exactly as before this fix."""
    with pytest.raises(HTTPException) as exc_info:
        plugin_module.get_planspec_detail(path="/etc/passwd")
    assert exc_info.value.status_code == 400


def test_prose_ingest_sofort_never_appears_as_pending(plugin_module):
    prose = """# Ingest Plan Immediate
**Goal:** Create the pre-existing immediate kanban chain.

## Slice: Ship immediately
- done-when: Chain can run without operator approval.
"""
    ingest = plugin_module.ingest_prose_planspec(
        plugin_module.PlanSpecProseIngestBody(prose=prose, author="pytest", freigabe="sofort"),
        board=None,
    )
    assert ingest["freigabe"] == "complete"

    listing, _ = _prose_list(plugin_module)
    assert all(r["path"] != ingest["path"] for r in listing["planspecs"]), (
        "a freigabe:sofort prose ingest must never surface as awaiting operator release"
    )

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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

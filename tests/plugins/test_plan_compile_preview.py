from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


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
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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

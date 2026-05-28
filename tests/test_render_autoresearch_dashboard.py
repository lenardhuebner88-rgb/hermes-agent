from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_autoresearch_dashboard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("render_autoresearch_dashboard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_renderer_emits_dashboard_html():
    module = load_module()
    rc = module.main()
    assert rc == 0
    assert module.OUTPUT.exists()
    text = module.OUTPUT.read_text(encoding="utf-8")
    assert "Hermes Autoresearch Dashboard" in text


def test_dashboard_includes_inventory_metrics():
    module = load_module()
    module.main()
    text = module.OUTPUT.read_text(encoding="utf-8")
    assert "Inventoried skills" in text
    assert "High priority" in text
    assert "Iterations logged" in text


def test_dashboard_includes_embedded_autoresearch_json():
    module = load_module()
    module.main()
    text = module.OUTPUT.read_text(encoding="utf-8")
    assert "data-autoresearch=\"dashboard-v1\"" in text
    assert "autoresearch-dashboard-data-v1" in text
    assert "recommended_actions" in text


def test_dashboard_all_area_count_matches_total_rows():
    module = load_module()
    rows = [
        {"priority": "high", "area": "all", "weaknesses": []},
        {"priority": "medium", "area": "github", "weaknesses": []},
    ]
    data = module.build_dashboard_data(rows, [], {})
    assert data["area_counts"]["all"] == 2
    assert data["area_counts"]["github"] == 1

    module.main()
    text = module.OUTPUT.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="data-autoresearch">(.+?)</script>', text)
    assert match
    embedded = json.loads(match.group(1))
    assert embedded["area_counts"]["all"] == int(embedded["inventory_summary"]["Total SKILL.md files inventoried"])


def test_area_from_path_classifies_kanban_before_broad_devops():
    module = load_module()
    assert module.area_from_path("/home/piet/.hermes/skills/devops/kanban-orchestrator/SKILL.md") == "hermes-kanban"
    assert module.area_from_path("/home/piet/.hermes/skills/hermes-kanban/kanban-review-operations/SKILL.md") == "hermes-kanban"


def test_dashboard_has_no_post_mutation_forms():
    module = load_module()
    module.main()
    text = module.OUTPUT.read_text(encoding="utf-8").lower()
    assert 'method="post"' not in text
    assert "<form" not in text
    assert "fetch(" not in text


def test_dashboard_includes_operator_go_safety_copy():
    module = load_module()
    module.main()
    text = module.OUTPUT.read_text(encoding="utf-8")
    assert "requires operator Go" in text
    assert "no provider routing change" in text
    assert "no runtime mutation" in text

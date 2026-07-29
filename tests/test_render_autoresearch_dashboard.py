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

    # Seed an inventory file whose declared total matches the "all" area count
    # the dashboard derives from the rubric, so the consistency invariant below
    # is actually exercised (extract_inventory returns an empty summary when the
    # file is absent, which would make the assertion vacuous / KeyError).
    rubric_rows = module.parse_rubric()
    expected_all = len(rubric_rows)
    module.INVENTORY.parent.mkdir(parents=True, exist_ok=True)
    module.INVENTORY.write_text(
        f"- Total SKILL.md files inventoried: {expected_all}\n",
        encoding="utf-8",
    )
    try:
        module.main()
        text = module.OUTPUT.read_text(encoding="utf-8")
    finally:
        module.INVENTORY.unlink(missing_ok=True)
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


# ---------------------------------------------------------------------------
# Parser units
# ---------------------------------------------------------------------------

def test_parse_rubric_keeps_only_ten_column_data_rows(tmp_path, monkeypatch):
    """Exactly-10-column data rows parse; a 10-column SEPARATOR row and a
    9-column row must never surface as skills (the dashboard would render
    a '---' skill or a truncated one)."""
    module = load_module()
    rubric = tmp_path / "rubric.md"
    data = "| " + " | ".join(["good"] * 10) + " |"
    sep = "| " + " | ".join(["---"] * 10) + " |"
    short = "| " + " | ".join(["bad"] * 9) + " |"
    rubric.write_text("\n".join([sep, data, short]) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "RUBRIC", rubric)

    rows = module.parse_rubric()

    assert [row["skill"] for row in rows] == ["good"]


def test_parse_results_reads_a_nonempty_tsv(tmp_path, monkeypatch):
    """The empty-file guard must not swallow a real results file — an
    empty dashboard on a results night is the silent-failure mode."""
    module = load_module()
    results = tmp_path / "results.tsv"
    results.write_text("skill\tsafety\nalpha\t2\n", encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS", results)

    assert module.parse_results() == [{"skill": "alpha", "safety": "2"}]


def test_extract_inventory_routes_bullets_after_heading_to_the_skill(
    tmp_path, monkeypatch
):
    """Bullets AFTER a skill heading are that skill's details, not global
    summary lines — misrouting them inflates the summary and starves the
    per-skill cards."""
    module = load_module()
    inv = tmp_path / "inv.md"
    inv.write_text(
        "- Total skills: 3\n\n### 1. my-skill\n- Status: active\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "INVENTORY", inv)

    summary, inventory = module.extract_inventory()

    assert summary == {"Total skills": "3"}
    assert inventory["my-skill"]["status"] == "active"


def test_area_from_path_recognises_firecrawl_hyphen_paths():
    module = load_module()
    assert module.area_from_path("skills/firecrawl-search/SKILL.md") == "firecrawl"


def test_weakness_keys_treats_score_three_as_healthy():
    """3 is the floor of 'fine' — every score of 3 must yield NO weakness
    keys; only scores below 3 flag the section."""
    module = load_module()
    healthy = {k: "3" for k in ("safety", "output", "activation", "workflow", "eval", "maintain")}
    assert module.weakness_keys(healthy) == []
    assert module.weakness_keys(dict(healthy, safety="2")) == ["safety_gates"]


# ---------------------------------------------------------------------------
# Second pass: trend svg, action cap, audit dir
# ---------------------------------------------------------------------------

def test_trend_svg_two_points_and_flat_series():
    """Two points are already a trend (boundary '< 2'); a flat series
    (span 0) must still render via the span-1.0 fallback, never a
    ZeroDivisionError."""
    module = load_module()
    assert module.trend_svg([1.0, 2.0]).startswith("<svg")
    assert module.trend_svg([5.0, 5.0]).startswith("<svg")


def test_recommended_actions_keeps_area_and_caps_at_eight():
    """Actions keep the row's area verbatim (not coerced to 'all') and
    stop at exactly 8 entries."""
    module = load_module()
    rows = [
        {"area": f"area{i}", "weaknesses": ["safety_gates"], "skill": f"s{i}"}
        for i in range(9)
    ]
    actions = module.recommended_actions(rows)
    assert len(actions) == 8
    assert actions[0]["area"] == "area0"


def test_main_tolerates_existing_audit_dir():
    """AUDIT already exists on a real checkout — main() must not fail on
    mkdir(exist_ok)."""
    module = load_module()
    module.AUDIT.mkdir(parents=True, exist_ok=True)
    assert module.main() == 0

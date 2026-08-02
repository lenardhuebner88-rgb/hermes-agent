"""Acceptance-route threading from PlanSpec criteria into review prompts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs
from hermes_cli.plan_compiler import AcceptanceCriterion


ROUTE = "/control/fleet?planSegment=all"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    """Real isolated Kanban DB initialized through the production schema path."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_structured_planspec(plans_root: Path, *, route: str | None) -> Path:
    path = plans_root / "Codex" / "plans" / "2026-08-02-acceptance-route.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    route_line = f'          route: "{route}"\n' if route is not None else ""
    path.write_text(
        """---
status: freigegeben-komplett
owner: Codex
slice: acceptance-route
topic: "Acceptance route review prompt"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "Expose the visual acceptance route"
      lane: coder
      deps: []
      scope_files:
        - "hermes_cli/plan_compiler.py"
        - "hermes_cli/kanban_db.py"
      acceptance_criteria:
        - id: AC-7
          scope_level: review
          statement: "The tile is visually accepted."
          verification: "Open the review target and inspect the tile."
          done_signal: "The tile matches the approved visual."
          owner: reviewer
"""
        + route_line
        + """---
# Acceptance route
""",
        encoding="utf-8",
    )
    return path


def _write_legacy_planspec(plans_root: Path) -> Path:
    path = plans_root / "Codex" / "plans" / "2026-08-02-legacy-string-ac.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
status: freigegeben-komplett
owner: Codex
slice: legacy-string-ac
topic: "Legacy acceptance criterion"
freigabe: complete
live_test_depth: smoke
acceptance_criteria:
  - "Legacy tile acceptance stays unchanged."
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "Keep the legacy criterion"
      lane: coder
      deps: []
---
# Legacy acceptance criterion
""",
        encoding="utf-8",
    )
    return path


def _criterion_dict(*, route: str | None = None) -> dict[str, object]:
    criterion: dict[str, object] = {
        "id": "AC-7",
        "scope_level": "review",
        "statement": "The tile is visually accepted.",
        "verification": "Open the review target and inspect the tile.",
        "done_signal": "The tile matches the approved visual.",
        "owner": "reviewer",
        "applies_to": ["S1"],
        "required": True,
    }
    if route is not None:
        criterion["route"] = route
    return criterion


def _claim_review_prompt(conn, task_id: str) -> str:
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
    assert kb.claim_review_task(conn, task_id) is not None
    return kb.build_worker_context(conn, task_id)


def test_acceptance_criterion_route_serialization_is_strictly_additive():
    old_json = (
        '{"id":"AC-7","scope_level":"review",'
        '"statement":"The tile is visually accepted.",'
        '"verification":"Open the review target and inspect the tile.",'
        '"done_signal":"The tile matches the approved visual.",'
        '"owner":"reviewer","applies_to":["S1"],"required":true}'
    )
    without_route = AcceptanceCriterion.model_validate(_criterion_dict())
    assert json.dumps(
        without_route.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ) == old_json

    with_route = AcceptanceCriterion.model_validate(_criterion_dict(route=ROUTE))
    assert with_route.model_dump(mode="json") == {
        **_criterion_dict(),
        "route": ROUTE,
    }


def test_review_prompt_without_route_matches_legacy_snapshot(kanban_home):
    expected = (
        "## Acceptance checklist (verifier — judge each item)\n"
        "- [ ] AC-7: The tile is visually accepted. "
        "(verification: Open the review target and inspect the tile.; "
        "done_signal: The tile matches the approved visual.)\n\n"
        "Render a per-criterion verdict — each item MET or UNMET with the "
        "evidence you checked. Any UNMET → kanban_block.\n\n"
        "## Changed files at submit (caller check required)\n"
        "Kein Diff-Zugriff in diesem Run — prüfe gegen die vorliegende Evidenz "
        "und benenne fehlende Evidenz als NEEDS_MORE_CONTEXT statt als Blocker.\n\n"
        "Coder worker_gate: not configured for this repo (no coder-side gate ran)\n"
    )
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="probe", assignee="coder")
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ?, status = 'review' WHERE id = ?",
            (json.dumps([_criterion_dict()]), task_id),
        )
        assert kb.claim_review_task(conn, task_id) is not None
        rendered = "\n".join(kb._render_review_verifier_section(conn, task_id))

    assert rendered == expected


def test_legacy_string_ac_planspec_ingests_unchanged(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_legacy_planspec(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root, force=True)

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?",
            (result["child_ids"][0],),
        ).fetchone()
    assert row["body"] == (
        "PlanSpec subtask: S1\n\nLane: coder\n\n"
        "- AC-1: Legacy tile acceptance stays unchanged."
    )
    assert json.loads(row["acceptance_criteria"]) == [
        {"statement": "Legacy tile acceptance stays unchanged."}
    ]


def test_planspec_route_roundtrip_reaches_tasks_acceptance_criteria(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_structured_planspec(plans_root, route=ROUTE)

    result = planspecs.ingest_planspec(path, plans_root=plans_root, force=True)

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?",
            (result["child_ids"][0],),
        ).fetchone()
    criteria = json.loads(row["acceptance_criteria"])
    assert criteria[0]["route"] == ROUTE


def test_planspec_route_reaches_review_prompt(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_structured_planspec(plans_root, route=ROUTE)
    result = planspecs.ingest_planspec(path, plans_root=plans_root, force=True)

    with kb.connect_closing() as conn:
        prompt = _claim_review_prompt(conn, result["child_ids"][0])

    assert f"Acceptance location (route): {ROUTE}" in prompt

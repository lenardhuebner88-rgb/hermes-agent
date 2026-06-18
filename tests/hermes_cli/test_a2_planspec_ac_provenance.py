"""A2 tests: per-subtask AC threading + PlanSpec provenance columns on ingest.

AC1-persist-child-ac: planspec-sourced child tasks get non-NULL acceptance_criteria
    carrying the AC text.
AC2-migration-idempotent (provenance part): child rows get planspec_subtask_id +
    planspec_source; root row gets freigabe + live_test_depth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs
from hermes_cli.plan_compiler import (
    BindingSubtask,
    TaskgraphHints,
    taskgraph_hints_to_children,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_planspec_with_ac(plans_root: Path, name: str = "2026-06-17-a2-test.md") -> Path:
    """Write a minimal binding PlanSpec with structured plan-level AC."""
    path = plans_root / "Claude-Code" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
status: approved_for_ingest
owner: Claude-Code
slice: a2-test
topic: "A2 AC threading test"
freigabe: complete
live_test_depth: contract
acceptance_criteria:
  - id: AC-TEST-1
    scope_level: child
    statement: The child body carries this AC statement.
    verification: pytest assertion.
    done_signal: Test green.
    owner: coder
    applies_to: [S1, S2]
  - id: AC-TEST-2
    scope_level: child
    statement: A second criterion that also applies to S1.
    verification: pytest assertion.
    done_signal: Test green.
    owner: coder
    applies_to: [S1]
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "First subtask"
      lane: coder
      deps: []
    - id: S2
      title: "Second subtask"
      lane: coder-claude
      deps: [S1]
---
# A2 test plan
""",
        encoding="utf-8",
    )
    return path


def _write_planspec_no_ac(plans_root: Path, name: str = "2026-06-17-a2-noac.md") -> Path:
    """Write a binding PlanSpec WITHOUT any acceptance_criteria (backward-compat)."""
    path = plans_root / "Claude-Code" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
status: approved_for_ingest
owner: Claude-Code
slice: a2-noac
topic: "A2 backward-compat test"
freigabe: smoke-only
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: B1
      title: "Only subtask"
      lane: coder
      deps: []
---
# A2 no-AC plan
""",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Unit tests: taskgraph_hints_to_children AC threading
# ---------------------------------------------------------------------------


def test_taskgraph_hints_to_children_threads_plan_level_ac():
    """Children receive AC bullets from plan-level AC whose applies_to matches."""
    from hermes_cli.plan_compiler import AcceptanceCriterion

    plan_ac = [
        AcceptanceCriterion(
            id="AC-X",
            scope_level="child",
            statement="X applies to S1.",
            verification="test",
            done_signal="green",
            applies_to=["S1"],
        ),
        AcceptanceCriterion(
            id="AC-Y",
            scope_level="child",
            statement="Y applies to S2.",
            verification="test",
            done_signal="green",
            applies_to=["S2"],
        ),
    ]
    hints = TaskgraphHints(
        binding=True,
        subtasks=[
            BindingSubtask(id="S1", title="T1", lane="coder"),
            BindingSubtask(id="S2", title="T2", lane="coder", deps=["S1"]),
        ],
    )

    children = taskgraph_hints_to_children(hints, plan_ac=plan_ac)

    s1_body = children[0]["body"]
    s2_body = children[1]["body"]
    # S1 gets AC-X; S2 gets AC-Y. The token is the id with a single AC- marker
    # (no doubled "AC-AC-" prefix even though the id already starts with "AC").
    assert "AC-X" in s1_body, f"Expected AC-X bullet in S1 body; got:\n{s1_body}"
    assert "AC-AC-X" not in s1_body, "AC- prefix must not be doubled"
    assert "X applies to S1" in s1_body
    assert "AC-X" not in s2_body
    assert "AC-Y" in s2_body, f"Expected AC-Y bullet in S2 body; got:\n{s2_body}"
    assert "Y applies to S2" in s2_body


def test_taskgraph_hints_to_children_no_ac_is_backward_compatible():
    """Without plan_ac, children body unchanged (no AC section added)."""
    hints = TaskgraphHints(
        binding=True,
        subtasks=[BindingSubtask(id="Z1", title="T", lane="coder")],
    )
    children = taskgraph_hints_to_children(hints)
    # No AC bullets — body should NOT contain any AC- bullet line
    body = children[0]["body"]
    assert "AC-" not in body


def test_taskgraph_hints_to_children_populates_planspec_subtask_id():
    """Each child dict has planspec_subtask_id == its subtask id."""
    hints = TaskgraphHints(
        binding=True,
        subtasks=[
            BindingSubtask(id="ID-A", title="Task A", lane="coder"),
            BindingSubtask(id="ID-B", title="Task B", lane="coder", deps=["ID-A"]),
        ],
    )
    children = taskgraph_hints_to_children(hints, planspec_source="/tmp/test.md")
    assert children[0]["planspec_subtask_id"] == "ID-A"
    assert children[1]["planspec_subtask_id"] == "ID-B"
    assert children[0]["planspec_source"] == "/tmp/test.md"
    assert children[1]["planspec_source"] == "/tmp/test.md"


def test_taskgraph_hints_to_children_planspec_source_none_omits_key():
    """When planspec_source is None, the key is absent (not stored as None)."""
    hints = TaskgraphHints(
        binding=True,
        subtasks=[BindingSubtask(id="Z1", title="T", lane="coder")],
    )
    children = taskgraph_hints_to_children(hints)
    # planspec_subtask_id is always present; planspec_source may be absent
    assert "planspec_subtask_id" in children[0]
    assert "planspec_source" not in children[0]


def test_ac_token_not_doubled_for_ids_starting_with_ac():
    """#6: an AC id that already starts with 'AC' yields a single AC- token,
    not a doubled 'AC-AC...' prefix."""
    from hermes_cli.plan_compiler import AcceptanceCriterion

    plan_ac = [
        AcceptanceCriterion(
            id="AC1-persist-child-ac",
            scope_level="child",
            statement="must persist",
            verification="test",
            done_signal="green",
            applies_to=["S1"],
        ),
    ]
    hints = TaskgraphHints(
        binding=True, subtasks=[BindingSubtask(id="S1", title="T", lane="coder")]
    )
    body = taskgraph_hints_to_children(hints, plan_ac=plan_ac)[0]["body"]
    assert "AC-1-persist-child-ac" in body
    assert "AC-AC1" not in body


def test_plan_level_ac_without_applies_to_threads_to_all_subtasks():
    """#3: a structured plan-level AC with empty applies_to is plan-wide and
    threads into every subtask instead of being silently dropped."""
    from hermes_cli.plan_compiler import AcceptanceCriterion

    plan_ac = [
        AcceptanceCriterion(
            id="AC-GLOBAL",
            scope_level="plan",
            statement="applies everywhere",
            verification="test",
            done_signal="green",
            # no applies_to → plan-wide
        ),
    ]
    hints = TaskgraphHints(
        binding=True,
        subtasks=[
            BindingSubtask(id="S1", title="T1", lane="coder"),
            BindingSubtask(id="S2", title="T2", lane="coder"),
        ],
    )
    children = taskgraph_hints_to_children(hints, plan_ac=plan_ac)
    assert "applies everywhere" in children[0]["body"]
    assert "applies everywhere" in children[1]["body"]


def test_free_form_plan_level_ac_threads_to_all_subtasks():
    """#3: a free-form plan-level AC string (no applies_to possible) is plan-wide."""
    hints = TaskgraphHints(
        binding=True,
        subtasks=[
            BindingSubtask(id="S1", title="T1", lane="coder"),
            BindingSubtask(id="S2", title="T2", lane="coder"),
        ],
    )
    children = taskgraph_hints_to_children(
        hints, plan_ac=["free-form global criterion"]
    )
    assert "free-form global criterion" in children[0]["body"]
    assert "free-form global criterion" in children[1]["body"]


def test_invalid_per_subtask_ac_falls_back_to_plan_level():
    """#2: when a subtask's own acceptance_criteria are ALL invalid, the
    plan-level fallback still applies (child is not left with no AC)."""
    from hermes_cli.plan_compiler import AcceptanceCriterion

    plan_ac = [
        AcceptanceCriterion(
            id="AC-FALLBACK",
            scope_level="child",
            statement="fallback criterion",
            verification="test",
            done_signal="green",
            applies_to=["S1"],
        ),
    ]
    hints = TaskgraphHints(
        binding=True,
        subtasks=[
            BindingSubtask(
                id="S1", title="T1", lane="coder",
                # structurally-invalid AC dict (missing required fields)
                acceptance_criteria=[{"id": "broken"}],
            ),
        ],
    )
    body = taskgraph_hints_to_children(hints, plan_ac=plan_ac)[0]["body"]
    assert "fallback criterion" in body


def test_multiline_ac_statement_collapsed_to_single_line():
    """#5: a multi-line AC statement is collapsed so it stays on one bullet line
    and survives _parse_acceptance_criteria (which matches only the first line)."""
    from hermes_cli.plan_compiler import AcceptanceCriterion

    plan_ac = [
        AcceptanceCriterion(
            id="AC-ML",
            scope_level="child",
            statement="line one\nline two\nline three",
            verification="test",
            done_signal="green",
            applies_to=["S1"],
        ),
    ]
    hints = TaskgraphHints(
        binding=True, subtasks=[BindingSubtask(id="S1", title="T", lane="coder")]
    )
    body = taskgraph_hints_to_children(hints, plan_ac=plan_ac)[0]["body"]
    ac_line = next(ln for ln in body.splitlines() if ln.startswith("- AC-ML"))
    assert "line one line two line three" in ac_line


# ---------------------------------------------------------------------------
# Integration tests: ingest_planspec → DB columns
# ---------------------------------------------------------------------------


def test_ingest_planspec_child_ac_is_non_null_and_carries_statement(
    kanban_home, tmp_path: Path
):
    """AC1: planspec-sourced children have non-NULL acceptance_criteria containing the
    AC statement text (not just column structure).
    """
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec_with_ac(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    child_ids = result["child_ids"]
    assert len(child_ids) == 2

    with kb.connect_closing() as conn:
        for cid in child_ids:
            row = conn.execute(
                "SELECT acceptance_criteria FROM tasks WHERE id = ?", (cid,)
            ).fetchone()
            assert row is not None
            ac_json = row["acceptance_criteria"]
            assert ac_json is not None, (
                f"Child {cid}: acceptance_criteria is NULL — AC threading failed"
            )
            ac_list = json.loads(ac_json)
            assert isinstance(ac_list, list) and len(ac_list) >= 1, (
                f"Child {cid}: acceptance_criteria parsed to empty list"
            )
            # At least one criterion carries a known statement substring
            all_text = json.dumps(ac_list)
            assert "AC statement" in all_text or "carries this AC statement" in all_text, (
                f"Child {cid}: AC text not found in parsed criteria; got: {all_text}"
            )


def test_ingest_planspec_child_has_correct_planspec_subtask_id(
    kanban_home, tmp_path: Path
):
    """AC2: each child row's planspec_subtask_id matches the originating subtask id."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec_with_ac(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    child_ids = result["child_ids"]

    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, planspec_subtask_id FROM tasks WHERE id IN ({})".format(
                ",".join("?" * len(child_ids))
            ),
            child_ids,
        ).fetchall()
    subtask_ids = {row["planspec_subtask_id"] for row in rows}
    assert subtask_ids == {"S1", "S2"}, (
        f"Expected subtask ids {{S1, S2}}, got {subtask_ids}"
    )


def test_ingest_planspec_child_has_planspec_source(kanban_home, tmp_path: Path):
    """AC2: each child row's planspec_source equals the resolved .md path."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec_with_ac(plans_root)
    resolved_path = str(path.resolve())

    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    child_ids = result["child_ids"]

    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT planspec_source FROM tasks WHERE id IN ({})".format(
                ",".join("?" * len(child_ids))
            ),
            child_ids,
        ).fetchall()
    for row in rows:
        assert row["planspec_source"] == resolved_path, (
            f"planspec_source mismatch: expected {resolved_path}, got {row['planspec_source']}"
        )


def test_ingest_planspec_root_has_freigabe_and_live_test_depth(
    kanban_home, tmp_path: Path
):
    """AC2: root task row has freigabe + live_test_depth from the PlanSpec frontmatter."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec_with_ac(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    root_id = result["root_task_id"]

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT freigabe, live_test_depth FROM tasks WHERE id = ?", (root_id,)
        ).fetchone()
    assert row is not None
    assert row["freigabe"] == "complete", (
        f"Root freigabe: expected 'complete', got {row['freigabe']!r}"
    )
    assert row["live_test_depth"] == "contract", (
        f"Root live_test_depth: expected 'contract', got {row['live_test_depth']!r}"
    )


def test_ingest_planspec_without_ac_is_backward_compatible(
    kanban_home, tmp_path: Path
):
    """Backward-compat: a PlanSpec with no acceptance_criteria still ingests fine;
    child acceptance_criteria is NULL; no crash.
    """
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec_no_ac(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 1

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?",
            (result["child_ids"][0],),
        ).fetchone()
    assert row is not None
    # No AC → NULL is expected and fine
    assert row["acceptance_criteria"] is None


def test_ingest_planspec_root_freigabe_in_noac_plan(kanban_home, tmp_path: Path):
    """Root freigabe/live_test_depth are populated even when no AC is present."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec_no_ac(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT freigabe, live_test_depth FROM tasks WHERE id = ?",
            (result["root_task_id"],),
        ).fetchone()
    assert row["freigabe"] == "smoke-only"
    assert row["live_test_depth"] == "smoke"

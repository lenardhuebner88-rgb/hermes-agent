"""Deterministic spec-rubric gate (validate_spec_rubric) in the ingest path.

The rubric is layered ON TOP of parse_binding_planspec's structural validation
and runs synchronously inside ingest_planspec — immediately after the parse and
BEFORE any DB write. Each broken-spec check must raise PlanSpecBlocked with an
actionable finding and leave the board untouched. ``--force`` bypasses the
rubric and logs a WARNING with the skipped reasons.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs
from hermes_cli.subcommands import plan as plan_cmd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write(plans_root: Path, body: str, name: str = "2026-06-18-rubric.md") -> Path:
    path = plans_root / "Hermes" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _task_count() -> int:
    with kb.connect_closing() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


def _run_plan_ingest(args, plans_root: Path, caplog) -> dict:
    """Run ``plan_command`` for an ``ingest`` namespace against *plans_root*.

    ``plan_command`` calls ``planspecs.ingest_planspec`` without ``plans_root`` —
    it relies on the def-time default, which cannot be monkeypatched. So spy on
    the forwarded kwargs and inject ``plans_root`` for the tmp fixture, then
    delegate to the real implementation (proving --force wires through).
    """
    real_ingest = planspecs.ingest_planspec
    captured: dict = {}

    def _spy(path, **kwargs):
        captured.update(kwargs)
        kwargs.setdefault("plans_root", plans_root)
        return real_ingest(path, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(plan_cmd.planspecs, "ingest_planspec", _spy)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.planspecs"):
            rc = plan_cmd.plan_command(args)
    finally:
        mp.undo()
    return {"rc": rc, "captured": captured}


# A clean, rubric-passing PlanSpec in the canonical shape (per-subtask AC,
# valid lanes, no template residue, no CC instrument).
CLEAN = """---
status: freigegeben-komplett
owner: Hermes
slice: R1
topic: "Rubric clean"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1-S1
      title: "Short verbatim task title"
      lane: coder
      deps: []
      acceptance_criteria:
        - "Verbatim AC statement that must hold for this subtask"
      body: "Optional verbatim worker body"
    - id: R1-S2
      title: "Final verdict on the slice"
      lane: reviewer
      deps: [R1-S1]
      acceptance_criteria:
        - "Review verdict recorded with evidence"
---
# R1
"""


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_spec_passes_rubric_and_ingests(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 2


def test_validate_spec_rubric_returns_none_for_clean_spec(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)

    assert planspecs.validate_spec_rubric(spec) is None


def test_inherited_plan_level_ac_satisfies_check1(kanban_home, tmp_path: Path):
    """A subtask with no per-subtask AC passes check 1 when a plan-wide
    (no-applies_to / free-form) top-level AC threads into every subtask."""
    body = """---
status: freigegeben-komplett
owner: Hermes
slice: R1
topic: "Inherited AC"
freigabe: complete
live_test_depth: smoke
acceptance_criteria:
  - "Plan-wide criterion threads into every subtask"
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1-S1
      title: "No own AC"
      lane: coder
      deps: []
    - id: R1-S2
      title: "Also no own AC"
      lane: coder-claude
      deps: [R1-S1]
---
# R1
"""
    path = _write(plans_root := tmp_path / "03-Agents", body)
    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert result["ok"] is True
    assert len(result["child_ids"]) == 2


# ---------------------------------------------------------------------------
# Check 1: every subtask has >= 1 AC
# ---------------------------------------------------------------------------


def test_ac_less_subtask_blocks_and_writes_nothing(kanban_home, tmp_path: Path):
    body = """---
status: freigegeben-komplett
owner: Hermes
slice: R1
topic: "AC-less"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1-S1
      title: "Has its own AC"
      lane: coder
      deps: []
      acceptance_criteria:
        - "This one is fine"
    - id: R1-S2
      title: "No acceptance criteria at all"
      lane: coder
      deps: [R1-S1]
---
# R1
"""
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert "AC-less subtask: R1-S2" in exc.value.findings
    # No DB write on a blocked ingest.
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# Check 2: template residue in title / body / AC
# ---------------------------------------------------------------------------


def test_angle_placeholder_residue_in_title_blocks(kanban_home, tmp_path: Path):
    body = CLEAN.replace('title: "Short verbatim task title"', 'title: "Implement <id> handler"')
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert any("placeholder residue in R1-S1" in f and "<id>" in f for f in exc.value.findings)
    assert _task_count() == 0


def test_todo_marker_residue_in_ac_blocks(kanban_home, tmp_path: Path):
    body = CLEAN.replace(
        '        - "Verbatim AC statement that must hold for this subtask"',
        '        - "TODO: write a real acceptance criterion"',
    )
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert any("placeholder residue in R1-S1" in f and "TODO" in f for f in exc.value.findings)
    assert _task_count() == 0


def test_bare_ellipsis_residue_in_body_blocks(kanban_home, tmp_path: Path):
    body = CLEAN.replace('body: "Optional verbatim worker body"', 'body: "Do the thing ..."')
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert any("placeholder residue in R1-S1" in f for f in exc.value.findings)
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# Check 3: lane membership
# ---------------------------------------------------------------------------


def test_unknown_lane_blocks(kanban_home, tmp_path: Path):
    body = CLEAN.replace("      lane: coder\n", "      lane: frontend-wizard\n", 1)
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert "unknown lane: frontend-wizard" in exc.value.findings
    assert _task_count() == 0


@pytest.mark.parametrize("lane", sorted(planspecs.VALID_PLANSPEC_LANES))
def test_every_documented_lane_is_accepted(lane: str, tmp_path: Path):
    body = CLEAN.replace("      lane: coder\n", f"      lane: {lane}\n", 1)
    path = _write(plans_root := tmp_path / "03-Agents", body, name=f"lane-{lane}.md")
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    # No lane finding for any documented lane.
    findings = planspecs._collect_spec_rubric_findings(spec)
    assert not any("lane" in f for f in findings), findings


# ---------------------------------------------------------------------------
# Check 4: no CC instrument as lane or baked into a worker AC
# ---------------------------------------------------------------------------


def test_cc_instrument_as_lane_blocks_with_specific_message(kanban_home, tmp_path: Path):
    body = CLEAN.replace("      lane: coder\n", "      lane: council\n", 1)
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    # council gets the actionable CC-instrument message, not the generic one.
    assert any("CC-instrument as lane" in f and "council" in f for f in exc.value.findings)
    assert "unknown lane: council" not in exc.value.findings
    assert _task_count() == 0


def test_cc_instrument_in_ac_blocks(kanban_home, tmp_path: Path):
    body = CLEAN.replace(
        '        - "Verbatim AC statement that must hold for this subtask"',
        '        - "Lass das council-Panel das Ergebnis final freigeben"',
    )
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert any("CC-instrument in AC of R1-S1" in f and "council" in f for f in exc.value.findings)
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# --force bypass
# ---------------------------------------------------------------------------


def test_force_bypasses_rubric_and_logs_warning(kanban_home, tmp_path: Path, caplog):
    body = """---
status: freigegeben-komplett
owner: Hermes
slice: R1
topic: "Force bypass"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1-S1
      title: "No acceptance criteria at all"
      lane: coder
      deps: []
---
# R1
"""
    path = _write(plans_root := tmp_path / "03-Agents", body)

    # Without --force the rubric blocks.
    with pytest.raises(planspecs.PlanSpecBlocked):
        planspecs.ingest_planspec(path, plans_root=plans_root)
    assert _task_count() == 0

    with caplog.at_level(logging.WARNING, logger="hermes_cli.planspecs"):
        result = planspecs.ingest_planspec(path, plans_root=plans_root, force=True)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 1
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "rubric bypassed" in warnings.lower()
    assert "AC-less subtask: R1-S1" in warnings


def test_plan_ingest_cli_force_flag_wires_through(kanban_home, tmp_path: Path, caplog):
    body = """---
status: freigegeben-komplett
owner: Hermes
slice: R1
topic: "CLI force"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1-S1
      title: "No acceptance criteria at all"
      lane: coder
      deps: []
---
# R1
"""
    path = _write(plans_root := tmp_path / "03-Agents", body)

    # Build the parsed namespace exactly as the top-level parser would.
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    plan_cmd.build_plan_parser(sub)
    args = parser.parse_args(["plan", "ingest", str(path), "--force"])
    assert args.force is True

    # Spy on the forwarded kwargs (and point plans_root at the tmp fixture, since
    # plan_command relies on the def-time default which can't be monkeypatched).
    rc = _run_plan_ingest(args, plans_root, caplog)

    assert rc["captured"]["force"] is True
    assert rc["rc"] == 0
    assert _task_count() == 2  # root + 1 child
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "rubric bypassed" in warnings.lower()


def test_plan_ingest_cli_without_force_blocks(kanban_home, tmp_path: Path, caplog):
    body = """---
status: freigegeben-komplett
owner: Hermes
slice: R1
topic: "CLI no force"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1-S1
      title: "No acceptance criteria at all"
      lane: coder
      deps: []
---
# R1
"""
    path = _write(plans_root := tmp_path / "03-Agents", body)

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    plan_cmd.build_plan_parser(sub)
    args = parser.parse_args(["plan", "ingest", str(path)])
    assert args.force is False

    rc = _run_plan_ingest(args, plans_root, caplog)

    # plan_command maps PlanSpecBlocked to exit code 2 and writes nothing.
    assert rc["captured"]["force"] is False
    assert rc["rc"] == 2
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# Canon example shape still ingests
# ---------------------------------------------------------------------------


def test_canon_example_shape_ingests(kanban_home, tmp_path: Path):
    """The vault/00-Canon/planspec-taskgraph.md *Required shape* — per-subtask
    AC plus a top-level applies_to-inherited AC — passes the rubric."""
    body = """---
status: freigegeben-komplett
owner: Hermes
slice: B1
topic: "Canon shape"
freigabe: complete
live_test_depth: smoke
acceptance_criteria:
  - id: AC-PLAN-1
    scope_level: child
    statement: Inherited criterion for the second subtask.
    verification: pytest assertion.
    done_signal: Test green.
    owner: coder
    applies_to: [B1-S2]
taskgraph_hints:
  binding: true
  subtasks:
    - id: B1-S1
      title: "Short verbatim task title"
      lane: coder
      deps: []
      acceptance_criteria:
        - "Verbatim AC statement that must hold for this subtask"
      body: "Optional verbatim worker body"
    - id: B1-S2
      title: "Second subtask inherits its AC"
      lane: coder-claude
      deps: [B1-S1]
---
# Canon shape
"""
    path = _write(plans_root := tmp_path / "03-Agents", body, name="canon.md")
    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert result["ok"] is True
    assert len(result["child_ids"]) == 2

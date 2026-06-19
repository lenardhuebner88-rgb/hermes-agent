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
# Check 2b: code-span-aware residue — markers that are only *quoted* inside
# backtick spans / code fences / path tokens are documentary, not residue. A
# genuine unfilled placeholder sitting in prose is still caught.
# ---------------------------------------------------------------------------


def test_residue_tokens_exempts_quoted_markers_in_code_spans():
    """Unit-level: backtick spans, code fences and path tokens are masked before
    the marker scan, so a quoted marker yields no residue token."""
    # Inline-backtick spans around every marker kind.
    assert planspecs._residue_tokens("Doku: `<id>` / `TODO` / `FIXME` / `TBD` / `...`") == []
    # The nested backtick *display* of inline-code and code-fence examples
    # (verbatim shape from the residue-fix spec body) leaves no residue.
    assert planspecs._residue_tokens("(`` `...` ``), Code-Fences (```` ```...``` ````)") == []
    # An obvious path token with a trailing ellipsis is a path, not residue.
    assert planspecs._residue_tokens("siehe `.worktrees/...` und a/b/c.py") == []


def test_residue_tokens_still_flags_genuine_prose_placeholders():
    """Unit-level: a marker sitting bare in prose (no backticks / not a path) is
    still reported — the gate is not weakened."""
    assert planspecs._residue_tokens("Implement the <handler> for X") == ["<handler>"]
    assert planspecs._residue_tokens("TODO: write this") == ["TODO"]
    assert planspecs._residue_tokens("Do the thing ...") == ["..."]


def test_residue_marker_is_case_sensitive():
    """Fix(a) 2026-06-19: the TODO/FIXME/TBD marker is case-sensitive. The literal
    template markers are all-caps by convention; a lowercase status word like
    ``todo`` (the kanban status) is NOT template residue and must not block an
    otherwise-clean spec. The all-caps markers stay residue (gate not weakened)."""
    # lowercase status words are NOT residue
    assert planspecs._residue_tokens("move the card to todo and start") == []
    assert planspecs._residue_tokens("status todo / fixme later / tbd") == []
    # all-caps markers are still residue
    assert planspecs._residue_tokens("TODO: real marker") == ["TODO"]
    assert planspecs._residue_tokens("FIXME this") == ["FIXME"]
    assert planspecs._residue_tokens("TBD") == ["TBD"]


def test_quoted_markers_in_backticks_in_body_and_ac_pass_rubric(kanban_home, tmp_path: Path):
    """(1) A spec that *cites* the forbidden markers inside backticks in BOTH the
    body AND an AC statement passes the rubric and ingests."""
    body = CLEAN.replace(
        'body: "Optional verbatim worker body"',
        'body: "Doku: `<id>`/`TODO`/`FIXME`/`TBD`/`...` werden nur ZITIERT"',
    ).replace(
        '        - "Verbatim AC statement that must hold for this subtask"',
        '        - "Scan nimmt `<iso>` und `...` in Backticks aus (Pfad `a/b/c.py`)"',
    )
    path = _write(plans_root := tmp_path / "03-Agents", body)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)

    # Deterministic rubric raises nothing — no residue finding.
    assert planspecs.validate_spec_rubric(spec) is None
    findings = planspecs._collect_spec_rubric_findings(spec)
    assert not any("residue" in f for f in findings), findings

    # And the full ingest succeeds (judge disabled → deterministic-only).
    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert result["ok"] is True
    assert len(result["child_ids"]) == 2


def test_genuine_unfilled_angle_placeholder_in_prose_body_still_blocks(kanban_home, tmp_path: Path):
    """(2) A real unfilled, backtick-free angle placeholder in the prose body
    still blocks — the gate is not weakened."""
    body = CLEAN.replace(
        'body: "Optional verbatim worker body"',
        'body: "Implement the <handler> for the thing"',
    )
    path = _write(plans_root := tmp_path / "03-Agents", body)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert any("placeholder residue in R1-S1" in f and "<handler>" in f for f in exc.value.findings)
    assert _task_count() == 0


def test_real_vision_flywheel_fixture_passes_rubric(kanban_home, tmp_path: Path, monkeypatch):
    """(4) A real Vision-Flywheel PlanSpec that quotes the markers (in backticks,
    code-fence displays and paths, in body AND AC) — the very spec describing this
    fix — passes the deterministic rubric and ingests after the code-span fix."""
    monkeypatch.setenv("HERMES_PLANSPEC_JUDGE", "0")  # deterministic-only, no network
    fixture = (
        Path(__file__).parent / "fixtures" / "vision_flywheel_haerter_residue_fix_planspec.md"
    )
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, fixture.read_text(encoding="utf-8"), name="residue-fix.md")

    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    # The deterministic rubric finds NO residue in the quoted markers.
    findings = planspecs._collect_spec_rubric_findings(spec)
    assert not any("residue" in f for f in findings), findings
    assert planspecs.validate_spec_rubric(spec) is None

    result = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert result["ok"] is True
    assert len(result["child_ids"]) == 2


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
# Fix(b) 2026-06-19: operator-signed spec = WARN (not block) + judge skipped.
# Signed = approved_by set (non-empty) AND freigabe == "complete". Structural
# validation stays hard for everyone (it runs before the rubric branch).
# ---------------------------------------------------------------------------


def _signed_ac_less_body(approved_by_line: str = "approved_by: Piet", freigabe: str = "complete") -> str:
    return f"""---
status: defined
owner: Hermes
slice: R1
topic: "Signed but AC-less"
{approved_by_line}
freigabe: {freigabe}
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


@pytest.mark.parametrize(
    "approved_by_line, freigabe, expected",
    [
        ("approved_by: Piet", "complete", True),   # both → signed
        ("approved_by: Piet", "operator", False),  # approved but not complete
        ("approved_by:", "complete", False),       # empty approved_by
        ("owner_note: x", "complete", False),      # no approved_by at all
    ],
)
def test_spec_is_signed_requires_approved_by_and_freigabe_complete(
    tmp_path: Path, approved_by_line: str, freigabe: str, expected: bool
):
    body = _signed_ac_less_body(approved_by_line, freigabe)
    path = _write(plans_root := tmp_path / "03-Agents", body, name=f"signed-{freigabe}.md")
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    assert planspecs._spec_is_signed(spec) is expected


def test_signed_spec_with_finding_warns_skips_judge_and_ingests(
    kanban_home, tmp_path: Path, caplog, monkeypatch
):
    """An operator-signed spec with a rubric finding logs a WARNING and ingests
    (does not block), and the subjective judge is skipped (operator approval
    replaces it)."""
    judge_calls: list = []
    monkeypatch.setattr(planspecs, "run_spec_quality_judge", lambda spec: judge_calls.append(spec))
    path = _write(plans_root := tmp_path / "03-Agents", _signed_ac_less_body())

    with caplog.at_level(logging.WARNING, logger="hermes_cli.planspecs"):
        result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 1
    # Judge skipped for the signed spec.
    assert judge_calls == []
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "AC-less subtask: R1-S1" in warnings
    assert "signed" in warnings.lower()


def test_unsigned_spec_with_finding_still_blocks(kanban_home, tmp_path: Path, monkeypatch):
    """A spec WITHOUT approved_by (even with freigabe=complete) is unsigned — the
    rubric still blocks. The signed-WARN path requires BOTH conditions, which is
    exactly why the existing block-tests (freigabe:complete, no approved_by) keep
    blocking."""
    monkeypatch.setattr(planspecs, "run_spec_quality_judge", lambda spec: None)
    path = _write(plans_root := tmp_path / "03-Agents", _signed_ac_less_body(approved_by_line="owner_note: x"))

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    assert "AC-less subtask: R1-S1" in exc.value.findings
    assert _task_count() == 0


def test_signed_clean_spec_ingests_and_skips_judge(kanban_home, tmp_path: Path, monkeypatch):
    """A signed, rubric-clean spec ingests with no findings and still skips the
    judge (operator approval is authoritative)."""
    judge_calls: list = []
    monkeypatch.setattr(planspecs, "run_spec_quality_judge", lambda spec: judge_calls.append(spec))
    signed_clean = CLEAN.replace("status: freigegeben-komplett\n", "status: defined\napproved_by: Piet\n")
    path = _write(plans_root := tmp_path / "03-Agents", signed_clean, name="signed-clean.md")

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 2
    assert judge_calls == []


# ---------------------------------------------------------------------------
# Fix(c) 2026-06-19: `hermes plan validate` — read-only preview (no DB write).
# Reports disposition clean | warn (signed+findings) | block (unsigned+findings)
# | invalid (structural / YAML error), with clean error messages.
# ---------------------------------------------------------------------------


def test_validate_planspec_clean_signed_spec_is_clean(kanban_home, tmp_path: Path):
    signed_clean = CLEAN.replace("status: freigegeben-komplett\n", "status: defined\napproved_by: Piet\n")
    path = _write(plans_root := tmp_path / "03-Agents", signed_clean, name="vclean.md")

    result = planspecs.validate_planspec(path, plans_root=plans_root)

    assert result["disposition"] == "clean"
    assert result["ok"] is True
    assert result["findings"] == []
    assert result["signed"] is True
    assert _task_count() == 0  # read-only: nothing written


def test_validate_planspec_signed_with_finding_is_warn(tmp_path: Path):
    path = _write(plans_root := tmp_path / "03-Agents", _signed_ac_less_body(), name="vwarn.md")

    result = planspecs.validate_planspec(path, plans_root=plans_root)

    assert result["disposition"] == "warn"
    assert result["ok"] is True
    assert result["would_block"] is False
    assert any("AC-less subtask: R1-S1" in f for f in result["findings"])


def test_validate_planspec_unsigned_with_finding_would_block(tmp_path: Path):
    path = _write(
        plans_root := tmp_path / "03-Agents",
        _signed_ac_less_body(approved_by_line="owner_note: x"),
        name="vblock.md",
    )

    result = planspecs.validate_planspec(path, plans_root=plans_root)

    assert result["disposition"] == "block"
    assert result["ok"] is False
    assert result["would_block"] is True
    assert any("AC-less subtask: R1-S1" in f for f in result["findings"])


def test_validate_planspec_invalid_yaml_reports_clean_message(tmp_path: Path):
    body = "---\nstatus: defined\nfreigabe: [unclosed\n---\n# X\n"
    path = _write(plans_root := tmp_path / "03-Agents", body, name="vyaml.md")

    result = planspecs.validate_planspec(path, plans_root=plans_root)

    assert result["disposition"] == "invalid"
    assert result["ok"] is False
    blob = " ".join(result["findings"])
    assert "yaml" in blob.lower()           # clean, human-readable
    assert "Traceback" not in blob          # not a raw Python traceback


def test_validate_planspec_missing_required_field_is_invalid(tmp_path: Path):
    body = CLEAN.replace("freigabe: complete\n", "")  # drop the required field
    path = _write(plans_root := tmp_path / "03-Agents", body, name="vmissing.md")

    result = planspecs.validate_planspec(path, plans_root=plans_root)

    assert result["disposition"] == "invalid"
    assert any("freigabe is required" in f for f in result["findings"])


def test_plan_validate_cli_wires_through(kanban_home, tmp_path: Path, capsys):
    """`hermes plan validate <path>` exists, returns rc 2 for a would-block spec
    and rc 0 for a signed (warn) spec, and writes nothing to the board."""
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    plan_cmd.build_plan_parser(sub)

    plans_root = tmp_path / "03-Agents"
    block_path = _write(plans_root, _signed_ac_less_body(approved_by_line="owner_note: x"), name="cliblock.md")
    warn_path = _write(plans_root, _signed_ac_less_body(), name="cliwarn.md")

    # validate_planspec relies on the def-time default plans_root; inject the tmp
    # fixture via a spy (same pattern as the ingest CLI tests).
    real = planspecs.validate_planspec
    mp = pytest.MonkeyPatch()
    mp.setattr(plan_cmd.planspecs, "validate_planspec", lambda path, **kw: real(path, **{**kw, "plans_root": plans_root}))
    try:
        rc_block = plan_cmd.plan_command(parser.parse_args(["plan", "validate", str(block_path)]))
        rc_warn = plan_cmd.plan_command(parser.parse_args(["plan", "validate", str(warn_path), "--json"]))
    finally:
        mp.undo()

    assert rc_block == 2
    assert rc_warn == 0
    out = capsys.readouterr()
    assert '"disposition": "warn"' in out.out  # --json run emitted the structured result
    assert _task_count() == 0  # validate never writes


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

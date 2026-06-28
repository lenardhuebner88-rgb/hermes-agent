"""Tests for hermes-plan-compile contract compiler v1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pydantic import ValidationError

from hermes_cli.plan_compiler import (
    BindingSubtask,
    TaskgraphHints,
    compile_plan,
    main,
    taskgraph_hints_to_children,
)


VALID_PLAN = """---
contract_version: 1
goal: Ship a local contract compiler
anti_scope:
  - no runtime config changes
acceptance_criteria:
  - valid contract is emitted
evidence_required:
  - pytest is green
risk_class: MEDIUM
next_decision: Reviewer gate can inspect artifacts
allowed_actions:
  - edit hermes_cli local files
forbidden_actions:
  - restart services
requires_approval:
  - deploy or restart
 taskgraph_hints:
  candidate_tasks:
    - Draft compiler output
    - Review non-binding marker
  dependencies:
    - Review non-binding marker depends_on Draft compiler output
  recommended_roles:
    - reviewer
---
## Goal
Ship the compiler.

## Acceptance Criteria
- Valid compile succeeds.

## Anti-Scope
- No runtime mutation.

## Evidence Required
- Tests pass.
""".replace("\n taskgraph_hints", "\ntaskgraph_hints")


STRUCTURED_AC_PLAN = """---
contract_version: 1
goal: Ship a PlanSpec acceptance-criteria contract
anti_scope:
  - no runtime config changes
acceptance_criteria:
  - id: AC-PLAN-1
    scope_level: plan
    statement: Contract output preserves a structured acceptance criterion.
    verification: Inspect compiled contract.yaml for AC-PLAN-1.
    done_signal: contract.acceptance_criteria[0].id == AC-PLAN-1
    owner: coder
    required: true
evidence_required:
  - pytest tests/hermes_cli/test_plan_compiler.py passes
risk_class: MEDIUM
next_decision: Reviewer gate can inspect artifacts
allowed_actions:
  - edit hermes_cli local files
forbidden_actions:
  - restart services
requires_approval:
  - deploy or restart
---
## Goal
Ship the compiler.

## Acceptance Criteria
- AC-PLAN-1 is structured and verifiable.

## Anti-Scope
- No runtime mutation.

## Evidence Required
- Tests pass.
"""

STRUCTURED_AC_MISSING_VERIFICATION = """---
contract_version: 1
goal: Reject incomplete structured criteria
anti_scope:
  - no runtime config changes
acceptance_criteria:
  - id: AC-PLAN-1
    scope_level: plan
    statement: Criterion has no verification.
    done_signal: validation blocks
evidence_required:
  - pytest captures BLOCKED output
risk_class: MEDIUM
next_decision: Fix the criterion
allowed_actions:
  - edit plan only
forbidden_actions:
  - restart services
requires_approval:
  - deploy or restart
---
## Goal
Reject the plan.

## Acceptance Criteria
- Structured ACs need verification.

## Anti-Scope
- No runtime mutation.

## Evidence Required
- Blocked output names the missing field.
"""

DUPLICATE_STRUCTURED_AC_IDS = """---
contract_version: 1
goal: Reject duplicate acceptance criteria ids
anti_scope:
  - no runtime config changes
acceptance_criteria:
  - id: AC-DUP-1
    scope_level: plan
    statement: First criterion.
    verification: Inspect compiled contract.
    done_signal: first signal
  - id: AC-DUP-1
    scope_level: review
    statement: Second criterion.
    verification: Inspect validation output.
    done_signal: second signal
evidence_required:
  - pytest captures BLOCKED output
risk_class: MEDIUM
next_decision: Fix duplicate ids
allowed_actions:
  - edit plan only
forbidden_actions:
  - restart services
requires_approval:
  - deploy or restart
---
## Goal
Reject the plan.

## Acceptance Criteria
- AC ids are unique.

## Anti-Scope
- No runtime mutation.

## Evidence Required
- Blocked output names duplicate ids.
"""


SINGLE_DICT_AC_PLAN = """---
contract_version: 1
goal: Accept a single mapping as acceptance criteria
anti_scope:
  - no runtime config changes
acceptance_criteria:
  id: AC-SINGLE-1
  scope_level: plan
  statement: A single mapping is accepted as one criterion.
  verification: Inspect compiled contract.
  done_signal: contract.acceptance_criteria[0].id == AC-SINGLE-1
evidence_required:
  - pytest passes
risk_class: MEDIUM
next_decision: Continue
allowed_actions:
  - edit plan only
forbidden_actions:
  - restart services
requires_approval:
  - deploy
---
## Goal
Accept single mapping.

## Acceptance Criteria
- Single mapping works.

## Anti-Scope
- None.

## Evidence Required
- Tests.
"""


INVALID_PLAN = """---
goal: Incomplete plan
risk_class: MEDIUM
---
## Goal
Missing required keys and sections.
"""


def test_compile_plan_emits_contract_receipt_source_and_schema(tmp_path: Path):
    plan = tmp_path / "compiler-plan.md"
    plan.write_text(VALID_PLAN, encoding="utf-8")

    artifacts = compile_plan(
        plan,
        compiled_root=tmp_path / "compiled",
        templates_root=tmp_path / "templates",
    )

    assert artifacts["source"].read_text(encoding="utf-8") == VALID_PLAN
    contract = yaml.safe_load(artifacts["contract"].read_text(encoding="utf-8"))
    assert contract["contract_version"] == 1
    assert contract["goal"] == "Ship a local contract compiler"
    assert contract["taskgraph_hints"]["non_binding"] is True
    draft = yaml.safe_load(artifacts["taskgraph_draft"].read_text(encoding="utf-8"))
    assert draft["schema_version"] == "taskgraph.draft.v1.1"
    assert draft["non_binding"] is True
    assert draft["binding"] == "non-binding"
    assert draft["disclaimer"].startswith("NON-BINDING DRAFT")
    assert draft["tasks"] == [
        {"id": "draft-compiler-output", "title": "Draft compiler output", "role_hint": "reviewer"},
        {"id": "review-non-binding-marker", "title": "Review non-binding marker", "role_hint": "reviewer"},
    ]
    assert draft["dependencies"] == ["Review non-binding marker depends_on Draft compiler output"]
    receipt = artifacts["receipt"].read_text(encoding="utf-8")
    assert "Result: GREEN" in receipt
    assert "taskgraph draft" in receipt
    schema = json.loads(artifacts["schema"].read_text(encoding="utf-8"))
    assert schema["title"] == "PlanContract"


def test_compile_plan_accepts_crlf_line_endings(tmp_path: Path):
    """CRLF line endings must not be mistaken for missing frontmatter."""
    plan = tmp_path / "crlf-plan.md"
    plan.write_bytes(VALID_PLAN.replace("\n", "\r\n").encode("utf-8"))

    artifacts = compile_plan(
        plan,
        compiled_root=tmp_path / "compiled",
        templates_root=tmp_path / "templates",
    )

    assert artifacts["contract"].exists()
    contract = yaml.safe_load(artifacts["contract"].read_text(encoding="utf-8"))
    assert contract["goal"] == "Ship a local contract compiler"


def test_compile_plan_accepts_utf8_bom(tmp_path: Path):
    """A leading UTF-8 BOM must not be mistaken for missing frontmatter."""
    plan = tmp_path / "bom-plan.md"
    plan.write_bytes("\ufeff".encode("utf-8") + VALID_PLAN.encode("utf-8"))

    artifacts = compile_plan(
        plan,
        compiled_root=tmp_path / "compiled",
        templates_root=tmp_path / "templates",
    )

    assert artifacts["contract"].exists()
    contract = yaml.safe_load(artifacts["contract"].read_text(encoding="utf-8"))
    assert contract["goal"] == "Ship a local contract compiler"


def test_compile_plan_accepts_structured_acceptance_criteria(tmp_path: Path):
    plan = tmp_path / "structured-ac-plan.md"
    plan.write_text(STRUCTURED_AC_PLAN, encoding="utf-8")

    artifacts = compile_plan(
        plan,
        compiled_root=tmp_path / "compiled",
        templates_root=tmp_path / "templates",
    )

    contract = yaml.safe_load(artifacts["contract"].read_text(encoding="utf-8"))
    criterion = contract["acceptance_criteria"][0]
    assert criterion["id"] == "AC-PLAN-1"
    assert criterion["scope_level"] == "plan"
    assert criterion["verification"] == "Inspect compiled contract.yaml for AC-PLAN-1."
    assert criterion["done_signal"] == "contract.acceptance_criteria[0].id == AC-PLAN-1"
    assert criterion["owner"] == "coder"
    assert criterion["required"] is True

    schema = json.loads(artifacts["schema"].read_text(encoding="utf-8"))
    assert "AcceptanceCriterion" in schema["$defs"]


def test_compile_plan_accepts_single_mapping_acceptance_criteria(tmp_path: Path):
    """A lone structured criterion written as a mapping (not wrapped in a list)
    must not be silently dropped."""
    plan = tmp_path / "single-dict-ac-plan.md"
    plan.write_text(SINGLE_DICT_AC_PLAN, encoding="utf-8")

    artifacts = compile_plan(
        plan,
        compiled_root=tmp_path / "compiled",
        templates_root=tmp_path / "templates",
    )

    contract = yaml.safe_load(artifacts["contract"].read_text(encoding="utf-8"))
    assert len(contract["acceptance_criteria"]) == 1
    assert contract["acceptance_criteria"][0]["id"] == "AC-SINGLE-1"


def test_structured_acceptance_criteria_require_verification(tmp_path: Path, capsys):
    plan = tmp_path / "missing-verification.md"
    plan.write_text(STRUCTURED_AC_MISSING_VERIFICATION, encoding="utf-8")

    code = main([
        str(plan),
        "--compiled-root",
        str(tmp_path / "compiled"),
        "--templates-root",
        str(tmp_path / "templates"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert "acceptance_criteria.0.verification" in captured.err


def test_structured_acceptance_criteria_ids_must_be_unique(tmp_path: Path, capsys):
    plan = tmp_path / "duplicate-ac-ids.md"
    plan.write_text(DUPLICATE_STRUCTURED_AC_IDS, encoding="utf-8")

    code = main([
        str(plan),
        "--compiled-root",
        str(tmp_path / "compiled"),
        "--templates-root",
        str(tmp_path / "templates"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert "duplicate acceptance_criteria id: AC-DUP-1" in captured.err


def test_invalid_plan_blocks_without_valid_output(tmp_path: Path, capsys):
    plan = tmp_path / "bad-plan.md"
    plan.write_text(INVALID_PLAN, encoding="utf-8")
    compiled_root = tmp_path / "compiled"

    code = main([
        str(plan),
        "--compiled-root",
        str(compiled_root),
        "--templates-root",
        str(tmp_path / "templates"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert "BLOCKED" in captured.err
    assert "anti_scope" in captured.err
    assert not (compiled_root / "bad-plan" / "contract.yaml").exists()


def test_binding_taskgraph_hints_compile_to_children(tmp_path: Path):
    plan = tmp_path / "binding-plan.md"
    plan.write_text(
        """---
contract_version: 1
goal: Ship binding taskgraph
anti_scope:
  - no runtime config changes
acceptance_criteria:
  - children are emitted
evidence_required:
  - pytest passes
risk_class: MEDIUM
next_decision: ingest can consume children
allowed_actions:
  - edit local files
forbidden_actions:
  - restart services
requires_approval:
  - deploy
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: First
      lane: coder
      deps: []
    - id: S2
      title: Second
      lane: verifier
      deps: [S1]
---
## Goal
Ship it.

## Acceptance Criteria
- Children emitted.

## Anti-Scope
- None.

## Evidence Required
- Tests.
""",
        encoding="utf-8",
    )

    artifacts = compile_plan(
        plan,
        compiled_root=tmp_path / "compiled",
        templates_root=tmp_path / "templates",
    )

    draft = yaml.safe_load(artifacts["taskgraph_draft"].read_text(encoding="utf-8"))
    assert draft["schema_version"] == "taskgraph.binding.v1"
    assert draft["binding"] is True
    assert draft["children"][1]["parents"] == [0]
    assert draft["children"][1]["assignee"] == "verifier"


def test_binding_taskgraph_hints_reject_unknown_dep():
    with pytest.raises(ValidationError) as exc:
        TaskgraphHints.model_validate(
            {
                "binding": True,
                "subtasks": [
                    {"id": "S1", "title": "First", "lane": "coder", "deps": ["missing"]},
                ],
            }
        )
    assert "unknown id" in str(exc.value)


def test_taskgraph_hints_to_children_preserves_titles_lanes_and_deps():
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder", "deps": []},
                {"id": "S2", "title": "Verify", "lane": "verifier", "deps": ["S1"]},
            ],
        }
    )

    assert children == [
        {
            "title": "Build",
            "body": "PlanSpec subtask: S1\n\nLane: coder",
            "assignee": "coder",
            "kind": "code",
            "parents": [],
            "planspec_lane": "coder",
            "planspec_deps": [],
            # A2: planspec_subtask_id is always populated; planspec_source is
            # absent when the caller doesn't pass planspec_source=...
            "planspec_subtask_id": "S1",
        },
        {
            "title": "Verify",
            "body": "PlanSpec subtask: S2\n\nLane: verifier\n\nDepends on: S1",
            "assignee": "verifier",
            "kind": "code",
            "parents": [0],
            "planspec_lane": "verifier",
            "planspec_deps": ["S1"],
            "planspec_subtask_id": "S2",
        },
    ]


def test_subtask_kind_analysis_threads_into_child_kind():
    """A1-classaware: a PlanSpec subtask may opt into the read-only analysis
    class via ``kind: analysis``; it threads into the child's ``kind`` so plan
    ingest persists it in ``tasks.kind`` (the verifier class marker)."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {
                    "id": "S1",
                    "title": "Probe latency",
                    "lane": "coder-claude",
                    "kind": "analysis",
                    "deps": [],
                },
            ],
        }
    )
    assert children[0]["kind"] == "analysis"


def test_subtask_kind_absent_is_lane_derived_default_strict():
    """Default-strict: a subtask WITHOUT an explicit analysis kind falls back to
    lane-derivation (byte-identical to pre-classaware behaviour)."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder", "deps": []},
                {"id": "S2", "title": "Review", "lane": "reviewer", "deps": []},
                # a non-analysis explicit kind is NOT honoured as an override —
                # opt-in is analysis-only, so this stays lane-derived too.
                {"id": "S3", "title": "Probe", "lane": "coder", "kind": "code", "deps": []},
            ],
        }
    )
    assert [c["kind"] for c in children] == ["code", "review", "code"]


def test_unmarked_subtask_serialization_byte_identical_to_main():
    """Operator-directive regression: ``model_dump`` must NOT add ``kind: ''`` for
    an unmarked subtask. The opt-in ``kind`` field was added by A1-classaware; the
    serialized shape of an unmarked subtask must stay byte-identical to pre-classaware
    main (the exact field set that existed before ``kind`` was introduced)."""
    unmarked = BindingSubtask(id="S1", title="Build", lane="coder")
    dumped = unmarked.model_dump(mode="json")
    assert dumped == {
        "id": "S1",
        "title": "Build",
        "lane": "coder",
        "deps": [],
        "body": "",
        "acceptance_criteria": [],
    }
    assert "kind" not in dumped
    # python-mode dump (used by contract.model_dump recursion) is just as clean
    assert "kind" not in unmarked.model_dump()


def test_marked_analysis_subtask_serializes_its_kind():
    """An explicit ``kind: analysis`` marker IS emitted (the opt-in case), so the
    drop-when-empty serializer never hides a set value."""
    analysis = BindingSubtask(id="S2", title="Probe", lane="coder-claude", kind="analysis")
    assert analysis.model_dump(mode="json")["kind"] == "analysis"


def test_taskgraph_hints_subtasks_serialization_omits_empty_kind():
    """The serialized ``subtasks`` array in ``taskgraph.draft.yaml`` (build_taskgraph_draft,
    line 504) and in ``contract.yaml`` (contract.model_dump recursion, line 602) must not
    grow a ``kind: ''`` key for unmarked subtasks, while preserving an analysis marker."""
    hints = TaskgraphHints(
        binding=True,
        subtasks=[
            BindingSubtask(id="S1", title="Build", lane="coder"),
            BindingSubtask(id="S2", title="Probe", lane="coder-claude", kind="analysis"),
        ],
    )
    # line-504 path: [task.model_dump(mode="json") for task in hints.subtasks]
    line504 = [t.model_dump(mode="json") for t in hints.subtasks]
    assert "kind" not in line504[0]
    assert line504[1]["kind"] == "analysis"
    # line-602 path: contract.model_dump recurses into taskgraph_hints.subtasks
    recursed = hints.model_dump(mode="json")["subtasks"]
    assert "kind" not in recursed[0]
    assert recursed[1]["kind"] == "analysis"


def test_children_carry_only_planspec_subtask_id_not_redundant_planspec_id():
    """#10: each child dict carries the subtask id under ``planspec_subtask_id``
    (the A1 migration column name) only — not also under the legacy
    ``planspec_id`` key, which was always set to the same value and read via a
    dead ``or`` fallback in plugin_api."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder", "deps": []},
            ],
        }
    )
    child = children[0]
    assert child["planspec_subtask_id"] == "S1"
    assert "planspec_id" not in child, (
        "redundant legacy key planspec_id should be gone; "
        "use planspec_subtask_id exclusively"
    )


def test_cli_json_success_reports_artifacts(tmp_path: Path, capsys):
    plan = tmp_path / "compiler-plan.md"
    plan.write_text(VALID_PLAN, encoding="utf-8")

    code = main([
        str(plan),
        "--compiled-root",
        str(tmp_path / "compiled"),
        "--templates-root",
        str(tmp_path / "templates"),
        "--json",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["status"] == "GREEN"
    assert Path(payload["artifacts"]["contract"]).exists()
    assert Path(payload["artifacts"]["taskgraph_draft"]).name == "taskgraph.draft.yaml"


# ---------------------------------------------------------------------------
# B1: iteration-budget floor derived from task severity
#
# The flat agent.max_turns default (90) kills genuinely hard tasks mid-flight
# (a `critical` multi-file build, or a `contract`-depth live-test slice): the
# worker times out and the chain blocks. taskgraph_hints_to_children derives a
# per-child max_iterations FLOOR from the signals that actually predict effort
# (review_tier + live_test_depth). Floor semantics, strictly additive:
#   * an explicit per-subtask max_iterations always wins;
#   * an unmarked subtask emits no max_iterations field (default 90 unchanged).
# ---------------------------------------------------------------------------


def test_critical_review_tier_derives_max_iterations_floor():
    """A `critical` review_tier subtask gets an elevated max_iterations floor so
    a hard build does not die at the flat 90-turn default."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Hard build", "lane": "coder",
                 "review_tier": "critical", "deps": []},
            ],
        }
    )
    assert children[0]["max_iterations"] == 220


def test_review_tier_derives_moderate_floor():
    """A `review` review_tier subtask gets a moderate floor (still above default)."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder",
                 "review_tier": "review", "deps": []},
            ],
        }
    )
    assert children[0]["max_iterations"] == 150


def test_contract_live_test_depth_derives_floor_for_all_children():
    """A `contract`-depth PlanSpec means every subtask must build/verify live
    integration tests — a multi-iteration job — so each child gets the
    contract-depth floor even without a per-subtask review_tier."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder", "deps": []},
                {"id": "S2", "title": "Verify", "lane": "verifier", "deps": ["S1"]},
            ],
        },
        live_test_depth="contract",
    )
    assert children[0]["max_iterations"] == 180
    assert children[1]["max_iterations"] == 180


def test_max_iterations_floor_takes_max_of_signals():
    """When both review_tier and live_test_depth apply, the floor is the MAX of
    the two (critical=220 dominates contract=180)."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Hard build", "lane": "coder",
                 "review_tier": "critical", "deps": []},
            ],
        },
        live_test_depth="contract",
    )
    assert children[0]["max_iterations"] == 220


def test_explicit_subtask_max_iterations_always_wins():
    """An explicit per-subtask max_iterations overrides the derived floor (even a
    LOWER explicit value), so an operator/PlanSpec author keeps full control."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Hard build", "lane": "coder",
                 "review_tier": "critical", "max_iterations": 300, "deps": []},
                {"id": "S2", "title": "Capped", "lane": "coder",
                 "review_tier": "critical", "max_iterations": 50, "deps": []},
            ],
        }
    )
    assert children[0]["max_iterations"] == 300
    assert children[1]["max_iterations"] == 50


def test_unmarked_subtask_has_no_max_iterations_field():
    """Default path (no review_tier, no contract depth) emits no max_iterations
    key at all — the child dict stays byte-identical to pre-B1 and the worker
    falls through to the profile default (90)."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder", "deps": []},
            ],
        }
    )
    assert "max_iterations" not in children[0]


def test_smoke_live_test_depth_does_not_raise_floor():
    """Only `contract` depth raises the floor; a lighter `smoke` depth on an
    unmarked subtask stays at the default (no field)."""
    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "S1", "title": "Build", "lane": "coder", "deps": []},
            ],
        },
        live_test_depth="smoke",
    )
    assert "max_iterations" not in children[0]


def test_subtask_max_iterations_serialization_omits_when_unset():
    """Like `kind`/`review_tier`, the opt-in max_iterations field must be dropped
    from the serialized subtask when unset, so unmarked PlanSpecs stay
    byte-identical; an explicit value IS emitted."""
    unmarked = BindingSubtask(id="S1", title="Build", lane="coder")
    assert "max_iterations" not in unmarked.model_dump(mode="json")
    assert "max_iterations" not in unmarked.model_dump()
    marked = BindingSubtask(id="S2", title="Build", lane="coder", max_iterations=220)
    assert marked.model_dump(mode="json")["max_iterations"] == 220


def test_subtask_max_iterations_must_be_positive():
    """A non-positive explicit max_iterations is rejected at model validation."""
    with pytest.raises(ValidationError):
        BindingSubtask(id="S1", title="Build", lane="coder", max_iterations=0)

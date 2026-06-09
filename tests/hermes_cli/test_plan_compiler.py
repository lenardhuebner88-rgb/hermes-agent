"""Tests for hermes-plan-compile contract compiler v1."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from hermes_cli.plan_compiler import compile_plan, main


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

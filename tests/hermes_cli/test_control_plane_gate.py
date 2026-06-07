"""Tests for the One-Orchestrator control-plane gate."""

from __future__ import annotations

import pytest

from hermes_cli.control_plane_gate import (
    GateDecision,
    SubstantiveCoordinatorChangeError,
    coordinator_gate_decision,
    orchestrator_gate_decision,
    reviewer_gate_required,
    validate_reviewer_verdict_metadata,
)

WORKFLOW_ID = "wf-one-orchestrator-20260607"

ORCHESTRATOR_DOCS_PLAN = {
    "workflow_id": WORKFLOW_ID,
    "source_role": "default",
    "goal": "freeze docs/prompt wording for Coordinator retirement",
    "risk_class": "LOW-docs-only",
    "scope_contract": {
        "version": 2,
        "allowed_systems": ["hermes-agent-vault-docs"],
        "forbidden_systems": ["OpenClaw", "Atlas", "Mission-Control", "Telegram"],
        "allowed_tools": ["read_file", "search_files", "patch"],
    },
    "acceptance_criteria": [
        "default is Orchestrator",
        "Reviewer is optional",
        "Coordinator is not a target-hop",
    ],
    "forbidden_actions": ["config edits", "restarts", "kanban dispatch"],
}

RUNTIME_PLAN = {
    **ORCHESTRATOR_DOCS_PLAN,
    "risk_class": "MEDIUM-runtime-behavior",
    "action_class": "profile-config-routing-edit-and-service-change-if-needed",
}

APPROVED_VERDICT = {
    "workflow_id": WORKFLOW_ID,
    "verdict": "APPROVED",
    "blocking_findings": [],
    "required_verification": [],
    "residual_risk": "runtime/config changes remain separate gates",
    "evidence_audited": ["plan_spec", "test_output"],
    "scope_attestation": True,
    "scope_contract_version": 2,
    "forbidden_actions_taken": 0,
}


def test_reviewer_verdict_metadata_requires_gate_fields():
    missing = validate_reviewer_verdict_metadata(
        {
            "workflow_id": WORKFLOW_ID,
            "verdict": "APPROVED",
            "scope_attestation": True,
        },
        expected_workflow_id=WORKFLOW_ID,
    )

    assert missing == [
        "evidence_audited non-empty list",
        "residual_risk",
        "scope_contract_version >= 2",
        "forbidden_actions_taken = 0",
    ]


def test_low_risk_docs_orchestrator_plan_does_not_require_reviewer_or_coordinator():
    assert reviewer_gate_required(ORCHESTRATOR_DOCS_PLAN) is False

    decision = orchestrator_gate_decision(
        plan_spec=ORCHESTRATOR_DOCS_PLAN,
        current_thread_approval="GO docs-only",
    )

    assert decision == GateDecision(
        allowed=True,
        reason="piet_approved_low_risk_or_docs_only",
        blocking_findings=[],
        mechanical_diffs={"source_role": {"from": "default", "to": "orchestrator"}},
    )


def test_missing_approval_blocks_even_low_risk_docs_plan():
    decision = orchestrator_gate_decision(
        plan_spec=ORCHESTRATOR_DOCS_PLAN,
        current_thread_approval=None,
    )

    assert decision == GateDecision(
        allowed=False,
        reason="orchestrator_apply_blocked",
        blocking_findings=["current_thread_piet_approval_required"],
    )


def test_medium_or_runtime_scope_requires_reviewer_or_explicit_piet_override():
    assert reviewer_gate_required(RUNTIME_PLAN) is True

    decision = orchestrator_gate_decision(
        plan_spec=RUNTIME_PLAN,
        current_thread_approval="GO apply",
    )

    assert decision == GateDecision(
        allowed=False,
        reason="orchestrator_apply_blocked",
        blocking_findings=["reviewer_or_explicit_piet_override_required"],
    )


def test_approved_reviewer_allows_runtime_scope_without_coordinator_hop():
    decision = orchestrator_gate_decision(
        plan_spec=RUNTIME_PLAN,
        reviewer_metadata=APPROVED_VERDICT,
        current_thread_approval="GO apply",
    )

    assert decision == GateDecision(
        allowed=True,
        reason="piet_approved_with_reviewer_gate",
        blocking_findings=[],
        mechanical_diffs={"source_role": {"from": "default", "to": "orchestrator"}},
    )


def test_reviewer_needs_revision_blocks_unless_piet_override_is_explicit():
    needs_revision = {**APPROVED_VERDICT, "verdict": "NEEDS_REVISION"}

    blocked = orchestrator_gate_decision(
        plan_spec=RUNTIME_PLAN,
        reviewer_metadata=needs_revision,
        current_thread_approval="GO apply",
    )
    assert blocked == GateDecision(
        allowed=False,
        reason="orchestrator_apply_blocked",
        blocking_findings=["reviewer verdict is NEEDS_REVISION"],
    )

    overridden = orchestrator_gate_decision(
        plan_spec=RUNTIME_PLAN,
        reviewer_metadata=needs_revision,
        current_thread_approval="GO apply",
        piet_override="explicit override after seeing NEEDS_REVISION",
    )
    assert overridden.allowed is True
    assert overridden.reason == "piet_approved_with_reviewer_gate"


def test_runtime_scope_can_use_explicit_piet_override_without_mandatory_reviewer():
    decision = orchestrator_gate_decision(
        plan_spec=RUNTIME_PLAN,
        current_thread_approval="GO apply",
        piet_override="explicit override after risk readout",
    )

    assert decision == GateDecision(
        allowed=True,
        reason="explicit_piet_override_for_review_required_scope",
        blocking_findings=[],
        mechanical_diffs={"source_role": {"from": "default", "to": "orchestrator"}},
    )


# Legacy compatibility: old callers can still prove a mechanical-only Coordinator
# normalization path, but these tests do not make Coordinator a target requirement.

def test_legacy_coordinator_gate_blocks_without_approved_reviewer_verdict_metadata():
    decision = coordinator_gate_decision(
        hub_plan_spec=ORCHESTRATOR_DOCS_PLAN,
        reviewer_metadata={**APPROVED_VERDICT, "verdict": "NEEDS_REVISION"},
        coordinator_plan_spec=ORCHESTRATOR_DOCS_PLAN,
    )

    assert decision == GateDecision(
        allowed=False,
        reason="reviewer_verdict_not_approved",
        blocking_findings=["reviewer verdict is NEEDS_REVISION"],
    )


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("goal", "expanded goal"),
        ("risk_class", "medium"),
        ("scope_contract", {**ORCHESTRATOR_DOCS_PLAN["scope_contract"], "forbidden_systems": ["OpenClaw"]}),
    ],
)
def test_legacy_coordinator_gate_rejects_substantive_non_mechanical_changes(field, new_value):
    changed = {**ORCHESTRATOR_DOCS_PLAN, field: new_value}

    with pytest.raises(SubstantiveCoordinatorChangeError, match=field):
        coordinator_gate_decision(
            hub_plan_spec=ORCHESTRATOR_DOCS_PLAN,
            reviewer_metadata=APPROVED_VERDICT,
            coordinator_plan_spec=changed,
            mechanical_fields={"workflow_id"},
        )


def test_legacy_coordinator_gate_allows_only_mechanical_normalization():
    coordinator_spec = {
        **ORCHESTRATOR_DOCS_PLAN,
        "workflow_id": "wf-normalized-by-legacy-coordinator",
    }

    decision = coordinator_gate_decision(
        hub_plan_spec=ORCHESTRATOR_DOCS_PLAN,
        reviewer_metadata=APPROVED_VERDICT,
        coordinator_plan_spec=coordinator_spec,
        mechanical_fields={"workflow_id"},
    )

    assert decision == GateDecision(
        allowed=True,
        reason="reviewer_approved_and_only_mechanical_changes",
        blocking_findings=[],
        mechanical_diffs={
            "workflow_id": {"from": WORKFLOW_ID, "to": "wf-normalized-by-legacy-coordinator"},
        },
    )

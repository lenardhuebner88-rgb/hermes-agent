"""Tests for the Hub -> Reviewer -> Coordinator target-architecture gate."""

from __future__ import annotations

import pytest

from hermes_cli.control_plane_gate import (
    GateDecision,
    SubstantiveCoordinatorChangeError,
    coordinator_gate_decision,
    validate_reviewer_verdict_metadata,
)


WORKFLOW_ID = "wf-target-arch-20260513"

HUB_PLAN_SPEC = {
    "workflow_id": WORKFLOW_ID,
    "source_role": "hub",
    "goal": "prove target-architecture gate",
    "risk_class": "low",
    "scope_contract": {
        "version": 2,
        "allowed_systems": ["hermes-agent", "hermes-kanban"],
        "forbidden_systems": ["OpenClaw", "Atlas", "Mission-Control", "Telegram"],
        "allowed_tools": ["kanban_show", "kanban_complete"],
    },
    "acceptance_criteria": ["reviewer gates before coordinator"],
}

APPROVED_VERDICT = {
    "workflow_id": WORKFLOW_ID,
    "verdict": "APPROVED",
    "blocking_findings": [],
    "required_verification": [],
    "residual_risk": "none — hermes-only proof",
    "evidence_audited": ["hub_plan_spec", "test_output"],
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


def test_coordinator_gate_blocks_without_approved_reviewer_verdict_metadata():
    decision = coordinator_gate_decision(
        hub_plan_spec=HUB_PLAN_SPEC,
        reviewer_metadata={**APPROVED_VERDICT, "verdict": "NEEDS_REVISION"},
        coordinator_plan_spec=HUB_PLAN_SPEC,
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
        ("scope_contract", {**HUB_PLAN_SPEC["scope_contract"], "forbidden_systems": ["OpenClaw"]}),
    ],
)
def test_coordinator_gate_rejects_substantive_non_mechanical_changes(field, new_value):
    changed = {**HUB_PLAN_SPEC, field: new_value}

    with pytest.raises(SubstantiveCoordinatorChangeError, match=field):
        coordinator_gate_decision(
            hub_plan_spec=HUB_PLAN_SPEC,
            reviewer_metadata=APPROVED_VERDICT,
            coordinator_plan_spec=changed,
            mechanical_fields={"workflow_id"},
        )


def test_coordinator_gate_allows_only_after_approved_verdict_and_mechanical_normalization():
    coordinator_spec = {
        **HUB_PLAN_SPEC,
        "workflow_id": "wf-normalized-by-coordinator",
    }

    decision = coordinator_gate_decision(
        hub_plan_spec=HUB_PLAN_SPEC,
        reviewer_metadata=APPROVED_VERDICT,
        coordinator_plan_spec=coordinator_spec,
        mechanical_fields={"workflow_id"},
    )

    assert decision == GateDecision(
        allowed=True,
        reason="reviewer_approved_and_only_mechanical_changes",
        blocking_findings=[],
        mechanical_diffs={
            "workflow_id": {"from": WORKFLOW_ID, "to": "wf-normalized-by-coordinator"},
        },
    )

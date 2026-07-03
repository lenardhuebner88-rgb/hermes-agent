"""Tests for the One-Orchestrator control-plane gate."""

from __future__ import annotations

import pytest

from hermes_cli.control_plane_gate import (
    GateDecision,
    SubstantiveCoordinatorChangeError,
    classify_review_tier,
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

LIVE_FALSE_POSITIVE_TASK_FIXTURES = (
    (
        "t_30804f14",
        "worker_gate Repo-Identitäts-Lookup für isolierte Worktrees (Stamp fürs Hermes-Repo ermöglichen)",
        "hermes_cli/kanban_db.py, _submit_for_review, aktuell exakter resolved-Pfad-Match",
        "code",
    ),
    (
        "t_e61f48fc-anti-scope",
        "Review-Wert-Telemetrie end-to-end",
        "KEIN Schema-/Migrations-Change an der DB; Aggregation liest task_runs.metadata-JSON zur Query-Zeit.",
        "code",
    ),
    (
        "t_ae5ecc3a",
        "Reviewer-SOUL v2 draften: Code-Diff-Auftrag, Nicht-Wiederholen-Klausel, Pflicht-Fund-Metadata",
        "Vollständiger Drop-in-Draft für ~/.hermes/profiles/reviewer/SOUL.md als Task-Artefakt.",
        "code",
    ),
    (
        "t_e61f48fc-visual-ac",
        "Review-Wert-Telemetrie end-to-end",
        "headless Screenshot von /control/statistik in Desktop- UND >=390px-Viewport über den auth-enabled Visual-Harness",
        "code",
    ),
)

LIVE_REVIEW_REQUIRED_FALSE_POSITIVE_TASK_FIXTURES = (
    (
        "t_e7926429",
        "WEGWERF S7-Push-Beweis",
        "Wegwerf.",
        "",
    ),
    (
        "t_aca040b5",
        "PlanSpec ESCALATION-RELEASE-GATE-ERROR-CONTEXT: Release-Gate/Silent-Block-Eskalationen mit Fehlerkontext befuellen, damit sie klassifizierbar sind",
        "grounding: Auditor-Sample (7-Tage, kanban.db read-only): 3 von 18 unclassified distinct roots sind [Release-Gate]-Tasks mit last_error='' und blocked_kind='retryable'",
        "",
    ),
)

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


# --- FRD Phase 1b: flag-gated disposition enforcement (scope_contract_version >= 3) ---


def test_disposition_not_required_below_v3():
    """A complete v2 verdict without a disposition block stays valid (backward-compat)."""
    md = dict(APPROVED_VERDICT)  # scope_contract_version == 2, no disposition key
    missing = validate_reviewer_verdict_metadata(md, expected_workflow_id=WORKFLOW_ID)
    assert missing == []


def test_disposition_required_at_v3_when_absent():
    """At scope_contract_version >= 3 a missing disposition block is flagged."""
    md = dict(APPROVED_VERDICT)
    md["scope_contract_version"] = 3
    missing = validate_reviewer_verdict_metadata(md, expected_workflow_id=WORKFLOW_ID)
    assert any("disposition" in m for m in missing)


def test_disposition_empty_items_is_valid_at_v3():
    """An explicit empty items=[] is the legitimate 'nothing open' outcome — not flagged."""
    md = dict(APPROVED_VERDICT)
    md["scope_contract_version"] = 3
    md["disposition"] = {"items": []}
    missing = validate_reviewer_verdict_metadata(md, expected_workflow_id=WORKFLOW_ID)
    assert missing == []


def test_disposition_valid_items_pass_at_v3():
    """A well-formed disposition item passes the gate at v3."""
    md = dict(APPROVED_VERDICT)
    md["scope_contract_version"] = 3
    md["disposition"] = {
        "items": [
            {
                "typ": "risk",
                "disposition": "defer",
                "next_action": "harden severity validation",
                "severity": "real-risk",
                "evidence": "control_plane_gate.py:215",
            }
        ]
    }
    missing = validate_reviewer_verdict_metadata(md, expected_workflow_id=WORKFLOW_ID)
    assert missing == []


def test_disposition_refusal_marker_flagged_at_v3():
    """A disposition block carrying an llm-refusal/truncation marker fails at v3."""
    md = dict(APPROVED_VERDICT)
    md["scope_contract_version"] = 3
    md["disposition"] = {"__llm_refusal__": True, "items": []}
    missing = validate_reviewer_verdict_metadata(md, expected_workflow_id=WORKFLOW_ID)
    assert any("disposition" in m for m in missing)


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


def test_classify_review_tier_levels():
    """B-T4: None/low → standard; gate-required non-critical → review;
    DB/deploy/security markers → critical."""
    assert classify_review_tier(None) == "standard"
    assert (
        classify_review_tier({
            "risk_class": "low",
            "action_class": "",
            "scope": "docs only",
        })
        == "standard"
    )
    assert (
        classify_review_tier({
            "risk_class": "medium",
            "objective": "refactor code module",
        })
        == "review"
    )
    assert (
        classify_review_tier({
            "risk_class": "high",
            "objective": "run database migration + deploy",
        })
        == "critical"
    )


def test_classify_review_tier_does_not_critical_on_path_or_subword_markers():
    """Critical markers must be real risk words, not substrings in paths/titles."""
    assert (
        classify_review_tier({
            "risk_class": "medium",
            "objective": "fix hermes_cli/kanban_db.py",
        })
        == "review"
    )
    assert (
        classify_review_tier({
            "risk_class": "medium",
            "objective": "adjust database.py helper",
        })
        == "review"
    )
    assert (
        classify_review_tier({
            "risk_class": "medium",
            "objective": "document drop-in config snippet",
        })
        == "review"
    )


@pytest.mark.parametrize("task_id,title,body,kind", LIVE_FALSE_POSITIVE_TASK_FIXTURES)
def test_classify_review_tier_live_false_positive_fixtures_stay_review(
    task_id, title, body, kind
):
    """Live 2026-07-02 fixture snippets, using the _task_plan_spec field shape."""
    spec = {"risk_class": kind, "objective": title, "goal": body}

    assert reviewer_gate_required(spec) is True, task_id
    assert classify_review_tier(spec) == "review", task_id


@pytest.mark.parametrize(
    "task_id,title,body,kind", LIVE_REVIEW_REQUIRED_FALSE_POSITIVE_TASK_FIXTURES
)
def test_reviewer_gate_required_live_substring_overfire_fixtures_stay_standard(
    task_id, title, body, kind
):
    """Live 2026-07-03 task snippets: substrings in hyphenated/path-ish text are not risk markers."""
    spec = {"risk_class": kind, "objective": title, "goal": body}

    assert reviewer_gate_required(spec) is False, task_id
    assert classify_review_tier(spec) == "standard", task_id


def test_classify_review_tier_ignores_anti_scope_critical_words():
    assert (
        classify_review_tier({
            "risk_class": "medium",
            "objective": "refactor code module",
            "goal": "no database migration, no deploy, no secrets, no auth changes",
            "forbidden_actions": ["drop tables", "alter db schema", "deploy"],
        })
        == "review"
    )


def test_classify_review_tier_does_not_let_negated_marker_hide_real_critical():
    assert (
        classify_review_tier({
            "risk_class": "high",
            "objective": "no database migration but deploy gateway change",
        })
        == "critical"
    )
    assert (
        classify_review_tier({
            "risk_class": "high",
            "objective": "release gateway",
            "goal": "no database migration",
            "allowed_actions": ["deploy gateway change"],
        })
        == "critical"
    )


def test_classify_review_tier_keeps_true_critical_word_family_markers():
    for objective in (
        "DB-Migration durchführen",
        "run DB migration",
        "run migrations",
        "apply ALTER TABLE to database",
        "schema alterations",
        "deploy gateway change",
        "production deployment",
        "rotate credential for auth service",
        "rotate credentials",
        "read secrets from vault",
    ):
        assert (
            classify_review_tier({"risk_class": "high", "objective": objective})
            == "critical"
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


def test_false_piet_override_does_not_bypass_reviewer_gate():
    decision = orchestrator_gate_decision(
        plan_spec=RUNTIME_PLAN,
        current_thread_approval="GO apply",
        piet_override=False,
    )

    assert decision == GateDecision(
        allowed=False,
        reason="orchestrator_apply_blocked",
        blocking_findings=["reviewer_or_explicit_piet_override_required"],
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
        (
            "scope_contract",
            {
                **ORCHESTRATOR_DOCS_PLAN["scope_contract"],
                "forbidden_systems": ["OpenClaw"],
            },
        ),
    ],
)
def test_legacy_coordinator_gate_rejects_substantive_non_mechanical_changes(
    field, new_value
):
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
            "workflow_id": {
                "from": WORKFLOW_ID,
                "to": "wf-normalized-by-legacy-coordinator",
            },
        },
    )

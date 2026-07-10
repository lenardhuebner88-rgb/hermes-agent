from __future__ import annotations

import pytest

from hermes_cli.autoresearch_lane_contracts import (
    LaneContractError,
    classify_lane_outcome,
    load_lane_specs,
    nightly_exit_code,
)


def test_default_lane_specs_make_safety_contracts_explicit():
    specs = load_lane_specs(config={})

    assert specs["skill"].aux_task == "skills_hub"
    assert specs["skill"].independent_gate == "operator_judge"
    assert specs["code"].mutation_policy == "proposal_only"
    assert specs["deep-audit"].mutation_policy == "read_only"
    assert specs["test-foundry"].independent_gate == "mutation_gate"
    assert specs["test-foundry"].aux_task == "test_hardening"
    assert "config.yaml" in specs["code"].forbidden_paths
    assert specs["deep-audit"].model_role == "read-only subsystem auditor"
    assert "zero grounded findings is clean" in specs["deep-audit"].failure_semantics


def test_lane_spec_overrides_are_validated():
    with pytest.raises(LaneContractError, match="mutation_policy"):
        load_lane_specs(config={"autoresearch": {"lanes": {"code": {"mutation_policy": "write-live"}}}})


def test_all_errors_are_infra_failed_but_authenticated_zero_findings_are_clean():
    failed = classify_lane_outcome("code", scanned=18, errors=18, yielded=0, ok=True)
    clean = classify_lane_outcome("deep-audit", scanned=12, errors=0, yielded=0, ok=True)

    assert failed.outcome == "infra_failed"
    assert failed.fatal is True
    assert clean.outcome == "clean"
    assert clean.fatal is False


def test_auth_failure_and_expected_skip_are_distinct():
    failed = classify_lane_outcome(
        "deep-audit", scanned=0, errors=1, yielded=0, ok=False,
        reason="AuthenticationError: invalid API key",
    )
    skipped = classify_lane_outcome(
        "test-foundry", scanned=0, errors=0, yielded=0, ok=False,
        reason="target file is not clean in the main checkout",
    )

    assert failed.outcome == "infra_failed"
    assert skipped.outcome == "skipped_expected"


def test_missing_provider_model_is_infra_not_expected_file_skip():
    failed = classify_lane_outcome(
        "code",
        scanned=12,
        errors=12,
        yielded=0,
        ok=False,
        reason="Error code: 404 - configured model not found",
    )

    assert failed.outcome == "infra_failed"
    assert failed.fatal is True


def test_nightly_only_fails_nonzero_when_every_selected_lane_failed():
    fatal = classify_lane_outcome("deep-audit", scanned=0, errors=1, yielded=0, ok=False, reason="timeout")
    clean = classify_lane_outcome("test-foundry", scanned=3, errors=0, yielded=0, ok=False,
                                  reason="no validated mutation tests kept")

    assert nightly_exit_code([fatal, fatal]) == 2
    assert nightly_exit_code([fatal, clean]) == 0

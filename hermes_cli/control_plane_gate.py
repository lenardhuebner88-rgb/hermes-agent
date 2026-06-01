"""Target control-plane gate helpers for Hub -> Reviewer -> Coordinator.

These helpers are intentionally pure and side-effect free. They encode the
contract Piet wants for the target architecture: Hub owns the execution-ready
PlanSpec, Reviewer returns a verdict-only gate, and Coordinator may only perform
mechanical normalization after an APPROVED reviewer verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


VERDICTS = {"APPROVED", "NEEDS_REVISION", "BLOCKED"}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    blocking_findings: list[str]
    mechanical_diffs: dict[str, dict[str, Any]] = field(default_factory=dict)


class SubstantiveCoordinatorChangeError(ValueError):
    """Raised when Coordinator changes Hub-owned plan semantics."""



def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "ok", "pass", "passed"}
    return bool(value)



def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None



def validate_reviewer_verdict_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    expected_workflow_id: str | None = None,
) -> list[str]:
    """Return missing/invalid verdict metadata fields.

    The reviewer verdict is the only thing that unlocks Coordinator takeover in
    the target architecture. Keep this strict and small: enough to prove audit
    trail, scope attestation, and verdict identity without coupling to any live
    system.
    """
    if not isinstance(metadata, Mapping):
        return ["metadata object is required"]

    missing: list[str] = []
    workflow_id = metadata.get("workflow_id")
    if expected_workflow_id is not None and workflow_id != expected_workflow_id:
        missing.append("workflow_id mismatch")
    elif not workflow_id:
        missing.append("workflow_id")

    verdict = metadata.get("verdict")
    if verdict not in VERDICTS:
        missing.append("verdict in APPROVED|NEEDS_REVISION|BLOCKED")

    evidence = metadata.get("evidence_audited")
    if not isinstance(evidence, list) or not evidence:
        missing.append("evidence_audited non-empty list")

    if not metadata.get("residual_risk"):
        missing.append("residual_risk")

    if not _truthy(metadata.get("scope_attestation")):
        missing.append("scope_attestation = true")

    version = _int_value(metadata.get("scope_contract_version"))
    if version is None or version < 2:
        missing.append("scope_contract_version >= 2")

    forbidden = _int_value(metadata.get("forbidden_actions_taken"))
    if forbidden is None:
        missing.append("forbidden_actions_taken = 0")
    elif forbidden != 0:
        missing.append("forbidden_actions_taken must be 0")

    return missing



def _substantive_diffs(
    *,
    hub_plan_spec: Mapping[str, Any],
    coordinator_plan_spec: Mapping[str, Any],
    mechanical_fields: set[str],
) -> list[str]:
    diffs: list[str] = []
    keys = set(hub_plan_spec) | set(coordinator_plan_spec)
    for key in sorted(keys):
        if key in mechanical_fields:
            continue
        if hub_plan_spec.get(key) != coordinator_plan_spec.get(key):
            diffs.append(key)
    return diffs


def _mechanical_diffs(
    *,
    hub_plan_spec: Mapping[str, Any],
    coordinator_plan_spec: Mapping[str, Any],
    mechanical_fields: set[str],
) -> dict[str, dict[str, Any]]:
    diffs: dict[str, dict[str, Any]] = {}
    for key in sorted(mechanical_fields):
        hub_value = hub_plan_spec.get(key)
        coordinator_value = coordinator_plan_spec.get(key)
        if hub_value != coordinator_value:
            diffs[key] = {"from": hub_value, "to": coordinator_value}
    return diffs



def coordinator_gate_decision(
    *,
    hub_plan_spec: Mapping[str, Any],
    reviewer_metadata: Mapping[str, Any] | None,
    coordinator_plan_spec: Mapping[str, Any],
    mechanical_fields: Iterable[str] = (),
) -> GateDecision:
    """Decide whether Coordinator may take over a Hub-authored PlanSpec."""
    expected_workflow_id = str(hub_plan_spec.get("workflow_id") or "") or None
    verdict_missing = validate_reviewer_verdict_metadata(
        reviewer_metadata,
        expected_workflow_id=expected_workflow_id,
    )
    if verdict_missing:
        return GateDecision(
            allowed=False,
            reason="reviewer_verdict_metadata_invalid",
            blocking_findings=verdict_missing,
        )

    verdict = reviewer_metadata.get("verdict") if isinstance(reviewer_metadata, Mapping) else None
    if verdict != "APPROVED":
        return GateDecision(
            allowed=False,
            reason="reviewer_verdict_not_approved",
            blocking_findings=[f"reviewer verdict is {verdict}"],
        )

    mechanical = set(mechanical_fields)
    diffs = _substantive_diffs(
        hub_plan_spec=hub_plan_spec,
        coordinator_plan_spec=coordinator_plan_spec,
        mechanical_fields=mechanical,
    )
    if diffs:
        raise SubstantiveCoordinatorChangeError(
            "Coordinator changed Hub-owned PlanSpec fields: " + ", ".join(diffs)
        )

    return GateDecision(
        allowed=True,
        reason="reviewer_approved_and_only_mechanical_changes",
        blocking_findings=[],
        mechanical_diffs=_mechanical_diffs(
            hub_plan_spec=hub_plan_spec,
            coordinator_plan_spec=coordinator_plan_spec,
            mechanical_fields=mechanical,
        ),
    )

"""Pure control-plane gate helpers for the One-Orchestrator target.

These helpers are intentionally side-effect free.  The current target contract is:

* ``default`` is normalized to the canonical operator-facing role
  ``orchestrator``.
* Reviewer verdicts are optional for low-risk/docs-only scopes, but useful and
  gate-enforced for medium/high-risk, code, config, runtime, cron/systemd,
  credential, database, deployment, or restart scopes unless Piet explicitly
  overrides in the current thread.
* Coordinator is not a target-hop requirement.  Any remaining Coordinator
  runtime ownership is a separately inventoried retirement dependency, not an
  apply prerequisite encoded here.

The legacy ``coordinator_gate_decision`` remains for older callers that still
need to prove a mechanical-only Coordinator normalization path, but new routing
logic should use ``orchestrator_gate_decision``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


VERDICTS = {"APPROVED", "NEEDS_REVISION", "BLOCKED"}
_REVIEW_REQUIRED_MARKERS = {
    "medium",
    "high",
    "critical",
    "code",
    "config",
    "profile",
    "runtime",
    "restart",
    "reload",
    "deploy",
    "build",
    "cron",
    "timer",
    "systemd",
    "gateway",
    "secret",
    "credential",
    "auth",
    "database",
    "db",
    "kanban-dispatch",
    "dispatch",
    "push",
}
# B (staged review gate): the strong markers that escalate an already
# review-required task all the way to the 'critical' tier (verifier→reviewer→
# critic). Conservative on purpose — DB writes, deploys and secrets/auth only.
_CRITICAL_REVIEW_MARKERS = frozenset({
    "database",
    "db",
    "migration",
    "deploy",
    "secret",
    "credential",
    "drop",
    "alter",
    "auth",
})


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    blocking_findings: list[str]
    mechanical_diffs: dict[str, dict[str, Any]] = field(default_factory=dict)


class SubstantiveCoordinatorChangeError(ValueError):
    """Raised when Coordinator changes Orchestrator/Hub-owned plan semantics."""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {"true", "yes", "1", "ok", "pass", "passed", "approved", "go"} or normalized.startswith("go ")
    return bool(value)


def _explicit_override(value: Any) -> bool:
    """True for explicit non-empty override evidence, but not false literals."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        return bool(normalized) and normalized not in {"false", "no", "0", "none", "null", "off"}
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


def _normalize_role(role: Any) -> str | None:
    if role is None:
        return None
    normalized = str(role).strip().lower().replace("-", "_")
    if normalized in {"default", "hub", "orchestrator"}:
        return "orchestrator"
    return normalized or None


def _plan_text(plan_spec: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("risk_class", "action_class", "scope", "objective", "goal"):
        value = plan_spec.get(key)
        if value is not None:
            values.append(str(value))
    for key in ("allowed_actions", "forbidden_actions", "changed_paths"):
        value = plan_spec.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(values).lower()


def reviewer_gate_required(plan_spec: Mapping[str, Any] | None) -> bool:
    """Return whether the plan's risk/scope requires Reviewer or Piet override.

    This is a conservative text/field classifier for control-plane gate helpers;
    it does not dispatch, inspect files, or infer approval from Discord pointers.
    Low-risk/docs-only scopes can proceed with explicit Piet approval and no
    Reviewer verdict.  Risky scopes require either an APPROVED Reviewer verdict
    or an explicit current-thread Piet override after seeing the risk.
    """
    if not isinstance(plan_spec, Mapping):
        return True
    text = _plan_text(plan_spec)
    if any(marker in text for marker in _REVIEW_REQUIRED_MARKERS):
        # Keep docs-only low-risk work out of mandatory review if the risky words
        # only appear in anti-scope / forbidden actions.
        risk = str(plan_spec.get("risk_class") or "").lower()
        action = str(plan_spec.get("action_class") or "").lower()
        if "low" in risk and "docs" in text and not action:
            return False
        return True
    return False


def classify_review_tier(plan_spec: Mapping[str, Any] | None) -> str:
    """Auto-risk → staged-review tier ∈ {standard, review, critical}.

    Conservative by design and reuses the existing review classifier:
    ``standard`` whenever there is no spec or no reviewer gate is required;
    ``critical`` only when a strong DB/deploy/security marker is present; every
    other gate-required scope is ``review``. NULL/None → ``standard`` so an
    unclassified task keeps today's single-verifier behavior.
    """
    if not plan_spec or not reviewer_gate_required(plan_spec):
        return "standard"
    blob = _plan_text(plan_spec)
    if any(marker in blob for marker in _CRITICAL_REVIEW_MARKERS):
        return "critical"
    return "review"


def validate_reviewer_verdict_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    expected_workflow_id: str | None = None,
) -> list[str]:
    """Return missing/invalid verdict metadata fields.

    Reviewer verdicts are optional for low-risk/docs-only Orchestrator work, but
    whenever a Reviewer verdict is supplied or required it must carry enough
    evidence to be auditable.
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


def _role_mechanical_diff(plan_spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    role = plan_spec.get("source_role")
    normalized = _normalize_role(role)
    if role is not None and normalized != role:
        return {"source_role": {"from": role, "to": normalized}}
    return {}


def orchestrator_gate_decision(
    *,
    plan_spec: Mapping[str, Any],
    reviewer_metadata: Mapping[str, Any] | None = None,
    current_thread_approval: Any = None,
    piet_override: Any = None,
) -> GateDecision:
    """Decide whether an Orchestrator-authored plan may proceed to apply.

    The function encodes the One-Orchestrator target without requiring a
    Coordinator takeover.  It never treats a Discord pointer as approval; callers
    must pass explicit current-thread approval evidence.
    """
    if not isinstance(plan_spec, Mapping):
        return GateDecision(False, "plan_spec_invalid", ["plan_spec object is required"])

    findings: list[str] = []
    if not _truthy(current_thread_approval):
        findings.append("current_thread_piet_approval_required")

    expected_workflow_id = str(plan_spec.get("workflow_id") or "") or None
    needs_review = reviewer_gate_required(plan_spec)
    override = _explicit_override(piet_override) if piet_override is not None else False

    if reviewer_metadata is not None:
        verdict_missing = validate_reviewer_verdict_metadata(
            reviewer_metadata,
            expected_workflow_id=expected_workflow_id,
        )
        if verdict_missing:
            findings.extend(f"reviewer metadata invalid: {item}" for item in verdict_missing)
        else:
            verdict = reviewer_metadata.get("verdict")
            if verdict != "APPROVED" and not override:
                findings.append(f"reviewer verdict is {verdict}")
    elif needs_review and not override:
        findings.append("reviewer_or_explicit_piet_override_required")

    if findings:
        return GateDecision(False, "orchestrator_apply_blocked", findings)

    if needs_review and override and reviewer_metadata is None:
        reason = "explicit_piet_override_for_review_required_scope"
    elif reviewer_metadata is not None:
        reason = "piet_approved_with_reviewer_gate"
    else:
        reason = "piet_approved_low_risk_or_docs_only"

    return GateDecision(
        allowed=True,
        reason=reason,
        blocking_findings=[],
        mechanical_diffs=_role_mechanical_diff(plan_spec),
    )


def coordinator_gate_decision(
    *,
    hub_plan_spec: Mapping[str, Any],
    reviewer_metadata: Mapping[str, Any] | None,
    coordinator_plan_spec: Mapping[str, Any],
    mechanical_fields: Iterable[str] = (),
) -> GateDecision:
    """Legacy: decide whether Coordinator may do mechanical-only normalization.

    This helper is kept for compatibility with old scoped commit/gate tests.  It
    must not be interpreted as target architecture requiring Coordinator.
    """
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
            "Coordinator changed Orchestrator-owned PlanSpec fields: " + ", ".join(diffs)
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

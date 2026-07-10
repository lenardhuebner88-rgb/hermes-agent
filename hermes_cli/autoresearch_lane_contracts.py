"""Validated contracts and truthful outcomes for every Autoresearch lane.

This is the single policy surface shared by the nightly entrypoints.  Execution
code may still live in lane-specific modules, but safety, routing and failure
semantics must not drift between scripts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class LaneContractError(ValueError):
    """Raised when an operator-provided lane contract is unsafe or malformed."""


OUTCOMES = {
    "yielded",
    "clean",
    "degraded",
    "skipped_expected",
    "budget_exhausted",
    "infra_failed",
    "invalid_output",
}
FATAL_OUTCOMES = {"infra_failed", "invalid_output"}
HEALTHY_OUTCOMES = {"yielded", "clean", "degraded"}

_MUTATION_POLICIES = {"read_only", "proposal_only", "isolated_worktree"}
_GATES = {"operator_judge", "importance_verifier", "staged_review", "mutation_gate"}


@dataclass(frozen=True)
class LaneSpec:
    name: str
    aux_task: str
    model_role: str
    scopes: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    mutation_policy: str
    tools: tuple[str, ...]
    independent_gate: str
    evaluator: str
    output_schema: str
    route: str
    schedule: str
    budget: Mapping[str, Any]
    required_evidence: tuple[str, ...]
    failure_semantics: str
    fail_on_all_errors: bool
    allow_empty_success: bool


@dataclass(frozen=True)
class LaneOutcome:
    lane: str
    outcome: str
    scanned: int
    errors: int
    yielded: int
    reason: str = ""

    @property
    def fatal(self) -> bool:
        return self.outcome in FATAL_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "outcome": self.outcome,
            "fatal": self.fatal,
            "scanned": self.scanned,
            "errors": self.errors,
            "yielded": self.yielded,
            "reason": self.reason,
        }


_DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    "skill": {
        "aux_task": "skills_hub",
        "model_role": "grounded skill capability critic",
        "scopes": ["configured skill roots", "usage-ranked long tail"],
        "forbidden_paths": ["outside configured skill roots", ".env", "auth.json", "config.yaml", "*.db"],
        "mutation_policy": "proposal_only",
        "tools": ["skill reader", "grounded proposal writer"],
        "independent_gate": "operator_judge",
        "evaluator": "local skill eval, then independent operator judge",
        "output_schema": "autoresearch-proposal-v1",
        "route": "proposal queue -> judge -> batch confirm",
        "schedule": "rotating nightly",
        "budget": {"max_iterations_source": "AR_NIGHTLY_ITERATIONS"},
        "required_evidence": ["target_path", "evidence", "fix_hint"],
        "failure_semantics": "100% research errors or provider/auth failure is infra_failed",
        "fail_on_all_errors": True,
        "allow_empty_success": True,
    },
    "code": {
        "aux_task": "code_audit",
        "model_role": "grounded code weakness auditor",
        "scopes": ["hermes_cli allowlist", "incremental content hashes"],
        "forbidden_paths": ["outside code allowlist", ".env", "auth.json", "config.yaml", "*.db"],
        "mutation_policy": "proposal_only",
        "tools": ["allowlisted file reader", "verbatim evidence verifier"],
        "independent_gate": "importance_verifier",
        "evaluator": "verbatim evidence validation plus independent importance verifier",
        "output_schema": "autoresearch-proposal-v1",
        "route": "grounded contract -> deduped Kanban code task",
        "schedule": "rotating nightly",
        "budget": {"max_files": 40, "max_proposals": 8},
        "required_evidence": ["target_path", "evidence", "fix_hint"],
        "failure_semantics": "100% file errors or provider/auth failure is infra_failed",
        "fail_on_all_errors": True,
        "allow_empty_success": True,
    },
    "deep-audit": {
        "aux_task": "code_audit",
        "model_role": "read-only subsystem auditor",
        "scopes": ["configured subsystem globs", "read-only allowlist"],
        "forbidden_paths": ["outside subsystem allowlist", ".env", "auth.json", "config.yaml", "*.db"],
        "mutation_policy": "read_only",
        "tools": ["read_file", "grep", "list_dir", "report_finding", "finish_audit"],
        "independent_gate": "staged_review",
        "evaluator": "tool-protocol validation, verbatim evidence validation, staged review",
        "output_schema": "deep-audit-v1",
        "route": "grounded contract -> deduped Kanban code task",
        "schedule": "nightly subsystem rotation",
        "budget": {"max_files": 12, "wall_clock_source": "AR_V2_WALL_CLOCK_BUDGET_SECONDS"},
        "required_evidence": ["fileline", "evidence", "fix_hint"],
        "failure_semantics": "provider/auth/tool-loop failure is infra_failed; zero grounded findings is clean",
        "fail_on_all_errors": True,
        "allow_empty_success": True,
    },
    "test-foundry": {
        "aux_task": "test_hardening",
        "model_role": "mutation-test hardener",
        "scopes": ["curated Python targets", "affected tests"],
        "forbidden_paths": ["live checkout writes", "protected branches", ".env", "auth.json", "config.yaml", "*.db"],
        "mutation_policy": "isolated_worktree",
        "tools": ["mutation generator", "affected test runner", "proposal writer"],
        "independent_gate": "mutation_gate",
        "evaluator": "green HEAD, red target mutant, green unrelated mutant",
        "output_schema": "test-foundry-v1",
        "route": "validated test proposal -> deduped Kanban code task",
        "schedule": "nightly target rotation",
        "budget": {"max_mutants": 15, "targets": 2, "wall_clock_source": "AR_V2_WALL_CLOCK_BUDGET_SECONDS"},
        "required_evidence": ["target_path", "mutation", "affected_tests", "fix_hint"],
        "failure_semantics": "all survivor generations failing is infra_failed; zero validated tests with healthy calls is clean",
        "fail_on_all_errors": True,
        "allow_empty_success": True,
    },
}


def _string_tuple(value: Any, *, field: str, lane: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise LaneContractError(f"{lane}.{field} must be a list")
    out = tuple(str(item).strip() for item in value if str(item).strip())
    if not out:
        raise LaneContractError(f"{lane}.{field} must not be empty")
    return out


def _validated(name: str, raw: Mapping[str, Any]) -> LaneSpec:
    mutation = str(raw.get("mutation_policy") or "").strip()
    if mutation not in _MUTATION_POLICIES:
        raise LaneContractError(
            f"{name}.mutation_policy must be one of {sorted(_MUTATION_POLICIES)}"
        )
    gate = str(raw.get("independent_gate") or "").strip()
    if gate not in _GATES:
        raise LaneContractError(f"{name}.independent_gate must be one of {sorted(_GATES)}")
    budget = raw.get("budget")
    if not isinstance(budget, Mapping):
        raise LaneContractError(f"{name}.budget must be a mapping")
    required_strings = (
        "aux_task", "model_role", "evaluator", "output_schema", "route", "schedule",
        "failure_semantics",
    )
    values = {field: str(raw.get(field) or "").strip() for field in required_strings}
    missing = [field for field, value in values.items() if not value]
    if missing:
        raise LaneContractError(f"{name} missing required fields: {', '.join(missing)}")
    return LaneSpec(
        name=name,
        aux_task=values["aux_task"],
        model_role=values["model_role"],
        scopes=_string_tuple(raw.get("scopes"), field="scopes", lane=name),
        forbidden_paths=_string_tuple(
            raw.get("forbidden_paths"), field="forbidden_paths", lane=name
        ),
        mutation_policy=mutation,
        tools=_string_tuple(raw.get("tools"), field="tools", lane=name),
        independent_gate=gate,
        evaluator=values["evaluator"],
        output_schema=values["output_schema"],
        route=values["route"],
        schedule=values["schedule"],
        budget=dict(budget),
        required_evidence=_string_tuple(
            raw.get("required_evidence"), field="required_evidence", lane=name
        ),
        failure_semantics=values["failure_semantics"],
        fail_on_all_errors=bool(raw.get("fail_on_all_errors", True)),
        allow_empty_success=bool(raw.get("allow_empty_success", True)),
    )


def load_lane_specs(*, config: Mapping[str, Any] | None = None) -> dict[str, LaneSpec]:
    """Load defaults plus validated ``autoresearch.lanes`` overrides."""
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    overrides = ((config.get("autoresearch") or {}).get("lanes") or {})
    if not isinstance(overrides, Mapping):
        raise LaneContractError("autoresearch.lanes must be a mapping")
    unknown = sorted(set(overrides) - set(_DEFAULT_SPECS))
    if unknown:
        raise LaneContractError(f"unknown autoresearch lanes: {', '.join(unknown)}")
    specs: dict[str, LaneSpec] = {}
    for name, defaults in _DEFAULT_SPECS.items():
        override = overrides.get(name) or {}
        if not isinstance(override, Mapping):
            raise LaneContractError(f"autoresearch.lanes.{name} must be a mapping")
        merged = {**defaults, **dict(override)}
        specs[name] = _validated(name, merged)
    return specs


_INFRA_MARKERS = (
    "authenticationerror",
    "invalid api key",
    "unauthorized",
    "http 401",
    "payment required",
    "http 402",
    "insufficient credit",
    "timeout",
    "timed out",
    "connection error",
    "connection refused",
    "llm failed",
    "provider unavailable",
    "model not found",
    "model does not exist",
    "unknown model",
)
_EXPECTED_SKIP_MARKERS = (
    "not clean",
    "no affected tests",
    "not found",
    "no files resolved",
    "baseline tests failed",
    "skipped: wall-clock budget exhausted",
)
_INVALID_OUTPUT_MARKERS = (
    "invalid finding",
    "invalid output",
    "schema validation",
    "max iterations reached before finish_audit",
)


def classify_lane_outcome(
    lane: str,
    *,
    scanned: int,
    errors: int,
    yielded: int,
    ok: bool,
    reason: str = "",
    specs: Mapping[str, LaneSpec] | None = None,
) -> LaneOutcome:
    lane_specs = dict(specs or load_lane_specs())
    if lane not in lane_specs:
        raise LaneContractError(f"unknown lane: {lane}")
    spec = lane_specs[lane]
    scanned_i = max(0, int(scanned or 0))
    errors_i = max(0, int(errors or 0))
    yielded_i = max(0, int(yielded or 0))
    text = str(reason or "").strip()
    lower = text.lower()

    if "budget exhausted" in lower:
        outcome = "budget_exhausted"
    elif any(marker in lower for marker in _INFRA_MARKERS):
        outcome = "infra_failed"
    elif any(marker in lower for marker in _INVALID_OUTPUT_MARKERS):
        outcome = "invalid_output"
    elif any(marker in lower for marker in _EXPECTED_SKIP_MARKERS):
        outcome = "skipped_expected"
    elif spec.fail_on_all_errors and scanned_i > 0 and errors_i >= scanned_i:
        outcome = "infra_failed"
    elif not ok and errors_i > 0 and scanned_i == 0:
        outcome = "infra_failed"
    elif errors_i > 0:
        outcome = "degraded"
    elif yielded_i > 0:
        outcome = "yielded"
    elif ok or spec.allow_empty_success:
        outcome = "clean"
    else:
        outcome = "invalid_output"
    return LaneOutcome(lane, outcome, scanned_i, errors_i, yielded_i, text)


def nightly_exit_code(outcomes: list[LaneOutcome]) -> int:
    """Fail only a night with a fatal lane and no successfully evaluated lane."""
    if not outcomes:
        return 0
    if any(item.outcome in HEALTHY_OUTCOMES for item in outcomes):
        return 0
    return 2 if any(item.fatal for item in outcomes) else 0

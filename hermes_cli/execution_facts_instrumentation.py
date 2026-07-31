"""Content-free tool and test span adapters for execution-facts V1."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionEvent,
    ExecutionSurface,
    SpanKind,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_idempotency_key,
    stable_span_id,
)

_TOOL_ALIASES = {
    "bash": "terminal",
    "functions.exec_command": "terminal",
    "functions.write_stdin": "terminal",
    "shell": "terminal",
    "terminal": "terminal",
    "functions.apply_patch": "filesystem_edit",
    "apply_patch": "filesystem_edit",
    "codegraph": "code_intelligence",
    "codegraph_explore": "code_intelligence",
    "web": "web",
    "web.run": "web",
    "read": "filesystem_read",
    "grep": "filesystem_read",
    "rg": "filesystem_read",
}
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,126}$")
_SAFE_HASH = re.compile(r"^[a-f0-9]{64}$")


def canonical_tool_name(raw_tool_name: str) -> str:
    """Map adapter-specific spellings without discarding the raw name."""
    lowered = raw_tool_name.strip().lower()
    if not _SAFE_TOOL_NAME.fullmatch(raw_tool_name):
        raise ValueError("raw tool name must be a bounded content-free name")
    if lowered in _TOOL_ALIASES:
        return _TOOL_ALIASES[lowered]
    if "terminal" in lowered or lowered.endswith(".exec"):
        return "terminal"
    if "browser" in lowered or lowered.startswith("web."):
        return "web"
    return "other"


@dataclass(frozen=True, slots=True)
class ToolObservation:
    source_execution_id: str
    execution_id: str
    raw_tool_name: str
    started_at_ms: int
    ended_at_ms: int | None = None
    parent_span_id: str | None = None
    terminal_run_id: str | None = None
    task_run_id: str | None = None
    status: str | None = None
    error_class: str | None = None
    retry_class: str | None = None
    retry_attempt: int | None = None
    timed_out: bool = False
    agent_kind: str | None = None
    agent_provider: str | None = None
    agent_model: str | None = None
    execution_phase: str | None = None
    evidence_ref: str | None = None
    execution_surface: ExecutionSurface = ExecutionSurface.HERMES
    workload_kind: WorkloadKind = WorkloadKind.INTERACTIVE
    trigger_kind: TriggerKind = TriggerKind.RECONCILIATION
    validity: Validity = Validity.EXACT

    def __post_init__(self) -> None:
        canonical_tool_name(self.raw_tool_name)
        if self.parent_span_id is None:
            raise ValueError("tool spans require an explicit parent_span_id")
        if self.started_at_ms < 0:
            raise ValueError("tool start must be non-negative")
        if self.ended_at_ms is not None and self.ended_at_ms < self.started_at_ms:
            raise ValueError("tool end must not precede start")
        if self.retry_attempt is not None and self.retry_attempt < 0:
            raise ValueError("tool retry attempt must be non-negative")


@dataclass(frozen=True, slots=True)
class TestObservation:
    source_execution_id: str
    execution_id: str
    started_at_ms: int
    ended_at_ms: int | None
    selected_tests: int | None = None
    executed_tests: int | None = None
    passed_tests: int | None = None
    failed_tests: int | None = None
    skipped_tests: int | None = None
    timeout_lost_tests: int | None = None
    timed_out: bool = False
    timeout_ms: int | None = None
    test_suite: str | None = None
    test_file: str | None = None
    gate_type: str | None = None
    flake_class: str | None = None
    test_command_hash: str | None = None
    parent_span_id: str | None = None
    terminal_run_id: str | None = None
    task_run_id: str | None = None
    status: str | None = None
    error_class: str | None = None
    retry_class: str | None = None
    retry_attempt: int | None = None
    exit_code: int | None = None
    agent_kind: str | None = None
    agent_provider: str | None = None
    agent_model: str | None = None
    execution_phase: str | None = None
    evidence_ref: str | None = None
    execution_surface: ExecutionSurface = ExecutionSurface.PROCESS
    workload_kind: WorkloadKind = WorkloadKind.KANBAN_TASK
    trigger_kind: TriggerKind = TriggerKind.RECONCILIATION
    validity: Validity = Validity.EXACT

    def __post_init__(self) -> None:
        if self.started_at_ms < 0:
            raise ValueError("test start must be non-negative")
        if self.parent_span_id is None:
            raise ValueError("test spans require an explicit parent_span_id")
        if self.ended_at_ms is not None and self.ended_at_ms < self.started_at_ms:
            raise ValueError("test end must not precede start")
        counts = (
            self.selected_tests,
            self.executed_tests,
            self.passed_tests,
            self.failed_tests,
            self.skipped_tests,
            self.timeout_lost_tests,
            self.timeout_ms,
            self.retry_attempt,
        )
        if any(value is not None and value < 0 for value in counts):
            raise ValueError("test counters must be non-negative")
        if (
            self.selected_tests is not None
            and self.executed_tests is not None
            and self.timeout_lost_tests is not None
            and self.executed_tests + self.timeout_lost_tests
            > self.selected_tests
        ):
            raise ValueError("executed plus timeout-lost exceeds selected tests")
        classified = sum(
            value or 0
            for value in (
                self.passed_tests,
                self.failed_tests,
                self.skipped_tests,
            )
        )
        if self.executed_tests is not None and classified > self.executed_tests:
            raise ValueError("classified results exceed executed tests")
        if (
            self.test_command_hash is not None
            and not _SAFE_HASH.fullmatch(self.test_command_hash)
        ):
            raise ValueError("test command hash must be a SHA-256 digest")
        success_statuses = {"green", "ok", "passed", "succeeded"}
        normalized_status = str(self.status or "").strip().lower()
        if normalized_status in success_statuses and (
            self.timed_out
            or self.exit_code != 0
            or int(self.failed_tests or 0) > 0
            or int(self.timeout_lost_tests or 0) > 0
        ):
            raise ValueError(
                "successful test status requires exit 0 and no failures "
                "or timeout loss"
            )


@dataclass(frozen=True, slots=True)
class ActivityObservation:
    source_execution_id: str
    execution_id: str
    span_key: str
    span_kind: SpanKind | str
    started_at_ms: int
    ended_at_ms: int | None = None
    parent_span_id: str | None = None
    terminal_run_id: str | None = None
    task_run_id: str | None = None
    agent_kind: str | None = None
    agent_provider: str | None = None
    agent_model: str | None = None
    operator_action_kind: str | None = None
    status: str | None = None
    error_class: str | None = None
    retry_class: str | None = None
    execution_surface: ExecutionSurface = ExecutionSurface.HERMES
    workload_kind: WorkloadKind = WorkloadKind.INTERACTIVE
    trigger_kind: TriggerKind = TriggerKind.RECONCILIATION
    validity: Validity = Validity.EXACT
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "span_kind", SpanKind(self.span_kind))
        if self.span_kind not in {
            SpanKind.AGENT,
            SpanKind.SUBAGENT,
            SpanKind.OPERATOR,
        }:
            raise ValueError("activity span must be agent, subagent, or operator")
        if self.span_kind is SpanKind.SUBAGENT and self.parent_span_id is None:
            raise ValueError("subagent spans require an explicit parent")
        if self.started_at_ms < 0:
            raise ValueError("activity start must be non-negative")
        if self.ended_at_ms is not None and self.ended_at_ms < self.started_at_ms:
            raise ValueError("activity end must not precede start")


@dataclass(frozen=True, slots=True)
class ShadowValidationObservation:
    """Explicit per-source evidence required before a shadow gate can pass."""

    validated_source: str
    observed_at_ms: int
    sample_size: int
    identity_coverage_passed: bool
    metric_coverage_passed: bool
    capture_p95_us: int
    dedupe_reconciled: bool
    behavior_equivalent: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.observed_at_ms < 0:
            raise ValueError("validation observation time must be non-negative")
        if self.sample_size < 1:
            raise ValueError("validation sample must be positive")
        if self.capture_p95_us < 0:
            raise ValueError("capture p95 must be non-negative")


def _span_events(
    *,
    source: str,
    source_execution_id: str,
    execution_id: str,
    span_id: str,
    span_kind: SpanKind,
    started_at_ms: int,
    ended_at_ms: int | None,
    parent_span_id: str | None,
    status: str | None,
    error_class: str | None,
    retry_class: str | None,
    exit_code: int | None,
    evidence_ref: str | None,
    terminal_run_id: str | None,
    task_run_id: str | None,
    execution_surface: ExecutionSurface,
    workload_kind: WorkloadKind,
    trigger_kind: TriggerKind,
    validity: Validity,
    attributes: dict[str, Any],
) -> list[ExecutionEvent]:
    recorded_at_ms = int(time.time() * 1000)
    common = {
        "source": source,
        "source_execution_id": source_execution_id,
        "execution_id": execution_id,
        "validity": validity,
        "workload_kind": workload_kind,
        "execution_surface": execution_surface,
        "trigger_kind": trigger_kind,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "span_kind": span_kind,
        "status": status,
        "error_class": error_class,
        "retry_class": retry_class,
        "exit_code": exit_code,
        "evidence_ref": evidence_ref,
        "terminal_run_id": terminal_run_id,
        "task_run_id": task_run_id,
        "attributes": attributes,
    }
    events = [
        make_event(
            **common,
            idempotency_key=stable_idempotency_key(
                source, source_execution_id, span_id, "started"
            ),
            event_type=EventType.SPAN_STARTED,
            observed_at_ms=started_at_ms,
            recorded_at_ms=recorded_at_ms,
        )
    ]
    if ended_at_ms is not None:
        events.append(
            make_event(
                **common,
                idempotency_key=stable_idempotency_key(
                    source, source_execution_id, span_id, "ended"
                ),
                event_type=EventType.SPAN_ENDED,
                observed_at_ms=ended_at_ms,
                recorded_at_ms=recorded_at_ms,
            )
        )
    return events


def tool_span_events(observation: ToolObservation) -> list[ExecutionEvent]:
    canonical = canonical_tool_name(observation.raw_tool_name)
    span_id = stable_span_id(
        "tool_runtime",
        (
            f"{observation.source_execution_id}:"
            f"{observation.raw_tool_name}:{observation.started_at_ms}"
        ),
    )
    attributes: dict[str, Any] = {
        "tool_name": canonical,
        "raw_tool_name": observation.raw_tool_name,
        "timeout": observation.timed_out,
    }
    for name in (
        "retry_attempt",
        "agent_kind",
        "agent_provider",
        "agent_model",
        "execution_phase",
    ):
        value = getattr(observation, name)
        if value is not None:
            attributes[name] = value
    if observation.ended_at_ms is not None:
        attributes["duration_ms"] = (
            observation.ended_at_ms - observation.started_at_ms
        )
    return _span_events(
        source="tool_runtime",
        source_execution_id=observation.source_execution_id,
        execution_id=observation.execution_id,
        span_id=span_id,
        span_kind=SpanKind.TOOL,
        started_at_ms=observation.started_at_ms,
        ended_at_ms=observation.ended_at_ms,
        parent_span_id=observation.parent_span_id,
        status=observation.status,
        error_class=observation.error_class,
        retry_class=observation.retry_class,
        exit_code=None,
        evidence_ref=observation.evidence_ref,
        terminal_run_id=observation.terminal_run_id,
        task_run_id=observation.task_run_id,
        execution_surface=observation.execution_surface,
        workload_kind=observation.workload_kind,
        trigger_kind=observation.trigger_kind,
        validity=observation.validity,
        attributes=attributes,
    )


def test_span_events(observation: TestObservation) -> list[ExecutionEvent]:
    span_id = stable_span_id(
        "test_runtime",
        (
            f"{observation.source_execution_id}:"
            f"{observation.test_suite or observation.test_file or 'test'}:"
            f"{observation.started_at_ms}"
        ),
    )
    attributes: dict[str, Any] = {"timeout": observation.timed_out}
    for name in (
        "selected_tests",
        "executed_tests",
        "passed_tests",
        "failed_tests",
        "skipped_tests",
        "timeout_lost_tests",
        "timeout_ms",
        "test_suite",
        "test_file",
        "gate_type",
        "flake_class",
        "test_command_hash",
        "retry_attempt",
        "agent_kind",
        "agent_provider",
        "agent_model",
        "execution_phase",
    ):
        value = getattr(observation, name)
        if value is not None:
            attributes[name] = value
    if observation.ended_at_ms is not None:
        attributes["duration_ms"] = (
            observation.ended_at_ms - observation.started_at_ms
        )
    return _span_events(
        source="test_runtime",
        source_execution_id=observation.source_execution_id,
        execution_id=observation.execution_id,
        span_id=span_id,
        span_kind=SpanKind.TEST,
        started_at_ms=observation.started_at_ms,
        ended_at_ms=observation.ended_at_ms,
        parent_span_id=observation.parent_span_id,
        status=observation.status,
        error_class=observation.error_class,
        retry_class=observation.retry_class,
        exit_code=observation.exit_code,
        evidence_ref=observation.evidence_ref,
        terminal_run_id=observation.terminal_run_id,
        task_run_id=observation.task_run_id,
        execution_surface=observation.execution_surface,
        workload_kind=observation.workload_kind,
        trigger_kind=observation.trigger_kind,
        validity=observation.validity,
        attributes=attributes,
    )


def activity_span_events(
    observation: ActivityObservation,
) -> list[ExecutionEvent]:
    span_id = stable_span_id(
        "activity_runtime",
        f"{observation.source_execution_id}:{observation.span_key}",
    )
    attributes: dict[str, Any] = {}
    for name in (
        "agent_kind",
        "agent_provider",
        "agent_model",
        "operator_action_kind",
    ):
        value = getattr(observation, name)
        if value is not None:
            attributes[name] = value
    if observation.ended_at_ms is not None:
        attributes["duration_ms"] = (
            observation.ended_at_ms - observation.started_at_ms
        )
    return _span_events(
        source="activity_runtime",
        source_execution_id=observation.source_execution_id,
        execution_id=observation.execution_id,
        span_id=span_id,
        span_kind=observation.span_kind,
        started_at_ms=observation.started_at_ms,
        ended_at_ms=observation.ended_at_ms,
        parent_span_id=observation.parent_span_id,
        status=observation.status,
        error_class=observation.error_class,
        retry_class=observation.retry_class,
        exit_code=None,
        evidence_ref=observation.evidence_ref,
        terminal_run_id=observation.terminal_run_id,
        task_run_id=observation.task_run_id,
        execution_surface=observation.execution_surface,
        workload_kind=observation.workload_kind,
        trigger_kind=observation.trigger_kind,
        validity=observation.validity,
        attributes=attributes,
    )


def shadow_validation_event(
    observation: ShadowValidationObservation,
) -> ExecutionEvent:
    """Create exact, versionable evidence without refreshing source data."""
    return make_event(
        source="execution_facts_validation",
        source_execution_id=(
            f"{observation.validated_source}:{observation.observed_at_ms}"
        ),
        idempotency_key=stable_idempotency_key(
            "execution_facts_validation",
            observation.validated_source,
            observation.observed_at_ms,
            observation.evidence_ref,
        ),
        event_type=EventType.SHADOW_VALIDATION,
        validity=Validity.EXACT,
        observed_at_ms=observation.observed_at_ms,
        workload_kind=WorkloadKind.SYSTEM_JOB,
        execution_surface=ExecutionSurface.PROCESS,
        trigger_kind=TriggerKind.RECONCILIATION,
        evidence_ref=observation.evidence_ref,
        attributes={
            "validated_source": observation.validated_source,
            "sample_size": observation.sample_size,
            "identity_coverage_passed": (
                observation.identity_coverage_passed
            ),
            "metric_coverage_passed": observation.metric_coverage_passed,
            "capture_p95_us": observation.capture_p95_us,
            "dedupe_reconciled": observation.dedupe_reconciled,
            "behavior_equivalent": observation.behavior_equivalent,
        },
    )


__all__ = (
    "ActivityObservation",
    "ShadowValidationObservation",
    "TestObservation",
    "ToolObservation",
    "activity_span_events",
    "canonical_tool_name",
    "shadow_validation_event",
    "test_span_events",
    "tool_span_events",
)

"""Versioned, content-free contract for universal execution facts.

The contract is deliberately independent from Kanban, usage-facts, tmux, cron,
and loop persistence.  Source adapters may only submit the identifiers,
milestones, bounded classifications, counters, hashes, and evidence references
declared here.  Prompts, commands, arguments, outputs, pane contents, and logs
have no representation in V1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
import time
from types import MappingProxyType
from typing import Any, Mapping
import uuid

CONTRACT_VERSION = "execution-facts.v1"

_EVENT_NAMESPACE = uuid.UUID("1539566f-ec47-40bb-9a6d-25749f6ad283")
_EXECUTION_NAMESPACE = uuid.UUID("743bfb34-e51c-4d62-b6b7-1d71dcc1b090")
_SPAN_NAMESPACE = uuid.UUID("04d48d4e-30bc-4ce5-8146-f04d5af5b406")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,254}$")
_SAFE_CLASSIFICATION = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:/@+?#=&%\[\](),-]{0,254}$"
)
_SAFE_REFERENCE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.+-]{0,63}"
    r"(?::[A-Za-z0-9][A-Za-z0-9_./@+-]{0,127}){1,5}$"
)
_SAFE_HASH = re.compile(r"^[a-f0-9]{7,128}$")


class _ContractEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Validity(_ContractEnum):
    EXACT = "exact"
    LOWER_BOUND = "lower_bound"
    DERIVED = "derived"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class WorkloadKind(_ContractEnum):
    KANBAN_TASK = "kanban_task"
    CRON_JOB = "cron_job"
    LOOP = "loop"
    INTERACTIVE = "interactive"
    SYSTEM_JOB = "system_job"
    UNCLASSIFIED = "unclassified"


class ExecutionSurface(_ContractEnum):
    HERMES = "hermes"
    CLAUDE_CLI = "claude_cli"
    CODEX_CLI = "codex_cli"
    GROK_CLI = "grok_cli"
    KIMI_CLI = "kimi_cli"
    QWEN_CLI = "qwen_cli"
    TMUX = "tmux"
    LOOP = "loop"
    PROCESS = "process"
    SYSTEMD = "systemd"
    CRONTAB = "crontab"
    UNKNOWN = "unknown"


class TriggerKind(_ContractEnum):
    OPERATOR = "operator"
    KANBAN = "kanban"
    HERMES_CRON = "hermes_cron"
    SYSTEMD = "systemd"
    CRONTAB = "crontab"
    LOOP_PHASE = "loop_phase"
    RETRY = "retry"
    SENTINEL = "sentinel"
    RECONCILIATION = "reconciliation"
    UNKNOWN = "unknown"


class SpanKind(_ContractEnum):
    AGENT = "agent"
    SUBAGENT = "subagent"
    TOOL = "tool"
    TEST = "test"
    TASK_BINDING = "task_binding"
    OPERATOR = "operator"
    UNCLASSIFIED = "unclassified"


class LifecyclePhase(_ContractEnum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    CLAIMED = "claimed"
    SPAWN_STARTED = "spawn_started"
    PROCESS_STARTED = "process_started"
    FIRST_REQUEST = "first_request"
    FIRST_TOKEN = "first_token"
    RUNNING = "running"
    ENDED = "ended"
    REVIEW_STARTED = "review_started"
    REVIEWED = "reviewed"
    INTEGRATION_STARTED = "integration_started"
    LANDED = "landed"
    DEPLOYED = "deployed"


class EventType(_ContractEnum):
    LIFECYCLE = "lifecycle"
    TERMINAL_OBSERVED = "terminal_observed"
    TERMINAL_GENERATION_ENDED = "terminal_generation_ended"
    TERMINAL_CLOSED = "terminal_closed"
    SPAN_STARTED = "span_started"
    SPAN_ENDED = "span_ended"
    TASK_BINDING_STARTED = "task_binding_started"
    TASK_BINDING_ENDED = "task_binding_ended"
    USAGE_OBSERVED = "usage_observed"
    COST_OBSERVED = "cost_observed"
    OUTCOME_OBSERVED = "outcome_observed"
    COLLECTOR_HEALTH = "collector_health"
    SOURCE_CENSUS = "source_census"
    SHADOW_VALIDATION = "shadow_validation"


# Only these bounded, content-free values may travel in V1's attributes object.
ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "accepted",
        "agent_kind",
        "agent_model",
        "agent_provider",
        "allocated_subscription_cost",
        "api_equivalent_cost",
        "archived",
        "accepted_events",
        "allocation_formula",
        "allocation_month",
        "billing_mode",
        "behavior_equivalent",
        "behavior_proof",
        "cache_read_tokens",
        "cache_write_tokens",
        "capture_p95_us",
        "closed_reason",
        "currency",
        "deduped",
        "deployed_sha",
        "dropped",
        "duration_ms",
        "eligible",
        "execution_phase",
        "executed_tests",
        "failed_tests",
        "fee_version",
        "fee_amount",
        "finish_reason",
        "flake_class",
        "fx_version",
        "gate_type",
        "input_tokens",
        "identity_coverage_passed",
        "identity_observed",
        "eligibility_rule",
        "landed_sha",
        "marginal_cost",
        "metric_observed",
        "metric_coverage_passed",
        "metered_cost",
        "missing_links",
        "observed",
        "operator_action_kind",
        "orphans",
        "output_tokens",
        "passed_tests",
        "population_size",
        "pricing_version",
        "queue_capacity",
        "queue_depth",
        "queue_lag_ms",
        "raw_tool_name",
        "reasoning_effort",
        "reasoning_tokens",
        "reconciliation_lag_ms",
        "retry_attempt",
        "retry_instrumented",
        "dedupe_reconciled",
        "review_required",
        "sample_size",
        "selected_tests",
        "serving_tier",
        "skipped_tests",
        "source_freshness_ms",
        "source_contract_version",
        "source_read_errors",
        "source_store_count",
        "submitted_events",
        "terminal_status",
        "test_file",
        "test_command_hash",
        "test_suite",
        "timeout",
        "timeout_ms",
        "timeout_lost_tests",
        "tool_name",
        "total_tokens",
        "verdict",
        "validated_source",
        "window_end_ms",
        "window_start_ms",
        "writer_p50_us",
        "writer_p95_us",
        "write_errors",
        "written_events",
    }
)
_INTEGER_ATTRIBUTE_KEYS = frozenset(
    {
        "cache_read_tokens",
        "cache_write_tokens",
        "capture_p95_us",
        "accepted_events",
        "deduped",
        "dropped",
        "duration_ms",
        "eligible",
        "executed_tests",
        "failed_tests",
        "input_tokens",
        "identity_observed",
        "metric_observed",
        "missing_links",
        "observed",
        "orphans",
        "output_tokens",
        "passed_tests",
        "population_size",
        "queue_capacity",
        "queue_depth",
        "queue_lag_ms",
        "reasoning_tokens",
        "reconciliation_lag_ms",
        "retry_attempt",
        "sample_size",
        "selected_tests",
        "skipped_tests",
        "source_freshness_ms",
        "source_read_errors",
        "source_store_count",
        "submitted_events",
        "timeout_lost_tests",
        "timeout_ms",
        "total_tokens",
        "window_end_ms",
        "window_start_ms",
        "writer_p50_us",
        "writer_p95_us",
        "write_errors",
        "written_events",
    }
)
_BOOLEAN_ATTRIBUTE_KEYS = frozenset(
    {
        "accepted",
        "archived",
        "behavior_equivalent",
        "dedupe_reconciled",
        "identity_coverage_passed",
        "metric_coverage_passed",
        "retry_instrumented",
        "review_required",
        "timeout",
    }
)
_DECIMAL_STRING_ATTRIBUTE_KEYS = frozenset(
    {
        "allocated_subscription_cost",
        "api_equivalent_cost",
        "fee_amount",
        "marginal_cost",
        "metered_cost",
    }
)


def _enum_value(value: _ContractEnum | str, enum_type: type[_ContractEnum]) -> str:
    try:
        return enum_type(value).value
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{value!r} is not one of: {allowed}") from exc


def _safe_name(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not _SAFE_NAME.fullmatch(text):
        raise ValueError(f"{field_name} must be a bounded content-free name")
    return text


def _safe_reference(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not _SAFE_REFERENCE.fullmatch(text):
        raise ValueError(
            f"{field_name} must be a bounded content-free reference"
        )
    return text


def _safe_hash(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).lower()
    if not _SAFE_HASH.fullmatch(text):
        raise ValueError(f"{field_name} must be a hexadecimal digest")
    return text


def _normalize_attributes(attributes: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized: dict[str, Any] = {}
    unknown = set(attributes).difference(ALLOWED_ATTRIBUTE_KEYS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"attributes are outside the content-free V1 allowlist: {names}")
    for key, value in attributes.items():
        if value is None:
            normalized[key] = None
            continue
        if key in _INTEGER_ATTRIBUTE_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"attribute {key} must be a non-negative integer")
            normalized[key] = value
            continue
        if key in _BOOLEAN_ATTRIBUTE_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"attribute {key} must be boolean")
            normalized[key] = value
            continue
        if key in _DECIMAL_STRING_ATTRIBUTE_KEYS:
            text = str(value)
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
                raise ValueError(
                    f"attribute {key} must be a non-negative base-10 decimal string"
                )
            normalized[key] = text
            continue
        text = str(value)
        if not _SAFE_CLASSIFICATION.fullmatch(text):
            raise ValueError(
                f"attribute {key} must be a bounded content-free value"
            )
        normalized[key] = text
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One immutable raw fact accepted by the append-only ledger."""

    event_id: str
    idempotency_key: str
    source: str
    source_execution_id: str
    execution_id: str
    event_type: EventType | str
    validity: Validity | str
    observed_at_ms: int
    recorded_at_ms: int
    workload_kind: WorkloadKind | str
    execution_surface: ExecutionSurface | str
    trigger_kind: TriggerKind | str
    lifecycle_phase: LifecyclePhase | str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    span_kind: SpanKind | str | None = None
    terminal_run_id: str | None = None
    terminal_generation: int | None = None
    task_run_id: str | None = None
    task_id: str | None = None
    chain_id: str | None = None
    cron_execution_id: str | None = None
    loop_run_id: str | None = None
    loop_phase: str | None = None
    loop_round: int | None = None
    profile: str | None = None
    board: str | None = None
    status: str | None = None
    end_reason: str | None = None
    error_class: str | None = None
    retry_class: str | None = None
    exit_code: int | None = None
    evidence_ref: str | None = None
    evidence_sha256: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported execution-facts contract: {self.contract_version}")
        for name in (
            "event_id",
            "idempotency_key",
            "source",
            "source_execution_id",
            "execution_id",
        ):
            _safe_name(str(getattr(self, name)), field_name=name)
        if len(self.idempotency_key) < 16:
            raise ValueError("idempotency_key must carry collision-resistant entropy")
        object.__setattr__(self, "event_type", EventType(self.event_type))
        object.__setattr__(self, "validity", Validity(self.validity))
        object.__setattr__(self, "workload_kind", WorkloadKind(self.workload_kind))
        object.__setattr__(
            self, "execution_surface", ExecutionSurface(self.execution_surface)
        )
        object.__setattr__(self, "trigger_kind", TriggerKind(self.trigger_kind))
        if self.lifecycle_phase is not None:
            object.__setattr__(
                self, "lifecycle_phase", LifecyclePhase(self.lifecycle_phase)
            )
        if self.span_kind is not None:
            object.__setattr__(self, "span_kind", SpanKind(self.span_kind))
        if (
            self.event_type is EventType.LIFECYCLE
            and self.lifecycle_phase is None
        ):
            raise ValueError("lifecycle events require a lifecycle_phase")
        if self.event_type in {
            EventType.SPAN_STARTED,
            EventType.SPAN_ENDED,
        } and (self.span_id is None or self.span_kind is None):
            raise ValueError(
                "span events require an explicit span_id and span_kind"
            )
        if self.event_type in {
            EventType.TASK_BINDING_STARTED,
            EventType.TASK_BINDING_ENDED,
        } and (
            self.span_id is None
            or self.span_kind is not SpanKind.TASK_BINDING
            or self.terminal_run_id is None
            or self.task_run_id is None
        ):
            raise ValueError(
                "task binding events require binding span, terminal, and task identities"
            )
        if self.event_type in {
            EventType.TERMINAL_OBSERVED,
            EventType.TERMINAL_GENERATION_ENDED,
        } and (
            self.terminal_run_id is None
            or self.terminal_generation is None
        ):
            raise ValueError(
                "terminal generation events require run and generation identities"
            )
        if (
            self.event_type is EventType.TERMINAL_CLOSED
            and self.terminal_run_id is None
        ):
            raise ValueError("terminal close events require a terminal_run_id")
        if self.parent_span_id is not None and self.span_id is None:
            raise ValueError("parent_span_id requires an explicit span_id")
        if self.span_kind is not None and self.span_id is None:
            raise ValueError("span_kind requires an explicit span_id")
        if (
            self.span_kind
            in {SpanKind.SUBAGENT, SpanKind.TOOL, SpanKind.TEST}
            and self.parent_span_id is None
        ):
            raise ValueError(
                f"{self.span_kind.value} spans require an explicit parent_span_id"
            )
        if self.terminal_generation is not None and self.terminal_generation < 1:
            raise ValueError("terminal_generation must be positive")
        if self.loop_round is not None and self.loop_round < 0:
            raise ValueError("loop_round must be non-negative")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code must be an integer or None")
        for name in ("observed_at_ms", "recorded_at_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "span_id",
            "parent_span_id",
            "terminal_run_id",
            "task_run_id",
            "task_id",
            "chain_id",
            "cron_execution_id",
            "loop_run_id",
            "loop_phase",
            "profile",
            "board",
            "status",
            "end_reason",
            "error_class",
            "retry_class",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _safe_name(str(value), field_name=name)
                )
        object.__setattr__(
            self,
            "evidence_ref",
            _safe_reference(self.evidence_ref, field_name="evidence_ref"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _safe_hash(self.evidence_sha256, field_name="evidence_sha256"),
        )
        object.__setattr__(self, "attributes", _normalize_attributes(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        result = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "attributes"
        }
        for name in (
            "event_type",
            "validity",
            "workload_kind",
            "execution_surface",
            "trigger_kind",
            "lifecycle_phase",
            "span_kind",
        ):
            value = result[name]
            result[name] = value.value if isinstance(value, Enum) else value
        result["attributes"] = dict(self.attributes)
        return result

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionEvent":
        return cls(**dict(value))


def _prefixed_uuid(prefix: str, value: uuid.UUID) -> str:
    return f"{prefix}_{value.hex}"


def new_execution_id() -> str:
    return _prefixed_uuid("ex", uuid.uuid4())


def stable_execution_id(source: str, source_execution_id: str) -> str:
    return _prefixed_uuid(
        "ex", uuid.uuid5(_EXECUTION_NAMESPACE, f"{source}\0{source_execution_id}")
    )


def new_span_id() -> str:
    return _prefixed_uuid("sp", uuid.uuid4())


def stable_span_id(source: str, source_span_id: str) -> str:
    return _prefixed_uuid(
        "sp", uuid.uuid5(_SPAN_NAMESPACE, f"{source}\0{source_span_id}")
    )


def stable_idempotency_key(source: str, *parts: object) -> str:
    digest = hashlib.sha256()
    for value in (source, *parts):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def make_event(
    *,
    source: str,
    source_execution_id: str,
    event_type: EventType | str,
    validity: Validity | str,
    observed_at_ms: int,
    workload_kind: WorkloadKind | str,
    execution_surface: ExecutionSurface | str,
    trigger_kind: TriggerKind | str,
    idempotency_key: str | None = None,
    execution_id: str | None = None,
    recorded_at_ms: int | None = None,
    **fields: Any,
) -> ExecutionEvent:
    key = idempotency_key or stable_idempotency_key(
        source,
        source_execution_id,
        event_type,
        fields.get("lifecycle_phase"),
        fields.get("span_id"),
        observed_at_ms,
    )
    stable_event_uuid = uuid.uuid5(_EVENT_NAMESPACE, f"{source}\0{key}")
    return ExecutionEvent(
        event_id=_prefixed_uuid("ev", stable_event_uuid),
        idempotency_key=key,
        source=source,
        source_execution_id=source_execution_id,
        execution_id=execution_id
        or stable_execution_id(source, source_execution_id),
        event_type=event_type,
        validity=validity,
        observed_at_ms=int(observed_at_ms),
        recorded_at_ms=(
            int(time.time() * 1000)
            if recorded_at_ms is None
            else int(recorded_at_ms)
        ),
        workload_kind=workload_kind,
        execution_surface=execution_surface,
        trigger_kind=trigger_kind,
        **fields,
    )


def contract_schema() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "validity": [item.value for item in Validity],
        "workload_kind": [item.value for item in WorkloadKind],
        "execution_surface": [item.value for item in ExecutionSurface],
        "trigger_kind": [item.value for item in TriggerKind],
        "span_kind": [item.value for item in SpanKind],
        "lifecycle_phase": [item.value for item in LifecyclePhase],
        "event_type": [item.value for item in EventType],
        "content_free_attribute_keys": sorted(ALLOWED_ATTRIBUTE_KEYS),
        "forbidden_content": [
            "prompts",
            "commands",
            "arguments",
            "outputs",
            "pane_contents",
            "test_logs",
        ],
    }


__all__ = (
    "ALLOWED_ATTRIBUTE_KEYS",
    "CONTRACT_VERSION",
    "EventType",
    "ExecutionEvent",
    "ExecutionSurface",
    "LifecyclePhase",
    "SpanKind",
    "TriggerKind",
    "Validity",
    "WorkloadKind",
    "contract_schema",
    "make_event",
    "new_execution_id",
    "new_span_id",
    "stable_execution_id",
    "stable_idempotency_key",
    "stable_span_id",
)

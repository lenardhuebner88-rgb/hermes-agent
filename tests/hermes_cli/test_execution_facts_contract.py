"""Contract tests for content-free universal execution facts."""

from __future__ import annotations

import pytest

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionSurface,
    LifecyclePhase,
    SpanKind,
    TriggerKind,
    Validity,
    WorkloadKind,
    contract_schema,
    make_event,
    stable_span_id,
)


def _event(**fields):
    defaults = {
        "source": "test_source",
        "source_execution_id": "run_1",
        "event_type": EventType.LIFECYCLE,
        "validity": Validity.EXACT,
        "observed_at_ms": 1_000,
        "recorded_at_ms": 1_001,
        "workload_kind": WorkloadKind.KANBAN_TASK,
        "execution_surface": ExecutionSurface.HERMES,
        "trigger_kind": TriggerKind.KANBAN,
    }
    defaults.update(fields)
    if (
        defaults["event_type"] is EventType.LIFECYCLE
        and "lifecycle_phase" not in fields
    ):
        defaults["lifecycle_phase"] = LifecyclePhase.RUNNING
    return make_event(**defaults)


def test_contract_round_trip_preserves_explicit_unknown_not_zero() -> None:
    event = _event(
        validity=Validity.UNKNOWN,
        attributes={
            "input_tokens": None,
            "api_equivalent_cost": None,
            "population_size": 0,
        },
    )

    payload = event.to_dict()
    restored = type(event).from_dict(payload)

    assert restored.validity is Validity.UNKNOWN
    assert restored.attributes["input_tokens"] is None
    assert restored.attributes["api_equivalent_cost"] is None
    assert restored.attributes["population_size"] == 0


@pytest.mark.parametrize(
    "attributes",
    (
        {"prompt": "secret"},
        {"command": "rm"},
        {"pane_content": "secret"},
        {"finish_reason": "copied prompt content"},
        {"metered_cost": "-0.01"},
        {"input_tokens": -1},
    ),
)
def test_contract_rejects_content_or_invalid_measurements(attributes) -> None:
    with pytest.raises(ValueError):
        _event(attributes=attributes)


@pytest.mark.parametrize("exit_code", ("17", True, [], {"content": "leak"}))
def test_contract_rejects_non_integer_exit_codes(exit_code) -> None:
    with pytest.raises(ValueError, match="exit_code"):
        _event(exit_code=exit_code)


def test_contract_accepts_versioned_cost_and_tool_test_fields() -> None:
    event = _event(
        event_type=EventType.COST_OBSERVED,
        attributes={
            "api_equivalent_cost": "0.2116692",
            "allocated_subscription_cost": "1.25",
            "marginal_cost": "0",
            "currency": "USD",
            "pricing_version": "models-2026-07-30",
            "fee_version": "subscriptions-2026-07",
            "tool_name": "terminal",
            "raw_tool_name": "functions.exec_command",
            "selected_tests": 307,
            "executed_tests": 0,
            "timeout_lost_tests": 307,
            "timeout_ms": 300000,
        },
    )

    assert event.attributes["api_equivalent_cost"] == "0.2116692"
    assert event.attributes["marginal_cost"] == "0"
    assert event.attributes["timeout_lost_tests"] == 307


def test_schema_names_every_validity_and_forbidden_content_class() -> None:
    schema = contract_schema()

    assert schema["validity"] == [
        "exact",
        "lower_bound",
        "derived",
        "unknown",
        "not_applicable",
    ]
    assert "commands" in schema["forbidden_content"]
    assert "raw_tool_name" in schema["content_free_attribute_keys"]


@pytest.mark.parametrize(
    "span_kind",
    (SpanKind.SUBAGENT, SpanKind.TOOL, SpanKind.TEST),
)
def test_child_span_kinds_require_an_explicit_parent(span_kind) -> None:
    with pytest.raises(ValueError, match="parent_span_id"):
        _event(
            event_type=EventType.SPAN_STARTED,
            span_id=stable_span_id("test_source", span_kind.value),
            span_kind=span_kind,
        )


@pytest.mark.parametrize(
    ("event_type", "fields"),
    (
        (EventType.LIFECYCLE, {"lifecycle_phase": None}),
        (EventType.SPAN_STARTED, {}),
        (EventType.SPAN_ENDED, {}),
        (
            EventType.TASK_BINDING_STARTED,
            {
                "span_id": "sp_binding",
                "span_kind": SpanKind.TASK_BINDING,
            },
        ),
        (EventType.TERMINAL_OBSERVED, {}),
        (EventType.TERMINAL_GENERATION_ENDED, {}),
        (EventType.TERMINAL_CLOSED, {}),
    ),
)
def test_structural_event_types_require_projection_identities(
    event_type,
    fields,
) -> None:
    with pytest.raises(ValueError, match="require"):
        _event(event_type=event_type, **fields)


@pytest.mark.parametrize(
    "evidence_ref",
    (
        "sk-proj-secret",
        "https://example.invalid/proof?token=secret",
        "shadow:source:proof%3Dsecret",
    ),
)
def test_evidence_reference_rejects_secret_shaped_free_text(
    evidence_ref,
) -> None:
    with pytest.raises(ValueError, match="content-free reference"):
        _event(evidence_ref=evidence_ref)

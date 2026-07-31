"""Tool normalization and timeout-aware test-span contracts."""

from __future__ import annotations

import pytest

from hermes_cli.execution_facts_contract import stable_execution_id
from hermes_cli.execution_facts_instrumentation import (
    ActivityObservation,
    ShadowValidationObservation,
    TestObservation as ExecutionTestObservation,
    ToolObservation,
    activity_span_events,
    canonical_tool_name,
    shadow_validation_event,
    test_span_events as build_test_span_events,
    tool_span_events,
)


def test_tool_span_keeps_canonical_and_raw_adapter_names() -> None:
    execution_id = stable_execution_id("kanban_timeline", "task_run:1")

    events = tool_span_events(
        ToolObservation(
            source_execution_id="task_run:1",
            execution_id=execution_id,
            raw_tool_name="functions.exec_command",
            started_at_ms=1_000,
            ended_at_ms=1_250,
            parent_span_id="sp_parent",
            status="ok",
        )
    )

    assert [event.event_type.value for event in events] == [
        "span_started",
        "span_ended",
    ]
    assert events[-1].attributes == {
        "tool_name": "terminal",
            "raw_tool_name": "functions.exec_command",
            "timeout": False,
            "duration_ms": 250,
    }
    assert events[0].span_id == events[1].span_id
    assert canonical_tool_name("web.run") == "web"


def test_timeout_preserves_selected_executed_and_lost_denominators() -> None:
    execution_id = stable_execution_id("kanban_timeline", "task_run:2")

    events = build_test_span_events(
        ExecutionTestObservation(
            source_execution_id="task_run:2",
            execution_id=execution_id,
            started_at_ms=2_000,
            ended_at_ms=302_000,
            selected_tests=307,
            executed_tests=0,
            passed_tests=0,
            failed_tests=0,
            skipped_tests=0,
            timeout_lost_tests=307,
            timed_out=True,
            timeout_ms=300_000,
            parent_span_id="sp_parent",
            test_suite="kanban_dashboard",
            gate_type="targeted",
            test_command_hash="a" * 64,
            status="timed_out",
        )
    )

    attributes = events[-1].attributes
    assert attributes["selected_tests"] == 307
    assert attributes["executed_tests"] == 0
    assert attributes["timeout_lost_tests"] == 307
    assert attributes["timeout"] is True
    assert attributes["duration_ms"] == 300_000


def test_invalid_test_counter_contradictions_raise() -> None:
    with pytest.raises(ValueError, match="classified"):
        ExecutionTestObservation(
            source_execution_id="run:1",
            execution_id=stable_execution_id("test", "run:1"),
            started_at_ms=0,
            ended_at_ms=1,
            parent_span_id="sp_parent",
            selected_tests=1,
            executed_tests=1,
            passed_tests=2,
        )
    with pytest.raises(ValueError, match="timeout-lost"):
        ExecutionTestObservation(
            source_execution_id="run:2",
            execution_id=stable_execution_id("test", "run:2"),
            started_at_ms=0,
            ended_at_ms=1,
            parent_span_id="sp_parent",
            selected_tests=1,
            executed_tests=1,
            timeout_lost_tests=1,
        )
    with pytest.raises(ValueError, match="successful test status"):
        ExecutionTestObservation(
            source_execution_id="run:3",
            execution_id=stable_execution_id("test", "run:3"),
            started_at_ms=0,
            ended_at_ms=1,
            parent_span_id="sp_parent",
            selected_tests=1,
            executed_tests=0,
            timeout_lost_tests=1,
            timed_out=True,
            status="passed",
            exit_code=0,
        )
    with pytest.raises(ValueError, match="successful test status"):
        ExecutionTestObservation(
            source_execution_id="run:4",
            execution_id=stable_execution_id("test", "run:4"),
            started_at_ms=0,
            ended_at_ms=1,
            parent_span_id="sp_parent",
            selected_tests=1,
            executed_tests=1,
            passed_tests=1,
            status="passed",
            exit_code=None,
        )


def test_agent_subagent_and_operator_spans_keep_explicit_parentage() -> None:
    execution_id = stable_execution_id("kanban_timeline", "task_run:3")
    agent = activity_span_events(
        ActivityObservation(
            source_execution_id="task_run:3",
            execution_id=execution_id,
            span_key="root-agent",
            span_kind="agent",
            started_at_ms=1_000,
            ended_at_ms=2_000,
            terminal_run_id="terminal_root",
            task_run_id="3",
            agent_kind="codex",
        )
    )
    subagent = activity_span_events(
        ActivityObservation(
            source_execution_id="task_run:3",
            execution_id=execution_id,
            span_key="subagent-review",
            span_kind="subagent",
            parent_span_id=agent[0].span_id,
            started_at_ms=1_100,
            ended_at_ms=1_900,
            terminal_run_id="terminal_child",
            task_run_id="3",
            agent_kind="reviewer",
        )
    )
    operator = activity_span_events(
        ActivityObservation(
            source_execution_id="task_run:3",
            execution_id=execution_id,
            span_key="operator-approval",
            span_kind="operator",
            parent_span_id=agent[0].span_id,
            started_at_ms=1_500,
            ended_at_ms=1_501,
            operator_action_kind="approval",
        )
    )

    assert subagent[0].parent_span_id == agent[0].span_id
    assert subagent[0].terminal_run_id == "terminal_child"
    assert operator[0].span_kind.value == "operator"
    assert operator[0].attributes["operator_action_kind"] == "approval"


def test_shadow_validation_is_explicit_content_free_source_evidence() -> None:
    event = shadow_validation_event(
        ShadowValidationObservation(
            validated_source="kanban_timeline",
            observed_at_ms=1_000,
            sample_size=20,
            identity_coverage_passed=True,
            metric_coverage_passed=True,
            capture_p95_us=420,
            dedupe_reconciled=True,
            behavior_equivalent=True,
            evidence_ref="gate:kanban:v1",
        )
    )

    assert event.source == "execution_facts_validation"
    assert event.event_type.value == "shadow_validation"
    assert event.attributes["validated_source"] == "kanban_timeline"
    assert event.attributes["capture_p95_us"] == 420

    with pytest.raises(ValueError, match="sample"):
        ShadowValidationObservation(
            validated_source="kanban_timeline",
            observed_at_ms=1_000,
            sample_size=0,
            identity_coverage_passed=True,
            metric_coverage_passed=True,
            capture_p95_us=420,
            dedupe_reconciled=True,
            behavior_equivalent=True,
            evidence_ref="gate:kanban:v1",
        )

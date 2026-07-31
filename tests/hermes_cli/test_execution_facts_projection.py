"""Replay determinism and projection identity invariants."""

from __future__ import annotations

import json
import sqlite3

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionSurface,
    LifecyclePhase,
    SpanKind,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_idempotency_key,
    stable_span_id,
)
from hermes_cli.execution_facts_ledger import ExecutionFactsLedger
from hermes_cli.execution_facts_projection import rebuild_projections


def _event(tag: str, **fields):
    defaults = {
        "source": "fixture",
        "source_execution_id": "run:1",
        "idempotency_key": stable_idempotency_key("fixture", tag),
        "event_type": EventType.LIFECYCLE,
        "validity": Validity.EXACT,
        "observed_at_ms": 1_000,
        "recorded_at_ms": 1_000,
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


def test_rebuild_is_deterministic_and_exact_phase_beats_derived(tmp_path) -> None:
    path = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(path)
    ledger.initialize()
    ledger.append_batch(
        (
            _event(
                "derived-start",
                validity=Validity.DERIVED,
                observed_at_ms=900,
                lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
            ),
            _event(
                "exact-start",
                validity=Validity.EXACT,
                observed_at_ms=1_000,
                lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
            ),
            _event(
                "ended",
                observed_at_ms=2_000,
                lifecycle_phase=LifecyclePhase.ENDED,
                status="done",
            ),
            _event(
                "exact-attribute",
                observed_at_ms=2_100,
                attributes={"total_tokens": 10},
            ),
            _event(
                "derived-attribute",
                validity=Validity.DERIVED,
                observed_at_ms=2_200,
                attributes={"total_tokens": 1},
            ),
        )
    )

    first = rebuild_projections(path)
    second = rebuild_projections(path)

    with sqlite3.connect(path) as connection:
        projection = connection.execute(
            """
            SELECT lifecycle_json, attributes_json, attribute_validity_json
              FROM execution_projection
            """
        ).fetchone()
        lifecycle = json.loads(projection[0])
        attributes = json.loads(projection[1])
        attribute_validity = json.loads(projection[2])
    assert first.digest == second.digest
    assert first.executions == 1
    assert lifecycle["process_started"]["validity"] == "exact"
    assert lifecycle["process_started"]["observed_at_ms"] == 1_000
    assert attributes["total_tokens"] == 10
    assert attribute_validity["total_tokens"] == "exact"


def test_same_source_timestamp_uses_later_recorded_revision(tmp_path) -> None:
    path = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(path)
    ledger.initialize()
    older = _event(
        "mutable-older",
        observed_at_ms=1_000,
        recorded_at_ms=1_000,
        attributes={"total_tokens": 10},
    )
    newer = next(
        candidate
        for ordinal in range(100)
        if (
            candidate := _event(
                f"mutable-newer-{ordinal}",
                observed_at_ms=1_000,
                recorded_at_ms=2_000,
                attributes={"total_tokens": 20},
            )
        ).event_id
        < older.event_id
    )
    ledger.append_batch((newer, older))

    rebuild_projections(path)

    with sqlite3.connect(path) as connection:
        attributes = json.loads(
            connection.execute(
                "SELECT attributes_json FROM execution_projection"
            ).fetchone()[0]
        )
    assert attributes["total_tokens"] == 20


def test_projection_keeps_explicit_spans_generations_and_many_bindings(tmp_path) -> None:
    path = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(path)
    ledger.initialize()
    terminal_run_id = "terminal_1"
    binding_a = stable_span_id("fixture", "binding-a")
    binding_b = stable_span_id("fixture", "binding-b")
    ledger.append_batch(
        (
            _event(
                "terminal",
                source_execution_id="terminal:1",
                event_type=EventType.TERMINAL_OBSERVED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                workload_kind=WorkloadKind.UNCLASSIFIED,
                terminal_run_id=terminal_run_id,
                terminal_generation=1,
                lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
                validity=Validity.LOWER_BOUND,
            ),
            _event(
                "binding-a",
                source_execution_id="terminal:1",
                event_type=EventType.TASK_BINDING_STARTED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                terminal_run_id=terminal_run_id,
                terminal_generation=1,
                span_id=binding_a,
                span_kind=SpanKind.TASK_BINDING,
                task_run_id="run_a",
            ),
            _event(
                "binding-b",
                source_execution_id="terminal:1",
                event_type=EventType.TASK_BINDING_STARTED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                terminal_run_id=terminal_run_id,
                terminal_generation=1,
                span_id=binding_b,
                span_kind=SpanKind.TASK_BINDING,
                task_run_id="run_b",
            ),
            _event(
                "generation-end",
                source_execution_id="terminal:1",
                event_type=EventType.TERMINAL_GENERATION_ENDED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                terminal_run_id=terminal_run_id,
                terminal_generation=1,
                observed_at_ms=2_000,
                lifecycle_phase=LifecyclePhase.ENDED,
            ),
        )
    )

    result = rebuild_projections(path)

    with sqlite3.connect(path) as connection:
        bindings = connection.execute(
            """
            SELECT task_run_id
              FROM task_binding_projection
             ORDER BY task_run_id
            """
        ).fetchall()
        generation = connection.execute(
            """
            SELECT ended_at_ms
              FROM terminal_generation_projection
             WHERE terminal_run_id = ? AND generation = 1
            """,
            (terminal_run_id,),
        ).fetchone()
    assert result.task_bindings == 2
    assert bindings == [("run_a",), ("run_b",)]
    assert generation == (2_000,)


def test_composite_span_and_generation_keep_their_weakest_validity(
    tmp_path,
) -> None:
    path = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(path)
    ledger.initialize()
    span_id = stable_span_id("fixture", "mixed-validity-span")
    ledger.append_batch(
        (
            _event(
                "span-start",
                event_type=EventType.SPAN_STARTED,
                span_id=span_id,
                span_kind=SpanKind.AGENT,
            ),
            _event(
                "span-end-derived",
                event_type=EventType.SPAN_ENDED,
                span_id=span_id,
                span_kind=SpanKind.AGENT,
                observed_at_ms=2_000,
                validity=Validity.DERIVED,
            ),
            _event(
                "terminal-lower-bound",
                source_execution_id="terminal:mixed",
                event_type=EventType.TERMINAL_OBSERVED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                workload_kind=WorkloadKind.UNCLASSIFIED,
                terminal_run_id="terminal_mixed",
                terminal_generation=1,
                validity=Validity.LOWER_BOUND,
            ),
            _event(
                "terminal-end-exact",
                source_execution_id="terminal:mixed",
                event_type=EventType.TERMINAL_GENERATION_ENDED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                workload_kind=WorkloadKind.UNCLASSIFIED,
                terminal_run_id="terminal_mixed",
                terminal_generation=1,
                observed_at_ms=2_000,
            ),
        )
    )

    rebuild_projections(path)

    with sqlite3.connect(path) as connection:
        span_validity = connection.execute(
            "SELECT validity FROM execution_span_projection WHERE span_id = ?",
            (span_id,),
        ).fetchone()[0]
        generation_validity = connection.execute(
            """
            SELECT validity
              FROM terminal_generation_projection
             WHERE terminal_run_id = 'terminal_mixed' AND generation = 1
            """
        ).fetchone()[0]
    assert span_validity == "derived"
    assert generation_validity == "lower_bound"

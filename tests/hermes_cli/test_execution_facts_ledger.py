"""Ledger, retention, and fail-open emitter invariants."""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
import statistics
import threading
import time

import pytest

import hermes_cli.execution_facts_ledger as ledger_module
from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionSurface,
    LifecyclePhase,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_idempotency_key,
)
from hermes_cli.execution_facts_ledger import (
    AppendResult,
    EmitterMode,
    ExecutionFactsLedger,
    IdempotencyConflictError,
    NonBlockingEmitter,
    emitter_mode_from_environment,
    emitter_snapshot_event,
    publish_after_commit,
)


def _event(
    name: str,
    *,
    observed_at_ms: int = 1_000,
    recorded_at_ms: int | None = None,
    status: str | None = None,
):
    return make_event(
        source="fixture",
        source_execution_id=f"run:{name}",
        idempotency_key=stable_idempotency_key("fixture", name),
        event_type=EventType.LIFECYCLE,
        validity=Validity.EXACT,
        observed_at_ms=observed_at_ms,
        recorded_at_ms=recorded_at_ms or observed_at_ms,
        workload_kind=WorkloadKind.KANBAN_TASK,
        execution_surface=ExecutionSurface.HERMES,
        trigger_kind=TriggerKind.KANBAN,
        lifecycle_phase=LifecyclePhase.RUNNING,
        status=status,
    )


def test_append_is_idempotent_across_later_reconciliation_time(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db", clock_ms=lambda: 5_000)
    ledger.initialize()
    first = _event("same", recorded_at_ms=1_000)
    later_retry = _event("same", recorded_at_ms=4_000)

    inserted = ledger.append(first)
    deduped = ledger.append(later_retry)

    assert inserted.inserted is True
    assert deduped.inserted is False
    assert ledger.count_events() == 1
    assert next(ledger.iter_events()).recorded_at_ms == 1_000


def test_same_idempotency_key_cannot_describe_different_fact(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    ledger.append(_event("conflict", status="done"))

    with pytest.raises(IdempotencyConflictError):
        ledger.append(_event("conflict", status="failed"))


def test_initialize_backfills_permanent_dedupe_registry(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    event = _event("migration")
    ledger.append(event)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DROP TABLE execution_event_dedupe")

    ledger.initialize()

    assert ledger.append(event).inserted is False
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_event_dedupe"
        ).fetchone()[0] == 1


def test_queue_and_reconciliation_lag_are_distinct_measurements(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db", clock_ms=lambda: 5_000)
    ledger.initialize()

    result = ledger.append(
        _event("lag", observed_at_ms=1_000, recorded_at_ms=4_000)
    )

    assert result.queue_lag_ms == 1_000
    assert result.reconciliation_lag_ms == 3_000
    assert ledger.ledger_health()["max_queue_lag_ms"] == 1_000
    assert ledger.ledger_health()["max_reconciliation_lag_ms"] == 3_000


def test_retention_keeps_permanent_additive_monthly_aggregates(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    january = int(datetime(2026, 1, 10, tzinfo=UTC).timestamp() * 1000)
    february = int(datetime(2026, 2, 10, tzinfo=UTC).timestamp() * 1000)
    now = datetime(2026, 7, 1, tzinfo=UTC)
    ledger.append_batch(
        (_event("jan-1", observed_at_ms=january), _event("feb", observed_at_ms=february))
    )

    first = ledger.apply_retention(now=now)
    retained_retry = ledger.append(
        _event("jan-1", observed_at_ms=january)
    )
    with pytest.raises(IdempotencyConflictError):
        ledger.append(
            _event(
                "jan-1",
                observed_at_ms=january,
                status="conflicting_revision",
            )
        )
    ledger.append(_event("jan-2", observed_at_ms=january + 1))
    second = ledger.apply_retention(now=now)

    aggregates = ledger.monthly_aggregates()
    january_row = next(row for row in aggregates if row["month"] == "2026-01")
    assert first.deleted_events == 2
    assert retained_retry.inserted is False
    assert second.deleted_events == 1
    assert january_row["event_count"] == 2
    assert ledger.count_events() == 0
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_event_dedupe"
        ).fetchone()[0] == 3


def test_second_initialize_skips_full_dedupe_backfill(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    ledger.append(_event("retained"))

    def unexpected_hash(_payload):
        raise AssertionError("schema-v3 initialize rehashed retained events")

    monkeypatch.setattr(
        ledger_module,
        "_immutable_payload_sha256",
        unexpected_hash,
    )
    ledger.initialize()


def test_emitter_off_is_fail_open_without_starting_thread() -> None:
    emitter = NonBlockingEmitter(
        lambda _event: AppendResult("unused", True, 0),
        mode=EmitterMode.OFF,
    )

    assert emitter.start() is False
    assert emitter.try_emit(_event("off")) is False
    snapshot = emitter.snapshot()
    assert snapshot.dropped_disabled == 1
    assert snapshot.written == 0
    assert emitter_mode_from_environment({}) is EmitterMode.OFF
    assert emitter_mode_from_environment(
        {"HERMES_EXECUTION_FACTS_MODE": "shadow"}
    ) is EmitterMode.SHADOW


def test_emitter_storage_failure_does_not_escape_source_call() -> None:
    def unavailable(_event):
        raise OSError("storage unavailable")

    emitter = NonBlockingEmitter(unavailable, mode=EmitterMode.SHADOW)
    emitter.start()

    assert emitter.try_emit(_event("down")) is True
    assert emitter.wait_until_idle()
    snapshot = emitter.snapshot()

    assert snapshot.dropped_write_error == 1
    assert snapshot.last_error_class == "OSError"
    health = emitter_snapshot_event(
        snapshot,
        collector_id="fixture",
        observed_at_ms=2_000,
    )
    assert health.event_type is EventType.COLLECTOR_HEALTH
    assert health.attributes["submitted_events"] == 1
    assert health.attributes["write_errors"] == 1
    assert health.attributes["dropped"] == 1
    assert emitter.shutdown()


def test_slow_full_collector_keeps_hot_path_p95_below_one_millisecond() -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_sink(event):
        entered.set()
        assert release.wait(timeout=2)
        return AppendResult(event.event_id, True, 0)

    emitter = NonBlockingEmitter(
        slow_sink,
        mode=EmitterMode.SHADOW,
        capacity=1,
    )
    emitter.start()
    assert emitter.try_emit(_event("slow-0"))
    assert entered.wait(timeout=1)

    timings_ms: list[float] = []
    accepted = 0
    events = [_event(f"slow-{index + 1}") for index in range(10_000)]
    for event in events:
        started = time.perf_counter_ns()
        accepted += int(emitter.try_emit(event))
        timings_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    p95_ms = statistics.quantiles(timings_ms, n=100)[94]

    print(f"execution-facts enqueue p95_ms={p95_ms:.6f}")
    assert accepted == 1
    assert emitter.snapshot().dropped_full == 9_999
    assert p95_ms < 1.0
    release.set()
    assert emitter.wait_until_idle()
    assert emitter.shutdown()


def test_post_commit_off_down_slow_and_full_never_change_source_result() -> None:
    source = sqlite3.connect(":memory:")
    source.execute("CREATE TABLE lifecycle (case_name TEXT, status TEXT)")
    release = threading.Event()

    def slow_sink(event):
        release.wait(timeout=2)
        return AppendResult(event.event_id, True, 0)

    emitters = {
        "off": NonBlockingEmitter(
            lambda event: AppendResult(event.event_id, True, 0),
            mode=EmitterMode.OFF,
        ),
        "down": NonBlockingEmitter(
            lambda _event: (_ for _ in ()).throw(OSError("down")),
            mode=EmitterMode.SHADOW,
        ),
        "slow": NonBlockingEmitter(
            slow_sink,
            mode=EmitterMode.SHADOW,
        ),
        "full": NonBlockingEmitter(
            lambda event: AppendResult(event.event_id, True, 0),
            mode=EmitterMode.SHADOW,
            capacity=1,
        ),
    }
    emitters["down"].start()
    emitters["slow"].start()
    assert emitters["full"].try_emit(_event("prefill"))

    for case_name, emitter in emitters.items():
        source.execute("BEGIN")
        source.execute(
            "INSERT INTO lifecycle VALUES (?, 'done')",
            (case_name,),
        )
        source.commit()
        publish_after_commit(emitter, _event(f"case-{case_name}"))

    statuses = source.execute(
        "SELECT case_name, status FROM lifecycle ORDER BY case_name"
    ).fetchall()
    assert statuses == [
        ("down", "done"),
        ("full", "done"),
        ("off", "done"),
        ("slow", "done"),
    ]
    assert emitters["full"].snapshot().dropped_full == 1
    release.set()
    assert emitters["down"].wait_until_idle()
    assert emitters["slow"].wait_until_idle()
    assert emitters["down"].shutdown()
    assert emitters["slow"].shutdown()


def test_batch_conflict_does_not_discard_the_other_sources(tmp_path) -> None:
    """One poisoned identity must not roll back an entire shadow sweep.

    The shadow collector appends every source in a single batch. Before this
    guard a single conflicting fact aborted the transaction, so unrelated
    sources silently stopped being collected.
    """
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    ledger.append(_event("poison", status="done"))

    results = ledger.append_batch(
        (
            _event("healthy-before"),
            _event("poison", status="restated"),
            _event("healthy-after"),
        ),
        on_conflict="skip",
    )

    assert [result.inserted for result in results] == [True, False, True]
    assert [result.conflicted for result in results] == [False, True, False]
    assert ledger.count_events() == 3


def test_batch_conflict_still_raises_by_default(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    ledger.append(_event("poison", status="done"))

    with pytest.raises(IdempotencyConflictError):
        ledger.append_batch((_event("poison", status="restated"),))


def test_skipped_conflict_never_overwrites_the_retained_fact(tmp_path) -> None:
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()
    ledger.append(_event("poison", status="done"))

    ledger.append_batch(
        (_event("poison", status="restated"),), on_conflict="skip"
    )

    retained = [
        event for event in ledger.iter_events()
        if event.source_execution_id == "run:poison"
    ]
    assert len(retained) == 1
    assert retained[0].status == "done"

"""Source-adapter truth and content-canary tests."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from agent.usage_pricing import CostResult
from hermes_cli.execution_facts_contract import (
    ExecutionSurface,
    LifecyclePhase,
    Validity,
)
from hermes_cli.execution_facts_ledger import ExecutionFactsLedger
from hermes_cli.execution_facts_reconcile import (
    SystemInvocationObservation,
    reconcile_cron,
    reconcile_kanban,
    reconcile_loop_records,
    reconcile_system_invocations,
    reconcile_usage_facts,
)


def _kanban_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            outcome TEXT,
            worker_exit_kind TEXT,
            worker_exit_code INTEGER
        );
        CREATE TABLE worker_run_timeline_events (
            task_run_id INTEGER NOT NULL,
            event_kind TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            source TEXT NOT NULL,
            task_id TEXT NOT NULL,
            board TEXT NOT NULL,
            chain_root_id TEXT,
            profile TEXT
        );
        CREATE TABLE worker_run_terminal_facts (
            task_run_id INTEGER PRIMARY KEY,
            worker_exit_kind TEXT NOT NULL,
            worker_exit_code INTEGER,
            worker_protocol_state TEXT NOT NULL,
            task_outcome TEXT,
            end_reason TEXT NOT NULL,
            task_id TEXT NOT NULL,
            board TEXT NOT NULL
        );
        CREATE TABLE worker_run_retry_links (
            task_run_id INTEGER PRIMARY KEY,
            retry_of_task_run_id INTEGER NOT NULL,
            retry_class TEXT NOT NULL,
            triggering_event_id INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            board TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        """
        INSERT INTO task_runs (
            id, task_id, profile, status, started_at, ended_at, outcome,
            worker_exit_kind, worker_exit_code
        ) VALUES (?, ?, 'coder', 'done', ?, ?, 'succeeded', 'clean', 0)
        """,
        (
            (1, "task-a", 100, 110),
            (2, "task-a", 120, 130),
        ),
    )
    connection.executemany(
        """
        INSERT INTO worker_run_timeline_events VALUES (
            ?, ?, ?, 'runtime_hook', 'task-a', 'default', 'chain-a', 'coder'
        )
        """,
        (
            (1, "queued", 99_000),
            (1, "process_started", 100_000),
            (1, "first_llm_request", 101_000),
            (1, "first_token", 101_250),
            (1, "ended", 110_000),
            (2, "queued", 119_000),
            (2, "ended", 130_000),
        ),
    )
    connection.execute(
        """
        INSERT INTO worker_run_terminal_facts VALUES (
            2, 'nonzero', 1, 'valid', 'failed', 'worker_exit',
            'task-a', 'default'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO worker_run_retry_links VALUES (
            2, 1, 'auto', 99, 'task-a', 'default'
        )
        """
    )
    return connection


def test_kanban_reconciliation_prefers_exact_timeline_and_exact_retry_lineage() -> None:
    connection = _kanban_connection()

    events = reconcile_kanban(connection)

    first_request = next(
        event
        for event in events
        if event.task_run_id == "1"
        and event.lifecycle_phase is LifecyclePhase.FIRST_REQUEST
    )
    process_events = [
        event
        for event in events
        if event.task_run_id == "1"
        and event.lifecycle_phase is LifecyclePhase.PROCESS_STARTED
    ]
    retry = next(event for event in events if event.retry_class == "auto")
    terminal = next(event for event in events if event.end_reason == "worker_exit")

    assert first_request.validity is Validity.EXACT
    assert len(process_events) == 1
    assert process_events[0].validity is Validity.EXACT
    assert retry.validity is Validity.EXACT
    assert retry.trigger_kind.value == "retry"
    assert terminal.exit_code == 1
    assert all(event.attributes["retry_instrumented"] for event in events)


def test_retry_instrumentation_is_proven_per_run_not_by_table_existence() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
            status TEXT NOT NULL, started_at INTEGER NOT NULL,
            ended_at INTEGER, outcome TEXT, worker_exit_kind TEXT,
            worker_exit_code INTEGER
        );
        CREATE TABLE worker_run_timeline_events (
            task_run_id INTEGER NOT NULL, event_kind TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL, source TEXT NOT NULL,
            task_id TEXT NOT NULL, board TEXT NOT NULL,
            chain_root_id TEXT, profile TEXT
        );
        CREATE TABLE worker_run_retry_links (
            task_run_id INTEGER PRIMARY KEY,
            retry_of_task_run_id INTEGER NOT NULL,
            retry_class TEXT NOT NULL, triggering_event_id INTEGER NOT NULL,
            task_id TEXT NOT NULL, board TEXT NOT NULL
        );
        INSERT INTO task_runs VALUES
            (1, 'historical', 'coder', 'done', 100, 110, 'done', 'clean', 0),
            (2, 'instrumented', 'coder', 'done', 120, 130, 'done', 'clean', 0);
        INSERT INTO worker_run_timeline_events VALUES
            (2, 'process_started', 120000, 'runtime_hook', 'instrumented',
             'default', NULL, 'coder');
        """
    )

    events = reconcile_kanban(connection)
    by_run = {
        run_id: {
            event.attributes["retry_instrumented"]
            for event in events
            if event.task_run_id == run_id
        }
        for run_id in ("1", "2")
    }

    assert by_run == {"1": {False}, "2": {True}}


def test_mutable_kanban_fallback_appends_content_revisions(
    tmp_path,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
            status TEXT NOT NULL, started_at INTEGER NOT NULL,
            ended_at INTEGER, outcome TEXT, worker_exit_kind TEXT,
            worker_exit_code INTEGER
        )
        """
    )
    connection.execute(
        "INSERT INTO task_runs VALUES "
        "(1, 'task-a', 'coder', 'running', 100, NULL, NULL, NULL, NULL)"
    )
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()

    first = reconcile_kanban(connection)
    ledger.append_batch(first)
    connection.execute(
        """
        UPDATE task_runs
           SET status='done', ended_at=110, outcome='succeeded',
               worker_exit_kind='clean', worker_exit_code=0
         WHERE id=1
        """
    )
    second = reconcile_kanban(connection)
    results = ledger.append_batch(second)

    first_start = next(
        event
        for event in first
        if event.lifecycle_phase is LifecyclePhase.PROCESS_STARTED
    )
    second_start = next(
        event
        for event in second
        if event.lifecycle_phase is LifecyclePhase.PROCESS_STARTED
    )
    assert second_start.idempotency_key != first_start.idempotency_key
    assert all(result.inserted for result in results)
    assert ledger.count_events() == 4


def test_mutable_terminal_fact_appends_content_revision(tmp_path) -> None:
    connection = _kanban_connection()
    ledger = ExecutionFactsLedger(tmp_path / "facts.db")
    ledger.initialize()

    first = reconcile_kanban(connection)
    ledger.append_batch(first)
    first_terminal = next(
        event for event in first if event.end_reason == "worker_exit"
    )
    connection.execute(
        """
        UPDATE worker_run_terminal_facts
           SET worker_exit_kind='clean', worker_exit_code=0,
               task_outcome='succeeded', end_reason='completed'
         WHERE task_run_id=2
        """
    )

    second = reconcile_kanban(connection)
    results = ledger.append_batch(second)
    second_terminal = next(
        event for event in second if event.end_reason == "completed"
    )

    assert second_terminal.idempotency_key != first_terminal.idempotency_key
    assert any(
        event.idempotency_key == second_terminal.idempotency_key
        and result.inserted
        for event, result in zip(second, results, strict=True)
    )
    assert ledger.count_events() == len(first) + 1


def test_cron_reconciliation_classifies_error_without_persisting_message() -> None:
    canary = "DO_NOT_PERSIST_CRON_ERROR_CONTENT"
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE executions (
            id TEXT, job_id TEXT, source TEXT, status TEXT,
            claimed_at TEXT, started_at TEXT, finished_at TEXT, error TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO executions VALUES (
            'cron-1', 'health-check', 'scheduler', 'failed',
            '2026-07-30T10:00:00+00:00',
            '2026-07-30T10:00:01+00:00',
            '2026-07-30T10:00:02+00:00', ?
        )
        """,
        (canary,),
    )

    events = reconcile_cron(connection)
    payload = "\n".join(event.to_json() for event in events)

    assert [event.lifecycle_phase for event in events] == [
        LifecyclePhase.CLAIMED,
        LifecyclePhase.PROCESS_STARTED,
        LifecyclePhase.ENDED,
    ]
    assert events[-1].error_class == "reported_error"
    assert canary not in payload


def test_cron_reconciliation_tolerates_schema_without_optional_error() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE executions (
            id TEXT, job_id TEXT, status TEXT,
            claimed_at TEXT, started_at TEXT, finished_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO executions VALUES (
            'cron-plain', 'health-check', 'done',
            '2026-07-30T10:00:00+00:00',
            '2026-07-30T10:00:01+00:00',
            '2026-07-30T10:00:02+00:00'
        )
        """
    )

    events = reconcile_cron(connection)

    assert len(events) == 3
    assert events[-1].error_class is None
    assert all(
        event.attributes["retry_instrumented"] is False
        for event in events
    )


def test_cron_retry_instrumentation_is_proven_per_execution() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE executions (
            id TEXT, job_id TEXT, status TEXT,
            claimed_at TEXT, started_at TEXT, finished_at TEXT,
            retry_of_execution_id TEXT, retry_class TEXT,
            retry_instrumented INTEGER
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO executions VALUES (
            ?, 'health-check', 'done',
            '2026-07-30T10:00:00+00:00',
            '2026-07-30T10:00:01+00:00',
            '2026-07-30T10:00:02+00:00', ?, ?, ?
        )
        """,
        (
            ("historical", None, None, None),
            ("explicit-new", None, None, 1),
            ("retry", "historical", "automatic", None),
        ),
    )

    events = reconcile_cron(connection)
    flags = {
        event.cron_execution_id: event.attributes["retry_instrumented"]
        for event in events
    }

    assert flags == {
        "historical": False,
        "explicit-new": True,
        "retry": True,
    }


def test_system_invocation_key_is_stable_across_timestamp_recomputation() -> None:
    first = reconcile_system_invocations(
        (
            SystemInvocationObservation(
                source_execution_id="systemd:unit:invocation",
                surface=ExecutionSurface.SYSTEMD,
                observed_at_ms=1_000,
                phase=LifecyclePhase.PROCESS_STARTED,
            ),
        )
    )[0]
    second = reconcile_system_invocations(
        (
            SystemInvocationObservation(
                source_execution_id="systemd:unit:invocation",
                surface=ExecutionSurface.SYSTEMD,
                observed_at_ms=1_009,
                phase=LifecyclePhase.PROCESS_STARTED,
            ),
        )
    )[0]

    assert first.idempotency_key == second.idempotency_key


def test_loop_reconciliation_uses_explicit_allowlist_and_honest_derived_id() -> None:
    canary = "DO_NOT_PERSIST_LOOP_PLAN_OR_OUTPUT"
    records = [
        {
            "ts": "2026-07-30T11:00:00+00:00",
            "pack": "fleet",
            "event": "phase_usage",
            "round": 1,
            "phase": "build",
            "engine": "xai",
            "model": "grok-4.5",
            "input_tokens": 220,
            "cached_input_tokens": 180,
            "output_tokens": 50,
            "total_tokens": 270,
            "billing": "subscription",
            "metered_cost_eur": 0,
            "plan": canary,
            "output": canary,
        }
    ]

    event = reconcile_loop_records(records)[0]

    assert event.validity is Validity.DERIVED
    assert event.attributes["total_tokens"] == 270
    assert event.attributes["metered_cost"] == "0"
    assert event.attributes["currency"] == "EUR"
    assert canary not in event.to_json()


def test_loop_status_only_record_is_a_bounded_outcome() -> None:
    event = reconcile_loop_records(
        (
            {
                "ts": "2026-07-30T11:00:00+00:00",
                "pack": "fleet",
                "event": "proof",
                "status": "ok",
                "proof": "DO_NOT_PERSIST_PROOF_CONTENT",
            },
        )
    )[0]

    assert event.event_type.value == "outcome_observed"
    assert event.lifecycle_phase is None
    assert event.status == "ok"
    assert "DO_NOT_PERSIST" not in event.to_json()


def test_systemd_and_crontab_are_explicit_distinct_surfaces() -> None:
    events = reconcile_system_invocations(
        (
            SystemInvocationObservation(
                "timer:1",
                ExecutionSurface.SYSTEMD,
                1_000,
                LifecyclePhase.PROCESS_STARTED,
            ),
            SystemInvocationObservation(
                "cron:1",
                ExecutionSurface.CRONTAB,
                2_000,
                LifecyclePhase.ENDED,
                status="ok",
                exit_code=0,
            ),
        )
    )

    assert [event.execution_surface for event in events] == [
        ExecutionSurface.SYSTEMD,
        ExecutionSurface.CRONTAB,
    ]
    assert [event.trigger_kind.value for event in events] == [
        "systemd",
        "crontab",
    ]


def test_usage_bridge_keeps_unknown_nullable_and_allocates_subscription_fee() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE run_usage_facts (
            run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
            task_id TEXT, chain_id TEXT, board TEXT, provider TEXT, model TEXT,
            profile TEXT, billing_mode TEXT, serving_tier TEXT,
            reasoning_effort TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, finish_reason TEXT, error_type TEXT,
            duration_ms REAL, captured_at TEXT, source TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO run_usage_facts VALUES (
            ?, 'kimi_cli', ?, 'task-a', 'chain-a', 'default',
            'kimi-coding', 'k3', 'coder', 'subscription_included',
            NULL, NULL, ?, 0, 0, 0, NULL, 'stop', NULL, 1000,
            '2026-07-30T10:00:00+00:00', ?
        )
        """,
        (
            ("run-a", "1", 100, "measured"),
            ("run-b", "2", 200, "measured"),
            ("run-unknown", None, None, "unknown"),
        ),
    )
    connection.execute(
        """
        UPDATE run_usage_facts
           SET model='provider model label with spaces'
         WHERE run_id='run-unknown'
        """
    )

    def equivalent(_model, usage, **_kwargs):
        return CostResult(
            Decimal(usage.input_tokens),
            "equivalent",
            "official_docs_snapshot",
            "fixture",
            pricing_version="fixture-prices-v1",
        )

    def actual(_model, _usage, **_kwargs):
        return CostResult(
            Decimal(0),
            "included",
            "none",
            "included",
            pricing_version="included-route",
        )

    events = reconcile_usage_facts(
        connection,
        subscription_fees={
            "kimi-coding": "999",
            "2026-07:kimi-coding": "30",
        },
        fee_version="kimi-fee-2026-07",
        equivalent_estimator=equivalent,
        actual_estimator=actual,
    )
    costs = [event for event in events if event.event_type.value == "cost_observed"]
    usage_unknown = next(
        event
        for event in events
        if event.event_type.value == "usage_observed"
        and event.validity is Validity.UNKNOWN
    )

    assert all(
        "allocated_subscription_cost" not in event.attributes
        for event in costs
    )
    assert all(event.attributes["marginal_cost"] == "0" for event in costs)
    assert all(event.validity is Validity.DERIVED for event in costs)
    assert usage_unknown.validity is Validity.UNKNOWN
    assert "total_tokens" not in usage_unknown.attributes
    assert "api_equivalent_cost" not in usage_unknown.attributes
    assert "agent_model" not in usage_unknown.attributes

    connection.execute(
        """
        UPDATE run_usage_facts
           SET model='k3', input_tokens=0, output_tokens=0,
               cache_read_tokens=0, cache_write_tokens=0, source='measured'
         WHERE run_id='run-unknown'
        """
    )
    complete_population = reconcile_usage_facts(
        connection,
        subscription_fees={"2026-07:kimi-coding": "30"},
        fee_version="kimi-fee-2026-07",
        equivalent_estimator=equivalent,
        actual_estimator=actual,
    )
    complete_costs = [
        event
        for event in complete_population
        if event.event_type.value == "cost_observed"
    ]
    assert [
        event.attributes["allocated_subscription_cost"]
        for event in complete_costs
    ] == ["10.00000000", "20.00000000", "0"]
    assert all(
        event.attributes["population_size"] == 3
        for event in complete_costs
    )
    assert sum(
        Decimal(event.attributes["allocated_subscription_cost"])
        for event in complete_costs
    ) == Decimal(30)

    limited = reconcile_usage_facts(
        connection,
        limit=1,
        equivalent_estimator=equivalent,
        actual_estimator=actual,
    )
    assert {
        event.task_run_id for event in limited
    } == {"1"}
    with pytest.raises(ValueError, match="unbounded monthly cohort"):
        reconcile_usage_facts(
            connection,
            subscription_fees={"2026-07:kimi-coding": "30"},
            fee_version="kimi-fee-2026-07",
            limit=1,
            equivalent_estimator=equivalent,
            actual_estimator=actual,
        )

    original_keys = {
        (event.task_run_id, event.event_type.value): event.idempotency_key
        for event in events
    }
    connection.execute(
        """
        UPDATE run_usage_facts
           SET input_tokens=101,
               captured_at='2026-07-30T10:01:00+00:00'
         WHERE run_id='run-a'
        """
    )
    revised = reconcile_usage_facts(
        connection,
        subscription_fees={"2026-07:kimi-coding": "30"},
        fee_version="kimi-fee-2026-07",
        equivalent_estimator=equivalent,
        actual_estimator=actual,
    )
    revised_keys = {
        (event.task_run_id, event.event_type.value): event.idempotency_key
        for event in revised
    }
    assert revised_keys[
        ("1", "usage_observed")
    ] != original_keys[("1", "usage_observed")]
    assert revised_keys[
        ("2", "usage_observed")
    ] == original_keys[("2", "usage_observed")]
    assert revised_keys[
        ("2", "cost_observed")
    ] != original_keys[("2", "cost_observed")]
    assert [
        event.idempotency_key
        for event in reconcile_usage_facts(
            connection,
            subscription_fees={"2026-07:kimi-coding": "30"},
            fee_version="kimi-fee-2026-07",
            equivalent_estimator=equivalent,
            actual_estimator=actual,
        )
    ] == [event.idempotency_key for event in revised]


def test_run_8802_control_sample_reproduces_exact_api_equivalent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE run_usage_facts (
            run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
            task_id TEXT, chain_id TEXT, board TEXT, provider TEXT, model TEXT,
            profile TEXT, billing_mode TEXT, serving_tier TEXT,
            reasoning_effort TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, finish_reason TEXT, error_type TEXT,
            duration_ms REAL, captured_at TEXT, source TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO run_usage_facts VALUES (
            '8802', 'hermes_agent', '8802', 't_9c40a433', 't_cda9e14e',
            'default', 'kimi-coding', 'k3', 'coder',
            'subscription_included', NULL, NULL,
            42668, 3202, 118784, 0, NULL, 'stop', NULL, NULL,
            '2026-07-30T18:00:00+00:00', 'measured'
        )
        """
    )

    events = reconcile_usage_facts(connection)
    usage = next(
        event for event in events if event.event_type.value == "usage_observed"
    )
    cost = next(
        event for event in events if event.event_type.value == "cost_observed"
    )

    assert usage.attributes["total_tokens"] == 164654
    assert cost.attributes["api_equivalent_cost"] == "0.2116692"
    assert cost.attributes["marginal_cost"] == "0"
    assert cost.attributes["pricing_version"] == "moonshot-k3-2026-07"
    assert "allocated_subscription_cost" not in cost.attributes


def test_inclusive_usage_subtracts_both_cache_buckets_before_pricing() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE run_usage_facts (
            run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
            task_id TEXT, chain_id TEXT, board TEXT, provider TEXT, model TEXT,
            profile TEXT, billing_mode TEXT, serving_tier TEXT,
            reasoning_effort TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, finish_reason TEXT, error_type TEXT,
            duration_ms REAL, captured_at TEXT, source TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO run_usage_facts VALUES (
            'inclusive', 'kimi_cli', NULL, NULL, NULL, NULL,
            'kimi-coding', 'k3', NULL, 'subscription_included', NULL, NULL,
            100, 10, 60, 15, NULL, 'stop', NULL, NULL,
            '2026-07-30T18:00:00+00:00', 'measured'
        )
        """
    )

    observed: list[tuple[int, int, int]] = []

    def equivalent(_model, usage, **_kwargs):
        observed.append(
            (
                usage.input_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            )
        )
        return CostResult(
            Decimal("1"),
            "equivalent",
            "official_docs_snapshot",
            "fixture",
        )

    reconcile_usage_facts(connection, equivalent_estimator=equivalent)

    assert observed == [(25, 60, 15)]


def test_inclusive_usage_with_missing_cache_split_stays_unknown() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE run_usage_facts (
            run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
            task_id TEXT, chain_id TEXT, board TEXT, provider TEXT, model TEXT,
            profile TEXT, billing_mode TEXT, serving_tier TEXT,
            reasoning_effort TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, finish_reason TEXT, error_type TEXT,
            duration_ms REAL, captured_at TEXT, source TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO run_usage_facts VALUES (
            'partial', 'kimi_cli', NULL, NULL, NULL, NULL,
            'kimi-coding', 'k3', NULL, 'subscription_included', NULL, NULL,
            100, 10, 60, NULL, NULL, 'stop', NULL, NULL,
            '2026-07-30T18:00:00+00:00', 'measured'
        )
        """
    )

    events = reconcile_usage_facts(connection)

    usage = next(
        event for event in events if event.event_type.value == "usage_observed"
    )
    assert usage.validity is Validity.UNKNOWN
    assert "total_tokens" not in usage.attributes
    assert not any(event.event_type.value == "cost_observed" for event in events)


def test_subscription_row_is_free_at_the_margin_whatever_the_price_route() -> None:
    """The recorded billing mode outranks the pricing route's provider guess.

    Claude Code records provider='anthropic', which the pricing route reads as
    metered API usage. Live that was 104253 of 109363 usage rows, every one of
    them billed at list price although the recorded billing_mode says the
    request is already covered by the subscription.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE run_usage_facts (
            run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
            task_id TEXT, chain_id TEXT, board TEXT, provider TEXT, model TEXT,
            profile TEXT, billing_mode TEXT, serving_tier TEXT,
            reasoning_effort TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, finish_reason TEXT, error_type TEXT,
            duration_ms REAL, captured_at TEXT, source TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO run_usage_facts VALUES (
            '9001', 'claude_code', NULL, NULL, NULL, NULL,
            'anthropic', 'claude-sonnet-5', NULL,
            'subscription_included', NULL, NULL,
            1000, 500, 2000, 0, NULL, 'stop', NULL, NULL,
            '2026-07-31T08:00:00+00:00', 'measured'
        )
        """
    )

    events = reconcile_usage_facts(connection)
    cost = next(
        event for event in events if event.event_type.value == "cost_observed"
    )

    # An extra request on a flat subscription costs nothing extra...
    assert cost.attributes["marginal_cost"] == "0"
    assert "metered_cost" not in cost.attributes
    # ...while what it would have cost on the API stays visible.
    assert float(cost.attributes["api_equivalent_cost"]) > 0


def test_metered_row_keeps_its_real_price() -> None:
    """Guard the other direction: real API usage must not become free."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE run_usage_facts (
            run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
            task_id TEXT, chain_id TEXT, board TEXT, provider TEXT, model TEXT,
            profile TEXT, billing_mode TEXT, serving_tier TEXT,
            reasoning_effort TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, finish_reason TEXT, error_type TEXT,
            duration_ms REAL, captured_at TEXT, source TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO run_usage_facts VALUES (
            '9002', 'hermes_agent', NULL, NULL, NULL, NULL,
            'anthropic', 'claude-sonnet-5', NULL,
            'metered', NULL, NULL,
            1000, 500, 2000, 0, NULL, 'stop', NULL, NULL,
            '2026-07-31T08:00:00+00:00', 'measured'
        )
        """
    )

    events = reconcile_usage_facts(connection)
    cost = next(
        event for event in events if event.event_type.value == "cost_observed"
    )

    assert float(cost.attributes["metered_cost"]) > 0
    assert cost.attributes["marginal_cost"] == cost.attributes["metered_cost"]

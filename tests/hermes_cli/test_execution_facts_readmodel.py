"""P0 presentation contract tests: denominators, validity, and unknowns."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

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
from hermes_cli.execution_facts_ledger import (
    EmitterSnapshot,
    ExecutionFactsLedger,
    emitter_snapshot_event,
)
from hermes_cli.execution_facts_instrumentation import (
    ShadowValidationObservation,
    shadow_validation_event,
)
from hermes_cli.execution_facts_projection import rebuild_projections
from hermes_cli.execution_facts_readmodel import (
    READMODEL_VERSION,
    build_execution_facts_payload,
)
from hermes_cli.execution_facts_shadow import (
    SourceCohort,
    source_census_event,
)


def _event(source: str, run: str, tag: str, at: int, **fields):
    defaults = {
        "source": source,
        "source_execution_id": run,
        "idempotency_key": stable_idempotency_key(source, run, tag),
        "event_type": EventType.LIFECYCLE,
        "validity": Validity.EXACT,
        "observed_at_ms": at,
        "recorded_at_ms": at,
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


def _seed(path) -> None:
    ledger = ExecutionFactsLedger(path)
    ledger.initialize()
    tool_span = stable_span_id("kanban_timeline", "tool-1")
    test_span = stable_span_id("kanban_timeline", "test-1")
    agent_span = stable_span_id("kanban_timeline", "agent-1")
    ledger.append_batch(
        (
            _event(
                "kanban_timeline",
                "task_run:1",
                "queued",
                1_000,
                lifecycle_phase=LifecyclePhase.QUEUED,
                attributes={"retry_instrumented": True},
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "claimed",
                2_000,
                lifecycle_phase=LifecyclePhase.CLAIMED,
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "process",
                3_000,
                lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "request",
                3_100,
                lifecycle_phase=LifecyclePhase.FIRST_REQUEST,
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "token",
                3_250,
                lifecycle_phase=LifecyclePhase.FIRST_TOKEN,
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "usage",
                3_500,
                event_type=EventType.USAGE_OBSERVED,
                attributes={
                    "input_tokens": 42668,
                    "cache_read_tokens": 118784,
                    "output_tokens": 3202,
                    "total_tokens": 164654,
                    "agent_provider": "kimi",
                    "agent_model": "kimi-k3",
                },
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "cost",
                3_501,
                event_type=EventType.COST_OBSERVED,
                attributes={
                    "api_equivalent_cost": "0.2116692",
                    "allocated_subscription_cost": "1.25",
                    "marginal_cost": "0",
                    "currency": "USD",
                    "pricing_version": "models-2026-07-30",
                    "fee_version": "subscription-2026-07",
                },
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "agent",
                3_550,
                event_type=EventType.SPAN_STARTED,
                span_id=agent_span,
                span_kind=SpanKind.AGENT,
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "tool",
                3_600,
                event_type=EventType.SPAN_STARTED,
                span_id=tool_span,
                parent_span_id=agent_span,
                span_kind=SpanKind.TOOL,
                attributes={
                    "tool_name": "terminal",
                    "raw_tool_name": "functions.exec_command",
                },
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "test",
                3_700,
                event_type=EventType.SPAN_STARTED,
                span_id=test_span,
                parent_span_id=agent_span,
                span_kind=SpanKind.TEST,
                attributes={
                    "selected_tests": 307,
                    "executed_tests": 0,
                    "passed_tests": 0,
                    "failed_tests": 0,
                    "skipped_tests": 0,
                    "timeout_lost_tests": 307,
                    "timeout": True,
                },
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "end",
                5_000,
                event_type=EventType.OUTCOME_OBSERVED,
                lifecycle_phase=LifecyclePhase.ENDED,
                status="done",
                exit_code=0,
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "land",
                5_100,
                lifecycle_phase=LifecyclePhase.LANDED,
                attributes={"landed_sha": "a" * 40},
            ),
            _event(
                "kanban_timeline",
                "task_run:1",
                "deploy",
                5_200,
                lifecycle_phase=LifecyclePhase.DEPLOYED,
                attributes={"deployed_sha": "a" * 40},
            ),
            _event(
                "kanban_timeline",
                "task_run:2",
                "retry",
                5_300,
                trigger_kind=TriggerKind.RETRY,
                lifecycle_phase=LifecyclePhase.QUEUED,
                retry_class="auto",
                attributes={"retry_instrumented": True},
            ),
            _event(
                "tmux_reconciliation",
                "terminal:1",
                "observed",
                9_900,
                event_type=EventType.TERMINAL_OBSERVED,
                validity=Validity.LOWER_BOUND,
                workload_kind=WorkloadKind.UNCLASSIFIED,
                execution_surface=ExecutionSurface.TMUX,
                trigger_kind=TriggerKind.RECONCILIATION,
                lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
                terminal_run_id="terminal_1",
                terminal_generation=1,
            ),
            _event(
                "hermes_cron",
                "cron:1",
                "end",
                6_000,
                event_type=EventType.OUTCOME_OBSERVED,
                workload_kind=WorkloadKind.CRON_JOB,
                execution_surface=ExecutionSurface.PROCESS,
                trigger_kind=TriggerKind.HERMES_CRON,
                lifecycle_phase=LifecyclePhase.ENDED,
                status="ok",
            ),
            _event(
                "loop_ledger",
                "loop:1",
                "end",
                7_000,
                event_type=EventType.OUTCOME_OBSERVED,
                workload_kind=WorkloadKind.LOOP,
                execution_surface=ExecutionSurface.LOOP,
                trigger_kind=TriggerKind.LOOP_PHASE,
                lifecycle_phase=LifecyclePhase.ENDED,
                status="ok",
            ),
        )
    )
    rebuild_projections(path)


def test_readmodel_makes_every_p0_metric_displayable_with_truth_state(tmp_path) -> None:
    path = tmp_path / "facts.db"
    _seed(path)

    payload = build_execution_facts_payload(
        path,
        now=datetime.fromtimestamp(10, tz=UTC),
    )

    assert payload["schema_version"] == READMODEL_VERSION
    assert set(payload["p0"]) == {
        "run_identity",
        "kanban",
        "terminal_identity",
        "cron_systemd",
        "loops",
        "tokens_models_costs",
        "data_trust",
    }
    for metric in payload["p0"].values():
        assert {"computed_status", "validity", "observed", "eligible"} <= set(metric)
    assert payload["p0"]["run_identity"]["eligible"] is None
    assert payload["p0"]["run_identity"]["percent"] is None
    assert payload["p0"]["run_identity"]["unknown_reason"] == (
        "universal_external_execution_denominator_unavailable"
    )
    assert payload["p0"]["kanban"]["percent"] == "100.00"
    assert payload["retries"]["observed"] == 1
    assert payload["retries"]["eligible"] == 2
    assert payload["retries"]["by_class"] == {"auto": 1}
    assert payload["usage"]["tokens"]["total_tokens"] == 164654
    assert payload["usage"]["costs"]["api_equivalent_total"] == "0.2116692"
    assert payload["usage"]["costs"]["allocated_subscription_total"] == "1.25"
    assert payload["usage"]["costs"]["marginal_total"] == "0"
    assert payload["usage"]["costs"]["pricing_versions"] == [
        "models-2026-07-30"
    ]
    assert payload["usage"]["costs"]["fx_status"] == (
        "not_applicable_same_currency"
    )
    assert payload["tools"]["by_name"] == {"terminal": 1}
    assert payload["tools"]["by_dimensions"][0]["key"]["tool_name"] == "terminal"
    assert payload["tools"]["by_dimensions"][0]["completed"] == 0
    assert payload["tests"]["selected"] == 307
    assert payload["tests"]["executed"] == 0
    assert payload["tests"]["timeout_lost"] == 307
    assert payload["tests"]["timeouts"] == 1
    assert payload["tests"]["gate_results"] == {"unknown:unknown": 1}
    assert payload["tests"]["by_dimensions"][0]["timeouts"] == 1
    assert payload["tests"]["by_dimensions"][0]["selected"] == 307
    assert payload["tests"]["by_dimensions"][0]["timeout_lost"] == 307
    ttft = next(
        metric
        for metric in payload["lifecycle_latencies"]
        if metric["metric_id"] == "request_to_first_token"
    )
    assert ttft["p50_ms"] == 150
    assert payload["landing"]["observed"] == 1
    assert payload["landing"]["sha_evidence"] == 1
    assert payload["deployment"]["observed"] == 1
    assert payload["deployment"]["sha_evidence"] == 1
    assert payload["collector"]["computed_status"] == "unknown"
    assert payload["shadow_gates"]["activation_effect"] == "none"
    assert all(
        source["computed_status"] == "shadow_blocked"
        for source in payload["shadow_gates"]["sources"]
    )
    assert payload["alert_gate"] == {
        "computed_status": "ready",
        "reasons": [],
    }


def test_missing_test_counters_stay_unknown_with_observed_lower_bound(
    tmp_path,
) -> None:
    path = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(path)
    ledger.initialize()
    ledger.append_batch(
        (
            _event(
                "test_observer",
                "test-run:complete",
                "agent-complete",
                999,
                event_type=EventType.SPAN_STARTED,
                span_id=stable_span_id(
                    "test_observer", "agent-complete"
                ),
                span_kind=SpanKind.AGENT,
            ),
            _event(
                "test_observer",
                "test-run:complete",
                "test-complete",
                1_000,
                event_type=EventType.SPAN_STARTED,
                span_id=stable_span_id("test_observer", "complete"),
                parent_span_id=stable_span_id(
                    "test_observer", "agent-complete"
                ),
                span_kind=SpanKind.TEST,
                attributes={
                    "selected_tests": 5,
                    "executed_tests": 5,
                    "passed_tests": 5,
                    "failed_tests": 0,
                    "skipped_tests": 0,
                    "timeout_lost_tests": 0,
                },
            ),
            _event(
                "test_observer",
                "test-run:missing",
                "agent-missing",
                1_999,
                event_type=EventType.SPAN_STARTED,
                span_id=stable_span_id(
                    "test_observer", "agent-missing"
                ),
                span_kind=SpanKind.AGENT,
            ),
            _event(
                "test_observer",
                "test-run:missing",
                "test-missing",
                2_000,
                event_type=EventType.SPAN_STARTED,
                span_id=stable_span_id("test_observer", "missing"),
                parent_span_id=stable_span_id(
                    "test_observer", "agent-missing"
                ),
                span_kind=SpanKind.TEST,
                attributes={"gate_type": "targeted"},
            ),
        )
    )
    rebuild_projections(path)

    payload = build_execution_facts_payload(
        path,
        now=datetime.fromtimestamp(3, tz=UTC),
    )

    assert payload["tests"]["computed_status"] == "unknown"
    assert payload["tests"]["selected"] is None
    assert (
        payload["tests"]["counter_observed_lower_bounds"]["selected"] == 5
    )
    assert payload["tests"]["counter_coverage"]["selected"] == {
        "computed_status": "partial",
        "observed_rows": 1,
        "denominator_rows": 2,
        "validity": "unknown",
    }
    assert payload["tests"]["by_dimensions"][0]["selected"] is None


def test_freshness_is_per_source_not_masked_by_one_fresh_writer(tmp_path) -> None:
    path = tmp_path / "facts.db"
    _seed(path)

    payload = build_execution_facts_payload(
        path,
        now=datetime.fromtimestamp(10, tz=UTC),
        source_freshness_ms=500,
    )
    by_source = {row["source"]: row for row in payload["sources"]}

    assert by_source["tmux_reconciliation"]["freshness_status"] == "fresh"
    assert by_source["kanban_timeline"]["freshness_status"] == "stale"
    assert payload["alert_gate"]["computed_status"] == "unknown"
    assert any(
        reason.startswith("stale_sources:")
        for reason in payload["alert_gate"]["reasons"]
    )


def test_readmodel_honors_execution_facts_environment_path(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "facts.db"
    _seed(path)
    monkeypatch.setenv("HERMES_EXECUTION_FACTS_DB", str(path))

    payload = build_execution_facts_payload(
        now=datetime.fromtimestamp(10, tz=UTC)
    )

    assert payload["database"]["path"] == str(path)
    assert payload["database"]["computed_status"] == "ready"


def test_missing_database_returns_unknown_never_synthetic_zero(tmp_path) -> None:
    payload = build_execution_facts_payload(tmp_path / "missing.db")

    assert payload["database"]["computed_status"] == "unavailable"
    assert payload["p0"]["kanban"]["observed"] is None
    assert payload["p0"]["kanban"]["eligible"] is None
    assert payload["usage"]["tokens"]["computed_status"] == "unknown"
    assert payload["usage"]["tokens"].get("total_tokens") is None


def test_shadow_gate_requires_complete_fresh_source_specific_proof(
    tmp_path,
) -> None:
    path = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(path, clock_ms=lambda: 10_000)
    ledger.initialize()
    source_events = tuple(
        _event(
            "fixture_source",
            f"run:{index}",
            "observed",
            9_000 + index,
            workload_kind=WorkloadKind.SYSTEM_JOB,
            execution_surface=ExecutionSurface.PROCESS,
            trigger_kind=TriggerKind.RECONCILIATION,
            lifecycle_phase=LifecyclePhase.ENDED,
            status="ok",
        )
        for index in range(20)
    )
    collector = emitter_snapshot_event(
        EmitterSnapshot(
            mode="shadow",
            submitted=20,
            accepted=20,
            dropped_disabled=0,
            dropped_full=0,
            dropped_write_error=0,
            deduped=20,
            written=20,
            queue_depth=0,
            queue_capacity=64,
            last_enqueue_at_ms=9_500,
            last_write_at_ms=9_500,
            last_error_class=None,
        ),
        collector_id="fixture",
        observed_at_ms=9_500,
    )
    passing_proof = shadow_validation_event(
        ShadowValidationObservation(
            validated_source="fixture_source",
            observed_at_ms=9_500,
            sample_size=20,
            identity_coverage_passed=True,
            metric_coverage_passed=True,
            capture_p95_us=999,
            dedupe_reconciled=True,
            behavior_equivalent=True,
            evidence_ref="validation:fixture:passing",
        )
    )
    census = source_census_event(
        SourceCohort(
            source="fixture_source",
            events=source_events,
            eligible=20,
            identity_observed=20,
            metric_observed=20,
            eligibility_rule="fixture_exact_cohort",
            store_count=1,
            read_errors=0,
            window_start_ms=9_000,
            window_end_ms=9_500,
            behavior_equivalent=True,
            behavior_proof="sqlite_query_only_snapshot",
        ),
        scan_id="fixture-scan",
        observed_at_ms=9_500,
        writer_p50_us=500,
        writer_p95_us=999,
        dedupe_reconciled=True,
    )
    ledger.append_batch(
        (*source_events, collector, census, passing_proof)
    )
    rebuild_projections(path)

    payload = build_execution_facts_payload(
        path,
        now=datetime.fromtimestamp(10, tz=UTC),
    )
    gate = next(
        row
        for row in payload["shadow_gates"]["sources"]
        if row["source"] == "fixture_source"
    )
    failed_checks = [
        name for name, passed in gate["checks"].items() if not passed
    ]
    assert failed_checks == [], failed_checks
    assert gate["computed_status"] == "shadow_pass"
    assert all(gate["checks"].values())
    assert gate["activation_effect"] == "none"

    failing_proof = shadow_validation_event(
        ShadowValidationObservation(
            validated_source="fixture_source",
            observed_at_ms=9_600,
            sample_size=20,
            identity_coverage_passed=True,
            metric_coverage_passed=True,
            capture_p95_us=999,
            dedupe_reconciled=True,
            behavior_equivalent=False,
            evidence_ref="validation:fixture:behavior_block",
        )
    )
    ledger.append(failing_proof)
    rebuild_projections(path)

    blocked_payload = build_execution_facts_payload(
        path,
        now=datetime.fromtimestamp(10, tz=UTC),
    )
    blocked_gate = next(
        row
        for row in blocked_payload["shadow_gates"]["sources"]
        if row["source"] == "fixture_source"
    )
    assert blocked_gate["computed_status"] == "shadow_blocked"
    assert blocked_gate["checks"]["behavior_equivalence"] is False


def test_versioned_cli_reconciles_fixture_validates_and_reads_without_writes(
    tmp_path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "execution_facts.py"
    fixture = (
        repo
        / "tests"
        / "fixtures"
        / "execution_facts"
        / "system-observations.jsonl"
    )
    database = tmp_path / "execution_facts.db"

    reconcile = subprocess.run(
        [
            sys.executable,
            str(script),
            "reconcile",
            "--db",
            str(database),
            "--system-observations",
            str(fixture),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    validate = subprocess.run(
        [
            sys.executable,
            str(script),
            "validate",
            "--db",
            str(database),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    before = database.stat().st_mtime_ns
    show = subprocess.run(
        [
            sys.executable,
            str(script),
            "show",
            "--db",
            str(database),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    after = database.stat().st_mtime_ns

    assert reconcile.returncode == 0, reconcile.stderr
    assert validate.returncode == 0, validate.stdout + validate.stderr
    assert show.returncode == 0, show.stderr
    assert before == after
    payload = json.loads(show.stdout)
    assert payload["schema_version"] == READMODEL_VERSION
    assert {
        row["source"] for row in payload["sources"]
    } == {"crontab_invocation", "systemd_invocation"}

    proof = subprocess.run(
        [
            sys.executable,
            str(script),
            "record-shadow-proof",
            "--db",
            str(database),
            "--source",
            "systemd_invocation",
            "--sample-size",
            "20",
            "--capture-p95-us",
            "420",
            "--identity-coverage-passed",
            "--metric-coverage-passed",
            "--dedupe-reconciled",
            "--behavior-equivalent",
            "--evidence-sha256",
            "a" * 64,
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proof.returncode == 0, proof.stderr
    assert json.loads(proof.stdout)["computed_status"] == "recorded"

    retention = subprocess.run(
        [
            sys.executable,
            str(script),
            "retention",
            "--db",
            str(database),
            "--raw-days",
            "1",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert retention.returncode == 0, retention.stderr
    retained = json.loads(retention.stdout)
    assert retained["projection"]["digest"]
    post_retention = build_execution_facts_payload(database)
    assert post_retention["database"]["computed_status"] == "ready"
    assert (
        post_retention["database"]["raw_event_count"]
        == post_retention["database"]["projected_event_count"]
    )


def test_nonzero_exit_without_error_class_counts_as_error(tmp_path) -> None:
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    ledger.append(
        _event(
            "systemd_invocation",
            "systemd:failed",
            "ended",
            1_000,
            event_type=EventType.OUTCOME_OBSERVED,
            lifecycle_phase=LifecyclePhase.ENDED,
            status="exit_code",
            exit_code=17,
            workload_kind=WorkloadKind.SYSTEM_JOB,
            execution_surface=ExecutionSurface.SYSTEMD,
            trigger_kind=TriggerKind.SYSTEMD,
        )
    )
    rebuild_projections(database)

    payload = build_execution_facts_payload(database)

    assert payload["errors"]["observed"] == 1
    assert payload["errors"]["by_class"] == {"exit_code:17": 1}
    assert payload["errors"]["validity"] == "exact"


def test_error_rate_uses_complete_measurement_denominator(tmp_path) -> None:
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    ledger.append_batch(
        (
            _event(
                "fixture",
                "failed-status",
                "ended",
                1_000,
                event_type=EventType.OUTCOME_OBSERVED,
                lifecycle_phase=LifecyclePhase.ENDED,
                status="failed",
            ),
            _event(
                "fixture",
                "clean-exit",
                "ended",
                2_000,
                event_type=EventType.OUTCOME_OBSERVED,
                lifecycle_phase=LifecyclePhase.ENDED,
                exit_code=0,
            ),
            _event(
                "fixture",
                "unmeasured",
                "ended",
                3_000,
                event_type=EventType.OUTCOME_OBSERVED,
                lifecycle_phase=LifecyclePhase.ENDED,
            ),
        )
    )
    rebuild_projections(database)

    errors = build_execution_facts_payload(database)["errors"]

    assert errors["observed"] == 1
    assert errors["eligible"] == 3
    assert errors["measured"] == 2
    assert errors["validity"] == "unknown"
    assert errors["unknown_reason"] == "incomplete_error_instrumentation"
    assert errors["by_class"] == {"status:failed": 1}


def test_zero_exit_is_exact_negative_error_observation(tmp_path) -> None:
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    ledger.append(
        _event(
            "fixture",
            "clean",
            "ended",
            1_000,
            event_type=EventType.OUTCOME_OBSERVED,
            lifecycle_phase=LifecyclePhase.ENDED,
            exit_code=0,
        )
    )
    rebuild_projections(database)

    errors = build_execution_facts_payload(database)["errors"]

    assert errors["observed"] == 0
    assert errors["eligible"] == 1
    assert errors["measured"] == 1
    assert errors["validity"] == "exact"


def test_data_trust_excludes_unknown_and_not_applicable_events(tmp_path) -> None:
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    ledger.append_batch(
        tuple(
            _event(
                "fixture",
                f"run-{validity.value}",
                validity.value,
                ordinal,
                validity=validity,
            )
            for ordinal, validity in enumerate(
                (
                    Validity.EXACT,
                    Validity.UNKNOWN,
                    Validity.NOT_APPLICABLE,
                ),
                start=1,
            )
        )
    )
    rebuild_projections(database)

    trust = build_execution_facts_payload(database)["p0"]["data_trust"]

    assert trust["observed"] == 1
    assert trust["eligible"] == 3
    assert trust["percent"] == "33.33"
    assert trust["validity"] == "exact"


def test_landing_and_deployment_preserve_source_validity(tmp_path) -> None:
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    ledger.append_batch(
        (
            _event(
                "kanban_timeline",
                "task_run:validity",
                "ended",
                1_000,
                event_type=EventType.OUTCOME_OBSERVED,
                lifecycle_phase=LifecyclePhase.ENDED,
                status="done",
            ),
            _event(
                "kanban_timeline",
                "task_run:validity",
                "landed",
                2_000,
                lifecycle_phase=LifecyclePhase.LANDED,
                validity=Validity.LOWER_BOUND,
            ),
            _event(
                "kanban_timeline",
                "task_run:validity",
                "deployed",
                3_000,
                lifecycle_phase=LifecyclePhase.DEPLOYED,
                validity=Validity.DERIVED,
            ),
        )
    )
    rebuild_projections(database)

    payload = build_execution_facts_payload(database)

    assert payload["landing"]["validity"] == "lower_bound"
    assert payload["deployment"]["validity"] == "derived"


def test_cost_totals_stay_null_without_population_and_for_mixed_currency(
    tmp_path,
) -> None:
    empty_database = tmp_path / "empty-costs.db"
    empty_ledger = ExecutionFactsLedger(empty_database)
    empty_ledger.initialize()
    empty_ledger.append(
        _event(
            "usage_facts",
            "usage:no-cost",
            "usage",
            1_000,
            event_type=EventType.USAGE_OBSERVED,
            attributes={"input_tokens": 1, "total_tokens": 1},
        )
    )
    rebuild_projections(empty_database)
    empty_costs = build_execution_facts_payload(empty_database)[
        "usage"
    ]["costs"]
    assert empty_costs["api_equivalent_total"] is None
    assert empty_costs["allocated_subscription_total"] is None
    assert empty_costs["marginal_total"] is None
    assert empty_costs["metered_total"] is None

    mixed_database = tmp_path / "mixed-costs.db"
    mixed_ledger = ExecutionFactsLedger(mixed_database)
    mixed_ledger.initialize()
    mixed_ledger.append_batch(
        tuple(
            _event(
                "usage_facts",
                f"usage:{currency.lower()}",
                "cost",
                at,
                event_type=EventType.COST_OBSERVED,
                validity=Validity.DERIVED,
                attributes={
                    "api_equivalent_cost": "1",
                    "currency": currency,
                    "pricing_version": "fixture-v1",
                },
            )
            for at, currency in ((1_000, "USD"), (2_000, "EUR"))
        )
    )
    rebuild_projections(mixed_database)
    mixed_costs = build_execution_facts_payload(mixed_database)[
        "usage"
    ]["costs"]

    assert mixed_costs["computed_status"] == "unknown"
    assert mixed_costs["fx_status"] == "unknown"
    assert mixed_costs["api_equivalent_population"] == 2
    assert mixed_costs["api_equivalent_total"] is None

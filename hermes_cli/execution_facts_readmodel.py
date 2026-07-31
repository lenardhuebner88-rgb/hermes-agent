"""Read-only, versioned P0 read model for universal execution facts."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from hermes_cli.execution_facts_contract import CONTRACT_VERSION, contract_schema
from hermes_cli.execution_facts_ledger import (
    execution_facts_db_path,
)
from hermes_cli.execution_facts_projection import PROJECTION_VERSION

READMODEL_VERSION = "execution-facts-read.v1"
DEFAULT_SOURCE_FRESHNESS_MS = 24 * 60 * 60 * 1000
SHADOW_GATE_VERSION = "execution-facts-shadow-gates.v1"
SHADOW_MIN_EVENTS = 20
SHADOW_MAX_QUEUE_LAG_MS = 1_000
SHADOW_MAX_CAPTURE_P95_US = 1_000
_CENSUS_SOURCE = "execution_facts_census"
_NON_WORK_SOURCES = {
    "execution_facts_collector",
    _CENSUS_SOURCE,
    "execution_facts_validation",
    "observability_sentinel",
}
_UNIVERSAL_EXECUTION_SOURCES = (
    "kanban_timeline",
    "hermes_cron",
    "loop_ledger",
    "tmux_reconciliation",
    "systemd_invocation",
    "crontab_invocation",
)
_SCHEDULED_EXECUTION_SOURCES = (
    "hermes_cron",
    "systemd_invocation",
    "crontab_invocation",
)

_PHASE_PAIRS = (
    ("schedule_to_queue", "scheduled", "queued"),
    ("queue_to_claim", "queued", "claimed"),
    ("claim_to_spawn", "claimed", "spawn_started"),
    ("spawn_to_process", "spawn_started", "process_started"),
    ("claim_to_process", "claimed", "process_started"),
    ("process_to_first_request", "process_started", "first_request"),
    ("request_to_first_token", "first_request", "first_token"),
    ("process_to_end", "process_started", "ended"),
    ("review_duration", "review_started", "reviewed"),
    ("integration_to_land", "integration_started", "landed"),
    ("land_to_deploy", "landed", "deployed"),
)
_VALIDITY_ORDER = {
    "exact": 5,
    "lower_bound": 4,
    "derived": 3,
    "unknown": 2,
    "not_applicable": 1,
}
_TEST_COUNTERS = (
    ("selected_tests", "selected"),
    ("executed_tests", "executed"),
    ("passed_tests", "passed"),
    ("failed_tests", "failed"),
    ("skipped_tests", "skipped"),
    ("timeout_lost_tests", "timeout_lost"),
)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _percent(observed: int, eligible: int | None) -> str | None:
    if eligible is None or eligible <= 0 or observed < 0 or observed > eligible:
        return None
    return format(
        (Decimal(observed) * Decimal(100) / Decimal(eligible)).quantize(
            Decimal("0.01")
        ),
        "f",
    )


def _metric(
    metric_id: str,
    *,
    observed: int | None,
    eligible: int | None,
    validity: str,
    unknown_reason: str | None = None,
    unit: str = "executions",
    value: int | str | None = None,
) -> dict[str, Any]:
    if (
        observed is not None
        and eligible is not None
        and observed > eligible
    ):
        validity = "unknown"
        unknown_reason = "observed_exceeds_eligible"
    computed = (
        "unknown"
        if validity == "unknown" or observed is None or eligible is None
        else "not_applicable"
        if validity == "not_applicable"
        else "not_applicable"
        if eligible == 0
        else "ready"
    )
    return {
        "metric_id": metric_id,
        "computed_status": computed,
        "validity": validity,
        "observed": observed,
        "eligible": eligible,
        "percent": (
            _percent(observed, eligible)
            if observed is not None
            else None
        ),
        "value": value,
        "unit": unit,
        "unknown_reason": unknown_reason if computed == "unknown" else None,
    }


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, ((len(ordered) * percentile + 99) // 100) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _worst_validity(*values: str) -> str:
    present = [value for value in values if value in _VALIDITY_ORDER]
    return (
        min(present, key=_VALIDITY_ORDER.__getitem__)
        if present
        else "unknown"
    )


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _source_groups(sources: set[str]) -> dict[str, bool]:
    return {
        "kanban": "kanban_timeline" in sources,
        "tmux": "tmux_reconciliation" in sources,
        "cron_or_scheduler": bool(
            sources
            & {
                "hermes_cron",
                "systemd_invocation",
                "crontab_invocation",
            }
        ),
        "loops": "loop_ledger" in sources,
    }


def _span_dimension_groups(
    rows: Iterable[Mapping[str, Any]],
    *,
    name_key: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        attributes = row["attributes"]
        key = (
            str(attributes.get(name_key) or "unknown"),
            str(attributes.get("agent_kind") or "unknown"),
            str(attributes.get("agent_provider") or "unknown"),
            str(attributes.get("agent_model") or "unknown"),
            str(row.get("workload_kind") or "unknown"),
            str(attributes.get("execution_phase") or "unknown"),
        )
        group = grouped.setdefault(
            key,
            {
                "spans": 0,
                "completed": 0,
                "errors": 0,
                "retries": 0,
                "timeouts": 0,
                "durations": [],
            },
        )
        group["spans"] += 1
        group["completed"] += int(row.get("ended_at_ms") is not None)
        group["errors"] += int(
            row.get("error_class") is not None
            or (
                row.get("exit_code") is not None
                and int(row["exit_code"]) != 0
            )
        )
        group["retries"] += int(row.get("retry_class") is not None)
        group["timeouts"] += int(bool(attributes.get("timeout", False)))
        if attributes.get("duration_ms") is not None:
            group["durations"].append(int(attributes["duration_ms"]))
    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        group = grouped[key]
        durations = group.pop("durations")
        result.append(
            {
                "key": {
                    name_key: key[0],
                    "agent_kind": key[1],
                    "agent_provider": key[2],
                    "agent_model": key[3],
                    "workload_kind": key[4],
                    "execution_phase": key[5],
                },
                **group,
                "duration_p50_ms": _percentile(durations, 50),
                "duration_p95_ms": _percentile(durations, 95),
            }
        )
    return result


def _test_dimension_groups(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        attributes = row["attributes"]
        key = (
            str(attributes.get("test_suite") or "unknown"),
            str(attributes.get("test_file") or "unknown"),
            str(attributes.get("agent_kind") or "unknown"),
            str(attributes.get("agent_provider") or "unknown"),
            str(attributes.get("agent_model") or "unknown"),
            str(row.get("workload_kind") or "unknown"),
            str(attributes.get("execution_phase") or "unknown"),
        )
        group = grouped.setdefault(
            key,
            {
                "spans": 0,
                "completed": 0,
                "errors": 0,
                "retries": 0,
                "timeouts": 0,
                "durations": [],
                "counter_rows": [],
                "gate_results": Counter(),
                "flake_classes": Counter(),
            },
        )
        group["spans"] += 1
        group["completed"] += int(row.get("ended_at_ms") is not None)
        group["errors"] += int(
            row.get("error_class") is not None
            or (
                row.get("exit_code") is not None
                and int(row["exit_code"]) != 0
            )
        )
        group["retries"] += int(row.get("retry_class") is not None)
        group["timeouts"] += int(bool(attributes.get("timeout", False)))
        group["counter_rows"].append(row)
        if attributes.get("duration_ms") is not None:
            group["durations"].append(int(attributes["duration_ms"]))
        gate_key = (
            f"{attributes.get('gate_type', 'unknown')}:"
            f"{row.get('status') or 'unknown'}"
        )
        group["gate_results"][gate_key] += 1
        if attributes.get("flake_class") is not None:
            group["flake_classes"][str(attributes["flake_class"])] += 1
    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        group = grouped[key]
        durations = group.pop("durations")
        counter_summary = _test_counter_summary(group.pop("counter_rows"))
        gate_results = group.pop("gate_results")
        flake_classes = group.pop("flake_classes")
        result.append(
            {
                "key": {
                    "test_suite": key[0],
                    "test_file": key[1],
                    "agent_kind": key[2],
                    "agent_provider": key[3],
                    "agent_model": key[4],
                    "workload_kind": key[5],
                    "execution_phase": key[6],
                },
                **group,
                **counter_summary["values"],
                "counter_observed_lower_bounds": counter_summary[
                    "observed_lower_bounds"
                ],
                "counter_coverage": counter_summary["coverage"],
                "gate_results": dict(sorted(gate_results.items())),
                "flake_classes": dict(sorted(flake_classes.items())),
                "duration_p50_ms": _percentile(durations, 50),
                "duration_p95_ms": _percentile(durations, 95),
            }
        )
    return result


def _test_counter_summary(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Preserve missing test counters as unknown, never synthetic zero."""
    population = list(rows)
    values: dict[str, Any] = {}
    observed_lower_bounds: dict[str, int] = {}
    coverage: dict[str, Any] = {}
    for source_key, target_key in _TEST_COUNTERS:
        observed = [
            row
            for row in population
            if row["attributes"].get(source_key) is not None
        ]
        observed_total = sum(
            int(row["attributes"][source_key]) for row in observed
        )
        complete = bool(population) and len(observed) == len(population)
        validity = (
            _worst_validity(
                *(str(row.get("validity") or "unknown") for row in observed)
            )
            if complete
            else "unknown"
        )
        values[target_key] = observed_total if complete else None
        observed_lower_bounds[target_key] = observed_total
        coverage[target_key] = {
            "computed_status": (
                "ready"
                if complete and validity != "unknown"
                else "partial"
                if observed
                else "unknown"
            ),
            "observed_rows": len(observed),
            "denominator_rows": len(population),
            "validity": validity,
        }
    return {
        "values": values,
        "observed_lower_bounds": observed_lower_bounds,
        "coverage": coverage,
    }


def _efficiency_rows(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    definitions = {
        "accepted": lambda row: str(row.get("status") or "") in {
            "accepted",
            "done",
            "ok",
            "succeeded",
        },
        "reviewed": lambda row: "reviewed" in row["lifecycle"],
        "landed": lambda row: "landed" in row["lifecycle"],
    }
    result: dict[str, dict[str, Any]] = {}
    for name, predicate in definitions.items():
        population = [row for row in rows if predicate(row)]
        costs: list[Decimal] = []
        durations: list[int] = []
        for row in population:
            attributes = row["attributes"]
            cost = attributes.get("allocated_subscription_cost")
            if cost is None:
                cost = attributes.get("metered_cost")
            if cost is not None:
                costs.append(Decimal(str(cost)))
            duration = attributes.get("duration_ms")
            if duration is None:
                lifecycle = row["lifecycle"]
                start = lifecycle.get("process_started")
                end = lifecycle.get("ended")
                if isinstance(start, dict) and isinstance(end, dict):
                    measured = (
                        int(end["observed_at_ms"])
                        - int(start["observed_at_ms"])
                    )
                    duration = measured if measured >= 0 else None
            if duration is not None:
                durations.append(int(duration))
        count = len(population)
        result[name] = {
            "result_count": count,
            "computed_status": (
                "ready"
                if count and len(costs) == count and len(durations) == count
                else "unknown"
            ),
            "cost_population": len(costs),
            "duration_population": len(durations),
            "total_cost": (
                format(sum(costs, Decimal(0)), "f")
                if len(costs) == count and count
                else None
            ),
            "cost_per_result": (
                format(sum(costs, Decimal(0)) / Decimal(count), "f")
                if len(costs) == count and count
                else None
            ),
            "work_ms": (
                sum(durations)
                if len(durations) == count and count
                else None
            ),
            "work_ms_per_result": (
                sum(durations) // count
                if len(durations) == count and count
                else None
            ),
            "unknown_reason": (
                None
                if count and len(costs) == count and len(durations) == count
                else "missing_result_cost_or_duration_population"
            ),
        }
    return result


def _unavailable_payload(path: Path, reason: str, now_ms: int) -> dict[str, Any]:
    unknown = lambda metric_id: _metric(  # noqa: E731 - compact fixed payload
        metric_id,
        observed=None,
        eligible=None,
        validity="unknown",
        unknown_reason=reason,
    )
    return {
        "schema_version": READMODEL_VERSION,
        "contract_version": CONTRACT_VERSION,
        "projection_version": PROJECTION_VERSION,
        "generated_at_ms": now_ms,
        "database": {
            "path": str(path),
            "computed_status": "unavailable",
            "reason": reason,
        },
        "p0": {
            "run_identity": unknown("run_identity_adoption"),
            "kanban": unknown("kanban_lifecycle_coverage"),
            "terminal_identity": unknown("terminal_identity_adoption"),
            "cron_systemd": unknown("scheduled_execution_adoption"),
            "loops": unknown("loop_execution_adoption"),
            "tokens_models_costs": unknown("usage_cost_coverage"),
            "data_trust": unknown("validated_fact_coverage"),
        },
        "sources": [],
        "source_census": {
            "schema_version": "execution-facts-source-census.v1",
            "sources": [],
        },
        "lifecycle_latencies": [],
        "outcomes": {},
        "retries": unknown("retry_rate"),
        "errors": unknown("error_rate"),
        "landing": unknown("landed_execution_rate"),
        "deployment": unknown("deployed_execution_rate"),
        "usage": {
            "tokens": {"computed_status": "unknown", "reason": reason},
            "costs": {"computed_status": "unknown", "reason": reason},
        },
        "tools": {"computed_status": "unknown", "reason": reason},
        "tests": {"computed_status": "unknown", "reason": reason},
        "activity_spans": {
            "computed_status": "unknown",
            "reason": reason,
        },
        "collector": {"computed_status": "unknown", "reason": reason},
        "shadow_gates": {
            "gate_version": SHADOW_GATE_VERSION,
            "activation_effect": "none",
            "sources": [],
        },
        "efficiency": {
            stage: {
                "computed_status": "unknown",
                "unknown_reason": reason,
            }
            for stage in ("accepted", "reviewed", "landed")
        },
        "alert_gate": {
            "computed_status": "unknown",
            "reasons": [reason],
        },
    }


def build_execution_facts_payload(
    path: Path | str | None = None,
    *,
    now: datetime | None = None,
    source_freshness_ms: int = DEFAULT_SOURCE_FRESHNESS_MS,
) -> dict[str, Any]:
    """Build the dashboard/API contract without creating or mutating a DB."""
    database_path = execution_facts_db_path(path)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    now_ms = int(current.astimezone(UTC).timestamp() * 1000)
    if not database_path.is_file():
        return _unavailable_payload(database_path, "database_missing", now_ms)

    try:
        connection = _read_only_connection(database_path)
    except sqlite3.Error:
        return _unavailable_payload(database_path, "database_unreadable", now_ms)
    try:
        required = {
            "execution_events",
            "execution_projection",
            "execution_span_projection",
            "execution_source_projection",
            "execution_projection_meta",
            "terminal_run_projection",
        }
        missing = sorted(
            table for table in required if not _table_exists(connection, table)
        )
        if missing:
            return _unavailable_payload(
                database_path,
                f"projection_missing:{','.join(missing)}",
                now_ms,
            )

        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM execution_projection_meta"
            )
        }
        raw_event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM execution_events"
            ).fetchone()[0]
        )
        projected_event_count = int(meta.get("raw_event_count", "-1"))
        projection_current = raw_event_count == projected_event_count

        source_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source, event_count, execution_count, exact_count,
                       lower_bound_count, derived_count, unknown_count,
                       not_applicable_count, first_observed_at_ms,
                       last_observed_at_ms, max_queue_lag_ms,
                       max_reconciliation_lag_ms
                  FROM execution_source_projection
                 ORDER BY source
                """
            )
        ]
        sources: list[dict[str, Any]] = []
        for row in source_rows:
            freshness_ms = max(0, now_ms - int(row["last_observed_at_ms"]))
            sources.append(
                {
                    **row,
                    "freshness_ms": freshness_ms,
                    "freshness_status": (
                        "fresh"
                        if freshness_ms <= source_freshness_ms
                        else "stale"
                    ),
                }
            )

        execution_rows = [
            {
                **dict(row),
                "lifecycle": _json_object(row["lifecycle_json"]),
                "attributes": _json_object(row["attributes_json"]),
                "attribute_validity": _json_object(
                    row["attribute_validity_json"]
                ),
                "field_validity": _json_object(
                    row["field_validity_json"]
                ),
            }
            for row in connection.execute(
                """
                SELECT *
                  FROM execution_projection
                 ORDER BY first_observed_at_ms, execution_id
                """
            )
        ]
        span_rows = [
            {
                **dict(row),
                "attributes": _json_object(row["attributes_json"]),
            }
            for row in connection.execute(
                """
                SELECT *
                  FROM execution_span_projection
                 ORDER BY COALESCE(started_at_ms, ended_at_ms), span_id
                """
            )
        ]
        work_rows = [
            row
            for row in execution_rows
            if row["source"] not in _NON_WORK_SOURCES
        ]
        collector_rows = [
            row
            for row in execution_rows
            if row["source"] == "execution_facts_collector"
        ]
        validation_rows = [
            row
            for row in execution_rows
            if row["source"] == "execution_facts_validation"
        ]
        census_rows = [
            row
            for row in execution_rows
            if row["source"] == _CENSUS_SOURCE
        ]
        census_by_source: dict[str, dict[str, Any]] = {}
        for row in census_rows:
            target = row["attributes"].get("validated_source")
            if target is None:
                continue
            current_census = census_by_source.get(str(target))
            if (
                current_census is None
                or int(row["last_observed_at_ms"])
                > int(current_census["last_observed_at_ms"])
            ):
                census_by_source[str(target)] = row
        source_census = [
            {
                "source": source,
                "observed_at_ms": int(row["last_observed_at_ms"]),
                "validity": str(
                    row["attribute_validity"].get(
                        "eligible",
                        "unknown",
                    )
                ),
                **{
                    key: row["attributes"].get(key)
                    for key in (
                        "eligible",
                        "observed",
                        "identity_observed",
                        "metric_observed",
                        "eligibility_rule",
                        "source_store_count",
                        "source_read_errors",
                        "window_start_ms",
                        "window_end_ms",
                        "source_contract_version",
                        "writer_p50_us",
                        "writer_p95_us",
                        "dedupe_reconciled",
                        "behavior_equivalent",
                        "behavior_proof",
                        "missing_links",
                        "orphans",
                    )
                },
            }
            for source, row in sorted(census_by_source.items())
        ]
        source_names = {str(row["source"]) for row in source_rows}
        groups = _source_groups(source_names)

        lifecycle_latencies: list[dict[str, Any]] = []
        for metric_id, start_phase, end_phase in _PHASE_PAIRS:
            values: list[int] = []
            validities: list[str] = []
            eligible = 0
            for row in work_rows:
                lifecycle = row["lifecycle"]
                start = lifecycle.get(start_phase)
                if not isinstance(start, dict):
                    continue
                eligible += 1
                end = lifecycle.get(end_phase)
                if not isinstance(end, dict):
                    continue
                delta = int(end["observed_at_ms"]) - int(start["observed_at_ms"])
                if delta < 0:
                    continue
                values.append(delta)
                validities.append(
                    _worst_validity(
                        str(start.get("validity")),
                        str(end.get("validity")),
                    )
                )
            lifecycle_latencies.append(
                {
                    **_metric(
                        metric_id,
                        observed=len(values),
                        eligible=eligible,
                        validity=(
                            _worst_validity(*validities)
                            if validities
                            else "unknown"
                        ),
                        unknown_reason=(
                            "no_execution_has_both_required_phases"
                            if eligible > 0 and not values
                            else None
                        ),
                        unit="milliseconds",
                    ),
                    "p50_ms": _percentile(values, 50),
                    "p95_ms": _percentile(values, 95),
                    "max_ms": max(values) if values else None,
                }
            )

        ended_rows = [
            row for row in work_rows if "ended" in row["lifecycle"]
        ]
        statuses = Counter(
            str(row["status"])
            for row in ended_rows
            if row["status"] is not None
        )
        ended = len(ended_rows)
        outcomes_observed = sum(
            row["status"] is not None for row in ended_rows
        )
        outcome_validities = [
            str(row["field_validity"].get("status", "unknown"))
            for row in ended_rows
            if row["status"] is not None
        ]
        error_statuses = {
            "blocked",
            "crashed",
            "failed",
            "failure",
            "gave_up",
            "spawn_failed",
            "timed_out",
            "timeout",
        }
        success_statuses = {
            "clean",
            "completed",
            "done",
            "ok",
            "succeeded",
            "success",
        }
        error_observations: list[
            tuple[bool | None, str, str | None]
        ] = []
        for row in ended_rows:
            positive_validities: list[str] = []
            negative_validities: list[str] = []
            error_label = None
            if row["error_class"] is not None:
                error_label = str(row["error_class"])
                positive_validities.append(
                    str(
                        row["field_validity"].get(
                            "error_class", "unknown"
                        )
                    )
                )
            if row["exit_code"] is not None:
                exit_validity = str(
                    row["field_validity"].get("exit_code", "unknown")
                )
                if int(row["exit_code"]) != 0:
                    error_label = error_label or (
                        f"exit_code:{int(row['exit_code'])}"
                    )
                    positive_validities.append(exit_validity)
                else:
                    negative_validities.append(exit_validity)
            normalized_status = str(row["status"] or "").lower()
            status_validity = str(
                row["field_validity"].get("status", "unknown")
            )
            if normalized_status in error_statuses:
                error_label = error_label or f"status:{normalized_status}"
                positive_validities.append(status_validity)
            elif normalized_status in success_statuses:
                negative_validities.append(status_validity)
            if positive_validities:
                error_observations.append(
                    (
                        True,
                        _worst_validity(*positive_validities),
                        error_label,
                    )
                )
            elif negative_validities:
                error_observations.append(
                    (
                        False,
                        _worst_validity(*negative_validities),
                        None,
                    )
                )
            else:
                error_observations.append((None, "unknown", None))
        errors = sum(
            is_error is True
            for is_error, _validity, _label in error_observations
        )
        error_measured = sum(
            is_error is not None
            for is_error, _validity, _label in error_observations
        )
        error_validities = [
            validity
            for _is_error, validity, _label in error_observations
        ]
        retry_eligible_rows = [
            row
            for row in work_rows
            if row["attributes"].get("retry_instrumented") is True
            and row["attribute_validity"].get("retry_instrumented") == "exact"
        ]
        retries = sum(
            row["retry_class"] is not None or row["trigger_kind"] == "retry"
            for row in retry_eligible_rows
        )
        retry_validities = [
            _worst_validity(
                str(
                    row["attribute_validity"].get(
                        "retry_instrumented", "unknown"
                    )
                ),
                str(
                    row["field_validity"].get(
                        "retry_class",
                        row["attribute_validity"].get(
                            "retry_instrumented", "unknown"
                        ),
                    )
                ),
            )
            for row in retry_eligible_rows
        ]
        landed = sum("landed" in row["lifecycle"] for row in work_rows)
        deployed = sum("deployed" in row["lifecycle"] for row in work_rows)
        landed_validities = [
            str(row["lifecycle"]["landed"].get("validity", "unknown"))
            for row in work_rows
            if isinstance(row["lifecycle"].get("landed"), dict)
        ]
        deployed_validities = [
            str(row["lifecycle"]["deployed"].get("validity", "unknown"))
            for row in work_rows
            if isinstance(row["lifecycle"].get("deployed"), dict)
        ]

        token_executions = 0
        model_executions = 0
        usage_eligible_executions = 0
        total_tokens = 0
        cost_executions = 0
        api_equivalent = Decimal(0)
        allocated_subscription = Decimal(0)
        marginal = Decimal(0)
        metered = Decimal(0)
        pricing_versioned = 0
        api_equivalent_executions = 0
        subscription_executions = 0
        allocated_subscription_executions = 0
        marginal_executions = 0
        metered_executions = 0
        allocation_fee_versioned = 0
        currencies: set[str] = set()
        pricing_versions: set[str] = set()
        fee_versions: set[str] = set()
        allocation_formulas: set[str] = set()
        allocation_population_sizes: set[int] = set()
        token_validities: list[str] = []
        cost_validities: list[str] = []
        for row in work_rows:
            attributes = row["attributes"]
            attribute_validity = row["attribute_validity"]
            if any(
                attributes.get(key) is not None
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                    "agent_model",
                    "agent_provider",
                    "billing_mode",
                    "api_equivalent_cost",
                    "allocated_subscription_cost",
                    "marginal_cost",
                    "metered_cost",
                )
            ):
                usage_eligible_executions += 1
            if attributes.get("total_tokens") is not None:
                token_executions += 1
                total_tokens += int(attributes["total_tokens"])
                token_validities.append(
                    str(attribute_validity.get("total_tokens", "unknown"))
                )
            if attributes.get("agent_model") is not None:
                model_executions += 1
            if attributes.get("billing_mode") == "subscription_included":
                subscription_executions += 1
            cost_present = False
            for key, accumulator_name in (
                ("api_equivalent_cost", "api_equivalent"),
                ("allocated_subscription_cost", "allocated_subscription"),
                ("marginal_cost", "marginal"),
                ("metered_cost", "metered"),
            ):
                value = attributes.get(key)
                if value is None:
                    continue
                cost_present = True
                amount = Decimal(str(value))
                if accumulator_name == "api_equivalent":
                    api_equivalent += amount
                    api_equivalent_executions += 1
                elif accumulator_name == "allocated_subscription":
                    allocated_subscription += amount
                    allocated_subscription_executions += 1
                elif accumulator_name == "marginal":
                    marginal += amount
                    marginal_executions += 1
                else:
                    metered += amount
                    metered_executions += 1
            if cost_present:
                cost_executions += 1
                cost_validities.append(
                    _worst_validity(
                        *(
                            str(attribute_validity.get(key, "unknown"))
                            for key in (
                                "api_equivalent_cost",
                                "allocated_subscription_cost",
                                "marginal_cost",
                                "metered_cost",
                            )
                            if attributes.get(key) is not None
                        )
                    )
                )
                if attributes.get("pricing_version"):
                    pricing_versioned += 1
                    pricing_versions.add(str(attributes["pricing_version"]))
                if attributes.get("fee_version"):
                    allocation_fee_versioned += 1
                    fee_versions.add(str(attributes["fee_version"]))
                if attributes.get("allocation_formula"):
                    allocation_formulas.add(
                        str(attributes["allocation_formula"])
                    )
                if attributes.get("population_size") is not None:
                    allocation_population_sizes.add(
                        int(attributes["population_size"])
                    )
                if attributes.get("currency"):
                    currencies.add(str(attributes["currency"]))
        currency_compatible = len(currencies) <= 1
        costs_ready = (
            bool(cost_executions)
            and currency_compatible
            and pricing_versioned == cost_executions
            and (
                subscription_executions == 0
                or (
                    api_equivalent_executions >= subscription_executions
                    and allocated_subscription_executions
                    >= subscription_executions
                    and allocation_fee_versioned
                    >= subscription_executions
                )
            )
        )

        terminal_runs = int(
            connection.execute(
                "SELECT COUNT(*) FROM terminal_run_projection"
            ).fetchone()[0]
        )
        exact_events = sum(int(row["exact_count"]) for row in source_rows)
        validated_events = sum(
            int(row[key])
            for row in source_rows
            for key in (
                "exact_count",
                "lower_bound_count",
                "derived_count",
            )
        )
        kanban_rows = [
            row for row in work_rows if row["source"] == "kanban_timeline"
        ]
        kanban_validities = [
            str(phase["validity"])
            for row in kanban_rows
            for phase in row["lifecycle"].values()
            if isinstance(phase, dict) and phase.get("validity") is not None
        ]

        def census_metric(
            metric_id: str,
            *,
            sources: Iterable[str],
            observed_key: str,
            missing_reason: str,
            value: int | None = None,
        ) -> dict[str, Any]:
            requested = tuple(sources)
            rows = [census_by_source.get(source) for source in requested]
            if any(row is None for row in rows):
                return _metric(
                    metric_id,
                    observed=None,
                    eligible=None,
                    validity="unknown",
                    unknown_reason=missing_reason,
                    value=value,
                )
            present = [row for row in rows if row is not None]
            eligible_values = [
                row["attributes"].get("eligible") for row in present
            ]
            observed_values = [
                row["attributes"].get(observed_key) for row in present
            ]
            if not all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in (*eligible_values, *observed_values)
            ):
                return _metric(
                    metric_id,
                    observed=None,
                    eligible=None,
                    validity="unknown",
                    unknown_reason="source_census_denominator_unknown",
                    value=value,
                )
            validities = [
                str(
                    row["attribute_validity"].get(
                        key,
                        "unknown",
                    )
                )
                for row in present
                for key in ("eligible", observed_key)
            ]
            observed = sum(int(item) for item in observed_values)
            eligible = sum(int(item) for item in eligible_values)
            return _metric(
                metric_id,
                observed=observed,
                eligible=eligible,
                validity=_worst_validity(*validities),
                unknown_reason=None,
                value=observed if value is None else value,
            )

        run_identity_metric = census_metric(
            "run_identity_adoption",
            sources=_UNIVERSAL_EXECUTION_SOURCES,
            observed_key="identity_observed",
            missing_reason=(
                "universal_external_execution_denominator_unavailable"
            ),
        )
        kanban_census_metric = census_metric(
            "kanban_lifecycle_coverage",
            sources=("kanban_timeline",),
            observed_key="metric_observed",
            missing_reason="kanban_source_census_absent",
        )
        terminal_census_metric = census_metric(
            "terminal_identity_adoption",
            sources=("tmux_reconciliation",),
            observed_key="identity_observed",
            missing_reason="all_pane_generation_denominator_unavailable",
        )
        scheduled_census_metric = census_metric(
            "scheduled_execution_adoption",
            sources=_SCHEDULED_EXECUTION_SOURCES,
            observed_key="identity_observed",
            missing_reason="scheduler_invocation_denominator_unavailable",
        )
        loop_census_metric = census_metric(
            "loop_execution_adoption",
            sources=("loop_ledger",),
            observed_key="identity_observed",
            missing_reason="loop_invocation_denominator_unavailable",
        )
        scheduled_runs = sum(
            row["source"] in _SCHEDULED_EXECUTION_SOURCES
            for row in work_rows
        )
        loop_runs = sum(
            row["source"] == "loop_ledger" for row in work_rows
        )

        p0 = {
            "run_identity": run_identity_metric,
            "kanban": (
                kanban_census_metric
                if "kanban_timeline" in census_by_source
                else _metric(
                    "kanban_lifecycle_coverage",
                    observed=sum(
                        bool(row["lifecycle"]) for row in kanban_rows
                    ),
                    eligible=len(kanban_rows),
                    validity=(
                        _worst_validity(*kanban_validities)
                        if kanban_validities
                        else "unknown"
                    ),
                    unknown_reason=(
                        None
                        if groups["kanban"]
                        else "kanban_source_absent"
                    ),
                )
            ),
            "terminal_identity": (
                terminal_census_metric
                if "tmux_reconciliation" in census_by_source
                else _metric(
                    "terminal_identity_adoption",
                    observed=terminal_runs,
                    eligible=None,
                    validity="unknown",
                    unknown_reason=(
                        "all_pane_generation_denominator_unavailable"
                    ),
                    value=terminal_runs,
                )
            ),
            "cron_systemd": (
                scheduled_census_metric
                if all(
                    source in census_by_source
                    for source in _SCHEDULED_EXECUTION_SOURCES
                )
                else _metric(
                    "scheduled_execution_adoption",
                    observed=scheduled_runs,
                    eligible=None,
                    validity="unknown",
                    unknown_reason=(
                        "scheduler_invocation_denominator_unavailable"
                    ),
                    value=scheduled_runs,
                )
            ),
            "loops": (
                loop_census_metric
                if "loop_ledger" in census_by_source
                else _metric(
                    "loop_execution_adoption",
                    observed=loop_runs,
                    eligible=None,
                    validity="unknown",
                    unknown_reason=(
                        "loop_invocation_denominator_unavailable"
                    ),
                    value=loop_runs,
                )
            ),
            "tokens_models_costs": _metric(
                "usage_cost_coverage",
                observed=cost_executions,
                eligible=usage_eligible_executions,
                validity=(
                    _worst_validity(*cost_validities)
                    if usage_eligible_executions and costs_ready
                    else "unknown"
                ),
                unknown_reason=(
                    "missing_price_or_subscription_allocation"
                    if usage_eligible_executions and not costs_ready
                    else None
                ),
            ),
            "data_trust": _metric(
                "validated_fact_coverage",
                observed=validated_events,
                eligible=raw_event_count,
                validity=(
                    "exact"
                    if projection_current
                    else "unknown"
                ),
                unknown_reason=(
                    None
                    if projection_current
                    else "projection_stale_or_unvalidated_rows"
                ),
            ),
        }

        tool_spans = [row for row in span_rows if row["span_kind"] == "tool"]
        test_spans = [row for row in span_rows if row["span_kind"] == "test"]
        operator_spans = [
            row for row in span_rows if row["span_kind"] == "operator"
        ]
        agent_spans = [
            row
            for row in span_rows
            if row["span_kind"] in {"agent", "subagent"}
        ]
        latest_collector = (
            max(collector_rows, key=lambda row: row["last_observed_at_ms"])
            if collector_rows
            else None
        )
        collector_attributes = (
            latest_collector["attributes"]
            if latest_collector is not None
            else {}
        )
        collector_attribute_validity = (
            latest_collector["attribute_validity"]
            if latest_collector is not None
            else {}
        )
        collector_field_validity = (
            latest_collector["field_validity"]
            if latest_collector is not None
            else {}
        )
        collector_submitted = collector_attributes.get("submitted_events")
        collector_accepted = collector_attributes.get("accepted_events")
        collector_health = {
            "computed_status": (
                "ready" if latest_collector is not None else "unknown"
            ),
            "mode": (
                latest_collector["status"]
                if latest_collector is not None
                else None
            ),
            "submitted": collector_submitted,
            "accepted": collector_accepted,
            "adoption_percent": (
                _percent(int(collector_accepted), int(collector_submitted))
                if collector_submitted is not None
                and collector_accepted is not None
                else None
            ),
            "dropped": collector_attributes.get("dropped"),
            "write_errors": collector_attributes.get("write_errors"),
            "deduped": collector_attributes.get("deduped"),
            "written": collector_attributes.get("written_events"),
            "queue_depth": collector_attributes.get("queue_depth"),
            "queue_capacity": collector_attributes.get("queue_capacity"),
            "last_observed_at_ms": (
                latest_collector["last_observed_at_ms"]
                if latest_collector is not None
                else None
            ),
            "freshness_ms": (
                max(
                    0,
                    now_ms - int(latest_collector["last_observed_at_ms"]),
                )
                if latest_collector is not None
                else None
            ),
            "unknown_reason": (
                None
                if latest_collector is not None
                else "collector_health_fact_absent"
            ),
        }
        validation_by_source: dict[str, dict[str, Any]] = {}
        for row in validation_rows:
            target = row["attributes"].get("validated_source")
            if target is None:
                continue
            current_validation = validation_by_source.get(str(target))
            if (
                current_validation is None
                or int(row["last_observed_at_ms"])
                > int(current_validation["last_observed_at_ms"])
            ):
                validation_by_source[str(target)] = row
        shadow_gates: list[dict[str, Any]] = []
        for source in sources:
            if source["source"] in _NON_WORK_SOURCES:
                continue
            validation = validation_by_source.get(str(source["source"]))
            validation_attributes = (
                validation["attributes"] if validation is not None else {}
            )
            census = census_by_source.get(str(source["source"]))
            census_attributes = (
                census["attributes"] if census is not None else {}
            )
            census_validity = (
                census["attribute_validity"] if census is not None else {}
            )
            validation_validity = (
                validation["attribute_validity"]
                if validation is not None
                else {}
            )
            validation_fresh = (
                validation is not None
                and max(
                    0,
                    now_ms - int(validation["last_observed_at_ms"]),
                )
                <= source_freshness_ms
            )

            def exact_validation_flag(name: str) -> bool:
                return (
                    validation_attributes.get(name) is True
                    and validation_validity.get(name) == "exact"
                )

            checks = {
                "minimum_sample": int(source["event_count"])
                >= SHADOW_MIN_EVENTS,
                "freshness": source["freshness_status"] == "fresh",
                "validation_sample": (
                    validation_validity.get("sample_size") == "exact"
                    and isinstance(
                        validation_attributes.get("sample_size"), int
                    )
                    and int(validation_attributes["sample_size"])
                    >= SHADOW_MIN_EVENTS
                ),
                "identity": (
                    int(source["execution_count"]) > 0
                    and exact_validation_flag("identity_coverage_passed")
                ),
                "metric_coverage": exact_validation_flag(
                    "metric_coverage_passed"
                ),
                "validity": int(source["unknown_count"]) == 0,
                "queue_lag": int(source["max_queue_lag_ms"])
                <= SHADOW_MAX_QUEUE_LAG_MS,
                "reconciliation_lag": int(
                    source["max_reconciliation_lag_ms"]
                )
                <= source_freshness_ms,
                "performance_budget": (
                    validation_validity.get("capture_p95_us") == "exact"
                    and isinstance(
                        validation_attributes.get("capture_p95_us"), int
                    )
                    and int(validation_attributes["capture_p95_us"])
                    < SHADOW_MAX_CAPTURE_P95_US
                ),
                "dedupe_reconciliation": exact_validation_flag(
                    "dedupe_reconciled"
                ),
                "behavior_equivalence": exact_validation_flag(
                    "behavior_equivalent"
                ),
                "source_read_errors_zero": (
                    census_attributes.get("source_read_errors") == 0
                    and census_validity.get("source_read_errors") == "exact"
                ),
                "static_behavior_proof": (
                    isinstance(census_attributes.get("behavior_proof"), str)
                    and census_validity.get("behavior_proof") == "exact"
                ),
                "validation_freshness": validation_fresh,
                "collector_shadow": (
                    collector_health["mode"] == "shadow"
                    and collector_field_validity.get("status") == "exact"
                ),
                "collector_freshness": (
                    collector_health["freshness_ms"] is not None
                    and int(collector_health["freshness_ms"])
                    <= source_freshness_ms
                ),
                "collector_no_drops": (
                    collector_health["dropped"] == 0
                    and collector_attribute_validity.get("dropped") == "exact"
                ),
            }
            shadow_gates.append(
                {
                    "source": source["source"],
                    "gate_version": SHADOW_GATE_VERSION,
                    "computed_status": (
                        "shadow_pass"
                        if all(checks.values())
                        else "shadow_blocked"
                    ),
                    "checks": checks,
                    "thresholds": {
                        "minimum_events": SHADOW_MIN_EVENTS,
                        "max_queue_lag_ms": SHADOW_MAX_QUEUE_LAG_MS,
                        "max_reconciliation_lag_ms": source_freshness_ms,
                        "max_capture_p95_us_exclusive": (
                            SHADOW_MAX_CAPTURE_P95_US
                        ),
                        "max_dropped_events": 0,
                    },
                    "activation_effect": "none",
                }
            )
        stale_sources = [
            row["source"]
            for row in sources
            if row["source"] not in _NON_WORK_SOURCES
            and row["freshness_status"] == "stale"
        ]
        missing_groups = [name for name, present in groups.items() if not present]
        alert_reasons: list[str] = []
        if not projection_current:
            alert_reasons.append("projection_stale")
        if stale_sources:
            alert_reasons.append("stale_sources:" + ",".join(stale_sources))
        if missing_groups:
            alert_reasons.append("missing_source_groups:" + ",".join(missing_groups))
        if token_executions and cost_executions < token_executions:
            alert_reasons.append("incomplete_cost_population")
        if cost_executions and pricing_versioned < cost_executions:
            alert_reasons.append("unversioned_pricing_population")
        if (
            subscription_executions
            and allocated_subscription_executions < subscription_executions
        ):
            alert_reasons.append("incomplete_subscription_allocation")
        test_counter_summary = _test_counter_summary(test_spans)
        test_counters_complete = bool(test_spans) and all(
            item["computed_status"] == "ready"
            for item in test_counter_summary["coverage"].values()
        )

        return {
            "schema_version": READMODEL_VERSION,
            "contract_version": CONTRACT_VERSION,
            "projection_version": PROJECTION_VERSION,
            "generated_at_ms": now_ms,
            "database": {
                "path": str(database_path),
                "computed_status": "ready" if projection_current else "stale",
                "raw_event_count": raw_event_count,
                "projected_event_count": projected_event_count,
                "projection_digest": meta.get("digest"),
                "rebuilt_at_ms": (
                    int(meta["rebuilt_at_ms"])
                    if meta.get("rebuilt_at_ms")
                    else None
                ),
            },
            "contract": contract_schema(),
            "p0": p0,
            "sources": sources,
            "source_census": {
                "schema_version": (
                    "execution-facts-source-census.v1"
                ),
                "sources": source_census,
            },
            "lifecycle_latencies": lifecycle_latencies,
            "outcomes": {
                "observed": outcomes_observed,
                "eligible": ended,
                "coverage_percent": _percent(outcomes_observed, ended),
                "validity": (
                    _worst_validity(*outcome_validities)
                    if outcome_validities
                    else "unknown"
                ),
                "by_status": dict(sorted(statuses.items())),
            },
            "retries": {
                **_metric(
                    "retry_rate",
                    observed=retries,
                    eligible=len(retry_eligible_rows),
                    validity=(
                        _worst_validity(*retry_validities)
                        if retry_validities
                        else "unknown"
                    ),
                    unknown_reason=(
                        None
                        if retry_eligible_rows
                        else "retry_instrumentation_absent"
                    ),
                ),
                "by_class": dict(
                    sorted(
                        Counter(
                            str(row["retry_class"])
                            for row in retry_eligible_rows
                            if row["retry_class"] is not None
                        ).items()
                    )
                ),
            },
            "errors": {
                **_metric(
                    "error_rate",
                    observed=errors,
                    eligible=ended,
                    validity=(
                        _worst_validity(*error_validities)
                        if error_validities
                        else "unknown"
                    ),
                    unknown_reason=(
                        "no_ended_executions"
                        if not ended
                        else (
                            "incomplete_error_instrumentation"
                            if error_measured < ended
                            else None
                        )
                    ),
                ),
                "measured": error_measured,
                "by_class": dict(
                    sorted(
                        Counter(
                            label
                            for is_error, _validity, label
                            in error_observations
                            if is_error is True and label is not None
                        ).items()
                    )
                ),
            },
            "landing": {
                **_metric(
                    "landed_execution_rate",
                    observed=landed,
                    eligible=ended,
                    validity=(
                        _worst_validity(*landed_validities)
                        if landed_validities
                        else "unknown"
                    ),
                    unknown_reason=(
                        "landing_evidence_absent"
                        if ended and not landed
                        else None
                    ),
                ),
                "sha_evidence": sum(
                    row["attributes"].get("landed_sha") is not None
                    for row in work_rows
                ),
            },
            "deployment": {
                **_metric(
                    "deployed_execution_rate",
                    observed=deployed,
                    eligible=landed,
                    validity=(
                        _worst_validity(*deployed_validities)
                        if deployed_validities
                        else "unknown"
                    ),
                    unknown_reason=(
                        "deployment_evidence_absent"
                        if landed and not deployed
                        else None
                    ),
                ),
                "sha_evidence": sum(
                    row["attributes"].get("deployed_sha") is not None
                    for row in work_rows
                ),
            },
            "usage": {
                "tokens": {
                    "computed_status": (
                        "ready" if token_executions else "unknown"
                    ),
                    "execution_count": token_executions,
                    "model_execution_count": model_executions,
                    "total_tokens": total_tokens if token_executions else None,
                    "validity": (
                        _worst_validity(*token_validities)
                        if token_validities
                        else "unknown"
                    ),
                    "reason": None if token_executions else "no_usage_facts",
                },
                "costs": {
                    "computed_status": (
                        "ready" if costs_ready else "unknown"
                    ),
                    "execution_count": cost_executions,
                    "validity": (
                        _worst_validity(*cost_validities)
                        if cost_validities
                        else "unknown"
                    ),
                    "pricing_versioned": pricing_versioned,
                    "pricing_versions": sorted(pricing_versions),
                    "currencies": sorted(currencies),
                    "api_equivalent_total": (
                        format(api_equivalent, "f")
                        if api_equivalent_executions and currency_compatible
                        else None
                    ),
                    "allocated_subscription_total": (
                        format(allocated_subscription, "f")
                        if allocated_subscription_executions
                        and currency_compatible
                        else None
                    ),
                    "marginal_total": (
                        format(marginal, "f")
                        if marginal_executions and currency_compatible
                        else None
                    ),
                    "metered_total": (
                        format(metered, "f")
                        if metered_executions and currency_compatible
                        else None
                    ),
                    "api_equivalent_population": api_equivalent_executions,
                    "subscription_population": subscription_executions,
                    "allocated_subscription_population": (
                        allocated_subscription_executions
                    ),
                    "marginal_population": marginal_executions,
                    "metered_population": metered_executions,
                    "allocation_fee_versioned": allocation_fee_versioned,
                    "fee_versions": sorted(fee_versions),
                    "allocation_formulas": sorted(allocation_formulas),
                    "allocation_population_sizes": sorted(
                        allocation_population_sizes
                    ),
                    "fx_status": (
                        "not_applicable_same_currency"
                        if currencies <= {"USD"}
                        else "unknown"
                    ),
                    "reason": (
                        None if costs_ready
                        else "missing_price_fee_or_allocation_facts"
                    ),
                },
            },
            "tools": {
                "computed_status": "ready" if tool_spans else "unknown",
                "observed_spans": len(tool_spans),
                "eligible": None,
                "unknown_reason": (
                    None
                    if tool_spans
                    else "tool_invocation_denominator_unavailable"
                ),
                "by_name": dict(
                    sorted(
                        Counter(
                            str(row["attributes"]["tool_name"])
                            for row in tool_spans
                            if row["attributes"].get("tool_name")
                        ).items()
                    )
                ),
                "completed": sum(
                    row["ended_at_ms"] is not None for row in tool_spans
                ),
                "errors": sum(
                    row["error_class"] is not None
                    or (
                        row["exit_code"] is not None
                        and int(row["exit_code"]) != 0
                    )
                    for row in tool_spans
                ),
                "retries": sum(
                    row["retry_class"] is not None for row in tool_spans
                ),
                "timeouts": sum(
                    bool(row["attributes"].get("timeout", False))
                    for row in tool_spans
                ),
                "by_dimensions": _span_dimension_groups(
                    tool_spans,
                    name_key="tool_name",
                ),
            },
            "tests": {
                "computed_status": (
                    "ready" if test_counters_complete else "unknown"
                ),
                "span_status": "ready" if test_spans else "unknown",
                "observed_spans": len(test_spans),
                "eligible": None,
                "unknown_reason": (
                    None
                    if test_counters_complete
                    else (
                        "missing_test_counter_coverage"
                        if test_spans
                        else "selected_test_denominator_unavailable"
                    )
                ),
                **test_counter_summary["values"],
                "counter_observed_lower_bounds": test_counter_summary[
                    "observed_lower_bounds"
                ],
                "counter_coverage": test_counter_summary["coverage"],
                "errors": sum(
                    row["error_class"] is not None
                    or (
                        row["exit_code"] is not None
                        and int(row["exit_code"]) != 0
                    )
                    for row in test_spans
                ),
                "retries": sum(
                    row["retry_class"] is not None for row in test_spans
                ),
                "timeouts": sum(
                    bool(row["attributes"].get("timeout", False))
                    for row in test_spans
                ),
                "gate_results": dict(
                    sorted(
                        Counter(
                            (
                                f"{row['attributes'].get('gate_type', 'unknown')}:"
                                f"{row.get('status') or 'unknown'}"
                            )
                            for row in test_spans
                        ).items()
                    )
                ),
                "flakes": dict(
                    sorted(
                        Counter(
                            str(row["attributes"]["flake_class"])
                            for row in test_spans
                            if row["attributes"].get("flake_class")
                        ).items()
                    )
                ),
                "by_dimensions": _test_dimension_groups(test_spans),
            },
            "activity_spans": {
                "agents": sum(
                    row["span_kind"] == "agent" for row in agent_spans
                ),
                "subagents": sum(
                    row["span_kind"] == "subagent" for row in agent_spans
                ),
                "operators": len(operator_spans),
                "operator_actions": dict(
                    sorted(
                        Counter(
                            str(row["attributes"]["operator_action_kind"])
                            for row in operator_spans
                            if row["attributes"].get("operator_action_kind")
                        ).items()
                    )
                ),
            },
            "collector": collector_health,
            "shadow_gates": {
                "gate_version": SHADOW_GATE_VERSION,
                "activation_effect": "none",
                "sources": shadow_gates,
            },
            "efficiency": _efficiency_rows(work_rows),
            "event_validity": {
                "exact": exact_events,
                "lower_bound": sum(
                    int(row["lower_bound_count"]) for row in source_rows
                ),
                "derived": sum(
                    int(row["derived_count"]) for row in source_rows
                ),
                "unknown": sum(
                    int(row["unknown_count"]) for row in source_rows
                ),
                "not_applicable": sum(
                    int(row["not_applicable_count"]) for row in source_rows
                ),
            },
            "alert_gate": {
                "computed_status": "ready" if not alert_reasons else "unknown",
                "reasons": alert_reasons,
            },
        }
    finally:
        connection.close()


__all__ = (
    "DEFAULT_SOURCE_FRESHNESS_MS",
    "READMODEL_VERSION",
    "build_execution_facts_payload",
)

"""Deterministic projections rebuilt exclusively from the raw event ledger."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionEvent,
    LifecyclePhase,
    Validity,
)

PROJECTION_VERSION = "execution-facts-projection.v1"

_PROJECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_projection (
    execution_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_execution_id TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    execution_surface TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    first_observed_at_ms INTEGER NOT NULL,
    last_observed_at_ms INTEGER NOT NULL,
    lifecycle_json TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    attribute_validity_json TEXT NOT NULL,
    field_validity_json TEXT NOT NULL,
    status TEXT,
    end_reason TEXT,
    error_class TEXT,
    retry_class TEXT,
    exit_code INTEGER,
    terminal_run_id TEXT,
    task_run_id TEXT,
    task_id TEXT,
    chain_id TEXT,
    cron_execution_id TEXT,
    loop_run_id TEXT,
    profile TEXT,
    board TEXT
);

CREATE TABLE IF NOT EXISTS execution_span_projection (
    span_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    parent_span_id TEXT,
    span_kind TEXT NOT NULL,
    validity TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    execution_surface TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    started_at_ms INTEGER,
    ended_at_ms INTEGER,
    terminal_run_id TEXT,
    task_run_id TEXT,
    status TEXT,
    error_class TEXT,
    retry_class TEXT,
    exit_code INTEGER,
    evidence_ref TEXT,
    attributes_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_span_parent
    ON execution_span_projection(parent_span_id);
CREATE INDEX IF NOT EXISTS idx_execution_span_execution
    ON execution_span_projection(execution_id, started_at_ms, span_id);

CREATE TABLE IF NOT EXISTS terminal_run_projection (
    terminal_run_id TEXT PRIMARY KEY,
    first_observed_at_ms INTEGER NOT NULL,
    first_observed_validity TEXT NOT NULL,
    last_observed_at_ms INTEGER NOT NULL,
    closed_at_ms INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    status TEXT
);

CREATE TABLE IF NOT EXISTS terminal_generation_projection (
    terminal_run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    first_observed_at_ms INTEGER NOT NULL,
    last_observed_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER,
    validity TEXT NOT NULL,
    status TEXT,
    PRIMARY KEY(terminal_run_id, generation)
);

CREATE TABLE IF NOT EXISTS task_binding_projection (
    span_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    terminal_run_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER,
    validity TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_binding_terminal
    ON task_binding_projection(terminal_run_id, started_at_ms);
CREATE INDEX IF NOT EXISTS idx_task_binding_task
    ON task_binding_projection(task_run_id, started_at_ms);

CREATE TABLE IF NOT EXISTS execution_source_projection (
    source TEXT PRIMARY KEY,
    event_count INTEGER NOT NULL,
    execution_count INTEGER NOT NULL,
    exact_count INTEGER NOT NULL,
    lower_bound_count INTEGER NOT NULL,
    derived_count INTEGER NOT NULL,
    unknown_count INTEGER NOT NULL,
    not_applicable_count INTEGER NOT NULL,
    first_observed_at_ms INTEGER NOT NULL,
    last_observed_at_ms INTEGER NOT NULL,
    max_queue_lag_ms INTEGER NOT NULL,
    max_reconciliation_lag_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_projection_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_VALIDITY_PRECEDENCE = {
    Validity.EXACT.value: 5,
    Validity.LOWER_BOUND.value: 4,
    Validity.DERIVED.value: 3,
    Validity.UNKNOWN.value: 2,
    Validity.NOT_APPLICABLE.value: 1,
}
_IDENTITY_SOURCE_PRECEDENCE = {
    "usage_facts": 1,
    "kanban_timeline": 10,
}


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    executions: int
    spans: int
    terminal_runs: int
    terminal_generations: int
    task_bindings: int
    sources: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_version": PROJECTION_VERSION,
            "executions": self.executions,
            "spans": self.spans,
            "terminal_runs": self.terminal_runs,
            "terminal_generations": self.terminal_generations,
            "task_bindings": self.task_bindings,
            "sources": self.sources,
            "digest": self.digest,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _prefer_phase(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if current is None:
        return candidate
    current_rank = _VALIDITY_PRECEDENCE[current["validity"]]
    candidate_rank = _VALIDITY_PRECEDENCE[candidate["validity"]]
    if candidate_rank > current_rank:
        return candidate
    if candidate_rank == current_rank and candidate["observed_at_ms"] < current["observed_at_ms"]:
        return candidate
    return current


def _initial_execution(event: ExecutionEvent) -> dict[str, Any]:
    return {
        "execution_id": event.execution_id,
        "source": event.source,
        "source_execution_id": event.source_execution_id,
        "workload_kind": event.workload_kind.value,
        "execution_surface": event.execution_surface.value,
        "trigger_kind": event.trigger_kind.value,
        "first_observed_at_ms": event.observed_at_ms,
        "last_observed_at_ms": event.observed_at_ms,
        "lifecycle": {},
        "attributes": {},
        "attribute_validity": {},
        "field_validity": {},
        "status": None,
        "end_reason": None,
        "error_class": None,
        "retry_class": None,
        "exit_code": None,
        "terminal_run_id": None,
        "task_run_id": None,
        "task_id": None,
        "chain_id": None,
        "cron_execution_id": None,
        "loop_run_id": None,
        "profile": None,
        "board": None,
    }


def _apply_event_to_execution(row: dict[str, Any], event: ExecutionEvent) -> None:
    if _IDENTITY_SOURCE_PRECEDENCE.get(
        event.source, 5
    ) > _IDENTITY_SOURCE_PRECEDENCE.get(str(row["source"]), 5):
        row["source"] = event.source
        row["source_execution_id"] = event.source_execution_id
        row["workload_kind"] = event.workload_kind.value
        row["execution_surface"] = event.execution_surface.value
        row["trigger_kind"] = event.trigger_kind.value
    row["first_observed_at_ms"] = min(
        row["first_observed_at_ms"], event.observed_at_ms
    )
    row["last_observed_at_ms"] = max(
        row["last_observed_at_ms"], event.observed_at_ms
    )
    if event.lifecycle_phase is not None:
        phase = event.lifecycle_phase.value
        candidate = {
            "observed_at_ms": event.observed_at_ms,
            "validity": event.validity.value,
            "event_id": event.event_id,
            "evidence_ref": event.evidence_ref,
        }
        row["lifecycle"][phase] = _prefer_phase(
            row["lifecycle"].get(phase), candidate
        )
    for key, value in event.attributes.items():
        current_validity = row["attribute_validity"].get(key)
        if (
            current_validity is None
            or _VALIDITY_PRECEDENCE[event.validity.value]
            >= _VALIDITY_PRECEDENCE[current_validity]
        ):
            row["attributes"][key] = value
            row["attribute_validity"][key] = event.validity.value
    for name in (
        "status",
        "end_reason",
        "error_class",
        "retry_class",
        "exit_code",
        "terminal_run_id",
        "task_run_id",
        "task_id",
        "chain_id",
        "cron_execution_id",
        "loop_run_id",
        "profile",
        "board",
    ):
        value = getattr(event, name)
        current_validity = row["field_validity"].get(name)
        if value is not None and (
            current_validity is None
            or _VALIDITY_PRECEDENCE[event.validity.value]
            >= _VALIDITY_PRECEDENCE[current_validity]
        ):
            row[name] = value
            row["field_validity"][name] = event.validity.value


def _apply_span(
    spans: dict[str, dict[str, Any]],
    event: ExecutionEvent,
) -> None:
    if event.span_id is None or event.span_kind is None:
        return
    row = spans.setdefault(
        event.span_id,
        {
            "span_id": event.span_id,
            "execution_id": event.execution_id,
            "parent_span_id": event.parent_span_id,
            "span_kind": event.span_kind.value,
            "validity": event.validity.value,
            "workload_kind": event.workload_kind.value,
            "execution_surface": event.execution_surface.value,
            "trigger_kind": event.trigger_kind.value,
            "started_at_ms": None,
            "ended_at_ms": None,
            "terminal_run_id": event.terminal_run_id,
            "task_run_id": event.task_run_id,
            "status": None,
            "error_class": None,
            "retry_class": None,
            "exit_code": None,
            "evidence_ref": None,
            "attributes": {},
        },
    )
    if row["execution_id"] != event.execution_id:
        raise ValueError(f"span {event.span_id} crosses execution identities")
    if (
        row["parent_span_id"] is not None
        and event.parent_span_id is not None
        and row["parent_span_id"] != event.parent_span_id
    ):
        raise ValueError(f"span {event.span_id} changes parent identity")
    if (
        row["span_kind"] != event.span_kind.value
        or row["parent_span_id"] != event.parent_span_id
        or row["workload_kind"] != event.workload_kind.value
        or row["execution_surface"] != event.execution_surface.value
        or row["trigger_kind"] != event.trigger_kind.value
    ):
        raise ValueError(f"span {event.span_id} changes immutable metadata")
    if _VALIDITY_PRECEDENCE[event.validity.value] < _VALIDITY_PRECEDENCE[row["validity"]]:
        row["validity"] = event.validity.value
    if event.event_type in {
        EventType.SPAN_STARTED,
        EventType.TASK_BINDING_STARTED,
    }:
        started = row["started_at_ms"]
        row["started_at_ms"] = (
            event.observed_at_ms
            if started is None
            else min(started, event.observed_at_ms)
        )
    if event.event_type in {
        EventType.SPAN_ENDED,
        EventType.TASK_BINDING_ENDED,
    }:
        ended = row["ended_at_ms"]
        row["ended_at_ms"] = (
            event.observed_at_ms
            if ended is None
            else max(ended, event.observed_at_ms)
        )
    for name in (
        "terminal_run_id",
        "task_run_id",
        "status",
        "error_class",
        "retry_class",
        "exit_code",
        "evidence_ref",
    ):
        value = getattr(event, name)
        if value is not None:
            row[name] = value
    row["attributes"].update(dict(event.attributes))


def _apply_terminal(
    terminal_runs: dict[str, dict[str, Any]],
    generations: dict[tuple[str, int], dict[str, Any]],
    event: ExecutionEvent,
) -> None:
    if event.terminal_run_id is None or event.event_type not in {
        EventType.TERMINAL_OBSERVED,
        EventType.TERMINAL_GENERATION_ENDED,
        EventType.TERMINAL_CLOSED,
    }:
        return
    run = terminal_runs.setdefault(
        event.terminal_run_id,
        {
            "terminal_run_id": event.terminal_run_id,
            "first_observed_at_ms": event.observed_at_ms,
            "first_observed_validity": event.validity.value,
            "last_observed_at_ms": event.observed_at_ms,
            "closed_at_ms": None,
            "archived": 0,
            "status": event.status,
        },
    )
    if event.observed_at_ms < run["first_observed_at_ms"]:
        run["first_observed_at_ms"] = event.observed_at_ms
        run["first_observed_validity"] = event.validity.value
    run["last_observed_at_ms"] = max(
        run["last_observed_at_ms"], event.observed_at_ms
    )
    if event.status is not None:
        run["status"] = event.status
    if (
        event.event_type is EventType.TERMINAL_CLOSED
        and run["closed_at_ms"] is None
    ):
        run["closed_at_ms"] = event.observed_at_ms
        run["archived"] = int(bool(event.attributes.get("archived", False)))
    if event.terminal_generation is None:
        return
    key = (event.terminal_run_id, event.terminal_generation)
    generation = generations.setdefault(
        key,
        {
            "terminal_run_id": event.terminal_run_id,
            "generation": event.terminal_generation,
            "first_observed_at_ms": event.observed_at_ms,
            "last_observed_at_ms": event.observed_at_ms,
            "ended_at_ms": None,
            "validity": event.validity.value,
            "status": event.status,
        },
    )
    generation["first_observed_at_ms"] = min(
        generation["first_observed_at_ms"], event.observed_at_ms
    )
    generation["last_observed_at_ms"] = max(
        generation["last_observed_at_ms"], event.observed_at_ms
    )
    if event.event_type is EventType.TERMINAL_GENERATION_ENDED:
        generation["ended_at_ms"] = event.observed_at_ms
    if event.status is not None:
        generation["status"] = event.status
    if _VALIDITY_PRECEDENCE[event.validity.value] < _VALIDITY_PRECEDENCE[generation["validity"]]:
        generation["validity"] = event.validity.value


def _source_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT source,
                   COUNT(*) AS event_count,
                   COUNT(DISTINCT execution_id) AS execution_count,
                   SUM(validity = 'exact') AS exact_count,
                   SUM(validity = 'lower_bound') AS lower_bound_count,
                   SUM(validity = 'derived') AS derived_count,
                   SUM(validity = 'unknown') AS unknown_count,
                   SUM(validity = 'not_applicable') AS not_applicable_count,
                   MIN(observed_at_ms) AS first_observed_at_ms,
                   MAX(observed_at_ms) AS last_observed_at_ms,
                   MAX(queue_lag_ms) AS max_queue_lag_ms,
                   MAX(reconciliation_lag_ms)
                        AS max_reconciliation_lag_ms
              FROM execution_events
             GROUP BY source
             ORDER BY source
            """
        ).fetchall()
    ]


def _digest_rows(groups: Iterable[Iterable[dict[str, Any]]]) -> str:
    digest = hashlib.sha256()
    for rows in groups:
        for row in rows:
            digest.update(_canonical_json(row).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def rebuild_projections(path: Path | str) -> ProjectionResult:
    """Replace every derived table atomically from immutable raw events."""
    connection = sqlite3.connect(Path(path))
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(_PROJECTION_SCHEMA)
        source_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(execution_source_projection)"
            )
        }
        if "max_reconciliation_lag_ms" not in source_columns:
            connection.execute(
                """
                ALTER TABLE execution_source_projection
                ADD COLUMN max_reconciliation_lag_ms
                    INTEGER NOT NULL DEFAULT 0
                """
            )
        span_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(execution_span_projection)"
            )
        }
        span_migrations = {
            "workload_kind": "TEXT NOT NULL DEFAULT 'unclassified'",
            "execution_surface": "TEXT NOT NULL DEFAULT 'unknown'",
            "trigger_kind": "TEXT NOT NULL DEFAULT 'unknown'",
            "exit_code": "INTEGER",
            "evidence_ref": "TEXT",
        }
        for name, definition in span_migrations.items():
            if name not in span_columns:
                connection.execute(
                    f"""
                    ALTER TABLE execution_span_projection
                    ADD COLUMN {name} {definition}
                    """
                )
        execution_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(execution_projection)"
            )
        }
        if "attribute_validity_json" not in execution_columns:
            connection.execute(
                """
                ALTER TABLE execution_projection
                ADD COLUMN attribute_validity_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        if "field_validity_json" not in execution_columns:
            connection.execute(
                """
                ALTER TABLE execution_projection
                ADD COLUMN field_validity_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        event_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(execution_events)")
        }
        if "reconciliation_lag_ms" not in event_columns:
            connection.execute(
                """
                ALTER TABLE execution_events
                ADD COLUMN reconciliation_lag_ms INTEGER NOT NULL DEFAULT 0
                """
            )
        raw_rows = connection.execute(
            """
            SELECT event_json
              FROM execution_events
             ORDER BY observed_at_ms, recorded_at_ms, event_id
            """
        ).fetchall()
        events = [
            ExecutionEvent.from_dict(json.loads(row["event_json"]))
            for row in raw_rows
        ]
        executions: dict[str, dict[str, Any]] = {}
        spans: dict[str, dict[str, Any]] = {}
        terminal_runs: dict[str, dict[str, Any]] = {}
        generations: dict[tuple[str, int], dict[str, Any]] = {}
        for event in events:
            execution = executions.setdefault(
                event.execution_id, _initial_execution(event)
            )
            _apply_event_to_execution(execution, event)
            _apply_span(spans, event)
            _apply_terminal(terminal_runs, generations, event)

        execution_rows = [
            {
                **row,
                "lifecycle_json": _canonical_json(row.pop("lifecycle")),
                "attributes_json": _canonical_json(row.pop("attributes")),
                "attribute_validity_json": _canonical_json(
                    row.pop("attribute_validity")
                ),
                "field_validity_json": _canonical_json(
                    row.pop("field_validity")
                ),
            }
            for row in (dict(executions[key]) for key in sorted(executions))
        ]
        span_rows = [
            {
                **row,
                "attributes_json": _canonical_json(row.pop("attributes")),
            }
            for row in (dict(spans[key]) for key in sorted(spans))
        ]
        terminal_rows = [terminal_runs[key] for key in sorted(terminal_runs)]
        generation_rows = [generations[key] for key in sorted(generations)]
        binding_rows = [
            {
                "span_id": row["span_id"],
                "execution_id": row["execution_id"],
                "terminal_run_id": row["terminal_run_id"],
                "task_run_id": row["task_run_id"],
                "started_at_ms": row["started_at_ms"],
                "ended_at_ms": row["ended_at_ms"],
                "validity": row["validity"],
            }
            for row in span_rows
            if row["span_kind"] == "task_binding"
            and row["terminal_run_id"] is not None
            and row["task_run_id"] is not None
            and row["started_at_ms"] is not None
        ]
        sources = _source_rows(connection)

        digest = _digest_rows(
            (
                execution_rows,
                span_rows,
                terminal_rows,
                generation_rows,
                binding_rows,
                sources,
            )
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in (
                "execution_projection",
                "execution_span_projection",
                "terminal_run_projection",
                "terminal_generation_projection",
                "task_binding_projection",
                "execution_source_projection",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.executemany(
                """
                INSERT INTO execution_projection (
                    execution_id, source, source_execution_id, workload_kind,
                    execution_surface, trigger_kind, first_observed_at_ms,
                    last_observed_at_ms, lifecycle_json, attributes_json,
                    attribute_validity_json, field_validity_json, status,
                    end_reason, error_class, retry_class, exit_code,
                    terminal_run_id, task_run_id, task_id, chain_id,
                    cron_execution_id, loop_run_id, profile, board
                ) VALUES (
                    :execution_id, :source, :source_execution_id, :workload_kind,
                    :execution_surface, :trigger_kind, :first_observed_at_ms,
                    :last_observed_at_ms, :lifecycle_json, :attributes_json,
                    :attribute_validity_json, :field_validity_json, :status,
                    :end_reason, :error_class, :retry_class, :exit_code,
                    :terminal_run_id, :task_run_id, :task_id, :chain_id,
                    :cron_execution_id, :loop_run_id, :profile, :board
                )
                """,
                execution_rows,
            )
            connection.executemany(
                """
                INSERT INTO execution_span_projection (
                    span_id, execution_id, parent_span_id, span_kind, validity,
                    workload_kind, execution_surface, trigger_kind,
                    started_at_ms, ended_at_ms, terminal_run_id, task_run_id,
                    status, error_class, retry_class, exit_code, evidence_ref,
                    attributes_json
                ) VALUES (
                    :span_id, :execution_id, :parent_span_id, :span_kind,
                    :validity, :workload_kind, :execution_surface,
                    :trigger_kind, :started_at_ms, :ended_at_ms,
                    :terminal_run_id, :task_run_id, :status, :error_class,
                    :retry_class, :exit_code, :evidence_ref, :attributes_json
                )
                """,
                span_rows,
            )
            connection.executemany(
                """
                INSERT INTO terminal_run_projection (
                    terminal_run_id, first_observed_at_ms,
                    first_observed_validity, last_observed_at_ms, closed_at_ms,
                    archived, status
                ) VALUES (
                    :terminal_run_id, :first_observed_at_ms,
                    :first_observed_validity, :last_observed_at_ms,
                    :closed_at_ms, :archived, :status
                )
                """,
                terminal_rows,
            )
            connection.executemany(
                """
                INSERT INTO terminal_generation_projection (
                    terminal_run_id, generation, first_observed_at_ms,
                    last_observed_at_ms, ended_at_ms, validity, status
                ) VALUES (
                    :terminal_run_id, :generation, :first_observed_at_ms,
                    :last_observed_at_ms, :ended_at_ms, :validity, :status
                )
                """,
                generation_rows,
            )
            connection.executemany(
                """
                INSERT INTO task_binding_projection (
                    span_id, execution_id, terminal_run_id, task_run_id,
                    started_at_ms, ended_at_ms, validity
                ) VALUES (
                    :span_id, :execution_id, :terminal_run_id, :task_run_id,
                    :started_at_ms, :ended_at_ms, :validity
                )
                """,
                binding_rows,
            )
            connection.executemany(
                """
                INSERT INTO execution_source_projection (
                    source, event_count, execution_count, exact_count,
                    lower_bound_count, derived_count, unknown_count,
                    not_applicable_count, first_observed_at_ms,
                    last_observed_at_ms, max_queue_lag_ms,
                    max_reconciliation_lag_ms
                ) VALUES (
                    :source, :event_count, :execution_count, :exact_count,
                    :lower_bound_count, :derived_count, :unknown_count,
                    :not_applicable_count, :first_observed_at_ms,
                    :last_observed_at_ms, :max_queue_lag_ms,
                    :max_reconciliation_lag_ms
                )
                """,
                sources,
            )
            meta = {
                "projection_version": PROJECTION_VERSION,
                "contract_version": (
                    events[0].contract_version if events else "execution-facts.v1"
                ),
                "digest": digest,
                "rebuilt_at_ms": str(int(time.time() * 1000)),
                "raw_event_count": str(len(events)),
            }
            connection.executemany(
                """
                INSERT INTO execution_projection_meta(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                sorted(meta.items()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()
    return ProjectionResult(
        executions=len(execution_rows),
        spans=len(span_rows),
        terminal_runs=len(terminal_rows),
        terminal_generations=len(generation_rows),
        task_bindings=len(binding_rows),
        sources=len(sources),
        digest=digest,
    )


__all__ = (
    "PROJECTION_VERSION",
    "ProjectionResult",
    "rebuild_projections",
)

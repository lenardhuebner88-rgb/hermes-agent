"""Append-only SQLite ledger and non-blocking execution-facts emitter."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Mapping

from hermes_cli.execution_facts_contract import (
    CONTRACT_VERSION,
    EventType,
    ExecutionEvent,
    ExecutionSurface,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_idempotency_key,
)

DEFAULT_EXECUTION_FACTS_DB = Path(
    "/mnt/data/hermes-observability/execution_facts.db"
)
DEFAULT_RAW_RETENTION_DAYS = 90
SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_facts_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    source_execution_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    validity TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    recorded_at_ms INTEGER NOT NULL,
    ingested_at_ms INTEGER NOT NULL,
    queue_lag_ms INTEGER NOT NULL,
    reconciliation_lag_ms INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    UNIQUE(source, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_execution_events_execution
    ON execution_events(execution_id, observed_at_ms, event_id);
CREATE INDEX IF NOT EXISTS idx_execution_events_source_observed
    ON execution_events(source, observed_at_ms, event_id);
CREATE INDEX IF NOT EXISTS idx_execution_events_type_observed
    ON execution_events(event_type, observed_at_ms, event_id);

CREATE TABLE IF NOT EXISTS execution_event_dedupe (
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    immutable_payload_sha256 TEXT NOT NULL,
    queue_lag_ms INTEGER NOT NULL,
    reconciliation_lag_ms INTEGER NOT NULL,
    first_ingested_at_ms INTEGER NOT NULL,
    PRIMARY KEY(source, idempotency_key)
);

CREATE TABLE IF NOT EXISTS execution_monthly_aggregates (
    month TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    validity TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    first_observed_at_ms INTEGER NOT NULL,
    last_observed_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(month, source, event_type, validity)
);
"""


def _immutable_payload_sha256(payload: str) -> str:
    value = json.loads(payload)
    value.pop("recorded_at_ms", None)
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyConflictError(RuntimeError):
    """The same source key was reused for a different immutable event."""


class EmitterMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"


def emitter_mode_from_environment(
    environment: Mapping[str, str] | None = None,
) -> EmitterMode:
    values = environment if environment is not None else os.environ
    return EmitterMode(
        str(values.get("HERMES_EXECUTION_FACTS_MODE", "off")).strip().lower()
    )


@dataclass(frozen=True, slots=True)
class AppendResult:
    event_id: str
    inserted: bool
    queue_lag_ms: int
    reconciliation_lag_ms: int = 0


@dataclass(frozen=True, slots=True)
class RetentionResult:
    cutoff_ms: int
    aggregated_rows: int
    deleted_events: int


@dataclass(frozen=True, slots=True)
class EmitterSnapshot:
    mode: str
    submitted: int
    accepted: int
    dropped_disabled: int
    dropped_full: int
    dropped_write_error: int
    deduped: int
    written: int
    queue_depth: int
    queue_capacity: int
    last_enqueue_at_ms: int | None
    last_write_at_ms: int | None
    last_error_class: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "submitted": self.submitted,
            "accepted": self.accepted,
            "dropped_disabled": self.dropped_disabled,
            "dropped_full": self.dropped_full,
            "dropped_write_error": self.dropped_write_error,
            "deduped": self.deduped,
            "written": self.written,
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue_capacity,
            "last_enqueue_at_ms": self.last_enqueue_at_ms,
            "last_write_at_ms": self.last_write_at_ms,
            "last_error_class": self.last_error_class,
        }


def execution_facts_db_path(
    path: os.PathLike[str] | str | None = None,
) -> Path:
    explicit = path or os.environ.get("HERMES_EXECUTION_FACTS_DB")
    return Path(explicit).expanduser() if explicit else DEFAULT_EXECUTION_FACTS_DB


class ExecutionFactsLedger:
    """Own the raw immutable event truth in a dedicated SQLite database."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.path = execution_facts_db_path(path)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            meta_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='execution_facts_meta'"
                ).fetchone()
                is not None
            )
            dedupe_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='execution_event_dedupe'"
                ).fetchone()
                is not None
            )
            previous_schema = None
            if meta_exists:
                row = connection.execute(
                    "SELECT value FROM execution_facts_meta "
                    "WHERE key='schema_version'"
                ).fetchone()
                if row is not None:
                    try:
                        previous_schema = int(row[0])
                    except (TypeError, ValueError):
                        previous_schema = None
            connection.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(execution_events)"
                )
            }
            if "reconciliation_lag_ms" not in columns:
                connection.execute(
                    """
                    ALTER TABLE execution_events
                    ADD COLUMN reconciliation_lag_ms INTEGER NOT NULL DEFAULT 0
                    """
                )
            if not dedupe_exists or previous_schema is None or previous_schema < 3:
                retained_rows = connection.execute(
                    """
                    SELECT source, idempotency_key, event_id, event_json,
                           queue_lag_ms, reconciliation_lag_ms, ingested_at_ms
                      FROM execution_events
                    """
                ).fetchall()
                for row in retained_rows:
                    fingerprint = _immutable_payload_sha256(
                        str(row["event_json"])
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO execution_event_dedupe (
                            source, idempotency_key, event_id,
                            immutable_payload_sha256, queue_lag_ms,
                            reconciliation_lag_ms, first_ingested_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source, idempotency_key) DO NOTHING
                        """,
                        (
                            row["source"],
                            row["idempotency_key"],
                            row["event_id"],
                            fingerprint,
                            row["queue_lag_ms"],
                            row["reconciliation_lag_ms"],
                            row["ingested_at_ms"],
                        ),
                    )
                    if cursor.rowcount == 0:
                        existing = connection.execute(
                            """
                            SELECT event_id, immutable_payload_sha256
                              FROM execution_event_dedupe
                             WHERE source = ? AND idempotency_key = ?
                            """,
                            (row["source"], row["idempotency_key"]),
                        ).fetchone()
                        if (
                            existing is None
                            or existing["event_id"] != row["event_id"]
                            or existing["immutable_payload_sha256"]
                            != fingerprint
                        ):
                            raise IdempotencyConflictError(
                                f"{row['source']}:{row['idempotency_key']} "
                                "conflicts with the permanent dedupe registry"
                            )
            connection.execute(
                """
                INSERT INTO execution_facts_meta(key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                """
                INSERT INTO execution_facts_meta(key, value)
                VALUES ('contract_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (CONTRACT_VERSION,),
            )
            connection.commit()

    def append(self, event: ExecutionEvent) -> AppendResult:
        return self.append_batch((event,))[0]

    def append_batch(
        self, events: Sequence[ExecutionEvent]
    ) -> tuple[AppendResult, ...]:
        if not events:
            return ()
        ingested_at_ms = self._clock_ms()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                results = tuple(
                    self._append_in_transaction(
                        connection, event, ingested_at_ms=ingested_at_ms
                    )
                    for event in events
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return results

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: ExecutionEvent,
        *,
        ingested_at_ms: int,
    ) -> AppendResult:
        payload = event.to_json()
        fingerprint = _immutable_payload_sha256(payload)
        queue_lag_ms = max(0, ingested_at_ms - event.recorded_at_ms)
        reconciliation_lag_ms = max(
            0, event.recorded_at_ms - event.observed_at_ms
        )
        dedupe_cursor = connection.execute(
            """
            INSERT INTO execution_event_dedupe (
                source, idempotency_key, event_id,
                immutable_payload_sha256, queue_lag_ms,
                reconciliation_lag_ms, first_ingested_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, idempotency_key) DO NOTHING
            """,
            (
                event.source,
                event.idempotency_key,
                event.event_id,
                fingerprint,
                queue_lag_ms,
                reconciliation_lag_ms,
                ingested_at_ms,
            ),
        )
        if dedupe_cursor.rowcount == 0:
            existing = connection.execute(
                """
                SELECT event_id, immutable_payload_sha256, queue_lag_ms,
                       reconciliation_lag_ms
                  FROM execution_event_dedupe
                 WHERE source = ? AND idempotency_key = ?
                """,
                (event.source, event.idempotency_key),
            ).fetchone()
            if (
                existing is None
                or existing["event_id"] != event.event_id
                or existing["immutable_payload_sha256"] != fingerprint
            ):
                raise IdempotencyConflictError(
                    f"{event.source}:{event.idempotency_key} "
                    "identifies different facts"
                )
            return AppendResult(
                str(existing["event_id"]),
                False,
                int(existing["queue_lag_ms"]),
                int(existing["reconciliation_lag_ms"]),
            )
        cursor = connection.execute(
            """
            INSERT INTO execution_events (
                event_id, source, idempotency_key, execution_id,
                source_execution_id, event_type, validity, observed_at_ms,
                recorded_at_ms, ingested_at_ms, queue_lag_ms,
                reconciliation_lag_ms, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.source,
                event.idempotency_key,
                event.execution_id,
                event.source_execution_id,
                event.event_type.value,
                event.validity.value,
                event.observed_at_ms,
                event.recorded_at_ms,
                ingested_at_ms,
                queue_lag_ms,
                reconciliation_lag_ms,
                payload,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("new dedupe record did not insert its raw event")
        return AppendResult(
            event.event_id,
            True,
            queue_lag_ms,
            reconciliation_lag_ms,
        )

    def iter_events(
        self,
        *,
        after_observed_at_ms: int | None = None,
        source: str | None = None,
    ) -> Iterator[ExecutionEvent]:
        clauses: list[str] = []
        values: list[Any] = []
        if after_observed_at_ms is not None:
            clauses.append("observed_at_ms >= ?")
            values.append(int(after_observed_at_ms))
        if source is not None:
            clauses.append("source = ?")
            values.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT event_json
                  FROM execution_events
                  {where}
                 ORDER BY observed_at_ms, event_id
                """,
                values,
            ).fetchall()
        for row in rows:
            yield ExecutionEvent.from_dict(json.loads(row["event_json"]))

    def count_events(self) -> int:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM execution_events"
            ).fetchone()
        return int(row["count"])

    def ledger_health(self) -> dict[str, Any]:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       COUNT(DISTINCT execution_id) AS execution_count,
                       MIN(observed_at_ms) AS first_observed_at_ms,
                       MAX(observed_at_ms) AS last_observed_at_ms,
                       MAX(queue_lag_ms) AS max_queue_lag_ms,
                       MAX(reconciliation_lag_ms)
                            AS max_reconciliation_lag_ms
                  FROM execution_events
                """
            ).fetchone()
            sources = connection.execute(
                """
                SELECT source, COUNT(*) AS event_count,
                       MAX(observed_at_ms) AS last_observed_at_ms,
                       MAX(queue_lag_ms) AS max_queue_lag_ms,
                       MAX(reconciliation_lag_ms)
                            AS max_reconciliation_lag_ms
                  FROM execution_events
                 GROUP BY source
                 ORDER BY source
                """
            ).fetchall()
        return {
            "event_count": int(row["event_count"]),
            "execution_count": int(row["execution_count"]),
            "first_observed_at_ms": row["first_observed_at_ms"],
            "last_observed_at_ms": row["last_observed_at_ms"],
            "max_queue_lag_ms": row["max_queue_lag_ms"],
            "max_reconciliation_lag_ms": row[
                "max_reconciliation_lag_ms"
            ],
            "sources": [
                {
                    "source": source["source"],
                    "event_count": int(source["event_count"]),
                    "last_observed_at_ms": source["last_observed_at_ms"],
                    "max_queue_lag_ms": source["max_queue_lag_ms"],
                    "max_reconciliation_lag_ms": source[
                        "max_reconciliation_lag_ms"
                    ],
                }
                for source in sources
            ],
        }

    def apply_retention(
        self,
        *,
        raw_days: int = DEFAULT_RAW_RETENTION_DAYS,
        now: datetime | None = None,
    ) -> RetentionResult:
        if raw_days < 1:
            raise ValueError("raw_days must be positive")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        cutoff = current.astimezone(UTC) - timedelta(days=raw_days)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        updated_at_ms = int(current.timestamp() * 1000)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidates = connection.execute(
                    """
                    SELECT strftime(
                               '%Y-%m', observed_at_ms / 1000, 'unixepoch'
                           ) AS month,
                           source, event_type, validity,
                           COUNT(*) AS event_count,
                           MIN(observed_at_ms) AS first_observed_at_ms,
                           MAX(observed_at_ms) AS last_observed_at_ms
                      FROM execution_events
                     WHERE observed_at_ms < ?
                     GROUP BY month, source, event_type, validity
                    """,
                    (cutoff_ms,),
                ).fetchall()
                for row in candidates:
                    connection.execute(
                        """
                        INSERT INTO execution_monthly_aggregates (
                            month, source, event_type, validity, event_count,
                            first_observed_at_ms, last_observed_at_ms, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(month, source, event_type, validity)
                        DO UPDATE SET
                            event_count = (
                                execution_monthly_aggregates.event_count
                                + excluded.event_count
                            ),
                            first_observed_at_ms = MIN(
                                execution_monthly_aggregates.first_observed_at_ms,
                                excluded.first_observed_at_ms
                            ),
                            last_observed_at_ms = MAX(
                                execution_monthly_aggregates.last_observed_at_ms,
                                excluded.last_observed_at_ms
                            ),
                            updated_at_ms = excluded.updated_at_ms
                        """,
                        (
                            row["month"],
                            row["source"],
                            row["event_type"],
                            row["validity"],
                            int(row["event_count"]),
                            int(row["first_observed_at_ms"]),
                            int(row["last_observed_at_ms"]),
                            updated_at_ms,
                        ),
                    )
                deleted = connection.execute(
                    "DELETE FROM execution_events WHERE observed_at_ms < ?",
                    (cutoff_ms,),
                ).rowcount
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return RetentionResult(cutoff_ms, len(candidates), int(deleted))

    def monthly_aggregates(self) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT month, source, event_type, validity, event_count,
                       first_observed_at_ms, last_observed_at_ms, updated_at_ms
                  FROM execution_monthly_aggregates
                 ORDER BY month, source, event_type, validity
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def _connect(
        self, *, read_only: bool = False
    ) -> Iterator[sqlite3.Connection]:
        if read_only:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
        else:
            connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        if not read_only:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
        finally:
            connection.close()


class NonBlockingEmitter:
    """Bounded hand-off; source work never waits for collector I/O."""

    def __init__(
        self,
        sink: Callable[[ExecutionEvent], AppendResult],
        *,
        mode: EmitterMode | str = EmitterMode.OFF,
        capacity: int = 4096,
        thread_name: str = "execution-facts-writer",
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.mode = EmitterMode(mode)
        self._sink = sink
        self._queue: queue.Queue[ExecutionEvent | object] = queue.Queue(
            maxsize=capacity
        )
        self._capacity = capacity
        self._sentinel = object()
        self._thread_name = thread_name
        self._thread: threading.Thread | None = None
        self._metrics_lock = threading.Lock()
        self._submitted = 0
        self._accepted = 0
        self._dropped_disabled = 0
        self._dropped_full = 0
        self._dropped_write_error = 0
        self._deduped = 0
        self._written = 0
        self._last_enqueue_at_ms: int | None = None
        self._last_write_at_ms: int | None = None
        self._last_error_class: str | None = None

    def start(self) -> bool:
        if self.mode is EmitterMode.OFF:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._run,
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()
        return True

    def try_emit(self, event: ExecutionEvent) -> bool:
        """Return immediately; never perform storage, network, or retries."""
        now_ms = int(time.time() * 1000)
        with self._metrics_lock:
            self._submitted += 1
        if self.mode is EmitterMode.OFF:
            with self._metrics_lock:
                self._dropped_disabled += 1
            return False
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._metrics_lock:
                self._dropped_full += 1
            return False
        with self._metrics_lock:
            self._accepted += 1
            self._last_enqueue_at_ms = now_ms
        return True

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        """Test/operator drain helper; it is never used by source hot paths."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def shutdown(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        try:
            self._queue.put_nowait(self._sentinel)
        except queue.Full:
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def snapshot(self) -> EmitterSnapshot:
        with self._metrics_lock:
            return EmitterSnapshot(
                mode=self.mode.value,
                submitted=self._submitted,
                accepted=self._accepted,
                dropped_disabled=self._dropped_disabled,
                dropped_full=self._dropped_full,
                dropped_write_error=self._dropped_write_error,
                deduped=self._deduped,
                written=self._written,
                queue_depth=self._queue.qsize(),
                queue_capacity=self._capacity,
                last_enqueue_at_ms=self._last_enqueue_at_ms,
                last_write_at_ms=self._last_write_at_ms,
                last_error_class=self._last_error_class,
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._sentinel:
                    return
                assert isinstance(item, ExecutionEvent)
                try:
                    result = self._sink(item)
                except Exception as exc:  # noqa: BLE001 - telemetry is fail-open
                    with self._metrics_lock:
                        self._dropped_write_error += 1
                        self._last_error_class = type(exc).__name__
                    continue
                with self._metrics_lock:
                    if result.inserted:
                        self._written += 1
                    else:
                        self._deduped += 1
                    self._last_write_at_ms = int(time.time() * 1000)
                    self._last_error_class = None
            finally:
                self._queue.task_done()


def emitter_snapshot_event(
    snapshot: EmitterSnapshot,
    *,
    collector_id: str,
    observed_at_ms: int,
) -> ExecutionEvent:
    """Convert exact in-process counters into a content-free health fact."""
    return make_event(
        source="execution_facts_collector",
        source_execution_id=f"collector:{collector_id}",
        idempotency_key=stable_idempotency_key(
            "execution_facts_collector",
            collector_id,
            observed_at_ms,
        ),
        event_type=EventType.COLLECTOR_HEALTH,
        validity=Validity.EXACT,
        observed_at_ms=observed_at_ms,
        recorded_at_ms=observed_at_ms,
        workload_kind=WorkloadKind.SYSTEM_JOB,
        execution_surface=ExecutionSurface.PROCESS,
        trigger_kind=TriggerKind.RECONCILIATION,
        status=snapshot.mode,
        error_class=snapshot.last_error_class,
        attributes={
            "submitted_events": snapshot.submitted,
            "accepted_events": snapshot.accepted,
            "dropped": (
                snapshot.dropped_disabled
                + snapshot.dropped_full
                + snapshot.dropped_write_error
            ),
            "write_errors": snapshot.dropped_write_error,
            "deduped": snapshot.deduped,
            "written_events": snapshot.written,
            "queue_depth": snapshot.queue_depth,
            "queue_capacity": snapshot.queue_capacity,
        },
    )


def publish_after_commit(
    emitter: NonBlockingEmitter,
    event: ExecutionEvent,
) -> bool:
    """Best-effort post-commit seam; telemetry can never reverse source work."""
    try:
        return emitter.try_emit(event)
    except Exception:  # noqa: BLE001 - telemetry is explicitly fail-open
        return False


def initialize_execution_facts_db(
    path: os.PathLike[str] | str | None = None,
) -> ExecutionFactsLedger:
    ledger = ExecutionFactsLedger(execution_facts_db_path(path))
    ledger.initialize()
    return ledger


__all__ = (
    "DEFAULT_EXECUTION_FACTS_DB",
    "DEFAULT_RAW_RETENTION_DAYS",
    "AppendResult",
    "EmitterMode",
    "EmitterSnapshot",
    "ExecutionFactsLedger",
    "IdempotencyConflictError",
    "NonBlockingEmitter",
    "RetentionResult",
    "execution_facts_db_path",
    "emitter_mode_from_environment",
    "emitter_snapshot_event",
    "initialize_execution_facts_db",
    "publish_after_commit",
)

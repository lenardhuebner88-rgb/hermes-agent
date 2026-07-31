"""Read-only source census and fail-open Shadow collection for Execution Facts.

The collector reads existing execution stores and neutral runtime metadata.  It
never changes source databases, Loop ledgers, tmux options, systemd units, or
Crontab.  Its only writes are the explicitly selected Execution-Facts database
and a content-free evidence report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote
import uuid

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionEvent,
    ExecutionSurface,
    LifecyclePhase,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_idempotency_key,
)
from hermes_cli.execution_facts_instrumentation import (
    ShadowValidationObservation,
    shadow_validation_event,
)
from hermes_cli.execution_facts_ledger import (
    EmitterMode,
    ExecutionFactsLedger,
    NonBlockingEmitter,
    emitter_snapshot_event,
)
from hermes_cli.execution_facts_projection import rebuild_projections
from hermes_cli.execution_facts_reconcile import (
    SystemInvocationObservation,
    reconcile_cron,
    reconcile_kanban,
    reconcile_loop_records,
    reconcile_system_invocations,
    reconcile_usage_facts,
)
from hermes_cli.execution_facts_terminal import (
    PaneObservation,
    SqliteIdentityStore,
    TmuxReconciliationAdapter,
    parse_tmux_list_panes,
)

SOURCE_CENSUS_VERSION = "execution-facts-source-census.v1"
DEFAULT_EVIDENCE_DIR = Path(
    "/mnt/data/hermes-observability/execution-facts-shadow"
)
DEFAULT_CRONTAB_WINDOW_DAYS = 30
DEFAULT_USAGE_SAMPLE_LIMIT = 200
# `(user) CMD (command)` - the only CRON line that marks an actual run.
_CRONTAB_COMMAND = re.compile(
    r"^\((?P<user>[^)]+)\)\s+CMD\s+\((?P<command>.*)\)\s*$"
)
_SAFE_JOB_LABEL = re.compile(r"[^A-Za-z0-9_.+-]+")
_SAFE_SCOPE = re.compile(r"[^A-Za-z0-9_.:+@/-]+")


@dataclass(frozen=True, slots=True)
class SourceCohort:
    """One exhaustive, explicitly bounded source observation."""

    source: str
    events: tuple[ExecutionEvent, ...]
    eligible: int | None
    identity_observed: int
    metric_observed: int
    eligibility_rule: str
    store_count: int
    read_errors: int
    window_start_ms: int
    window_end_ms: int
    behavior_equivalent: bool
    behavior_proof: str = "read_only_snapshot"

    def __post_init__(self) -> None:
        for name in ("identity_observed", "metric_observed", "store_count", "read_errors"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.eligible is not None:
            if (
                isinstance(self.eligible, bool)
                or not isinstance(self.eligible, int)
                or self.eligible < 0
            ):
                raise ValueError("eligible must be a non-negative integer or None")
            for name, value in (
                ("observed", self.observed),
                ("identity_observed", self.identity_observed),
                ("metric_observed", self.metric_observed),
            ):
                if value > self.eligible:
                    raise ValueError(
                        f"{name} cannot exceed the eligible source cohort"
                    )

    @property
    def observed(self) -> int:
        return len({event.execution_id for event in self.events})

    @property
    def validity(self) -> Validity:
        if self.eligible is None:
            return Validity.UNKNOWN
        if self.read_errors:
            return Validity.LOWER_BOUND
        return Validity.EXACT

    @property
    def identity_coverage_passed(self) -> bool:
        return (
            self.eligible is not None
            and self.eligible > 0
            and self.identity_observed == self.eligible
        )

    @property
    def metric_coverage_passed(self) -> bool:
        return (
            self.eligible is not None
            and self.eligible > 0
            and self.metric_observed == self.eligible
        )


@dataclass(frozen=True, slots=True)
class ShadowCollectionConfig:
    database: Path
    hermes_home: Path
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR
    usage_database: Path | None = None
    tmux_server_id: str = "default"
    include_systemd: bool = True
    include_crontab: bool = True
    crontab_window_days: int = DEFAULT_CRONTAB_WINDOW_DAYS
    subscription_fees: Mapping[str, object] | None = None
    fee_version: str | None = None
    usage_sample_limit: int = DEFAULT_USAGE_SAMPLE_LIMIT


@dataclass(frozen=True, slots=True)
class ShadowCollectionResult:
    scan_id: str
    database: str
    evidence_path: str
    cohorts: tuple[Mapping[str, Any], ...]
    collector: Mapping[str, Any]
    projection: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "computed_status": "ready",
            "schema_version": SOURCE_CENSUS_VERSION,
            "scan_id": self.scan_id,
            "database": self.database,
            "evidence_path": self.evidence_path,
            "cohorts": [dict(row) for row in self.cohorts],
            "collector": dict(self.collector),
            "projection": dict(self.projection),
            "activation_effect": "none",
        }


@dataclass(slots=True)
class _SystemCommandRunner:
    """Fixed-argv command boundary; tests replace this object."""

    timeout: float = 30.0

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )


def _safe_scope(value: str) -> str:
    normalized = _SAFE_SCOPE.sub("-", value.strip()).strip("-")
    return normalized[:200] or "unknown"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    # Pin COUNTs, rows, and identity adoption to one coherent source snapshot.
    connection.execute("BEGIN")
    return connection


def _sqlite_read_only_proof(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA query_only").fetchone()
    return bool(row and int(row[0]) == 1 and connection.total_changes == 0)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _kanban_eligible_execution_ids(
    connection: sqlite3.Connection,
) -> set[str] | None:
    """Return every run with authoritative execution activity."""
    task_run_columns = _columns(connection, "task_runs")
    if not {"id", "started_at"} <= task_run_columns:
        return None
    identifiers = {
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM task_runs WHERE started_at IS NOT NULL"
        )
    }
    for table in (
        "worker_run_timeline_events",
        "worker_run_terminal_facts",
        "worker_run_retry_links",
    ):
        if "task_run_id" not in _columns(connection, table):
            continue
        identifiers.update(
            str(row[0])
            for row in connection.execute(
                f'SELECT DISTINCT task_run_id FROM "{table}" '
                "WHERE task_run_id IS NOT NULL"
            )
        )
    return identifiers


def _event_window(
    events: Sequence[ExecutionEvent],
    *,
    fallback_start_ms: int,
    fallback_end_ms: int,
) -> tuple[int, int]:
    if not events:
        return fallback_start_ms, fallback_end_ms
    observed = [event.observed_at_ms for event in events]
    return min(observed), max(observed)


def _metric_execution_count(events: Sequence[ExecutionEvent]) -> int:
    return len(
        {
            event.execution_id
            for event in events
            if event.lifecycle_phase is not None
            or event.event_type is EventType.OUTCOME_OBSERVED
        }
    )


def _exact_identity_count(events: Sequence[ExecutionEvent]) -> int:
    return len(
        {
            event.source_execution_id
            for event in events
            if event.validity is Validity.EXACT
        }
    )


def _unknown_cohort(
    source: str,
    *,
    eligibility_rule: str,
    observed_at_ms: int,
    store_count: int = 0,
    read_errors: int = 1,
) -> SourceCohort:
    return SourceCohort(
        source=source,
        events=(),
        eligible=None,
        identity_observed=0,
        metric_observed=0,
        eligibility_rule=eligibility_rule,
        store_count=store_count,
        read_errors=read_errors,
        window_start_ms=observed_at_ms,
        window_end_ms=observed_at_ms,
        behavior_equivalent=False,
    )


def collect_kanban_cohort(
    database: Path,
    *,
    observed_at_ms: int,
) -> SourceCohort:
    """Read the authoritative root board without importing kanban_db."""
    if not database.is_file():
        return _unknown_cohort(
            "kanban_timeline",
            eligibility_rule="task_runs_with_authoritative_execution_activity",
            observed_at_ms=observed_at_ms,
            read_errors=0,
        )
    try:
        connection = _read_only_connection(database)
    except sqlite3.Error:
        return _unknown_cohort(
            "kanban_timeline",
            eligibility_rule="task_runs_with_authoritative_execution_activity",
            observed_at_ms=observed_at_ms,
        )
    try:
        eligible_ids = _kanban_eligible_execution_ids(connection)
        eligible = len(eligible_ids) if eligible_ids is not None else None
        events = tuple(reconcile_kanban(connection))
        behavior_equivalent = _sqlite_read_only_proof(connection)
    except sqlite3.Error:
        return _unknown_cohort(
            "kanban_timeline",
            eligibility_rule="task_runs_with_authoritative_execution_activity",
            observed_at_ms=observed_at_ms,
        )
    finally:
        connection.close()
    start_ms, end_ms = _event_window(
        events,
        fallback_start_ms=observed_at_ms,
        fallback_end_ms=observed_at_ms,
    )
    return SourceCohort(
        source="kanban_timeline",
        events=events,
        eligible=eligible,
        identity_observed=len(eligible_ids) if eligible_ids is not None else 0,
        metric_observed=_metric_execution_count(events),
        eligibility_rule="task_runs_with_authoritative_execution_activity",
        store_count=1,
        read_errors=0,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        behavior_equivalent=behavior_equivalent,
        behavior_proof="sqlite_query_only_snapshot",
    )


def _cron_databases(hermes_home: Path) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = [
        ("root", hermes_home / "cron" / "executions.db")
    ]
    profiles_root = hermes_home / "profiles"
    if profiles_root.is_dir():
        candidates.extend(
            (
                _safe_scope(path.parent.parent.name),
                path,
            )
            for path in sorted(profiles_root.glob("*/cron/executions.db"))
        )
    return tuple((scope, path) for scope, path in candidates if path.is_file())


def collect_cron_cohort(
    hermes_home: Path,
    *,
    observed_at_ms: int,
) -> SourceCohort:
    events: list[ExecutionEvent] = []
    eligible = 0
    errors = 0
    behavior_equivalent = True
    stores = _cron_databases(hermes_home)
    for profile, path in stores:
        try:
            connection = _read_only_connection(path)
        except sqlite3.Error:
            errors += 1
            continue
        try:
            eligible += int(
                connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            )
            adapted = reconcile_cron(connection, profile=profile)
            events.extend(adapted)
            behavior_equivalent = (
                behavior_equivalent and _sqlite_read_only_proof(connection)
            )
        except sqlite3.Error:
            errors += 1
        finally:
            connection.close()
    start_ms, end_ms = _event_window(
        events,
        fallback_start_ms=observed_at_ms,
        fallback_end_ms=observed_at_ms,
    )
    return SourceCohort(
        source="hermes_cron",
        events=tuple(events),
        eligible=eligible if stores and errors == 0 else None,
        identity_observed=eligible if stores and errors == 0 else _exact_identity_count(events),
        metric_observed=_metric_execution_count(events),
        eligibility_rule="all_rows_in_visible_cron_execution_stores",
        store_count=len(stores),
        read_errors=errors,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        behavior_equivalent=behavior_equivalent and errors == 0,
        behavior_proof="sqlite_query_only_snapshot",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("loop JSONL records must be objects")
            records.append(value)
    return records


def _prefix_digest(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = size
        while remaining:
            chunk = handle.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def collect_loop_cohort(
    hermes_home: Path,
    *,
    observed_at_ms: int,
) -> SourceCohort:
    paths = sorted((hermes_home / "loops").glob("*/ledger.jsonl"))
    events: list[ExecutionEvent] = []
    eligible_ids: set[str] = set()
    exact_ids: set[str] = set()
    errors = 0
    behavior_equivalent = True
    for path in paths:
        try:
            initial_size = path.stat().st_size
            before = _prefix_digest(path, initial_size)
            records = _read_jsonl(path)
            adapted = reconcile_loop_records(
                [
                    {
                        **record,
                        "pack": record.get("pack") or path.parent.name,
                    }
                    for record in records
                ]
            )
            after = _prefix_digest(path, initial_size)
        except (OSError, json.JSONDecodeError, ValueError):
            errors += 1
            continue
        events.extend(adapted)
        behavior_equivalent = behavior_equivalent and before == after
        eligible_ids.update(event.source_execution_id for event in adapted)
        exact_ids.update(
            event.source_execution_id
            for event in adapted
            if event.validity is Validity.EXACT
        )
    start_ms, end_ms = _event_window(
        events,
        fallback_start_ms=observed_at_ms,
        fallback_end_ms=observed_at_ms,
    )
    return SourceCohort(
        source="loop_ledger",
        events=tuple(events),
        eligible=len(eligible_ids) if paths and errors == 0 else None,
        identity_observed=len(exact_ids),
        metric_observed=_metric_execution_count(events),
        eligibility_rule="structured_loop_records_grouped_by_run_identity",
        store_count=len(paths),
        read_errors=errors,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        behavior_equivalent=behavior_equivalent and errors == 0,
        behavior_proof="immutable_prefix_snapshot",
    )


def _tmux_argv(server_id: str) -> list[str]:
    argv = ["tmux"]
    if server_id != "default":
        argv.extend(["-L", server_id])
    argv.extend(
        [
            "list-panes",
            "-a",
            "-F",
            (
                "#{session_id}\x1f#{window_id}\x1f#{pane_id}\x1f"
                "#{pane_pid}\x1f#{pane_dead}\x1f"
                "#{@hermes_terminal_run_id}"
            ),
        ]
    )
    return argv


def collect_tmux_cohort(
    database: Path,
    *,
    server_id: str,
    observed_at_ms: int,
    runner: _SystemCommandRunner,
) -> SourceCohort:
    argv = _tmux_argv(server_id)
    first = runner.run(argv)
    if first.returncode != 0:
        return _unknown_cohort(
            "tmux_reconciliation",
            eligibility_rule="all_panes_on_selected_tmux_server",
            observed_at_ms=observed_at_ms,
        )
    try:
        first_observations = parse_tmux_list_panes(
            first.stdout,
            observed_at_ms=observed_at_ms,
            server_id=server_id,
        )
        store = SqliteIdentityStore(database)
        store.initialize()
        adapter = TmuxReconciliationAdapter(store)
        events = list(adapter.reconcile(first_observations))
    except (ValueError, sqlite3.Error):
        return _unknown_cohort(
            "tmux_reconciliation",
            eligibility_rule="all_panes_on_selected_tmux_server",
            observed_at_ms=observed_at_ms,
        )
    # A second neutral snapshot proves behavior equivalence. It does not mint
    # extra source events or synthetic observation times just to satisfy a
    # sample gate; small live cohorts must accumulate real scheduled scans.
    second = runner.run(argv)
    read_errors = int(second.returncode != 0)
    stable_locators = second.returncode == 0 and sorted(
        first.stdout.splitlines()
    ) == sorted(second.stdout.splitlines())
    marker_count = sum(
        bool(observation.durable_terminal_run_id)
        for observation in first_observations
    )
    return SourceCohort(
        source="tmux_reconciliation",
        events=tuple(events),
        eligible=(
            len(first_observations)
            if read_errors == 0 and stable_locators
            else None
        ),
        identity_observed=marker_count,
        metric_observed=len(
            {
                event.execution_id
                for event in events
                if event.event_type is EventType.TERMINAL_OBSERVED
            }
        ),
        eligibility_rule="all_panes_on_selected_tmux_server",
        store_count=1,
        read_errors=read_errors,
        window_start_ms=observed_at_ms,
        window_end_ms=observed_at_ms,
        behavior_equivalent=stable_locators and read_errors == 0,
        behavior_proof="double_read_neutral_locator_snapshot",
    )


def _systemd_realtime_epoch_ms(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    for pattern in (
        "%a %Y-%m-%d %H:%M:%S.%f UTC",
        "%a %Y-%m-%d %H:%M:%S UTC",
    ):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
        return int(parsed.timestamp() * 1000)
    raise ValueError("invalid systemd realtime timestamp")


def _parse_systemctl_properties(text: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def collect_systemd_cohort(
    *,
    observed_at_ms: int,
    runner: _SystemCommandRunner,
) -> SourceCohort:
    listed = runner.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ]
    )
    if listed.returncode != 0:
        return _unknown_cohort(
            "systemd_invocation",
            eligibility_rule="latest_retained_invocation_per_user_service",
            observed_at_ms=observed_at_ms,
        )
    units = sorted(
        {
            line.split()[0]
            for line in listed.stdout.splitlines()
            if line.strip() and line.split()[0].endswith(".service")
        }
    )
    observations: list[SystemInvocationObservation] = []
    eligible_ids: set[str] = set()
    errors = 0
    for unit in units:
        shown = runner.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=InvocationID",
                "--property=ExecMainStartTimestamp",
                "--property=ExecMainExitTimestamp",
                "--property=Result",
                "--property=ExecMainStatus",
                "--timestamp=us+utc",
                "--no-pager",
            ]
        )
        if shown.returncode != 0:
            errors += 1
            continue
        properties = _parse_systemctl_properties(shown.stdout)
        required_properties = {
            "InvocationID",
            "ExecMainStartTimestamp",
            "ExecMainExitTimestamp",
            "Result",
            "ExecMainStatus",
        }
        if not required_properties <= properties.keys():
            errors += 1
            continue
        invocation_id = properties["InvocationID"].strip()
        try:
            started_at_ms = _systemd_realtime_epoch_ms(
                properties["ExecMainStartTimestamp"]
            )
            ended_at_ms = _systemd_realtime_epoch_ms(
                properties["ExecMainExitTimestamp"]
            )
        except ValueError:
            errors += 1
            continue
        if not invocation_id:
            if started_at_ms is not None or ended_at_ms is not None:
                errors += 1
            continue
        if started_at_ms is None:
            errors += 1
            continue
        source_execution_id = (
            f"systemd:{_safe_scope(unit)}:{_safe_scope(invocation_id)}"
        )
        eligible_ids.add(source_execution_id)
        observations.append(
            SystemInvocationObservation(
                source_execution_id=source_execution_id,
                surface=ExecutionSurface.SYSTEMD,
                observed_at_ms=started_at_ms,
                phase=LifecyclePhase.PROCESS_STARTED,
            )
        )
        if ended_at_ms is not None:
            raw_exit_code = properties["ExecMainStatus"]
            raw_result = properties["Result"]
            if not raw_exit_code or not raw_result:
                errors += 1
                continue
            try:
                exit_code = int(raw_exit_code)
            except ValueError:
                errors += 1
                continue
            observations.append(
                SystemInvocationObservation(
                    source_execution_id=source_execution_id,
                    surface=ExecutionSurface.SYSTEMD,
                    observed_at_ms=ended_at_ms,
                    phase=LifecyclePhase.ENDED,
                    status=_safe_scope(raw_result),
                    exit_code=exit_code,
                )
            )
    events = tuple(reconcile_system_invocations(observations))
    start_ms, end_ms = _event_window(
        events,
        fallback_start_ms=observed_at_ms,
        fallback_end_ms=observed_at_ms,
    )
    return SourceCohort(
        source="systemd_invocation",
        events=events,
        eligible=len(eligible_ids) if errors == 0 else None,
        identity_observed=len(eligible_ids),
        metric_observed=_metric_execution_count(events),
        eligibility_rule="latest_retained_invocation_per_user_service",
        store_count=1,
        read_errors=errors,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        behavior_equivalent=errors == 0,
        behavior_proof="fixed_read_only_commands",
    )


def _journal_message(raw: object) -> str:
    """Decode a journal MESSAGE, which may arrive as a byte array."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        return bytes(int(byte) for byte in raw).decode("utf-8", "replace")
    raise TypeError("unsupported MESSAGE encoding")


def _crontab_job_scope(message: str) -> str | None:
    """Identify which cron job a journal line belongs to, if any.

    CRON writes three kinds of line: the command it ran, PAM session
    bookkeeping, and daemon notices. Only the first is an execution, and it
    carries the invoking user plus the command -- enough to attribute the run
    to a specific job instead of to an anonymous process.

    The command is hashed into the identity so that a long or path-heavy
    crontab entry cannot bloat or leak through the scope, while a readable
    prefix keeps the identity diagnosable by eye.
    """
    match = _CRONTAB_COMMAND.match(message)
    if match is None:
        return None
    user = _safe_scope(match.group("user"))
    command = match.group("command").strip()
    if not command:
        return None
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    label = _SAFE_JOB_LABEL.sub("-", Path(command.split()[0]).name)[:40]
    # One segment, because evidence references allow only a few colon-
    # separated parts and the surface and phase already claim two of them.
    return f"crontab:{user}-{label or 'job'}-{digest}"


def collect_crontab_cohort(
    *,
    observed_at_ms: int,
    window_days: int,
    runner: _SystemCommandRunner,
) -> SourceCohort:
    result = runner.run(
        [
            "journalctl",
            "--since",
            f"{window_days} days ago",
            "SYSLOG_IDENTIFIER=CRON",
            "-o",
            "json",
            "--output-fields=_BOOT_ID,_PID,__REALTIME_TIMESTAMP,MESSAGE",
            "--no-pager",
        ]
    )
    window_start_ms = observed_at_ms - window_days * 86_400_000
    if result.returncode != 0:
        return _unknown_cohort(
            "crontab_invocation",
            eligibility_rule="cron_command_invocations_in_journal_window",
            observed_at_ms=observed_at_ms,
        )
    invocations: list[tuple[str, int]] = []
    errors = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            timestamp_ms = int(record["__REALTIME_TIMESTAMP"]) // 1000
            message = _journal_message(record["MESSAGE"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            errors += 1
            continue
        job_scope = _crontab_job_scope(message)
        if job_scope is None:
            # Session bookkeeping and daemon notices are not job runs. On this
            # host they were 67% of every line CRON writes; counting them made
            # the source look three times busier than it is.
            continue
        invocations.append((f"{job_scope}:{timestamp_ms}", timestamp_ms))
    observations = [
        SystemInvocationObservation(
            source_execution_id=_safe_scope(source_execution_id),
            surface=ExecutionSurface.CRONTAB,
            observed_at_ms=started_at_ms,
            phase=LifecyclePhase.PROCESS_STARTED,
            # The journal names the command, so the run is exactly attributed
            # to a job rather than guessed from process bookkeeping.
            validity=Validity.EXACT,
        )
        for source_execution_id, started_at_ms in sorted(invocations)
    ]
    events = tuple(reconcile_system_invocations(observations))
    return SourceCohort(
        source="crontab_invocation",
        events=events,
        eligible=len(invocations) if errors == 0 else None,
        identity_observed=len(invocations),
        metric_observed=_metric_execution_count(events),
        eligibility_rule="cron_command_invocations_in_journal_window",
        store_count=1,
        read_errors=errors,
        window_start_ms=window_start_ms,
        window_end_ms=observed_at_ms,
        behavior_equivalent=errors == 0,
        behavior_proof="fixed_read_only_commands",
    )


def collect_usage_cohort(
    database: Path | None,
    *,
    observed_at_ms: int,
    subscription_fees: Mapping[str, object] | None = None,
    fee_version: str | None = None,
    sample_limit: int = DEFAULT_USAGE_SAMPLE_LIMIT,
) -> SourceCohort:
    if database is None or not database.is_file():
        return _unknown_cohort(
            "usage_facts",
            eligibility_rule="all_rows_in_usage_facts_store",
            observed_at_ms=observed_at_ms,
            read_errors=0,
        )
    try:
        connection = _read_only_connection(database)
    except sqlite3.Error:
        return _unknown_cohort(
            "usage_facts",
            eligibility_rule="all_rows_in_usage_facts_store",
            observed_at_ms=observed_at_ms,
        )
    try:
        eligible = int(
            connection.execute(
                "SELECT COUNT(*) FROM run_usage_facts"
            ).fetchone()[0]
        )
        identity_observed = int(
            connection.execute(
                "SELECT COUNT(run_id) FROM run_usage_facts"
            ).fetchone()[0]
        )
        events = tuple(
            reconcile_usage_facts(
                connection,
                subscription_fees=subscription_fees,
                fee_version=fee_version,
                limit=None if subscription_fees else sample_limit,
            )
        )
        metric_observed = sum(
            event.event_type is EventType.USAGE_OBSERVED
            for event in events
        )
        behavior_equivalent = _sqlite_read_only_proof(connection)
    except (sqlite3.Error, ValueError):
        return _unknown_cohort(
            "usage_facts",
            eligibility_rule="all_rows_in_usage_facts_store",
            observed_at_ms=observed_at_ms,
        )
    finally:
        connection.close()
    start_ms, end_ms = _event_window(
        events,
        fallback_start_ms=observed_at_ms,
        fallback_end_ms=observed_at_ms,
    )
    return SourceCohort(
        source="usage_facts",
        events=events,
        eligible=eligible,
        identity_observed=identity_observed,
        metric_observed=metric_observed,
        eligibility_rule="all_usage_fact_rows_with_bounded_ledger_sample",
        store_count=1,
        read_errors=0,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        behavior_equivalent=behavior_equivalent,
        behavior_proof="sqlite_query_only_snapshot",
    )


def source_census_event(
    cohort: SourceCohort,
    *,
    scan_id: str,
    observed_at_ms: int,
    writer_p50_us: int | None,
    writer_p95_us: int | None,
    dedupe_reconciled: bool,
    missing_links: int = 0,
    orphans: int = 0,
) -> ExecutionEvent:
    return make_event(
        source="execution_facts_census",
        source_execution_id=f"census:{cohort.source}:{scan_id}",
        idempotency_key=stable_idempotency_key(
            "execution_facts_census",
            cohort.source,
            scan_id,
        ),
        event_type=EventType.SOURCE_CENSUS,
        validity=cohort.validity,
        observed_at_ms=observed_at_ms,
        recorded_at_ms=observed_at_ms,
        workload_kind=WorkloadKind.SYSTEM_JOB,
        execution_surface=ExecutionSurface.PROCESS,
        trigger_kind=TriggerKind.RECONCILIATION,
        status="shadow",
        evidence_ref=f"shadow:{scan_id}:{cohort.source}",
        attributes={
            "validated_source": cohort.source,
            "eligible": cohort.eligible,
            "observed": cohort.observed,
            "identity_observed": cohort.identity_observed,
            "metric_observed": cohort.metric_observed,
            "eligibility_rule": cohort.eligibility_rule,
            "source_store_count": cohort.store_count,
            "source_read_errors": cohort.read_errors,
            "window_start_ms": cohort.window_start_ms,
            "window_end_ms": cohort.window_end_ms,
            "source_contract_version": SOURCE_CENSUS_VERSION,
            "writer_p50_us": writer_p50_us,
            "writer_p95_us": writer_p95_us,
            "dedupe_reconciled": dedupe_reconciled,
            "behavior_equivalent": cohort.behavior_equivalent,
            "behavior_proof": cohort.behavior_proof,
            "missing_links": missing_links,
            "orphans": orphans,
        },
    )


def _percentile_us(
    values_ns: Sequence[int], percentile: int
) -> int | None:
    if not values_ns:
        return None
    ordered = sorted(values_ns)
    index = max(
        0,
        min(
            len(ordered) - 1,
            math.ceil((percentile / 100) * len(ordered)) - 1,
        ),
    )
    return max(0, math.ceil(ordered[index] / 1000))


def _source_event_counts(database: Path) -> dict[str, int]:
    connection = _read_only_connection(database)
    try:
        if not _table_exists(connection, "execution_source_projection"):
            return {}
        return {
            str(row["source"]): int(row["event_count"])
            for row in connection.execute(
                "SELECT source, event_count FROM execution_source_projection"
            )
        }
    finally:
        connection.close()


def _evidence_payload(
    *,
    scan_id: str,
    observed_at_ms: int,
    cohorts: Sequence[SourceCohort],
    timings: Mapping[str, Sequence[int]],
    dedupe_by_source: Mapping[str, bool],
    collector: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SOURCE_CENSUS_VERSION,
        "scan_id": scan_id,
        "observed_at_ms": observed_at_ms,
        "mode": "shadow",
        "activation_effect": "none",
        "cohorts": [
            {
                "source": cohort.source,
                "eligible": cohort.eligible,
                "observed": cohort.observed,
                "identity_observed": cohort.identity_observed,
                "metric_observed": cohort.metric_observed,
                "sample_events": len(cohort.events),
                "eligibility_rule": cohort.eligibility_rule,
                "source_contract_version": SOURCE_CENSUS_VERSION,
                "window_start_ms": cohort.window_start_ms,
                "window_end_ms": cohort.window_end_ms,
                "source_store_count": cohort.store_count,
                "source_read_errors": cohort.read_errors,
                "writer_p50_us": _percentile_us(
                    timings.get(cohort.source, ()), 50
                ),
                "writer_p95_us": _percentile_us(
                    timings.get(cohort.source, ()), 95
                ),
                "dedupe_reconciled": bool(
                    dedupe_by_source.get(cohort.source, False)
                ),
                "behavior_equivalent": cohort.behavior_equivalent,
                "behavior_proof": cohort.behavior_proof,
                "validity": cohort.validity.value,
            }
            for cohort in cohorts
        ],
        "collector": dict(collector),
        "projection": dict(projection),
    }


def _write_evidence(
    directory: Path,
    *,
    scan_id: str,
    payload: Mapping[str, Any],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{scan_id}.json"
    temporary = directory / f".{scan_id}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def collect_shadow(
    config: ShadowCollectionConfig,
    *,
    runner: _SystemCommandRunner | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> ShadowCollectionResult:
    """Collect every configured source in the fixed rollout order."""
    command_runner = runner or _SystemCommandRunner()
    now_ms = (
        clock_ms()
        if clock_ms is not None
        else int(datetime.now(UTC).timestamp() * 1000)
    )
    scan_id = (
        datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime(
            "%Y%m%dT%H%M%S"
        )
        + "-"
        + uuid.uuid4().hex[:8]
    )
    cohorts: list[SourceCohort] = [
        collect_kanban_cohort(
            config.hermes_home / "kanban.db",
            observed_at_ms=now_ms,
        ),
        collect_cron_cohort(config.hermes_home, observed_at_ms=now_ms),
        collect_loop_cohort(config.hermes_home, observed_at_ms=now_ms),
    ]

    ledger = ExecutionFactsLedger(config.database)
    ledger.initialize()
    cohorts.append(
        collect_tmux_cohort(
            config.database,
            server_id=config.tmux_server_id,
            observed_at_ms=now_ms,
            runner=command_runner,
        )
    )
    if config.include_systemd:
        cohorts.append(
            collect_systemd_cohort(
                observed_at_ms=now_ms,
                runner=command_runner,
            )
        )
    if config.include_crontab:
        cohorts.append(
            collect_crontab_cohort(
                observed_at_ms=now_ms,
                window_days=config.crontab_window_days,
                runner=command_runner,
            )
        )
    if config.usage_database is not None:
        cohorts.append(
            collect_usage_cohort(
                config.usage_database,
                observed_at_ms=now_ms,
                subscription_fees=config.subscription_fees,
                fee_version=config.fee_version,
                sample_limit=config.usage_sample_limit,
            )
        )

    events = [event for cohort in cohorts for event in cohort.events]
    # Exercise the real fail-open writer with a bounded, source-balanced probe.
    # Historical reconciliation itself is an offline batch and must not turn
    # thousands of old facts into thousands of source-hot-path transactions.
    probe_events: list[ExecutionEvent] = []
    for cohort in cohorts:
        source_probe = list(cohort.events[:20])
        if source_probe:
            source_probe_basis = tuple(source_probe)
            source_probe.extend(
                source_probe_basis[index % len(source_probe_basis)]
                for index in range(20 - len(source_probe_basis))
            )
        probe_events.extend(source_probe)
    emitter = NonBlockingEmitter(
        ledger.append,
        mode=EmitterMode.SHADOW,
        capacity=max(64, len(probe_events) + 16),
        thread_name=f"execution-facts-shadow-{scan_id}",
    )
    emitter.start()
    timings: dict[str, list[int]] = {}
    accepted_by_source: dict[str, int] = {}
    for event in probe_events:
        started_ns = time.perf_counter_ns()
        accepted = emitter.try_emit(event)
        elapsed_ns = time.perf_counter_ns() - started_ns
        timings.setdefault(event.source, []).append(elapsed_ns)
        if accepted:
            accepted_by_source[event.source] = (
                accepted_by_source.get(event.source, 0) + 1
            )
    if not emitter.wait_until_idle(timeout=300):
        raise RuntimeError("Shadow emitter did not drain within 300 seconds")
    snapshot = emitter.snapshot()
    if not emitter.shutdown(timeout=30):
        raise RuntimeError("Shadow emitter did not shut down cleanly")

    # Fail open: a single source whose identity turned out to be unstable
    # must not roll back every other source's facts. Conflicts are counted
    # per source and reported instead of aborting the sweep.
    bulk_pass = ledger.append_batch(events, on_conflict="skip")
    second_pass = ledger.append_batch(events, on_conflict="skip")
    conflicts_by_source: dict[str, int] = {}
    for result, event in zip(bulk_pass, events, strict=True):
        if result.conflicted:
            conflicts_by_source[event.source] = (
                conflicts_by_source.get(event.source, 0) + 1
            )
    dedupe_by_source = {
        cohort.source: bool(cohort.events)
        and not conflicts_by_source.get(cohort.source)
        and all(
            not result.inserted
            for result, event in zip(second_pass, events, strict=True)
            if event.source == cohort.source
        )
        for cohort in cohorts
    }
    first_projection = rebuild_projections(config.database)
    second_projection = rebuild_projections(config.database)
    projection_reproducible = (
        first_projection.to_dict() == second_projection.to_dict()
    )
    dedupe_by_source = {
        source: value and projection_reproducible
        for source, value in dedupe_by_source.items()
    }

    source_counts = _source_event_counts(config.database)
    census_events: list[ExecutionEvent] = []
    validation_events: list[ExecutionEvent] = []
    cohort_rows: list[dict[str, Any]] = []
    for cohort in cohorts:
        p50_us = _percentile_us(timings.get(cohort.source, ()), 50)
        p95_us = _percentile_us(timings.get(cohort.source, ()), 95)
        dedupe_reconciled = bool(
            dedupe_by_source.get(cohort.source, False)
        )
        census_events.append(
            source_census_event(
                cohort,
                scan_id=scan_id,
                observed_at_ms=now_ms,
                writer_p50_us=p50_us,
                writer_p95_us=p95_us,
                dedupe_reconciled=dedupe_reconciled,
            )
        )
        cumulative_samples = int(source_counts.get(cohort.source, 0))
        if cumulative_samples > 0 and p95_us is not None:
            validation_events.append(
                shadow_validation_event(
                    ShadowValidationObservation(
                        validated_source=cohort.source,
                        observed_at_ms=now_ms,
                        sample_size=cumulative_samples,
                        identity_coverage_passed=(
                            cohort.identity_coverage_passed
                        ),
                        metric_coverage_passed=(
                            cohort.metric_coverage_passed
                        ),
                        capture_p95_us=p95_us,
                        dedupe_reconciled=dedupe_reconciled,
                        behavior_equivalent=cohort.behavior_equivalent,
                        evidence_ref=f"shadow:{scan_id}:{cohort.source}",
                    )
                )
            )
        cohort_rows.append(
            {
                "source": cohort.source,
                "eligible": cohort.eligible,
                "observed": cohort.observed,
                "identity_observed": cohort.identity_observed,
                "metric_observed": cohort.metric_observed,
                "sample_events": cumulative_samples,
                "validity": cohort.validity.value,
                "writer_p50_us": p50_us,
                "writer_p95_us": p95_us,
                "dedupe_reconciled": dedupe_reconciled,
                "behavior_equivalent": cohort.behavior_equivalent,
            }
        )

    collector_event = emitter_snapshot_event(
        snapshot,
        collector_id=scan_id,
        observed_at_ms=now_ms,
    )
    ledger.append_batch(
        (*census_events, *validation_events, collector_event)
    )
    final_projection = rebuild_projections(config.database)
    collector_row = {
        "mode": snapshot.mode,
        "submitted": snapshot.submitted,
        "accepted": snapshot.accepted,
        "dropped": (
            snapshot.dropped_disabled
            + snapshot.dropped_full
            + snapshot.dropped_write_error
        ),
        "written": snapshot.written,
        "deduped": snapshot.deduped,
        "queue_capacity": snapshot.queue_capacity,
        "projection_reproducible": projection_reproducible,
        "accepted_by_source": accepted_by_source,
        "probe_events": len(probe_events),
        "reconciled_events": len(events),
        "reconciled_inserted": (
            snapshot.written
            + sum(result.inserted for result in bulk_pass)
        ),
        # Non-zero means some source restated a fact under an identity it had
        # already used. The retained fact wins and the sweep continues, but
        # that source's identity rule is broken and must be fixed.
        "identity_conflicts": sum(conflicts_by_source.values()),
        "identity_conflicts_by_source": conflicts_by_source,
    }
    projection_row = final_projection.to_dict()
    evidence = _evidence_payload(
        scan_id=scan_id,
        observed_at_ms=now_ms,
        cohorts=cohorts,
        timings=timings,
        dedupe_by_source=dedupe_by_source,
        collector=collector_row,
        projection=projection_row,
    )
    evidence_path = _write_evidence(
        config.evidence_dir,
        scan_id=scan_id,
        payload=evidence,
    )
    return ShadowCollectionResult(
        scan_id=scan_id,
        database=str(config.database),
        evidence_path=str(evidence_path),
        cohorts=tuple(cohort_rows),
        collector=collector_row,
        projection=projection_row,
    )


__all__ = (
    "DEFAULT_CRONTAB_WINDOW_DAYS",
    "DEFAULT_EVIDENCE_DIR",
    "DEFAULT_USAGE_SAMPLE_LIMIT",
    "SOURCE_CENSUS_VERSION",
    "ShadowCollectionConfig",
    "ShadowCollectionResult",
    "SourceCohort",
    "collect_cron_cohort",
    "collect_crontab_cohort",
    "collect_kanban_cohort",
    "collect_loop_cohort",
    "collect_shadow",
    "collect_systemd_cohort",
    "collect_tmux_cohort",
    "collect_usage_cohort",
    "source_census_event",
)

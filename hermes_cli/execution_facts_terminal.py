"""Content-free, read-only reconciliation for tmux pane observations.

The adapter deliberately accepts observations from an injected runner.  It
never invokes ``tmux`` itself, captures a pane, reads an argv, or writes tmux
options.  A caller that is allowed to inspect tmux supplies only the neutral
locator fields below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionEvent,
    ExecutionSurface,
    LifecyclePhase,
    SpanKind,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_execution_id,
    stable_idempotency_key,
    stable_span_id,
)


_SAFE_METADATA = frozenset(
    {
        "task_run_id",
        "span_id",
        "parent_span_id",
        "workload_kind",
        "span_kind",
        "agent_kind",
        "operator_kind",
        "binding_state",
    }
)
_FORBIDDEN_METADATA_TOKENS = ("content", "command", "argv", "prompt", "output", "log")
_DURABLE_MARKER = re.compile(
    r"(?:[a-f0-9]{32}|tr_[a-f0-9]{32}|"
    r"shadow-terminal-[0-9]{1,12}-[0-9]{8})"
)


@dataclass(frozen=True)
class PaneObservation:
    """A content-free observation of one tmux pane at one instant."""

    server_id: str
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int | None
    observed_at_ms: int
    dead: bool = False
    durable_terminal_run_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def locator_key(self) -> str:
        return "\x1f".join((self.server_id, self.session_id, self.window_id, self.pane_id))

    def safe_metadata(self) -> dict[str, object]:
        """Return explicit identity metadata, rejecting content-shaped keys."""
        result: dict[str, object] = {}
        for key, value in self.metadata.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(token in lowered for token in _FORBIDDEN_METADATA_TOKENS):
                raise ValueError(f"content-shaped pane metadata is forbidden: {key_text}")
            if key_text in _SAFE_METADATA and value is not None:
                result[key_text] = value
        return result


@dataclass(frozen=True)
class TerminalAction:
    """An explicit close/archive action, never inferred from inactivity."""

    terminal_run_id: str
    action: str
    observed_at_ms: int

    def __post_init__(self) -> None:
        if self.action not in {"close", "archive"}:
            raise ValueError("terminal action must be close or archive")


@dataclass(frozen=True)
class TaskRunBinding:
    terminal_run_id: str
    task_run_id: str
    started_at_ms: int
    generation: int
    ended_at_ms: int | None = None


@dataclass
class TerminalIdentity:
    terminal_run_id: str
    first_observed_at_ms: int
    generation: int = 1
    current_pane_id: str | None = None
    current_generation_started_at_ms: int | None = None
    current_generation_ended_at_ms: int | None = None
    closed_at_ms: int | None = None
    archived_at_ms: int | None = None
    retired_pane_ids: list[str] = field(default_factory=list)
    bindings: list[TaskRunBinding] = field(default_factory=list)


class IdentityStore(Protocol):
    """Isolated persistence boundary; the adapter never stamps live tmux."""

    def get_by_run_id(self, terminal_run_id: str) -> TerminalIdentity | None: ...

    def get_by_pane_locator(self, locator_key: str) -> TerminalIdentity | None: ...

    def save(self, identity: TerminalIdentity, *, locator_key: str | None = None) -> None: ...

    def clear_locators(self, terminal_run_id: str) -> None: ...


class MemoryIdentityStore:
    """Small deterministic store suitable for tests and isolated adapters."""

    def __init__(self) -> None:
        self.by_run_id: dict[str, TerminalIdentity] = {}
        self.by_locator: dict[str, str] = {}

    def get_by_run_id(self, terminal_run_id: str) -> TerminalIdentity | None:
        return self.by_run_id.get(terminal_run_id)

    def get_by_pane_locator(self, locator_key: str) -> TerminalIdentity | None:
        run_id = self.by_locator.get(locator_key)
        return self.by_run_id.get(run_id) if run_id else None

    def save(self, identity: TerminalIdentity, *, locator_key: str | None = None) -> None:
        self.by_run_id[identity.terminal_run_id] = identity
        if locator_key:
            self.by_locator[locator_key] = identity.terminal_run_id

    def clear_locators(self, terminal_run_id: str) -> None:
        self.by_locator = {
            locator: run_id
            for locator, run_id in self.by_locator.items()
            if run_id != terminal_run_id
        }


class SqliteIdentityStore:
    """Durable identity state inside the isolated execution-facts database."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS terminal_identity_state (
        terminal_run_id TEXT PRIMARY KEY,
        first_observed_at_ms INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        current_pane_id TEXT,
        current_generation_started_at_ms INTEGER,
        current_generation_ended_at_ms INTEGER,
        closed_at_ms INTEGER,
        archived_at_ms INTEGER,
        retired_pane_ids_json TEXT NOT NULL DEFAULT '[]',
        bindings_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS terminal_locator_state (
        locator_sha256 TEXT PRIMARY KEY,
        terminal_run_id TEXT NOT NULL
    );
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(self._SCHEMA)
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(terminal_identity_state)"
                )
            }
            if "current_generation_started_at_ms" not in columns:
                connection.execute(
                    """
                    ALTER TABLE terminal_identity_state
                    ADD COLUMN current_generation_started_at_ms INTEGER
                    """
                )
                connection.execute(
                    """
                    UPDATE terminal_identity_state
                       SET current_generation_started_at_ms =
                           first_observed_at_ms
                     WHERE generation = 1
                    """
                )
            if "retired_pane_ids_json" not in columns:
                connection.execute(
                    """
                    ALTER TABLE terminal_identity_state
                    ADD COLUMN retired_pane_ids_json
                    TEXT NOT NULL DEFAULT '[]'
                    """
                )

    def get_by_run_id(self, terminal_run_id: str) -> TerminalIdentity | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT terminal_run_id, first_observed_at_ms, generation,
                       current_pane_id, current_generation_started_at_ms,
                       current_generation_ended_at_ms,
                       closed_at_ms, archived_at_ms,
                       retired_pane_ids_json, bindings_json
                  FROM terminal_identity_state
                 WHERE terminal_run_id = ?
                """,
                (terminal_run_id,),
            ).fetchone()
        return self._identity(row) if row is not None else None

    def get_by_pane_locator(self, locator_key: str) -> TerminalIdentity | None:
        digest = self._locator_digest(locator_key)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT terminal_run_id
                  FROM terminal_locator_state
                 WHERE locator_sha256 = ?
                """,
                (digest,),
            ).fetchone()
        return self.get_by_run_id(str(row[0])) if row is not None else None

    def save(
        self, identity: TerminalIdentity, *, locator_key: str | None = None
    ) -> None:
        retired_pane_ids_json = json.dumps(
            sorted(set(identity.retired_pane_ids)),
            separators=(",", ":"),
        )
        bindings_json = json.dumps(
            [
                {
                    "terminal_run_id": binding.terminal_run_id,
                    "task_run_id": binding.task_run_id,
                    "started_at_ms": binding.started_at_ms,
                    "generation": binding.generation,
                    "ended_at_ms": binding.ended_at_ms,
                }
                for binding in identity.bindings
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO terminal_identity_state (
                    terminal_run_id, first_observed_at_ms, generation,
                    current_pane_id, current_generation_started_at_ms,
                    current_generation_ended_at_ms,
                    closed_at_ms, archived_at_ms,
                    retired_pane_ids_json, bindings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(terminal_run_id) DO UPDATE SET
                    first_observed_at_ms = excluded.first_observed_at_ms,
                    generation = excluded.generation,
                    current_pane_id = excluded.current_pane_id,
                    current_generation_started_at_ms =
                        excluded.current_generation_started_at_ms,
                    current_generation_ended_at_ms =
                        excluded.current_generation_ended_at_ms,
                    closed_at_ms = excluded.closed_at_ms,
                    archived_at_ms = excluded.archived_at_ms,
                    retired_pane_ids_json =
                        excluded.retired_pane_ids_json,
                    bindings_json = excluded.bindings_json
                """,
                (
                    identity.terminal_run_id,
                    identity.first_observed_at_ms,
                    identity.generation,
                    identity.current_pane_id,
                    identity.current_generation_started_at_ms,
                    identity.current_generation_ended_at_ms,
                    identity.closed_at_ms,
                    identity.archived_at_ms,
                    retired_pane_ids_json,
                    bindings_json,
                ),
            )
            if locator_key is not None:
                connection.execute(
                    """
                    INSERT INTO terminal_locator_state (
                        locator_sha256, terminal_run_id
                    ) VALUES (?, ?)
                    ON CONFLICT(locator_sha256) DO UPDATE SET
                        terminal_run_id = excluded.terminal_run_id
                    """,
                    (
                        self._locator_digest(locator_key),
                        identity.terminal_run_id,
                    ),
                )

    def clear_locators(self, terminal_run_id: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                DELETE FROM terminal_locator_state
                 WHERE terminal_run_id = ?
                """,
                (terminal_run_id,),
            )

    @staticmethod
    def _locator_digest(locator_key: str) -> str:
        return hashlib.sha256(locator_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _identity(row: sqlite3.Row) -> TerminalIdentity:
        bindings = [
            TaskRunBinding(
                terminal_run_id=str(item["terminal_run_id"]),
                task_run_id=str(item["task_run_id"]),
                started_at_ms=int(item["started_at_ms"]),
                generation=int(item.get("generation", 1)),
                ended_at_ms=(
                    int(item["ended_at_ms"])
                    if item.get("ended_at_ms") is not None
                    else None
                ),
            )
            for item in json.loads(str(row["bindings_json"]))
        ]
        return TerminalIdentity(
            terminal_run_id=str(row["terminal_run_id"]),
            first_observed_at_ms=int(row["first_observed_at_ms"]),
            generation=int(row["generation"]),
            current_pane_id=row["current_pane_id"],
            current_generation_started_at_ms=row[
                "current_generation_started_at_ms"
            ],
            current_generation_ended_at_ms=row[
                "current_generation_ended_at_ms"
            ],
            closed_at_ms=row["closed_at_ms"],
            archived_at_ms=row["archived_at_ms"],
            retired_pane_ids=[
                str(item)
                for item in json.loads(str(row["retired_pane_ids_json"]))
            ],
            bindings=bindings,
        )


def parse_tmux_list_panes(
    output: str,
    *,
    observed_at_ms: int,
    server_id: str,
) -> list[PaneObservation]:
    """Parse a neutral six-field ``tmux list-panes -a`` result.

    The runner should use this exact content-free format string:
    ``#{session_id}\\x1f#{window_id}\\x1f#{pane_id}\\x1f#{pane_pid}``
    ``\\x1f#{pane_dead}\\x1f#{@hermes_terminal_run_id}``.
    """
    observations: list[PaneObservation] = []
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\x1f")
        if len(fields) == 1:
            # tmux renders C0 separators in formatted output as octal escapes.
            fields = line.split("\\037")
        # Six fields is the original identity-only shape; eight adds the task
        # binding a managed window already carries. Both are accepted so a
        # collector and a tmux server can be upgraded independently.
        if len(fields) not in (6, 8):
            raise ValueError(
                "tmux pane observation must have six or eight fields"
            )
        session_id, window_id, pane_id, pid, dead, marker = fields[:6]
        metadata: dict[str, object] = {}
        if len(fields) == 8:
            task_id, task_run_id = fields[6:]
            if task_id:
                metadata["task_id"] = task_id
            if task_run_id:
                metadata["task_run_id"] = task_run_id
        observations.append(
            PaneObservation(
                server_id=server_id,
                session_id=session_id,
                window_id=window_id,
                pane_id=pane_id,
                pane_pid=int(pid) if pid else None,
                observed_at_ms=observed_at_ms,
                dead=dead == "1",
                durable_terminal_run_id=marker or None,
                metadata=metadata,
            )
        )
    return observations


def _source_locator_id(observation: PaneObservation) -> str:
    """A content-free source identifier; raw tmux locator stays in the store."""
    digest = hashlib.sha256(observation.locator_key.encode("utf-8")).hexdigest()
    return f"tmuxpane_{digest}"


def _durable_marker(observation: PaneObservation) -> str | None:
    marker = (observation.durable_terminal_run_id or "").strip()
    if not marker:
        return None
    if not _DURABLE_MARKER.fullmatch(marker):
        raise ValueError(
            "tmux durable marker must use a reserved content-free ID format"
        )
    return marker


def _event(
    *,
    event_type: EventType,
    terminal_run_id: str,
    observed_at_ms: int,
    validity: Validity,
    generation: int,
    attributes: Mapping[str, object],
    lifecycle_phase: LifecyclePhase | None,
    span_kind: SpanKind | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    task_run_id: str | None = None,
    workload_kind: WorkloadKind = WorkloadKind.UNCLASSIFIED,
) -> ExecutionEvent:
    """One contract-constrained construction seam for terminal facts."""
    source_execution_id = f"{terminal_run_id}:g{generation}"
    event_key = stable_idempotency_key(
        "tmux_reconciliation", source_execution_id, event_type.value, observed_at_ms,
        task_run_id or "", span_id or "",
    )
    return make_event(
        source="tmux_reconciliation",
        source_execution_id=source_execution_id,
        execution_id=stable_execution_id("tmux_reconciliation", source_execution_id),
        idempotency_key=event_key,
        event_type=event_type,
        validity=validity,
        observed_at_ms=observed_at_ms,
        recorded_at_ms=observed_at_ms,
        workload_kind=workload_kind,
        execution_surface=ExecutionSurface.TMUX,
        trigger_kind=TriggerKind.RECONCILIATION,
        lifecycle_phase=lifecycle_phase,
        span_kind=span_kind,
        span_id=span_id,
        parent_span_id=parent_span_id,
        terminal_run_id=terminal_run_id,
        terminal_generation=generation,
        task_run_id=task_run_id,
        attributes=attributes,
    )


def _explicit_span_fields(
    metadata: Mapping[str, object], *, terminal_run_id: str, generation: int
) -> dict[str, object]:
    """Translate only explicit span metadata; time never creates parentage."""
    raw_kind = metadata.get("span_kind")
    if raw_kind is None:
        return {}
    kind = SpanKind(str(raw_kind))
    raw_span_id = metadata.get("span_id")
    span_id = (
        str(raw_span_id)
        if raw_span_id is not None
        else stable_span_id("tmux_reconciliation", f"{terminal_run_id}:g{generation}:{kind.value}")
    )
    parent_span_id = metadata.get("parent_span_id")
    return {
        "span_kind": kind,
        "span_id": span_id,
        "parent_span_id": str(parent_span_id) if parent_span_id is not None else None,
    }


def _explicit_workload(metadata: Mapping[str, object]) -> WorkloadKind:
    raw = metadata.get("workload_kind")
    return WorkloadKind(str(raw)) if raw is not None else WorkloadKind.UNCLASSIFIED


def _explicit_attributes(metadata: Mapping[str, object], *, terminal_status: str) -> dict[str, object]:
    attributes: dict[str, object] = {"terminal_status": terminal_status}
    if metadata.get("agent_kind") is not None:
        attributes["agent_kind"] = str(metadata["agent_kind"])
    if metadata.get("operator_kind") is not None:
        attributes["operator_action_kind"] = str(metadata["operator_kind"])
    return attributes


class TmuxReconciliationAdapter:
    """Reconcile neutral pane observations into deterministic facts.

    New pane IDs only continue a run when a durable ``terminal_run_id`` marker
    says so.  A matching window/session/PID is deliberately insufficient.
    """

    def __init__(self, store: IdentityStore) -> None:
        self._store = store

    def reconcile(
        self,
        observations: Iterable[PaneObservation],
        *,
        actions: Iterable[TerminalAction] = (),
    ) -> list[ExecutionEvent]:
        observation_list = list(observations)
        current_panes: dict[str, str | None] = {}
        snapshot_panes: dict[tuple[str, int], set[str]] = {}
        for observation in observation_list:
            marker = _durable_marker(observation)
            if marker is None:
                continue
            if marker not in current_panes:
                identity = self._store.get_by_run_id(marker)
                current_panes[marker] = (
                    identity.current_pane_id
                    if identity is not None
                    else None
                )
            snapshot_panes.setdefault(
                (marker, observation.observed_at_ms),
                set(),
            ).add(observation.pane_id)

        events: list[ExecutionEvent] = []
        for observation in sorted(
            observation_list,
            key=lambda item: (
                item.observed_at_ms,
                0
                if (
                    _durable_marker(item) is not None
                    and item.pane_id
                    == current_panes.get(_durable_marker(item) or "")
                )
                else 1,
                item.locator_key,
            ),
        ):
            marker = _durable_marker(observation)
            events.extend(
                self._reconcile_pane(
                    observation,
                    snapshot_pane_ids=(
                        frozenset(
                            snapshot_panes.get(
                                (marker, observation.observed_at_ms),
                                set(),
                            )
                        )
                        if marker is not None
                        else frozenset()
                    ),
                )
            )
        for action in sorted(actions, key=lambda item: (item.observed_at_ms, item.terminal_run_id, item.action)):
            events.extend(self._reconcile_action(action))
        return events

    def reconcile_runner(
        self,
        list_panes: Callable[[], Sequence[PaneObservation]],
        *,
        actions: Iterable[TerminalAction] = (),
    ) -> list[ExecutionEvent]:
        """Use an injected neutral runner; this adapter executes no tmux command."""
        return self.reconcile(list_panes(), actions=actions)

    def bind_task_run(
        self, terminal_run_id: str, task_run_id: str, *, observed_at_ms: int
    ) -> list[ExecutionEvent]:
        identity = self._store.get_by_run_id(terminal_run_id)
        if identity is None:
            raise ValueError(f"unknown terminal run: {terminal_run_id}")
        if (
            identity.closed_at_ms is not None
            or identity.archived_at_ms is not None
        ):
            raise ValueError(
                "a closed or archived terminal cannot accept task bindings"
            )
        active_binding = next(
            (
                binding
                for binding in identity.bindings
                if binding.task_run_id == task_run_id
                and binding.ended_at_ms is None
            ),
            None,
        )
        if active_binding is not None:
            return (
                self._replayable(
                    self._task_binding_event(
                        identity,
                        active_binding,
                        event_type=EventType.TASK_BINDING_STARTED,
                        observed_at_ms=observed_at_ms,
                    )
                )
                if active_binding.started_at_ms == observed_at_ms
                else []
            )
        binding = TaskRunBinding(
            terminal_run_id=terminal_run_id,
            task_run_id=task_run_id,
            started_at_ms=observed_at_ms,
            generation=identity.generation,
        )
        identity.bindings.append(binding)
        self._store.save(identity)
        return self._replayable(
            self._task_binding_event(
                identity,
                binding,
                event_type=EventType.TASK_BINDING_STARTED,
                observed_at_ms=observed_at_ms,
            )
        )

    def end_task_run_binding(
        self,
        terminal_run_id: str,
        task_run_id: str,
        *,
        observed_at_ms: int,
    ) -> list[ExecutionEvent]:
        """End one explicit temporal binding without ending the terminal."""
        identity = self._store.get_by_run_id(terminal_run_id)
        if identity is None:
            raise ValueError(f"unknown terminal run: {terminal_run_id}")
        terminal_end_times = {
            value
            for value in (identity.closed_at_ms, identity.archived_at_ms)
            if value is not None
        }
        if terminal_end_times:
            replay = next(
                (
                    binding
                    for binding in identity.bindings
                    if binding.task_run_id == task_run_id
                    and binding.ended_at_ms == observed_at_ms
                    and observed_at_ms in terminal_end_times
                ),
                None,
            )
            if replay is None:
                raise ValueError(
                    "a closed or archived terminal cannot mutate task bindings"
                )
            return self._replayable(
                self._task_binding_event(
                    identity,
                    replay,
                    event_type=EventType.TASK_BINDING_ENDED,
                    observed_at_ms=observed_at_ms,
                )
            )
        active_index = next(
            (
                index
                for index, binding in enumerate(identity.bindings)
                if binding.task_run_id == task_run_id
                and binding.ended_at_ms is None
            ),
            None,
        )
        if active_index is None:
            ended = next(
                (
                    binding
                    for binding in identity.bindings
                    if binding.task_run_id == task_run_id
                    and binding.ended_at_ms == observed_at_ms
                ),
                None,
            )
            return (
                self._replayable(
                    self._task_binding_event(
                        identity,
                        ended,
                        event_type=EventType.TASK_BINDING_ENDED,
                        observed_at_ms=observed_at_ms,
                    )
                )
                if ended is not None
                else []
            )
        active = identity.bindings[active_index]
        if observed_at_ms < active.started_at_ms:
            raise ValueError("task binding end must not precede its start")
        identity.bindings[active_index] = TaskRunBinding(
            terminal_run_id=active.terminal_run_id,
            task_run_id=active.task_run_id,
            started_at_ms=active.started_at_ms,
            generation=active.generation,
            ended_at_ms=observed_at_ms,
        )
        self._store.save(identity)
        return self._replayable(
            self._task_binding_event(
                identity,
                active,
                event_type=EventType.TASK_BINDING_ENDED,
                observed_at_ms=observed_at_ms,
            )
        )

    def _reconcile_pane(
        self,
        observation: PaneObservation,
        *,
        snapshot_pane_ids: frozenset[str],
    ) -> list[ExecutionEvent]:
        metadata = observation.safe_metadata()
        marker = _durable_marker(observation)
        identity = self._store.get_by_run_id(marker) if marker else None
        if identity is None and marker is None:
            identity = self._store.get_by_pane_locator(observation.locator_key)
        if (
            marker is not None
            and identity is not None
            and (
                identity.closed_at_ms is not None
                or identity.archived_at_ms is not None
            )
        ):
            raise ValueError("a closed or archived terminal run cannot respawn")
        if (
            marker is not None
            and identity is not None
            and identity.current_pane_id is not None
            and identity.current_pane_id != observation.pane_id
        ):
            current_generation_active = (
                identity.current_generation_ended_at_ms is None
            )
            if (
                observation.dead
                or (
                    current_generation_active
                    and identity.current_pane_id in snapshot_pane_ids
                )
            ):
                return []
        unmanaged_reuse = (
            marker is None
            and identity is not None
            and (
                identity.closed_at_ms is not None
                or identity.archived_at_ms is not None
                or (
                    identity.current_generation_ended_at_ms is not None
                    and not observation.dead
                )
            )
        )
        if unmanaged_reuse:
            identity = None
        if identity is None:
            locator_identity = _source_locator_id(observation)
            locator_identity = (
                f"{locator_identity}:{observation.observed_at_ms}"
            )
            run_id = marker or stable_execution_id(
                "tmux_reconciliation", locator_identity
            )
            identity = TerminalIdentity(
                terminal_run_id=run_id,
                first_observed_at_ms=observation.observed_at_ms,
                current_generation_started_at_ms=observation.observed_at_ms,
            )

        generation_changed = (
            marker is not None
            and identity.current_pane_id is not None
            and (
                (
                    identity.current_pane_id != observation.pane_id
                    and not observation.dead
                )
                or (
                    identity.current_generation_ended_at_ms is not None
                    and not observation.dead
                )
            )
        )
        if generation_changed:
            previous_pane_id = identity.current_pane_id
            if (
                previous_pane_id is not None
                and previous_pane_id != observation.pane_id
                and previous_pane_id not in identity.retired_pane_ids
            ):
                identity.retired_pane_ids.append(previous_pane_id)
            identity.retired_pane_ids = [
                pane_id
                for pane_id in identity.retired_pane_ids
                if pane_id != observation.pane_id
            ]
            identity.generation += 1
            identity.current_generation_started_at_ms = (
                observation.observed_at_ms
            )
            identity.current_generation_ended_at_ms = None
        identity.current_pane_id = observation.pane_id
        self._store.clear_locators(identity.terminal_run_id)
        self._store.save(identity, locator_key=observation.locator_key)
        span_fields = _explicit_span_fields(
            metadata, terminal_run_id=identity.terminal_run_id, generation=identity.generation
        )
        workload = _explicit_workload(metadata)

        events: list[ExecutionEvent] = []
        start_replay = (
            identity.current_generation_started_at_ms
            == observation.observed_at_ms
        )
        if start_replay:
            is_first_generation = identity.generation == 1
            events.extend(
                self._replayable(
                    _event(
                        event_type=EventType.TERMINAL_OBSERVED,
                        terminal_run_id=identity.terminal_run_id,
                        observed_at_ms=observation.observed_at_ms,
                        validity=(
                            Validity.LOWER_BOUND
                            if is_first_generation
                            else Validity.EXACT
                        ),
                        generation=identity.generation,
                        lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
                        attributes=_explicit_attributes(
                            metadata,
                            terminal_status=(
                                "first_observed_lower_bound"
                                if is_first_generation
                                else "durable_marker_respawn"
                            ),
                        ),
                        workload_kind=workload,
                        **span_fields,
                    )
                )
            )
        ended_replay = (
            observation.dead
            and identity.current_generation_ended_at_ms
            == observation.observed_at_ms
        )
        if observation.dead and (
            identity.current_generation_ended_at_ms is None or ended_replay
        ):
            if identity.current_generation_ended_at_ms is None:
                identity.current_generation_ended_at_ms = (
                    observation.observed_at_ms
                )
                self._store.save(
                    identity,
                    locator_key=observation.locator_key,
                )
            events.extend(
                self._replayable(
                    _event(
                        event_type=EventType.TERMINAL_GENERATION_ENDED,
                        terminal_run_id=identity.terminal_run_id,
                        observed_at_ms=observation.observed_at_ms,
                        validity=Validity.EXACT,
                        generation=identity.generation,
                        lifecycle_phase=LifecyclePhase.ENDED,
                        attributes=_explicit_attributes(metadata, terminal_status="pane_dead"),
                        workload_kind=workload,
                        **span_fields,
                    )
                )
            )
        elif not start_replay:
            # A state-neutral observation proves that reconciliation is still
            # seeing this pane. It refreshes source coverage/freshness without
            # inventing another lifecycle milestone or changing identity.
            events.extend(
                self._replayable(
                    _event(
                        event_type=EventType.TERMINAL_OBSERVED,
                        terminal_run_id=identity.terminal_run_id,
                        observed_at_ms=observation.observed_at_ms,
                        validity=Validity.EXACT,
                        generation=identity.generation,
                        lifecycle_phase=None,
                        attributes=_explicit_attributes(
                            metadata,
                            terminal_status=(
                                "pane_dead_observed"
                                if observation.dead
                                else "pane_alive_observed"
                            ),
                        ),
                        workload_kind=workload,
                        **span_fields,
                    )
                )
            )
        return events

    def _reconcile_action(self, action: TerminalAction) -> list[ExecutionEvent]:
        identity = self._store.get_by_run_id(action.terminal_run_id)
        if identity is None:
            return []
        requested_action_at_ms = (
            identity.closed_at_ms
            if action.action == "close"
            else identity.archived_at_ms
        )
        replaying = requested_action_at_ms == action.observed_at_ms
        terminal_ended_at_ms = (
            identity.closed_at_ms
            if identity.closed_at_ms is not None
            else identity.archived_at_ms
        )
        if terminal_ended_at_ms is not None and not replaying:
            return []
        action_bindings = (
            [
                (index, binding)
                for index, binding in enumerate(identity.bindings)
                if binding.ended_at_ms == action.observed_at_ms
            ]
            if replaying
            else [
                (index, binding)
                for index, binding in enumerate(identity.bindings)
                if binding.ended_at_ms is None
            ]
        )
        if any(
            action.observed_at_ms < binding.started_at_ms
            for _, binding in action_bindings
        ):
            raise ValueError(
                "terminal close must not precede an active task binding"
            )
        if not replaying:
            if action.action == "close":
                identity.closed_at_ms = action.observed_at_ms
            else:
                identity.archived_at_ms = action.observed_at_ms
            for index, binding in action_bindings:
                identity.bindings[index] = TaskRunBinding(
                    terminal_run_id=binding.terminal_run_id,
                    task_run_id=binding.task_run_id,
                    started_at_ms=binding.started_at_ms,
                    generation=binding.generation,
                    ended_at_ms=action.observed_at_ms,
                )
            self._store.save(identity)
        self._store.clear_locators(identity.terminal_run_id)
        events = self._replayable(
            _event(
                event_type=EventType.TERMINAL_CLOSED,
                terminal_run_id=identity.terminal_run_id,
                observed_at_ms=action.observed_at_ms,
                validity=Validity.EXACT,
                generation=identity.generation,
                lifecycle_phase=LifecyclePhase.ENDED,
                attributes={
                    "archived": action.action == "archive",
                    "terminal_status": action.action,
                },
            )
        )
        for _, binding in sorted(
            action_bindings,
            key=lambda item: (
                item[1].started_at_ms,
                item[1].task_run_id,
            ),
        ):
            events.extend(
                self._replayable(
                    self._task_binding_event(
                        identity,
                        binding,
                        event_type=EventType.TASK_BINDING_ENDED,
                        observed_at_ms=action.observed_at_ms,
                    )
                )
            )
        return events

    @staticmethod
    def _task_binding_event(
        identity: TerminalIdentity,
        binding: TaskRunBinding,
        *,
        event_type: EventType,
        observed_at_ms: int,
    ) -> ExecutionEvent:
        started = event_type is EventType.TASK_BINDING_STARTED
        return _event(
            event_type=event_type,
            terminal_run_id=identity.terminal_run_id,
            observed_at_ms=observed_at_ms,
            validity=Validity.EXACT,
            generation=binding.generation,
            lifecycle_phase=LifecyclePhase.RUNNING if started else None,
            span_kind=SpanKind.TASK_BINDING,
            span_id=stable_span_id(
                "tmux_reconciliation",
                (
                    f"{identity.terminal_run_id}:task:"
                    f"{binding.task_run_id}:{binding.started_at_ms}"
                ),
            ),
            task_run_id=binding.task_run_id,
            attributes={
                "terminal_status": (
                    "task_binding_started"
                    if started
                    else "task_binding_ended"
                )
            },
        )

    @staticmethod
    def _replayable(event: ExecutionEvent) -> list[ExecutionEvent]:
        """Return a deterministic fact; the durable ledger owns deduplication."""
        return [event]

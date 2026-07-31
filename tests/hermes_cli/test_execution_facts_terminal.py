"""Contracts for content-free, read-only tmux reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionSurface,
    LifecyclePhase,
    SpanKind,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_execution_id,
    stable_idempotency_key,
)
from hermes_cli.execution_facts_instrumentation import (
    ActivityObservation,
    TestObservation as ExecutionTestObservation,
    ToolObservation,
    activity_span_events,
    test_span_events as build_test_span_events,
    tool_span_events,
)
from hermes_cli.execution_facts_ledger import (
    EmitterSnapshot,
    ExecutionFactsLedger,
    emitter_snapshot_event,
)
from hermes_cli.execution_facts_projection import rebuild_projections
from hermes_cli.execution_facts_readmodel import build_execution_facts_payload
from hermes_cli.execution_facts_reconcile import (
    SystemInvocationObservation,
    append_reconciled,
    reconcile_cron,
    reconcile_kanban,
    reconcile_loop_records,
    reconcile_system_invocations,
    reconcile_usage_facts,
)
from hermes_cli.execution_facts_terminal import (
    MemoryIdentityStore,
    PaneObservation,
    SqliteIdentityStore,
    TerminalAction,
    TmuxReconciliationAdapter,
    parse_tmux_list_panes,
)


def _marker(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]
    return f"tr_{digest}"


def _pane(
    pane_id: str = "%1",
    *,
    observed_at_ms: int = 1_000,
    dead: bool = False,
    marker: str | None = None,
    metadata: dict[str, object] | None = None,
) -> PaneObservation:
    return PaneObservation(
        server_id="tmux_server_1",
        session_id="$1",
        window_id="@1",
        pane_id=pane_id,
        pane_pid=1234,
        observed_at_ms=observed_at_ms,
        dead=dead,
        durable_terminal_run_id=marker,
        metadata=metadata or {},
    )


def test_first_observed_shell_is_lower_bound_and_unclassified() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())

    events = adapter.reconcile([_pane()])

    assert len(events) == 1
    event = events[0]
    assert event.event_type is EventType.TERMINAL_OBSERVED
    assert event.validity is Validity.LOWER_BOUND
    assert event.workload_kind is WorkloadKind.UNCLASSIFIED
    assert event.lifecycle_phase.value == "process_started"
    assert event.terminal_generation == 1
    assert event.attributes == {"terminal_status": "first_observed_lower_bound"}


def test_two_panes_in_one_window_are_independent_terminal_containers() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())

    events = adapter.reconcile([_pane("%1"), _pane("%2")])

    assert len(events) == 2
    assert {event.terminal_run_id for event in events} == {
        event.terminal_run_id for event in events
    }
    assert events[0].terminal_run_id != events[1].terminal_run_id


def test_existing_durable_marker_is_adopted_without_live_stamping() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())

    event = adapter.reconcile([_pane(marker=_marker("existing"))])[0]

    assert event.terminal_run_id == _marker("existing")
    assert event.validity is Validity.LOWER_BOUND


@pytest.mark.parametrize(
    "marker",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
        "operator-notes",
        "tr_ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "tr_operator_notes",
    ),
)
def test_durable_marker_rejects_secret_or_content_shaped_values(
    marker: str,
) -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())

    with pytest.raises(
        ValueError,
        match="reserved content-free ID format",
    ):
        adapter.reconcile([_pane(marker=marker)])


def test_stable_pane_observation_refreshes_source_without_new_lifecycle() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())
    first = adapter.reconcile([_pane(marker=_marker("stable"))])[0]

    refreshed = adapter.reconcile(
        [_pane(marker=_marker("stable"), observed_at_ms=2_000)]
    )

    assert len(refreshed) == 1
    assert refreshed[0].terminal_run_id == first.terminal_run_id
    assert refreshed[0].validity is Validity.EXACT
    assert refreshed[0].lifecycle_phase is None
    assert refreshed[0].attributes["terminal_status"] == (
        "pane_alive_observed"
    )


def test_marker_proves_respawn_and_increments_generation() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())
    first = adapter.reconcile([_pane("%1", marker=_marker("managed"))])[0]

    respawn = adapter.reconcile([
        _pane("%2", marker=_marker("managed"), observed_at_ms=2_000)
    ])[0]

    assert first.terminal_generation == 1
    assert respawn.terminal_run_id == first.terminal_run_id
    assert respawn.terminal_generation == 2
    assert respawn.validity is Validity.EXACT
    assert respawn.attributes["terminal_status"] == "durable_marker_respawn"


def test_marker_respawn_increments_generation_even_when_pane_id_is_reused() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())
    adapter.reconcile([_pane("%1", marker=_marker("managed"))])
    adapter.reconcile(
        [
            _pane(
                "%1",
                marker=_marker("managed"),
                dead=True,
                observed_at_ms=2_000,
            )
        ]
    )

    respawn = adapter.reconcile(
        [
            _pane(
                "%1",
                marker=_marker("managed"),
                dead=False,
                observed_at_ms=3_000,
            )
        ]
    )[0]

    assert respawn.terminal_generation == 2
    assert respawn.attributes["terminal_status"] == "durable_marker_respawn"


def test_duplicate_marker_keeps_one_current_pane_and_retires_old_locator() -> None:
    store = MemoryIdentityStore()
    adapter = TmuxReconciliationAdapter(store)
    adapter.reconcile([_pane("%1", marker=_marker("single_current"))])

    duplicate_snapshot = adapter.reconcile(
        [
            _pane(
                "%2",
                marker=_marker("single_current"),
                observed_at_ms=2_000,
            ),
            _pane(
                "%1",
                marker=_marker("single_current"),
                observed_at_ms=2_000,
            ),
        ]
    )
    unchanged = store.get_by_run_id(_marker("single_current"))

    assert len(duplicate_snapshot) == 1
    assert unchanged is not None
    assert unchanged.generation == 1
    assert unchanged.current_pane_id == "%1"

    respawn = adapter.reconcile(
        [
            _pane(
                "%2",
                marker=_marker("single_current"),
                observed_at_ms=3_000,
            )
        ]
    )
    stale_old = adapter.reconcile(
        [
            _pane(
                "%1",
                marker=_marker("single_current"),
                dead=True,
                observed_at_ms=4_000,
            )
        ]
    )
    current = store.get_by_run_id(_marker("single_current"))

    assert respawn[0].terminal_generation == 2
    assert stale_old == []
    assert current is not None
    assert current.current_pane_id == "%2"
    assert current.current_generation_ended_at_ms is None
    assert store.get_by_pane_locator(_pane("%1").locator_key) is None
    assert (
        store.get_by_pane_locator(_pane("%2").locator_key)
        is current
    )

    handoff = adapter.reconcile(
        [
            _pane(
                "%1",
                marker=_marker("single_current"),
                observed_at_ms=5_000,
            ),
            _pane(
                "%2",
                marker=_marker("single_current"),
                dead=True,
                observed_at_ms=5_000,
            ),
        ]
    )
    handed_off = store.get_by_run_id(_marker("single_current"))
    assert [event.event_type for event in handoff] == [
        EventType.TERMINAL_GENERATION_ENDED,
        EventType.TERMINAL_OBSERVED,
    ]
    assert handed_off is not None
    assert handed_off.generation == 3
    assert handed_off.current_pane_id == "%1"

    sole_survivor = adapter.reconcile(
        [
            _pane(
                "%2",
                marker=_marker("single_current"),
                observed_at_ms=6_000,
            )
        ]
    )
    survived = store.get_by_run_id(_marker("single_current"))
    assert sole_survivor[0].terminal_generation == 4
    assert survived is not None
    assert survived.current_pane_id == "%2"


def test_new_pane_without_marker_is_not_heuristically_joined() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())
    first = adapter.reconcile([_pane("%1")])[0]
    second = adapter.reconcile([_pane("%2", observed_at_ms=2_000)])[0]

    assert second.terminal_run_id != first.terminal_run_id
    assert second.terminal_generation == 1


def test_unmanaged_locator_reuse_after_death_gets_new_terminal_identity() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())
    first = adapter.reconcile([_pane("%1")])[0]
    adapter.reconcile(
        [_pane("%1", dead=True, observed_at_ms=2_000)]
    )

    reused = adapter.reconcile(
        [_pane("%1", dead=False, observed_at_ms=3_000)]
    )[0]

    assert reused.terminal_run_id != first.terminal_run_id
    assert reused.terminal_generation == 1
    assert reused.validity is Validity.LOWER_BOUND


def test_pane_death_ends_generation_not_terminal_until_explicit_close_or_archive() -> None:
    store = MemoryIdentityStore()
    adapter = TmuxReconciliationAdapter(store)
    first = adapter.reconcile([_pane(marker=_marker("lived"))])[0]

    died = adapter.reconcile([
        _pane("%1", marker=_marker("lived"), dead=True, observed_at_ms=2_000)
    ])
    identity = store.get_by_run_id(_marker("lived"))

    assert died[0].event_type is EventType.TERMINAL_GENERATION_ENDED
    assert died[0].terminal_run_id == first.terminal_run_id
    assert identity is not None and identity.closed_at_ms is None and identity.archived_at_ms is None

    closed = adapter.reconcile([], actions=[TerminalAction(_marker("lived"), "close", 3_000)])
    assert closed[0].event_type is EventType.TERMINAL_CLOSED
    assert closed[0].end_reason is None
    assert closed[0].attributes["archived"] is False
    assert store.get_by_run_id(_marker("lived")).closed_at_ms == 3_000  # type: ignore[union-attr]


def test_close_ends_bindings_and_closed_identity_cannot_be_reused() -> None:
    store = MemoryIdentityStore()
    adapter = TmuxReconciliationAdapter(store)
    adapter.reconcile([_pane(marker=_marker("closed"))])
    adapter.bind_task_run(_marker("closed"), "run_a", observed_at_ms=2_000)

    closed = adapter.reconcile(
        [],
        actions=[TerminalAction(_marker("closed"), "close", 3_000)],
    )
    identity = store.get_by_run_id(_marker("closed"))

    assert [event.event_type for event in closed] == [
        EventType.TERMINAL_CLOSED,
        EventType.TASK_BINDING_ENDED,
    ]
    assert identity is not None
    assert identity.bindings[0].ended_at_ms == 3_000
    assert store.get_by_pane_locator(_pane().locator_key) is None
    replayed_end = adapter.end_task_run_binding(
        _marker("closed"),
        "run_a",
        observed_at_ms=3_000,
    )
    assert replayed_end[0].event_type is EventType.TASK_BINDING_ENDED
    with pytest.raises(ValueError, match="cannot mutate task bindings"):
        adapter.end_task_run_binding(
            _marker("closed"),
            "run_a",
            observed_at_ms=4_000,
        )
    with pytest.raises(ValueError, match="cannot accept task bindings"):
        adapter.bind_task_run(_marker("closed"), "run_b", observed_at_ms=4_000)
    with pytest.raises(ValueError, match="cannot respawn"):
        adapter.reconcile(
            [_pane(marker=_marker("closed"), observed_at_ms=4_000)]
        )

    reused = adapter.reconcile([_pane(observed_at_ms=4_000)])
    assert len(reused) == 1
    assert reused[0].terminal_run_id != _marker("closed")
    assert reused[0].validity is Validity.LOWER_BOUND


@pytest.mark.parametrize(
    ("first_action", "second_action"),
    (("close", "archive"), ("archive", "close")),
)
def test_terminal_run_accepts_only_its_first_end_action(
    first_action: str,
    second_action: str,
) -> None:
    store = MemoryIdentityStore()
    adapter = TmuxReconciliationAdapter(store)
    adapter.reconcile([_pane(marker=_marker("one_end"))])

    first = adapter.reconcile(
        [],
        actions=[TerminalAction(_marker("one_end"), first_action, 3_000)],
    )
    second = adapter.reconcile(
        [],
        actions=[TerminalAction(_marker("one_end"), second_action, 4_000)],
    )

    assert len(first) == 1
    assert first[0].attributes["archived"] is (first_action == "archive")
    assert second == []


def test_binding_keeps_start_generation_across_terminal_respawn(
    tmp_path,
) -> None:
    store = MemoryIdentityStore()
    adapter = TmuxReconciliationAdapter(store)
    events = adapter.reconcile(
        [_pane(marker=_marker("respawn"), observed_at_ms=1_000)]
    )
    events += adapter.bind_task_run(
        _marker("respawn"),
        "task-run-1",
        observed_at_ms=1_100,
    )
    events += adapter.reconcile(
        [
            _pane(
                marker=_marker("respawn"),
                observed_at_ms=1_200,
                dead=True,
            )
        ]
    )
    events += adapter.reconcile(
        [
            _pane(
                "%2",
                marker=_marker("respawn"),
                observed_at_ms=1_300,
            )
        ]
    )
    ended = adapter.end_task_run_binding(
        _marker("respawn"),
        "task-run-1",
        observed_at_ms=1_400,
    )
    events += ended

    assert ended[0].terminal_generation == 1
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database, clock_ms=lambda: 2_000)
    ledger.initialize()
    ledger.append_batch(events)
    rebuild_projections(database)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT terminal_run_id, task_run_id, started_at_ms, ended_at_ms "
            "FROM task_binding_projection"
        ).fetchone()
    assert row == (_marker("respawn"), "task-run-1", 1_100, 1_400)


def test_multiple_task_run_bindings_append_without_scalar_overwrite() -> None:
    store = MemoryIdentityStore()
    adapter = TmuxReconciliationAdapter(store)
    terminal_run_id = adapter.reconcile([_pane(marker=_marker("bindings"))])[0].terminal_run_id

    first = adapter.bind_task_run(terminal_run_id, "run_a", observed_at_ms=2_000)
    second = adapter.bind_task_run(terminal_run_id, "run_b", observed_at_ms=3_000)
    duplicate = adapter.bind_task_run(
        terminal_run_id,
        "run_a",
        observed_at_ms=3_500,
    )
    ended = adapter.end_task_run_binding(
        terminal_run_id,
        "run_a",
        observed_at_ms=4_000,
    )
    identity = store.get_by_run_id(terminal_run_id)

    assert [event.event_type for event in first + second + ended] == [
        EventType.TASK_BINDING_STARTED,
        EventType.TASK_BINDING_STARTED,
        EventType.TASK_BINDING_ENDED,
    ]
    assert duplicate == []
    assert [binding.task_run_id for binding in identity.bindings] == ["run_a", "run_b"]  # type: ignore[union-attr]
    assert identity.bindings[0].ended_at_ms == 4_000  # type: ignore[union-attr]
    assert identity.bindings[1].ended_at_ms is None  # type: ignore[union-attr]


def test_spans_are_only_explicit_and_parent_is_never_inferred_from_time() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())

    unclassified = adapter.reconcile([_pane(marker=_marker("plain"))])[0]
    child = adapter.reconcile([
        _pane(
            marker=_marker("child"),
            metadata={
                "span_kind": "subagent",
                "span_id": "sp_child",
                "parent_span_id": "sp_parent",
                "workload_kind": "interactive",
                "agent_kind": "codex",
            },
        )
    ])[0]

    assert unclassified.span_id is None and unclassified.parent_span_id is None
    assert child.span_kind is SpanKind.SUBAGENT
    assert child.span_id == "sp_child"
    assert child.parent_span_id == "sp_parent"
    assert child.workload_kind is WorkloadKind.INTERACTIVE
    assert child.attributes["agent_kind"] == "codex"


def test_content_canary_is_rejected_and_never_reaches_event() -> None:
    canary = "DO_NOT_PERSIST_PANE_CONTENT"
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())

    with pytest.raises(ValueError, match="content-shaped") as raised:
        adapter.reconcile([_pane(metadata={"command": canary})])

    assert canary not in str(raised.value)
    harmlessly_dropped = adapter.reconcile([
        _pane("%2", metadata={"unrecognized_metadata": canary})
    ])[0]
    assert canary not in harmlessly_dropped.to_json()


def test_second_identical_reconciliation_is_replayable_and_runner_is_injected() -> None:
    adapter = TmuxReconciliationAdapter(MemoryIdentityStore())
    calls = 0

    def list_panes() -> list[PaneObservation]:
        nonlocal calls
        calls += 1
        return [_pane(marker=_marker("idempotent"))]

    first = adapter.reconcile_runner(list_panes)
    second = adapter.reconcile_runner(list_panes)

    assert len(first) == 1
    assert len(second) == 1
    assert second[0].to_json() == first[0].to_json()
    assert calls == 2


def test_neutral_list_panes_parser_covers_every_pane_without_content() -> None:
    marker = _marker("managed")
    output = (
        f"$1\x1f@1\x1f%1\x1f123\x1f0\x1f{marker}\n"
        "$1\x1f@1\x1f%2\x1f456\x1f1\x1f\n"
    )

    observations = parse_tmux_list_panes(
        output,
        observed_at_ms=4_000,
        server_id="isolated-test",
    )

    assert [item.pane_id for item in observations] == ["%1", "%2"]
    assert observations[0].durable_terminal_run_id == _marker("managed")
    assert observations[1].durable_terminal_run_id is None
    assert observations[1].dead is True


def test_launcher_legacy_uuid_marker_round_trips_without_content() -> None:
    marker = "0123456789abcdef0123456789abcdef"
    observations = parse_tmux_list_panes(
        f"$1\x1f@1\x1f%1\x1f123\x1f0\x1f{marker}\n",
        observed_at_ms=4_000,
        server_id="isolated-test",
    )

    event = TmuxReconciliationAdapter(MemoryIdentityStore()).reconcile(
        observations
    )[0]

    assert event.terminal_run_id == marker
    assert marker not in event.attributes.values()


def test_sqlite_identity_store_survives_adapter_restart(tmp_path) -> None:
    store = SqliteIdentityStore(tmp_path / "facts.db")
    store.initialize()
    first_adapter = TmuxReconciliationAdapter(store)
    first = first_adapter.reconcile([_pane(marker=_marker("durable"))])

    restarted = TmuxReconciliationAdapter(SqliteIdentityStore(tmp_path / "facts.db"))
    respawn = restarted.reconcile(
        [_pane("%2", marker=_marker("durable"), observed_at_ms=2_000)]
    )
    identity = store.get_by_run_id(_marker("durable"))

    assert first[0].terminal_generation == 1
    assert respawn[0].terminal_generation == 2
    assert identity is not None and identity.generation == 2


def test_replayed_observation_deduplicates_across_adapter_restart(tmp_path) -> None:
    database = tmp_path / "facts.db"
    ledger = ExecutionFactsLedger(database, clock_ms=lambda: 10_000)
    ledger.initialize()
    store = SqliteIdentityStore(database)
    store.initialize()
    observation = _pane(marker=_marker("replayable"))

    first = TmuxReconciliationAdapter(store).reconcile([observation])
    first_result = ledger.append_batch(first)
    restarted_store = SqliteIdentityStore(database)
    restarted_store.initialize()
    replay = TmuxReconciliationAdapter(restarted_store).reconcile(
        [observation]
    )
    replay_result = ledger.append_batch(replay)

    assert len(first) == len(replay) == 1
    assert first[0].to_json() == replay[0].to_json()
    assert first_result[0].inserted is True
    assert replay_result[0].inserted is False
    assert ledger.count_events() == 1


def test_real_isolated_tmux_to_landed_result_end_to_end(tmp_path) -> None:
    """One content-free chain across every required V1 source family."""
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    canary = "DO_NOT_PERSIST_E2E_CONTENT_CANARY"
    server = f"ef_{time.time_ns():x}"[-24:]
    tmux_base = [tmux, "-L", server, "-f", "/dev/null"]
    format_string = (
        "#{session_id}\x1f#{window_id}\x1f#{pane_id}\x1f#{pane_pid}"
        "\x1f#{pane_dead}\x1f#{@hermes_terminal_run_id}"
    )
    database = tmp_path / "execution_facts.db"
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    store = SqliteIdentityStore(database)
    store.initialize()
    terminal_adapter = TmuxReconciliationAdapter(store)
    now_ms = int(time.time() * 1000)
    terminal_events = []
    try:
        subprocess.run(
            tmux_base
            + [
                "new-session",
                "-d",
                "-s",
                "execution-facts-e2e",
                "sh",
                "-c",
                "sleep 2; exit 0",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            tmux_base + ["set-option", "-g", "remain-on-exit", "on"],
            check=True,
            capture_output=True,
            text=True,
        )
        initial = subprocess.run(
            tmux_base + ["list-panes", "-a", "-F", format_string],
            check=True,
            capture_output=True,
            text=True,
        )
        observations = parse_tmux_list_panes(
            initial.stdout,
            observed_at_ms=now_ms,
            server_id=server,
        )
        terminal_events.extend(terminal_adapter.reconcile(observations))
        terminal_run_id = terminal_events[0].terminal_run_id
        assert terminal_run_id is not None
        terminal_events.extend(
            terminal_adapter.bind_task_run(
                terminal_run_id,
                "2",
                observed_at_ms=now_ms + 1,
            )
        )
        time.sleep(2.1)
        ended = subprocess.run(
            tmux_base + ["list-panes", "-a", "-F", format_string],
            check=True,
            capture_output=True,
            text=True,
        )
        dead_observations = parse_tmux_list_panes(
            ended.stdout,
            observed_at_ms=now_ms + 2_100,
            server_id=server,
        )
        assert dead_observations[0].dead is True
        terminal_events.extend(
            terminal_adapter.reconcile(dead_observations)
        )
        terminal_events.extend(
            terminal_adapter.reconcile(
                [],
                actions=[
                    TerminalAction(
                        terminal_run_id,
                        "close",
                        now_ms + 2_101,
                    )
                ],
            )
        )
    finally:
        subprocess.run(
            tmux_base + ["kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )

    kanban = sqlite3.connect(":memory:")
    kanban.executescript(
        """
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
            started_at INTEGER, ended_at INTEGER, outcome TEXT,
            worker_exit_kind TEXT, worker_exit_code INTEGER
        );
        CREATE TABLE worker_run_timeline_events (
            task_run_id INTEGER, event_kind TEXT, observed_at_ms INTEGER,
            source TEXT, task_id TEXT, board TEXT, chain_root_id TEXT,
            profile TEXT
        );
        CREATE TABLE worker_run_terminal_facts (
            task_run_id INTEGER, worker_exit_kind TEXT,
            worker_exit_code INTEGER, worker_protocol_state TEXT,
            task_outcome TEXT, end_reason TEXT, task_id TEXT, board TEXT
        );
        CREATE TABLE worker_run_retry_links (
            task_run_id INTEGER, retry_of_task_run_id INTEGER,
            retry_class TEXT, triggering_event_id INTEGER,
            task_id TEXT, board TEXT
        );
        """
    )
    base_seconds = now_ms // 1000
    kanban.executemany(
        """
        INSERT INTO task_runs VALUES (
            ?, 'task-e2e', 'coder', 'done', ?, ?, ?, ?, ?
        )
        """,
        (
            (1, base_seconds, base_seconds + 1, "failed", "nonzero", 1),
            (2, base_seconds + 2, base_seconds + 4, "succeeded", "clean", 0),
        ),
    )
    kanban.executemany(
        """
        INSERT INTO worker_run_timeline_events VALUES (
            ?, ?, ?, 'e2e', 'task-e2e', 'default', 'chain-e2e', 'coder'
        )
        """,
        (
            (1, "queued", now_ms),
            (1, "process_started", now_ms + 10),
            (1, "ended", now_ms + 1_000),
            (2, "queued", now_ms + 2_000),
            (2, "claimed", now_ms + 2_010),
            (2, "spawn_started", now_ms + 2_020),
            (2, "process_started", now_ms + 2_030),
            (2, "first_llm_request", now_ms + 2_040),
            (2, "first_token", now_ms + 2_090),
            (2, "ended", now_ms + 4_000),
        ),
    )
    kanban.executemany(
        "INSERT INTO worker_run_terminal_facts VALUES (?, ?, ?, ?, ?, ?, 'task-e2e', 'default')",
        (
            (1, "nonzero", 1, "valid", "failed", "worker_exit"),
            (2, "clean", 0, "valid", "succeeded", "worker_exit"),
        ),
    )
    kanban.execute(
        """
        INSERT INTO worker_run_retry_links VALUES (
            2, 1, 'auto', 42, 'task-e2e', 'default'
        )
        """
    )

    failed_process = subprocess.run(["/bin/false"], check=False)
    retried_process = subprocess.run(["/bin/true"], check=False)
    assert failed_process.returncode == 1
    assert retried_process.returncode == 0
    cron = sqlite3.connect(":memory:")
    cron.execute(
        """
        CREATE TABLE executions (
            id TEXT, job_id TEXT, source TEXT, status TEXT,
            claimed_at TEXT, started_at TEXT, finished_at TEXT, error TEXT,
            retry_of_execution_id TEXT, retry_class TEXT
        )
        """
    )
    started_iso = datetime.fromtimestamp(now_ms / 1000, tz=UTC).isoformat()
    cron.executemany(
        """
        INSERT INTO executions VALUES (
            ?, 'e2e-job', 'test', ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            (
                "cron-failed",
                "failed",
                started_iso,
                started_iso,
                started_iso,
                canary,
                None,
                None,
            ),
            (
                "cron-retry",
                "succeeded",
                started_iso,
                started_iso,
                started_iso,
                None,
                "cron-failed",
                "transient",
            ),
        ),
    )
    loop_process = subprocess.run(["/bin/true"], check=False)
    assert loop_process.returncode == 0
    loop_events = reconcile_loop_records(
        [
            {
                "ts": started_iso,
                "pack": "e2e",
                "event": "phase_usage",
                "round": 1,
                "phase": "build",
                "engine": "kimi-coding",
                "model": "k3",
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "total_tokens": 11,
                "billing": "subscription",
                "plan": canary,
            },
            {
                "ts": started_iso,
                "pack": "e2e",
                "event": "phase_result",
                "round": 1,
                "phase": "verify",
                "verdict": "ok",
            },
        ]
    )

    usage = sqlite3.connect(":memory:")
    usage.execute(
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
    usage.execute(
        """
        INSERT INTO run_usage_facts VALUES (
            'e2e-usage', 'hermes_agent', '2', 'task-e2e', 'chain-e2e',
            'default', 'kimi-coding', 'k3', 'coder',
            'subscription_included', NULL, NULL,
            42668, 3202, 118784, 0, NULL, 'stop', NULL, 1970,
            ?, 'measured'
        )
        """,
        (started_iso,),
    )
    usage_events = reconcile_usage_facts(
        usage,
        subscription_fees={"kimi-coding": "30"},
        fee_version="e2e-fee-v1",
    )

    execution_id = stable_execution_id(
        "kanban_timeline", "task_run:2"
    )
    agent_events = activity_span_events(
        ActivityObservation(
            source_execution_id="task_run:2",
            execution_id=execution_id,
            span_key="agent",
            span_kind=SpanKind.AGENT,
            started_at_ms=now_ms + 2_030,
            ended_at_ms=now_ms + 3_900,
            terminal_run_id=terminal_run_id,
            task_run_id="2",
            agent_kind="codex",
            agent_provider="openai",
            agent_model="gpt-5.6-sol",
        )
    )
    subagent_events = activity_span_events(
        ActivityObservation(
            source_execution_id="task_run:2",
            execution_id=execution_id,
            span_key="subagent",
            span_kind=SpanKind.SUBAGENT,
            parent_span_id=agent_events[0].span_id,
            started_at_ms=now_ms + 2_100,
            ended_at_ms=now_ms + 2_500,
            terminal_run_id=terminal_run_id,
            task_run_id="2",
            agent_kind="reviewer",
        )
    )
    tool_started = int(time.time() * 1000)
    tool_process = subprocess.run(["/bin/true"], check=False)
    tool_ended = int(time.time() * 1000)
    tool_events = tool_span_events(
        ToolObservation(
            source_execution_id="task_run:2",
            execution_id=execution_id,
            raw_tool_name="functions.exec_command",
            parent_span_id=subagent_events[0].span_id,
            terminal_run_id=terminal_run_id,
            task_run_id="2",
            started_at_ms=tool_started,
            ended_at_ms=tool_ended,
            status="ok",
            agent_provider="openai",
            agent_model="gpt-5.6-sol",
            execution_phase="build",
            evidence_ref="e2e:tool:true",
        )
    )
    assert tool_process.returncode == 0
    test_started = int(time.time() * 1000)
    test_process = subprocess.run(
        [sys.executable, "-c", "assert 2 + 2 == 4"],
        check=False,
        capture_output=True,
        text=True,
    )
    test_ended = int(time.time() * 1000)
    test_events = build_test_span_events(
        ExecutionTestObservation(
            source_execution_id="task_run:2",
            execution_id=execution_id,
            parent_span_id=agent_events[0].span_id,
            terminal_run_id=terminal_run_id,
            task_run_id="2",
            started_at_ms=test_started,
            ended_at_ms=test_ended,
            selected_tests=1,
            executed_tests=1,
            passed_tests=1,
            failed_tests=0,
            skipped_tests=0,
            timeout_lost_tests=0,
            test_suite="execution_facts_e2e",
            gate_type="e2e",
            test_command_hash="b" * 64,
            status="passed",
            exit_code=test_process.returncode,
            agent_provider="openai",
            agent_model="gpt-5.6-sol",
            execution_phase="verify",
            evidence_ref="e2e:test:assertion",
        )
    )
    assert test_process.returncode == 0

    milestone_events = [
        make_event(
            source="kanban_timeline",
            source_execution_id="task_run:2",
            execution_id=execution_id,
            idempotency_key=stable_idempotency_key("e2e", phase.value),
            event_type=EventType.LIFECYCLE,
            validity=Validity.EXACT,
            observed_at_ms=now_ms + offset,
            recorded_at_ms=now_ms + offset,
            workload_kind=WorkloadKind.KANBAN_TASK,
            execution_surface=ExecutionSurface.HERMES,
            trigger_kind=TriggerKind.KANBAN,
            lifecycle_phase=phase,
            task_run_id="2",
            task_id="task-e2e",
            chain_id="chain-e2e",
            attributes=attributes,
        )
        for phase, offset, attributes in (
            (LifecyclePhase.REVIEW_STARTED, 4_010, {}),
            (LifecyclePhase.REVIEWED, 4_020, {"verdict": "accepted"}),
            (LifecyclePhase.INTEGRATION_STARTED, 4_030, {}),
            (LifecyclePhase.LANDED, 4_040, {"landed_sha": "a" * 40}),
        )
    ]
    systemd_process = subprocess.run(["/bin/true"], check=False)
    crontab_process = subprocess.run(["/bin/true"], check=False)
    assert systemd_process.returncode == 0
    assert crontab_process.returncode == 0
    system_events = reconcile_system_invocations(
        [
            SystemInvocationObservation(
                "systemd:e2e",
                ExecutionSurface.SYSTEMD,
                now_ms + 100,
                LifecyclePhase.SCHEDULED,
            ),
            SystemInvocationObservation(
                "systemd:e2e",
                ExecutionSurface.SYSTEMD,
                now_ms + 110,
                LifecyclePhase.PROCESS_STARTED,
            ),
            SystemInvocationObservation(
                "systemd:e2e",
                ExecutionSurface.SYSTEMD,
                now_ms + 120,
                LifecyclePhase.ENDED,
                status="ok",
                exit_code=0,
            ),
            SystemInvocationObservation(
                "crontab:e2e",
                ExecutionSurface.CRONTAB,
                now_ms + 130,
                LifecyclePhase.ENDED,
                status="ok",
                exit_code=0,
            ),
        ]
    )
    health_event = emitter_snapshot_event(
        EmitterSnapshot(
            mode="shadow",
            submitted=25,
            accepted=25,
            dropped_disabled=0,
            dropped_full=0,
            dropped_write_error=0,
            deduped=3,
            written=22,
            queue_depth=0,
            queue_capacity=4096,
            last_enqueue_at_ms=now_ms,
            last_write_at_ms=now_ms,
            last_error_class=None,
        ),
        collector_id="e2e",
        observed_at_ms=now_ms + 4_100,
    )
    all_events = [
        *reconcile_kanban(kanban),
        *reconcile_cron(cron),
        *loop_events,
        *terminal_events,
        *system_events,
        *usage_events,
        *agent_events,
        *subagent_events,
        *tool_events,
        *test_events,
        *milestone_events,
        health_event,
    ]
    append_reconciled(ledger, all_events)

    first_projection = rebuild_projections(database)
    second_projection = rebuild_projections(database)
    payload = build_execution_facts_payload(
        database,
        now=datetime.fromtimestamp((now_ms + 5_000) / 1000, tz=UTC),
    )
    with sqlite3.connect(database) as connection:
        binding = connection.execute(
            """
            SELECT terminal_run_id, task_run_id
              FROM task_binding_projection
             WHERE terminal_run_id = ? AND task_run_id = '2'
            """,
            (terminal_run_id,),
        ).fetchone()
        subagent_parent = connection.execute(
            """
            SELECT parent_span_id
              FROM execution_span_projection
             WHERE span_kind = 'subagent'
            """
        ).fetchone()
        content_rows = connection.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_json LIKE ?",
            (f"%{canary}%",),
        ).fetchone()[0]

    assert first_projection.digest == second_projection.digest
    assert binding == (terminal_run_id, "2")
    assert subagent_parent == (agent_events[0].span_id,)
    assert content_rows == 0
    assert payload["p0"]["kanban"]["percent"] == "100.00"
    assert payload["p0"]["terminal_identity"]["observed"] == 1
    assert payload["usage"]["costs"]["api_equivalent_total"] == "0.2116692"
    assert payload["usage"]["costs"]["allocated_subscription_total"] == "30"
    assert payload["activity_spans"]["agents"] >= 1
    assert payload["activity_spans"]["subagents"] == 1
    assert payload["activity_spans"]["operators"] == 0
    assert payload["tools"]["observed_spans"] == 1
    assert payload["tests"]["passed"] == 1
    assert payload["tests"]["executed"] == 1
    assert payload["retries"]["observed"] == 2
    assert payload["retries"]["eligible"] == 3
    assert payload["landing"]["sha_evidence"] == 1
    assert payload["collector"]["dropped"] == 0
    assert payload["collector"]["deduped"] == 3

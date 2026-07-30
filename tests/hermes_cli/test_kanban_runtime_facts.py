"""Behavioral tests for the queryable worker-run runtime timeline."""

from __future__ import annotations

import gc
import json
import os
import platform
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_facts as facts
from hermes_cli import kanban_worker_runtime as worker_runtime


def _claimed_run(conn, *, assignee: str = "coder") -> tuple[str, int]:
    task_id = kb.create_task(conn, title="runtime facts", assignee=assignee, kind="code")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["current_run_id"]
    assert isinstance(run_id, int)
    return task_id, run_id


def test_timeline_keeps_exact_run_link_and_ignores_stale_or_duplicate_event(kanban_home):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="queued",
            observed_at_ms=100,
            source="tasks.created_at",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=300,
            source="agent.conversation_loop",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=200,
            source="late_old_callback",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=300,
            source="duplicate_callback",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=400,
            source="later_turn",
            preserve_first=True,
        )

        timeline = facts.list_timeline(conn, task_run_id=run_id)

    first_token = next(item for item in timeline if item["event_kind"] == "first_token")
    assert (first_token["observed_at_ms"], first_token["source"]) == (
        300,
        "agent.conversation_loop",
    )
    assert all(item["task_run_id"] == run_id for item in timeline)
    assert all(item["task_id"] == task_id for item in timeline)


def test_spawn_started_environment_writer_uses_injected_run_identity(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)

    assert facts.record_event_from_environment(
        "spawn_started",
        source="kanban_db._launch_worker_process",
        observed_at_ms=500,
        env={
            "HERMES_KANBAN_RUN_ID": str(run_id),
            "HERMES_KANBAN_DB": str(kb.kanban_db_path()),
            "HERMES_KANBAN_BOARD": "default",
        },
    )

    with kb.connect_closing() as conn:
        spawn_started = next(
            item
            for item in facts.list_timeline(conn, task_run_id=run_id)
            if item["event_kind"] == "spawn_started"
        )

    assert (spawn_started["observed_at_ms"], spawn_started["source"]) == (
        500,
        "kanban_db._launch_worker_process",
    )


def test_direct_process_locator_has_only_pid_and_timeline_leaves_first_token_absent(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=4242, env={})
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_llm_request",
            observed_at_ms=110,
            source="agent.conversation_loop",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="process_started",
            observed_at_ms=123,
            source="kanban_db._set_worker_pid",
        )

        locator = facts.get_locator(conn, task_run_id=run_id)
        timeline = facts.list_timeline(conn, task_run_id=run_id)
        request_to_token = facts.calculate_latencies(conn, task_run_id=run_id)[
            "request_to_first_token_ms"
        ]

    assert locator == {"locator_type": "pid", "pid": 4242}
    assert {"first_llm_request", "process_started"} <= {
        item["event_kind"] for item in timeline
    }
    assert request_to_token is None


def test_tmux_locator_contains_session_window_and_pane_without_process_payload(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        facts.record_locator(
            conn,
            task_run_id=run_id,
            pid=9001,
            env={"TMUX_PANE": "%7"},
            tmux_display=lambda _pane: ("workers", "3", "%7"),
        )
        locator = facts.get_locator(conn, task_run_id=run_id)

    assert locator == {
        "locator_type": "tmux_pane",
        "pid": 9001,
        "tmux_session": "workers",
        "tmux_window": "3",
        "tmux_pane": "%7",
    }


@pytest.mark.parametrize(
    ("exit_code", "protocol_state", "outcome", "exit_kind"),
    [
        (0, "completed", "completed", "exited"),
        (17, "missing_completion", "crashed", "exited"),
        (None, "completed", "completed", "unobserved"),
        (0, "protocol_violation", "blocked", "exited"),
    ],
)
def test_terminal_facts_keep_process_protocol_outcome_and_reason_independent(
    kanban_home, exit_code, protocol_state, outcome, exit_kind
):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        kb._end_run(
            conn,
            _task_id,
            outcome=outcome,
            metadata={
                "worker_exit_kind": exit_kind,
                "worker_exit_code": exit_code,
                "worker_protocol_state": protocol_state,
                "worker_end_reason": "worker_process_observed",
            },
        )
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert terminal is not None
    assert terminal["worker_exit_code"] == exit_code
    assert terminal["worker_protocol_state"] == protocol_state
    assert terminal["task_outcome"] == outcome
    assert terminal["end_reason"] == "worker_process_observed"


def test_reaped_clean_exit_fills_process_facts_without_rewriting_terminal_meaning(
    kanban_home,
):
    with kb.connect_closing() as conn:
        conn.execute("ALTER TABLE task_runs ADD COLUMN worker_exit_kind TEXT")
        conn.execute("ALTER TABLE task_runs ADD COLUMN worker_exit_code INTEGER")
        conn.execute("ALTER TABLE task_runs ADD COLUMN worker_protocol_state TEXT")
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=4242, env={})
        kb._end_run(conn, task_id, outcome="completed")

        reconciled = facts.reconcile_reaped_worker_exits(
            conn,
            exits=[{
                "pid": 4242,
                "task_run_id": run_id,
                "task_id": task_id,
                "board": "default",
                "classified_kind": "clean_exit",
                "exit_code": 0,
            }],
            board="default",
        )
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert reconciled == [{"pid": 4242, "task_run_id": run_id}]
    assert terminal == {
        "task_run_id": run_id,
        "worker_exit_kind": "exited",
        "worker_exit_code": 0,
        "worker_protocol_state": "unobserved",
        "task_outcome": "completed",
        "end_reason": "completed",
        "task_id": task_id,
        "board": "default",
    }


@pytest.mark.parametrize(
    ("raw_status", "expected_returncode"),
    [(0, 0), (7 << 8, 7)],
)
@pytest.mark.skipif(os.name == "nt", reason="Windows has no waitpid reaper")
def test_reaped_worker_releases_retained_popen_with_exact_returncode(
    raw_status,
    expected_returncode,
):
    class FakeProcess:
        pid = 414141
        returncode = None

    process = FakeProcess()
    assert facts.retain_worker_process_handle(process) == process.pid

    kb._record_worker_exit(process.pid, raw_status)

    assert process.returncode == expected_returncode
    assert not facts.release_worker_process_handle(process.pid, raw_status)


def test_windows_worker_handle_is_not_retained_without_a_waitpid_reaper(
    monkeypatch,
):
    class FakeProcess:
        pid = 424242
        returncode = None

    process = FakeProcess()
    monkeypatch.setattr(facts.os, "name", "nt")

    assert facts.retain_worker_process_handle(process) == process.pid
    assert not facts.release_worker_process_handle(process.pid, 0)
    assert process.returncode is None


def test_worker_launcher_retains_popen_handle_until_parent_reaper(
    monkeypatch,
    tmp_path,
):
    class FakeProcess:
        pid = 515151
        returncode = None

    process = FakeProcess()
    retained = []

    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(kb, "_maybe_scope_worker_cmd", lambda argv: argv)
    monkeypatch.setattr(
        facts,
        "record_event_from_environment",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        facts,
        "retain_worker_process_handle",
        lambda candidate: retained.append(candidate) or candidate.pid,
    )

    pid = kb._launch_worker_process(
        worker_runtime.WorkerLaunchSpec(
            argv=("hermes", "chat"),
            env={},
            cwd=None,
            log_path=tmp_path / "worker.log",
            missing_executable_message="missing",
        )
    )

    assert pid == process.pid
    assert retained == [process]


@pytest.mark.skipif(
    os.name == "nt" or platform.python_implementation() != "CPython",
    reason="regression pins CPython's POSIX Popen cleanup behavior",
)
def test_real_popen_cleanup_cannot_steal_retained_worker_exit(
    tmp_path,
):
    # Control: dropping the only reference while the delayed child still runs
    # registers it in subprocess._active. A later Popen constructor cleans that
    # list and reaps the child before a dispatcher waitpid can observe it.
    control = subprocess.Popen(  # noqa: S603 -- fixed test interpreter argv
        [
            sys.executable,
            "-c",
            "import sys,time; time.sleep(0.15); sys.exit(9)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    control_pid = control.pid
    del control
    gc.collect()
    time.sleep(0.25)
    trigger = subprocess.Popen(  # noqa: S603 -- fixed test interpreter argv
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert trigger.wait(timeout=5) == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(control_pid, os.WNOHANG)

    # Production path: _launch_worker_process keeps a strong handle outside
    # subprocess._active. The same later-Popen cleanup cannot steal its status,
    # and the dispatcher reaper records the exact nonzero exit.
    retained_pid = kb._launch_worker_process(
        worker_runtime.WorkerLaunchSpec(
            argv=(
                sys.executable,
                "-c",
                "import sys,time; time.sleep(0.15); sys.exit(7)",
            ),
            env={},
            cwd=None,
            log_path=tmp_path / "real-worker.log",
            missing_executable_message="missing",
        )
    )
    time.sleep(0.25)
    trigger = subprocess.Popen(  # noqa: S603 -- fixed test interpreter argv
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert trigger.wait(timeout=5) == 0

    assert retained_pid in kb.reap_worker_zombies()
    assert kb._classify_worker_exit(retained_pid) == ("nonzero_exit", 7)


def test_spawn_and_reap_serialize_numeric_pid_generations(
    monkeypatch,
    tmp_path,
):
    reused_pid = 616161
    wait_entered = threading.Event()
    allow_wait_to_return = threading.Event()
    popen_entered = threading.Event()
    wait_calls = 0

    def fake_waitpid(_pid, _flags):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            wait_entered.set()
            assert allow_wait_to_return.wait(timeout=5)
            return reused_pid, 7 << 8
        raise ChildProcessError

    class NewGenerationProcess:
        pid = reused_pid
        returncode = None

    new_process = NewGenerationProcess()

    def fake_popen(*args, **kwargs):
        popen_entered.set()
        return new_process

    monkeypatch.setattr(kb.os, "waitpid", fake_waitpid)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(kb, "_maybe_scope_worker_cmd", lambda argv: argv)
    monkeypatch.setattr(
        facts,
        "record_event_from_environment",
        lambda *args, **kwargs: True,
    )

    reaped: list[int] = []
    spawned: list[int] = []
    reaper = threading.Thread(target=lambda: reaped.extend(kb.reap_worker_zombies()))
    spawner = threading.Thread(
        target=lambda: spawned.append(
            kb._launch_worker_process(
                worker_runtime.WorkerLaunchSpec(
                    argv=("hermes", "chat"),
                    env={},
                    cwd=None,
                    log_path=tmp_path / "reused-pid-worker.log",
                    missing_executable_message="missing",
                )
            )
        )
    )

    reaper.start()
    assert wait_entered.wait(timeout=5)
    spawner.start()
    assert not popen_entered.wait(timeout=0.1)
    allow_wait_to_return.set()
    reaper.join(timeout=5)
    spawner.join(timeout=5)

    assert not reaper.is_alive()
    assert not spawner.is_alive()
    assert reaped == [reused_pid]
    assert spawned == [reused_pid]
    assert new_process.returncode is None
    assert facts.release_worker_process_handle(reused_pid, 0)
    assert new_process.returncode == 0


def test_reaped_pid_reuse_updates_only_the_exact_historical_run(kanban_home):
    with kb.connect_closing() as conn:
        old_task, old_run = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=old_run, pid=5151, env={})
        kb._end_run(conn, old_task, outcome="completed")
        _new_task, new_run = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=new_run, pid=5151, env={})

        reconciled = facts.reconcile_reaped_worker_exits(
            conn,
            exits=[{
                "pid": 5151,
                "task_run_id": old_run,
                "task_id": old_task,
                "board": "default",
                "classified_kind": "clean_exit",
                "exit_code": 0,
            }],
            board="default",
        )
        old_terminal = facts.get_terminal_facts(conn, task_run_id=old_run)

    assert new_run > old_run
    assert reconciled == [{"pid": 5151, "task_run_id": old_run}]
    assert old_terminal["worker_exit_kind"] == "exited"
    assert old_terminal["worker_exit_code"] == 0


def test_reaped_exit_never_overwrites_an_already_observed_process_fact(kanban_home):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=6262, env={})
        kb._end_run(
            conn,
            task_id,
            outcome="completed",
            metadata={
                "worker_exit_kind": "exited",
                "worker_exit_code": 0,
                "worker_protocol_state": "completed",
                "worker_end_reason": "worker_reported",
            },
        )

        reconciled = facts.reconcile_reaped_worker_exits(
            conn,
            exits=[{
                "pid": 6262,
                "task_run_id": run_id,
                "task_id": task_id,
                "board": "default",
                "classified_kind": "nonzero_exit",
                "exit_code": 17,
            }],
            board="default",
        )
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert reconciled == [{"pid": 6262, "task_run_id": run_id}]
    assert terminal["worker_exit_kind"] == "exited"
    assert terminal["worker_exit_code"] == 0
    assert terminal["worker_protocol_state"] == "completed"
    assert terminal["end_reason"] == "worker_reported"


def test_dispatch_tick_reconciles_the_reaped_pid_before_other_maintenance(
    kanban_home,
    monkeypatch,
):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=7373, env={})
        kb._end_run(conn, task_id, outcome="completed")
        kb._register_worker_process_identity(
            7373,
            board="default",
            task_run_id=run_id,
            task_id=task_id,
        )
        monkeypatch.setattr(
            kb,
            "reap_worker_zombies",
            lambda: kb._record_worker_exit(7373, 0),
        )

        kb.dispatch_once(conn, dry_run=True, max_spawn=0)
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert terminal["worker_exit_kind"] == "exited"
    assert terminal["worker_exit_code"] == 0


def test_reaped_exit_is_retained_until_its_exact_board_dispatches(
    kanban_home,
):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=8484, env={})
        kb._end_run(conn, task_id, outcome="completed")
        kb._register_worker_process_identity(
            8484,
            board="default",
            task_run_id=run_id,
            task_id=task_id,
        )
        kb._record_worker_exit(8484, 0)

        assert kb._reaped_worker_exits_for_board("other") == []
        assert kb._reconcile_worker_exits_for_board(conn) == [run_id]
        assert kb._reaped_worker_exits_for_board("default") == []


def test_reap_registry_keeps_two_pid_generations_until_exact_ack() -> None:
    pid = 8585
    kb._register_worker_process_identity(
        pid,
        board="board-a",
        task_run_id=101,
        task_id="task-a",
    )
    kb._record_worker_exit(pid, 0)
    kb._register_worker_process_identity(
        pid,
        board="board-b",
        task_run_id=202,
        task_id="task-b",
    )
    kb._record_worker_exit(pid, 7 << 8)

    board_a = kb._reaped_worker_exits_for_board("board-a")
    board_b = kb._reaped_worker_exits_for_board("board-b")
    assert board_a[0]["task_run_id"] == 101
    assert board_a[0]["exit_code"] == 0
    assert board_b[0]["task_run_id"] == 202
    assert board_b[0]["exit_code"] == 7

    assert kb._acknowledge_reaped_worker_exit(
        pid=pid,
        board="board-a",
        task_run_id=101,
    )
    assert kb._reaped_worker_exits_for_board("board-a") == []
    assert kb._reaped_worker_exits_for_board("board-b") == board_b


def test_registration_adopts_a_just_reaped_unbound_worker_exit() -> None:
    pid = 8686
    kb._record_worker_exit(pid, 0)

    kb._register_worker_process_identity(
        pid,
        board="default",
        task_run_id=303,
        task_id="task-race",
    )

    records = kb._reaped_worker_exits_for_board("default")
    assert records == [{
        "pid": pid,
        "task_run_id": 303,
        "task_id": "task-race",
        "board": "default",
        "classified_kind": "clean_exit",
        "exit_code": 0,
    }]


def test_concurrent_registration_and_reap_preserve_exact_worker_identity() -> None:
    pid = 8736
    barrier = threading.Barrier(3)

    def register() -> None:
        barrier.wait()
        kb._register_worker_process_identity(
            pid,
            board="default",
            task_run_id=304,
            task_id="task-concurrent-race",
        )

    def reap() -> None:
        barrier.wait()
        kb._record_worker_exit(pid, 0)

    register_thread = threading.Thread(target=register)
    reap_thread = threading.Thread(target=reap)
    register_thread.start()
    reap_thread.start()
    barrier.wait()
    register_thread.join(timeout=5)
    reap_thread.join(timeout=5)

    assert not register_thread.is_alive()
    assert not reap_thread.is_alive()
    records = [
        record
        for record in kb._reaped_worker_exits_for_board("default")
        if record["pid"] == pid
    ]
    assert records == [{
        "pid": pid,
        "task_run_id": 304,
        "task_id": "task-concurrent-race",
        "board": "default",
        "classified_kind": "clean_exit",
        "exit_code": 0,
    }]


def test_current_pid_generation_never_falls_back_to_older_exit() -> None:
    pid = 8746
    kb._register_worker_process_identity(
        pid,
        board="previous",
        task_run_id=305,
        task_id="task-previous-generation",
    )
    kb._record_worker_exit(pid, 0)
    kb._register_worker_process_identity(
        pid,
        board="current",
        task_run_id=306,
        task_id="task-current-generation",
    )

    assert kb._classify_worker_exit(pid) == ("unknown", None)
    assert kb._reaped_worker_exits_for_board("previous")[0]["task_run_id"] == 305
    assert kb._reaped_worker_exits_for_board("current") == []


def test_exact_old_exit_wins_over_liveness_of_reused_pid_generation(
    kanban_home,
    monkeypatch,
) -> None:
    pid = 8787
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=pid, env={})
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (pid, task_id),
        )
        kb._register_worker_process_identity(
            pid,
            board="default",
            task_run_id=run_id,
            task_id=task_id,
        )
        kb._record_worker_exit(pid, 0)
        kb._register_worker_process_identity(
            pid,
            board="other",
            task_run_id=404,
            task_id="new-generation",
        )
        monkeypatch.setattr(kb, "reap_worker_zombies", lambda: None)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: pytest.fail(
                "must not signal a reused PID generation"
            ),
        )

        kb.dispatch_once(conn, dry_run=True, max_spawn=0)
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)
        task_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()["status"]

    assert task_status != "running"
    assert terminal["worker_exit_kind"] == "exited"
    assert terminal["worker_exit_code"] == 0


def test_active_same_tick_crash_reconciles_after_detector_ends_run(
    kanban_home,
    monkeypatch,
):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=9595, env={})
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (9595, task_id),
        )
        kb._register_worker_process_identity(
            9595,
            board="default",
            task_run_id=run_id,
            task_id=task_id,
        )
        monkeypatch.setattr(kb, "reap_worker_zombies", lambda: None)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        kb._record_worker_exit(9595, 9 << 8)

        kb.dispatch_once(conn, dry_run=True, max_spawn=0)
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert terminal["worker_exit_kind"] == "exited"
    assert terminal["worker_exit_code"] == 9


def test_exit_reconciliation_rolls_back_both_projections_on_mirror_failure(
    kanban_home,
):
    with kb.connect_closing() as conn:
        conn.execute("ALTER TABLE task_runs ADD COLUMN worker_exit_kind TEXT")
        conn.execute("ALTER TABLE task_runs ADD COLUMN worker_exit_code INTEGER")
        conn.execute("ALTER TABLE task_runs ADD COLUMN worker_protocol_state TEXT")
        task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=9696, env={})
        kb._end_run(conn, task_id, outcome="completed")

        class FaultConnection:
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE task_runs SET"):
                    raise sqlite3.OperationalError("synthetic mirror failure")
                return conn.execute(sql, parameters)

        reconciled = facts.reconcile_reaped_worker_exits(
            FaultConnection(),
            exits=[{
                "pid": 9696,
                "task_run_id": run_id,
                "task_id": task_id,
                "board": "default",
                "classified_kind": "clean_exit",
                "exit_code": 0,
            }],
            board="default",
        )
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert reconciled == []
    assert terminal["worker_exit_kind"] == "unobserved"
    assert terminal["worker_exit_code"] is None


def test_worker_terminal_tools_stamp_observed_protocol_and_end_reason(
    kanban_home,
):
    with kb.connect_closing() as conn:
        complete_task_id, complete_run_id = _claimed_run(conn)
        assert kb.complete_task(
            conn,
            complete_task_id,
            expected_run_id=complete_run_id,
        )
        completed = facts.get_terminal_facts(
            conn,
            task_run_id=complete_run_id,
        )

        block_task_id, block_run_id = _claimed_run(conn)
        assert kb.block_task(
            conn,
            block_task_id,
            reason="controlled test block",
            expected_run_id=block_run_id,
        )
        blocked = facts.get_terminal_facts(conn, task_run_id=block_run_id)

    assert completed["worker_protocol_state"] == "completed"
    assert completed["end_reason"] == "kanban_complete"
    assert blocked["worker_protocol_state"] == "blocked"
    assert blocked["end_reason"] == "kanban_block"


@pytest.mark.parametrize(
    ("event_kind", "retry_class", "payload", "pass_run_id"),
    [
        ("auto_retried", "auto", {"blocked_run_id": "predecessor"}, False),
        (kb.INTEGRATION_RETRY_EVENT, "integration", {}, False),
        (kb.TRANSIENT_RETRY_EVENT, "transient", {}, True),
        ("unblocked", "operator", {}, False),
    ],
)
def test_append_event_stages_exact_retry_relationship_for_next_claim(
    kanban_home, event_kind, retry_class, payload, pass_run_id
):
    with kb.connect_closing() as conn:
        task_id, predecessor_id = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL WHERE id = ?",
            (task_id,),
        )
        resolved_payload = {
            key: predecessor_id if value == "predecessor" else value
            for key, value in payload.items()
        }
        event_id = kb._append_event(
            conn,
            task_id,
            event_kind,
            resolved_payload,
            run_id=predecessor_id if pass_run_id else None,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        retry_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["current_run_id"]

        link = facts.get_retry_link(conn, task_run_id=retry_id)
        event = conn.execute(
            "SELECT task_id, kind FROM task_events WHERE id = ?", (event_id,)
        ).fetchone()

    assert link is not None
    assert link["retry_of_task_run_id"] == predecessor_id
    assert link["retry_class"] == retry_class
    assert link["triggering_event_id"] == event_id
    assert link["task_id"] == task_id
    assert link["board"] == "default"
    assert event["task_id"] == task_id
    assert event["kind"] == event_kind


def _current_run(conn, task_id: str) -> int:
    return conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["current_run_id"]


def _pending_retry_rows(conn, task_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pending_worker_run_retry_links WHERE task_id = ?",
        (task_id,),
    ).fetchone()[0]


def _park(conn, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL "
        "WHERE id = ?",
        (status, task_id),
    )


def test_retry_event_classes_are_pinned_to_the_lifecycle_event_constants():
    assert facts.RETRY_EVENT_CLASSES == {
        "auto_retried": "auto",
        kb.INTEGRATION_RETRY_EVENT: "integration",
        kb.TRANSIENT_RETRY_EVENT: "transient",
        "unblocked": "operator",
    }
    assert set(facts.RETRY_EVENT_CLASSES.values()) <= facts.RETRY_CLASSES


def test_review_claim_consumes_lineage_so_a_later_coder_claim_inherits_nothing(
    kanban_home,
):
    """The review-lane run is the retry; a later coder run must stay unlinked."""
    with kb.connect_closing() as conn:
        task_id, crashed_run = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        _park(conn, task_id, "review")
        event_id = kb._append_event(
            conn, task_id, kb.TRANSIENT_RETRY_EVENT, {}, run_id=crashed_run
        )
        staged = conn.execute(
            "SELECT retry_of_task_run_id, retry_class FROM "
            "pending_worker_run_retry_links WHERE task_id = ?",
            (task_id,),
        ).fetchone()

        assert kb.claim_review_task(conn, task_id, reviewer_profile="reviewer") is not None
        review_run = _current_run(conn, task_id)
        review_link = facts.get_retry_link(conn, task_run_id=review_run)
        pending_after_review = _pending_retry_rows(conn, task_id)

        kb._end_run(conn, task_id, outcome="completed")
        _park(conn, task_id, "ready")
        assert kb.claim_task(conn, task_id) is not None
        coder_run = _current_run(conn, task_id)
        coder_link = facts.get_retry_link(conn, task_run_id=coder_run)
        pending_after_coder = _pending_retry_rows(conn, task_id)
        trigger = conn.execute(
            "SELECT task_id, kind FROM task_events WHERE id = ?", (event_id,)
        ).fetchone()

    assert (staged["retry_of_task_run_id"], staged["retry_class"]) == (
        crashed_run,
        "transient",
    )
    assert crashed_run < review_run < coder_run
    assert review_link is not None
    assert review_link["task_run_id"] == review_run
    assert review_link["retry_of_task_run_id"] == crashed_run
    assert review_link["retry_class"] == "transient"
    assert review_link["triggering_event_id"] == event_id
    assert review_link["task_id"] == task_id
    assert review_link["board"] == "default"
    assert (trigger["task_id"], trigger["kind"]) == (task_id, kb.TRANSIENT_RETRY_EVENT)
    assert coder_link is None
    assert (pending_after_review, pending_after_coder) == (0, 0)


def test_retry_event_leaves_lineage_absent_while_a_run_is_active(kanban_home):
    """With an active run the latest ended run is not the predecessor."""
    with kb.connect_closing() as conn:
        task_id, ended_run = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        _park(conn, task_id, "ready")
        assert kb.claim_task(conn, task_id) is not None
        active_run = _current_run(conn, task_id)

        kb._append_event(conn, task_id, "unblocked", {})
        pending_while_active = _pending_retry_rows(conn, task_id)

        # Control: the very same event does stage once no run is active, so the
        # empty result above is the guard and not a broken event path.
        kb._end_run(conn, task_id, outcome="crashed")
        kb._append_event(conn, task_id, "unblocked", {})
        staged_when_idle = conn.execute(
            "SELECT retry_of_task_run_id, retry_class FROM "
            "pending_worker_run_retry_links WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    assert ended_run < active_run
    assert pending_while_active == 0
    assert (staged_when_idle["retry_of_task_run_id"], staged_when_idle["retry_class"]) == (
        active_run,
        "operator",
    )


def test_invalid_staged_retry_link_is_discarded_instead_of_blocking_the_claim(
    kanban_home,
):
    with kb.connect_closing() as conn:
        task_id, first_run = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        _park(conn, task_id, "ready")
        event_id = kb._append_event(
            conn, task_id, "operator_retry", {"attempt": 1}, run_id=first_run
        )
        unknown_run = first_run + 10_000
        facts.stage_retry_link(
            conn,
            task_id=task_id,
            retry_of_task_run_id=unknown_run,
            retry_class="operator",
            triggering_event_id=event_id,
            board="default",
        )

        claimed = kb.claim_task(conn, task_id)
        retry_run = _current_run(conn, task_id)
        link = facts.get_retry_link(conn, task_run_id=retry_run)
        pending_after_claim = _pending_retry_rows(conn, task_id)
        rejected = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'retry_link_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        discarded = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'pending_retry_link_discarded' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert claimed is not None
    assert link is None
    assert pending_after_claim == 0
    assert json.loads(rejected["payload"])["finding"] == "missing_predecessor"
    discarded_payload = json.loads(discarded["payload"])
    assert discarded_payload["finding"] == "staged_link_no_longer_valid"
    assert discarded_payload["retry_of_task_run_id"] == unknown_run
    assert discarded_payload["task_run_id"] == retry_run


def test_foreign_task_retry_link_is_rejected_with_structured_diagnostic(kanban_home):
    with kb.connect_closing() as conn:
        first_task, foreign_run_id = _claimed_run(conn)
        kb._end_run(conn, first_task, outcome="crashed")
        second_task, second_run_id = _claimed_run(conn)
        event_id = conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'operator_retry', '{}', 1)",
            (second_task,),
        ).lastrowid

        with pytest.raises(ValueError, match="foreign_task_predecessor"):
            facts.record_retry_link(
                conn,
                task_run_id=second_run_id,
                retry_of_task_run_id=foreign_run_id,
                retry_class="operator",
                triggering_event_id=event_id,
            )
        diagnostic = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'retry_link_rejected'",
            (second_task,),
        ).fetchone()

    assert json.loads(diagnostic["payload"])["finding"] == "foreign_task_predecessor"


def test_retry_link_rejects_non_previous_predecessor(kanban_home):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        event_id = kb._append_event(
            conn,
            task_id,
            kb.TRANSIENT_RETRY_EVENT,
            {"attempt": 1},
            run_id=run_id,
        )

        with pytest.raises(ValueError, match="cyclic_or_non_previous_predecessor"):
            facts.record_retry_link(
                conn,
                task_run_id=run_id,
                retry_of_task_run_id=run_id,
                retry_class="transient",
                triggering_event_id=event_id,
            )
        diagnostic = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'retry_link_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert diagnostic is not None
    assert json.loads(diagnostic["payload"])["finding"] == (
        "cyclic_or_non_previous_predecessor"
    )


def test_worker_session_reuse_does_not_create_or_replace_retry_link(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            ('{"worker_session_id":"claude-session-1"}', run_id),
        )

        assert facts.get_retry_link(conn, task_run_id=run_id) is None


def test_claim_to_end_preserves_exact_worker_runtime_metadata(
    kanban_home, monkeypatch
) -> None:
    with kb.connect_closing() as conn:
        for runtime in ("hermes", "claude-cli"):
            task_id = kb.create_task(
                conn, title=f"runtime-{runtime}", assignee="coder"
            )
            monkeypatch.setattr(
                kb,
                "_spawn_identity_metadata",
                lambda *args, _runtime=runtime, **kwargs: {
                    "worker_runtime": _runtime,
                    "route_provider": "test-provider",
                    "model_source": "profile",
                },
            )
            claimed = kb.claim_task(conn, task_id)
            assert claimed is not None
            run_id = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["current_run_id"]

            kb._end_run(
                conn,
                task_id,
                outcome="completed",
                metadata={
                    "completion_fact": runtime,
                    "provider": "terminal-provider",
                    "model": "terminal-model",
                },
            )

            stored = json.loads(
                conn.execute(
                    "SELECT metadata FROM task_runs WHERE id = ?", (run_id,)
                ).fetchone()[0]
            )
            assert stored["worker_runtime"] == runtime
            assert stored["route_provider"] == "test-provider"
            assert stored["model_source"] == "profile"
            assert stored["completion_fact"] == runtime
            assert stored["provider"] == "terminal-provider"
            assert stored["model"] == "terminal-model"
            assert "cost_source" not in stored

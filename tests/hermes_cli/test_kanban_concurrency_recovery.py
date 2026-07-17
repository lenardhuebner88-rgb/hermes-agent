"""Deterministic concurrency and crash-recovery contracts for Kanban workers.

The tests drive production transition APIs.  OS/process probes and explicit
crash boundaries are replaceable test seams; claim, dispatch, requeue,
completion, blocking, and integration logic are never replaced by assertions
about their own mocks.  ``Cx`` labels map to the concurrency report dated
2026-07-18.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt


class SimulatedProcessDeath(BaseException):
    """Stop a call at an exact crash boundary without running its handlers."""


def _absent_worker(*_args, **_kwargs) -> dict:
    return {
        "host_local": True,
        "termination_attempted": True,
        "terminated": True,
        "sigkill": False,
        "group_alive_after": False,
    }


def _claim_and_spawn(conn, task_id: str, pid: int):
    task = kb.claim_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    kb._set_worker_pid(conn, task_id, pid)
    return task


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "concurrency@example.invalid")
    _git(repo, "config", "user.name", "Concurrency Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def test_c1_double_dispatch_has_one_claim_and_one_spawn(
    kanban_home, all_assignees_spawnable
):
    """C1: the board dispatch lock makes a recursive rival tick lose cleanly."""
    rival_results = []
    spawn_calls = []

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="single dispatch", assignee="worker")

        def spawn_once(task, _workspace):
            spawn_calls.append(task.id)
            with kb.connect_closing() as rival_conn:
                rival_results.append(
                    kb.dispatch_once(
                        rival_conn,
                        spawn_fn=lambda *_args: 20202,
                        serialize_by_repo=False,
                    )
                )
            return 10101

        winner = kb.dispatch_once(
            conn,
            spawn_fn=spawn_once,
            serialize_by_repo=False,
        )
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert spawn_calls == [task_id]
    assert [item[0] for item in winner.spawned] == [task_id]
    assert len(rival_results) == 1 and rival_results[0].skipped_locked is True
    assert task.status == "running" and task.worker_pid == 10101
    assert len(runs) == 1 and runs[0].status == "running"


def test_c2_claim_without_spawn_recovers_after_ttl(kanban_home, monkeypatch):
    """C2: death after claim but before spawn is bounded by the claim TTL."""
    clock = [1_900_000_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _absent_worker)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="dies before spawn", assignee="worker")
        claimed = kb.claim_task(conn, task_id, ttl_seconds=5)
        assert claimed is not None and claimed.worker_pid is None
        run_id = claimed.current_run_id

        clock[0] += 6
        assert kb.release_stale_claims(conn) == 1
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)

    assert task.status == "ready"
    assert task.claim_lock is None and task.claim_expires is None
    assert task.worker_pid is None and task.current_run_id is None
    assert run.status == "reclaimed" and run.ended_at is not None


def test_c3_expired_old_generation_cannot_finish_new_claim(
    kanban_home, monkeypatch
):
    """C3: reclaim changes generation; late terminal calls from run 1 lose."""
    clock = [1_900_100_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _absent_worker)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="generation fence", assignee="worker")
        run1 = _claim_and_spawn(conn, task_id, 30301).current_run_id
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (clock[0] - 1, task_id),
            )
        assert kb.release_stale_claims(conn) == 1

        run2_task = kb.claim_task(conn, task_id, ttl_seconds=30)
        assert run2_task is not None and run2_task.current_run_id != run1
        run2 = run2_task.current_run_id

        assert not kb.complete_task(
            conn,
            task_id,
            result="late old result",
            expected_run_id=run1,
        )
        assert not kb.block_task(
            conn,
            task_id,
            reason="late old block",
            kind="transient",
            expected_run_id=run1,
        )
        current = kb.get_task(conn, task_id)

    assert current.status == "running"
    assert current.current_run_id == run2
    assert current.claim_lock == run2_task.claim_lock


@pytest.mark.xfail(
    strict=True,
    reason="C4: crash/protocol breaker omits block_kind; fixed on Turn-1 branch",
)
def test_c4_clean_exit_without_terminal_call_is_bounded_and_typed(
    kanban_home, monkeypatch
):
    """C4: rc=0 without complete/block ends in a typed terminal policy state."""
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _absent_worker)
    kb._recent_worker_exits.clear()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="clean exit protocol miss",
            assignee="worker",
            max_retries=3,
        )
        for attempt in range(1, 4):
            pid = 40400 + attempt
            _claim_and_spawn(conn, task_id, pid)
            kb._record_worker_exit(pid, 0)
            assert kb.detect_crashed_workers(conn) == [task_id]

        task = kb.get_task(conn, task_id)
        protocol_events = _event_payloads(conn, task_id, "protocol_violation")

    assert task.status == "blocked"
    assert task.block_kind is not None
    assert task.claim_lock is None and task.worker_pid is None
    assert len(protocol_events) == 3


def test_c5_auto_requeue_racing_manual_complete_has_done_dominance(
    kanban_home, monkeypatch
):
    """C5: a completion injected after retry scan wins without a stale retry."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="retry versus done", assignee="worker")
        run_id = kb.claim_task(conn, task_id).current_run_id
        assert kb.block_task(
            conn,
            task_id,
            reason="temporary infrastructure failure",
            kind="transient",
            expected_run_id=run_id,
        )

        original = kb._latest_auto_retry_body_hash
        injected = [False]

        def complete_between_scan_and_retry(read_conn, scanned_task_id):
            if not injected[0]:
                injected[0] = True
                with kb.connect_closing() as operator_conn:
                    assert kb.complete_task(
                        operator_conn,
                        task_id,
                        result="operator accepted result",
                    )
            return original(read_conn, scanned_task_id)

        monkeypatch.setattr(
            kb,
            "_latest_auto_retry_body_hash",
            complete_between_scan_and_retry,
        )
        assert kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=2,
        ) == []
        task = kb.get_task(conn, task_id)
        kinds = [event.kind for event in kb.list_events(conn, task_id)]

    assert injected == [True]
    assert task.status == "done"
    assert "completed" in kinds
    assert "auto_retried" not in kinds


def test_c6_event_failure_rolls_back_the_status_transition(
    kanban_home, monkeypatch
):
    """C6: task status, run close, and completed event share one transaction."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="atomic completion", assignee="worker")
        run_id = kb.claim_task(conn, task_id).current_run_id
        original = kb._append_event

        def fail_completed_event(event_conn, event_task_id, kind, *args, **kwargs):
            if event_task_id == task_id and kind == "completed":
                raise RuntimeError("injected event append failure")
            return original(event_conn, event_task_id, kind, *args, **kwargs)

        monkeypatch.setattr(kb, "_append_event", fail_completed_event)
        with pytest.raises(RuntimeError, match="injected event append failure"):
            kb.complete_task(
                conn,
                task_id,
                result="must roll back",
                expected_run_id=run_id,
            )

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        completed = _event_payloads(conn, task_id, "completed")

    assert task.status == "running"
    assert task.current_run_id == run_id and task.claim_lock
    assert run.status == "running" and run.ended_at is None
    assert completed == []


@pytest.mark.xfail(
    strict=True,
    reason="C7: _set_worker_pid has no status/run/claim ownership CAS",
)
def test_c7_spawn_pid_write_rejects_a_task_completed_during_spawn(
    kanban_home, all_assignees_spawnable
):
    """C7: a stale spawn generation cannot attach its PID to a done task."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="complete during spawn", assignee="worker")

        def complete_then_return_pid(_task, _workspace):
            with kb.connect_closing() as operator_conn:
                assert kb.complete_task(
                    operator_conn,
                    task_id,
                    result="operator completed while spawn was in flight",
                )
            return 70707

        result = kb.dispatch_once(
            conn,
            spawn_fn=complete_then_return_pid,
            serialize_by_repo=False,
        )
        task = kb.get_task(conn, task_id)
        spawned = _event_payloads(conn, task_id, "spawned")

    assert task.status == "done"
    assert task.worker_pid is None and task.claim_lock is None
    assert spawned == []
    assert result.spawned == []


@pytest.mark.xfail(
    strict=True,
    reason="C8: child PID exists only in parent memory until post-spawn DB write",
)
def test_c8_dispatcher_death_after_spawn_does_not_requeue_unknown_live_child(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """C8: a forked child must be fenced before a TTL recovery may requeue."""
    clock = [1_900_200_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    def die_before_pid_persist(*_args, **_kwargs):
        raise SimulatedProcessDeath()

    monkeypatch.setattr(kb, "_set_worker_pid", die_before_pid_persist)
    termination_pids = []

    def record_termination(pid, *_args, **_kwargs):
        termination_pids.append(pid)
        return _absent_worker()

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", record_termination)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="crash after fork", assignee="worker")
        with pytest.raises(SimulatedProcessDeath):
            kb.dispatch_once(
                conn,
                spawn_fn=lambda *_args: 80808,
                ttl_seconds=5,
                serialize_by_repo=False,
            )
        claimed = kb.get_task(conn, task_id)
        assert claimed.status == "running" and claimed.worker_pid is None

        clock[0] += 6
        kb.release_stale_claims(conn)
        recovered = kb.get_task(conn, task_id)

    assert termination_pids == [80808]
    assert recovered.status != "ready"


@pytest.mark.xfail(
    strict=True,
    reason="C9: git cleanup precedes durable integration witnesses",
)
def test_c9_integration_crash_before_witness_recovers_without_false_park(
    kanban_home, tmp_path, monkeypatch
):
    """C9: a landed merge must remain recoverable if DB witness append crashes."""
    repo = _new_repo(tmp_path)
    monkeypatch.setattr(kwt, "default_quick_gate", lambda *_args: (True, "green"))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="integration witness crash",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.claim_task(conn, task_id)
        worktree = kwt.provision_for_task(conn, task, str(repo))
        (worktree / "feature.py").write_text("VALUE = 9\n", encoding="utf-8")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", "worker change")

        original = kwt._record_integration_events_and_receipts

        def die_before_witness(*_args, **_kwargs):
            raise SimulatedProcessDeath()

        monkeypatch.setattr(
            kwt,
            "_record_integration_events_and_receipts",
            die_before_witness,
        )
        with pytest.raises(SimulatedProcessDeath):
            kb.complete_task(conn, task_id, result="done")

        assert (repo / "feature.py").read_text(encoding="utf-8") == "VALUE = 9\n"
        assert _event_payloads(conn, task_id, "integration_merged") == []
        monkeypatch.setattr(
            kwt,
            "_record_integration_events_and_receipts",
            original,
        )

        assert kb.complete_task(conn, task_id, result="retry completion")
        recovered = kb.get_task(conn, task_id)

    assert recovered.status == "done"


@pytest.mark.xfail(
    strict=True,
    reason="C10: done commit clears PID before out-of-txn worker reap",
)
def test_c10_manual_complete_crash_after_commit_retains_worker_fence(
    kanban_home, monkeypatch
):
    """C10: post-commit reaper death must not erase every trace of a live PID."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)

    def die_during_reap(*_args, **_kwargs):
        raise SimulatedProcessDeath()

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", die_during_reap)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="manual done reap", assignee="worker")
        _claim_and_spawn(conn, task_id, 100010)
        with pytest.raises(SimulatedProcessDeath):
            kb.complete_task(conn, task_id, result="manual terminal")

        task = kb.get_task(conn, task_id)
        reaped = _event_payloads(conn, task_id, "worker_reaped")

    assert task.status == "done"
    assert task.worker_pid == 100010 or reaped

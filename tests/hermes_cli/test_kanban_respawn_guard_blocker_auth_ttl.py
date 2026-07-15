"""Bounded respawn guarding for quota/auth failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


AUTH_ERROR = "provider returned 429 rate-limit"
BASE_TIME = 1_900_000_000
DISPATCH_TICK_SECONDS = 60
MAX_DISPATCH_TICKS = 20


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with the production Kanban schema."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _record_production_shaped_crash(conn, task_id: str, *, error: str) -> int:
    """Close a claimed run exactly like the crash path before bookkeeping."""
    running = conn.execute(
        "SELECT current_run_id, claim_lock, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert running is not None and running["current_run_id"] is not None
    run_id = int(running["current_run_id"])
    pid = int(running["worker_pid"])
    claimer = running["claim_lock"]

    with kb.write_txn(conn):
        released = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        assert released.rowcount == 1
        closed_run_id = kb._end_run(
            conn,
            task_id,
            outcome="crashed",
            status="crashed",
            error=error,
            metadata={"pid": pid, "claimer": claimer},
        )
        assert closed_run_id == run_id
        kb._append_event(
            conn,
            task_id,
            "crashed",
            {"pid": pid, "claimer": claimer},
            run_id=run_id,
        )

    blocked = kb._record_task_failure(
        conn,
        task_id,
        error=error,
        outcome="crashed",
        release_claim=False,
        end_run=False,
        event_payload_extra={"pid": pid, "claimer": claimer},
        closed_run_id=run_id,
    )
    assert blocked is False
    return run_id


def _prime_task_with_auth_crash(conn, task_id: str, clock: list[int]) -> int:
    """Use real dispatch calls to reach a below-limit auth crash artifact."""

    def transient_spawn_failure(task, workspace):
        raise RuntimeError("temporary launcher failure")

    for _ in range(kb.TRANSIENT_RETRY_LIMIT):
        result = kb.dispatch_once(conn, spawn_fn=transient_spawn_failure)
        assert result.auto_blocked == []
        clock[0] += kb.TRANSIENT_RETRY_BACKOFF_SECONDS + 1

    spawned = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: 987_654)
    assert [item[0] for item in spawned.spawned] == [task_id]
    _record_production_shaped_crash(conn, task_id, error=AUTH_ERROR)

    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.consecutive_failures == 1
    assert task.consecutive_failures < kb.DEFAULT_FAILURE_LIMIT
    assert task.last_failure_error == AUTH_ERROR
    return clock[0]


def test_blocker_auth_expires_and_reaches_the_circuit_breaker(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A real blocker stays quiet briefly, then stops wedging dispatch."""
    clock = [BASE_TIME]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="quota wall", assignee="coder")
        failure_at = _prime_task_with_auth_crash(conn, task_id, clock)

        # Regression contract: the guard still protects a real quota wall
        # throughout the cooldown instead of immediately burning another slot.
        clock[0] = failure_at + 299
        assert kb.check_respawn_guard(conn, task_id) == "blocker_auth"
        clock[0] = failure_at

        spawn_attempts: list[str] = []

        def persistent_auth_failure(task, workspace):
            spawn_attempts.append(task.id)
            raise RuntimeError(AUTH_ERROR)

        guarded_ticks = 0
        last_result = None
        for _ in range(MAX_DISPATCH_TICKS):
            last_result = kb.dispatch_once(conn, spawn_fn=persistent_auth_failure)
            task = kb.get_task(conn, task_id)
            if last_result.respawn_guarded:
                assert last_result.respawn_guarded == [(task_id, "blocker_auth")]
                assert task.status == "ready"
                assert task.consecutive_failures == 1
                guarded_ticks += 1
            if task.status != "ready":
                break
            clock[0] += DISPATCH_TICK_SECONDS

        task = kb.get_task(conn, task_id)
        event_kinds = [event.kind for event in kb.list_events(conn, task_id)]

    assert guarded_ticks > 0
    assert spawn_attempts == [task_id], (
        f"blocker_auth still wedged the ready task after {MAX_DISPATCH_TICKS} "
        f"advancing dispatcher ticks ({guarded_ticks} guarded)"
    )
    assert last_result is not None and task_id in last_result.auto_blocked
    assert task.status == "blocked"
    assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT
    assert "gave_up" in event_kinds
    assert kb.OPERATOR_ESCALATION_EVENT in event_kinds


def test_blocker_auth_ttl_preserves_other_guard_order(
    kanban_home, monkeypatch
):
    """The TTL does not reorder or weaken the four neighboring guards."""
    monkeypatch.setattr(kb.time, "time", lambda: BASE_TIME)

    with kb.connect_closing() as conn:
        rate_limited = kb.create_task(conn, title="rate limited", assignee="coder")
        transient = kb.create_task(conn, title="transient", assignee="coder")
        succeeded = kb.create_task(conn, title="succeeded", assignee="coder")
        active_pr = kb.create_task(conn, title="active pr", assignee="coder")
        blocker_before_success = kb.create_task(
            conn, title="blocked before success", assignee="coder"
        )

        with kb.write_txn(conn):
            conn.executemany(
                "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                [
                    (AUTH_ERROR, rate_limited),
                    (AUTH_ERROR, transient),
                    (AUTH_ERROR, blocker_before_success),
                ],
            )
            conn.executemany(
                "INSERT INTO task_runs "
                "(task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'coder', ?, ?, ?, ?)",
                [
                    (
                        rate_limited,
                        "rate_limited",
                        "rate_limited",
                        BASE_TIME - 10,
                        BASE_TIME,
                    ),
                    (
                        transient,
                        kb.TRANSIENT_RETRY_OUTCOME,
                        kb.TRANSIENT_RETRY_OUTCOME,
                        BASE_TIME - 10,
                        BASE_TIME,
                    ),
                    (
                        succeeded,
                        "done",
                        "completed",
                        BASE_TIME - 10,
                        BASE_TIME,
                    ),
                    (
                        blocker_before_success,
                        "done",
                        "completed",
                        BASE_TIME - 10,
                        BASE_TIME,
                    ),
                ],
            )
        kb.add_comment(
            conn,
            succeeded,
            "worker",
            "PR: https://github.com/example/project/pull/42",
        )
        kb.add_comment(
            conn,
            active_pr,
            "worker",
            "PR: https://github.com/example/project/pull/43",
        )

        assert kb.check_respawn_guard(conn, rate_limited) == "rate_limit_cooldown"
        assert kb.check_respawn_guard(conn, transient) == "transient_retry_backoff"
        assert kb.check_respawn_guard(conn, blocker_before_success) == "blocker_auth"
        assert kb.check_respawn_guard(conn, succeeded) == "recent_success"
        assert kb.check_respawn_guard(conn, active_pr) == "active_pr"

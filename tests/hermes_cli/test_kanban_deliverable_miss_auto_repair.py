"""auto_retry_blocked_tasks must repair protocol-miss blocks before re-dispatch.

Production path: detect_crashed_workers blocks with reason
"deliverable posted but worker exited cleanly (rc=0) without calling
kanban_complete — repair required" and a deliverable_posted_not_completed
event carrying evidence. auto_retry_blocked_tasks is the gateway/sweep
consumer (dispatch_once(..., auto_retry_blocked=True)).

Repair must run first when evidence is usable; only on repair failure may
the existing bounded re-dispatch fire. Every attempt (repair or re-dispatch)
consumes the auto_retry_count budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# Exact error_text from detect_crashed_workers (kanban_db.py recoverable path).
PROTOCOL_MISS_REASON = (
    "deliverable posted but worker exited cleanly (rc=0) "
    "without calling kanban_complete — repair required"
)

# Structure-preserving non-code deliverable (matches production comment shape
# used by existing repair_deliverable fidelity / spawn_workdir tests).
LIVE_DELIVERABLE = (
    "# Deliverable: render quarterly report\n\n"
    "The quarterly report is complete and mapped to the requested "
    "objective. Evidence includes the final section list, validation "
    "notes, and remaining risk. " + "x" * 120
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    return home


def _block_protocol_miss_with_deliverable(
    conn,
    *,
    pid: int,
    title: str = "render quarterly report",
    deliverable: str | None = LIVE_DELIVERABLE,
) -> str:
    """Fixture via the real detect_crashed_workers production path."""
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="default",
        kind="text",
    )
    assert kb.claim_task(conn, task_id) is not None
    if deliverable is not None:
        kb.add_comment(conn, task_id, "default", deliverable)
    kb._set_worker_pid(conn, task_id, pid)
    kb._record_worker_exit(pid, 0)
    assert task_id not in kb.detect_crashed_workers(conn)
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.last_failure_error == PROTOCOL_MISS_REASON
    return task_id


def _event_kinds(conn, task_id: str) -> list[str]:
    return [event.kind for event in kb.list_events(conn, task_id)]


def test_auto_retry_repairs_protocol_miss_before_redispatch(
    kanban_home: Path,
) -> None:
    """(1) Usable evidence → repair first; no fresh worker re-dispatch."""
    with kb.connect_closing() as conn:
        task_id = _block_protocol_miss_with_deliverable(conn, pid=710001)

        # Production call shape: dispatch_once → auto_retry_blocked_tasks with
        # configured backoff (0 here so the tick acts immediately).
        retried = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
        )
        task = kb.get_task(conn, task_id)
        kinds = _event_kinds(conn, task_id)
        repaired_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "deliverable_protocol_repaired"
        ]
        auto_retried_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "auto_retried"
        ]

    assert retried == [], "repair must not re-dispatch a fresh worker"
    assert auto_retried_events == [], "no auto_retried event on successful repair"
    assert repaired_events, "expected deliverable_protocol_repaired success signal"
    assert "deliverable_protocol_repaired" in kinds
    assert task is not None
    assert task.status == "done"
    # Budget consumed by the repair attempt (done_when (3) partial).
    assert task.auto_retry_count >= 1


def test_auto_retry_falls_back_to_bounded_redispatch_without_evidence(
    kanban_home: Path,
) -> None:
    """(2) Same block reason, no usable evidence → today's bounded retry."""
    with kb.connect_closing() as conn:
        # Protocol-miss block with the real reason, but strip the evidence
        # event so the repair primitive fails.
        task_id = _block_protocol_miss_with_deliverable(conn, pid=710002)
        # Invalidate evidence: delete the deliverable_posted_not_completed
        # event payload the repair primitive reads.
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, kb.DELIVERABLE_POSTED_NOT_COMPLETED),
        )
        # Keep the blocked run error as the production reason text.
        run = conn.execute(
            "SELECT id, error, outcome FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert run is not None
        assert run["error"] == PROTOCOL_MISS_REASON

        retried = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
        )
        task = kb.get_task(conn, task_id)
        kinds = _event_kinds(conn, task_id)

    assert retried == [(task_id, 1)]
    assert task is not None
    assert task.status == "ready"
    assert task.auto_retry_count == 1
    assert "deliverable_protocol_repaired" not in kinds
    assert "auto_retried" in kinds


def test_auto_retry_repair_consumes_budget_no_unbounded_re_repair(
    kanban_home: Path,
) -> None:
    """(3) Repair attempts count against auto_retry_count; no endless re-repair."""
    with kb.connect_closing() as conn:
        task_id = _block_protocol_miss_with_deliverable(conn, pid=710003)

        first = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=1,
        )
        after_first = kb.get_task(conn, task_id)
        assert first == []
        assert after_first is not None
        assert after_first.status == "done"
        assert after_first.auto_retry_count == 1
        repair_count_after_first = sum(
            1
            for event in kb.list_events(conn, task_id)
            if event.kind == "deliverable_protocol_repaired"
        )
        assert repair_count_after_first == 1

        # Second tick must not re-repair (task is already terminal).
        second = kb.auto_retry_blocked_tasks(
            conn,
            backoff_seconds=0,
            retry_limit=1,
        )
        repair_count_after_second = sum(
            1
            for event in kb.list_events(conn, task_id)
            if event.kind == "deliverable_protocol_repaired"
        )

    assert second == []
    assert repair_count_after_second == 1


def test_auto_retry_failed_repair_respects_retry_limit(
    kanban_home: Path,
) -> None:
    """Failed repair + re-dispatch still exhausts the bounded budget."""
    with kb.connect_closing() as conn:
        task_id = _block_protocol_miss_with_deliverable(conn, pid=710004)
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, kb.DELIVERABLE_POSTED_NOT_COMPLETED),
        )

        assert kb.auto_retry_blocked_tasks(
            conn, backoff_seconds=0, retry_limit=1,
        ) == [(task_id, 1)]
        # Re-block with the same protocol-miss reason (no evidence again).
        assert kb.claim_task(conn, task_id) is not None
        # Direct production-shaped block: status blocked + ended run with
        # protocol-miss error (detect path needs a live pid cycle; simulate
        # the post-detect DB shape the auto-retry lane reads).
        now = int(kb.time.time())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', "
                "last_failure_error = ?, auto_retry_count = 1, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "current_run_id = NULL WHERE id = ?",
                (PROTOCOL_MISS_REASON, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET ended_at = ?, outcome = ?, status = ?, "
                "error = ? WHERE task_id = ? AND ended_at IS NULL",
                (
                    now,
                    kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                    kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                    PROTOCOL_MISS_REASON,
                    task_id,
                ),
            )
            # Ensure there is an ended protocol-miss run for the lane.
            open_run = conn.execute(
                "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL",
                (task_id,),
            ).fetchone()
            if open_run is not None:
                conn.execute(
                    "UPDATE task_runs SET ended_at = ?, outcome = ?, status = ?, "
                    "error = ? WHERE id = ?",
                    (
                        now,
                        kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                        kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                        PROTOCOL_MISS_REASON,
                        open_run["id"],
                    ),
                )
            else:
                # claim_task opened a run that may already be ended via prior path
                latest = conn.execute(
                    "SELECT id, ended_at, outcome FROM task_runs "
                    "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if latest is not None and latest["ended_at"] is None:
                    conn.execute(
                        "UPDATE task_runs SET ended_at = ?, outcome = ?, "
                        "status = ?, error = ? WHERE id = ?",
                        (
                            now,
                            kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                            kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                            PROTOCOL_MISS_REASON,
                            latest["id"],
                        ),
                    )
                elif latest is None or latest["outcome"] != kb.DELIVERABLE_POSTED_NOT_COMPLETED:
                    conn.execute(
                        "INSERT INTO task_runs "
                        "(task_id, profile, status, outcome, error, started_at, ended_at) "
                        "VALUES (?, 'default', ?, ?, ?, ?, ?)",
                        (
                            task_id,
                            kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                            kb.DELIVERABLE_POSTED_NOT_COMPLETED,
                            PROTOCOL_MISS_REASON,
                            now - 10,
                            now,
                        ),
                    )

        retried = kb.auto_retry_blocked_tasks(
            conn, backoff_seconds=0, retry_limit=1,
        )
        task = kb.get_task(conn, task_id)
        exhausted = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "auto_retry_exhausted"
        ]

    assert retried == []
    assert task is not None
    assert task.status == "blocked"
    assert task.auto_retry_count == 1
    assert exhausted, "budget must exhaust instead of unbounded re-repair/retry"

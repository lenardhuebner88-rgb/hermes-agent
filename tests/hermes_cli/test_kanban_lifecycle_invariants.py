"""End-to-end invariants for the persisted Kanban worker lifecycle.

These tests intentionally drive production transition APIs.  They may replace
OS process probes with deterministic fakes, but never mock the transition under
test.  Each ``Ix`` label maps to the lifecycle report of 2026-07-18.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kw


def _absent_worker(*_args, **_kwargs) -> dict:
    return {
        "host_local": True,
        "termination_attempted": True,
        "terminated": True,
        "sigkill": False,
        "group_alive_after": False,
    }


def _claim_and_spawn(conn, task_id: str, pid: int):
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert claimed.current_run_id is not None
    kb._set_worker_pid(conn, task_id, pid)
    return claimed


def _assert_runtime_cleared(task) -> None:
    assert task.claim_lock is None
    assert task.claim_expires is None
    assert task.worker_pid is None
    assert task.current_run_id is None


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def test_i1_worker_and_born_blocked_tasks_are_typed(kanban_home):
    """I1: every persisted ``blocked`` state carries a machine-readable kind."""
    with kb.connect_closing() as conn:
        worker_task = kb.create_task(conn, title="worker block")
        claimed = kb.claim_task(conn, worker_task)
        assert claimed is not None
        assert kb.block_task(
            conn,
            worker_task,
            reason="cannot continue without operator input",
            expected_run_id=claimed.current_run_id,
        )

        born_blocked = kb.create_task(
            conn,
            title="operator decision required",
            initial_status="blocked",
        )

        assert kb.get_task(conn, worker_task).block_kind == "needs_input"
        assert kb.get_task(conn, born_blocked).block_kind == "needs_input"


@pytest.mark.parametrize(
    ("park", "expected_kind"),
    [
        (
            lambda conn, task_id: kw._block_decompose_root_no_real_completion(
                conn, root_id=task_id
            ),
            "needs_input",
        ),
        (
            lambda conn, task_id: kw._block_decompose_root(
                conn,
                root_id=task_id,
                reason="integration conflict",
                outcome={"action": "rebase_conflict"},
            ),
            "integration",
        ),
    ],
)
def test_i1_decompose_system_parks_are_typed(kanban_home, park, expected_kind):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="decompose root")
        park(conn, task_id)
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == expected_kind
        _assert_runtime_cleared(task)


def test_i2_spawned_running_task_has_complete_claim_identity(
    kanban_home, all_assignees_spawnable
):
    """I2: after spawn, running means lock + expiry + pid + active run."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="spawn identity", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: 424242,
            serialize_by_repo=False,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, task.current_run_id)

        assert [entry[0] for entry in result.spawned] == [task_id]
        assert task.status == "running"
        assert task.claim_lock
        assert task.claim_expires
        assert task.worker_pid == 424242
        assert run is not None and run.status == "running" and run.ended_at is None
        assert run.claim_lock == task.claim_lock
        assert run.worker_pid == task.worker_pid


def test_i3_clean_exit_protocol_miss_is_bounded_and_terminal(
    kanban_home, monkeypatch
):
    """I3: repeated rc=0 protocol misses cannot leave a silent ready/running loop."""
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _absent_worker)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="clean worker exit",
            assignee="worker",
            max_retries=3,
        )
        for attempt in range(1, 4):
            pid = 700000 + attempt
            _claim_and_spawn(conn, task_id, pid)
            kb._record_worker_exit(pid, 0)
            assert kb.detect_crashed_workers(conn) == [task_id]
            task = kb.get_task(conn, task_id)
            if attempt < 3:
                assert task.status == "ready"
                _assert_runtime_cleared(task)

        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind is not None
        _assert_runtime_cleared(task)
        assert len(_event_payloads(conn, task_id, "protocol_violation")) == 3
        assert len(_event_payloads(conn, task_id, "gave_up")) == 1


def test_i4_transient_requeue_bumps_counter_and_event(
    kanban_home, all_assignees_spawnable
):
    """I4: a requeue's counter and event are committed together."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="transient spawn", assignee="worker")

        def fail_spawn(_task, _workspace):
            raise RuntimeError("Resource temporarily unavailable")

        kb.dispatch_once(conn, spawn_fn=fail_spawn, serialize_by_repo=False)
        task = kb.get_task(conn, task_id)
        payloads = _event_payloads(conn, task_id, kb.TRANSIENT_RETRY_EVENT)

        assert task.status == "ready"
        assert task.transient_retry_count == 1
        assert payloads[-1]["attempt"] == task.transient_retry_count
        assert payloads[-1]["trigger_outcome"] == "spawn_failed"
        _assert_runtime_cleared(task)


_GIT = shutil.which("git")


@pytest.mark.skipif(_GIT is None, reason="git not installed")
def test_i5_review_diff_survives_block_promote_resubmit(kanban_home, tmp_path):
    """I5: block -> promote -> reclaim cannot erase the original review diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([_GIT, "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        [_GIT, "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        [_GIT, "-C", str(repo), "config", "user.name", "Lifecycle Test"],
        check=True,
    )
    target = repo / "tracked.py"
    target.write_text("value = 1\n", encoding="utf-8")
    subprocess.run([_GIT, "-C", str(repo), "add", "tracked.py"], check=True)
    subprocess.run([_GIT, "-C", str(repo), "commit", "-m", "base"], check=True)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review diff carry",
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        run1 = kb.claim_task(conn, task_id).current_run_id
        target.write_text("value = 2\n", encoding="utf-8")
        subprocess.run([_GIT, "-C", str(repo), "add", "tracked.py"], check=True)
        subprocess.run([_GIT, "-C", str(repo), "commit", "-m", "change"], check=True)
        assert kb._submit_for_review(
            conn,
            task_id,
            result="done",
            summary="first submit",
            metadata=None,
            verified_cards=[],
            expected_run_id=run1,
        )
        first = _event_payloads(conn, task_id, "submitted_for_review")[-1]
        assert first.get("diff_text")

        review_run = kb.claim_review_task(conn, task_id).current_run_id
        assert kb.block_task(
            conn,
            task_id,
            reason="request changes",
            expected_run_id=review_run,
        )
        promoted, reason = kb.promote_task(conn, task_id, actor="test")
        assert promoted, reason
        run2 = kb.claim_task(conn, task_id).current_run_id
        assert not kb._capture_review_diff_snapshot(
            conn, task_id, expected_run_id=run2
        ).get("diff_text")
        assert kb._submit_for_review(
            conn,
            task_id,
            result="done",
            summary="resubmit",
            metadata=None,
            verified_cards=[],
            expected_run_id=run2,
        )
        second = _event_payloads(conn, task_id, "submitted_for_review")[-1]
        assert second.get("diff_text") == first["diff_text"]
        assert second.get("changed_files") == first.get("changed_files")
        assert second.get("diff_stat") == first.get("diff_stat")


def test_i6_done_and_archived_tasks_release_runtime_identity(kanban_home):
    """I6: terminal tasks own no live claim, pid, or run pointer."""
    with kb.connect_closing() as conn:
        done_id = kb.create_task(conn, title="complete cleanup")
        done_claim = _claim_and_spawn(conn, done_id, 810001)
        assert kb.complete_task(
            conn,
            done_id,
            result="complete",
            expected_run_id=done_claim.current_run_id,
        )
        done = kb.get_task(conn, done_id)
        assert done.status == "done"
        _assert_runtime_cleared(done)

        archived_id = kb.create_task(conn, title="archive cleanup")
        _claim_and_spawn(conn, archived_id, 810002)
        assert kb.archive_task(conn, archived_id)
        archived = kb.get_task(conn, archived_id)
        assert archived.status == "archived"
        _assert_runtime_cleared(archived)


def test_i7_timeout_breaker_block_is_typed(kanban_home, monkeypatch):
    """I7: the separate timeout/crash breaker branch must persist block_kind."""
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _absent_worker)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="timeout breaker",
            max_runtime_seconds=1,
            max_retries=2,
        )
        for attempt in range(2):
            _claim_and_spawn(conn, task_id, 820000 + attempt)
            clock[0] += 2
            assert kb.enforce_max_runtime(conn) == [task_id]

        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == "capacity"
        _assert_runtime_cleared(task)


def test_i8_running_task_and_current_run_are_bidirectionally_coherent(kanban_home):
    """I8: current_run_id identifies the one open run for the running task."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="run pointer")
        claimed = _claim_and_spawn(conn, task_id, 830001)
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        open_runs = [run for run in kb.list_runs(conn, task_id) if run.ended_at is None]

        assert task.current_run_id == run.id
        assert run.task_id == task_id
        assert run.status == "running"
        assert len(open_runs) == 1


def test_i9_block_closes_the_only_open_run(kanban_home):
    """I9: no open run may remain attached to a non-running task."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="close blocked run")
        claimed = _claim_and_spawn(conn, task_id, 840001)
        assert kb.block_task(
            conn,
            task_id,
            reason="needs input",
            kind="needs_input",
            expected_run_id=claimed.current_run_id,
        )
        run = kb.get_run(conn, claimed.current_run_id)

        assert run.ended_at is not None
        assert run.outcome == "blocked"
        assert kb.get_task(conn, task_id).current_run_id is None


def test_i10_blocked_task_has_no_runtime_identity(kanban_home):
    """I10: a blocked card can never retain a runnable claim."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="blocked cleanup")
        claimed = _claim_and_spawn(conn, task_id, 850001)
        assert kb.block_task(
            conn,
            task_id,
            kind="needs_input",
            expected_run_id=claimed.current_run_id,
        )
        _assert_runtime_cleared(kb.get_task(conn, task_id))


def test_i11_ready_requeue_has_no_runtime_identity(
    kanban_home, all_assignees_spawnable
):
    """I11: a requeued ready task is immediately claimable, not half-owned."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ready cleanup", assignee="worker")

        def fail_spawn(_task, _workspace):
            raise RuntimeError("Cannot allocate memory")

        kb.dispatch_once(conn, spawn_fn=fail_spawn, serialize_by_repo=False)
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        _assert_runtime_cleared(task)


def test_i12_release_gate_done_fallback_sets_terminal_evidence(kanban_home):
    """I12: even the fallback done path sets completed_at and terminal hygiene."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="unfinished parent")
        child = kb.create_task(conn, title="release gate", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"

        kw._finish_release_gate_green(conn, child, parent, fixer_attempts=1)
        task = kb.get_task(conn, child)

        assert task.status == "done"
        assert task.completed_at is not None
        _assert_runtime_cleared(task)
        assert _event_payloads(conn, child, "release_gate_green")


def test_i13_completed_run_has_terminal_timestamp_and_outcome(kanban_home):
    """I13: successful completion closes the run with an auditable outcome."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="run terminal outcome")
        claimed = _claim_and_spawn(conn, task_id, 860001)
        assert kb.complete_task(
            conn,
            task_id,
            result="done",
            expected_run_id=claimed.current_run_id,
        )
        run = kb.get_run(conn, claimed.current_run_id)

        assert run.ended_at is not None
        assert run.outcome == "completed"
        assert run.status == "done"


def test_i14_expired_claim_is_reclaimed_without_sleep(kanban_home, monkeypatch):
    """I14: the TTL reaper leaves no expired running claim behind."""
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _absent_worker)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="expired claim")
        claimed = kb.claim_task(conn, task_id, ttl_seconds=1)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 870001)
        clock[0] += 2

        assert kb.release_stale_claims(conn) == 1
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)

        assert task.status == "ready"
        _assert_runtime_cleared(task)
        assert run.outcome == "reclaimed" and run.ended_at is not None


def test_i15_claim_gate_demotes_child_with_undone_parent(kanban_home):
    """I15: even a forced ready row cannot run before every parent is done."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        promoted, reason = kb.promote_task(conn, child, actor="test", force=True)
        assert promoted, reason
        assert kb.get_task(conn, child).status == "ready"

        assert kb.claim_task(conn, child) is None
        assert kb.get_task(conn, child).status == "todo"
        payload = _event_payloads(conn, child, "claim_rejected")[-1]
        assert payload["reason"] == "parents_not_done"

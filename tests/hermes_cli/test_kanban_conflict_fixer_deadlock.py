"""Regression coverage for conflict-fixer cards that give up while blocked."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_chain_status as chain_status
from hermes_cli import kanban_lane_fixer as lane_fixer
from hermes_cli import kanban_worktrees as kwt
from hermes_cli.kanban_chain_status import (
    CONFLICT_FIXER_FAILED_EVENT,
    is_settled_fixer_card,
)


_PARK_REASON = "integration parked: merge conflict/failure (aborted): foo.py"
FIXER_FAILURE_PUBLISH_FAILED_EVENT = "fixer_failure_publish_failed"


def _parked_parent(conn, tmp_path: Path) -> tuple[str, str, Path]:
    parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
    assert kb.claim_task(conn, parent_id) is not None
    assert kb.block_task(
        conn,
        parent_id,
        reason=_PARK_REASON,
        kind="integration",
    )
    worktree = tmp_path / "repo" / ".worktrees" / "kanban" / parent_id
    worktree.mkdir(parents=True)
    kb.set_workspace_path(conn, parent_id, str(worktree))
    return parent_id, parent_id, worktree


def _create_fixer(
    conn,
    *,
    parent_id: str,
    root_id: str,
    worktree: Path,
    attempt: int,
) -> str:
    parent = conn.execute(
        "SELECT * FROM tasks WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert parent is not None
    child_id = kb._create_conflict_park_fixer_subtask(
        conn,
        parent,
        reason=_PARK_REASON,
        root_id=root_id,
        wt=worktree,
        attempt=attempt,
    )
    assert child_id is not None
    return child_id


def _fail_after_timeout(conn, task_id: str) -> None:
    assert kb.claim_task(conn, task_id) is not None
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            "timed_out",
            {"elapsed_seconds": 1827, "limit_seconds": 1800},
        )
    assert kb._record_task_failure(
        conn,
        task_id,
        "worker timed out after 1827s (limit 1800s)",
        outcome="timed_out",
        failure_limit=1,
        release_claim=True,
        end_run=True,
    )
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "blocked"
    kinds = [event.kind for event in kb.list_events(conn, task_id)]
    assert kinds.index("claimed") < kinds.index("timed_out") < kinds.index("gave_up")


def _after_fixer_backoff(conn, parent_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(created_at) AS created_at FROM task_events "
        "WHERE task_id = ? AND kind = ?",
        (parent_id, kb.CONFLICT_FIXER_DISPATCHED_EVENT),
    ).fetchone()
    assert row is not None and row["created_at"] is not None
    return int(row["created_at"]) + kb.CONFLICT_FIXER_BACKOFF_SECONDS + 1


def _init_fixer_worktree(worktree: Path) -> None:
    """Create a small real git worktree so claim-time commit baselines exist."""
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(worktree), *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init")
    git("config", "user.email", "fixer@test.invalid")
    git("config", "user.name", "fixer test")
    (worktree / "fix.py").write_text("base = 1\n", encoding="utf-8")
    git("add", "fix.py")
    git("commit", "-m", "baseline")


def _commit_fixer_progress(worktree: Path) -> None:
    (worktree / "fix.py").write_text("base = 1\nresolved = True\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "fix.py"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "resolve conflict"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _claim_at_runtime_limit(conn, task_id: str, *, now: int) -> None:
    assert kb.claim_task(conn, task_id) is not None
    kb._set_worker_pid(conn, task_id, 987654)
    run_id = kb.get_task(conn, task_id).current_run_id
    assert run_id is not None
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (now - kb.CONFLICT_FIXER_MAX_RUNTIME_SECONDS, run_id),
    )
    conn.execute(
        "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
        (now - 1, task_id),
    )
    conn.commit()


def test_gave_up_conflict_fixer_releases_in_flight_guard_for_second_attempt(
    kanban_home,
    tmp_path,
):
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        first_child = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        _fail_after_timeout(conn, first_child)
        assert is_settled_fixer_card(conn, first_child)

        summary = {"parked": [], "conflict_fixer_dispatched": []}
        parent = conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        kb._maybe_route_conflict_park_fixer(
            conn,
            parent,
            reason=_PARK_REASON,
            retry_count=0,
            now=_after_fixer_backoff(conn, parent_id),
            summary=summary,
        )

        dispatched = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]

    assert len(dispatched) == 2
    assert summary["conflict_fixer_dispatched"][0]["attempt"] == 2
    assert dispatched[-1].payload["child_id"] != first_child


def test_gave_up_regular_blocked_task_still_holds_retry_and_merge_gates(
    kanban_home,
    tmp_path,
):
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        normal_id = kb.create_task(
            conn,
            title="ordinary blocked work",
            assignee="coder",
            max_retries=1,
        )
        fingerprint = kb._conflict_fingerprint(_PARK_REASON)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                kb.CONFLICT_FIXER_DISPATCHED_EVENT,
                {
                    "child_id": normal_id,
                    "root_id": root_id,
                    "attempt": 1,
                    "reason": _PARK_REASON,
                    "conflict_fingerprint": fingerprint,
                },
            )
        _fail_after_timeout(conn, normal_id)
        assert not is_settled_fixer_card(conn, normal_id)

        summary = {"parked": [], "conflict_fixer_dispatched": []}
        parent = conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        kb._maybe_route_conflict_park_fixer(
            conn,
            parent,
            reason=_PARK_REASON,
            retry_count=0,
            now=_after_fixer_backoff(conn, parent_id),
            summary=summary,
        )
        open_sibling = kwt._find_open_chain_sibling(
            conn,
            parent_id,
            {normal_id},
            worktree,
        )

        dispatched = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]

    assert len(dispatched) == 1
    assert summary["conflict_fixer_dispatched"] == []
    assert open_sibling is not None


def test_blocked_fixer_without_gave_up_releases_in_flight_guard_without_unblocking_hold(
    kanban_home,
    tmp_path,
):
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        conflict_fixer = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        assert kb.claim_task(conn, conflict_fixer) is not None
        assert kb.block_task(
            conn,
            conflict_fixer,
            reason="operator hold: await approval",
            kind="needs_input",
        )

        lane_fixer_id = kb.create_task(
            conn,
            title="held lane-scope fixer",
            assignee="coder",
            idempotency_key="lane-scope-fixer:held:abc:1",
        )
        assert kb.claim_task(conn, lane_fixer_id) is not None
        assert kb.block_task(
            conn,
            lane_fixer_id,
            reason="manual hold: do not dispatch",
            kind="capability",
        )

        conflict_after = kb.get_task(conn, conflict_fixer)
        lane_after = kb.get_task(conn, lane_fixer_id)
        conflict_block = next(
            event for event in kb.list_events(conn, conflict_fixer) if event.kind == "blocked"
        )
        lane_block = next(
            event for event in kb.list_events(conn, lane_fixer_id) if event.kind == "blocked"
        )

        assert is_settled_fixer_card(conn, conflict_fixer)
        assert is_settled_fixer_card(conn, lane_fixer_id)

    assert conflict_after is not None and conflict_after.status == "blocked"
    assert conflict_block.payload["reason"] == "operator hold: await approval"
    assert lane_after is not None and lane_after.status == "blocked"
    assert lane_block.payload["reason"] == "manual hold: do not dispatch"


def test_failed_lane_scope_fixer_wakes_a_bounded_replacement_attempt(
    kanban_home,
    tmp_path,
):
    with kb.connect_closing() as conn:
        parent_id, _root_id, worktree = _parked_parent(conn, tmp_path)
        parent = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent_id,)
        ).fetchone()
        first_child = lane_fixer.maybe_route_lane_scope_fixer(
            conn,
            parent,
            violating_paths=["web/src/control/Panel.tsx"],
            expected_lane="coder-frontend",
        )
        assert first_child is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = ?",
                (
                    int(time.time()) - kb.CONFLICT_FIXER_BACKOFF_SECONDS - 1,
                    parent_id,
                    lane_fixer.LANE_FIXER_DISPATCHED_EVENT,
                ),
            )

        _fail_after_timeout(conn, first_child)
        dispatched = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == lane_fixer.LANE_FIXER_DISPATCHED_EVENT
        ]

    assert len(dispatched) == 2
    assert dispatched[-1].payload["child_id"] != first_child


def test_fixer_failure_publish_error_is_recorded_as_an_event(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        fixer_id = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )

        def raise_during_wake(*args, **kwargs):
            raise RuntimeError("wake publication failed")

        monkeypatch.setattr(
            chain_status,
            "_matching_conflict_fixer_attempts",
            raise_during_wake,
        )
        _fail_after_timeout(conn, fixer_id)
        failures = [
            event
            for event in kb.list_events(conn, fixer_id)
            if event.kind == FIXER_FAILURE_PUBLISH_FAILED_EVENT
        ]

    assert len(failures) == 1
    assert failures[0].payload["source"] == "on_fixer_card_failed"
    assert failures[0].payload["error_type"] == "RuntimeError"


def test_fixer_failure_signals_child_and_parent_and_escalates_once(
    kanban_home,
    tmp_path,
):
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        first_child = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        _fail_after_timeout(conn, first_child)
        second_child = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=2,
        )

        _fail_after_timeout(conn, second_child)
        assert kb._record_task_failure(
            conn,
            second_child,
            "worker timed out after 1827s (limit 1800s)",
            outcome="timed_out",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )

        child_failures = [
            event
            for event in kb.list_events(conn, second_child)
            if event.kind == CONFLICT_FIXER_FAILED_EVENT
        ]
        parent_failures = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == CONFLICT_FIXER_FAILED_EVENT
            and event.payload["attempt"] == 2
        ]
        parent_escalations = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert len(child_failures) == 1
    assert len(parent_failures) == 1
    assert child_failures[0].payload == parent_failures[0].payload
    assert child_failures[0].payload["outcome"] == "timed_out"
    assert len(parent_escalations) == 1
    assert parent_escalations[0].payload["evidence"]["attempts"] == 2
    assert parent_escalations[0].payload["evidence"]["fixer_exhausted"] is True
    assert parent_escalations[0].payload["evidence"]["via"] == "fixer_failure"


def test_structured_integration_root_park_reaches_sweep_and_decision_queue(
    kanban_home,
):
    reason = "decompose-root finalize: chain has open siblings"
    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="stranded root", assignee="coder")
        assert kb.claim_task(conn, root_id) is not None
        assert kb.block_task(
            conn,
            root_id,
            reason=reason,
            kind="integration",
        )

        before = kb.decision_queue(conn)
        sweep = kb.no_silent_stall_sweep(conn, now=1_900_000_000)
        after = kb.decision_queue(conn)

    assert any(
        item["task_id"] == root_id and item["kind"] == "integration_parked"
        for item in before["decisions"]
    )
    assert {"task_id": root_id, "class": kb.INTEGRATION_PARKED_STALL_CLASS} in sweep[
        "parked"
    ]
    assert any(
        item["task_id"] == root_id and item["kind"] == "integration_parked"
        for item in after["decisions"]
    )


def test_pending_root_finalizer_ignores_settled_fixer_card(
    kanban_home,
    tmp_path,
):
    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root finalizer", assignee="coder")
        worktree = tmp_path / "repo" / ".worktrees" / "kanban" / root_id
        worktree.mkdir(parents=True)
        kb.set_workspace_path(conn, root_id, str(worktree))

        completed_id = kb.create_task(conn, title="completed child", assignee="coder")
        kb.set_workspace_path(conn, completed_id, str(worktree))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (completed_id,),
            )

        fixer_id = _create_fixer(
            conn,
            parent_id=root_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        _fail_after_timeout(conn, fixer_id)
        pending_id = kwt._pending_root_finalizer_id(
            conn,
            task_id=completed_id,
            root_id=root_id,
            wt=worktree,
            members={root_id, completed_id, fixer_id},
        )

    assert pending_id == root_id


def test_conflict_fixer_commit_near_runtime_limit_gets_bounded_extension(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        _init_fixer_worktree(worktree)
        fixer_id = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        _claim_at_runtime_limit(conn, fixer_id, now=now)
        _commit_fixer_progress(worktree)

        monkeypatch.setattr(kb.time, "time", lambda: now)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("a progressing fixer must not be terminated")
            ),
        )

        assert kb.enforce_max_runtime(conn) == []
        task = kb.get_task(conn, fixer_id)
        extension = next(
            event
            for event in kb.list_events(conn, fixer_id)
            if event.kind == kb.FIXER_RUNTIME_EXTENSION_GRANTED_EVENT
        )

    assert task is not None and task.status == "running"
    assert extension.payload["reason"] == "fresh_heartbeat_and_new_commit"
    assert extension.payload["prior_limit_seconds"] == kb.CONFLICT_FIXER_MAX_RUNTIME_SECONDS
    assert extension.payload["new_limit_seconds"] > extension.payload["prior_limit_seconds"]
    assert extension.payload["new_limit_seconds"] <= kb.CONFLICT_FIXER_MAX_RUNTIME_SECONDS_CAP
    assert extension.payload["commit_sha"]


def test_conflict_fixer_heartbeat_without_new_commit_times_out_at_base_limit(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        _init_fixer_worktree(worktree)
        fixer_id = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        _claim_at_runtime_limit(conn, fixer_id, now=now)

        monkeypatch.setattr(kb.time, "time", lambda: now)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
            },
        )

        assert kb.enforce_max_runtime(conn) == [fixer_id]
        events = kb.list_events(conn, fixer_id)

    assert "timed_out" in [event.kind for event in events]
    assert kb.FIXER_RUNTIME_EXTENSION_GRANTED_EVENT not in [event.kind for event in events]


def test_conflict_fixer_progress_cannot_extend_past_hard_runtime_cap(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        parent_id, root_id, worktree = _parked_parent(conn, tmp_path)
        _init_fixer_worktree(worktree)
        fixer_id = _create_fixer(
            conn,
            parent_id=parent_id,
            root_id=root_id,
            worktree=worktree,
            attempt=1,
        )
        _claim_at_runtime_limit(conn, fixer_id, now=now)
        task = kb.get_task(conn, fixer_id)
        assert task is not None and task.current_run_id is not None
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (now - kb.CONFLICT_FIXER_MAX_RUNTIME_SECONDS_CAP, task.current_run_id),
        )
        conn.commit()
        _commit_fixer_progress(worktree)

        monkeypatch.setattr(kb.time, "time", lambda: now)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
            },
        )

        assert kb.enforce_max_runtime(conn) == [fixer_id]
        events = kb.list_events(conn, fixer_id)

    assert "timed_out" in [event.kind for event in events]
    assert kb.FIXER_RUNTIME_EXTENSION_GRANTED_EVENT not in [event.kind for event in events]

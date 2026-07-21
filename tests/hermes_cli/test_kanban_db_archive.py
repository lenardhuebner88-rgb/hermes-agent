"""Regression coverage for archive dependency repair and legacy sweeps."""

from __future__ import annotations

import json
import signal

from hermes_cli import kanban_db as kb


def _surviving_worker(*_args, **_kwargs) -> dict:
    """Process-probe fake: the worker group outlives SIGTERM/SIGKILL."""
    return {
        "prev_pid": 4242,
        "host_local": True,
        "termination_attempted": True,
        "terminated": False,
        "sigkill": True,
    }


def _events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def test_archive_task_releases_outgoing_dependencies_and_promotes_remaining_chain(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        upstream = kb.create_task(conn, title="upstream", assignee="alice")
        assert kb.complete_task(conn, upstream)
        archived_parent = kb.create_task(
            conn, title="archive me", assignee="alice", parents=[upstream]
        )
        dependent = kb.create_task(
            conn, title="dependent", assignee="alice", parents=[archived_parent]
        )
        root = kb.create_task(conn, title="root", assignee="alice", parents=[dependent])

        assert kb.archive_task(conn, archived_parent) is True

        links = {
            (row["parent_id"], row["child_id"])
            for row in conn.execute("SELECT parent_id, child_id FROM task_links")
        }
        assert (archived_parent, dependent) not in links
        assert (upstream, archived_parent) in links
        assert (dependent, root) in links
        released = [
            event
            for event in kb.list_events(conn, dependent)
            if event.kind == "dependency_released_by_archive"
        ]
        assert len(released) == 1
        assert released[0].payload is not None
        assert released[0].payload == {
            "archived_parent_id": archived_parent,
            "removed_link": {"parent_id": archived_parent, "child_id": dependent},
        }
        comments = kb.list_comments(conn, dependent)
        assert any(
            archived_parent in comment.body and "dependency" in comment.body.lower()
            for comment in comments
        )
        dependent_task = kb.get_task(conn, dependent)
        assert dependent_task is not None
        assert dependent_task.status == "ready"

        assert kb.complete_task(conn, dependent)
        root_task = kb.get_task(conn, root)
        assert root_task is not None
        assert root_task.status == "ready"


def test_silent_block_sweep_escalates_legacy_archived_dependency_waits(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        archived_parent = kb.create_task(conn, title="legacy parent", assignee="alice")
        other_archived_parent = kb.create_task(
            conn, title="other legacy parent", assignee="alice"
        )
        waiting = kb.create_task(
            conn,
            title="legacy wait",
            assignee="alice",
            parents=[archived_parent, other_archived_parent],
        )
        held_parent = kb.create_task(conn, title="held parent", assignee="alice")
        held_waiting = kb.create_task(
            conn,
            title="held legacy wait",
            assignee="alice",
            parents=[held_parent],
            freigabe="operator",
        )
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id IN (?, ?, ?)",
            (archived_parent, other_archived_parent, held_parent),
        )
        conn.execute(
            "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
            (held_waiting,),
        )

        kb.escalate_silent_blocks_sweep(conn)

        escalations = [
            event
            for event in kb.list_events(conn, waiting)
            if event.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        assert len(escalations) == 1
        assert escalations[0].payload is not None
        assert escalations[0].payload["why_now"] == "waiting_on_archived_parent"
        assert escalations[0].payload["archived_parent_ids"] == sorted(
            [archived_parent, other_archived_parent]
        )
        assert not [
            event
            for event in kb.list_events(conn, held_waiting)
            if event.kind == kb.OPERATOR_ESCALATION_EVENT
        ]


# ---------------------------------------------------------------------------
# Archive/reclaim process fence (RCA jarvis-b3-orchestration-2026-07-21, RC1):
# `archive_task` used to clear claim/pid/worktree ownership while the worker
# process group was still alive, so an archived premium worker kept writing
# into the shared chain worktree and fail-closed every following review start.
# ---------------------------------------------------------------------------


def test_archive_keeps_board_ownership_while_worker_group_is_still_alive(
    kanban_home, monkeypatch
):
    """AC-1 repro: archiving must NOT release ownership of a live writer."""
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _surviving_worker)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="live writer", assignee="premium")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 4242)

        assert kb.archive_task(conn, task_id) is False

        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.claim_lock is not None
        assert task.worker_pid == 4242
        assert task.current_run_id == claimed.current_run_id
        deferred = _events(conn, task_id, "reclaim_deferred")
        assert deferred and deferred[-1]["reason"] == "archive_worker_alive"
        assert deferred[-1]["terminated"] is False
        assert not _events(conn, task_id, "archived")


def test_archive_deferred_by_live_worker_dispatches_no_second_writer(
    kanban_home, monkeypatch, all_assignees_spawnable
):
    """AC-3: a fail-closed archive may never free the card for another writer."""
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _surviving_worker)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="live writer", assignee="premium")
        assert kb.claim_task(conn, task_id) is not None
        kb._set_worker_pid(conn, task_id, 4242)

        assert kb.archive_task(conn, task_id) is False

        spawned: list[str] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 99,
            serialize_by_repo=False,
        )
        assert spawned == []
        assert [entry[0] for entry in result.spawned] == []
        assert kb.get_task(conn, task_id).status == "running"


def test_archive_terminates_worker_group_before_releasing_ownership(kanban_home):
    """AC-2: the existing process-group terminator runs and is confirmed first."""
    alive = {"value": True}
    signalled: list[int] = []
    ownership_at_signal: list[tuple] = []

    def fake_signal(pid: int, sig: int) -> None:
        signalled.append(sig)
        row = conn.execute(
            "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        ownership_at_signal.append(
            (row["status"], row["claim_lock"], row["worker_pid"])
        )
        alive["value"] = False

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="reapable writer", assignee="premium")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 43210)

        assert (
            kb.archive_task(
                conn,
                task_id,
                signal_fn=fake_signal,
                probe_fn=lambda _pid: alive["value"],
            )
            is True
        )

        # The signal was sent while the card still owned claim + pid: the
        # release happens strictly after confirmed process absence.
        assert signalled == [signal.SIGTERM]
        assert ownership_at_signal == [("running", claimed.claim_lock, 43210)]

        task = kb.get_task(conn, task_id)
        assert task.status == "archived"
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.current_run_id is None

        reaped = _events(conn, task_id, "worker_reaped")
        assert reaped and reaped[-1]["pid"] == 43210
        assert reaped[-1]["terminated"] is True
        run = kb.get_run(conn, claimed.current_run_id)
        assert run.outcome == "reclaimed"


def test_archive_of_already_dead_worker_sends_no_signal(kanban_home):
    """AC-4: an already-exited pid stays a no-op success (no signal at all)."""
    signalled: list[int] = []

    def fake_signal(_pid: int, sig: int) -> None:  # pragma: no cover - guard
        signalled.append(sig)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="dead writer", assignee="premium")
        assert kb.claim_task(conn, task_id) is not None
        kb._set_worker_pid(conn, task_id, 43211)

        assert (
            kb.archive_task(
                conn,
                task_id,
                signal_fn=fake_signal,
                probe_fn=lambda _pid: False,
            )
            is True
        )

        assert signalled == []
        assert kb.get_task(conn, task_id).status == "archived"
        reaped = _events(conn, task_id, "worker_reaped")
        assert reaped and reaped[-1]["terminated"] is True


def test_archive_without_worker_never_probes_and_stays_idempotent(
    kanban_home, monkeypatch
):
    """AC-4: pid-less and repeated archives keep their historical behaviour."""

    def _boom(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("no worker pid → no termination probe")

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _boom)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="never spawned", assignee="premium")

        assert kb.archive_task(conn, task_id) is True
        assert kb.get_task(conn, task_id).status == "archived"
        # Second call is a no-op False (already archived) and must not probe.
        assert kb.archive_task(conn, task_id) is False

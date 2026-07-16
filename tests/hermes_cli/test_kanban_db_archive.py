"""Regression coverage for archive dependency repair and legacy sweeps."""

from __future__ import annotations

from hermes_cli import kanban_db as kb


def test_archive_task_releases_outgoing_dependencies_with_audit_trail(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        upstream = kb.create_task(conn, title="upstream", assignee="alice")
        archived_parent = kb.create_task(
            conn, title="archive me", assignee="alice", parents=[upstream]
        )
        dependent = kb.create_task(
            conn, title="dependent", assignee="alice", parents=[archived_parent]
        )

        assert kb.archive_task(conn, archived_parent) is True

        links = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
        assert [(row["parent_id"], row["child_id"]) for row in links] == [
            (upstream, archived_parent)
        ]
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
        conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (dependent,))
        heal_summary = kb.no_silent_stall_sweep(conn, min_age_seconds=0)
        healed_task = kb.get_task(conn, dependent)
        assert healed_task is not None
        assert healed_task.status == "scheduled"
        assert dependent in heal_summary["skipped_archived_chain"]


def test_silent_block_sweep_escalates_legacy_archived_dependency_waits(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        archived_parent = kb.create_task(conn, title="legacy parent", assignee="alice")
        waiting = kb.create_task(
            conn, title="legacy wait", assignee="alice", parents=[archived_parent]
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
            "UPDATE tasks SET status = 'archived' WHERE id IN (?, ?)",
            (archived_parent, held_parent),
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
        assert not [
            event
            for event in kb.list_events(conn, held_waiting)
            if event.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

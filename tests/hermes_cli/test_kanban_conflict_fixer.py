"""Regression coverage for bounded conflict-park fixer worker instructions."""

from __future__ import annotations

from hermes_cli import kanban_db as kb


def test_conflict_fixer_brief_instructs_terminal_kanban_actions_and_keeps_cage():
    body = kb._conflict_fixer_body(
        parent_id="t_parent",
        parent_title="parked finalizer",
        root_id="t_root",
        branch="kanban/t_root",
        reason="integration parked: merge conflict",
        attempt=1,
    )

    assert "call kanban_complete" in body
    assert "call kanban_block" in body
    assert "NEVER push, merge, switch, or reset another branch" in body
    assert "operator" in body


def test_completed_conflict_fixer_resumes_its_parent_without_retry_sweep(kanban_home):
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason)
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": parent_id,
                    "conflict_fingerprint": kb._conflict_fingerprint(reason),
                },
            )

        assert kb.complete_task(conn, child_id, summary="conflict fixed")
        parent = kb.get_task(conn, parent_id)
        resume_events = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert parent is not None
    assert parent.status == "ready"
    assert [event.payload for event in resume_events] == [
        {
            "child_id": child_id,
            "conflict_fingerprint": kb._conflict_fingerprint(reason),
            "status": "ready",
        }
    ]

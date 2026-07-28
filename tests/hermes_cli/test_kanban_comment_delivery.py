"""Comment-write delivery metadata keeps operator feedback truthful."""

from __future__ import annotations

import time
from argparse import Namespace

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import _cmd_comment


def _claimed_task(conn) -> str:
    task_id = kb.create_task(conn, title="comment delivery", assignee="coder")
    assert kb.claim_task(conn, task_id, claimer="test-claimer") is not None
    return task_id


def test_expired_claim_and_dead_pid_are_not_reported_as_live_worker(
    kanban_home, monkeypatch,
):
    """A stale ``running`` status alone must not imply a reachable worker."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    conn = kb.connect()
    try:
        task_id = _claimed_task(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ?, worker_pid = ? WHERE id = ?",
                (int(time.time()) - 1, 424242, task_id),
            )

        result = kb.add_comment(conn, task_id, "operator", "Please continue.")

        assert result.reaches_current_worker is False
        assert result.effective_from == "next_worker_brief"
        assert result.worker_is_live is False
        assert len(kb.list_comments(conn, task_id)) == 1
    finally:
        conn.close()


def test_live_claim_and_pid_still_explain_that_next_brief_applies(
    kanban_home, monkeypatch,
):
    """Even a real current worker cannot receive text after its brief rendered."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    conn = kb.connect()
    try:
        task_id = _claimed_task(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (424243, task_id),
            )

        result = kb.add_comment(conn, task_id, "operator", "Priority changed.", kind="directive")

        assert result.reaches_current_worker is False
        assert result.effective_from == "next_worker_brief"
        assert result.worker_is_live is True
        assert "nächsten Worker-Brief" in result.message
    finally:
        conn.close()


def test_cli_makes_directive_effective_from_next_brief_visible(
    kanban_home, monkeypatch, capsys,
):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    conn = kb.connect()
    try:
        task_id = _claimed_task(conn)
    finally:
        conn.close()

    result = _cmd_comment(
        Namespace(
            task_id=task_id,
            text=["Use the new priority."],
            author="operator",
            directive=True,
            max_len=4000,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Directive added" in output
    assert "nächsten Worker-Brief" in output

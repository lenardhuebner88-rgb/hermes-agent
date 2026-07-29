"""Comment-write delivery metadata keeps operator feedback truthful."""

from __future__ import annotations

import re
import time
from argparse import Namespace

from hermes_cli import kanban_comment_delivery
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import _cmd_comment


def _claimed_task(conn) -> str:
    task_id = kb.create_task(conn, title="comment delivery", assignee="coder")
    assert kb.claim_task(conn, task_id, claimer="test-claimer") is not None
    return task_id


def test_default_return_is_the_int_comment_id(kanban_home):
    """The shared write path keeps its ``int`` contract unless delivery is asked.

    Callers such as ``kanban_close_sprint`` and ``pa_actions`` rely on the bare
    comment id; opting into delivery must stay strictly additive.
    """
    conn = kb.connect()
    try:
        task_id = _claimed_task(conn)
        comment_id = kb.add_comment(conn, task_id, "operator", "Plain note.")
        assert isinstance(comment_id, int)
        assert comment_id > 0
        assert len(kb.list_comments(conn, task_id)) == 1
    finally:
        conn.close()


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

        result = kanban_comment_delivery.write_comment(
            conn, task_id, "operator", "Please continue.",
        )

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

        result = kanban_comment_delivery.write_comment(
            conn, task_id, "operator", "Priority changed.", kind="directive",
        )

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


def test_worker_brief_declares_the_latest_comment_checkpoint(kanban_home):
    """The rendered brief exposes the precise comment state it includes."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="comment checkpoint", assignee="coder")
        kb.add_comment(conn, task_id, "operator", "First note.")
        latest_comment_id = kb.add_comment(conn, task_id, "operator", "Second note.")

        payload = kb.build_worker_context(conn, task_id, audience="operator")

        assert f"Comment checkpoint: comment_id_watermark={latest_comment_id}" in payload
        assert "Completion checkpoint: Before completing, re-read this task's comments" in payload
        assert "Second note." in payload
        assert (
            payload.index("Completion checkpoint:")
            < payload.index("## Assignment, acceptance criteria, and scope")
            < payload.index("## Relevant comments")
        )
    finally:
        conn.close()


def test_comments_after_worker_brief_checkpoint_are_identifiable(kanban_home):
    """Comment ids greater than the rendered watermark are newly arrived work."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="new comments", assignee="coder")
        known_comment_id = kb.add_comment(conn, task_id, "operator", "Known note.")
        payload = kb.build_worker_context(conn, task_id, audience="operator")
        watermark_match = re.search(r"comment_id_watermark=(\d+)", payload)
        assert watermark_match is not None
        watermark = int(watermark_match.group(1))
        new_comment_id = kb.add_comment(conn, task_id, "operator", "New note.")

        assert watermark == known_comment_id
        assert [comment.id for comment in kb.list_comments(conn, task_id) if comment.id > watermark] == [new_comment_id]
    finally:
        conn.close()


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_as_dict_returns_dict_not_none():
    """Kill return_none L20: as_dict() must return a real dict."""
    d = kanban_comment_delivery.CommentDelivery(
        comment_id=1, reaches_current_worker=True,
        effective_from="now", worker_is_live=True, message="ok",
    )
    result = d.as_dict()
    assert result is not None
    assert isinstance(result, dict)
    assert result["comment_id"] == 1
    assert result["reaches_current_worker"] is True


def test_build_delivery_unknown_task_raises(kanban_home):
    """Kill remove_guard L41: unknown task must raise ValueError."""
    conn = kb.connect()
    try:
        import pytest
        with pytest.raises(ValueError, match="unknown task"):
            kanban_comment_delivery.build_comment_delivery(
                conn, "nonexistent-task-id", 1, int(time.time()), lambda _p: True,
            )
    finally:
        conn.close()


def test_claim_expires_equal_now_is_not_valid(kanban_home, monkeypatch):
    """Kill comparison_swap L47 (> -> >=): claim_expires == now must NOT
    count as a valid claim (strict > required)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    conn = kb.connect()
    try:
        task_id = _claimed_task(conn)
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ?, worker_pid = ? WHERE id = ?",
                (now, 424244, task_id),
            )
        result = kanban_comment_delivery.build_comment_delivery(
            conn, task_id, 1, now, lambda _p: True,
        )
        # claim_expires == now -> NOT valid -> worker_is_live must be False
        assert result.worker_is_live is False
    finally:
        conn.close()

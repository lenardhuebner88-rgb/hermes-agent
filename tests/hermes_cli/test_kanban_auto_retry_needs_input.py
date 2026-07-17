"""S9b — auto-resume ``needs_input``/``capability`` blocks once an answer
comment lands.

Before this, ``auto_retry_blocked_tasks`` skipped these blocks forever until
an operator manually commented AND manually ran ``unblock``. This class of
trivially-answerable operator questions ("may I edit the test file of a
component already in my allowlist?", "no dashboard access for this
per-slice screenshot AC") required a human in the loop twice for a one-line
answer. Now the sweep unblocks automatically the first tick after any
non-self comment lands after the block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, assignee="coder", title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer=assignee)
    assert claimed is not None
    return tid


def test_operator_answer_comment_after_block_auto_unblocks(kanban_home: Path) -> None:
    """a) needs_input block + a non-self answer comment after it → the sweep
    unblocks and records a ``block_answered`` event with the comment id."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, assignee="coder")
        assert kb.block_task(conn, tid, reason="may I edit the schema too?", kind="needs_input")
        comment_id = kb.add_comment(conn, tid, author="piet-via-claude", body="yes, go ahead")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_kind is None
        assert any(t[0] == tid for t in retried)

        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        answered = [e for e in events if e["kind"] == "block_answered"]
        assert len(answered) == 1
        import json

        payload = json.loads(answered[0]["payload"])
        assert payload["comment_id"] == comment_id
        assert payload["author"] == "piet-via-claude"
        assert payload["block_kind"] == "needs_input"


def test_capability_block_with_answer_comment_auto_unblocks(kanban_home: Path) -> None:
    """New (live incident t_099b42d4): a ``capability`` block — e.g. a
    per-slice AC demanding authenticated dashboard screenshots the worker
    cannot take — resumes the same way once an operator answers it."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, assignee="coder")
        assert kb.block_task(
            conn, tid, reason="no dashboard access for the screenshot AC", kind="capability"
        )
        comment_id = kb.add_comment(
            conn, tid, author="piet-via-claude",
            body="screenshots happen at chain-end, complete the slice",
        )

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_kind is None
        assert any(t[0] == tid for t in retried)

        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        answered = [e for e in events if e["kind"] == "block_answered"]
        assert len(answered) == 1
        import json

        payload = json.loads(answered[0]["payload"])
        assert payload["comment_id"] == comment_id
        assert payload["block_kind"] == "capability"


def test_worker_self_comment_after_block_does_not_unblock(kanban_home: Path) -> None:
    """b) a comment from the worker's own lane identity (author == normalized
    assignee) after the block must NOT count as an answer."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, assignee="coder")
        assert kb.block_task(conn, tid, reason="may I edit the schema too?", kind="needs_input")
        kb.add_comment(conn, tid, author="coder", body="still thinking about this")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert not any(t[0] == tid for t in retried)

        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        assert not any(e["kind"] == "block_answered" for e in events)


def test_comment_before_block_does_not_unblock(kanban_home: Path) -> None:
    """c) a comment that predates the block (id <= watermark) must NOT count."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, assignee="coder")
        # Comment lands BEFORE the block — it's already covered by the
        # watermark, so it must not be (mis)read as an answer to the question.
        kb.add_comment(conn, tid, author="piet-via-claude", body="earlier unrelated note")
        assert kb.block_task(conn, tid, reason="may I edit the schema too?", kind="needs_input")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert not any(t[0] == tid for t in retried)


def test_other_block_kind_with_answer_comment_is_untouched(kanban_home: Path) -> None:
    """d) the new auto-unblock path is scoped to ``needs_input``/``capability``
    only — an ``integration`` block with a trailing non-self comment stays
    blocked."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, assignee="coder")
        assert kb.block_task(conn, tid, reason="merge conflict", kind="integration")
        kb.add_comment(conn, tid, author="piet-via-claude", body="resolved, try again")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "integration"
        assert not any(t[0] == tid for t in retried)

        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        assert not any(e["kind"] == "block_answered" for e in events)


def test_missing_watermark_skips_fail_closed(kanban_home: Path, monkeypatch) -> None:
    """Fail-safe: a needs_input block whose event carries no
    ``comment_id_watermark`` (legacy data) must not be auto-unblocked, even
    with a non-self comment sitting after it — no watermark means no
    reliable boundary to test comments against."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, assignee="coder")
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        # Strip the watermark from the persisted 'blocked' event payload to
        # simulate a legacy block written before the watermark existed.
        with kb.write_txn(conn):
            row = conn.execute(
                "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            import json

            payload = json.loads(row["payload"])
            payload.pop("comment_id_watermark", None)
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload), row["id"]),
            )
        kb.add_comment(conn, tid, author="piet-via-claude", body="here you go")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=2)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert not any(t[0] == tid for t in retried)

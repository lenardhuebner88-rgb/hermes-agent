"""Regression coverage for stale block_kind on legitimate unblock paths."""

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


def _running_task(conn, title: str = "worker task") -> str:
    task_id = kb.create_task(conn, title=title, assignee="coder")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id, claimer="test-worker")
    assert claimed is not None
    return task_id


def _make_running_again(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    assert kb.claim_task(conn, task_id, claimer="test-worker") is not None


def _block_fields(conn, task_id: str) -> dict[str, object]:
    row = conn.execute(
        "SELECT status, block_kind, block_recurrences "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def test_unblock_dependency_task_clears_visible_block_metadata(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = _running_task(conn)
        signal_id = kb.create_task(conn, title="signal", assignee="coder")

        assert kb.block_task(
            conn,
            task_id,
            reason="wait for signal",
            kind="dependency",
            wait_for={
                "type": "event_seen",
                "task_id": signal_id,
                "event_kind": "completed",
            },
        )
        assert _block_fields(conn, task_id) == {
            "status": "todo",
            "block_kind": "dependency",
            "block_recurrences": 1,
        }

        assert kb.unblock_task(
            conn,
            task_id,
            override_wait=True,
            actor="test-operator",
            reason="signal no longer required",
        )

        assert _block_fields(conn, task_id) == {
            "status": "ready",
            "block_kind": None,
            "block_recurrences": 0,
        }


def test_unblock_keeps_same_cause_loop_breaker_via_events(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = _running_task(conn)

        assert kb.block_task(conn, task_id, reason="need creds", kind="needs_input")
        assert kb.unblock_task(conn, task_id)
        assert _block_fields(conn, task_id) == {
            "status": "ready",
            "block_kind": None,
            "block_recurrences": 0,
        }

        _make_running_again(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="still need creds",
            kind="needs_input",
        )

        fields = _block_fields(conn, task_id)
        assert fields == {
            "status": "triage",
            "block_kind": "needs_input",
            "block_recurrences": 2,
        }
        events = [
            e for e in kb.list_events(conn, task_id)
            if e.kind == "block_loop_detected"
        ]
        assert events
        assert events[-1].payload == {
            "kind": "needs_input",
            "recurrences": 2,
            "reason": "still need creds",
        }


def test_untyped_unblock_keeps_loop_breaker_via_events(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = _running_task(conn)

        assert kb.block_task(conn, task_id, reason="first")
        assert kb.unblock_task(conn, task_id)
        assert _block_fields(conn, task_id) == {
            "status": "ready",
            "block_kind": None,
            "block_recurrences": 0,
        }

        _make_running_again(conn, task_id)
        assert kb.block_task(conn, task_id, reason="again")

        assert _block_fields(conn, task_id) == {
            "status": "triage",
            "block_kind": "transient",
            "block_recurrences": 2,
        }


def test_different_kind_after_unblock_starts_new_counter(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = _running_task(conn)

        assert kb.block_task(conn, task_id, reason="need input", kind="needs_input")
        assert kb.unblock_task(conn, task_id)
        _make_running_again(conn, task_id)
        assert kb.block_task(conn, task_id, reason="missing tool", kind="capability")

        assert _block_fields(conn, task_id) == {
            "status": "blocked",
            "block_kind": "capability",
            "block_recurrences": 1,
        }


def test_recompute_ready_clears_dependency_block_metadata(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="coder")
        child_id = _running_task(conn, title="child waits on parent")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
        assert kb.block_task(
            conn,
            child_id,
            reason="wait",
            kind="dependency",
            wait_for={"type": "parents_all_done", "task_ids": [parent_id]},
        )

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent_id,))

        assert kb.recompute_ready(conn) == 1

        assert _block_fields(conn, child_id) == {
            "status": "ready",
            "block_kind": None,
            "block_recurrences": 0,
        }


def test_promote_task_clears_block_metadata(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = _running_task(conn)
        assert kb.block_task(conn, task_id, reason="operator question", kind="needs_input")

        promoted, reason = kb.promote_task(
            conn,
            task_id,
            actor="operator",
            reason="answered",
        )

        assert promoted, reason
        assert _block_fields(conn, task_id) == {
            "status": "ready",
            "block_kind": None,
            "block_recurrences": 0,
        }

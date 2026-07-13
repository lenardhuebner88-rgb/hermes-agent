"""Regression coverage for respawn-guard timeline event deduplication."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a real Kanban database."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _respawn_guard_events(conn, task_id):
    return [
        json.loads(row["payload"])
        for row in conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'respawn_guarded' ORDER BY id",
            (task_id,),
        ).fetchall()
    ]


def test_dispatch_respawn_guard_dedups_only_consecutive_same_reason(
    kanban_home, all_assignees_spawnable
):
    now = int(time.time())
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="transient retry", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'coder', 'transient_retry', "
                "'transient_retry', ?, ?)",
                (task_id, now, now),
            )

        tick_results = [kb.dispatch_once(conn) for _ in range(3)]

        assert [result.respawn_guarded for result in tick_results] == [
            [(task_id, "transient_retry_backoff")],
            [(task_id, "transient_retry_backoff")],
            [(task_id, "transient_retry_backoff")],
        ]
        assert _respawn_guard_events(conn, task_id) == [
            {"reason": "transient_retry_backoff"}
        ]

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status = 'failed', outcome = 'timed_out' "
                "WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                ("Authentication failed: invalid credentials", task_id),
            )

        changed_reason = kb.dispatch_once(conn)
        assert changed_reason.respawn_guarded == [(task_id, "blocker_auth")]
        assert _respawn_guard_events(conn, task_id) == [
            {"reason": "transient_retry_backoff"},
            {"reason": "blocker_auth"},
        ]

        kb.add_comment(conn, task_id, "operator", "Retry after credentials refresh")
        repeated_after_comment = kb.dispatch_once(conn)

        assert repeated_after_comment.respawn_guarded == [(task_id, "blocker_auth")]
        assert _respawn_guard_events(conn, task_id) == [
            {"reason": "transient_retry_backoff"},
            {"reason": "blocker_auth"},
            {"reason": "blocker_auth"},
        ]

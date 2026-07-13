"""Auto-retry budget and loop-breaker coverage across unblock episodes."""

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


def _claim(conn, task_id: str) -> None:
    assert kb.claim_task(conn, task_id, claimer="test-worker") is not None


def _use_full_auto_retry_budget(conn, task_id: str, clock: list[int]) -> None:
    causes = (
        ("transient MCP unavailable", "capability"),
        ("tool crashed", "needs_input"),
    )
    for attempt, (reason, kind) in enumerate(causes, start=1):
        assert kb.block_task(conn, task_id, reason=reason, kind=kind)
        clock[0] += kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS + 1

        assert kb.auto_retry_blocked_tasks(conn) == [(task_id, attempt)]

        row = conn.execute(
            "SELECT status, auto_retry_count FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == attempt
        if attempt < kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT:
            _claim(conn, task_id)


def test_unblock_starts_fresh_auto_retry_budget_for_new_cause(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1_800_000_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="episodic retry", assignee="research")
        _claim(conn, task_id)
        _use_full_auto_retry_budget(conn, task_id, clock)

        _claim(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="capability disappeared",
            kind="capability",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET integration_retry_count = 3 WHERE id = ?",
                (task_id,),
            )

        assert kb.unblock_task(conn, task_id)
        row = conn.execute(
            "SELECT status, auto_retry_count, integration_retry_count "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "status": "ready",
            "auto_retry_count": 0,
            "integration_retry_count": 3,
        }

        _claim(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="transient input transport failure",
            kind="needs_input",
        )
        clock[0] += kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS + 1

        assert kb.auto_retry_blocked_tasks(conn) == [(task_id, 1)]
        assert not any(
            event.kind == "auto_retry_exhausted"
            for event in kb.list_events(conn, task_id)
        )


def test_unblock_preserves_same_cause_loop_breaker_after_budget_reset(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1_900_000_000]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="recurring block", assignee="research")
        _claim(conn, task_id)
        _use_full_auto_retry_budget(conn, task_id, clock)

        _claim(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="capability missing",
            kind="capability",
        )
        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).auto_retry_count == 0

        _claim(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="capability still missing",
            kind="capability",
        )

        row = conn.execute(
            "SELECT status, auto_retry_count, block_kind, block_recurrences "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "status": "triage",
            "auto_retry_count": 0,
            "block_kind": "capability",
            "block_recurrences": kb.BLOCK_RECURRENCE_LIMIT,
        }
        assert kb.auto_retry_blocked_tasks(conn) == []
        loop_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "block_loop_detected"
        ]
        assert loop_events[-1].payload == {
            "kind": "capability",
            "recurrences": kb.BLOCK_RECURRENCE_LIMIT,
            "reason": "capability still missing",
        }

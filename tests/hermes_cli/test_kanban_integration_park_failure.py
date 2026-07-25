"""Regression coverage for observable integration-park failures."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_kanban_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return home


def test_done_task_failed_integration_park_is_observable(
    isolated_kanban_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = {"reason": "post-merge gate failed: pytest[93]: exit 1"}

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="already done", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None
        run_id = kb.get_task(conn, task_id).current_run_id
        assert run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            result="completed before integration callback",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, task_id).status == "done"

        with caplog.at_level(logging.ERROR, logger=kb.__name__):
            assert not kb._park_integration(
                conn,
                task_id,
                outcome,
                expected_run_id=run_id,
            )

        failed_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "integration_park_failed"
        ]

    assert len(failed_events) == 1
    assert failed_events[0].payload == {
        "reason": f"integration parked: {outcome['reason']}",
        "actual_status": "done",
        "expected_run_id": run_id,
    }
    assert any(task_id in record.getMessage() for record in caplog.records)


def test_running_task_integration_park_behavior_is_unchanged(
    isolated_kanban_home: Path,
) -> None:
    outcome = {"reason": "post-merge gate failed: pytest[93]: exit 1"}

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="running park", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None
        run_id = kb.get_task(conn, task_id).current_run_id
        assert run_id is not None

        assert kb._park_integration(
            conn,
            task_id,
            outcome,
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT outcome, status FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        events = kb.list_events(conn, task_id)

    assert task.status == "blocked"
    assert run["outcome"] == kb.INTEGRATION_PARKED_OUTCOME
    assert run["status"] == "blocked"
    assert len([event for event in events if event.kind == "blocked"]) == 1
    assert not [
        event for event in events if event.kind == "integration_park_failed"
    ]

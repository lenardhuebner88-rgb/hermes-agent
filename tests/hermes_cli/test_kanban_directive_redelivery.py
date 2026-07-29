"""Regression coverage for operator-directive worker redelivery."""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def running_task_with_live_worker(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="directive target", assignee="coder")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:{os.getpid()}")
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), task_id)
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (os.getpid(), claimed.current_run_id),
        )
        conn.commit()
        yield conn, task_id, claimed.current_run_id


def _confirmed_termination(*_args, **_kwargs):
    return {"termination": "confirmed", "signal": "SIGTERM", "terminated": True}


def _redelivery_events(conn, task_id):
    return conn.execute(
        "SELECT payload, run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'directive_redelivered' ORDER BY id",
        (task_id,),
    ).fetchall()


def _persist_current_brief(task_id):
    with kb.connect_closing() as brief_conn:
        task = kb.get_task(brief_conn, task_id)
        assert task is not None
        kb._prepare_worker_brief_launch(task, board=None, audience="hermes")


def test_directive_present_in_delivered_brief_does_not_restart_worker(
    running_task_with_live_worker, monkeypatch
):
    conn, task_id, run_id = running_task_with_live_worker
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _confirmed_termination)

    delivered_directive_id = kb.add_comment(
        conn, task_id, "operator", "Already delivered direction", kind="directive"
    )
    _persist_current_brief(task_id)

    result = kb.dispatch_once(conn, max_spawn=0)

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert result.directive_redelivered == []
    assert _redelivery_events(conn, task_id) == []
    run = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run is not None
    assert json.loads(run["metadata"])["brief"]["comment_id_watermark"] == delivered_directive_id


def test_directive_restarts_live_worker_once_and_is_in_next_brief(
    running_task_with_live_worker, monkeypatch
):
    conn, task_id, old_run_id = running_task_with_live_worker
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _confirmed_termination)
    _persist_current_brief(task_id)

    directive_id = kb.add_comment(
        conn,
        task_id,
        "operator",
        "Stop the current approach and inspect the failing gate.",
        kind="directive",
    )

    first = kb.dispatch_once(conn, max_spawn=0)
    second = kb.dispatch_once(conn, max_spawn=0)

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"
    assert first.directive_redelivered == [task_id]
    assert second.directive_redelivered == []
    assert "Stop the current approach and inspect the failing gate." in kb.build_worker_context(
        conn, task_id, profile="worker_slim"
    )

    events = _redelivery_events(conn, task_id)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["directive_ids"] == [directive_id]
    assert payload["delivered_comment_id_watermark"] == 0
    assert payload["comment_id_watermark"] == directive_id
    assert payload["ended_run_id"] == old_run_id
    assert payload["reason"] == "operator_directive"
    assert events[0]["run_id"] == old_run_id

    old_run = conn.execute(
        "SELECT outcome, status FROM task_runs WHERE id = ?", (old_run_id,)
    ).fetchone()
    assert dict(old_run) == {"outcome": "directive_redelivered", "status": "restarted"}
    assert task.consecutive_failures == 0
    assert not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'gave_up'", (task_id,)
    ).fetchone()


def test_directive_without_persisted_brief_restarts_live_worker(
    running_task_with_live_worker, monkeypatch
):
    """Legacy in-flight workers have no brief watermark, so redelivery starts at zero."""
    conn, task_id, old_run_id = running_task_with_live_worker
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _confirmed_termination)
    directive_id = kb.add_comment(
        conn, task_id, "operator", "Recover this legacy worker.", kind="directive"
    )

    result = kb.dispatch_once(conn, max_spawn=0)

    assert result.directive_redelivered == [task_id]
    event = _redelivery_events(conn, task_id)[0]
    payload = json.loads(event["payload"])
    assert payload["directive_ids"] == [directive_id]
    assert payload["delivered_comment_id_watermark"] == 0
    assert payload["ended_run_id"] == old_run_id


def test_directives_arriving_together_share_one_restart_and_watermark(
    running_task_with_live_worker, monkeypatch
):
    conn, task_id, _old_run_id = running_task_with_live_worker
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _confirmed_termination)
    _persist_current_brief(task_id)

    first_directive = kb.add_comment(
        conn, task_id, "operator", "First direction", kind="directive"
    )
    second_directive = kb.add_comment(
        conn, task_id, "operator", "Second direction", kind="directive"
    )

    result = kb.dispatch_once(conn, max_spawn=0)

    assert result.directive_redelivered == [task_id]
    events = _redelivery_events(conn, task_id)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["directive_ids"] == [first_directive, second_directive]
    assert payload["delivered_comment_id_watermark"] == 0
    assert payload["comment_id_watermark"] == second_directive
    brief = kb.build_worker_context(conn, task_id, profile="worker_slim")
    assert "First direction" in brief
    assert "Second direction" in brief


def test_directive_arriving_during_termination_is_in_next_brief_without_restart(
    running_task_with_live_worker, monkeypatch
):
    conn, task_id, old_run_id = running_task_with_live_worker
    _persist_current_brief(task_id)
    first_directive = kb.add_comment(
        conn, task_id, "operator", "First direction", kind="directive"
    )

    def terminate_and_add_directive(*_args, **_kwargs):
        kb.add_comment(conn, task_id, "operator", "Direction during shutdown", kind="directive")
        return _confirmed_termination()

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate_and_add_directive)
    first = kb.dispatch_once(conn, max_spawn=0)

    late_directive = conn.execute(
        "SELECT MAX(id) AS id FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchone()["id"]
    events = _redelivery_events(conn, task_id)
    assert first.directive_redelivered == [task_id]
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["directive_ids"] == [
        first_directive,
        late_directive,
    ]
    assert json.loads(events[0]["payload"])["ended_run_id"] == old_run_id

    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:{os.getpid()}")
    assert claimed is not None
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), task_id))
    conn.execute(
        "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
        (os.getpid(), claimed.current_run_id),
    )
    conn.commit()
    _persist_current_brief(task_id)

    second = kb.dispatch_once(conn, max_spawn=0)
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == claimed.current_run_id
    assert second.directive_redelivered == []
    assert len(_redelivery_events(conn, task_id)) == 1


def test_plain_comment_does_not_restart_live_worker(
    running_task_with_live_worker, monkeypatch
):
    conn, task_id, run_id = running_task_with_live_worker
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _confirmed_termination)
    _persist_current_brief(task_id)

    kb.add_comment(conn, task_id, "operator", "FYI: keep the current approach.")
    result = kb.dispatch_once(conn, max_spawn=0)

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert result.directive_redelivered == []
    assert _redelivery_events(conn, task_id) == []

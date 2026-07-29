"""Behavioral tests for the queryable worker-run runtime timeline."""

from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_facts as facts


def _claimed_run(conn, *, assignee: str = "coder") -> tuple[str, int]:
    task_id = kb.create_task(conn, title="runtime facts", assignee=assignee, kind="code")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["current_run_id"]
    assert isinstance(run_id, int)
    return task_id, run_id


def test_timeline_keeps_exact_run_link_and_ignores_stale_or_duplicate_event(kanban_home):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="queued",
            observed_at_ms=100,
            source="tasks.created_at",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=300,
            source="agent.conversation_loop",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=200,
            source="late_old_callback",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=300,
            source="duplicate_callback",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_token",
            observed_at_ms=400,
            source="later_turn",
            preserve_first=True,
        )

        timeline = facts.list_timeline(conn, task_run_id=run_id)

    first_token = next(item for item in timeline if item["event_kind"] == "first_token")
    assert (first_token["observed_at_ms"], first_token["source"]) == (
        300,
        "agent.conversation_loop",
    )
    assert all(item["task_run_id"] == run_id for item in timeline)
    assert all(item["task_id"] == task_id for item in timeline)


def test_spawn_started_environment_writer_uses_injected_run_identity(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)

    assert facts.record_event_from_environment(
        "spawn_started",
        source="kanban_db._launch_worker_process",
        observed_at_ms=500,
        env={
            "HERMES_KANBAN_RUN_ID": str(run_id),
            "HERMES_KANBAN_DB": str(kb.kanban_db_path()),
            "HERMES_KANBAN_BOARD": "default",
        },
    )

    with kb.connect_closing() as conn:
        spawn_started = next(
            item
            for item in facts.list_timeline(conn, task_run_id=run_id)
            if item["event_kind"] == "spawn_started"
        )

    assert (spawn_started["observed_at_ms"], spawn_started["source"]) == (
        500,
        "kanban_db._launch_worker_process",
    )


def test_direct_process_locator_has_only_pid_and_timeline_leaves_first_token_absent(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        facts.record_locator(conn, task_run_id=run_id, pid=4242, env={})
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="first_llm_request",
            observed_at_ms=110,
            source="agent.conversation_loop",
        )
        facts.record_event(
            conn,
            task_run_id=run_id,
            event_kind="process_started",
            observed_at_ms=123,
            source="kanban_db._set_worker_pid",
        )

        locator = facts.get_locator(conn, task_run_id=run_id)
        timeline = facts.list_timeline(conn, task_run_id=run_id)
        request_to_token = facts.calculate_latencies(conn, task_run_id=run_id)[
            "request_to_first_token_ms"
        ]

    assert locator == {"locator_type": "pid", "pid": 4242}
    assert {"first_llm_request", "process_started"} <= {
        item["event_kind"] for item in timeline
    }
    assert request_to_token is None


def test_tmux_locator_contains_session_window_and_pane_without_process_payload(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        facts.record_locator(
            conn,
            task_run_id=run_id,
            pid=9001,
            env={"TMUX_PANE": "%7"},
            tmux_display=lambda _pane: ("workers", "3", "%7"),
        )
        locator = facts.get_locator(conn, task_run_id=run_id)

    assert locator == {
        "locator_type": "tmux_pane",
        "pid": 9001,
        "tmux_session": "workers",
        "tmux_window": "3",
        "tmux_pane": "%7",
    }

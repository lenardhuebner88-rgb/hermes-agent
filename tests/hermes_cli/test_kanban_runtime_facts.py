"""Behavioral tests for the queryable worker-run runtime timeline."""

from __future__ import annotations

import json

import pytest

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


@pytest.mark.parametrize(
    ("exit_code", "protocol_state", "outcome", "exit_kind"),
    [
        (0, "completed", "completed", "exited"),
        (17, "missing_completion", "crashed", "exited"),
        (None, "completed", "completed", "unobserved"),
        (0, "protocol_violation", "blocked", "exited"),
    ],
)
def test_terminal_facts_keep_process_protocol_outcome_and_reason_independent(
    kanban_home, exit_code, protocol_state, outcome, exit_kind
):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        kb._end_run(
            conn,
            _task_id,
            outcome=outcome,
            metadata={
                "worker_exit_kind": exit_kind,
                "worker_exit_code": exit_code,
                "worker_protocol_state": protocol_state,
                "worker_end_reason": "worker_process_observed",
            },
        )
        terminal = facts.get_terminal_facts(conn, task_run_id=run_id)

    assert terminal is not None
    assert terminal["worker_exit_code"] == exit_code
    assert terminal["worker_protocol_state"] == protocol_state
    assert terminal["task_outcome"] == outcome
    assert terminal["end_reason"] == "worker_process_observed"


@pytest.mark.parametrize(
    ("event_kind", "retry_class", "payload", "pass_run_id"),
    [
        ("auto_retried", "auto", {"blocked_run_id": "predecessor"}, False),
        (kb.INTEGRATION_RETRY_EVENT, "integration", {}, False),
        (kb.TRANSIENT_RETRY_EVENT, "transient", {}, True),
        ("unblocked", "operator", {}, False),
    ],
)
def test_append_event_stages_exact_retry_relationship_for_next_claim(
    kanban_home, event_kind, retry_class, payload, pass_run_id
):
    with kb.connect_closing() as conn:
        task_id, predecessor_id = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL WHERE id = ?",
            (task_id,),
        )
        resolved_payload = {
            key: predecessor_id if value == "predecessor" else value
            for key, value in payload.items()
        }
        event_id = kb._append_event(
            conn,
            task_id,
            event_kind,
            resolved_payload,
            run_id=predecessor_id if pass_run_id else None,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        retry_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["current_run_id"]

        link = facts.get_retry_link(conn, task_run_id=retry_id)
        event = conn.execute(
            "SELECT task_id, kind FROM task_events WHERE id = ?", (event_id,)
        ).fetchone()

    assert link is not None
    assert link["retry_of_task_run_id"] == predecessor_id
    assert link["retry_class"] == retry_class
    assert link["triggering_event_id"] == event_id
    assert link["task_id"] == task_id
    assert link["board"] == "default"
    assert event["task_id"] == task_id
    assert event["kind"] == event_kind


def _current_run(conn, task_id: str) -> int:
    return conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["current_run_id"]


def _pending_retry_rows(conn, task_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pending_worker_run_retry_links WHERE task_id = ?",
        (task_id,),
    ).fetchone()[0]


def _park(conn, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL "
        "WHERE id = ?",
        (status, task_id),
    )


def test_retry_event_classes_are_pinned_to_the_lifecycle_event_constants():
    assert facts.RETRY_EVENT_CLASSES == {
        "auto_retried": "auto",
        kb.INTEGRATION_RETRY_EVENT: "integration",
        kb.TRANSIENT_RETRY_EVENT: "transient",
        "unblocked": "operator",
    }
    assert set(facts.RETRY_EVENT_CLASSES.values()) <= facts.RETRY_CLASSES


def test_review_claim_consumes_lineage_so_a_later_coder_claim_inherits_nothing(
    kanban_home,
):
    """The review-lane run is the retry; a later coder run must stay unlinked."""
    with kb.connect_closing() as conn:
        task_id, crashed_run = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        _park(conn, task_id, "review")
        event_id = kb._append_event(
            conn, task_id, kb.TRANSIENT_RETRY_EVENT, {}, run_id=crashed_run
        )
        staged = conn.execute(
            "SELECT retry_of_task_run_id, retry_class FROM "
            "pending_worker_run_retry_links WHERE task_id = ?",
            (task_id,),
        ).fetchone()

        assert kb.claim_review_task(conn, task_id, reviewer_profile="reviewer") is not None
        review_run = _current_run(conn, task_id)
        review_link = facts.get_retry_link(conn, task_run_id=review_run)
        pending_after_review = _pending_retry_rows(conn, task_id)

        kb._end_run(conn, task_id, outcome="completed")
        _park(conn, task_id, "ready")
        assert kb.claim_task(conn, task_id) is not None
        coder_run = _current_run(conn, task_id)
        coder_link = facts.get_retry_link(conn, task_run_id=coder_run)
        pending_after_coder = _pending_retry_rows(conn, task_id)
        trigger = conn.execute(
            "SELECT task_id, kind FROM task_events WHERE id = ?", (event_id,)
        ).fetchone()

    assert (staged["retry_of_task_run_id"], staged["retry_class"]) == (
        crashed_run,
        "transient",
    )
    assert crashed_run < review_run < coder_run
    assert review_link is not None
    assert review_link["task_run_id"] == review_run
    assert review_link["retry_of_task_run_id"] == crashed_run
    assert review_link["retry_class"] == "transient"
    assert review_link["triggering_event_id"] == event_id
    assert review_link["task_id"] == task_id
    assert review_link["board"] == "default"
    assert (trigger["task_id"], trigger["kind"]) == (task_id, kb.TRANSIENT_RETRY_EVENT)
    assert coder_link is None
    assert (pending_after_review, pending_after_coder) == (0, 0)


def test_retry_event_leaves_lineage_absent_while_a_run_is_active(kanban_home):
    """With an active run the latest ended run is not the predecessor."""
    with kb.connect_closing() as conn:
        task_id, ended_run = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        _park(conn, task_id, "ready")
        assert kb.claim_task(conn, task_id) is not None
        active_run = _current_run(conn, task_id)

        kb._append_event(conn, task_id, "unblocked", {})
        pending_while_active = _pending_retry_rows(conn, task_id)

        # Control: the very same event does stage once no run is active, so the
        # empty result above is the guard and not a broken event path.
        kb._end_run(conn, task_id, outcome="crashed")
        kb._append_event(conn, task_id, "unblocked", {})
        staged_when_idle = conn.execute(
            "SELECT retry_of_task_run_id, retry_class FROM "
            "pending_worker_run_retry_links WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    assert ended_run < active_run
    assert pending_while_active == 0
    assert (staged_when_idle["retry_of_task_run_id"], staged_when_idle["retry_class"]) == (
        active_run,
        "operator",
    )


def test_invalid_staged_retry_link_is_discarded_instead_of_blocking_the_claim(
    kanban_home,
):
    with kb.connect_closing() as conn:
        task_id, first_run = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        _park(conn, task_id, "ready")
        event_id = kb._append_event(
            conn, task_id, "operator_retry", {"attempt": 1}, run_id=first_run
        )
        unknown_run = first_run + 10_000
        facts.stage_retry_link(
            conn,
            task_id=task_id,
            retry_of_task_run_id=unknown_run,
            retry_class="operator",
            triggering_event_id=event_id,
            board="default",
        )

        claimed = kb.claim_task(conn, task_id)
        retry_run = _current_run(conn, task_id)
        link = facts.get_retry_link(conn, task_run_id=retry_run)
        pending_after_claim = _pending_retry_rows(conn, task_id)
        rejected = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'retry_link_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        discarded = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'pending_retry_link_discarded' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert claimed is not None
    assert link is None
    assert pending_after_claim == 0
    assert json.loads(rejected["payload"])["finding"] == "missing_predecessor"
    discarded_payload = json.loads(discarded["payload"])
    assert discarded_payload["finding"] == "staged_link_no_longer_valid"
    assert discarded_payload["retry_of_task_run_id"] == unknown_run
    assert discarded_payload["task_run_id"] == retry_run


def test_foreign_task_retry_link_is_rejected_with_structured_diagnostic(kanban_home):
    with kb.connect_closing() as conn:
        first_task, foreign_run_id = _claimed_run(conn)
        kb._end_run(conn, first_task, outcome="crashed")
        second_task, second_run_id = _claimed_run(conn)
        event_id = conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'operator_retry', '{}', 1)",
            (second_task,),
        ).lastrowid

        with pytest.raises(ValueError, match="foreign_task_predecessor"):
            facts.record_retry_link(
                conn,
                task_run_id=second_run_id,
                retry_of_task_run_id=foreign_run_id,
                retry_class="operator",
                triggering_event_id=event_id,
            )
        diagnostic = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'retry_link_rejected'",
            (second_task,),
        ).fetchone()

    assert json.loads(diagnostic["payload"])["finding"] == "foreign_task_predecessor"


def test_retry_link_rejects_non_previous_predecessor(kanban_home):
    with kb.connect_closing() as conn:
        task_id, run_id = _claimed_run(conn)
        kb._end_run(conn, task_id, outcome="crashed")
        event_id = kb._append_event(
            conn,
            task_id,
            kb.TRANSIENT_RETRY_EVENT,
            {"attempt": 1},
            run_id=run_id,
        )

        with pytest.raises(ValueError, match="cyclic_or_non_previous_predecessor"):
            facts.record_retry_link(
                conn,
                task_run_id=run_id,
                retry_of_task_run_id=run_id,
                retry_class="transient",
                triggering_event_id=event_id,
            )
        diagnostic = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'retry_link_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert diagnostic is not None
    assert json.loads(diagnostic["payload"])["finding"] == (
        "cyclic_or_non_previous_predecessor"
    )


def test_worker_session_reuse_does_not_create_or_replace_retry_link(kanban_home):
    with kb.connect_closing() as conn:
        _task_id, run_id = _claimed_run(conn)
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            ('{"worker_session_id":"claude-session-1"}', run_id),
        )

        assert facts.get_retry_link(conn, task_run_id=run_id) is None


def test_claim_to_end_preserves_exact_worker_runtime_metadata(
    kanban_home, monkeypatch
) -> None:
    with kb.connect_closing() as conn:
        for runtime in ("hermes", "claude-cli"):
            task_id = kb.create_task(
                conn, title=f"runtime-{runtime}", assignee="coder"
            )
            monkeypatch.setattr(
                kb,
                "_spawn_identity_metadata",
                lambda *args, _runtime=runtime, **kwargs: {
                    "worker_runtime": _runtime,
                    "route_provider": "test-provider",
                    "model_source": "profile",
                },
            )
            claimed = kb.claim_task(conn, task_id)
            assert claimed is not None
            run_id = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["current_run_id"]

            kb._end_run(
                conn,
                task_id,
                outcome="completed",
                metadata={
                    "completion_fact": runtime,
                    "provider": "terminal-provider",
                    "model": "terminal-model",
                },
            )

            stored = json.loads(
                conn.execute(
                    "SELECT metadata FROM task_runs WHERE id = ?", (run_id,)
                ).fetchone()[0]
            )
            assert stored["worker_runtime"] == runtime
            assert stored["route_provider"] == "test-provider"
            assert stored["model_source"] == "profile"
            assert stored["completion_fact"] == runtime
            assert stored["provider"] == "terminal-provider"
            assert stored["model"] == "terminal-model"
            assert "cost_source" not in stored

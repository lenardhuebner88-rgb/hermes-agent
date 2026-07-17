"""Regression tests for deterministic failures at the spawn-dispatch seam."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


VERDICT_CAGE_ERROR = (
    "spawn_refused_allowlist_unenforceable: verdict-lane --allowedTools is not "
    "enforceable on '/usr/bin/claude'. Refusing to spawn a read-only verdict "
    "worker without an enforceable cage."
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Use the production schema in an isolated Hermes home."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize(
    ("error", "expected_marker"),
    [
        (VERDICT_CAGE_ERROR, "spawn_refused_allowlist_unenforceable"),
        ("embedded null byte", "embedded null byte"),
    ],
)
@pytest.mark.parametrize(
    ("failure_limit", "max_retries"),
    [
        (kb.DEFAULT_FAILURE_LIMIT, None),
        (3, None),
        (1, 5),
    ],
)
def test_deterministic_spawn_failure_escalates_without_transient_retry(
    kanban_home,
    error,
    expected_marker,
    failure_limit,
    max_retries,
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="verdict worker",
            assignee="reviewer",
            max_retries=max_retries,
        )
        assert kb.claim_task(conn, task_id) is not None

        result = kb._spawn_failure_or_transient_retry(
            conn,
            task_id,
            error,
            failure_limit=failure_limit,
            now=int(time.time()),
        )

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        classifications = [
            event
            for event in events
            if event.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]
        direct_class, direct_evidence = kb._classify_failure(
            error=error,
            outcome="spawn_failed",
        )

        assert result == ("escalated", True)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.transient_retry_count == 0
        assert kb.TRANSIENT_RETRY_EVENT not in [event.kind for event in events]
        assert kb.check_respawn_guard(conn, task_id) != "transient_retry_backoff"
        assert kb.OPERATOR_ESCALATION_EVENT in [event.kind for event in events]
        assert len(classifications) == 1
        assert classifications[0].payload["class"] == kb.HEILER_CLASS_BAD_SPEC
        assert classifications[0].payload["evidence"]["matched"] == expected_marker
        assert classifications[0].payload["evidence"]["signal_source"] == "text"
        assert direct_class == kb.HEILER_CLASS_BAD_SPEC
        assert direct_evidence["matched"] == expected_marker
        assert direct_evidence["signal_source"] == "text"


def test_strong_stall_class_precedes_deterministic_spawn_marker():
    heiler_class, evidence = kb._classify_failure(
        error=VERDICT_CAGE_ERROR,
        outcome="spawn_failed",
        stall_class="rate_limited_loop",
    )

    assert heiler_class == kb.HEILER_CLASS_TRANSIENT
    assert evidence["matched"] == "rate_limited_loop"
    assert evidence["signal_source"] == "stall_class"


def test_review_dispatch_deterministic_spawn_failure_blocks_first_attempt(
    kanban_home,
    all_assignees_spawnable,
):
    def verdict_cage_failure(task, workspace):
        raise RuntimeError(VERDICT_CAGE_ERROR)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review verdict cage",
            assignee="coder",
            max_retries=5,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?",
                (task_id,),
            )

        result = kb.dispatch_once(
            conn,
            spawn_fn=verdict_cage_failure,
            failure_limit=3,
            serialize_by_repo=False,
        )

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        latest_run = kb.list_runs(conn, task_id)[0]

    assert result.auto_blocked == [task_id]
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert task.transient_retry_count == 0
    assert latest_run.outcome == "gave_up"
    assert kb.TRANSIENT_RETRY_EVENT not in [event.kind for event in events]
    assert kb.OPERATOR_ESCALATION_EVENT in [event.kind for event in events]


@pytest.mark.parametrize("initial_status", ["ready", "review"])
def test_model_route_configuration_error_forces_first_attempt_block(
    kanban_home,
    all_assignees_spawnable,
    initial_status,
):
    def missing_model_route(task, workspace):
        raise kb.ModelRouteConfigurationError("no concrete model route")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title=f"{initial_status} missing route",
            assignee="coder",
            max_retries=5,
        )
        if initial_status == "review":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id = ?",
                    (task_id,),
                )

        result = kb.dispatch_once(
            conn,
            spawn_fn=missing_model_route,
            failure_limit=9,
            serialize_by_repo=False,
        )
        task = kb.get_task(conn, task_id)
        latest_run = kb.list_runs(conn, task_id)[0]

    assert result.auto_blocked == [task_id]
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert task.transient_retry_count == 0
    assert latest_run.outcome == "gave_up"


@pytest.mark.parametrize(
    "error",
    [
        "Resource temporarily unavailable",
        "Cannot allocate memory",
    ],
)
def test_infrastructure_spawn_failure_remains_transient(kanban_home, error):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="worker waiting for fork",
            assignee="coder",
        )
        assert kb.claim_task(conn, task_id) is not None

        result = kb._spawn_failure_or_transient_retry(
            conn,
            task_id,
            error,
            failure_limit=1,
            now=int(time.time()),
        )
        failure_class, _ = kb._classify_failure(
            error=error,
            outcome="spawn_failed",
        )
        events = kb.list_events(conn, task_id)

        assert result == ("retried", False)
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.TRANSIENT_RETRY_EVENT in [event.kind for event in events]
        assert kb.check_respawn_guard(conn, task_id) == "transient_retry_backoff"
        assert failure_class == kb.HEILER_CLASS_TRANSIENT

"""Unit tests for the fork-owned worker-termination classifier.

The board-level behaviour (a requested SIGKILL is not a crash) is covered in
tests/hermes_cli/test_kanban_db_claims.py; these tests pin the policy itself
so it stays readable without a full kanban board.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_worker_termination as kwterm


@pytest.fixture
def events_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE task_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT)"
    )
    return conn


def _event(conn, task_id, run_id, kind, payload):
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?,?,?,?)",
        (task_id, run_id, kind, json.dumps(payload)),
    )


def _marker(reason="archive_worker_alive", sigkill=True):
    return {"reason": reason, "termination": {"sigterm": True, "sigkill": sigkill}}


# ---------------------------------------------------------------------------
# intentional_sigkill_marker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", sorted(kwterm.INTENTIONAL_SIGKILL_REASONS),
)
def test_marker_is_found_for_every_intentional_reason(events_conn, reason):
    _event(events_conn, "t_1", 7, "reclaim_deferred", _marker(reason))
    assert kwterm.intentional_sigkill_marker(events_conn, "t_1", 7) is not None


def test_marker_requires_an_actual_sigkill(events_conn):
    """A deferred reclaim that only SIGTERMed did not kill the worker."""
    _event(events_conn, "t_1", 7, "reclaim_deferred", _marker(sigkill=False))
    assert kwterm.intentional_sigkill_marker(events_conn, "t_1", 7) is None


def test_marker_from_another_run_does_not_excuse_this_crash(events_conn):
    """Run scoping is the whole guard: an old kill must not whitewash a new crash."""
    _event(events_conn, "t_1", 6, "reclaim_deferred", _marker())
    assert kwterm.intentional_sigkill_marker(events_conn, "t_1", 7) is None


def test_marker_needs_a_run_id(events_conn):
    _event(events_conn, "t_1", 7, "reclaim_deferred", _marker())
    assert kwterm.intentional_sigkill_marker(events_conn, "t_1", None) is None


def test_unknown_reclaim_reason_is_not_intentional(events_conn):
    _event(events_conn, "t_1", 7, "reclaim_deferred", _marker("worktree_writer_active"))
    assert kwterm.intentional_sigkill_marker(events_conn, "t_1", 7) is None


def test_unparsable_payload_is_not_intentional(events_conn):
    events_conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?,?,?,?)",
        ("t_1", 7, "reclaim_deferred", "{not json"),
    )
    assert kwterm.intentional_sigkill_marker(events_conn, "t_1", 7) is None


# ---------------------------------------------------------------------------
# classify_external_termination
# ---------------------------------------------------------------------------


def test_classification_carries_everything_the_reaper_records(events_conn):
    _event(events_conn, "t_1", 7, "reclaim_deferred", _marker())

    external = kwterm.classify_external_termination(
        events_conn, "t_1", 7, pid=4242, claimer="host:worker",
        exit_kind="signaled", exit_code=9,
    )

    assert external is not None
    assert external["event_kind"] == kwterm.EXTERNAL_TERMINATION_EVENT
    assert "4242" in external["error_text"]
    assert "archive_worker_alive" in external["error_text"]
    payload = external["event_payload"]
    assert payload["pid"] == 4242
    assert payload["claimer"] == "host:worker"
    assert payload["signal"] == "SIGKILL"
    assert payload["exit_kind"] == "signaled"
    assert payload["exit_code"] == 9
    assert payload["requested_termination"] == {"sigterm": True, "sigkill": True}


def test_classification_returns_none_for_a_real_crash(events_conn):
    assert kwterm.classify_external_termination(
        events_conn, "t_1", 7, pid=4242, claimer="host:worker",
        exit_kind="signaled", exit_code=9,
    ) is None


# ---------------------------------------------------------------------------
# single_query_exit_code
# ---------------------------------------------------------------------------


def test_successful_run_exits_zero(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    assert kwterm.single_query_exit_code(None) == 0
    assert kwterm.single_query_exit_code({"failed": False}) == 0


def test_plain_failure_exits_one(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    assert kwterm.single_query_exit_code(
        {"failed": True, "failure_reason": "tool_error"}
    ) == 1


@pytest.mark.parametrize("reason", sorted(kwterm.RATE_LIMIT_FAILURE_REASONS))
def test_quota_wall_uses_the_rate_limit_sentinel(monkeypatch, reason):
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    assert kwterm.single_query_exit_code(
        {"failed": True, "failure_reason": reason}
    ) == KANBAN_RATE_LIMIT_EXIT_CODE


def test_non_worker_run_keeps_the_plain_contract(monkeypatch):
    """Only kanban workers get the sentinel; wrappers expect plain 0/1."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kwterm.single_query_exit_code(
        {"failed": True, "failure_reason": "rate_limit"}
    ) == 1

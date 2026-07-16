"""Honest breaker-trip error on gave_up run rows.

Live board 07-15 (t_2091df21, t_669e4014): two task_runs rows with
byte-identical error text. The second is the circuit-breaker trip
(metadata: failures/effective_limit/trigger_outcome) but task_runs.error
only repeats the raw last-attempt error — operators see a stuttering
duplicate instead of a tripped breaker.

Production call path (agent/turn_finalizer.py:172-191) on budget exhaust:

    _record_task_failure(
        conn, task_id,
        error="Iteration budget exhausted (6/6) — task could not complete "
              "within the allowed iterations",
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra={...},
        summary=...,
        expected_run_id=...,
    )

This module proves the gave_up terminal row prefixes an explicit breaker
message while keeping original error text as a suffix for text-signal
matching, and that non-tripping failure rows stay unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Exact production error shape from agent/turn_finalizer.py (live board 07-15).
_BUDGET_EXHAUSTED_ERROR = (
    "Iteration budget exhausted (6/6) — task could not complete "
    "within the allowed iterations"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _ready_task(conn):
    return kb.create_task(
        conn,
        title="breaker-honest-error",
        assignee="coder",
        max_iterations=6,
    )


def _claim(conn, tid):
    claimed = kb.claim_task(conn, tid, claimer="test-host:worker")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return claimed


def _production_timeout_failure(conn, tid, *, expected_run_id, failure_limit=None):
    """Mirror agent/turn_finalizer.py:172-191 parameter combination.

    No progress evidence so the auto-timeout continuation path does not
    intercept — we exercise the pure failure/breaker accounting path that
    produced the live gave_up duplicates.
    """
    kwargs = dict(
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra={
            "budget_used": 6,
            "budget_max": 6,
            "workspace_progress_size": 0,
        },
        summary="worker stopped mid-slice",
        expected_run_id=expected_run_id,
    )
    if failure_limit is not None:
        kwargs["failure_limit"] = failure_limit
    return kb._record_task_failure(
        conn,
        tid,
        _BUDGET_EXHAUSTED_ERROR,
        **kwargs,
    )


def test_breaker_trip_gave_up_run_error_is_honest(kanban_home):
    """done_when (1)+(2)+(3): gave_up terminal text prefixes the trip."""
    with kb.connect() as conn:
        tid = _ready_task(conn)
        limit = kb.DEFAULT_FAILURE_LIMIT  # 2

        # First burn: below threshold — not yet gave_up.
        claimed1 = _claim(conn, tid)
        blocked1 = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed1.current_run_id,
            failure_limit=limit,
        )
        assert blocked1 is False

        # Second burn: trips the breaker (failures >= effective_limit).
        claimed2 = _claim(conn, tid)
        blocked2 = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed2.current_run_id,
            failure_limit=limit,
        )
        assert blocked2 is True

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures == limit

        runs = kb.list_runs(conn, tid)
        gave_up = [r for r in runs if r.outcome == "gave_up"]
        assert len(gave_up) == 1, f"expected one gave_up run, got {[r.outcome for r in runs]}"
        terminal = gave_up[0]
        err = terminal.error or ""

        # (1) Honest terminal text: explicit breaker prefix + original suffix.
        assert err.startswith(
            f"circuit breaker tripped after {limit} consecutive timed_out "
            f"failures (limit {limit}):"
        ), f"gave_up error missing breaker prefix: {err!r}"
        assert _BUDGET_EXHAUSTED_ERROR in err, (
            f"original live error must remain as substring for text-signal "
            f"matching; got: {err!r}"
        )
        # Must NOT be a byte-identical copy of the raw predecessor error.
        assert err != _BUDGET_EXHAUSTED_ERROR

        # (2) Run metadata payload keys unchanged for consumers.
        meta = terminal.metadata or {}
        assert meta.get("failures") == limit
        assert meta.get("effective_limit") == limit
        assert meta.get("limit_source") == "dispatcher"
        assert meta.get("trigger_outcome") == "timed_out"

        # (3) Classification still sees the original signal via substring.
        events = kb.list_events(conn, tid)
        gave_up_ev = next(e for e in events if e.kind == "gave_up")
        assert gave_up_ev.payload.get("failures") == limit
        assert gave_up_ev.payload.get("effective_limit") == limit
        assert gave_up_ev.payload.get("limit_source") == "dispatcher"
        assert gave_up_ev.payload.get("trigger_outcome") == "timed_out"
        # Event payload error stays the raw caller string (classification input).
        assert _BUDGET_EXHAUSTED_ERROR in (gave_up_ev.payload.get("error") or "")


def test_non_tripping_failure_run_error_unchanged(kanban_home):
    """done_when (4): only the breaker branch rewrites the terminal error."""
    with kb.connect() as conn:
        tid = _ready_task(conn)
        claimed = _claim(conn, tid)
        blocked = _production_timeout_failure(
            conn,
            tid,
            expected_run_id=claimed.current_run_id,
            failure_limit=kb.DEFAULT_FAILURE_LIMIT,
        )
        assert blocked is False

        runs = kb.list_runs(conn, tid)
        timed = [r for r in runs if r.outcome == "timed_out"]
        assert len(timed) == 1
        assert timed[0].error == _BUDGET_EXHAUSTED_ERROR
        assert "circuit breaker tripped" not in (timed[0].error or "")

"""Tests for the worker-exit failure fingerprint (FAILURE-FINGERPRINT-S1).

``_record_task_failure`` funnels every non-success run outcome through one
place, but never stamped WHY it failed onto the run row beyond the raw
``error`` string, and the ``operator_escalation`` payload never told the
operator whether a repeated breaker-trip shared the SAME underlying cause
as a prior one. This mirrors the fixtures/queries the green-gate autoheal
already uses ("same-cause" streak) so the operator gets the identical
signal for kanban task failures.

Fixtures below are the exact error strings from live ``task_runs`` rows
(2026-07-03 planning query), not synthesized text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

ITERATION_BUDGET_ERROR = (
    "Iteration budget exhausted (12/12) — task could not complete within "
    "the allowed iterations"
)
PROTOCOL_VIOLATION_ERROR = (
    "worker exited cleanly (rc=0) without calling kanban_complete or "
    "kanban_block — protocol violation"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run_metadata(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, f"no task_runs row for {task_id}"
    return json.loads(row["metadata"] or "{}")


def test_error_fingerprint_normalizes_pid_but_keeps_causes_distinct() -> None:
    """Same underlying cause with a different PID collapses to one
    fingerprint; a genuinely different cause diverges."""
    fp_a = kb._error_fingerprint("pid 1788529 exited with code 1")
    fp_b = kb._error_fingerprint("pid 1750649 exited with code 1")
    fp_c = kb._error_fingerprint("pid 1783578 not alive")
    fp_d = kb._error_fingerprint(PROTOCOL_VIOLATION_ERROR)

    assert fp_a == fp_b
    assert fp_a != fp_c
    assert fp_a != fp_d
    assert fp_c != fp_d


def test_record_task_failure_stamps_worker_exit_kind_and_fingerprint(
    kanban_home: Path,
) -> None:
    """After _record_task_failure closes a run, the run row's metadata
    carries a non-NULL worker_exit_kind (the outcome class) and
    worker_failure_fingerprint (the normalized cause)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="crashed worker", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        kb._record_task_failure(
            conn, tid, "pid 1788529 exited with code 1",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        meta = _run_metadata(conn, tid)

    assert meta["worker_exit_kind"] == "crashed"
    assert meta["worker_failure_fingerprint"]
    assert meta["worker_failure_fingerprint"] == kb._error_fingerprint(
        "pid 1788529 exited with code 1"
    )


def test_record_task_failure_fingerprint_matches_across_runs_with_same_cause(
    kanban_home: Path,
) -> None:
    """Two independent tasks failing with the same underlying cause (only the
    PID differs) get the identical stamped fingerprint; a task failing for a
    different reason gets a different one."""
    with kb.connect_closing() as conn:
        same_a = kb.create_task(conn, title="crash a", assignee="coder")
        assert kb.claim_task(conn, same_a) is not None
        kb._record_task_failure(
            conn, same_a, "pid 1788529 exited with code 1",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )

        same_b = kb.create_task(conn, title="crash b", assignee="coder")
        assert kb.claim_task(conn, same_b) is not None
        kb._record_task_failure(
            conn, same_b, "pid 1750649 exited with code 1",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )

        different = kb.create_task(conn, title="budget exhausted", assignee="coder")
        assert kb.claim_task(conn, different) is not None
        kb._record_task_failure(
            conn, different, ITERATION_BUDGET_ERROR,
            outcome="timed_out", failure_limit=5,
            release_claim=True, end_run=True,
        )

        meta_a = _run_metadata(conn, same_a)
        meta_b = _run_metadata(conn, same_b)
        meta_diff = _run_metadata(conn, different)

    assert meta_a["worker_failure_fingerprint"] == meta_b["worker_failure_fingerprint"]
    assert meta_a["worker_failure_fingerprint"] != meta_diff["worker_failure_fingerprint"]
    assert meta_diff["worker_exit_kind"] == "timed_out"


def test_operator_escalation_evidence_carries_same_cause_count(
    kanban_home: Path,
) -> None:
    """Two consecutive same-cause failures trip the breaker; the resulting
    operator_escalation evidence.same_cause_count reflects both."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="iteration budget", assignee="coder")

        assert kb.claim_task(conn, tid) is not None
        blocked1 = kb._record_task_failure(
            conn, tid, ITERATION_BUDGET_ERROR,
            outcome="timed_out", failure_limit=2,
            release_claim=True, end_run=True,
        )
        assert blocked1 is False

        assert kb.claim_task(conn, tid) is not None
        blocked2 = kb._record_task_failure(
            conn, tid, ITERATION_BUDGET_ERROR,
            outcome="timed_out", failure_limit=2,
            release_claim=True, end_run=True,
        )
        assert blocked2 is True

        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert len(escalations) == 1
    assert escalations[0].payload["evidence"]["same_cause_count"] == 2


def test_operator_escalation_same_cause_count_resets_for_different_cause(
    kanban_home: Path,
) -> None:
    """A breaker trip whose two failures do NOT share a cause reports
    same_cause_count == 1 for the (only-matching) final failure."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="mixed causes", assignee="coder")

        assert kb.claim_task(conn, tid) is not None
        kb._record_task_failure(
            conn, tid, "pid 1783578 not alive",
            outcome="crashed", failure_limit=2,
            release_claim=True, end_run=True,
        )

        assert kb.claim_task(conn, tid) is not None
        blocked2 = kb._record_task_failure(
            conn, tid, PROTOCOL_VIOLATION_ERROR,
            outcome="crashed", failure_limit=2,
            release_claim=True, end_run=True,
        )
        assert blocked2 is True

        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert len(escalations) == 1
    assert escalations[0].payload["evidence"]["same_cause_count"] == 1

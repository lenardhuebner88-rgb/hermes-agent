from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli.landing_loop import (
    FailureClass,
    LL2Candidate,
    request_candidate_recovery,
)


def test_candidate_recovery_wireup_is_idempotent_across_restart(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")

    candidate = LL2Candidate(
        task_or_branch_id=f"loop/{task_id}",
        candidate_commit="a" * 40,
        failing_gate="affected",
        failure_class=FailureClass.CANDIDATE_REGRESSION,
    )

    assert request_candidate_recovery(candidate) == "requested"
    assert request_candidate_recovery(candidate) == "deduplicated"

    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'landing_recovery_requested'",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1

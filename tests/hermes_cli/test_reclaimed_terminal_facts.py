"""Terminal-fact coverage for invariant-recovery reclaims."""

from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_facts as facts


def _force_ready_with_current_run(conn, task_id: str, run_id: int) -> None:
    """Restore the leaked-pointer invariant that claim_task repairs."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = ?, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ?",
            (run_id, task_id),
        )


def test_reclaim_on_claim_records_only_unobserved_terminal_facts(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="reclaim terminal facts", assignee="worker")
        first_claim = kb.claim_task(conn, task_id, claimer="host:first")
        assert first_claim is not None
        assert kb.reclaim_task(conn, task_id, reason="prepare second run")

        second_claim = kb.claim_task(conn, task_id, claimer="host:second")
        assert second_claim is not None
        old_run_id = second_claim.current_run_id
        assert old_run_id is not None
        timeline_before = facts.list_timeline(conn, task_run_id=old_run_id)

        _force_ready_with_current_run(conn, task_id, old_run_id)
        replacement = kb.claim_task(conn, task_id, claimer="host:replacement")
        assert replacement is not None
        assert replacement.current_run_id != old_run_id

        terminal = facts.get_terminal_facts(conn, task_run_id=old_run_id)
        timeline_after = facts.list_timeline(conn, task_run_id=old_run_id)

    assert terminal is not None
    assert terminal["worker_exit_kind"] == "unobserved"
    assert terminal["worker_exit_code"] is None
    assert terminal["worker_protocol_state"] == "unobserved"
    assert terminal["task_outcome"] == "reclaimed"
    assert terminal["end_reason"] == "reclaimed"
    assert timeline_after == timeline_before


def test_reclaim_on_claim_does_not_overwrite_existing_terminal_facts(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="preserve terminal facts", assignee="worker")
        claimed = kb.claim_task(conn, task_id, claimer="host:observed")
        assert claimed is not None
        ended_run_id = claimed.current_run_id
        assert ended_run_id is not None

        kb._end_run(
            conn,
            task_id,
            outcome="completed",
            metadata={
                "worker_exit_kind": "exited",
                "worker_exit_code": 0,
                "worker_protocol_state": "completed",
                "worker_end_reason": "worker_reported",
            },
        )
        terminal_before = facts.get_terminal_facts(conn, task_run_id=ended_run_id)

        _force_ready_with_current_run(conn, task_id, ended_run_id)
        replacement = kb.claim_task(conn, task_id, claimer="host:replacement")
        assert replacement is not None
        terminal_after = facts.get_terminal_facts(conn, task_run_id=ended_run_id)
        terminal_rows = conn.execute(
            "SELECT COUNT(*) FROM worker_run_terminal_facts WHERE task_run_id = ?",
            (ended_run_id,),
        ).fetchone()[0]

    assert terminal_after == terminal_before
    assert terminal_rows == 1

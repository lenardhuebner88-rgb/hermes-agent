"""Regression coverage for iteration-budget self-report fingerprints."""

from __future__ import annotations

from hermes_cli import kanban_db as kb


def _claim_with_absent_worker(conn, task_id: str, *, pid: int):
    claimed = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
    assert claimed is not None
    assert claimed.current_run_id is not None
    # Match the production self-report path while keeping reaping deterministic:
    # the synthetic process group is absent, so no real process is signalled.
    kb._set_worker_pid(conn, task_id, pid)
    return claimed


def test_iteration_budget_self_report_stamps_failure_fingerprint(kanban_home):
    summary = "Iteration budget exhausted mid-implementation. Stopped all new work."

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="iteration budget fingerprint",
            assignee="coder",
            max_iterations=4,
            max_continuations=1,
        )
        claimed = _claim_with_absent_worker(conn, task_id, pid=987654321)

        assert kb.record_iteration_budget_exhausted(
            conn,
            task_id,
            summary=summary,
            expected_run_id=claimed.current_run_id,
        )

        run = kb.list_runs(conn, task_id)[-1]
        assert run.error == summary
        assert run.metadata["worker_exit_kind"] == "iteration_budget_exhausted"
        assert run.metadata["worker_failure_fingerprint"]
        assert kb.get_task(conn, task_id).consecutive_failures == 0


def test_iteration_budget_self_report_fingerprint_forms_real_streak(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="repeated iteration budget fingerprint",
            assignee="coder",
            max_iterations=4,
            max_continuations=3,
        )

        fingerprints = []
        pids = (987654321, 987654322, 987654323)
        for index, pid in enumerate(pids):
            claimed = _claim_with_absent_worker(conn, task_id, pid=pid)
            assert kb.record_iteration_budget_exhausted(
                conn,
                task_id,
                summary=f"Iteration budget exhausted while worker pid {pid} stopped.",
                expected_run_id=claimed.current_run_id,
            )
            run = kb.list_runs(conn, task_id)[-1]
            fingerprints.append(run.metadata["worker_failure_fingerprint"])
            if index < len(pids) - 1:
                assert kb.reap_pending_continuations(conn) == [task_id]

        assert fingerprints[0] == fingerprints[1] == fingerprints[2]
        assert all(str(pid) not in fingerprints[0] for pid in pids)
        assert kb._persistent_failure_fingerprint_streak(conn, task_id) == (
            fingerprints[0],
            3,
        )
        assert kb.get_task(conn, task_id).consecutive_failures == 0

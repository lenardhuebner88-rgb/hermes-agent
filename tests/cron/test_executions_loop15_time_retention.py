"""Loop 15 / F2: time-based retention for the cron executions ledger.

Pure count-based retention (50 terminal runs/job) gave no history horizon:
high-frequency jobs burned through their window in hours and error runs were
evicted as fast as successes. _prune_unlocked now deletes a terminal row only
when it is (a) beyond the per-job keep-N window AND (b) older than the
keep-days horizon (HERMES_CRON_EXECUTIONS_KEEP_DAYS, default 30) AND (c) not
a failed/unknown row inside that horizon. Since loop 18 the global cap is
horizon-aware: it only deletes rows already older than the horizon and warns
loudly when the ledger overflows the cap with in-horizon rows, instead of
silently eating the time guarantee at high run volume (old loop-15 cap
semantics). The in-horizon overflow behaviour is covered in
test_executions_loop18_horizon_cap.py.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from hermes_time import now as _hermes_now


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _run(executions, job_id, *, success=True):
    row = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(row["id"], success=success)
    return row["id"]


def _age(executions, execution_id, *, days):
    """Backdate one row's claimed_at (the column prune orders/filters by)."""
    claimed = (_hermes_now() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(executions._current_executions_file())
    try:
        conn.execute(
            "UPDATE executions SET claimed_at=? WHERE id=?",
            (claimed, execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_keep_days_default():
    import cron.executions as executions

    assert executions.KEEP_TERMINAL_EXECUTIONS_DAYS == 30


def test_old_run_beyond_keep_window_is_deleted(monkeypatch, tmp_path):
    """A row beyond keep-N AND older than the horizon is pruned."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")

    ids = [_run(executions, "aged") for _ in range(5)]
    for execution_id in ids:
        _age(executions, execution_id, days=40)
    fresh = _run(executions, "aged")  # this finish triggers the prune

    records = executions.list_executions(job_id="aged", limit=500)
    remaining = {r["id"] for r in records}
    # keep-N=2: the fresh run plus the newest aged row survive.
    assert len(records) == 2
    assert fresh in remaining


def test_fresh_run_beyond_keep_window_is_kept(monkeypatch, tmp_path):
    """The time guarantee: rows beyond keep-N but inside the horizon stay."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")

    for _ in range(5):
        _run(executions, "fresh")

    assert len(executions.list_executions(job_id="fresh", limit=500)) == 5


def test_failed_run_within_horizon_survives_beyond_keep_window(
    monkeypatch, tmp_path
):
    """Error history outlives the success flood for the keep-days horizon."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")

    failed_ids = [_run(executions, "erratic", success=False) for _ in range(4)]
    for execution_id in failed_ids:
        _age(executions, execution_id, days=10)  # inside the 30d horizon
    for _ in range(2):
        _run(executions, "erratic", success=True)  # triggers prune

    records = executions.list_executions(job_id="erratic", limit=500)
    assert len(records) == 6
    assert {r["id"] for r in records if r["status"] == "failed"} == set(failed_ids)


def test_old_failed_run_beyond_horizon_is_deleted(monkeypatch, tmp_path):
    """failed/unknown earns no retention past the horizon."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")

    failed_ids = [_run(executions, "old-errors", success=False) for _ in range(4)]
    for execution_id in failed_ids:
        _age(executions, execution_id, days=40)
    fresh = _run(executions, "old-errors", success=True)

    records = executions.list_executions(job_id="old-errors", limit=500)
    remaining = {r["id"] for r in records}
    assert len(records) == 2
    assert fresh in remaining
    # Only the newest aged row (still inside keep-N=2) survives.
    assert len([r for r in records if r["status"] == "failed"]) == 1


def test_global_cap_prunes_rows_beyond_horizon(monkeypatch, tmp_path):
    """The global cap still bounds OLD terminal history: rows past the
    keep-days horizon are pruned down to the cap, oldest first. (Loop 18
    changed the cap to be horizon-aware; the in-horizon overflow case it no
    longer prunes is covered in test_executions_loop18_horizon_cap.py.)"""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "50")
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

    aged = [_run(executions, "capped") for _ in range(10)]
    for execution_id in aged:
        _age(executions, execution_id, days=40)
    fresh = _run(executions, "capped")  # fresh run triggers the prune

    records = executions.list_executions(limit=500)
    # Cap=5: the fresh run plus the four newest aged rows survive.
    assert len(records) == 5
    assert fresh in {r["id"] for r in records}


def test_keep_days_zero_disables_time_guarantee(monkeypatch, tmp_path):
    """HERMES_CRON_EXECUTIONS_KEEP_DAYS=0 reverts to pure count retention."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_DAYS", "0")

    for _ in range(5):
        _run(executions, "legacy")

    assert len(executions.list_executions(job_id="legacy", limit=500)) == 2


def test_quiet_job_history_survives_noisy_job(monkeypatch, tmp_path):
    """The combined guarantee: a rare job keeps 30 days of history even when
    a noisy sibling produces far more than keep-N runs in the same period."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "3")

    quiet_ids = [_run(executions, "quiet") for _ in range(6)]
    for execution_id in quiet_ids[:3]:
        _age(executions, execution_id, days=20)  # older, but inside horizon
    for _ in range(20):
        _run(executions, "noisy")

    quiet = executions.list_executions(job_id="quiet", limit=500)
    assert len(quiet) == 6  # nothing pruned: all within the 30d horizon
    noisy = executions.list_executions(job_id="noisy", limit=500)
    assert len(noisy) == 20  # same guarantee applies to the noisy job itself

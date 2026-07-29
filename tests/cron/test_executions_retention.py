"""Per-job + global-cap retention for the cron executions ledger (A-M9).

The old global FIFO cap let high-frequency jobs evict every other job's
history (live: two */3min + */5min jobs ≈ 770 runs/day pushed daily report
jobs out of the ledger within ~1.5 days). Retention is now per-job (newest N
terminal executions per job_id) with a global safety cap on top.
"""

from __future__ import annotations


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


def test_retention_defaults():
    import cron.executions as executions

    assert executions.KEEP_TERMINAL_EXECUTIONS_PER_JOB == 50
    assert executions.MAX_TERMINAL_EXECUTIONS == 20000


def test_noisy_job_cannot_evict_quiet_job(monkeypatch, tmp_path):
    """A job past its per-job limit is trimmed; a job below it keeps its
    full history (the A-M9 regression shape)."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "5")
    # Count-only retention: loop 15 added a time-based guarantee (fresh rows
    # beyond the count window are kept); these tests pin the count semantics.
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_DAYS", "0")

    for _ in range(20):
        _run(executions, "noisy")
    for _ in range(3):
        _run(executions, "quiet")

    assert len(executions.list_executions(job_id="noisy", limit=500)) == 5
    assert len(executions.list_executions(job_id="quiet", limit=500)) == 3
    assert len(executions.list_executions(limit=500)) == 8


def test_per_job_retention_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_DAYS", "0")  # count-only

    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    for _ in range(5):
        _run(executions, "live")

    records = executions.list_executions(job_id="live", limit=500)
    assert len([r for r in records if r["status"] == "completed"]) == 2
    assert any(
        r["id"] == inflight["id"] and r["status"] == "running" for r in records
    )


def test_global_cap_bounds_total_terminal_history(monkeypatch, tmp_path):
    """Many jobs each under their per-job limit are still bounded in total
    by the global safety cap (module-constant override)."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "8")
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 10)

    for job_index in range(5):
        for _ in range(8):
            _run(executions, f"job-{job_index}")

    assert len(executions.list_executions(limit=500)) == 10
    for job_index in range(5):
        assert (
            len(executions.list_executions(job_id=f"job-{job_index}", limit=500))
            <= 8
        )


def test_global_cap_env_override(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "4")

    for _ in range(6):
        _run(executions, "capped")

    assert len(executions.list_executions(limit=500)) == 4


def test_env_override_beats_module_constant(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "KEEP_TERMINAL_EXECUTIONS_PER_JOB", 50)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_DAYS", "0")  # count-only

    for _ in range(6):
        _run(executions, "env-job")

    assert len(executions.list_executions(job_id="env-job", limit=500)) == 2


def test_invalid_env_falls_back_to_constant(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "KEEP_TERMINAL_EXECUTIONS_PER_JOB", 3)
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "not-a-number")
    monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_DAYS", "0")  # count-only

    for _ in range(6):
        _run(executions, "bad-env")

    assert len(executions.list_executions(job_id="bad-env", limit=500)) == 3

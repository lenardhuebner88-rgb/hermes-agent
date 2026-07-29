"""Audit loop 10 (B-M6): per-job script timeout via the job record's
``timeout_seconds`` field.

Before the fix, ``_get_script_timeout()`` was the only bound (global: module
override / HERMES_CRON_SCRIPT_TIMEOUT / config.yaml / 3600s default) — jobs
could not carry their own timeout, so e.g. reporting scripts could not be
clamped tighter than the global 1h default.

The fix adds ``_resolve_script_timeout(job)``: a valid per-job
``timeout_seconds`` (> 0, hard-capped at 7200s) wins; invalid values are
rejected with a warning and fall back to the global default. ``_run_job_script``
takes an optional ``timeout_seconds`` parameter; both call paths
(``_run_job_script_with_claim_heartbeat`` and ``_build_job_prompt``) resolve it
from the job record.

Hermetic: scripts run locally under a tmp HERMES_HOME; no network.
"""

import sys
import time
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME (same shape as
    test_cron_script.py's fixture)."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


class TestResolveScriptTimeout:
    """Resolution rules for the per-job field (no subprocess needed)."""

    def test_valid_per_job_timeout_wins(self, monkeypatch):
        from cron.scheduler import _resolve_script_timeout

        monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "300")
        assert _resolve_script_timeout({"id": "j1", "timeout_seconds": 30}) == 30

    def test_missing_field_uses_global(self, monkeypatch):
        from cron.scheduler import _resolve_script_timeout

        monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "300")
        assert _resolve_script_timeout({"id": "j1"}) == 300
        assert _resolve_script_timeout(None) == 300

    @pytest.mark.parametrize("bad", ["bogus", 0, -10, {}])
    def test_invalid_value_falls_back_to_global(self, monkeypatch, bad):
        from cron.scheduler import _resolve_script_timeout

        monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "300")
        assert _resolve_script_timeout({"id": "j1", "timeout_seconds": bad}) == 300

    def test_value_capped_at_7200(self, monkeypatch):
        import cron.scheduler as sched_mod
        from cron.scheduler import _resolve_script_timeout

        monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
        assert (
            _resolve_script_timeout({"id": "j1", "timeout_seconds": 99999})
            == sched_mod._MAX_PER_JOB_SCRIPT_TIMEOUT
            == 7200
        )

    def test_numeric_string_accepted(self, monkeypatch):
        from cron.scheduler import _resolve_script_timeout

        monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
        assert _resolve_script_timeout({"id": "j1", "timeout_seconds": "45"}) == 45


class TestRunJobScriptPerJobTimeout:
    """The bound is actually enforced on the subprocess."""

    def test_per_job_timeout_kills_long_script(self, cron_env, monkeypatch):
        """A 1s per-job timeout must kill a 30s script long before the global
        default (patched to a large value so ONLY the per-job field can be
        responsible)."""
        import cron.scheduler as sched_mod
        from cron.scheduler import _run_job_script_with_claim_heartbeat

        monkeypatch.setattr(sched_mod, "_SCRIPT_TIMEOUT", 3600)

        script = cron_env / "scripts" / "slow.py"
        script.write_text("import time; time.sleep(30)\n")

        job = {"id": "tight-job", "timeout_seconds": 1}
        started = time.monotonic()
        success, output = _run_job_script_with_claim_heartbeat(job, str(script))
        elapsed = time.monotonic() - started

        assert success is False
        assert "timed out after 1s" in output
        assert elapsed < 20, f"script ran {elapsed:.1f}s — per-job timeout not applied"

    def test_invalid_per_job_timeout_uses_global_default(
        self, cron_env, monkeypatch, caplog,
    ):
        """An invalid timeout_seconds value is ignored (with a warning) and the
        global timeout applies instead."""
        import cron.scheduler as sched_mod
        from cron.scheduler import _run_job_script_with_claim_heartbeat

        # Global bound of 2s: if the invalid per-job value were honored (or
        # crashed resolution), the 30s script would NOT die after ~2s.
        monkeypatch.setattr(sched_mod, "_SCRIPT_TIMEOUT", 2)

        script = cron_env / "scripts" / "slow.py"
        script.write_text("import time; time.sleep(30)\n")

        job = {"id": "bogus-timeout-job", "timeout_seconds": "bogus"}
        with caplog.at_level("WARNING", logger="cron.scheduler"):
            success, output = _run_job_script_with_claim_heartbeat(job, str(script))

        assert success is False
        assert "timed out after 2s" in output
        assert any(
            "invalid timeout_seconds" in record.message
            for record in caplog.records
        ), "invalid per-job timeout must log a warning"

    def test_run_job_script_timeout_seconds_parameter(self, cron_env, monkeypatch):
        """_run_job_script's explicit timeout_seconds parameter is honored
        directly (the path _build_job_prompt uses)."""
        import cron.scheduler as sched_mod
        from cron.scheduler import _run_job_script

        monkeypatch.setattr(sched_mod, "_SCRIPT_TIMEOUT", 3600)

        script = cron_env / "scripts" / "slow.py"
        script.write_text("import time; time.sleep(30)\n")

        success, output = _run_job_script(str(script), timeout_seconds=1)
        assert success is False
        assert "timed out after 1s" in output

    def test_fast_script_unaffected_by_per_job_timeout(self, cron_env, monkeypatch):
        """A script that finishes within its per-job bound runs normally."""
        from cron.scheduler import _run_job_script_with_claim_heartbeat

        script = cron_env / "scripts" / "fast.py"
        script.write_text('print("done")\n')

        job = {"id": "fast-job", "timeout_seconds": 30}
        success, output = _run_job_script_with_claim_heartbeat(job, str(script))
        assert success is True
        assert output == "done"

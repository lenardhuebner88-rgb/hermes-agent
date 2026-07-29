"""Audit loop 6: per-job run retry (A-M8) and bounded parallel cap (A-M5).

Run retry: a job with ``retry = {"max_attempts": N<=3, "backoff_seconds":
[..]}`` does NOT advance its regular schedule on a transient failure —
``mark_job_run`` re-arms ``next_run_at`` at now + backoff instead, up to N
consecutive attempts, then falls back to normal schedule advancement. Jobs
without the field keep the exact old behaviour.

Parallel cap: with neither HERMES_CRON_MAX_PARALLEL nor
cron.max_parallel_jobs set, the parallel pool now defaults to 4 workers
instead of unbounded; explicit env/config values are unchanged.

Hermetic: no network, no real sleeping — the clock is injected.
"""

from datetime import datetime, timedelta, timezone

import cron.jobs as jobs_mod
import cron.scheduler as s
from cron.jobs import compute_next_run, create_job, get_job, mark_job_run, use_cron_store


def _pin_clock(monkeypatch, start):
    holder = {"now": start}
    monkeypatch.setattr(jobs_mod, "_hermes_now", lambda: holder["now"])
    return holder


_START = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class TestRunRetry:
    """(c) Transient failure ⇒ next_run_at in the near future instead of
    schedule advancement; after max_attempts the schedule advances normally."""

    def test_failed_run_rearms_with_backoff_then_advances(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        with use_cron_store(home):
            _pin_clock(monkeypatch, _START)
            job = create_job(
                "report prompt",
                "every 1h",
                name="retry-job",
                deliver="local",
                retry={"max_attempts": 2, "backoff_seconds": [60, 120]},
            )
            assert job["retry"] == {"max_attempts": 2, "backoff_seconds": [60.0, 120.0]}

            # 1st failure: re-armed at now + first backoff rung (NOT +1h).
            mark_job_run(job["id"], False, "transient API error")
            j = get_job(job["id"])
            assert j["last_status"] == "error"
            assert j["last_error"] == "transient API error"
            assert j["retry_attempts"] == 1
            assert j["next_run_at"] == (_START + timedelta(seconds=60)).isoformat()

            # 2nd failure: next backoff rung.
            mark_job_run(job["id"], False, "still down")
            j = get_job(job["id"])
            assert j["retry_attempts"] == 2
            assert j["next_run_at"] == (_START + timedelta(seconds=120)).isoformat()

            # 3rd failure: retries exhausted → regular schedule advancement.
            mark_job_run(job["id"], False, "down again")
            j = get_job(job["id"])
            assert j["retry_attempts"] == 2  # not incremented past the cap
            assert j["next_run_at"] == compute_next_run(j["schedule"], _START.isoformat())

            # A success resets the consecutive-failure counter.
            mark_job_run(job["id"], True)
            j = get_job(job["id"])
            assert j["retry_attempts"] == 0
            assert j["last_status"] == "ok"

    def test_retry_attempts_capped_at_three(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        with use_cron_store(home):
            _pin_clock(monkeypatch, _START)
            job = create_job(
                "prompt", "every 1h", deliver="local",
                retry={"max_attempts": 9, "backoff_seconds": [10]},
            )
            # Hard cap: 9 requested → 3 effective; short ladder reuses its
            # last entry for the remaining attempts.
            assert job["retry"]["max_attempts"] == jobs_mod.MAX_RUN_RETRY_ATTEMPTS == 3
            for expected in (1, 2, 3):
                mark_job_run(job["id"], False, "x")
                j = get_job(job["id"])
                assert j["retry_attempts"] == expected
                assert j["next_run_at"] == (_START + timedelta(seconds=10)).isoformat()
            # 4th failure: cap reached → schedule advances normally.
            mark_job_run(job["id"], False, "x")
            j = get_job(job["id"])
            assert j["next_run_at"] == compute_next_run(j["schedule"], _START.isoformat())

    def test_default_backoff_when_ladder_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        with use_cron_store(home):
            _pin_clock(monkeypatch, _START)
            job = create_job(
                "prompt", "every 1h", deliver="local",
                retry={"max_attempts": 1},
            )
            mark_job_run(job["id"], False, "x")
            j = get_job(job["id"])
            expected = _START + timedelta(
                seconds=jobs_mod.DEFAULT_RUN_RETRY_BACKOFF_SECONDS
            )
            assert j["next_run_at"] == expected.isoformat()

    def test_no_retry_field_keeps_old_behaviour(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        with use_cron_store(home):
            _pin_clock(monkeypatch, _START)
            job = create_job("prompt", "every 1h", deliver="local")
            assert "retry" not in job  # byte-identical default
            mark_job_run(job["id"], False, "boom")
            j = get_job(job["id"])
            assert "retry_attempts" not in j
            assert j["next_run_at"] == compute_next_run(j["schedule"], _START.isoformat())


class TestParallelCap:
    """(d) No env/config ⇒ default cap 4; env override still wins;
    explicitly passed values are unchanged."""

    def _reset_pool(self):
        s._parallel_pool = None
        s._parallel_pool_max_workers = None

    def test_default_cap_is_4(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_MAX_PARALLEL", raising=False)
        self._reset_pool()
        pool = s._get_parallel_pool(None)
        assert pool._max_workers == 4
        s._shutdown_parallel_pool()

    def test_explicit_value_unchanged(self, monkeypatch):
        self._reset_pool()
        pool = s._get_parallel_pool(9)
        assert pool._max_workers == 9
        s._shutdown_parallel_pool()

    def test_env_override_respected_through_tick(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "7")
        self._reset_pool()
        s._running_job_ids.clear()

        job = {
            "id": "cap-job",
            "name": "cap-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }
        monkeypatch.setattr(s, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(s, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            s, "run_job", lambda j, **_kw: (True, "out", "resp", None),
        )
        monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(s, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(s, "_deliver_result", lambda *_a, **_kw: None)

        assert s.tick(verbose=False) == 1
        assert s._parallel_pool is not None
        assert s._parallel_pool._max_workers == 7
        s._shutdown_parallel_pool()

    def test_tick_without_env_uses_default_cap(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_MAX_PARALLEL", raising=False)
        self._reset_pool()
        s._running_job_ids.clear()

        job = {
            "id": "cap-default-job",
            "name": "cap-default",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }
        monkeypatch.setattr(s, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(s, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            s, "run_job", lambda j, **_kw: (True, "out", "resp", None),
        )
        monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(s, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(s, "_deliver_result", lambda *_a, **_kw: None)

        assert s.tick(verbose=False) == 1
        assert s._parallel_pool is not None
        assert s._parallel_pool._max_workers == 4
        s._shutdown_parallel_pool()

"""Loop 15 / F1: retry/timeout policy fields exposed on create + update.

retry, timeout_seconds and delivery_timeout_seconds existed in the job
record and were honored by the scheduler, but no regular create/update
surface could set them. They are now validated through one pair of helpers
in cron.jobs (validate_retry_policy / validate_timeout_field) and exposed on
the cronjob tool (create + update) and `hermes cron create/edit`.
"""

import json

import pytest

import cron.jobs as jobs
from cron.jobs import (
    MAX_JOB_DELIVERY_TIMEOUT_SECONDS,
    MAX_JOB_TIMEOUT_SECONDS,
    validate_retry_policy,
    validate_timeout_field,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestValidateRetryPolicy:
    def test_valid_policy_normalized(self):
        assert validate_retry_policy({"max_attempts": 2}) == {"max_attempts": 2}
        assert validate_retry_policy(
            {"max_attempts": 3, "backoff_seconds": [60, 300]}
        ) == {"max_attempts": 3, "backoff_seconds": [60, 300]}

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="retry must be an object"):
            validate_retry_policy("3")

    def test_rejects_bad_max_attempts(self):
        for bad in (None, 0, 4, -1, "2", 2.5, True):
            with pytest.raises(ValueError, match="max_attempts"):
                validate_retry_policy({"max_attempts": bad})

    def test_rejects_bad_backoff(self):
        with pytest.raises(ValueError, match="backoff_seconds must be a list"):
            validate_retry_policy({"max_attempts": 1, "backoff_seconds": 60})
        with pytest.raises(ValueError, match="at most 8 entries"):
            validate_retry_policy(
                {"max_attempts": 1, "backoff_seconds": [60] * 9}
            )
        for bad_entry in (0, -5, "60", 60.5, True):
            with pytest.raises(ValueError, match="positive integers"):
                validate_retry_policy(
                    {"max_attempts": 1, "backoff_seconds": [bad_entry]}
                )


class TestValidateTimeoutField:
    def test_bounds(self):
        assert validate_timeout_field("timeout_seconds", 1, MAX_JOB_TIMEOUT_SECONDS) == 1
        assert (
            validate_timeout_field(
                "timeout_seconds", 7200, MAX_JOB_TIMEOUT_SECONDS
            )
            == 7200
        )
        assert (
            validate_timeout_field(
                "delivery_timeout_seconds", 600, MAX_JOB_DELIVERY_TIMEOUT_SECONDS
            )
            == 600
        )

    def test_rejects_out_of_range_and_wrong_types(self):
        for bad in (0, -1, 7201, "300", 30.5, True, None):
            with pytest.raises(ValueError, match="timeout_seconds"):
                validate_timeout_field(
                    "timeout_seconds", bad, MAX_JOB_TIMEOUT_SECONDS
                )
        with pytest.raises(ValueError, match="delivery_timeout_seconds"):
            validate_timeout_field(
                "delivery_timeout_seconds", 601, MAX_JOB_DELIVERY_TIMEOUT_SECONDS
            )


class TestCreateJobPolicyFields:
    def test_stores_policy_fields(self, tmp_cron_dir):
        job = jobs.create_job(
            prompt="check stuff",
            schedule="every 1h",
            deliver="local",
            retry={"max_attempts": 2, "backoff_seconds": [60, 300]},
            timeout_seconds=300,
            delivery_timeout_seconds=45,
        )
        assert job["retry"] == {"max_attempts": 2, "backoff_seconds": [60, 300]}
        assert job["timeout_seconds"] == 300
        assert job["delivery_timeout_seconds"] == 45

        stored = jobs.get_job(job["id"])
        assert stored["retry"] == {"max_attempts": 2, "backoff_seconds": [60, 300]}
        assert stored["timeout_seconds"] == 300
        assert stored["delivery_timeout_seconds"] == 45

    def test_absent_by_default(self, tmp_cron_dir):
        job = jobs.create_job(prompt="plain", schedule="every 1h", deliver="local")
        assert "retry" not in job
        assert "timeout_seconds" not in job
        assert "delivery_timeout_seconds" not in job

    def test_invalid_timeout_rejected(self, tmp_cron_dir):
        with pytest.raises(ValueError, match="timeout_seconds"):
            jobs.create_job(
                prompt="x", schedule="every 1h", timeout_seconds=99999
            )
        with pytest.raises(ValueError, match="delivery_timeout_seconds"):
            jobs.create_job(
                prompt="x", schedule="every 1h", delivery_timeout_seconds=0
            )
        assert jobs.list_jobs() == []


class TestUpdateJobPolicyFields:
    def test_set_and_clear(self, tmp_cron_dir):
        job = jobs.create_job(prompt="x", schedule="every 1h", deliver="local")

        updated = jobs.update_job(
            job["id"],
            {
                "retry": {"max_attempts": 2, "backoff_seconds": [60]},
                "timeout_seconds": 120,
                "delivery_timeout_seconds": 30,
            },
        )
        assert updated["timeout_seconds"] == 120

        cleared = jobs.update_job(
            job["id"],
            {"retry": None, "timeout_seconds": None, "delivery_timeout_seconds": None},
        )
        # Clearing removes the keys entirely (absent == unset), not null.
        reloaded = jobs.get_job(job["id"])
        for field in ("retry", "timeout_seconds", "delivery_timeout_seconds"):
            assert field not in reloaded
        assert "retry" not in cleared


class TestCronjobToolPolicyFields:
    def _create(self, **kwargs):
        from tools.cronjob_tools import cronjob

        return json.loads(
            cronjob(
                action="create",
                schedule="every 1h",
                prompt="do the thing",
                deliver="local",
                **kwargs,
            )
        )

    def test_create_with_policy_fields(self, tmp_cron_dir):
        result = self._create(
            retry={"max_attempts": 2, "backoff_seconds": [60]},
            timeout_seconds=300,
            delivery_timeout_seconds=45,
        )
        assert result["success"] is True
        job = jobs.get_job(result["job_id"])
        assert job["retry"] == {"max_attempts": 2, "backoff_seconds": [60]}
        assert job["timeout_seconds"] == 300
        assert job["delivery_timeout_seconds"] == 45
        # Surface in the formatted job view too.
        assert result["job"]["retry"]["max_attempts"] == 2
        assert result["job"]["timeout_seconds"] == 300

    def test_create_rejects_invalid_policy(self, tmp_cron_dir):
        result = self._create(retry={"max_attempts": 9})
        assert result["success"] is False
        assert "max_attempts" in result["error"]

        result = self._create(timeout_seconds=7201)
        assert result["success"] is False
        assert "timeout_seconds" in result["error"]

        result = self._create(delivery_timeout_seconds=601)
        assert result["success"] is False
        assert "delivery_timeout_seconds" in result["error"]

        assert jobs.list_jobs() == []

    def test_create_rejects_clear_semantics(self, tmp_cron_dir):
        result = self._create(retry={})
        assert result["success"] is False
        assert "update" in result["error"]
        result = self._create(timeout_seconds=0)
        assert result["success"] is False
        assert "update" in result["error"]

    def test_update_sets_and_clears_policy(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        created = self._create()
        job_id = created["job_id"]

        updated = json.loads(
            cronjob(
                action="update",
                job_id=job_id,
                retry={"max_attempts": 3},
                timeout_seconds=600,
                delivery_timeout_seconds=120,
            )
        )
        assert updated["success"] is True
        job = jobs.get_job(job_id)
        assert job["retry"] == {"max_attempts": 3}
        assert job["timeout_seconds"] == 600
        assert job["delivery_timeout_seconds"] == 120

        cleared = json.loads(
            cronjob(
                action="update",
                job_id=job_id,
                retry={},
                timeout_seconds=0,
                delivery_timeout_seconds=0,
            )
        )
        assert cleared["success"] is True
        job = jobs.get_job(job_id)
        for field in ("retry", "timeout_seconds", "delivery_timeout_seconds"):
            assert field not in job

    def test_update_rejects_invalid_policy(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        created = self._create()
        result = json.loads(
            cronjob(
                action="update",
                job_id=created["job_id"],
                retry={"max_attempts": 2, "backoff_seconds": [0]},
            )
        )
        assert result["success"] is False
        assert "backoff" in result["error"]

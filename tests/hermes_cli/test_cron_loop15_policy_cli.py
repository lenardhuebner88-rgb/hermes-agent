"""Loop 15 / F1: `hermes cron create/edit` exposes retry + timeout flags.

The CLI validates through the same canonical cron.jobs helpers as the
cronjob model tool (no second copy of the rules) and passes the values
through the tool's create/update actions. Edit maps 0 to the tool's clear
semantics.
"""

from argparse import Namespace

import pytest

from cron.jobs import create_job, get_job, list_jobs
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _create_args(**overrides):
    base = dict(
        cron_command="create",
        schedule="every 1h",
        prompt="check the thing",
        name=None,
        deliver="local",
        repeat=None,
        skill=None,
        skills=None,
        script=None,
        workdir=None,
        no_agent=False,
        retry_attempts=None,
        retry_backoff=None,
        timeout_seconds=None,
        delivery_timeout_seconds=None,
    )
    base.update(overrides)
    return Namespace(**base)


def _edit_args(job_id, **overrides):
    base = dict(
        cron_command="edit",
        job_id=job_id,
        schedule=None,
        prompt=None,
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        clear_skills=False,
        add_skills=None,
        remove_skills=None,
        script=None,
        workdir=None,
        no_agent=None,
        retry_attempts=None,
        retry_backoff=None,
        timeout_seconds=None,
        delivery_timeout_seconds=None,
    )
    base.update(overrides)
    return Namespace(**base)


class TestCronCreatePolicyFlags:
    def test_create_with_retry_and_timeouts(self, tmp_cron_dir, capsys):
        rc = cron_command(
            _create_args(
                retry_attempts=2,
                retry_backoff=[60, 300],
                timeout_seconds=300,
                delivery_timeout_seconds=45,
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Retry: 2 attempt(s)" in out
        assert "Timeout: 300s" in out
        assert "Delivery timeout: 45s" in out

        job = list_jobs()[0]
        assert job["retry"] == {"max_attempts": 2, "backoff_seconds": [60, 300]}
        assert job["timeout_seconds"] == 300
        assert job["delivery_timeout_seconds"] == 45

    def test_create_rejects_invalid_retry(self, tmp_cron_dir, capsys):
        rc = cron_command(_create_args(retry_attempts=9))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Invalid retry/timeout options" in out
        assert "max_attempts" in out
        assert list_jobs() == []

    def test_create_rejects_backoff_without_attempts(self, tmp_cron_dir, capsys):
        rc = cron_command(_create_args(retry_backoff=[60]))
        assert rc == 1
        assert "--retry-backoff requires --retry-attempts" in capsys.readouterr().out
        assert list_jobs() == []

    def test_create_rejects_invalid_timeout(self, tmp_cron_dir, capsys):
        rc = cron_command(_create_args(delivery_timeout_seconds=601))
        assert rc == 1
        assert "delivery_timeout_seconds" in capsys.readouterr().out
        assert list_jobs() == []

    def test_create_rejects_zero_clear_on_create(self, tmp_cron_dir, capsys):
        rc = cron_command(_create_args(retry_attempts=0))
        assert rc == 1
        assert list_jobs() == []


class TestCronEditPolicyFlags:
    def test_edit_sets_and_clears(self, tmp_cron_dir, capsys):
        job = create_job(prompt="x", schedule="every 1h", deliver="local")

        rc = cron_command(
            _edit_args(
                job["id"],
                retry_attempts=3,
                retry_backoff=[120],
                timeout_seconds=600,
                delivery_timeout_seconds=90,
            )
        )
        assert rc == 0
        updated = get_job(job["id"])
        assert updated["retry"] == {"max_attempts": 3, "backoff_seconds": [120]}
        assert updated["timeout_seconds"] == 600
        assert updated["delivery_timeout_seconds"] == 90

        rc = cron_command(
            _edit_args(
                job["id"],
                retry_attempts=0,
                timeout_seconds=0,
                delivery_timeout_seconds=0,
            )
        )
        assert rc == 0
        cleared = get_job(job["id"])
        for field in ("retry", "timeout_seconds", "delivery_timeout_seconds"):
            assert field not in cleared

    def test_edit_rejects_invalid_values(self, tmp_cron_dir, capsys):
        job = create_job(prompt="x", schedule="every 1h", deliver="local")

        rc = cron_command(_edit_args(job["id"], retry_attempts=4))
        assert rc == 1
        assert "max_attempts" in capsys.readouterr().out

        rc = cron_command(_edit_args(job["id"], timeout_seconds=7201))
        assert rc == 1
        assert "timeout_seconds" in capsys.readouterr().out

        unchanged = get_job(job["id"])
        assert "retry" not in unchanged
        assert "timeout_seconds" not in unchanged

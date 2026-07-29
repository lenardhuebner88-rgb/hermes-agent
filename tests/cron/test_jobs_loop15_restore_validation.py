"""Loop 15 / F3: strict backup validation + quarantine rollback on recovery.

Loop 4 validated only the envelope (parseable JSON + a jobs list). Recovery
now validates every job record against the create_job shape (id, name,
schedule with a plausible kind payload), discards invalid records with a
warning count, and only treats a backup as a recovery source when at least
one valid record survives (an honestly-empty list stays a valid empty
restore). A failed restore rewrite rolls the quarantine back so jobs.json is
never missing entirely, and concurrent load_jobs() calls on a corrupt store
are serialized under _jobs_lock() — exactly one quarantine, no crash.
"""

import json
import logging
import threading

import pytest

import cron.jobs as jobs


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (documented compat surface)."""
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _valid_record(tag):
    return {
        "id": f"job-{tag}",
        "name": f"job-{tag}",
        "prompt": tag,
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
    }


def _jobs_file(tmp_path):
    return tmp_path / "cron" / "jobs.json"


def _write_backup(tmp_path, records, index=1):
    jobs.ensure_dirs()
    backup = _jobs_file(tmp_path).with_name(f"jobs.json.bak.{index}")
    backup.write_text(json.dumps({"jobs": records}), encoding="utf-8")
    return backup


class TestRecordValidation:
    def test_plausible_shapes_accepted(self):
        assert jobs._is_plausible_job_record(_valid_record("a"))
        cron_record = _valid_record("b")
        cron_record["schedule"] = {"kind": "cron", "expr": "0 9 * * *"}
        assert jobs._is_plausible_job_record(cron_record)
        once_record = _valid_record("c")
        once_record["schedule"] = {"kind": "once", "run_at": "2026-08-01T09:00:00+00:00"}
        assert jobs._is_plausible_job_record(once_record)

    def test_implausible_shapes_rejected(self):
        assert not jobs._is_plausible_job_record(None)
        assert not jobs._is_plausible_job_record("job-a")
        assert not jobs._is_plausible_job_record({"name": "x", "schedule": {"kind": "interval", "minutes": 5}})
        record = _valid_record("d")
        record["id"] = ""
        assert not jobs._is_plausible_job_record(record)
        record = _valid_record("e")
        record["name"] = None
        assert not jobs._is_plausible_job_record(record)
        record = _valid_record("f")
        record["schedule"] = "every 1h"  # raw string, not a parsed dict
        assert not jobs._is_plausible_job_record(record)
        record = _valid_record("g")
        record["schedule"] = {"kind": "interval", "minutes": -5}
        assert not jobs._is_plausible_job_record(record)
        record = _valid_record("h")
        record["schedule"] = {"kind": "cron", "expr": ""}
        assert not jobs._is_plausible_job_record(record)
        record = _valid_record("i")
        record["schedule"] = {"kind": "martian"}
        assert not jobs._is_plausible_job_record(record)


class TestStrictBackupRecovery:
    def test_mixed_backup_restores_only_valid_records(
        self, tmp_cron_dir, tmp_path, caplog
    ):
        jobs.save_jobs([_valid_record("good")])
        # Rewrite the newest backup with one valid + one corrupt record.
        _write_backup(
            tmp_path,
            [_valid_record("good"), {"id": "", "prompt": "torn"}],
        )
        _jobs_file(tmp_path).write_bytes(b"{ corrupt main")

        with caplog.at_level(logging.WARNING, logger="cron.jobs"):
            loaded = jobs.load_jobs()

        assert [j["id"] for j in loaded] == ["job-good"]
        assert "discarded 1 invalid job record" in caplog.text
        restored = json.loads(_jobs_file(tmp_path).read_text(encoding="utf-8"))
        assert [j["id"] for j in restored["jobs"]] == ["job-good"]

    def test_all_corrupt_backup_is_not_a_recovery_source(
        self, tmp_cron_dir, tmp_path
    ):
        jobs.save_jobs([_valid_record("good")])
        _write_backup(tmp_path, [{"id": "", "prompt": "torn"}, {"broken": True}])
        corrupt = b"{ corrupt main"
        _jobs_file(tmp_path).write_bytes(corrupt)

        with pytest.raises(RuntimeError, match="corrupted and unrepairable"):
            jobs.load_jobs()
        # Fail-closed: the corrupt store stays in place, nothing quarantined.
        assert _jobs_file(tmp_path).read_bytes() == corrupt
        assert not list((tmp_path / "cron").glob("jobs.json.corrupt-*"))

    def test_empty_backup_is_an_honest_empty_restore(self, tmp_cron_dir, tmp_path):
        jobs.save_jobs([_valid_record("good")])
        _write_backup(tmp_path, [])
        _jobs_file(tmp_path).write_bytes(b"{ corrupt main")

        loaded = jobs.load_jobs()
        assert loaded == []
        restored = json.loads(_jobs_file(tmp_path).read_text(encoding="utf-8"))
        assert restored["jobs"] == []

    def test_older_valid_backup_used_when_newest_is_all_garbage(
        self, tmp_cron_dir, tmp_path
    ):
        jobs.save_jobs([_valid_record("old")])
        jobs.save_jobs([_valid_record("new")])
        _write_backup(tmp_path, [{"id": "", "prompt": "torn"}], index=1)
        _jobs_file(tmp_path).write_bytes(b"{ corrupt main")

        loaded = jobs.load_jobs()
        assert [j["id"] for j in loaded] == ["job-old"]


class TestQuarantineRollback:
    def test_failed_restore_rolls_back_quarantine(
        self, tmp_cron_dir, tmp_path, monkeypatch
    ):
        jobs.save_jobs([_valid_record("good")])
        corrupt = b"{ corrupt main"
        _jobs_file(tmp_path).write_bytes(corrupt)

        def _boom(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(jobs, "atomic_replace", _boom)

        with pytest.raises(RuntimeError, match="corrupted and unrepairable"):
            jobs.load_jobs()

        # The quarantine was rolled back: the corrupt store is back in place
        # instead of jobs.json being missing entirely.
        assert _jobs_file(tmp_path).read_bytes() == corrupt
        assert not list((tmp_path / "cron").glob("jobs.json.corrupt-*"))

    def test_successful_restore_still_quarantines(self, tmp_cron_dir, tmp_path):
        jobs.save_jobs([_valid_record("good")])
        _jobs_file(tmp_path).write_bytes(b"{ corrupt main")

        loaded = jobs.load_jobs()
        assert [j["id"] for j in loaded] == ["job-good"]
        assert len(list((tmp_path / "cron").glob("jobs.json.corrupt-*"))) == 1


class TestConcurrentRecovery:
    def test_concurrent_loads_quarantine_exactly_once(
        self, tmp_cron_dir, tmp_path
    ):
        jobs.save_jobs([_valid_record("good")])
        _jobs_file(tmp_path).write_bytes(b"{ corrupt main")

        results: dict = {}
        errors: list = []

        def _load(tag):
            try:
                results[tag] = [j["id"] for j in jobs.load_jobs()]
            except Exception as exc:  # noqa: BLE001 - test captures all
                errors.append(exc)

        threads = [threading.Thread(target=_load, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert results == {i: ["job-good"] for i in range(4)}
        quarantined = list((tmp_path / "cron").glob("jobs.json.corrupt-*"))
        assert len(quarantined) == 1

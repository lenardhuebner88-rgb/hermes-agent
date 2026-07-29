"""Loop 4 (A-H4): jobs.json backup rotation + quarantine/backup recovery.

A corrupt jobs.json previously raised RuntimeError from load_jobs() on every
tick, laming the whole scheduler with no restore path. Now every successful
atomic save rotates jobs.json.bak.1..3, and load_jobs() quarantines a corrupt
store and restores the newest VALID backup before falling back to the old
fail-closed RuntimeError.
"""

import json
import os
import stat

import pytest

import cron.jobs as jobs


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (documented compat surface)."""
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _save(tmp_path, tag):
    # Records must carry the create_job shape (id/name/schedule) — loop 15
    # made backup recovery validate each record, so a bare {"id": ...} stub
    # would be discarded as implausible.
    jobs.save_jobs([{
        "id": f"job-{tag}",
        "name": f"job-{tag}",
        "prompt": tag,
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
    }])


def _jobs_file(tmp_path):
    return tmp_path / "cron" / "jobs.json"


class TestBackupRotation:
    def test_save_creates_backup_copy(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "v1")
        backup = _jobs_file(tmp_path).with_name("jobs.json.bak.1")
        assert backup.is_file()
        data = json.loads(backup.read_text(encoding="utf-8"))
        assert [j["id"] for j in data["jobs"]] == ["job-v1"]

    def test_backup_is_owner_only(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "v1")
        backup = _jobs_file(tmp_path).with_name("jobs.json.bak.1")
        mode = stat.S_IMODE(os.stat(backup).st_mode)
        assert mode == 0o600, oct(mode)

    def test_rotation_shifts_and_caps_at_three(self, tmp_cron_dir, tmp_path):
        for tag in ("v1", "v2", "v3", "v4"):
            _save(tmp_path, tag)
        cron_dir = tmp_path / "cron"
        backups = sorted(p.name for p in cron_dir.glob("jobs.json.bak.*"))
        assert backups == ["jobs.json.bak.1", "jobs.json.bak.2", "jobs.json.bak.3"]

        def ids(name):
            data = json.loads((cron_dir / name).read_text(encoding="utf-8"))
            return [j["id"] for j in data["jobs"]]

        # bak.1 mirrors the just-written store; older copies shift down.
        assert ids("jobs.json.bak.1") == ["job-v4"]
        assert ids("jobs.json.bak.2") == ["job-v3"]
        assert ids("jobs.json.bak.3") == ["job-v2"]

    def test_rotation_failure_never_breaks_the_save(self, tmp_cron_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(
            jobs.shutil, "copy2",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk ro")),
        )
        _save(tmp_path, "v1")  # must not raise
        assert [j["id"] for j in jobs.load_jobs()] == ["job-v1"]


class TestCorruptionRecovery:
    def test_corrupt_store_restores_newest_valid_backup(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "good")
        jobs_file = _jobs_file(tmp_path)
        original = jobs_file.read_bytes()
        jobs_file.write_bytes(b"{ not valid json !!!")

        loaded = jobs.load_jobs()
        assert [j["id"] for j in loaded] == ["job-good"]

        # Restored store parses and carries the backup's jobs.
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
        assert [j["id"] for j in data["jobs"]] == ["job-good"]

        # The corrupt bytes were quarantined, not deleted.
        quarantined = list((tmp_path / "cron").glob("jobs.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == b"{ not valid json !!!"
        assert original != jobs_file.read_bytes()

    def test_recovery_is_visible_via_ticker_error_channel(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "good")
        _jobs_file(tmp_path).write_bytes(b"\x00\x01 garbage")
        jobs.load_jobs()
        message = jobs.get_ticker_last_error()
        assert message is not None
        assert "quarantined" in message
        assert "restored" in message

    def test_skips_invalid_backup_and_uses_older_valid_one(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "old")
        _save(tmp_path, "new")
        cron_dir = tmp_path / "cron"
        # Newest backup is damaged too; bak.2 still holds "old".
        (cron_dir / "jobs.json.bak.1").write_bytes(b"also corrupt {")
        _jobs_file(tmp_path).write_bytes(b"corrupt main {")

        loaded = jobs.load_jobs()
        assert [j["id"] for j in loaded] == ["job-old"]

    def test_no_valid_backup_keeps_fail_closed_runtimeerror(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "good")
        jobs_file = _jobs_file(tmp_path)
        corrupt = b"corrupt main {"
        jobs_file.write_bytes(corrupt)
        for backup in (tmp_path / "cron").glob("jobs.json.bak.*"):
            backup.write_bytes(b"corrupt too {")

        with pytest.raises(RuntimeError, match="corrupted and unrepairable"):
            jobs.load_jobs()
        # Without a valid replacement the corrupt file stays in place — a
        # rename here would make the next load see "no file" and silently
        # continue with an empty store.
        assert jobs_file.read_bytes() == corrupt
        assert not list((tmp_path / "cron").glob("jobs.json.corrupt-*"))

    def test_no_backup_at_all_keeps_fail_closed_runtimeerror(self, tmp_cron_dir, tmp_path):
        jobs_file = _jobs_file(tmp_path)
        jobs.ensure_dirs()
        jobs_file.write_bytes(b"{ broken")
        with pytest.raises(RuntimeError, match="corrupted and unrepairable"):
            jobs.load_jobs()
        assert jobs_file.read_bytes() == b"{ broken"

    def test_wrong_top_level_type_also_recovers(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "good")
        _jobs_file(tmp_path).write_text("12345", encoding="utf-8")
        loaded = jobs.load_jobs()
        assert [j["id"] for j in loaded] == ["job-good"]

    def test_wrong_top_level_type_without_backup_raises(self, tmp_cron_dir, tmp_path):
        jobs_file = _jobs_file(tmp_path)
        jobs.ensure_dirs()
        jobs_file.write_text("12345", encoding="utf-8")
        with pytest.raises(RuntimeError, match="expected \{'jobs'"):
            jobs.load_jobs()

    def test_recovered_store_accepts_subsequent_writes(self, tmp_cron_dir, tmp_path):
        _save(tmp_path, "good")
        _jobs_file(tmp_path).write_bytes(b"junk {")
        jobs.load_jobs()  # recovers
        _save(tmp_path, "after")
        assert [j["id"] for j in jobs.load_jobs()] == ["job-after"]

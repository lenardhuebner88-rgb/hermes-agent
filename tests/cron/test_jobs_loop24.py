"""Loop 24 (K3 review): load_jobs exists→open race, non-finite retry
backoff, and quarantine retention.

- L4: load_jobs checked exists() and then open()ed — a foreign quarantine
  rename landing exactly in between surfaced as an IOError-RuntimeError
  instead of the loop-19 re-check. FileNotFoundError in the open path now
  re-checks under _jobs_lock(): a restored file is loaded normally, a
  still-missing one yields an honest [].
- L5: _normalize_retry accepted float("inf") as a backoff entry (``inf > 0``
  is True) and json.dump then wrote bare ``Infinity`` into jobs.json.
  Non-finite entries are now discarded like any other invalid value.
- L6: every successful corruption recovery renames the store to
  jobs.json.corrupt-<ts> and never cleaned up. Aged quarantine files
  (> 7 days, mtime) are now pruned best-effort after a successful recovery.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import time

import pytest

from cron import jobs as cron_jobs


@pytest.fixture()
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with cron_jobs.use_cron_store(home):
        yield home


def _write_store(home, names):
    store = home / "cron" / "jobs.json"
    store.write_text(
        json.dumps({"jobs": [{"id": n, "name": n, "schedule": {"kind": "cron", "expr": "0 0 * * *"}} for n in names]}),
        encoding="utf-8",
    )
    return store


class TestLoadJobsExistsOpenRace:
    """(a) L4: FileNotFoundError between exists() and open() re-checks under
    the lock instead of raising an IOError-RuntimeError."""

    def test_rename_between_exists_and_open_gone_returns_empty(
        self, store_home, monkeypatch,
    ):
        store = _write_store(store_home, ["job-a"])
        quarantined = store.with_name("jobs.json.corrupt-simulated")
        real_open = builtins.open
        fired = {"done": False}

        def _racing_open(file, *args, **kwargs):
            if not fired["done"] and str(file) == str(store):
                fired["done"] = True
                store.rename(quarantined)  # foreign quarantine rename
                raise FileNotFoundError(2, "No such file or directory", str(store))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _racing_open)
        with cron_jobs.use_cron_store(store_home):
            assert cron_jobs.load_jobs() == []  # honest empty, no crash
        assert fired["done"]
        quarantined.rename(store)  # restore for cleanliness

    def test_rename_between_exists_and_open_restored_loads_normally(
        self, store_home, monkeypatch,
    ):
        """The recovery window closes before the re-check: the file is back
        and must be loaded normally (loop-19 semantics, one window later)."""
        store = _write_store(store_home, ["job-a"])
        quarantined = store.with_name("jobs.json.corrupt-simulated")
        real_open = builtins.open
        fired = {"done": False}

        def _racing_open(file, *args, **kwargs):
            if not fired["done"] and str(file) == str(store):
                fired["done"] = True
                store.rename(quarantined)
                quarantined.rename(store)  # concurrent recovery finished
                raise FileNotFoundError(2, "No such file or directory", str(store))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _racing_open)
        with cron_jobs.use_cron_store(store_home):
            assert [j["name"] for j in cron_jobs.load_jobs()] == ["job-a"]
        assert fired["done"]


class TestNormalizeRetryFinite:
    """(b) L5: non-finite backoff entries are discarded, never written as
    bare ``Infinity`` into the store."""

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_backoff_entries_are_dropped(self, bad):
        result = cron_jobs._normalize_retry(
            {"max_attempts": 2, "backoff_seconds": [bad]}
        )
        assert result == {"max_attempts": 2}

    def test_valid_entries_survive_non_finite_siblings(self):
        result = cron_jobs._normalize_retry(
            {"max_attempts": 2, "backoff_seconds": [300, float("inf"), 60.5, float("nan")]}
        )
        assert result["backoff_seconds"] == [300.0, 60.5]
        # The normalized policy is always JSON-safe.
        json.dumps(result, allow_nan=False)


class TestQuarantineRetention:
    """(c) L6: a successful recovery prunes quarantine files older than the
    retention window; fresh ones are kept."""

    def test_aged_quarantine_pruned_fresh_kept(self, store_home):
        store = _write_store(store_home, ["job-a"])
        # A valid recovery source (plausible create_job-shaped records).
        shutil.copy2(store, cron_jobs._jobs_backup_path(store, 1))
        # Pre-place an OLD and a FRESH quarantine file.
        old_q = store.with_name(f"{store.name}.corrupt-20000101-000000-000000")
        old_q.write_text("garbage", encoding="utf-8")
        aged = time.time() - (cron_jobs.QUARANTINE_RETENTION_SECONDS + 86400)
        os.utime(old_q, (aged, aged))
        fresh_q = store.with_name(f"{store.name}.corrupt-29990101-000000-000000")
        fresh_q.write_text("garbage", encoding="utf-8")
        # Corrupt the live store so load_jobs recovers from the backup.
        store.write_text("{not json", encoding="utf-8")

        with cron_jobs.use_cron_store(store_home):
            jobs = cron_jobs.load_jobs()

        assert [j["name"] for j in jobs] == ["job-a"]  # recovery succeeded
        assert not old_q.exists(), "aged quarantine must be pruned"
        assert fresh_q.exists(), "fresh quarantine must be kept"
        # The quarantine from THIS recovery is of course kept too.
        new_quarantines = [
            p for p in store.parent.glob(f"{store.name}.corrupt-*")
            if p != fresh_q
        ]
        assert len(new_quarantines) == 1

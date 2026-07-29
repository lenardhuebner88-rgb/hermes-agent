"""Loop 19: load_jobs missing-file race guard.

Pre-existing race found during loop 18: load_jobs checked jobs_file.exists()
BEFORE taking _jobs_lock(), so a loader entering the quarantine→restore window
of a sibling process (file temporarily renamed away) silently got [] — a wiped
store. The fix re-checks under the lock: a concurrent recovery finishes first,
and only then is "missing" authoritative.
"""

import json
import threading
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


def test_missing_store_without_contention_returns_empty(store_home):
    assert cron_jobs.load_jobs() == []


def test_loader_waits_for_concurrent_recovery_window(store_home):
    """Loader entering the quarantine window must not see a wiped store."""
    store = _write_store(store_home, ["job-a"])
    quarantined = store.with_suffix(".corrupt-simulated")
    store.rename(quarantined)
    assert not store.exists()

    results = {}
    started = threading.Event()

    def loader():
        started.set()
        with cron_jobs.use_cron_store(store_home):
            results["jobs"] = cron_jobs.load_jobs()

    # Hold the lock (as _recover_corrupt_store does), then "restore" the file.
    with cron_jobs._jobs_lock():
        t = threading.Thread(target=loader)
        t.start()
        assert started.wait(timeout=5)
        # Give the loader a moment to reach the (now blocking) lock.
        time.sleep(0.2)
        assert t.is_alive(), "loader must block on _jobs_lock, not return [] early"
        quarantined.rename(store)  # recovery finishes

    t.join(timeout=10)
    assert not t.is_alive()
    assert [j["name"] for j in results["jobs"]] == ["job-a"]


def test_missing_store_under_lock_is_authoritative(store_home):
    """Still missing under the lock → honest empty result."""
    with cron_jobs._jobs_lock():
        assert cron_jobs.load_jobs() == []

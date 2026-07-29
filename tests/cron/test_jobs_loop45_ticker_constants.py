"""Loop 5a (A-M1): TICKER_HEARTBEAT_FILE / TICKER_SUCCESS_FILE are live.

Previously record_ticker_heartbeat()/get_ticker_*_age() used inline string
literals and the module constants were dead — tests that monkeypatched the
constants tested nothing. Prod code now resolves marker paths through the
constants: a deliberately re-pointed constant is honored verbatim, otherwise
the path derives from the ACTIVE cron store (per-profile scoping, #69377)
using the constant's file name.
"""

import time

import pytest

import cron.jobs as jobs


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestRepointedConstantsAreHonored:
    """The pre-fix dead-monkeypatch repro: repointing ONLY the constants must
    redirect where the heartbeat markers are written and read."""

    def test_heartbeat_constant_redirects_writes(self, tmp_cron_dir, tmp_path, monkeypatch):
        custom = tmp_path / "elsewhere" / "my_heartbeat"
        custom.parent.mkdir(parents=True)
        monkeypatch.setattr(jobs, "TICKER_HEARTBEAT_FILE", custom)

        jobs.record_ticker_heartbeat()

        assert custom.is_file()
        assert float(custom.read_text(encoding="utf-8")) <= time.time()
        # NOT written to the store-derived default location.
        assert not (tmp_path / "cron" / "ticker_heartbeat").exists()

    def test_success_constant_redirects_writes(self, tmp_cron_dir, tmp_path, monkeypatch):
        custom = tmp_path / "elsewhere" / "my_success"
        custom.parent.mkdir(parents=True)
        monkeypatch.setattr(jobs, "TICKER_SUCCESS_FILE", custom)

        jobs.record_ticker_heartbeat(success=True)

        assert custom.is_file()

    def test_heartbeat_constant_redirects_reads(self, tmp_cron_dir, tmp_path, monkeypatch):
        custom = tmp_path / "elsewhere" / "my_heartbeat"
        custom.parent.mkdir(parents=True)
        custom.write_text(str(time.time() - 5_000), encoding="utf-8")
        monkeypatch.setattr(jobs, "TICKER_HEARTBEAT_FILE", custom)

        age = jobs.get_ticker_heartbeat_age()
        assert age is not None and age > 4_000

    def test_success_constant_redirects_reads(self, tmp_cron_dir, tmp_path, monkeypatch):
        custom = tmp_path / "elsewhere" / "my_success"
        custom.parent.mkdir(parents=True)
        custom.write_text(str(time.time() - 5_000), encoding="utf-8")
        monkeypatch.setattr(jobs, "TICKER_SUCCESS_FILE", custom)

        age = jobs.get_ticker_success_age()
        assert age is not None and age > 4_000


class TestDefaultDerivationFromStore:
    """Untouched constants keep deriving from the active cron store, using the
    constant's file NAME — the store switch (#69377) must keep working."""

    def test_roundtrip_through_store_dir(self, tmp_cron_dir, tmp_path):
        assert jobs.get_ticker_heartbeat_age() is None
        assert jobs.get_ticker_success_age() is None

        jobs.record_ticker_heartbeat(success=True)

        assert (tmp_path / "cron" / "ticker_heartbeat").is_file()
        assert (tmp_path / "cron" / "ticker_last_success").is_file()
        assert jobs.get_ticker_heartbeat_age() < 60
        assert jobs.get_ticker_success_age() < 60

    def test_store_switch_moves_markers(self, tmp_cron_dir, tmp_path):
        jobs.record_ticker_heartbeat()
        assert (tmp_path / "cron" / "ticker_heartbeat").is_file()

        other_home = tmp_path / "other_profile"
        with jobs.use_cron_store(other_home):
            # The other profile has no heartbeat yet — None, not the first
            # profile's fresh marker.
            assert jobs.get_ticker_heartbeat_age() is None
            jobs.record_ticker_heartbeat()
            assert (other_home / "cron" / "ticker_heartbeat").is_file()
            assert jobs.get_ticker_heartbeat_age() < 60

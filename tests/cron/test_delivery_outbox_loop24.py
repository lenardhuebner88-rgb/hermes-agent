"""Loop 24 (K3 review): stale-lease retake attempts, lease-holder liveness,
delivery-timeout persistence and lease-TTL clamp.

- M2: ``enqueue`` persists the resolved delivery timeout as
  ``delivery_timeout_seconds`` on the entry so the REPLAY uses the same
  bound the failed first attempt used (scheduler-side replay coverage lives
  in test_scheduler_loop24_delivery.py).
- L1: a stale-lease retake (holder crashed in the send window) counts as an
  attempt — a reproducible crash in the send window used to resend forever
  and never dead-letter. Past MAX_ATTEMPTS the entry goes ``dead`` instead
  of being leased again.
- L2: a stale lease TIMESTAMP alone no longer proves the holder gone —
  ``begin_replay`` only retakes when the lease PID is provably dead
  (fail-safe: inability to prove death reads as ALIVE, skip). And a
  delivery timeout at/above the lease TTL is clamped strictly below it with
  a warning — a send outrunning its lease looks orphaned and would be
  retaken by a second process (duplicate).

Hermetic: no network, no real platform sends.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import cron.delivery_outbox as outbox
from cron.jobs import use_cron_store


def _target(chat_id="123"):
    return {"platform": "telegram", "chat_id": chat_id, "thread_id": None}


def _dead_pid():
    """A PID that is provably not alive (spawned and immediately reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _rewrite_lease_pid(entry_id, pid):
    """Overwrite the persisted lease_pid of a sending entry in the store."""
    path = outbox._current_outbox_file()
    entries = outbox._read_entries(path)
    for stored in entries:
        if stored.get("id") == entry_id:
            stored["lease_pid"] = pid
    outbox._write_entries(path, entries)


class TestPersistedDeliveryTimeout:
    """(a) M2: the resolved delivery timeout is stored on the entry."""

    def test_enqueue_persists_delivery_timeout(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue(
                "job", _target(), "p", "err",
                execution_id="e1", delivery_timeout_seconds=300,
            )
            assert entry["delivery_timeout_seconds"] == 300
            stored = outbox.list_entries()[0]
            assert stored["delivery_timeout_seconds"] == 300

    @pytest.mark.parametrize("bad", ["bogus", 0, -5, float("inf"), float("nan"), {}])
    def test_invalid_timeout_stores_no_override(self, tmp_path, bad):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue(
                "job", _target(), "p", "err",
                execution_id="e1", delivery_timeout_seconds=bad,
            )
            assert "delivery_timeout_seconds" not in entry

    def test_refresh_updates_persisted_timeout(self, tmp_path):
        """A same-execution refresh carries the latest resolved timeout."""
        home = tmp_path / "home"
        with use_cron_store(home):
            outbox.enqueue(
                "job", _target(), "p", "err",
                execution_id="e1", delivery_timeout_seconds=100,
            )
            refreshed = outbox.enqueue(
                "job", _target(), "p2", "err",
                execution_id="e1", delivery_timeout_seconds=200,
            )
            entries = outbox.list_entries()
            assert len(entries) == 1  # same execution: refresh, no duplicate
            assert refreshed["delivery_timeout_seconds"] == 200


class TestStaleRetakeCountsAttempts:
    """(b) L1: a crash in the send window IS an attempt; repeated crashes
    dead-letter instead of resending forever."""

    def test_repeated_crash_windows_dead_letter(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        base = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        clock = {"now": base}
        monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")
        dead = _dead_pid()

        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            # MAX_ATTEMPTS crash windows: first lease from queued, then
            # stale-lease retakes — each retake counts the lost send.
            for cycle in range(outbox.MAX_ATTEMPTS):
                leased = outbox.begin_replay(entry["id"])
                assert leased is not None, f"lease for cycle {cycle + 1} must be granted"
                # Simulate the crash: the process dies between begin_replay
                # and record_replay_result — the lease stays in `sending`,
                # its holder pid is dead, and the clock moves past the TTL.
                _rewrite_lease_pid(entry["id"], dead)
                clock["now"] += timedelta(seconds=120)

            # The next retake would be attempt MAX_ATTEMPTS+1 — instead the
            # entry is dead: no further send, and not due anymore.
            assert outbox.begin_replay(entry["id"]) is None
            stored = outbox.list_entries()[0]
            assert stored["status"] == "dead"
            assert stored["attempts"] == outbox.MAX_ATTEMPTS
            assert "crashed" in (stored["last_error"] or "")
            # Lease fields are cleared on the terminal transition.
            for field in ("lease_ts", "lease_pid", "send_attempt"):
                assert field not in stored
            assert outbox.due_entries() == []
            assert outbox.outbox_counts()["dead"] == 1

    def test_single_retake_increments_attempts(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        base = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        clock = {"now": base}
        monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")

        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            assert outbox.begin_replay(entry["id"]) is not None
            _rewrite_lease_pid(entry["id"], _dead_pid())
            clock["now"] += timedelta(seconds=120)

            leased = outbox.begin_replay(entry["id"])
            assert leased is not None
            assert leased["attempts"] == 1  # the crashed send counted
            assert leased["send_attempt"] == 2


class TestLeaseLiveness:
    """(c) L2: a stale lease is only retaken against a provably DEAD holder;
    the check is fail-safe toward ALIVE (never double-send)."""

    def _stale_sending_entry(self, home, monkeypatch):
        base = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        clock = {"now": base}
        monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")
        entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
        assert outbox.begin_replay(entry["id"]) is not None
        clock["now"] += timedelta(seconds=120)  # lease stale by timestamp
        return entry

    def test_live_holder_pid_blocks_retake_despite_stale_ts(
        self, tmp_path, monkeypatch, caplog,
    ):
        """A slow but LIVING sender (send window > lease TTL) must not be
        double-sent: stale ts + live pid (our own) ⇒ skip."""
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = self._stale_sending_entry(home, monkeypatch)
            # lease_pid is THIS process — alive.
            assert outbox.list_entries()[0]["lease_pid"] == os.getpid()
            with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
                assert outbox.begin_replay(entry["id"]) is None
            assert any("still alive" in r.message for r in caplog.records)
            stored = outbox.list_entries()[0]
            assert stored["status"] == "sending"
            assert stored["attempts"] == 0  # no retake, no attempt counted

    def test_dead_holder_pid_allows_retake(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = self._stale_sending_entry(home, monkeypatch)
            _rewrite_lease_pid(entry["id"], _dead_pid())
            leased = outbox.begin_replay(entry["id"])
            assert leased is not None
            assert leased["status"] == "sending"
            assert leased["lease_pid"] == os.getpid()  # re-stamped

    def test_unusable_liveness_check_is_fail_safe_alive(self, tmp_path, monkeypatch):
        """Inability to prove death must NOT retake (same fail-safe direction
        as cron/executions.py::_owner_is_live)."""
        home = tmp_path / "home"

        def _exploding_pid_exists(pid):
            raise RuntimeError("psutil exploded")

        monkeypatch.setattr("gateway.status._pid_exists", _exploding_pid_exists)
        with use_cron_store(home):
            entry = self._stale_sending_entry(home, monkeypatch)
            assert outbox.begin_replay(entry["id"]) is None
            assert outbox.list_entries()[0]["status"] == "sending"

    def test_unparseable_lease_pid_allows_retake(self, tmp_path, monkeypatch):
        """A hand-written/corrupt lease row without a usable pid has no
        holder to protect — the stale retake proceeds."""
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = self._stale_sending_entry(home, monkeypatch)
            _rewrite_lease_pid(entry["id"], "not-a-pid")
            assert outbox.begin_replay(entry["id"]) is not None


class TestTimeoutLeaseClamp:
    """(d) L2: a delivery timeout at/above the lease TTL is clamped strictly
    below it with a warning; a smaller timeout passes through silently."""

    def test_timeout_above_lease_ttl_is_clamped_with_warning(
        self, tmp_path, monkeypatch, caplog,
    ):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")
        home = tmp_path / "home"
        with use_cron_store(home):
            with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
                entry = outbox.enqueue(
                    "job", _target(), "p", "err",
                    execution_id="e1", delivery_timeout_seconds=300,
                )
            assert entry["delivery_timeout_seconds"] == 59  # strictly < TTL
            assert any(
                "clamping the delivery timeout" in r.message
                for r in caplog.records
            )

    def test_timeout_below_lease_ttl_passes_through(
        self, tmp_path, monkeypatch, caplog,
    ):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")
        home = tmp_path / "home"
        with use_cron_store(home):
            with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
                entry = outbox.enqueue(
                    "job", _target(), "p", "err",
                    execution_id="e1", delivery_timeout_seconds=30,
                )
            assert entry["delivery_timeout_seconds"] == 30
            assert caplog.records == []

    def test_clamp_helper_directly(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "600")
        assert outbox.clamp_delivery_timeout_to_lease(300) == 300
        assert outbox.clamp_delivery_timeout_to_lease(600) == 599.0
        assert outbox.clamp_delivery_timeout_to_lease(3600) == 599.0

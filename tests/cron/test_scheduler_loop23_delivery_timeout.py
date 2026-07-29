"""Audit loop 2 (A-H1): standalone cron delivery must be hard-bounded.

Before the fix, the standalone delivery path in ``_deliver_result`` ran the
async platform send as an unbounded ``asyncio.run(coro)``. A wedged platform
send blocked the cron worker thread forever; the job stayed pinned in
``_running_job_ids`` and every later tick skipped it with
"already running — skipping".

The fix wraps the send in ``asyncio.wait_for(coro, timeout)`` — default 60s,
``HERMES_CRON_DELIVERY_TIMEOUT`` env override, per-job override via the job
record's ``delivery_timeout_seconds`` field. On timeout the coroutine is
cancelled, the failure is recorded as a normal delivery error (lands in
``last_delivery_error``), and the worker returns so the job's finally block
releases the running guard.

Hermetic: no network — the platform send is replaced by a coroutine that
never returns.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import cron.scheduler as s
from cron.scheduler import _deliver_result, _get_delivery_timeout


def _telegram_gateway_cfg():
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    mock_cfg = MagicMock()
    mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
    return mock_cfg


def _origin_job(**extra):
    job = {
        "id": "delivery-timeout-job",
        "name": "wedged-report",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }
    job.update(extra)
    return job


class TestGetDeliveryTimeout:
    """Precedence: per-job field > module override > env > default (60s)."""

    def test_default_is_60(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)
        assert _get_delivery_timeout() == 60

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_DELIVERY_TIMEOUT", "15")
        assert _get_delivery_timeout() == 15

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_DELIVERY_TIMEOUT", "not-a-number")
        assert _get_delivery_timeout() == 60

    def test_per_job_override_beats_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_DELIVERY_TIMEOUT", "15")
        assert _get_delivery_timeout({"id": "j", "delivery_timeout_seconds": 5}) == 5

    @pytest.mark.parametrize("bad", ["bogus", 0, -5, {}])
    def test_invalid_per_job_value_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("HERMES_CRON_DELIVERY_TIMEOUT", "15")
        assert _get_delivery_timeout({"id": "j", "delivery_timeout_seconds": bad}) == 15

    def test_module_override(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", 7)
        assert _get_delivery_timeout() == 7


class TestWedgedStandaloneSend:
    """A platform send that never returns must be cancelled after the timeout
    and recorded as a delivery error — not hang the worker forever."""

    def test_wedged_send_times_out_and_is_cancelled(self, monkeypatch):
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", 1)
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)
        cancelled = threading.Event()

        async def _wedged(*args, **kwargs):
            try:
                await asyncio.Event().wait()  # never returns
            except asyncio.CancelledError:
                cancelled.set()
                raise

        started = time.monotonic()
        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_wedged):
            error = _deliver_result(_origin_job(), "report body")
        elapsed = time.monotonic() - started

        assert error is not None, "wedged send must surface as a delivery error"
        assert "timed out" in error
        assert "telegram:123" in error
        assert cancelled.is_set(), "timeout must cancel the wedged coroutine"
        # Bounded by the 1s override — nowhere near the old unbounded hang or
        # the 30s thread-pool retry budget.
        assert elapsed < 20, f"delivery took {elapsed:.1f}s — timeout did not bound the send"

    def test_per_job_timeout_field_bounds_the_send(self, monkeypatch):
        """delivery_timeout_seconds on the job record applies without env/module
        overrides (uses the field's own 1s bound)."""
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)

        async def _wedged(*args, **kwargs):
            await asyncio.Event().wait()  # never returns

        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_wedged):
            error = _deliver_result(
                _origin_job(delivery_timeout_seconds=1), "report body",
            )

        assert error is not None and "timed out" in error

    def test_healthy_send_within_timeout_still_delivers(self, monkeypatch):
        """A normal fast send is unaffected by the new bound."""
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", 5)

        async def _ok(*args, **kwargs):
            return {"success": True}

        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_ok):
            error = _deliver_result(_origin_job(), "report body")

        assert error is None, f"healthy send should deliver, got: {error!r}"


class TestTickReleasesRunningGuard:
    """End-to-end through tick(): after a wedged delivery times out, the job
    must leave ``_running_job_ids`` (so the next tick is not skipped with
    "already running") and the delivery failure must reach mark_job_run's
    ``delivery_error`` (i.e. ``last_delivery_error``)."""

    def test_wedged_delivery_releases_guard_and_records_error(self, monkeypatch):
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", 1)
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)

        run_job_calls = []
        marks = []
        job = _origin_job()

        def fake_run_job(j, *, defer_agent_teardown=None):
            run_job_calls.append(j["id"])
            return (True, "out", "final response", None)

        async def _wedged(*args, **kwargs):
            await asyncio.Event().wait()  # never returns

        monkeypatch.setattr(s, "run_job", fake_run_job)
        monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
        monkeypatch.setattr(
            s, "mark_job_run",
            lambda jid, ok, err=None, delivery_error=None: marks.append(
                {"id": jid, "ok": ok, "delivery_error": delivery_error}
            ),
        )
        monkeypatch.setattr(s, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(s, "advance_next_run", lambda jid: True)
        monkeypatch.setattr(s, "_interrupted_job_ids", set())
        monkeypatch.setattr(s, "_running_job_ids", set())

        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_wedged):
            first = s.tick(verbose=False, sync=True)

        assert first == 1
        assert run_job_calls == ["delivery-timeout-job"]
        # The running guard was released even though the send wedged.
        assert "delivery-timeout-job" not in s._running_job_ids
        # The timeout was recorded with the same semantics as any delivery
        # failure (mark_job_run's delivery_error -> last_delivery_error).
        assert marks and marks[0]["delivery_error"] is not None
        assert "timed out" in marks[0]["delivery_error"]

        # A follow-up tick must run the job again — not skip it as
        # "already running".
        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_wedged):
            second = s.tick(verbose=False, sync=True)

        assert second == 1
        assert run_job_calls == ["delivery-timeout-job", "delivery-timeout-job"]

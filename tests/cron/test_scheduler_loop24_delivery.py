"""Loop 24 (K3 review): fallback-send timeout, persisted replay timeout,
and the replay-tick exception guard.

- M1: the RuntimeError thread-pool fallback in ``_deliver_result`` ran the
  send UNBOUNDED (raw coroutine, hardcoded 30s pool budget, the resolved
  delivery timeout ignored) — a wedged send kept running in the pool thread
  and its LATE send landed after the failure path had already parked the
  payload in the outbox: the replay then delivered a guaranteed duplicate.
  The fallback coroutine now gets the same resolved delivery-timeout bound
  via ``asyncio.wait_for`` and is cancelled on timeout.
- M2: ``_send_outbox_entry`` re-resolved the delivery timeout from
  env/default, so a per-job ``delivery_timeout_seconds`` only applied to
  the FIRST attempt — a job with a legitimate 300s bound could dead-letter
  on replay under a smaller default. The resolved timeout is now persisted
  on the outbox entry at enqueue and the replay uses it.
- L3: ``_replay_delivery_outbox`` promises "never raises", but a SYNCHRONOUS
  ``_send_outbox_entry`` failure escaped the pass and broke the tick before
  every remaining entry (and all due jobs). The send call is now guarded;
  the failure is recorded as a normal failed attempt.

Hermetic: no network — platform sends are replaced by stub coroutines.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import cron.delivery_outbox as outbox
import cron.scheduler as s
from cron.jobs import use_cron_store
from cron.scheduler import _deliver_result


def _telegram_gateway_cfg():
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    mock_cfg = MagicMock()
    mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
    return mock_cfg


def _origin_job(**extra):
    job = {
        "id": "loop24-job",
        "name": "loop24-report",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }
    job.update(extra)
    return job


def _target(chat_id="123"):
    return {"platform": "telegram", "chat_id": chat_id, "thread_id": None}


def _ok_send(calls, result=None):
    async def _send(platform, pconfig, chat_id, content, **kwargs):
        calls.append({"chat_id": chat_id, "content": content, "kwargs": kwargs})
        return result if result is not None else {"success": True}

    return _send


class TestFallbackBoundedSend:
    """(a) M1: the thread-pool fallback must bound the send with the resolved
    delivery timeout and cancel it — no late send after the error path."""

    def test_wedged_fallback_send_is_cancelled_no_late_send(self, monkeypatch):
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", 1)
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)
        cancelled = threading.Event()
        completed = threading.Event()

        async def _wedged(*args, **kwargs):
            try:
                await asyncio.Event().wait()  # never returns
            except asyncio.CancelledError:
                cancelled.set()
                raise
            completed.set()  # a late send would land here
            return {"success": True}

        outcome = {}

        def _call_from_running_loop():
            # asyncio.run() inside a running event loop raises RuntimeError →
            # deterministically enters the thread-pool fallback under test.
            loop = asyncio.new_event_loop()
            try:
                async def _drive():
                    outcome["error"] = _deliver_result(_origin_job(), "report body")

                loop.run_until_complete(_drive())
            finally:
                loop.close()

        started = time.monotonic()
        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_wedged):
            worker = threading.Thread(target=_call_from_running_loop)
            worker.start()
            worker.join(timeout=20)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "fallback send must be bounded, not hang"
        assert cancelled.is_set(), "timeout must cancel the fallback coroutine"
        assert not completed.is_set(), "no late send after the timeout"
        assert elapsed < 15, f"fallback took {elapsed:.1f}s — timeout did not bound the send"
        assert outcome.get("error") is not None
        assert "telegram:123" in outcome["error"]

    def test_healthy_fallback_send_still_delivers(self, monkeypatch):
        """The fallback path itself is unchanged for a fast healthy send."""
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", 5)

        async def _ok(*args, **kwargs):
            return {"success": True}

        outcome = {}

        def _call_from_running_loop():
            loop = asyncio.new_event_loop()
            try:
                async def _drive():
                    outcome["error"] = _deliver_result(_origin_job(), "report body")

                loop.run_until_complete(_drive())
            finally:
                loop.close()

        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=_ok):
            worker = threading.Thread(target=_call_from_running_loop)
            worker.start()
            worker.join(timeout=20)

        assert not worker.is_alive()
        assert outcome.get("error") is None, f"healthy fallback should deliver, got: {outcome!r}"


class TestReplayUsesPersistedTimeout:
    """(b) M2: the replay send runs under the timeout persisted on the entry,
    not a re-resolved env/default."""

    def test_replay_uses_entry_delivery_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_DELIVERY_TIMEOUT", "15")
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", None)
        home = tmp_path / "home"
        sends = []
        seen = {}
        real_wait_for = asyncio.wait_for

        def _spy_wait_for(coro, timeout):
            seen["timeout"] = timeout
            return real_wait_for(coro, timeout)

        with use_cron_store(home):
            outbox.enqueue(
                "job", _target(), "p", "err",
                execution_id="exec-1", delivery_timeout_seconds=300,
            )
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)), \
                 monkeypatch.context() as m:
                m.setattr(asyncio, "wait_for", _spy_wait_for)
                assert s._replay_delivery_outbox() == 1

        # The persisted 300s bound won over the env default of 15s.
        assert seen["timeout"] == 300
        assert len(sends) == 1

    def test_replay_without_entry_timeout_falls_back_to_env(self, tmp_path, monkeypatch):
        """Legacy entries without the field keep the old env/default
        resolution — backwards compatible."""
        monkeypatch.setenv("HERMES_CRON_DELIVERY_TIMEOUT", "15")
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", None)
        home = tmp_path / "home"
        sends = []
        seen = {}
        real_wait_for = asyncio.wait_for

        def _spy_wait_for(coro, timeout):
            seen["timeout"] = timeout
            return real_wait_for(coro, timeout)

        with use_cron_store(home):
            outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)), \
                 monkeypatch.context() as m:
                m.setattr(asyncio, "wait_for", _spy_wait_for)
                assert s._replay_delivery_outbox() == 1

        assert seen["timeout"] == 15

    def test_failed_first_attempt_persists_resolved_timeout(self, tmp_path, monkeypatch):
        """End to end: a job with delivery_timeout_seconds=300 whose send
        fails on every path enqueues an entry carrying 300."""
        monkeypatch.delenv("HERMES_CRON_DELIVERY_TIMEOUT", raising=False)
        monkeypatch.setattr(s, "_DELIVERY_TIMEOUT", None)
        home = tmp_path / "home"

        async def _down(*args, **kwargs):
            return {"error": "platform down"}

        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_down):
                error = _deliver_result(
                    _origin_job(execution_id="exec-1", delivery_timeout_seconds=300),
                    "report body",
                )
            assert error is not None and "platform down" in error
            entry = outbox.list_entries()[0]
            assert entry["delivery_timeout_seconds"] == 300


class TestReplaySendExceptionGuard:
    """(c) L3: a synchronous _send_outbox_entry failure must not break the
    replay pass — it is recorded as a failed attempt and the pass continues."""

    def test_sync_send_failure_records_attempt_and_continues(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        with use_cron_store(home):
            first = outbox.enqueue("job", _target("1"), "p1", "err", execution_id="e1")
            second = outbox.enqueue("job", _target("2"), "p2", "err", execution_id="e2")

            def _exploding_send(entry):
                if entry["id"] == first["id"]:
                    raise OSError("synchronous transport explosion")
                return None, "platform-message:m-2"

            monkeypatch.setattr(s, "_send_outbox_entry", _exploding_send)
            # Never raises — and the SECOND entry still gets its replay.
            assert s._replay_delivery_outbox() == 1

            by_id = {e["id"]: e for e in outbox.list_entries()}
            failed = by_id[first["id"]]
            assert failed["status"] == "queued"  # not delivered, not dead yet
            assert failed["attempts"] == 1
            assert "replay send raised" in (failed["last_error"] or "")
            assert "synchronous transport explosion" in failed["last_error"]
            assert by_id[second["id"]]["status"] == "delivered"

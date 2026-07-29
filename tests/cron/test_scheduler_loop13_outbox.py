"""Audit loop 13: outbox idempotency, cross-process locking, provider-
independent replay, and error classes.

Fixes on top of the loop-6 persistent delivery outbox:

- F1: entries are keyed per execution (execution_id + job_id + target), so
  two failed RUNS of the same job/target no longer overwrite each other; a
  refresh of the SAME run still dedupes. Read-modify-write transitions run
  under a bounded cross-process flock (``<outbox>.lock``, same pattern as
  cron/jobs.py's ``.jobs.lock``) so parallel multiplex profiles cannot
  torn-write or lose each other's attempts.
- F2: the replay also fires from ``run_one_job`` — the shared firing body
  that Chronos ``fire_due`` runs — not only from the built-in tick. The
  non-blocking ``replay_lock`` keeps the two triggers from double-sending.
- F3: relay and config failures (unknown platform, platform not
  configured/enabled) are parked as visible dead letters with
  ``error_class`` config/relay — no retries, no silent loss, no unbounded
  dead-letter pile for a permanently misconfigured job.

Hermetic: no network — platform sends are replaced by stub coroutines, and
cross-process tests use a tmp-profile store via ProcessPoolExecutor.
"""

import asyncio
import json
import threading
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
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
        "id": "loop13-job",
        "name": "loop13-report",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }
    job.update(extra)
    return job


def _target(chat_id="123"):
    return {"platform": "telegram", "chat_id": chat_id, "thread_id": None}


def _ok_send(calls):
    async def _send(platform, pconfig, chat_id, content, **kwargs):
        calls.append({"chat_id": chat_id, "content": content, "kwargs": kwargs})
        return {"success": True}

    return _send


def _failing_send(calls):
    async def _send(*args, **kwargs):
        calls.append((args, kwargs))
        return {"error": "platform down"}

    return _send


# ── module-level workers for the cross-process tests (picklable) ──────────


def _proc_enqueue(home, prefix, count):
    from cron.jobs import use_cron_store as _ucs

    import cron.delivery_outbox as _ob

    with _ucs(home):
        for i in range(count):
            _ob.enqueue(
                "proc-job",
                {"platform": "telegram", "chat_id": "1", "thread_id": None},
                f"{prefix}-{i}",
                "err",
                execution_id=f"{prefix}-exec-{i}",
            )
    return count


def _proc_bump(home, entry_id, count):
    from cron.jobs import use_cron_store as _ucs

    import cron.delivery_outbox as _ob

    with _ucs(home):
        for _ in range(count):
            _ob.record_replay_result(entry_id, success=False, error="x")
    return True


def _patch_pipeline(monkeypatch):
    """Patch the run_one_job job pipeline (mirrors tests/cron/test_run_one_job.py)."""

    def fake_run_job(job, *, defer_agent_teardown=None):
        return (True, "out", "final response", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda job, content, adapters=None, loop=None: None)
    monkeypatch.setattr(s, "mark_job_run", lambda jid, ok, err=None, delivery_error=None: None)


class TestPerExecutionIdempotency:
    """(a) Two different executions of the same job/target fail before the
    replay ⇒ TWO entries, both replayed, neither overwritten."""

    def test_distinct_executions_never_collapse(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            e1 = outbox.enqueue("job", _target(), "report run 1", "err", execution_id="exec-1")
            e2 = outbox.enqueue("job", _target(), "report run 2", "err", execution_id="exec-2")

            entries = outbox.list_entries()
            assert len(entries) == 2
            assert e1["id"] != e2["id"]
            payloads = {e["payload"] for e in entries}
            assert payloads == {"report run 1", "report run 2"}
            assert {e["execution_id"] for e in entries} == {"exec-1", "exec-2"}

            # Both are replayed — no report is lost.
            sends = []
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 2
            assert {c["content"] for c in sends} == {"report run 1", "report run 2"}
            assert all(e["status"] == "delivered" for e in outbox.list_entries())

    def test_same_execution_refreshes_without_duplicate(self, tmp_path):
        """(b) The SAME execution failing again refreshes its entry — no
        duplicate, and the pending next_retry_at is not postponed."""
        home = tmp_path / "home"
        clock = {"now": datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)}

        with use_cron_store(home):
            first = outbox.enqueue("job", _target(), "v1", "err", execution_id="exec-1")
            # Simulate a scheduled future retry, then a refresh of the same run.
            with patch.object(outbox, "_hermes_now", lambda: clock["now"]):
                outbox.record_replay_result(first["id"], success=False, error="x")
                scheduled = outbox.list_entries()[0]["next_retry_at"]

                clock["now"] += timedelta(seconds=60)
                second = outbox.enqueue("job", _target(), "v2", "err2", execution_id="exec-1")

            entries = outbox.list_entries()
            assert len(entries) == 1
            assert second["id"] == first["id"]
            assert entries[0]["payload"] == "v2"
            assert entries[0]["last_error"] == "err2"
            # Refresh did not postpone the already scheduled retry.
            assert entries[0]["next_retry_at"] == scheduled
            # And the retry bookkeeping survived the refresh.
            assert entries[0]["attempts"] == 1

    def test_legacy_caller_without_execution_id_keeps_job_target_dedupe(self, tmp_path):
        """Backward compat: without an execution_id the loop-6 (job, target)
        refresh semantics are unchanged."""
        home = tmp_path / "home"
        with use_cron_store(home):
            first = outbox.enqueue("job", _target(), "v1", "err")
            second = outbox.enqueue("job", _target(), "v2", "err")
            entries = outbox.list_entries()
            assert len(entries) == 1
            assert second["id"] == first["id"]
            assert entries[0]["payload"] == "v2"


class TestCrossProcessLocking:
    """(c) Parallel read-modify-write from two processes ⇒ no torn writes,
    no lost entries, attempts stay consistent (flock around the RMW)."""

    def test_parallel_enqueues_from_two_processes_all_survive(self, tmp_path):
        home = tmp_path / "home"
        per_process = 20
        with ProcessPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_proc_enqueue, str(home), "alpha", per_process)
            f2 = pool.submit(_proc_enqueue, str(home), "beta", per_process)
            assert f1.result() == per_process
            assert f2.result() == per_process

        outbox_file = home / "cron" / "outbox.jsonl"
        lines = [l for l in outbox_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        # No torn writes: every line is a complete JSON object.
        entries = [json.loads(l) for l in lines]
        assert len(entries) == 2 * per_process
        assert len({e["id"] for e in entries}) == 2 * per_process
        assert len({e["execution_id"] for e in entries}) == 2 * per_process

    def test_parallel_transitions_keep_attempts_consistent(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")

        with ProcessPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_proc_bump, str(home), entry["id"], 4)
            f2 = pool.submit(_proc_bump, str(home), entry["id"], 4)
            assert f1.result() and f2.result()

        with use_cron_store(home):
            final = outbox.list_entries()[0]
        # 8 increments across two processes, capped dead at MAX_ATTEMPTS — a
        # lost update would leave attempts < MAX_ATTEMPTS and status queued.
        assert final["attempts"] == outbox.MAX_ATTEMPTS
        assert final["status"] == "dead"

    def test_parallel_transitions_from_threads_are_serialized(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")

            barrier = threading.Barrier(2)

            def _bump():
                # ContextVars do not propagate into new threads — enter the
                # store scope explicitly (production passes copy_context()).
                with use_cron_store(home):
                    barrier.wait()
                    for _ in range(4):
                        outbox.record_replay_result(entry["id"], success=False, error="x")

            threads = [threading.Thread(target=_bump) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            final = outbox.list_entries()[0]
            assert final["attempts"] == outbox.MAX_ATTEMPTS
            assert final["status"] == "dead"


class TestProviderIndependentReplay:
    """(d) The replay fires from run_one_job — the shared body Chronos
    fire_due runs — exactly once, never double-sending against the tick hook."""

    def test_run_one_job_triggers_replay_exactly_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            s, "_replay_delivery_outbox", lambda *a, **k: calls.append(1) or 0
        )
        _patch_pipeline(monkeypatch)

        # Direct run_one_job call == what CronScheduler.fire_due (Chronos)
        # does after claiming the job.
        assert s.run_one_job({"id": "loop13-hook", "name": "t"}) is True
        assert len(calls) == 1

    def test_run_one_job_replays_real_due_entry(self, tmp_path, monkeypatch):
        """End-to-end through the provider-independent path: a due entry is
        delivered when run_one_job fires — no tick involved."""
        home = tmp_path / "home"
        sends = []
        _patch_pipeline(monkeypatch)

        with use_cron_store(home):
            outbox.enqueue("job", _target(), "parked report", "err", execution_id="exec-9")
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s.run_one_job({"id": "loop13-e2e", "name": "t"}) is True

            assert len(sends) == 1
            assert sends[0]["content"] == "parked report"
            assert outbox.list_entries()[0]["status"] == "delivered"

    def test_concurrent_replay_passes_deliver_exactly_once(self, tmp_path):
        """Two replay triggers firing concurrently (tick hook + run_one_job
        hook, two processes/threads) ⇒ the entry is sent exactly once."""
        home = tmp_path / "home"
        sends = []
        started = threading.Barrier(3)

        async def _slow_ok_send(platform, pconfig, chat_id, content, **kwargs):
            sends.append(chat_id)
            # Hold the replay gate long enough for the second pass to arrive.
            await asyncio.sleep(0.4)
            return {"success": True}

        results = []

        def _replay():
            # ContextVars do not propagate into new threads — enter the store
            # scope explicitly (production passes copy_context()).
            with use_cron_store(home):
                started.wait()
                results.append(s._replay_delivery_outbox())

        with use_cron_store(home):
            outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_slow_ok_send):
                threads = [threading.Thread(target=_replay) for _ in range(2)]
                for t in threads:
                    t.start()
                started.wait()  # fire both at the same moment
                for t in threads:
                    t.join()

        # One pass won the replay gate and delivered; the other skipped.
        assert len(sends) == 1
        assert sorted(results) == [0, 1]
        with use_cron_store(home):
            assert outbox.list_entries()[0]["status"] == "delivered"


class TestErrorClasses:
    """(e) Config/relay failures park as visible dead letters with a clear
    error_class — no retries, no silent loss, no endless queue."""

    def test_unknown_platform_parks_config_dead_letter(self, tmp_path):
        home = tmp_path / "home"
        job = _origin_job(
            origin={"platform": "no_such_platform_xyz", "chat_id": "1"},
            execution_id="exec-cfg-1",
        )
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()):
                error = _deliver_result(job, "config report v1")

            assert error is not None and "unknown platform" in error
            entries = outbox.list_entries()
            assert len(entries) == 1
            entry = entries[0]
            assert entry["status"] == "dead"
            assert entry["error_class"] == "config"
            assert entry["attempts"] == 0
            assert "unknown platform" in (entry["last_error"] or "")

            # No retry: never due, a replay pass sends nothing.
            assert outbox.due_entries() == []
            sends = []
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 0
            assert sends == []

            # A permanently misconfigured job does NOT pile up dead letters:
            # the next run's failure refreshes the same visible entry — even
            # from a different execution.
            job2 = _origin_job(
                origin={"platform": "no_such_platform_xyz", "chat_id": "1"},
                execution_id="exec-cfg-2",
            )
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()):
                _deliver_result(job2, "config report v2")
            entries = outbox.list_entries()
            assert len(entries) == 1
            assert entries[0]["id"] == entry["id"]
            assert "config report v2" in entries[0]["payload"]
            # Still visible to `hermes cron status`.
            assert outbox.outbox_counts()["dead"] == 1

    def test_disabled_platform_parks_config_dead_letter(self, tmp_path):
        home = tmp_path / "home"
        cfg = _telegram_gateway_cfg()
        from gateway.config import Platform

        cfg.platforms[Platform.TELEGRAM].enabled = False
        job = _origin_job(execution_id="exec-cfg-3")
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=cfg):
                error = _deliver_result(job, "disabled report")
            assert error is not None and "not configured/enabled" in error
            entries = outbox.list_entries()
            assert len(entries) == 1
            assert entries[0]["status"] == "dead"
            assert entries[0]["error_class"] == "config"
            assert entries[0]["attempts"] == 0

    def test_relay_failure_parks_relay_dead_letter(self, tmp_path):
        """Relay targets fail closed (the relay owns the credential) — the
        payload is now parked as a visible relay-class dead letter instead of
        being silently lost; no replay is ever attempted for it."""
        home = tmp_path / "home"
        pconfig = MagicMock()
        pconfig.enabled = True
        pconfig.extra = {}
        transport = MagicMock(is_relay=True, config=pconfig, adapter=None)

        job = _origin_job(execution_id="exec-relay-1")
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("gateway.delivery.resolve_delivery_transport", return_value=transport):
                error = _deliver_result(job, "relay report")

            assert error is not None and "relay delivery" in error
            entries = outbox.list_entries()
            assert len(entries) == 1
            entry = entries[0]
            assert entry["status"] == "dead"
            assert entry["error_class"] == "relay"
            assert entry["attempts"] == 0
            assert "relay delivery" in (entry["last_error"] or "")

            # No retry: relay replays could never authenticate standalone.
            assert outbox.due_entries() == []
            sends = []
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 0
            assert sends == []

    def test_send_class_still_retries_with_backoff(self, tmp_path):
        """Regression guard: ordinary transport failures keep the loop-6
        retriable behaviour (queued, error_class=send)."""
        home = tmp_path / "home"
        job = _origin_job(execution_id="exec-send-1")
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_failing_send([])):
                error = _deliver_result(job, "send report")
            assert error is not None and "platform down" in error
            entries = outbox.list_entries()
            assert len(entries) == 1
            assert entries[0]["status"] == "queued"
            assert entries[0]["error_class"] == "send"
            assert entries[0]["execution_id"] == "exec-send-1"
            assert len(outbox.due_entries()) == 1

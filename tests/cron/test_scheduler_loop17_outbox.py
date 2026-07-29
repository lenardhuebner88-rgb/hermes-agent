"""Audit loop 17: execution-id wiring, append-only dead letters, crash-safe
replay lease + receipt, and config-load enqueue.

Fixes on top of the loop-6/loop-13 delivery outbox (Codex review #3):

- F1: ``run_one_job`` created an execution id for direct/tool runs but never
  wrote it into the job dict, so the outbox received ``execution_id=None``
  and several failed runs of the same job collapsed onto one legacy entry.
  The id is now wired into the dict handed to ``_deliver_result``; a
  send-class enqueue WITHOUT an execution_id (legacy caller) is marked with
  a warning but keeps its loop-6 semantics.
- F2: config/relay dead letters deduped on (job_id, target, class) across
  executions, so a newer run's failure overwrote an older run's payload.
  Dead letters are now append-only per execution; the anti-pile-up guard is
  a documented cap (MAX_DEAD_PER_TARGET=3, oldest replaced).
- F3: the send → record_replay_result crash window could double-deliver
  silently. A replay now persists status ``sending`` (lease_ts + attempt)
  BEFORE the send and ``delivered`` with a receipt right after; a stale
  lease (default 10min, HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS) is orphaned
  and retried, a fresh lease of a live process is skipped.
- F4: a load_gateway_config() failure returned before the outbox enqueue —
  it now parks a config-class dead letter per target.

Hermetic: no network — platform sends are replaced by stub coroutines.
"""

import logging
import os
from datetime import timedelta
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
        "id": "loop17-job",
        "name": "loop17-report",
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


def _failing_send(calls):
    async def _send(*args, **kwargs):
        calls.append((args, kwargs))
        return {"error": "platform down"}

    return _send


def _patch_run_pipeline(monkeypatch, final="report body"):
    """Patch the run_one_job job pipeline but keep the REAL _deliver_result
    (the delivery/outbox interaction is what this loop tests)."""

    def fake_run_job(job, *, defer_agent_teardown=None):
        return (True, "out", final, None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "mark_job_run", lambda jid, ok, err=None, delivery_error=None: None)


class TestExecutionIdWiring:
    """(a) F1: two direct run_one_job runs (no execution_id on the incoming
    job dict — the direct/tool path) with a delivery failure ⇒ TWO outbox
    entries keyed by the created execution ids, no collapse."""

    def test_two_direct_runs_park_two_entries(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _patch_run_pipeline(monkeypatch)

        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_failing_send([])):
                job1 = _origin_job(id="loop17-direct", name="t")
                assert s.run_one_job(job1) is True
                job2 = _origin_job(id="loop17-direct", name="t")
                assert s.run_one_job(job2) is True

            # The created execution id is wired into the delivery dict (F1) —
            # before the fix both jobs arrived at the outbox with None.
            assert job1.get("execution_id")
            assert job2.get("execution_id")
            assert job1["execution_id"] != job2["execution_id"]

            entries = outbox.list_entries()
            assert len(entries) == 2
            assert {e["execution_id"] for e in entries} == {
                job1["execution_id"], job2["execution_id"],
            }
            assert all(e["error_class"] == "send" for e in entries)
            assert all(e["status"] == "queued" for e in entries)

    def test_send_enqueue_without_execution_id_is_marked_but_legacy(
        self, tmp_path, caplog,
    ):
        """Legacy callers (no execution_id) keep the loop-6 refresh collapse
        but are visibly marked with a warning. Since loop 20 (F3) this path
        requires the explicit _allow_legacy_no_execution=True opt-in."""
        home = tmp_path / "home"
        with use_cron_store(home):
            with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
                outbox.enqueue(
                    "job", _target(), "v1", "err", _allow_legacy_no_execution=True,
                )
            assert any(
                "WITHOUT execution_id" in r.message for r in caplog.records
            )
            # Loop-6 semantics preserved: a repeat refreshes the same entry.
            outbox.enqueue(
                "job", _target(), "v2", "err", _allow_legacy_no_execution=True,
            )
            entries = outbox.list_entries()
            assert len(entries) == 1
            assert entries[0]["payload"] == "v2"


class TestDeadLetterAppendOnly:
    """(b) F2: config/relay dead letters no longer overwrite each other's
    payloads; the anti-pile-up cap replaces the OLDEST entry."""

    def test_two_executions_keep_both_config_payloads(self, tmp_path):
        home = tmp_path / "home"
        job1 = _origin_job(
            origin={"platform": "no_such_platform_xyz", "chat_id": "1"},
            execution_id="exec-cfg-1",
        )
        job2 = _origin_job(
            origin={"platform": "no_such_platform_xyz", "chat_id": "1"},
            execution_id="exec-cfg-2",
        )
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()):
                _deliver_result(job1, "config report v1")
                _deliver_result(job2, "config report v2")

            entries = outbox.list_entries()
            assert len(entries) == 2
            assert all(e["status"] == "dead" and e["error_class"] == "config" for e in entries)
            by_execution = {e["execution_id"]: e for e in entries}
            assert "config report v1" in by_execution["exec-cfg-1"]["payload"]
            assert "config report v2" in by_execution["exec-cfg-2"]["payload"]

    def test_relay_entries_are_append_only_per_execution(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            outbox.enqueue(
                "job", _target(), "relay v1", "err",
                execution_id="exec-r1", error_class=outbox.ERROR_CLASS_RELAY,
            )
            outbox.enqueue(
                "job", _target(), "relay v2", "err",
                execution_id="exec-r2", error_class=outbox.ERROR_CLASS_RELAY,
            )
            entries = outbox.list_entries()
            assert len(entries) == 2
            assert {e["payload"] for e in entries} == {"relay v1", "relay v2"}
            assert all(e["error_class"] == "relay" for e in entries)

    def test_fourth_config_failure_replaces_oldest_at_cap(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()):
                for i in (1, 2, 3, 4):
                    job = _origin_job(
                        origin={"platform": "no_such_platform_xyz", "chat_id": "1"},
                        execution_id=f"exec-cap-{i}",
                    )
                    _deliver_result(job, f"cfg report {i}")

            entries = outbox.list_entries()
            assert len(entries) == outbox.MAX_DEAD_PER_TARGET == 3
            # The oldest (exec-cap-1) was replaced; the three newest survive
            # with their payloads intact.
            assert {e["execution_id"] for e in entries} == {
                "exec-cap-2", "exec-cap-3", "exec-cap-4",
            }
            by_execution = {e["execution_id"]: e for e in entries}
            for i in (2, 3, 4):
                assert f"cfg report {i}" in by_execution[f"exec-cap-{i}"]["payload"]
            assert outbox.outbox_counts()["dead"] == 3


class TestSendLease:
    """(c) F3: sending lease persisted BEFORE the send; stale lease retried;
    fresh lease skipped; delivered carries a receipt."""

    def test_begin_replay_persists_sending_before_send(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")
            leased = outbox.begin_replay(entry["id"])

            assert leased is not None
            assert leased["status"] == "sending"
            # Persisted (not just in-memory): the stored row shows the lease.
            stored = outbox.list_entries()[0]
            assert stored["status"] == "sending"
            assert stored["lease_ts"]
            assert stored["lease_pid"] == os.getpid()
            assert stored["send_attempt"] == 1

    def test_fresh_lease_of_live_process_is_skipped(self, tmp_path):
        home = tmp_path / "home"
        sends = []
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")
            assert outbox.begin_replay(entry["id"]) is not None

            # A fresh lease: not due, a second lease attempt refuses, and a
            # replay pass sends nothing.
            assert outbox.due_entries() == []
            assert outbox.begin_replay(entry["id"]) is None
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 0
            assert sends == []
            assert outbox.list_entries()[0]["status"] == "sending"

    def test_stale_lease_is_orphaned_and_retried(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        sends = []
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")
            assert outbox.begin_replay(entry["id"]) is not None

            # Lease expired (holder crashed mid-send) ⇒ due again, replay
            # retakes the lease and delivers. Loop 20 / F6: TTL=0 is invalid
            # input and falls back to the default — expire the lease by
            # moving the clock past a small positive TTL instead.
            real_now = outbox._hermes_now
            monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")
            monkeypatch.setattr(
                outbox, "_hermes_now",
                lambda: real_now() + timedelta(seconds=120),
            )
            assert len(outbox.due_entries()) == 1
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 1
            assert len(sends) == 1
            assert outbox.list_entries()[0]["status"] == "delivered"

    def test_delivered_records_platform_message_id_receipt(self, tmp_path):
        home = tmp_path / "home"
        sends = []
        with use_cron_store(home):
            outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform",
                       new=_ok_send(sends, result={"success": True, "message_id": "m-4711"})):
                assert s._replay_delivery_outbox() == 1
            entry = outbox.list_entries()[0]
            assert entry["status"] == "delivered"
            # Loop 20 / F1: the receipt is stored structured; a platform
            # message_id is the external-delivery kind.
            assert entry["receipt"] == {
                "kind": outbox.RECEIPT_KIND_PLATFORM_MESSAGE,
                "value": "m-4711",
            }
            # Lease fields are cleared on the terminal transition.
            for field in ("lease_ts", "lease_pid", "send_attempt"):
                assert field not in entry

    def test_receipt_falls_back_to_payload_hash(self, tmp_path):
        home = tmp_path / "home"
        sends = []
        with use_cron_store(home):
            outbox.enqueue("job", _target(), "hashed payload", "err", execution_id="exec-1")
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 1
            entry = outbox.list_entries()[0]
            assert entry["status"] == "delivered"
            # Loop 20 / F1: the payload-hash fallback is stored structured
            # and honestly labelled as a LOCAL send witness, not a
            # delivery receipt.
            receipt = entry["receipt"]
            assert receipt["kind"] == outbox.RECEIPT_KIND_LOCAL_SEND_WITNESS
            assert "@" in receipt["value"]  # <sha256 hex>@<send timestamp>

    def test_crash_between_send_and_record_redelivers_after_lease_expiry(
        self, tmp_path, monkeypatch,
    ):
        """The documented remaining gap: the send landed but the status
        record crashed ⇒ the entry stays ``sending`` and, after the lease
        expires, is delivered AGAIN — at-least-once with a visible receipt,
        never a silent loss."""
        home = tmp_path / "home"
        sends = []
        with use_cron_store(home):
            outbox.enqueue("job", _target(), "p", "err", execution_id="exec-1")

            def _crash_record(*args, **kwargs):
                raise OSError("crash between send and record")

            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)), \
                 monkeypatch.context() as m:
                m.setattr(outbox, "record_replay_result", _crash_record)
                assert s._replay_delivery_outbox() == 0
            # The send DID go out once; the entry is stuck in sending.
            assert len(sends) == 1
            assert outbox.list_entries()[0]["status"] == "sending"

            # After the lease expires the orphaned entry is retried and
            # delivered — the duplicate is the honest at-least-once price.
            # Loop 20 / F6: TTL=0 is invalid input; expire the lease by
            # moving the clock past a small positive TTL instead.
            real_now = outbox._hermes_now
            monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")
            monkeypatch.setattr(
                outbox, "_hermes_now",
                lambda: real_now() + timedelta(seconds=120),
            )
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(sends)):
                assert s._replay_delivery_outbox() == 1
            assert len(sends) == 2
            assert outbox.list_entries()[0]["status"] == "delivered"


class TestConfigLoadEnqueue:
    """(d) F4: a load_gateway_config() failure no longer returns before the
    outbox — it parks a config-class dead letter with the payload."""

    def test_gateway_config_load_failure_enqueues_config_dead_letter(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config",
                       side_effect=RuntimeError("yaml kaputt")):
                error = _deliver_result(
                    _origin_job(execution_id="exec-load-1"), "report body",
                )

            assert error is not None and "failed to load gateway config" in error
            entries = outbox.list_entries()
            assert len(entries) == 1
            entry = entries[0]
            assert entry["error_class"] == "config"
            assert entry["status"] == "dead"
            assert entry["execution_id"] == "exec-load-1"
            assert "report body" in entry["payload"]
            assert "yaml kaputt" in (entry["last_error"] or "")
            # Dead on arrival: never retried.
            assert outbox.due_entries() == []
            assert outbox.outbox_counts()["dead"] == 1

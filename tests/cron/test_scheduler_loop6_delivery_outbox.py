"""Audit loop 6 (A-H2/A-M8): persistent delivery outbox with replay.

Before the fix, a delivery that failed on every path (live adapter +
standalone fallback) survived only as ``last_delivery_error`` — and the
error summary itself went over the same possibly-broken channel, so report
losses were silent. Now ``_deliver_result`` parks the payload in a durable
JSONL outbox (``cron/outbox.jsonl`` under the active cron store) and every
tick replays due entries with exponential backoff (5min → 80min cap); after
5 failed replays an entry is dead-lettered but stays visible.

Hermetic: no network — the platform send is replaced by stub coroutines.
"""

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
        "id": "outbox-job",
        "name": "outbox-report",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }
    job.update(extra)
    return job


def _failing_send(calls):
    async def _send(*args, **kwargs):
        calls.append((args, kwargs))
        return {"error": "platform down"}

    return _send


def _ok_send(calls):
    async def _send(platform, pconfig, chat_id, content, **kwargs):
        calls.append({"chat_id": chat_id, "content": content, "kwargs": kwargs})
        return {"success": True}

    return _send


class TestEnqueueOnTotalDeliveryFailure:
    """(a) Delivery fails on all paths ⇒ queued outbox entry; a later tick
    replays it successfully ⇒ delivered, nothing sent twice."""

    def test_failure_enqueues_and_successful_replay_delivers(self, tmp_path):
        home = tmp_path / "home"
        fail_sends = []
        ok_sends = []

        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_failing_send(fail_sends)):
                error = _deliver_result(_origin_job(), "report body")

            # The old net still catches the failure...
            assert error is not None and "platform down" in error
            # ...and the payload is now ALSO parked in the outbox.
            entries = outbox.list_entries()
            assert len(entries) == 1
            entry = entries[0]
            assert entry["status"] == "queued"
            assert entry["job_id"] == "outbox-job"
            assert entry["target"]["platform"] == "telegram"
            assert entry["target"]["chat_id"] == "123"
            assert "report body" in entry["payload"]
            assert entry["attempts"] == 0

            # Idempotent: a second failure for the same job+target refreshes
            # the SAME entry instead of appending a duplicate.
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_failing_send(fail_sends)):
                _deliver_result(_origin_job(), "report body v2")
            entries = outbox.list_entries()
            assert len(entries) == 1
            assert entries[0]["id"] == entry["id"]
            assert "report body v2" in entries[0]["payload"]

            # Replay with a healthy platform: delivered exactly once.
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(ok_sends)):
                replayed = s._replay_delivery_outbox()
            assert replayed == 1
            assert len(ok_sends) == 1
            assert ok_sends[0]["chat_id"] == "123"
            assert "report body v2" in ok_sends[0]["content"]
            assert outbox.list_entries()[0]["status"] == "delivered"

            # No double delivery: the next replay finds nothing due.
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_ok_send(ok_sends)):
                assert s._replay_delivery_outbox() == 0
            assert len(ok_sends) == 1

    def test_outbox_failure_does_not_change_delivery_contract(self, tmp_path, monkeypatch):
        """Fail-closed: a broken outbox must not break delivery reporting —
        the error still lands in the return value (-> last_delivery_error)."""
        home = tmp_path / "home"
        monkeypatch.setattr(
            outbox, "_read_entries",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")),
        )
        with use_cron_store(home):
            with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
                 patch("tools.send_message_tool._send_to_platform", new=_failing_send([])):
                error = _deliver_result(_origin_job(), "report body")
        assert error is not None and "platform down" in error


class TestDeadLetterAfterMaxAttempts:
    """(b) 5 failed replays (backoff fast-forwarded via time injection)
    ⇒ dead; nothing is sent again afterwards."""

    def test_five_failed_replays_mark_dead_and_stop(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        clock = {"now": datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)}
        monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])
        sends = []

        with use_cron_store(home):
            outbox.enqueue(
                "dead-job",
                {"platform": "telegram", "chat_id": "9", "thread_id": None},
                "lost report",
                "initial failure",
            )

            cfg_patch = patch(
                "gateway.config.load_gateway_config",
                return_value=_telegram_gateway_cfg(),
            )
            send_patch = patch(
                "tools.send_message_tool._send_to_platform",
                new=_failing_send(sends),
            )
            with cfg_patch, send_patch:
                # First replay is due immediately (fresh entry).
                assert s._replay_delivery_outbox() == 0
                assert len(sends) == 1
                entry = outbox.list_entries()[0]
                assert entry["status"] == "queued"
                assert entry["attempts"] == 1

                # Backoff is real: without advancing the clock the entry is
                # NOT due again on the very next tick.
                assert s._replay_delivery_outbox() == 0
                assert len(sends) == 1

                # Attempts 2..5: jump past any backoff rung (cap is 4800s).
                for expected_attempts in (2, 3, 4, 5):
                    clock["now"] += timedelta(seconds=5000)
                    assert s._replay_delivery_outbox() == 0
                    assert len(sends) == expected_attempts

                entry = outbox.list_entries()[0]
                assert entry["status"] == "dead"
                assert entry["attempts"] == outbox.MAX_ATTEMPTS

                # Dead letter: never sent again, but still visible.
                clock["now"] += timedelta(days=30)
                assert s._replay_delivery_outbox() == 0
                assert len(sends) == 5
                assert outbox.outbox_counts()["dead"] == 1

    def test_backoff_ladder_is_exponential_and_capped(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        clock = {"now": datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)}
        monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])

        with use_cron_store(home):
            entry = outbox.enqueue(
                "ladder-job",
                {"platform": "telegram", "chat_id": "1", "thread_id": None},
                "p",
                "e",
            )
            expected = [300, 600, 1200, 2400]  # then attempt 5 → dead
            start = clock["now"]
            for attempts, delay in enumerate(expected, start=1):
                entry = outbox.record_replay_result(entry["id"], success=False, error="x")
                assert entry["attempts"] == attempts
                next_retry = datetime.fromisoformat(entry["next_retry_at"])
                assert (next_retry - start).total_seconds() == delay


class TestStoreScoping:
    """(e) The outbox lands in the profile store (ContextVar), not the
    import home — same precedence as cron/executions.py."""

    def test_scoped_profiles_get_isolated_outboxes(self, tmp_path):
        home_a = tmp_path / "profiles" / "alpha"
        home_b = tmp_path / "profiles" / "beta"

        with use_cron_store(home_a):
            outbox.enqueue(
                "job",
                {"platform": "telegram", "chat_id": "1", "thread_id": None},
                "alpha payload",
                "err",
            )

        assert (home_a / "cron" / "outbox.jsonl").exists()
        assert not (home_b / "cron" / "outbox.jsonl").exists()

        with use_cron_store(home_a):
            assert [e["payload"] for e in outbox.list_entries()] == ["alpha payload"]
            assert outbox._current_outbox_file() == home_a / "cron" / "outbox.jsonl"
        with use_cron_store(home_b):
            assert outbox.list_entries() == []

    def test_outbox_follows_hermes_home_repointed_after_import(self, tmp_path, monkeypatch):
        home = tmp_path / "late-home"
        monkeypatch.setenv("HERMES_HOME", str(home))

        outbox.enqueue(
            "job",
            {"platform": "telegram", "chat_id": "2", "thread_id": None},
            "late payload",
            "err",
        )

        assert (home / "cron" / "outbox.jsonl").exists()
        assert [e["payload"] for e in outbox.list_entries()] == ["late payload"]

    def test_repointed_outbox_file_constant_is_honored(self, tmp_path, monkeypatch):
        pointed = tmp_path / "pointed" / "outbox.jsonl"
        monkeypatch.setattr(outbox, "OUTBOX_FILE", pointed)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))

        outbox.enqueue(
            "job",
            {"platform": "telegram", "chat_id": "3", "thread_id": None},
            "compat payload",
            "err",
        )

        assert pointed.exists()
        assert not (tmp_path / "elsewhere" / "cron" / "outbox.jsonl").exists()

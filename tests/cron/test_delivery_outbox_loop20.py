"""Audit loop 20: receipt honesty, operator outbox status, fail-closed
enqueue, and lease-TTL env validation (Codex review #4 — F1/F2/F3/F6).

- F1 (receipt honesty): the loop-17 receipt was a plain string and the
  payload-hash fallback (``payload-sha256:<hash>@<ts>``) read as "proof of
  delivery" while only witnessing the LOCAL send time. Receipts are now
  stored structured as ``{"kind", "value"}``: ``platform_message`` is an
  external delivery receipt; ``local_send_witness`` is explicitly NOT one.
- F2 (status visibility): the crash-critical ``sending`` state was visible
  only in the JSONL. ``outbox_status()`` surfaces counts, the oldest live
  lease age, and the number of leases already past the TTL (orphaned).
- F3 (fail-closed enqueue): a send-class enqueue without an execution_id
  silently degraded per-run idempotency. It now raises ValueError; only the
  explicit (deprecated) ``allow_legacy_no_execution=True`` opt-in keeps the
  loop-6 legacy key.
- F6 (lease-TTL env validation): only finite positive floats are accepted;
  NaN/inf/zero/negative/garbage fall back to the default WITH a warning.

Hermetic: no network, no real platform sends. The cross-process test uses
ProcessPoolExecutor against a tmp-profile store (mirrors loop 13).
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import cron.delivery_outbox as outbox
from cron.jobs import use_cron_store


def _target(chat_id="123"):
    return {"platform": "telegram", "chat_id": chat_id, "thread_id": None}


def _proc_enqueue_one(home, execution_id, payload):
    """Module-level worker (picklable): one send-class enqueue per process."""
    from cron.jobs import use_cron_store as _ucs

    import cron.delivery_outbox as _ob

    with _ucs(home):
        entry = _ob.enqueue(
            "shared-job",
            {"platform": "telegram", "chat_id": "1", "thread_id": None},
            payload,
            "err",
            execution_id=execution_id,
        )
    return entry["id"]


class TestStructuredReceipt:
    """(a) F1: receipts are stored structured; the hash fallback is honestly
    labelled a LOCAL send witness, never an external delivery receipt."""

    def test_platform_message_string_maps_to_platform_kind(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            updated = outbox.record_replay_result(
                entry["id"], success=True, receipt="platform-message:m-4711",
            )
            assert updated["receipt"] == {
                "kind": outbox.RECEIPT_KIND_PLATFORM_MESSAGE,
                "value": "m-4711",
            }
            # Persisted structured, not as the legacy string.
            stored = outbox.list_entries()[0]
            assert stored["receipt"]["kind"] == "platform_message"

    def test_payload_hash_string_maps_to_local_send_witness(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            updated = outbox.record_replay_result(
                entry["id"], success=True,
                receipt="payload-sha256:abc123@2026-07-29T12:00:00+00:00",
            )
            receipt = updated["receipt"]
            assert receipt["kind"] == outbox.RECEIPT_KIND_LOCAL_SEND_WITNESS
            assert receipt["value"] == "abc123@2026-07-29T12:00:00+00:00"

    def test_unknown_string_is_never_upgraded_to_platform_receipt(self, tmp_path):
        """Unclassified evidence defaults to the honest local-witness kind."""
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            updated = outbox.record_replay_result(
                entry["id"], success=True, receipt="some-future-format:xyz",
            )
            assert updated["receipt"] == {
                "kind": outbox.RECEIPT_KIND_LOCAL_SEND_WITNESS,
                "value": "some-future-format:xyz",
            }

    def test_structured_dict_passthrough_validated(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            updated = outbox.record_replay_result(
                entry["id"], success=True,
                receipt={"kind": "platform_message", "value": "direct-1"},
            )
            assert updated["receipt"] == {
                "kind": "platform_message", "value": "direct-1",
            }

    def test_invalid_receipt_dict_is_not_stored(self, tmp_path):
        """A dict with an unknown kind or empty value stores NO receipt —
        fabricated evidence must not be persisted."""
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            for bad in (
                {"kind": "delivery_proof", "value": "x"},
                {"kind": "platform_message", "value": ""},
                {"kind": "platform_message"},
            ):
                updated = outbox.record_replay_result(
                    entry["id"], success=True, receipt=bad,
                )
                assert "receipt" not in updated

    def test_failed_replay_stores_no_receipt(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
            updated = outbox.record_replay_result(
                entry["id"], success=False, error="down",
                receipt="platform-message:m-1",
            )
            assert updated["status"] == "queued"
            assert "receipt" not in updated


class TestOutboxStatus:
    """(b) F2: outbox_status() surfaces counts, oldest sending-lease age and
    the expired-lease alarm count."""

    def test_empty_outbox_reports_zeros(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            status = outbox.outbox_status()
        assert status["counts"] == {
            "queued": 0, "sending": 0, "delivered": 0, "dead": 0,
        }
        assert status["sending_oldest_age_seconds"] is None
        assert status["sending_expired"] == 0

    def test_counts_cover_every_status(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            queued = outbox.enqueue("job", _target("1"), "p", "e", execution_id="e1")
            sending = outbox.enqueue("job", _target("2"), "p", "e", execution_id="e2")
            delivered = outbox.enqueue("job", _target("3"), "p", "e", execution_id="e3")
            dead = outbox.enqueue(
                "job", _target("4"), "p", "e",
                execution_id="e4", error_class=outbox.ERROR_CLASS_CONFIG,
            )
            assert outbox.begin_replay(sending["id"]) is not None
            outbox.record_replay_result(
                delivered["id"], success=True, receipt="platform-message:m-1",
            )
            assert queued["status"] == "queued" and dead["status"] == "dead"

            status = outbox.outbox_status()
            assert status["counts"]["queued"] == 1
            assert status["counts"]["sending"] == 1
            assert status["counts"]["delivered"] == 1
            assert status["counts"]["dead"] == 1
            # The one sending lease is fresh: no alarm, small age.
            assert status["sending_expired"] == 0
            age = status["sending_oldest_age_seconds"]
            assert age is not None and age < 60

    def test_oldest_age_and_expired_count(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        base = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        clock = {"now": base}
        monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")

        with use_cron_store(home):
            old = outbox.enqueue("job", _target("1"), "p", "e", execution_id="e1")
            assert outbox.begin_replay(old["id"]) is not None
            clock["now"] = base + timedelta(seconds=300)  # old lease: 300s > TTL
            fresh = outbox.enqueue("job", _target("2"), "p", "e", execution_id="e2")
            assert outbox.begin_replay(fresh["id"]) is not None

            status = outbox.outbox_status()
            assert status["counts"]["sending"] == 2
            assert status["sending_oldest_age_seconds"] == pytest.approx(300.0)
            assert status["sending_expired"] == 1

    def test_sending_without_lease_ts_counts_as_expired(self, tmp_path):
        """A hand-written/corrupt sending row without lease_ts is stale by
        definition (_lease_is_stale) and must show up in the alarm count."""
        home = tmp_path / "home"
        with use_cron_store(home):
            entry = outbox.enqueue("job", _target(), "p", "e", execution_id="e1")
            assert outbox.begin_replay(entry["id"]) is not None
            path = outbox._current_outbox_file()
            entries = outbox._read_entries(path)
            entries[0].pop("lease_ts")
            outbox._write_entries(path, entries)

            status = outbox.outbox_status()
            assert status["counts"]["sending"] == 1
            assert status["sending_expired"] == 1
            assert status["sending_oldest_age_seconds"] is None


class TestFailClosedEnqueue:
    """(c) F3: send-class enqueues REQUIRE an execution_id; the deprecated
    opt-in preserves the legacy path."""

    def test_send_enqueue_without_execution_id_raises(self, tmp_path):
        home = tmp_path / "home"
        with use_cron_store(home):
            with pytest.raises(ValueError, match="execution_id"):
                outbox.enqueue("job", _target(), "p", "err")
            # Fail-closed means: NOTHING was persisted.
            assert outbox.list_entries() == []

    def test_opt_in_keeps_legacy_path_with_warning(self, tmp_path, caplog):
        home = tmp_path / "home"
        with use_cron_store(home):
            with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
                entry = outbox.enqueue(
                    "job", _target(), "p", "err", allow_legacy_no_execution=True,
                )
            assert entry["status"] == "queued"
            assert entry["execution_id"] is None
            assert any("WITHOUT execution_id" in r.message for r in caplog.records)

    def test_config_and_relay_classes_stay_enqueueable_without_execution_id(
        self, tmp_path,
    ):
        """Dead-on-arrival classes are exempt: their dedupe key does not
        depend on per-run idempotency the same way (loop-17 F2 cap applies)."""
        home = tmp_path / "home"
        with use_cron_store(home):
            for cls in (outbox.ERROR_CLASS_CONFIG, outbox.ERROR_CLASS_RELAY):
                entry = outbox.enqueue("job", _target(), "p", "err", error_class=cls)
                assert entry["status"] == "dead"

    def test_unknown_error_class_defaults_to_send_and_still_requires_id(
        self, tmp_path,
    ):
        """An unknown error_class is normalized to send BEFORE the fail-closed
        check — it cannot smuggle a send-class entry past the id requirement."""
        home = tmp_path / "home"
        with use_cron_store(home):
            with pytest.raises(ValueError, match="execution_id"):
                outbox.enqueue("job", _target(), "p", "err", error_class="bogus")

    def test_two_processes_enqueue_same_job_both_entries_intact(self, tmp_path):
        """Konkurrenz-Test: two PROCESSES enqueue the same job/target with
        DIFFERENT execution_ids concurrently ⇒ both entries survive intact
        (no torn write, no lost append under the cross-process lock)."""
        home = tmp_path / "home"
        with ProcessPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_proc_enqueue_one, str(home), "exec-alpha", "payload alpha")
            f2 = pool.submit(_proc_enqueue_one, str(home), "exec-beta", "payload beta")
            ids = {f1.result(), f2.result()}

        with use_cron_store(home):
            entries = outbox.list_entries()
        assert len(entries) == 2
        assert {e["id"] for e in entries} == ids
        assert {e["execution_id"] for e in entries} == {"exec-alpha", "exec-beta"}
        assert {e["payload"] for e in entries} == {"payload alpha", "payload beta"}


class TestLeaseTtlEnvValidation:
    """(d) F6: only finite positive floats are accepted; every garbage class
    falls back to the 600s default WITH a warning."""

    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
    def test_non_finite_values_fall_back_with_warning(self, raw, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", raw)
        with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
            assert outbox._lease_ttl_seconds() == outbox._DEFAULT_LEASE_TTL_SECONDS
        assert any(
            "HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS" in r.message
            for r in caplog.records
        )

    @pytest.mark.parametrize("raw", ["0", "-1", "-600.5"])
    def test_non_positive_values_fall_back_with_warning(self, raw, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", raw)
        with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
            assert outbox._lease_ttl_seconds() == outbox._DEFAULT_LEASE_TTL_SECONDS
        assert any(
            "HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS" in r.message
            for r in caplog.records
        )

    @pytest.mark.parametrize("raw", ["garbage", "", "  ", "10min", "1e"])
    def test_unparseable_values_fall_back_with_warning(self, raw, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", raw)
        with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
            assert outbox._lease_ttl_seconds() == outbox._DEFAULT_LEASE_TTL_SECONDS
        assert any(
            "HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS" in r.message
            for r in caplog.records
        )

    def test_positive_value_accepted_without_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "42.5")
        with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
            assert outbox._lease_ttl_seconds() == 42.5
        assert caplog.records == []

    def test_unset_env_uses_default_without_warning(self, monkeypatch, caplog):
        monkeypatch.delenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", raising=False)
        with caplog.at_level(logging.WARNING, logger="cron.delivery_outbox"):
            assert outbox._lease_ttl_seconds() == outbox._DEFAULT_LEASE_TTL_SECONDS
        assert caplog.records == []

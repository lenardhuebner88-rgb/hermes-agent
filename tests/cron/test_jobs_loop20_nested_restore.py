"""Audit loop 20 / F5: nested restore validation for job records.

Loop 18's full-schema check validated top-level field TYPES, but the three
nested payloads stayed shallow: ``retry`` and ``origin`` only had to be
dicts, and the schedule per-kind payload rules were open-coded inline.
Since loop 20:

- the schedule per-kind payload checks derive from the central
  ``_SCHEDULE_KIND_PAYLOAD`` table (single copy of "what parse_schedule()
  emits per kind");
- a present ``retry`` dict must match the shape create_job() persists via
  ``_normalize_retry`` (``_is_plausible_retry_policy``): max_attempts
  strictly int 1..MAX_RUN_RETRY_ATTEMPTS, backoff_seconds a list of
  positive numbers. The strict loop-15 API validator
  (validate_retry_policy) is deliberately NOT reused — create_job stores
  float backoff entries verbatim (``[300.0]``), which the API boundary
  rejects; the restore bar is "create_job() could have written this";
- a present ``origin`` dict may only carry str keys with str-or-None
  values (create_job stores the gateway origin verbatim:
  platform/chat_id/thread_id).

Nested-corrupt records are discarded on restore like any other implausible
record — present-then-strict, absent fields stay tolerated.
"""

from __future__ import annotations

import cron.jobs as jobs


def _record(**overrides):
    record = {
        "id": "job-1",
        "name": "job-1",
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
    }
    record.update(overrides)
    return record


class TestScheduleNestedValidation:
    def test_valid_kinds_accepted(self):
        assert jobs._is_plausible_job_record(_record()) is True
        assert jobs._is_plausible_job_record(
            _record(schedule={"kind": "cron", "expr": "0 9 * * *"})
        ) is True
        assert jobs._is_plausible_job_record(
            _record(schedule={"kind": "once", "run_at": "2026-07-30T09:00"})
        ) is True

    def test_unknown_kind_rejected(self):
        assert jobs._is_plausible_job_record(
            _record(schedule={"kind": "sometimes", "minutes": 60})
        ) is False

    def test_interval_payload_nested_corrupt(self):
        for bad in (None, "60", 60.5, True, 0, -5):
            assert jobs._is_plausible_job_record(
                _record(schedule={"kind": "interval", "minutes": bad})
            ) is False, bad

    def test_cron_payload_nested_corrupt(self):
        for bad in (None, "", "   ", 42, ["0 9 * * *"]):
            assert jobs._is_plausible_job_record(
                _record(schedule={"kind": "cron", "expr": bad})
            ) is False, bad

    def test_once_payload_nested_corrupt(self):
        for bad in (None, "", 0, {"at": "x"}):
            assert jobs._is_plausible_job_record(
                _record(schedule={"kind": "once", "run_at": bad})
            ) is False, bad

    def test_kind_payload_swapped_rejected(self):
        """A cron-shaped payload under kind=interval is not a record
        create_job() could have written."""
        assert jobs._is_plausible_job_record(
            _record(schedule={"kind": "interval", "expr": "0 9 * * *"})
        ) is False


class TestRetryNestedValidation:
    def test_valid_retry_accepted(self):
        assert jobs._is_plausible_job_record(
            _record(retry={"max_attempts": 2, "backoff_seconds": [60, 300]})
        ) is True
        assert jobs._is_plausible_job_record(
            _record(retry={"max_attempts": jobs.MAX_RUN_RETRY_ATTEMPTS})
        ) is True
        # Absent/None stays tolerated (present-then-strict).
        assert jobs._is_plausible_job_record(_record(retry=None)) is True
        assert jobs._is_plausible_job_record(_record()) is True

    def test_float_backoff_accepted_create_job_writes_it(self):
        """create_job stores _normalize_retry's output, which keeps numeric
        backoff entries as floats — a record with [300.0] IS one
        create_job() could have written (loop-18 full-record parity)."""
        assert jobs._is_plausible_job_record(
            _record(retry={"max_attempts": 2, "backoff_seconds": [300.0]})
        ) is True

    def test_retry_max_attempts_out_of_bounds_rejected(self):
        for bad in (0, jobs.MAX_RUN_RETRY_ATTEMPTS + 1, -1):
            assert jobs._is_plausible_job_record(
                _record(retry={"max_attempts": bad})
            ) is False, bad

    def test_retry_max_attempts_wrong_type_rejected(self):
        for bad in (True, "2", 2.0, None):
            assert jobs._is_plausible_job_record(
                _record(retry={"max_attempts": bad})
            ) is False, bad

    def test_retry_missing_max_attempts_rejected(self):
        assert jobs._is_plausible_job_record(
            _record(retry={"backoff_seconds": [60]})
        ) is False

    def test_retry_backoff_nested_corrupt_rejected(self):
        for bad in (
            [60, -1],      # non-positive entry
            [60, 0],       # zero entry
            [60, True],    # bool entry (isinstance(True, int) trap)
            [60, "300"],   # str entry — create_job stores floats, never str
            "60",          # not a list
        ):
            assert jobs._is_plausible_job_record(
                _record(retry={"max_attempts": 2, "backoff_seconds": bad})
            ) is False, bad

    def test_retry_not_a_dict_rejected(self):
        for bad in ("retry", 3, ["max_attempts"]):
            assert jobs._is_plausible_job_record(_record(retry=bad)) is False, bad


class TestOriginNestedValidation:
    def test_valid_origin_accepted(self):
        assert jobs._is_plausible_job_record(
            _record(origin={
                "platform": "telegram", "chat_id": "123", "thread_id": None,
            })
        ) is True
        assert jobs._is_plausible_job_record(_record(origin={})) is True
        assert jobs._is_plausible_job_record(_record(origin=None)) is True

    def test_origin_with_non_str_value_rejected(self):
        for bad in (
            {"platform": "telegram", "chat_id": 123},        # int value
            {"platform": "telegram", "thread_id": {"x": 1}},  # nested dict
            {"platform": ["telegram"]},                       # list value
            {"platform": "telegram", "ok": True},             # bool value
        ):
            assert jobs._is_plausible_job_record(_record(origin=bad)) is False, bad

    def test_origin_not_a_dict_rejected(self):
        for bad in ("telegram", 42, ["platform"]):
            assert jobs._is_plausible_job_record(_record(origin=bad)) is False, bad


class TestNestedValidationThroughBackupRestore:
    """End-to-end: a backup mixing valid and nested-corrupt records restores
    only the valid ones (the loud discard path, loop 15 / F3)."""

    def test_backup_filters_nested_corrupt_records(self, tmp_path, caplog):
        records = [
            _record(id="ok", name="ok"),
            _record(id="bad-retry", name="bad-retry",
                    retry={"max_attempts": 99}),
            _record(id="bad-origin", name="bad-origin",
                    origin={"chat_id": 123}),
            _record(id="bad-schedule", name="bad-schedule",
                    schedule={"kind": "interval", "minutes": "60"}),
        ]
        valid = jobs._validate_backup_records(tmp_path / "backup.json", records)
        assert valid is not None
        assert [r["id"] for r in valid] == ["ok"]
        assert "discarded 3 invalid job record(s)" in caplog.text

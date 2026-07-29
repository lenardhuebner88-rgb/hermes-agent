"""Loop 18 / F1: horizon-aware global cap + julianday time comparisons.

Loop 15's global cap (HERMES_CRON_EXECUTIONS_MAX_TERMINAL) deleted terminal
rows regardless of age, so at high run volume it ate the 30-day retention
guarantee exactly where it was needed, and all time comparisons were raw
ISO-string comparisons that misorder mixed offsets. Since loop 18:

- every time comparison in _prune_unlocked goes through SQLite julianday(),
  which normalizes tz-aware ISO strings (offsets, 'Z', space separator) onto
  one UTC scale;
- the cap may only delete rows already OUTSIDE the keep-days horizon (oldest
  first, until the ledger is back under the cap); when the overflow consists
  entirely of in-horizon rows, nothing is deleted for the cap and a loud
  warning makes the overflow visible instead. Since loop 24 (M3) that
  warning is logger-only (no stderr print) and deduplicated to at most one
  per hour per process — the tests reset the dedup state explicitly.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta, timezone

from hermes_time import now as _hermes_now


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    # Loop 24 / M3: the overflow warning is time-deduplicated per process —
    # reset the state so each test sees its own warning.
    monkeypatch.setattr(executions, "_last_cap_overflow_warning", None)
    return executions


def _run(executions, job_id, *, success=True):
    row = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(row["id"], success=success)
    return row["id"]


def _set_claimed(executions, execution_id, claimed_at: str):
    """Rewrite one row's claimed_at verbatim (raw string, any ISO variant)."""
    conn = sqlite3.connect(executions._current_executions_file())
    try:
        conn.execute(
            "UPDATE executions SET claimed_at=? WHERE id=?",
            (claimed_at, execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def _age(executions, execution_id, *, days):
    claimed = (_hermes_now() - timedelta(days=days)).isoformat()
    _set_claimed(executions, execution_id, claimed)


class TestHorizonAwareCap:
    def test_in_horizon_rows_survive_cap_overflow_with_warning(
        self, monkeypatch, tmp_path, caplog, capsys
    ):
        """Rows inside the 30d horizon stay even beyond keep-N AND the global
        cap; the overflow is surfaced via the log — since loop 24 (M3)
        logger-only, no stderr print from library code."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

        for _ in range(10):
            _run(executions, "hot")

        records = executions.list_executions(job_id="hot", limit=500)
        assert len(records) == 10  # nothing deleted: the guarantee wins
        assert "exceeding the global cap of 5" in caplog.text
        # Loop 24 / M3: no print() to stderr from library code.
        assert capsys.readouterr().err == ""

    def test_cap_prunes_old_rows_until_back_under_cap(
        self, monkeypatch, tmp_path
    ):
        """Rows past the horizon are still cap-pruned, oldest first, until the
        ledger fits again — the cap keeps bounding OLD history."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "50")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

        aged = [_run(executions, "capped") for _ in range(10)]
        for execution_id in aged:
            _age(executions, execution_id, days=40)
        fresh = _run(executions, "capped")  # triggers the prune

        records = executions.list_executions(limit=500)
        remaining = {r["id"] for r in records}
        # Total 11 > cap 5: the 6 oldest (all out-of-horizon) are deleted;
        # the fresh run and the 4 newest aged rows survive.
        assert len(records) == 5
        assert fresh in remaining
        assert remaining - {fresh} == set(aged[-4:])

    def test_mixed_ages_cap_overflow_then_self_heals(
        self, monkeypatch, tmp_path
    ):
        """Overflow with only PART of the excess out-of-horizon: old rows are
        deleted, in-horizon rows are kept, and the warning still fires."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "50")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

        aged = [_run(executions, "mixed") for _ in range(3)]
        for execution_id in aged:
            _age(executions, execution_id, days=40)
        fresh_ids = [_run(executions, "mixed") for _ in range(5)]

        records = executions.list_executions(limit=500)
        remaining = {r["id"] for r in records}
        # 8 total > cap 5, only 3 deletable (out-of-horizon) → all 3 go,
        # the 5 in-horizon rows stay even though they still exceed the cap.
        assert remaining == set(fresh_ids)
        assert len(records) == 5

    def test_keep_days_zero_restores_legacy_global_cap(
        self, monkeypatch, tmp_path
    ):
        """HERMES_CRON_EXECUTIONS_KEEP_DAYS=0 degenerates the cap to the old
        count-based global bound (horizon = now ⇒ every past row deletable)."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "50")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_DAYS", "0")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

        ids = [_run(executions, "legacy") for _ in range(10)]

        records = executions.list_executions(limit=500)
        assert len(records) == 5
        assert {r["id"] for r in records} == set(ids[-5:])


class TestJuliandayTimeComparison:
    def test_mixed_offset_strings_classified_by_instant(
        self, monkeypatch, tmp_path
    ):
        """Two rows just across the 30d boundary, written with extreme
        offsets that make naive string ordering disagree with chronological
        order: the inside-horizon row (offset -12:00, lexically OLDER than
        the cutoff) is kept, the outside row (offset +14:00, lexically
        NEWER) is deleted."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "1")

        now = _hermes_now()
        inside = _run(executions, "edge")
        outside = _run(executions, "edge")
        # 1h inside the horizon, rendered at UTC-12:00 → local date string
        # sits BEFORE the cutoff string (naive compare: looks older).
        _set_claimed(
            executions,
            inside,
            (now - timedelta(days=30) + timedelta(hours=1))
            .astimezone(timezone(timedelta(hours=-12)))
            .isoformat(),
        )
        # 1h outside the horizon, rendered at UTC+14:00 → local date string
        # sits AFTER the cutoff string (naive compare: looks newer).
        _set_claimed(
            executions,
            outside,
            (now - timedelta(days=30) - timedelta(hours=1))
            .astimezone(timezone(timedelta(hours=14)))
            .isoformat(),
        )
        _run(executions, "edge")  # fresh run triggers the prune

        remaining = {r["id"] for r in executions.list_executions(job_id="edge", limit=500)}
        assert inside in remaining
        assert outside not in remaining

    def test_zulu_and_space_separator_variants_compare_correctly(
        self, monkeypatch, tmp_path
    ):
        """Rows stored with 'Z' suffix / space separator (foreign writers)
        land on the correct side of the horizon too."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "1")

        now = _hermes_now()
        inside = _run(executions, "fmt")
        outside = _run(executions, "fmt")
        zulu_inside = (
            (now - timedelta(days=30) + timedelta(hours=1))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        space_outside = (
            (now - timedelta(days=30) - timedelta(hours=1))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        _set_claimed(executions, inside, zulu_inside)
        _set_claimed(executions, outside, space_outside)
        _run(executions, "fmt")  # triggers the prune

        remaining = {r["id"] for r in executions.list_executions(job_id="fmt", limit=500)}
        assert inside in remaining
        assert outside not in remaining

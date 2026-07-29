"""Loop 24 (K3 review): horizon-cap overflow alarm hygiene + julianday
expression indexes.

- M3: the horizon-cap overflow warning fired on EVERY finish_execution
  prune and additionally print()ed to stderr from library code. It is now
  logger-only and time-deduplicated (at most one warning per hour per
  process); the overflow condition itself is still evaluated every prune.
- M4: the julianday()-ordered queries (list_executions, latest_executions,
  the prune window) computed the expression per row and sorted from a full
  scan — the plain claimed_at indexes cannot serve an ORDER BY over an
  expression. Expression indexes on (job_id, julianday(claimed_at), id) and
  (julianday(claimed_at), id) are created IF NOT EXISTS at schema init.
"""

from __future__ import annotations

import logging
import sqlite3
import time


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    # The overflow warning is time-deduplicated per process — reset the
    # state so each test sees its own warning.
    monkeypatch.setattr(executions, "_last_cap_overflow_warning", None)
    return executions


def _run(executions, job_id, *, success=True):
    row = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(row["id"], success=success)
    return row["id"]


class TestOverflowWarningDedup:
    """(a) M3: logger-only, at most one warning per hour per process."""

    def test_repeated_overflow_prunes_warn_once_and_never_print(
        self, monkeypatch, tmp_path, caplog, capsys,
    ):
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

        # Every finish_execution prunes; the overflow persists across all of
        # them (in-horizon rows are not deleted for the cap).
        with caplog.at_level(logging.WARNING, logger="cron.executions"):
            for _ in range(10):
                _run(executions, "hot")

        warnings = [
            r for r in caplog.records
            if "exceeding the global cap" in r.getMessage()
        ]
        assert len(warnings) == 1, f"expected exactly 1 deduplicated warning, got {len(warnings)}"
        # No print() to stdout/stderr from library code.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_warning_fires_again_after_the_interval(
        self, monkeypatch, tmp_path, caplog,
    ):
        """The dedup is time-based, not a one-shot latch: after the minimum
        interval the alarm is allowed to fire again."""
        executions = _point_ledger(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_KEEP_PER_JOB", "2")
        monkeypatch.setenv("HERMES_CRON_EXECUTIONS_MAX_TERMINAL", "5")

        with caplog.at_level(logging.WARNING, logger="cron.executions"):
            for _ in range(10):
                _run(executions, "hot")
            first_count = len([
                r for r in caplog.records
                if "exceeding the global cap" in r.getMessage()
            ])
            assert first_count == 1

            # Simulate "more than an hour since the last warning".
            monkeypatch.setattr(
                executions,
                "_last_cap_overflow_warning",
                time.monotonic()
                - executions._CAP_OVERFLOW_WARNING_MIN_INTERVAL_SECONDS
                - 1,
            )
            _run(executions, "hot")

            warnings = [
                r for r in caplog.records
                if "exceeding the global cap" in r.getMessage()
            ]
            assert len(warnings) == 2


class TestJuliandayExpressionIndexes:
    """(b) M4: the expression indexes exist after init and the
    julianday-ordered queries use them."""

    def test_indexes_exist_after_init(self, monkeypatch, tmp_path):
        executions = _point_ledger(monkeypatch, tmp_path)
        _run(executions, "idx")
        conn = sqlite3.connect(executions._current_executions_file())
        try:
            names = {row[1] for row in conn.execute("PRAGMA index_list(executions)")}
        finally:
            conn.close()
        assert "idx_executions_job_jd" in names
        assert "idx_executions_jd" in names

    def test_latest_executions_query_uses_job_jd_index(self, monkeypatch, tmp_path):
        """The correlated subquery shape of latest_executions must be served
        by idx_executions_job_jd (no per-row expression sort)."""
        executions = _point_ledger(monkeypatch, tmp_path)
        for _ in range(3):
            _run(executions, "idx")
        conn = sqlite3.connect(executions._current_executions_file())
        try:
            plan = conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT e2.id FROM executions e2
                   WHERE e2.job_id=?
                   ORDER BY julianday(e2.claimed_at) DESC, e2.id DESC LIMIT 1""",
                ("idx",),
            ).fetchall()
        finally:
            conn.close()
        detail = " | ".join(str(row[3]) for row in plan)
        assert "idx_executions_job_jd" in detail, f"query plan does not use the index: {detail}"

    def test_global_order_query_uses_jd_index(self, monkeypatch, tmp_path):
        """list_executions WITHOUT a job_id filter orders the whole table by
        julianday(claimed_at) — served by the global idx_executions_jd."""
        executions = _point_ledger(monkeypatch, tmp_path)
        for _ in range(3):
            _run(executions, "idx")
        conn = sqlite3.connect(executions._current_executions_file())
        try:
            plan = conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT * FROM executions
                   ORDER BY julianday(claimed_at) DESC, id DESC LIMIT 10""",
            ).fetchall()
        finally:
            conn.close()
        detail = " | ".join(str(row[3]) for row in plan)
        assert "idx_executions_jd" in detail, f"query plan does not use the index: {detail}"

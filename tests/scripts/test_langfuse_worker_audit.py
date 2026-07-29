from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from scripts.langfuse_worker_audit import audit, main


def _seed_usage(path, *, with_correlation: bool) -> None:
    correlation = (
        """
        task_run_id TEXT, task_id TEXT, chain_id TEXT, board TEXT,
        session_id TEXT, correlation_source TEXT,
        """
        if with_correlation
        else ""
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""
            CREATE TABLE run_usage_facts (
                run_id TEXT PRIMARY KEY, origin TEXT, {correlation}
                provider TEXT, model TEXT, profile TEXT, lane TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_tokens INTEGER, tool_call_count INTEGER,
                error_type TEXT, first_token_ms REAL, duration_ms REAL,
                context_window_used INTEGER, billing_mode TEXT,
                captured_at TEXT
            )
            """
        )
        columns = ["run_id", "origin"]
        values: list[object] = ["usage-1", "hermes_agent"]
        if with_correlation:
            columns.extend((
                "task_run_id",
                "task_id",
                "chain_id",
                "board",
                "session_id",
                "correlation_source",
            ))
            values.extend((
                "7",
                "task-7",
                "root-7",
                "default",
                "session-7",
                "kanban_runtime",
            ))
        columns.extend((
            "model", "profile", "lane", "input_tokens", "output_tokens", "captured_at",
        ))
        values.extend(("model-1", "coder", "coder", 10, 5, "2026-07-28T00:00:00+00:00"))
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO run_usage_facts ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


def _seed_kanban(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, started_at INTEGER)"
        )
        connection.executemany(
            "INSERT INTO task_runs VALUES (?, ?, ?)",
            [(7, "task-7", 1_785_196_800), (8, "task-8", 1_785_196_800)],
        )


def test_audit_reports_exact_denominators_and_board_gap(tmp_path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    _seed_usage(usage, with_correlation=True)
    _seed_kanban(kanban)

    report = audit(
        usage_path=usage,
        kanban_path=kanban,
        days=7,
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )

    assert report["usage_db"]["total_runs"] == 1
    assert report["usage_db"]["schema_missing"] == []
    assert report["origins"]["hermes_agent"]["coverage"]["task_id"] == {
        "present": 1,
        "ratio": 1.0,
    }
    assert report["kanban"] == {
        "available": True,
        "recent_task_runs": 2,
        "matched_task_runs": 1,
        "coverage_ratio": 0.5,
        "unmatched_task_runs": 1,
    }
    assert report["worker_release_invariant"] == {
        "schema_capable": {"available": True, "missing_fields": []},
        "observed_worker_runs": {"count": 1, "denominator_usage_facts": 1},
        "exact_run_links": {
            "count": 1,
            "denominator_recent_task_runs": 2,
            "coverage_ratio": 0.5,
        },
        "exact_task_only_links": {"count": 0, "denominator_usage_facts": 1},
        "ambiguous_session_ids": {"count": 0, "usage_facts": 0},
        "unresolved_runs": {"count": 1, "denominator_recent_task_runs": 2},
    }


def test_old_schema_is_a_visible_gap_not_a_query_failure(tmp_path) -> None:
    usage = tmp_path / "old-usage.db"
    _seed_usage(usage, with_correlation=False)

    report = audit(
        usage_path=usage,
        kanban_path=None,
        days=7,
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )

    assert report["usage_db"]["total_runs"] == 1
    assert report["usage_db"]["schema_missing"] == sorted({
        "task_run_id",
        "task_id",
        "chain_id",
        "board",
        "session_id",
        "correlation_source",
    })
    assert report["kanban"]["available"] is False
    assert report["worker_release_invariant"]["schema_capable"] == {
        "available": False,
        "missing_fields": sorted({
            "task_run_id",
            "task_id",
            "chain_id",
            "board",
            "session_id",
        }),
    }
    assert report["recommendations"][0]["priority"] == "P0"


def test_empty_board_window_has_no_fake_zero_percent_backfill_gap(tmp_path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    _seed_usage(usage, with_correlation=True)
    with sqlite3.connect(kanban) as connection:
        connection.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, started_at INTEGER)"
        )

    report = audit(
        usage_path=usage,
        kanban_path=kanban,
        days=7,
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )

    assert report["kanban"]["recent_task_runs"] == 0
    assert all(
        item["adapter"] != "worker correlation backfill"
        for item in report["recommendations"]
    )


def test_audit_separates_task_only_and_ambiguous_session_links(tmp_path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    _seed_usage(usage, with_correlation=True)
    _seed_kanban(kanban)
    with sqlite3.connect(usage) as connection:
        connection.execute(
            """
            INSERT INTO run_usage_facts (
                run_id, origin, task_run_id, task_id, chain_id, board,
                session_id, correlation_source, profile, lane, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usage-2", "hermes_agent", None, "task-8", "root-8", "default",
                "session-8", "kanban_runtime", "coder", "coder",
                "2026-07-28T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO run_usage_facts (
                run_id, origin, task_run_id, task_id, chain_id, board,
                session_id, correlation_source, profile, lane, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usage-3", "hermes_agent", "8", "task-8", "root-8", "default",
                "session-7", "kanban_runtime", "coder", "coder",
                "2026-07-28T00:00:00+00:00",
            ),
        )

    report = audit(
        usage_path=usage,
        kanban_path=kanban,
        days=7,
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )

    assert report["worker_release_invariant"] == {
        "schema_capable": {"available": True, "missing_fields": []},
        "observed_worker_runs": {"count": 2, "denominator_usage_facts": 3},
        "exact_run_links": {
            "count": 2,
            "denominator_recent_task_runs": 2,
            "coverage_ratio": 1.0,
        },
        "exact_task_only_links": {"count": 1, "denominator_usage_facts": 3},
        "ambiguous_session_ids": {"count": 1, "usage_facts": 2},
        "unresolved_runs": {"count": 0, "denominator_recent_task_runs": 2},
    }


def test_exact_link_release_gate_fails_closed_for_partial_coverage(tmp_path, capsys) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    _seed_usage(usage, with_correlation=True)
    _seed_kanban(kanban)

    assert main([
        "--usage-db", str(usage), "--kanban-db", str(kanban), "--days", "3650",
        "--require-exact-run-links",
    ]) == 2
    assert '"unresolved_runs"' in capsys.readouterr().out

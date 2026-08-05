"""Tests for hermes_cli.deck_burn against a self-built SQLite fixture.

Deliberately never touches the live /mnt/data/hermes-observability/usage_facts.db —
schema + rows below are hand-built to match it (see PRAGMA table_info dump used
while writing this module), not copied from production data, because the
values under test are the arithmetic/edge-case rules themselves rather than
any particular payload shape.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

from hermes_cli import deck_burn

NOW = _dt.datetime(2026, 8, 5, 21, 0, 0, tzinfo=_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _make_db(path: Path, *, calls: list[dict], usage_facts: list[dict] | None = None) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE run_llm_calls (
                run_id TEXT,
                call_index INTEGER,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                total_tokens INTEGER,
                origin TEXT,
                occurred_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE run_usage_facts (
                run_id TEXT PRIMARY KEY,
                origin TEXT,
                lane TEXT
            )
            """
        )
        for row in calls:
            conn.execute(
                """
                INSERT INTO run_llm_calls
                    (run_id, call_index, provider, model, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, total_tokens, origin, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"],
                    row.get("call_index", 0),
                    row.get("provider", "anthropic"),
                    row.get("model", "claude"),
                    row.get("input_tokens", 0),
                    row.get("output_tokens", 0),
                    row.get("cache_read_tokens", 0),
                    row.get("cache_write_tokens", 0),
                    row.get("total_tokens", 0),
                    row.get("origin", "hermes_agent"),
                    row["occurred_at"],
                ),
            )
        for row in usage_facts or []:
            conn.execute(
                "INSERT INTO run_usage_facts (run_id, origin, lane) VALUES (?, ?, ?)",
                (row["run_id"], row.get("origin", "hermes_agent"), row.get("lane")),
            )
        conn.commit()
    finally:
        conn.close()


def test_db_missing_returns_none_rate_with_reason(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    result = deck_burn.compute_burn_rate(db_path=missing, now=NOW)
    assert result["source"] == "db_missing"
    assert result["rate_billable_tokens_per_hour"] is None
    assert result["rate_reason"]


def test_empty_table_returns_none_rate_with_reason(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    _make_db(db, calls=[])
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    assert result["source"] == "db_empty"
    assert result["calls"] == 0
    assert result["rate_billable_tokens_per_hour"] is None
    assert result["rate_reason"]


def test_single_call_is_too_few_measurement_points(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    _make_db(
        db,
        calls=[
            {
                "run_id": "r1",
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_tokens": 90000,
                "occurred_at": _iso(NOW - _dt.timedelta(minutes=10)),
            }
        ],
    )
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    assert result["calls"] == 1
    assert result["rate_billable_tokens_per_hour"] is None
    assert result["rate_reason"] == "zu wenig Messpunkte"


def test_calls_bunched_in_a_few_seconds_are_too_few_measurement_points(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    base = NOW - _dt.timedelta(minutes=5)
    _make_db(
        db,
        calls=[
            {
                "run_id": "r1",
                "input_tokens": 100,
                "output_tokens": 50,
                "occurred_at": _iso(base),
            },
            {
                "run_id": "r2",
                "input_tokens": 100,
                "output_tokens": 50,
                "occurred_at": _iso(base + _dt.timedelta(seconds=3)),
            },
        ],
    )
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    assert result["calls"] == 2
    assert result["covered_seconds"] == 3
    assert result["rate_billable_tokens_per_hour"] is None
    assert result["rate_reason"] == "zu wenig Messpunkte"


def test_billable_and_cache_read_are_kept_separate(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    start = NOW - _dt.timedelta(minutes=30)
    end = NOW - _dt.timedelta(minutes=10)
    _make_db(
        db,
        calls=[
            {
                "run_id": "r1",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_write_tokens": 200,
                "cache_read_tokens": 90000,
                "occurred_at": _iso(start),
            },
            {
                "run_id": "r2",
                "input_tokens": 800,
                "output_tokens": 400,
                "cache_write_tokens": 0,
                "cache_read_tokens": 80000,
                "occurred_at": _iso(end),
            },
        ],
    )
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    assert result["calls"] == 2
    assert result["billable_tokens"] == 1000 + 500 + 200 + 800 + 400 + 0
    assert result["cache_read_tokens"] == 90000 + 80000
    # cache_read must never leak into the rate: billable-only over the 20-minute
    # covered span, not the ~59x-larger number you get by including cache_read.
    assert result["rate_billable_tokens_per_hour"] is not None
    expected_rate = result["billable_tokens"] / (20 * 60) * 3600.0
    assert result["rate_billable_tokens_per_hour"] == expected_rate
    assert result["rate_billable_tokens_per_hour"] < result["cache_read_tokens"]


def test_naive_datetime_now_comparison_would_overmatch_the_whole_day(tmp_path: Path) -> None:
    """Pins the string-comparison trap from the module docstring.

    A row from 20 hours ago must NOT be inside a 60-minute window. The naive
    ``datetime('now','-1 hour')`` cutoff ('...20:00:00', space separator)
    would still string-compare below an ISO 'T'-separated timestamp from
    earlier today and match it — this test would go red under that bug.
    """
    db = tmp_path / "usage_facts.db"
    old_but_same_day = NOW - _dt.timedelta(hours=20)
    recent = NOW - _dt.timedelta(minutes=15)
    recent2 = NOW - _dt.timedelta(minutes=5)
    _make_db(
        db,
        calls=[
            {
                "run_id": "old",
                "input_tokens": 999999,
                "output_tokens": 999999,
                "occurred_at": _iso(old_but_same_day),
            },
            {
                "run_id": "recent1",
                "input_tokens": 100,
                "output_tokens": 50,
                "occurred_at": _iso(recent),
            },
            {
                "run_id": "recent2",
                "input_tokens": 100,
                "output_tokens": 50,
                "occurred_at": _iso(recent2),
            },
        ],
    )
    result = deck_burn.compute_burn_rate(window_minutes=60, db_path=db, now=NOW)
    assert result["calls"] == 2
    assert result["billable_tokens"] == 300


def test_by_provider_and_by_model_breakdown(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    start = NOW - _dt.timedelta(minutes=30)
    end = NOW - _dt.timedelta(minutes=6)
    _make_db(
        db,
        calls=[
            {
                "run_id": "r1",
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_tokens": 100,
                "output_tokens": 50,
                "occurred_at": _iso(start),
            },
            {
                "run_id": "r2",
                "provider": "openai",
                "model": "gpt-5",
                "input_tokens": 200,
                "output_tokens": 100,
                "occurred_at": _iso(end),
            },
        ],
    )
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    assert result["by_provider"]["anthropic"]["calls"] == 1
    assert result["by_provider"]["openai"]["billable_tokens"] == 300
    assert result["by_model"]["claude-sonnet"]["calls"] == 1
    assert result["by_model"]["gpt-5"]["billable_tokens"] == 300


def test_lane_attribution_reports_share_not_a_full_split(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    start = NOW - _dt.timedelta(minutes=30)
    end = NOW - _dt.timedelta(minutes=6)
    _make_db(
        db,
        calls=[
            {"run_id": "r1", "input_tokens": 100, "output_tokens": 50, "occurred_at": _iso(start)},
            {"run_id": "r2", "input_tokens": 200, "output_tokens": 100, "occurred_at": _iso(end)},
            {"run_id": "r3", "input_tokens": 50, "output_tokens": 25, "occurred_at": _iso(end)},
        ],
        usage_facts=[{"run_id": "r1", "lane": "coder"}],
    )
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    attribution = result["lane_attribution"]
    assert attribution["attributed_calls"] == 1
    assert attribution["attributed_share"] == 1 / 3
    assert attribution["by_lane"] == {"coder": {"calls": 1, "billable_tokens": 150}}
    assert "note" in attribution


def test_lane_attribution_empty_when_no_usage_facts_rows(tmp_path: Path) -> None:
    db = tmp_path / "usage_facts.db"
    start = NOW - _dt.timedelta(minutes=30)
    end = NOW - _dt.timedelta(minutes=6)
    _make_db(
        db,
        calls=[
            {"run_id": "r1", "input_tokens": 100, "output_tokens": 50, "occurred_at": _iso(start)},
            {"run_id": "r2", "input_tokens": 200, "output_tokens": 100, "occurred_at": _iso(end)},
        ],
    )
    result = deck_burn.compute_burn_rate(db_path=db, now=NOW)
    attribution = result["lane_attribution"]
    assert attribution["attributed_calls"] == 0
    assert attribution["attributed_share"] == 0.0
    assert attribution["by_lane"] == {}

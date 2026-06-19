"""Unit tests for scripts/lane-latency.py — the NULL-safe lane-latency helper.

The helper is the canonical, tested telemetry source that replaces fragile
inline DB heredocs in analysis/dogfood specs. The root cause it guards against:
an ``output_tokens=NULL`` row crashed the original heredoc (``max(o,1)`` / ``i/o``
=> TypeError), so every lane improvised its own query => divergent numbers.

Covers (per PlanSpec AC-B-helper):
  (i)   a row with output_tokens=NULL does NOT crash (the exact original bug),
  (ii)  duration + in:out ratio computed correctly,
  (iii) an empty lane => clean empty output (no crash, no division-by-zero).
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "lane_latency", REPO_ROOT / "scripts" / "lane-latency.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _make_board(tmp_path: Path, rows) -> Path:
    """Build a temp kanban.db with a minimal task_runs table and the given rows.

    Each row: (task_id, profile, started_at, ended_at, input_tokens, output_tokens).
    """
    db_path = tmp_path / "kanban.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE task_runs (
            task_id TEXT, profile TEXT,
            started_at INTEGER, ended_at INTEGER,
            input_tokens INTEGER, output_tokens INTEGER
        )"""
    )
    con.executemany(
        "INSERT INTO task_runs "
        "(task_id, profile, started_at, ended_at, input_tokens, output_tokens) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return db_path


def test_null_output_tokens_does_not_crash(tmp_path):
    """The exact case that crashed the original heredoc: output_tokens IS NULL."""
    mod = _load_module()
    db = _make_board(
        tmp_path,
        [("t_null", "coder", 1000, 1050, 200_000, None)],
    )
    report = mod.lane_report(db, "coder", limit=10)
    assert report["n"] == 1
    run = report["runs"][0]
    assert run["dur"] == 50
    assert run["output_tokens"] in (0, None)
    # ratio must be representable (n/a), never raise on NULL output
    assert run["in_out"] is None or isinstance(run["in_out"], float)


def test_duration_and_ratio_correct(tmp_path):
    mod = _load_module()
    db = _make_board(
        tmp_path,
        [
            ("t_a", "coder", 100, 150, 1000, 250),   # dur 50, ratio 4.0
            ("t_b", "coder", 200, 500, 9000, 90),    # dur 300, ratio 100.0
        ],
    )
    report = mod.lane_report(db, "coder", limit=10)
    assert report["n"] == 2
    by_id = {r["task_id"]: r for r in report["runs"]}
    assert by_id["t_a"]["dur"] == 50
    assert abs(by_id["t_a"]["in_out"] - 4.0) < 1e-9
    assert abs(by_id["t_b"]["in_out"] - 100.0) < 1e-9
    # aggregates: median dur of {50,300} = 175, max 300
    assert report["median_dur"] == 175
    assert report["max_dur"] == 300


def test_empty_lane_clean_output(tmp_path):
    """Empty lane => no crash, no division-by-zero, zeroed aggregates."""
    mod = _load_module()
    db = _make_board(tmp_path, [("t_x", "coder", 1, 2, 5, 5)])
    report = mod.lane_report(db, "coder-claude", limit=10)  # different lane
    assert report["n"] == 0
    assert report["runs"] == []
    assert report["median_dur"] == 0
    assert report["max_dur"] == 0
    assert report["median_in_out"] is None


def test_only_completed_runs_counted(tmp_path):
    """Runs with NULL started_at/ended_at are excluded (not yet finished)."""
    mod = _load_module()
    db = _make_board(
        tmp_path,
        [
            ("t_done", "coder", 100, 200, 1000, 100),
            ("t_running", "coder", 100, None, 1000, None),  # not finished
            ("t_nostart", "coder", None, 200, 1000, 100),   # malformed
        ],
    )
    report = mod.lane_report(db, "coder", limit=10)
    assert report["n"] == 1
    assert report["runs"][0]["task_id"] == "t_done"


def test_limit_takes_most_recent(tmp_path):
    """--limit N returns the N most recently ended runs."""
    mod = _load_module()
    db = _make_board(
        tmp_path,
        [
            ("t_old", "coder", 10, 20, 100, 10),
            ("t_mid", "coder", 30, 60, 100, 10),
            ("t_new", "coder", 70, 110, 100, 10),
        ],
    )
    report = mod.lane_report(db, "coder", limit=2)
    ids = {r["task_id"] for r in report["runs"]}
    assert ids == {"t_mid", "t_new"}  # the two most recent by ended_at

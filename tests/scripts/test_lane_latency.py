"""Tests for ``scripts/lane-latency.py`` (B1-helper, AC-B-helper).

The lane-latency helper is the ONE canonical, NULL-safe telemetry source that
analysis / dogfood PlanSpecs call instead of embedding fragile DB heredocs. The
root cause it fixes: an inline ``i/o`` / ``max(o,1)`` heredoc crashed with a
``TypeError`` whenever ``output_tokens IS NULL``, forcing every lane to
improvise the query and produce divergent numbers.

These tests pin the three behaviours the heredoc got wrong:
  (i)  a row with ``output_tokens=NULL`` must NOT crash (no TypeError),
  (ii) duration and in:out ratio are computed correctly,
  (iii) an empty lane yields clean empty output (no crash, no div-by-zero).

The module is loaded via ``importlib`` because the script file name contains a
hyphen (``lane-latency.py``) and is not importable as a normal package module.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lane-latency.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("lane_latency", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane_latency = _load_module()


def _make_board(path: Path, rows: list[dict]) -> None:
    """Create a minimal ``task_runs`` table and insert ``rows``.

    Only the columns the helper reads are modelled; ``status`` is NOT NULL in the
    real schema so we always provide it.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE task_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT NOT NULL,
            profile       TEXT,
            status        TEXT NOT NULL,
            started_at    INTEGER NOT NULL,
            ended_at      INTEGER,
            input_tokens  INTEGER,
            output_tokens INTEGER
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, input_tokens, output_tokens) "
            "VALUES (:task_id, :profile, :status, :started_at, :ended_at, :input_tokens, :output_tokens)",
            {
                "task_id": r["task_id"],
                "profile": r["profile"],
                "status": r.get("status", "done"),
                "started_at": r["started_at"],
                "ended_at": r.get("ended_at"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
            },
        )
    conn.commit()
    conn.close()


@pytest.fixture
def board(tmp_path: Path):
    """Return a builder that writes a temp board and yields its path."""

    def _build(rows: list[dict]) -> str:
        db = tmp_path / "kanban.db"
        _make_board(db, rows)
        return str(db)

    return _build


# ---------------------------------------------------------------------------
# (i) NULL output_tokens must not crash
# ---------------------------------------------------------------------------


def test_null_output_tokens_does_not_crash(board):
    db = board(
        [
            # NULL output_tokens (and NULL input_tokens) — the heredoc killer.
            {"task_id": "t_aaa", "profile": "coder-claude", "started_at": 1000, "ended_at": 1200,
             "input_tokens": None, "output_tokens": None},
            # A normal row in the same lane so aggregates still have data.
            {"task_id": "t_bbb", "profile": "coder-claude", "started_at": 2000, "ended_at": 2100,
             "input_tokens": 1000, "output_tokens": 100},
        ]
    )

    report = lane_latency.collect(db_path=db, lane="coder-claude", limit=10)

    assert report["n"] == 2
    null_run = next(r for r in report["runs"] if r["task_id"] == "t_aaa")
    # NULL output → ratio is n.a. (None), never a TypeError / div-by-zero.
    assert null_run["in_out_ratio"] is None
    assert null_run["output_tokens"] == 0
    assert null_run["dur"] == 200
    # Text + JSON rendering must also survive the NULL row.
    assert isinstance(lane_latency.format_text(report), str)
    assert isinstance(lane_latency.format_json(report), str)


# ---------------------------------------------------------------------------
# (ii) duration + ratio computed correctly
# ---------------------------------------------------------------------------


def test_duration_and_ratio_computed(board):
    db = board(
        [
            {"task_id": "t_x", "profile": "coder", "started_at": 1000, "ended_at": 1200,
             "input_tokens": 1000, "output_tokens": 100},
        ]
    )

    report = lane_latency.collect(db_path=db, lane="coder", limit=10)

    assert report["n"] == 1
    run = report["runs"][0]
    assert run["dur"] == 200  # ended_at - started_at
    assert run["input_tokens"] == 1000
    assert run["output_tokens"] == 100
    assert run["in_out_ratio"] == pytest.approx(10.0)
    # Single-row aggregates equal the row.
    assert report["median_dur"] == pytest.approx(200)
    assert report["max_dur"] == 200
    assert report["median_in_out"] == pytest.approx(10.0)


def test_median_aggregates_over_multiple_runs(board):
    db = board(
        [
            {"task_id": "t_1", "profile": "coder", "started_at": 0, "ended_at": 100,
             "input_tokens": 400, "output_tokens": 100},   # dur 100, ratio 4
            {"task_id": "t_2", "profile": "coder", "started_at": 0, "ended_at": 300,
             "input_tokens": 600, "output_tokens": 100},   # dur 300, ratio 6
            {"task_id": "t_3", "profile": "coder", "started_at": 0, "ended_at": 200,
             "input_tokens": 800, "output_tokens": 100},   # dur 200, ratio 8
        ]
    )

    report = lane_latency.collect(db_path=db, lane="coder", limit=10)

    assert report["n"] == 3
    assert report["median_dur"] == pytest.approx(200)  # median of [100,200,300]
    assert report["max_dur"] == 300
    assert report["median_in_out"] == pytest.approx(6.0)  # median of [4,6,8]


def test_lane_filter_and_limit(board):
    rows = [
        {"task_id": f"t_keep{i}", "profile": "coder", "started_at": i, "ended_at": i + 10,
         "input_tokens": 100, "output_tokens": 10}
        for i in range(5)
    ]
    rows.append(
        {"task_id": "t_other", "profile": "verifier", "started_at": 99, "ended_at": 100,
         "input_tokens": 100, "output_tokens": 10}
    )
    db = board(rows)

    report = lane_latency.collect(db_path=db, lane="coder", limit=3)

    # Only the requested lane, capped at the limit, newest first.
    assert report["n"] == 3
    assert all(r["task_id"].startswith("t_keep") for r in report["runs"])
    assert report["runs"][0]["task_id"] == "t_keep4"  # newest started_at first


def test_runs_with_open_or_missing_endpoints_excluded(board):
    db = board(
        [
            {"task_id": "t_done", "profile": "coder", "started_at": 1000, "ended_at": 1100,
             "input_tokens": 100, "output_tokens": 10},
            # ended_at NULL → still running → excluded.
            {"task_id": "t_running", "profile": "coder", "started_at": 2000, "ended_at": None,
             "input_tokens": 100, "output_tokens": 10},
        ]
    )

    report = lane_latency.collect(db_path=db, lane="coder", limit=10)

    assert report["n"] == 1
    assert report["runs"][0]["task_id"] == "t_done"


# ---------------------------------------------------------------------------
# (iii) empty lane → clean empty output
# ---------------------------------------------------------------------------


def test_empty_lane_clean_output(board):
    db = board(
        [
            {"task_id": "t_other", "profile": "verifier", "started_at": 1000, "ended_at": 1100,
             "input_tokens": 100, "output_tokens": 10},
        ]
    )

    report = lane_latency.collect(db_path=db, lane="coder-claude", limit=10)

    assert report["n"] == 0
    assert report["runs"] == []
    assert report["median_dur"] is None
    assert report["max_dur"] is None
    assert report["median_in_out"] is None
    # No crash / no div-by-zero rendering an empty report.
    text = lane_latency.format_text(report)
    assert isinstance(text, str)
    assert "coder-claude" in text
    assert isinstance(lane_latency.format_json(report), str)


# ---------------------------------------------------------------------------
# read-only contract: opening a board never writes to it
# ---------------------------------------------------------------------------


def test_connection_is_read_only(board):
    db = board(
        [
            {"task_id": "t_x", "profile": "coder", "started_at": 1000, "ended_at": 1100,
             "input_tokens": 100, "output_tokens": 10},
        ]
    )

    conn = lane_latency.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES ('t_z', 'done', 1)")
    conn.close()



def test_backward_compatible_aliases(board):
    db = board([
        {"task_id": "t_old_api", "profile": "coder", "started_at": 10, "ended_at": 20,
         "input_tokens": 50, "output_tokens": 10},
    ])

    report = lane_latency.lane_report(db, "coder", limit=10)

    assert report["n"] == 1
    assert report["runs"][0]["in_out"] == pytest.approx(5.0)
    assert lane_latency._fmt_text(report) == lane_latency.format_text(report)

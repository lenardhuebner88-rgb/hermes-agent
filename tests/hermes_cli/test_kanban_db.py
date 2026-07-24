"""Focused tests for read-only Kanban score reports."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


def _score(conn, *, name: str, value: float, source: str, created_at: int) -> None:
    task_id = kb.create_task(conn, title=f"score-{name}", assignee="tester")
    conn.execute(
        "INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (NULL, ?, ?, ?, 'binary', ?, ?)",
        (task_id, name, value, source, created_at),
    )


def test_scores_report_aggregates_and_fills_last_eight_iso_weeks(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    now = int(datetime(2026, 7, 23, 12, tzinfo=timezone.utc).timestamp())
    current_monday = datetime(2026, 7, 20, tzinfo=timezone.utc)
    with kb.connect_closing() as conn:
        _score(
            conn,
            name="review_verdict",
            value=1.0,
            source="review_gate",
            created_at=int(current_monday.timestamp()),
        )
        _score(
            conn,
            name="review_verdict",
            value=0.0,
            source="review_gate",
            created_at=int((current_monday - timedelta(weeks=1)).timestamp()),
        )
        _score(
            conn,
            name="quality",
            value=1.0,
            source="manual",
            created_at=int((current_monday - timedelta(weeks=7)).timestamp()),
        )
        _score(
            conn,
            name="old",
            value=1.0,
            source="legacy",
            created_at=int((current_monday - timedelta(weeks=8)).timestamp()),
        )
        report = kb.scores_report(conn, now=now)

    assert report["rows_total"] == 4
    assert report["by_name"] == {"old": 1, "quality": 1, "review_verdict": 2}
    assert report["by_source"] == {"legacy": 1, "manual": 1, "review_gate": 2}
    assert report["approved_rows"] == 3
    assert report["approval_rate"] == 0.75
    assert [(week["year"], week["week"]) for week in report["weeks"]] == [
        (2026, week) for week in range(23, 31)
    ]
    assert report["weeks"][-1]["approval_rate"] == 1.0
    assert report["weeks"][-2]["approval_rate"] == 0.0
    assert report["weeks"][1]["rows_total"] == 0
    assert report["weeks"][1]["approval_rate"] is None


def test_cli_scores_json_is_read_only_and_machine_readable(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        _score(
            conn,
            name="review_verdict",
            value=1.0,
            source="review_gate",
            created_at=int(datetime.now(timezone.utc).timestamp()),
        )

    data = json.loads(run_slash("scores --json"))

    assert data["rows_total"] == 1
    assert data["approval_rate"] == 1.0
    assert len(data["weeks"]) == 8
    assert "Approval rate:" in run_slash("scores")


# ---------------------------------------------------------------------------
# Run-metric scores (F1-S1)
# ---------------------------------------------------------------------------


def _make_run(
    conn,
    task_id: str,
    *,
    started_at: int = 1000,
    ended_at: int | None = 1060,
    input_tokens: int | None = 500,
    output_tokens: int | None = 300,
    cost_usd: float | None = 0.05,
) -> int:
    """Insert a task_runs row directly and return its id."""
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, started_at, ended_at, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, 'completed', ?, ?, ?, ?, ?)",
        (task_id, started_at, ended_at, input_tokens, output_tokens, cost_usd),
    )
    return int(cur.lastrowid)


def _scores_for_run(conn, run_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT name, value FROM scores WHERE run_id = ?", (run_id,)
    ).fetchall()
    return {r["name"]: r["value"] for r in rows}


def test_record_run_metric_scores_writes_all_present_fields(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="metric-test", assignee="tester")
        run_id = _make_run(conn, task_id)
        n = kb._record_run_metric_scores(conn, run_id, task_id, created_at=1060)

        assert n == 4  # duration, tokens, cost, attempt_index
        scores = _scores_for_run(conn, run_id)
        assert scores["run_duration_seconds"] == 60.0
        assert scores["run_tokens_total"] == 800.0
        assert scores["run_cost_usd"] == 0.05
        assert scores["run_attempt_index"] == 1.0

        # Verify value_type and source
        row = conn.execute(
            "SELECT value_type, source FROM scores WHERE run_id = ? AND name = 'run_duration_seconds'",
            (run_id,),
        ).fetchone()
        assert row["value_type"] == "numeric"
        assert row["source"] == "board-metrics"


def test_record_run_metric_scores_skips_null_fields(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="null-test", assignee="tester")
        # No ended_at, no tokens, no cost
        run_id = _make_run(
            conn, task_id,
            ended_at=None, input_tokens=None, output_tokens=None, cost_usd=None,
        )
        n = kb._record_run_metric_scores(conn, run_id, task_id, created_at=1000)

        # Only attempt_index should be written (always derivable)
        assert n == 1
        scores = _scores_for_run(conn, run_id)
        assert "run_attempt_index" in scores
        assert "run_duration_seconds" not in scores
        assert "run_tokens_total" not in scores
        assert "run_cost_usd" not in scores


def test_record_run_metric_scores_dedupe(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="dedupe-test", assignee="tester")
        run_id = _make_run(conn, task_id)
        n1 = kb._record_run_metric_scores(conn, run_id, task_id, created_at=1060)
        n2 = kb._record_run_metric_scores(conn, run_id, task_id, created_at=1060)

        assert n1 == 4
        assert n2 == 0  # dedupe: no double-insert


def test_record_run_metric_scores_attempt_index_increments(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="attempt-test", assignee="tester")
        r1 = _make_run(conn, task_id, started_at=1000, ended_at=1010)
        r2 = _make_run(conn, task_id, started_at=2000, ended_at=2020)
        kb._record_run_metric_scores(conn, r1, task_id, created_at=1010)
        kb._record_run_metric_scores(conn, r2, task_id, created_at=2020)

        s1 = _scores_for_run(conn, r1)
        s2 = _scores_for_run(conn, r2)
        assert s1["run_attempt_index"] == 1.0
        assert s2["run_attempt_index"] == 2.0


def test_backfill_run_metric_scores_idempotent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="backfill-test", assignee="tester")
        _make_run(conn, task_id, started_at=1000, ended_at=1060)
        _make_run(conn, task_id, started_at=2000, ended_at=2030)
        # Run without ended_at should be skipped by backfill
        _make_run(conn, task_id, started_at=3000, ended_at=None)

        n1 = kb.backfill_run_metric_scores(conn)
        n2 = kb.backfill_run_metric_scores(conn)

        # 2 runs × 4 metrics each = 8
        assert n1 == 8
        assert n2 == 0  # idempotent


def test_end_run_wires_metric_scores(tmp_path, monkeypatch):
    """_end_run should automatically record metric scores (AC-2 wiring)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="wire-test", assignee="tester")
        # Simulate claim → creates a run row
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,)
        )
        kb.claim_task(conn, task_id)
        # Stamp some token/cost data on the active run
        run_id = kb._current_run_id(conn, task_id)
        assert run_id is not None
        conn.execute(
            "UPDATE task_runs SET input_tokens = 100, output_tokens = 50, cost_usd = 0.01 "
            "WHERE id = ?",
            (run_id,),
        )
        # End the run
        kb._end_run(conn, task_id, outcome="completed")

        scores = _scores_for_run(conn, run_id)
        assert "run_tokens_total" in scores
        assert scores["run_tokens_total"] == 150.0
        assert scores["run_cost_usd"] == 0.01
        assert "run_attempt_index" in scores

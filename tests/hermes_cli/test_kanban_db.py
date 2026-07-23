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

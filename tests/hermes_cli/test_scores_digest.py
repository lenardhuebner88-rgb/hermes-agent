"""Tests for ``hermes kanban scores --digest`` (AC-1..AC-4)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


def _seed_db(tmp_path: Path, monkeypatch) -> int:
    """Initialise an isolated kanban DB and return a fixed ``now`` epoch."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return int(datetime(2026, 7, 23, 12, tzinfo=timezone.utc).timestamp())


def _insert_verdict(
    conn, *, task_id: str, run_id: int, value: float, created_at: int
) -> None:
    conn.execute(
        "INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (?, ?, 'review_verdict', ?, 'binary', 'review_gate', ?)",
        (run_id, task_id, value, created_at),
    )


def _insert_metric(
    conn, *, run_id: int, task_id: str, name: str, value: float, created_at: int
) -> None:
    conn.execute(
        "INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (?, ?, ?, ?, 'numeric', 'board-metrics', ?)",
        (run_id, task_id, name, value, created_at),
    )


def _create_run(conn, *, task_id: str, profile: str, model: str) -> int:
    """Insert a minimal task_runs row and return its id."""
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, active_model, status, started_at) "
        "VALUES (?, ?, ?, 'done', 0)",
        (task_id, profile, model),
    )
    return int(cur.lastrowid or 0)


# ── AC-4: consistency with scores_report ──────────────────────────────


def test_digest_approval_rate_matches_scores_report(tmp_path, monkeypatch):
    """Overall approval rate and weekly values must agree with scores_report."""
    now = _seed_db(tmp_path, monkeypatch)
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t1", assignee="tester")
        rid = _create_run(conn, task_id=tid, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=tid, run_id=rid, value=1.0,
                        created_at=int(monday.timestamp()))
        _insert_verdict(conn, task_id=tid, run_id=rid, value=0.0,
                        created_at=int((monday - timedelta(weeks=1)).timestamp()))

        report = kb.scores_report(conn, now=now)
        digest = kb.scores_digest(conn, weeks=4, now=now)

    assert digest["approval_rate"] == report["approval_rate"]
    assert digest["approved_rows"] == report["approved_rows"]
    assert digest["rows_total"] == report["rows_total"]
    # Weekly values for the weeks that overlap must match
    for dw, rw in zip(digest["weekly"], report["weeks"][-len(digest["weekly"]):]):
        assert dw["rows_total"] == rw["rows_total"]
        assert dw["approved_rows"] == rw["approved_rows"]


# ── AC-1: Markdown digest output ──────────────────────────────────────


def test_digest_markdown_output(tmp_path, monkeypatch, capsys):
    now = _seed_db(tmp_path, monkeypatch)
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t1", assignee="tester")
        rid = _create_run(conn, task_id=tid, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=tid, run_id=rid, value=1.0,
                        created_at=int(monday.timestamp()))
        _insert_metric(conn, run_id=rid, task_id=tid, name="run_cost_usd",
                       value=0.042, created_at=int(monday.timestamp()))
        _insert_metric(conn, run_id=rid, task_id=tid, name="run_duration_seconds",
                       value=120.0, created_at=int(monday.timestamp()))
        _insert_metric(conn, run_id=rid, task_id=tid, name="run_attempt_index",
                       value=3.0, created_at=int(monday.timestamp()))

    out = run_slash("scores --digest --weeks 2")
    assert "**Kanban Score Digest**" in out
    assert "Approval:" in out
    assert "Scorecard:" in out
    assert "Langfuse:" in out
    # Must be compact for Discord
    assert len(out) <= 1800


def test_digest_fallback_no_metrics(tmp_path, monkeypatch):
    """When no metric scores exist, show fallback instead of error."""
    now = _seed_db(tmp_path, monkeypatch)
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t1", assignee="tester")
        rid = _create_run(conn, task_id=tid, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=tid, run_id=rid, value=1.0,
                        created_at=int(monday.timestamp()))

    out = run_slash("scores --digest")
    assert "keine vorhanden" in out


# ── AC-2: JSON twin + sidecar file ────────────────────────────────────


def test_digest_json_output_and_sidecar(tmp_path, monkeypatch):
    now = _seed_db(tmp_path, monkeypatch)
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t1", assignee="tester")
        rid = _create_run(conn, task_id=tid, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=tid, run_id=rid, value=1.0,
                        created_at=int(monday.timestamp()))

    out = run_slash("scores --digest --json")
    data = json.loads(out)
    assert data["approval_rate"] is not None
    assert "weekly" in data
    assert "by_profile" in data
    assert "by_model" in data

    # Sidecar must exist
    sidecar = tmp_path / ".hermes" / "reports" / "scores-digest-latest.json"
    assert sidecar.exists()
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_data["approval_rate"] == data["approval_rate"]


# ── AC-1: per-profile and per-model breakdown ─────────────────────────


def test_digest_by_profile_and_model(tmp_path, monkeypatch):
    now = _seed_db(tmp_path, monkeypatch)
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="t1", assignee="tester")
        r1 = _create_run(conn, task_id=t1, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=t1, run_id=r1, value=1.0,
                        created_at=int(monday.timestamp()))

        t2 = kb.create_task(conn, title="t2", assignee="tester")
        r2 = _create_run(conn, task_id=t2, profile="reviewer", model="gpt5")
        _insert_verdict(conn, task_id=t2, run_id=r2, value=0.0,
                        created_at=int(monday.timestamp()))

        digest = kb.scores_digest(conn, weeks=2, now=now)

    assert "coder" in digest["by_profile"]
    assert digest["by_profile"]["coder"]["approval_rate"] == 1.0
    assert "reviewer" in digest["by_profile"]
    assert digest["by_profile"]["reviewer"]["approval_rate"] == 0.0

    assert "qwen3" in digest["by_model"]
    assert "gpt5" in digest["by_model"]


# ── AC-1: retry hotspots ──────────────────────────────────────────────


def test_digest_retry_hotspots(tmp_path, monkeypatch):
    now = _seed_db(tmp_path, monkeypatch)
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)

    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="t1", assignee="tester")
        r1 = _create_run(conn, task_id=t1, profile="coder", model="qwen3")
        _insert_metric(conn, run_id=r1, task_id=t1, name="run_attempt_index",
                       value=5.0, created_at=int(monday.timestamp()))

        t2 = kb.create_task(conn, title="t2", assignee="tester")
        r2 = _create_run(conn, task_id=t2, profile="coder", model="qwen3")
        _insert_metric(conn, run_id=r2, task_id=t2, name="run_attempt_index",
                       value=2.0, created_at=int(monday.timestamp()))

        digest = kb.scores_digest(conn, weeks=2, now=now)

    assert len(digest["retry_hotspots"]) == 2
    assert digest["retry_hotspots"][0]["task_id"] == t1
    assert digest["retry_hotspots"][0]["max_attempt"] == 5

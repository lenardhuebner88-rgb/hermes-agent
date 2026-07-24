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
    # Digest rows_total counts verdict rows only; scores_report exposes the
    # same figure as verdict_rows (rows_total there is the raw table size).
    assert digest["rows_total"] == report["verdict_rows"]
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
        for _ in range(2):
            _insert_verdict(conn, task_id=t1, run_id=r1, value=0.0,
                            created_at=int(monday.timestamp()))

        t2 = kb.create_task(conn, title="t2", assignee="tester")
        r2 = _create_run(conn, task_id=t2, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=t2, run_id=r2, value=0.0,
                        created_at=int(monday.timestamp()))
        # Old rejection outside the window must not count
        t3 = kb.create_task(conn, title="t3", assignee="tester")
        r3 = _create_run(conn, task_id=t3, profile="coder", model="qwen3")
        _insert_verdict(conn, task_id=t3, run_id=r3, value=0.0,
                        created_at=int((monday - timedelta(weeks=10)).timestamp()))

        digest = kb.scores_digest(conn, weeks=2, now=now)

    assert len(digest["retry_hotspots"]) == 2
    assert digest["retry_hotspots"][0]["task_id"] == t1
    assert digest["retry_hotspots"][0]["rejections"] == 2


# ── Cron-script binary resolution regression (salvage t_a8a941c6 + t_d0bc5e77) ──

def test_copied_script_resolves_hermes_home_venv(tmp_path: Path) -> None:
    """Regression (t_57aaa085 + t_d0bc5e77): when the script is copied to
    ``~/.hermes/scripts/`` (the cron deployment path), it must resolve
    ``HERMES_HOME/hermes-agent/venv/bin/hermes`` (canonical editable
    install) — beating both the stale sibling ``.venv`` (old hermes_cli
    without ``--digest``) and any stale PATH ``hermes``."""
    import os
    import shutil
    import subprocess

    repo_script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "cron"
        / "scores-weekly-digest.sh"
    )

    # Simulate copied-script layout: <HERMES_HOME>/scripts/scores-weekly-digest.sh
    hermes_home = tmp_path / ".hermes"
    scripts_dir = hermes_home / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(repo_script, scripts_dir / "scores-weekly-digest.sh")

    # Create the canonical venv hermes stub (the one we WANT selected)
    venv_bin = hermes_home / "hermes-agent" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "hermes"
    stub.write_text('#!/bin/sh\necho "SELECTED:$0"\n')
    stub.chmod(0o755)

    # Create the stale sibling .venv stub (live layout has BOTH; the old
    # precedence picked .venv, whose hermes predates ``--digest``).
    stale_dot_venv_bin = hermes_home / "hermes-agent" / ".venv" / "bin"
    stale_dot_venv_bin.mkdir(parents=True)
    stale_dot_venv_hermes = stale_dot_venv_bin / "hermes"
    stale_dot_venv_hermes.write_text('#!/bin/sh\necho "STALE_DOT_VENV:$0"\n')
    stale_dot_venv_hermes.chmod(0o755)

    # Create a stale $HOME/.venv/bin/hermes that must NOT be selected
    # (in copied layout, REPO_ROOT == $HOME; the old code picked this up).
    stale_home_venv = tmp_path / ".venv" / "bin"
    stale_home_venv.mkdir(parents=True)
    stale_home_hermes = stale_home_venv / "hermes"
    stale_home_hermes.write_text('#!/bin/sh\necho "STALE_HOME:$0"\n')
    stale_home_hermes.chmod(0o755)

    # Also place a $HOME/pyproject.toml to prove that the marker alone
    # does NOT make the copied layout look like a repo (reviewer finding 2).
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'stale'\n")

    # Create a stale PATH hermes that must NOT be selected
    stale_bin = tmp_path / "stale-bin"
    stale_bin.mkdir()
    stale_hermes = stale_bin / "hermes"
    stale_hermes.write_text('#!/bin/sh\necho "STALE:$0"\n')
    stale_hermes.chmod(0o755)

    # Run under scheduler-like PATH (stale hermes first, system dirs for bash)
    env = {
        **os.environ,
        "HERMES_HOME": str(hermes_home),
        "HOME": str(tmp_path),
        "PATH": f"{stale_bin}:/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/bin/bash", str(scripts_dir / "scores-weekly-digest.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert f"SELECTED:{venv_bin / 'hermes'}" in result.stdout
    assert "STALE" not in result.stdout

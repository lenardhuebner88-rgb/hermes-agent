"""Tests for evals.golden_builder against a synthetic mini-DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evals.golden_builder import build_golden_set

# Real task_runs columns (verified 2026-07-24 against live kanban.db)
_TASK_RUNS_DDL = """\
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    profile TEXT,
    step_key TEXT,
    status TEXT NOT NULL,
    claim_lock TEXT,
    claim_expires INTEGER,
    worker_pid INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    outcome TEXT,
    summary TEXT,
    metadata TEXT,
    error TEXT,
    worker_exit_kind TEXT,
    worker_exit_code INTEGER,
    worker_protocol_state TEXT,
    worker_failure_fingerprint TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    verdict TEXT,
    cost_status TEXT,
    pre_run_commit_sha TEXT,
    requested_provider TEXT,
    requested_model TEXT,
    active_provider TEXT,
    active_model TEXT,
    model_state TEXT,
    model_source TEXT,
    model_observed_at INTEGER,
    execution_capsule TEXT
)
"""

_TASKS_DDL = """\
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    body TEXT,
    assignee TEXT,
    status TEXT,
    tenant TEXT,
    priority INTEGER DEFAULT 0
)
"""


def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """Create a mini kanban.db with the real schema.

    Each row: (run_id, task_id, verdict, summary, active_model, body)
    """
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(_TASK_RUNS_DDL)
    conn.execute(_TASKS_DDL)
    for run_id, task_id, verdict, summary, model, body in rows:
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'done')",
            (task_id, f"Task {task_id}", body),
        )
        conn.execute(
            "INSERT INTO task_runs (id, task_id, status, started_at, verdict, summary, active_model) "
            "VALUES (?, ?, 'done', 1700000000, ?, ?, ?)",
            (run_id, task_id, verdict, summary, model),
        )
    conn.commit()
    conn.close()
    return db


class TestBuildGoldenSet:
    def test_basic_output(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path, [
            (1, "t_a", "APPROVED", "All good", "gpt-4o", "AC: pass tests"),
            (2, "t_b", "REQUEST_CHANGES", "Fix lint", "claude-3", "AC: ruff clean"),
        ])
        out = tmp_path / "golden.jsonl"
        result = build_golden_set(db, out)
        assert result == out
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 2
        samples = [json.loads(l) for l in lines]
        keys = {"task_id", "run_id", "ac_text", "worker_summary", "verdict_label"}
        for s in samples:
            assert set(s.keys()) == keys

    def test_filters_empty_summary(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path, [
            (1, "t_a", "APPROVED", "Good", "m1", "AC"),
            (2, "t_b", "APPROVED", "", "m2", "AC"),
            (3, "t_c", "APPROVED", None, "m3", "AC"),
        ])
        out = tmp_path / "golden.jsonl"
        build_golden_set(db, out)
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_filters_missing_verdict(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path, [
            (1, "t_a", None, "Good", "m1", "AC"),
            (2, "t_b", "APPROVED", "Good", "m2", "AC"),
        ])
        out = tmp_path / "golden.jsonl"
        build_golden_set(db, out)
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_balancing_downsamples_majority(self, tmp_path: Path) -> None:
        rows = []
        for i in range(1, 91):
            rows.append((i, f"t_{i}", "APPROVED", f"ok {i}", "m", "AC"))
        for i in range(91, 101):
            rows.append((i, f"t_{i}", "REQUEST_CHANGES", f"fix {i}", "m", "AC"))
        db = _make_db(tmp_path, rows)
        out = tmp_path / "golden.jsonl"
        build_golden_set(db, out, max_skew=0.70)
        samples = [json.loads(l) for l in out.read_text().strip().split("\n")]
        approved = sum(1 for s in samples if s["verdict_label"] == "APPROVED")
        total = len(samples)
        assert approved / total <= 0.70 + 0.01  # small float tolerance

    def test_read_only_db(self, tmp_path: Path) -> None:
        """DB must not be modified by the builder."""
        db = _make_db(tmp_path, [
            (1, "t_a", "APPROVED", "Good", "m1", "AC"),
        ])
        before = db.read_bytes()
        out = tmp_path / "golden.jsonl"
        build_golden_set(db, out)
        assert db.read_bytes() == before

    def test_ac_text_from_task_body(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path, [
            (1, "t_x", "APPROVED", "Done", "m", "AC-1: tests pass\nAC-2: lint clean"),
        ])
        out = tmp_path / "golden.jsonl"
        build_golden_set(db, out)
        sample = json.loads(out.read_text().strip())
        assert "AC-1" in sample["ac_text"]

    def test_empty_db(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path, [])
        out = tmp_path / "golden.jsonl"
        build_golden_set(db, out)
        assert out.read_text().strip() == ""

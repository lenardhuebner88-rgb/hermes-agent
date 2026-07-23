"""Regression coverage for the blocked-notification RCA scanner."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_kanban_block_notifications.py"
_spec = importlib.util.spec_from_file_location("scan_kanban_block_notifications", _SCRIPT)
assert _spec is not None and _spec.loader is not None
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


def test_run_report_groups_blocks_by_run_context_with_explicit_buckets():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, NULL, 'done')",
        [(1, "with-context", "reviewer", "blocked"), (2, "unknown-context", None, None)],
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "with-context", 1, "submitted_for_review", json.dumps({"review_stage": 2}), 100),
            (2, "with-context", 1, "blocked", json.dumps({"kind": "review_revision"}), 101),
            (3, "unknown-context", 2, "blocked", json.dumps({"kind": "capacity"}), 102),
            (4, "unmatched-context", None, "submitted_for_review", "{}", 103),
            (5, "unmatched-context", None, "blocked", json.dumps({"kind": "dependency"}), 104),
        ],
    )

    report = scanner.run_report(conn, days=1, focus_task="with-context")

    assert report["metrics"]["blocks_by_run_outcome"] == {
        "blocked": 1,
        "unknown": 1,
        "unmatched": 1,
    }
    assert report["metrics"]["blocks_by_profile"] == {
        "reviewer": 1,
        "unknown": 1,
        "unmatched": 1,
    }
    assert report["metrics"]["blocks_by_review_stage"] == {
        "2": 1,
        "unknown": 1,
        "unmatched": 1,
    }

"""Shared helpers for split kanban hermes_cli tests.

Pure moves from monolith test modules — no behavior changes.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

def _write_state_session(
    home, session_id, *,
    input_tokens=None, output_tokens=None,
    actual_cost=None, estimated_cost=None,
    model=None, billing_provider=None,
    cache_read_tokens=None, cache_write_tokens=None,
):
    """Create a minimal state.db with a single sessions row (K5b fixture)."""
    db = Path(home) / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER, "
            "actual_cost_usd REAL, estimated_cost_usd REAL, "
            "model TEXT, billing_provider TEXT, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions "
            "(id, input_tokens, output_tokens, actual_cost_usd, estimated_cost_usd, "
            "model, billing_provider, cache_read_tokens, cache_write_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, input_tokens, output_tokens, actual_cost, estimated_cost,
                model, billing_provider, cache_read_tokens, cache_write_tokens,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_ended_run(conn, task_id, *, profile, metadata, ended_at=None):
    """Insert a closed run row directly (cost_usd NULL). Returns run id."""
    import json as _json
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, ?, 'done', ?, ?, 'completed', ?)",
        (
            task_id, profile, now, ended_at if ended_at is not None else now,
            _json.dumps(metadata) if metadata is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _write_claude_result_log(task_id, *, total_cost_usd=0.28, input_tokens=11529,
                             cache_creation=24778, cache_read=93776,
                             output_tokens=861, session_id="sess-claude-1"):
    """Append a realistic ``claude -p --output-format json`` result line to the
    per-task worker log (the shape the live CLI v2.1.x emits)."""
    import json as _json
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 3, "result": "done", "stop_reason": "end_turn",
        "session_id": session_id, "total_cost_usd": total_cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
        },
    }
    with open(log_dir / f"{task_id}.log", "a", encoding="utf-8") as fh:
        fh.write("some non-json worker chatter\n")
        fh.write(_json.dumps(result) + "\n")


def _write_session_rows(db_path, rows):
    """Create a realistic ``state.db`` sessions table (with cwd/source/
    started_at) and insert ``rows`` (list of dicts). Used by the
    session-correlation backfill tests."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "actual_cost_usd REAL, estimated_cost_usd REAL, cwd TEXT, "
            "model TEXT, billing_provider TEXT, cost_status TEXT, "
            "openrouter_generation_id TEXT, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )
        for r in rows:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, ended_at, "
                "input_tokens, output_tokens, actual_cost_usd, "
                "estimated_cost_usd, cwd, model, billing_provider, "
                "cost_status, openrouter_generation_id, cache_read_tokens, cache_write_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["id"], r.get("source", "cli"), r.get("started_at"),
                    r.get("ended_at"), r.get("input_tokens"),
                    r.get("output_tokens"), r.get("actual_cost_usd"),
                    r.get("estimated_cost_usd"), r.get("cwd"), r.get("model"),
                    r.get("billing_provider"), r.get("cost_status"),
                    r.get("openrouter_generation_id"),
                    r.get("cache_read_tokens"), r.get("cache_write_tokens"),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_run_window(conn, task_id, *, profile, started_at, ended_at,
                       outcome="completed", metadata=None):
    """Insert a closed run with explicit start/end window (cost_usd NULL)."""
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, ?, 'done', ?, ?, ?, ?)",
        (
            task_id, profile, started_at, ended_at, outcome,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _seed_completed_run(conn, task_id, profile, ended_at, summary):
    """Insert a completed task_runs row for role-history."""
    conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, status, started_at, ended_at, outcome, summary
        ) VALUES (?, ?, 'done', ?, ?, 'completed', ?)
        """,
        (task_id, profile, ended_at - 1, ended_at, summary),
    )


def _operator_escalations(conn, task_id):
    return [
        e for e in kb.list_events(conn, task_id)
        if e.kind == kb.OPERATOR_ESCALATION_EVENT
    ]


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: set a task's status directly."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _latest_run_verdict(conn, task_id):
    row = conn.execute(
        "SELECT verdict FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["verdict"] if row else None


def _kinds_for(task_id, result):
    return [d["kind"] for d in result["decisions"] if d["task_id"] == task_id]


def _escalation_event(conn, task_id):
    """Return the (single) operator_escalation Event for a task."""
    return next(
        e for e in kb.list_events(conn, task_id)
        if e.kind == kb.OPERATOR_ESCALATION_EVENT
    )


def _heiler_events(conn, task_id):
    return [
        e for e in kb.list_events(conn, task_id)
        if e.kind == kb.HEILER_CLASSIFICATION_EVENT
    ]


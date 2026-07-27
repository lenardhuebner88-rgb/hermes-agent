"""Fork-owned, idempotent materialization of Kanban analytics scores.

Scores are inserted with ``INSERT … WHERE NOT EXISTS`` rather than a schema
constraint so historical databases containing duplicate score rows remain
readable.  The natural key is ``(task_id, run_id, name)``.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from agent.usage_pricing import normalize_usage
from hermes_constants import get_hermes_home


_SOURCE = "score_materialization"
_TERMINAL_TASK_STATUSES = ("done", "archived")


def _default_state_db_paths() -> list[Path]:
    home = get_hermes_home()
    paths = [home / "state.db"]
    profiles = home / "profiles"
    if profiles.exists():
        paths.extend(sorted(profiles.glob("*/state.db")))
    return paths


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _session_usage(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """Read the union of hub and per-profile state.db instances, never writing them."""
    usages: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        state = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            columns = _table_columns(state, "sessions")
            session_key = "session_id" if "session_id" in columns else "id"
            if not session_key or not {"input_tokens", "output_tokens"} <= columns:
                continue
            wanted = [session_key, "model", "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens"]
            available = [column for column in wanted if column in columns]
            for row in state.execute(f"SELECT {', '.join(available)} FROM sessions"):
                data = dict(zip(available, row, strict=True))
                session_id = str(data[session_key])
                # Main state.db wins over a mirrored profile entry; otherwise this
                # is a true UNION rather than a sum of duplicate sessions.
                usages.setdefault(session_id, data)
        finally:
            state.close()
    return usages


def _insert_score(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    name: str,
    value: float | str,
    value_type: str,
    created_at: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at)
        SELECT ?, ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM scores
            WHERE task_id IS ? AND run_id IS ? AND name = ?
        )
        """,
        (run_id, task_id, name, value, value_type, _SOURCE, created_at, task_id, run_id, name),
    )
    return int(cursor.rowcount)


def _latest_run_id(conn: sqlite3.Connection, task_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,)
    ).fetchone()
    return int(row[0]) if row else None


def _reasoning_is_output_subset(usage: dict[str, Any]) -> bool:
    """Use the canonical normalizer before deciding whether reasoning inflates output."""
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
    normalized = normalize_usage(
        SimpleNamespace(
            input_tokens=input_tokens + cache_read,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cache_read),
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
        api_mode="codex_responses",
    )
    return normalized.reasoning_tokens <= normalized.output_tokens


def _metadata_dict(raw: object) -> dict[str, Any]:
    """Parse the persisted ``task_runs.metadata`` JSON object defensively."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _finite_nonnegative_number(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _effective_run_cost(cost_usd: object, metadata: object) -> float | None:
    """Return actual plus equivalent cost, or ``None`` when the run is blind."""
    actual = _finite_nonnegative_number(cost_usd)
    equivalent = _finite_nonnegative_number(
        _metadata_dict(metadata).get("cost_usd_equivalent")
    )
    if not any(value is not None and value > 0 for value in (actual, equivalent)):
        return None
    return (actual or 0.0) + (equivalent or 0.0)


def materialize_scores(
    conn: sqlite3.Connection,
    *,
    state_db_paths: Iterable[Path] | None = None,
    created_at: int | None = None,
) -> dict[str, int | bool]:
    """Materialize score rows without modifying any table except ``scores``."""
    now = int(time.time()) if created_at is None else created_at
    inserted = 0
    queue_latency_excluded = 0
    usage_by_session = _session_usage(_default_state_db_paths() if state_db_paths is None else state_db_paths)
    reasoning_subset = all(_reasoning_is_output_subset(usage) for usage in usage_by_session.values())

    # Task-event scores are task-level and use the final run trace when available.
    veto_tasks = conn.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE kind = 'freigabe_vetoed'"
    ).fetchall()
    for (task_id,) in veto_tasks:
        inserted += _insert_score(conn, task_id=task_id, run_id=_latest_run_id(conn, task_id), name="operator_veto", value=1.0, value_type="binary", created_at=now)

    approved_tasks = conn.execute(
        """
        SELECT task_id, COUNT(*) FROM task_events
        WHERE kind = 'submitted_for_review'
          AND EXISTS (SELECT 1 FROM task_events approved
                      WHERE approved.task_id = task_events.task_id
                        AND approved.kind = 'review_approved')
        GROUP BY task_id
        """
    ).fetchall()
    for task_id, count in approved_tasks:
        inserted += _insert_score(conn, task_id=task_id, run_id=_latest_run_id(conn, task_id), name="review_submissions_to_approval", value=float(count), value_type="numeric", created_at=now)

    runs = conn.execute(
        """
        SELECT task_runs.id, task_runs.task_id, task_runs.outcome, tasks.session_id,
               task_runs.ended_at, task_runs.cost_usd, task_runs.metadata
        FROM task_runs JOIN tasks ON tasks.id = task_runs.task_id
        ORDER BY task_runs.id
        """
    ).fetchall()
    for run_id, task_id, outcome, session_id, ended_at, cost_usd, metadata in runs:
        if outcome:
            inserted += _insert_score(conn, task_id=task_id, run_id=run_id, name="run_outcome_kind", value=str(outcome), value_type="categorical", created_at=now)
        if ended_at is not None:
            effective_cost = _effective_run_cost(cost_usd, metadata)
            if effective_cost is not None:
                inserted += _insert_score(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    name="run_cost_effective_usd",
                    value=effective_cost,
                    value_type="numeric",
                    created_at=now,
                )
        usage = usage_by_session.get(str(session_id)) if session_id else None
        if usage:
            input_tokens = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
            inserted += _insert_score(conn, task_id=task_id, run_id=run_id, name="cache_read_tokens", value=float(cache_read), value_type="numeric", created_at=now)
            denominator = input_tokens + cache_read
            if denominator:
                inserted += _insert_score(conn, task_id=task_id, run_id=run_id, name="cache_hit_ratio", value=cache_read / denominator, value_type="numeric", created_at=now)
            if reasoning_tokens:
                # Reasoning is a subset of output when the canonical check above
                # says so; it is recorded as a bucket, never added to output.
                inserted += _insert_score(conn, task_id=task_id, run_id=run_id, name="reasoning_tokens", value=float(reasoning_tokens), value_type="numeric", created_at=now)

    tasks = conn.execute("SELECT id, status, created_at, due_at, wait_for FROM tasks").fetchall()
    for task_id, status, task_created, due_at, wait_for in tasks:
        last_run_id = _latest_run_id(conn, task_id)
        if last_run_id is not None:
            inserted += _insert_score(conn, task_id=task_id, run_id=last_run_id, name="task_outcome", value=str(status), value_type="categorical", created_at=now)
        if status in _TERMINAL_TASK_STATUSES:
            run_count = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0]
            inserted += _insert_score(conn, task_id=task_id, run_id=last_run_id, name="task_runs_to_done", value=float(run_count), value_type="numeric", created_at=now)
        if due_at is not None or wait_for is not None:
            queue_latency_excluded += 1
            continue
        first = conn.execute(
            "SELECT started_at, id FROM task_runs WHERE task_id = ? ORDER BY started_at ASC, id ASC LIMIT 1", (task_id,)
        ).fetchone()
        if first and task_created is not None:
            inserted += _insert_score(conn, task_id=task_id, run_id=int(first[1]), name="queue_latency_seconds", value=float(max(0, int(first[0]) - int(task_created))), value_type="numeric", created_at=now)

    return {
        "inserted": inserted,
        "queue_latency_excluded": queue_latency_excluded,
        "state_db_count": len(list(state_db_paths)) if state_db_paths is not None else len(_default_state_db_paths()),
        "reasoning_tokens_subset_of_output": reasoning_subset,
    }

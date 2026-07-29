"""Exact, queryable runtime facts for Kanban worker runs.

This module deliberately stores only events observed at lifecycle boundaries.
All timestamps are Unix epoch milliseconds in UTC; unavailable observations
are omitted rather than inferred from adjacent events.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any

EVENT_KINDS = frozenset(
    {
        "queued",
        "claimed",
        "spawn_started",
        "process_started",
        "first_llm_request",
        "first_token",
        "ended",
    }
)
SPAWN_IDENTITY_METADATA_FIELDS = frozenset(
    {
        "worker_runtime",
        "route_provider",
        "model_source",
        "provider",
        "model",
        "fallback_providers",
        "subscription",
        "billing_mode",
        "cost_source",
    }
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS worker_run_timeline_events (
    task_run_id    INTEGER NOT NULL,
    event_kind     TEXT NOT NULL CHECK (event_kind IN (
        'queued', 'claimed', 'spawn_started', 'process_started',
        'first_llm_request', 'first_token', 'ended'
    )),
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
    source         TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    board          TEXT NOT NULL,
    chain_root_id  TEXT,
    profile        TEXT,
    PRIMARY KEY (task_run_id, event_kind)
);
CREATE INDEX IF NOT EXISTS idx_worker_run_timeline_task_time
    ON worker_run_timeline_events(task_id, observed_at_ms);

CREATE TABLE IF NOT EXISTS worker_run_runtime_locators (
    task_run_id   INTEGER PRIMARY KEY,
    locator_type  TEXT NOT NULL CHECK (locator_type IN ('pid', 'tmux_pane')),
    pid           INTEGER NOT NULL,
    tmux_session  TEXT,
    tmux_window   TEXT,
    tmux_pane     TEXT,
    task_id       TEXT NOT NULL,
    board         TEXT NOT NULL,
    chain_root_id TEXT,
    profile       TEXT
);

CREATE TABLE IF NOT EXISTS worker_run_terminal_facts (
    task_run_id INTEGER PRIMARY KEY,
    worker_exit_kind TEXT NOT NULL,
    worker_exit_code INTEGER,
    worker_protocol_state TEXT NOT NULL,
    task_outcome TEXT,
    end_reason TEXT NOT NULL,
    task_id TEXT NOT NULL,
    board TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_run_retry_links (
    task_run_id INTEGER PRIMARY KEY,
    retry_of_task_run_id INTEGER NOT NULL,
    retry_class TEXT NOT NULL CHECK (
        retry_class IN ('auto', 'integration', 'transient', 'operator')
    ),
    triggering_event_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    board TEXT NOT NULL,
    CHECK (task_run_id != retry_of_task_run_id)
);
CREATE INDEX IF NOT EXISTS idx_worker_run_retry_predecessor
    ON worker_run_retry_links(retry_of_task_run_id);

CREATE TABLE IF NOT EXISTS pending_worker_run_retry_links (
    task_id TEXT PRIMARY KEY,
    retry_of_task_run_id INTEGER NOT NULL,
    retry_class TEXT NOT NULL,
    triggering_event_id INTEGER NOT NULL,
    board TEXT NOT NULL
);
"""

RETRY_CLASSES = frozenset({"auto", "integration", "transient", "operator"})


def init_schema(conn: sqlite3.Connection) -> None:
    """Install the fork-owned runtime-facts tables for an existing board."""
    conn.executescript(_SCHEMA_SQL)


def preserve_spawn_identity_metadata(
    conn: sqlite3.Connection,
    *,
    task_run_id: int,
    terminal_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge immutable claim-time identity into terminal run metadata."""
    merged = dict(terminal_metadata or {})
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE id = ?", (int(task_run_id),)
    ).fetchone()
    if not row or not row[0]:
        return merged
    try:
        spawn_metadata = json.loads(row[0])
    except (TypeError, ValueError):
        return merged
    if not isinstance(spawn_metadata, dict):
        return merged
    for field in SPAWN_IDENTITY_METADATA_FIELDS:
        if field in spawn_metadata:
            merged[field] = spawn_metadata[field]
    return merged


def _runtime_dimensions(conn: sqlite3.Connection, task_run_id: int, board: str | None) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.task_id, r.profile
          FROM task_runs AS r
         WHERE r.id = ?
        """,
        (int(task_run_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown task run {task_run_id}")
    task_id = str(row["task_id"])
    chain_root = conn.execute(
        """
        WITH RECURSIVE ancestors(id) AS (
            SELECT ?
            UNION
            SELECT links.parent_id
              FROM task_links AS links
              JOIN ancestors ON links.child_id = ancestors.id
        )
        SELECT ancestors.id
          FROM ancestors
         WHERE NOT EXISTS (
             SELECT 1 FROM task_links AS child_links
              WHERE child_links.child_id = ancestors.id
         )
         ORDER BY ancestors.id
         LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return {
        "task_id": task_id,
        "board": board or os.environ.get("HERMES_KANBAN_BOARD") or "default",
        "chain_root_id": str(chain_root[0]) if chain_root is not None else task_id,
        "profile": row["profile"],
    }


def record_terminal_facts(
    conn: sqlite3.Connection,
    *,
    task_run_id: int,
    worker_exit_kind: str,
    worker_exit_code: int | None,
    worker_protocol_state: str,
    end_reason: str,
    board: str | None = None,
) -> None:
    """Persist process, protocol, lifecycle outcome, and end reason separately."""
    if not worker_exit_kind or not worker_protocol_state or not end_reason:
        raise ValueError("terminal fact kind, protocol state, and end reason are required")
    dimensions = _runtime_dimensions(conn, task_run_id, board)
    run = conn.execute(
        "SELECT outcome, ended_at FROM task_runs WHERE id = ?", (int(task_run_id),)
    ).fetchone()
    if run is None or run["ended_at"] is None:
        raise ValueError(f"task run {task_run_id} has not ended")
    conn.execute(
        """
        INSERT INTO worker_run_terminal_facts (
            task_run_id, worker_exit_kind, worker_exit_code,
            worker_protocol_state, task_outcome, end_reason, task_id, board
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_run_id) DO UPDATE SET
            worker_exit_kind = excluded.worker_exit_kind,
            worker_exit_code = excluded.worker_exit_code,
            worker_protocol_state = excluded.worker_protocol_state,
            task_outcome = excluded.task_outcome,
            end_reason = excluded.end_reason
        """,
        (
            int(task_run_id), worker_exit_kind, worker_exit_code,
            worker_protocol_state, run["outcome"], end_reason,
            dimensions["task_id"], dimensions["board"],
        ),
    )


def get_terminal_facts(
    conn: sqlite3.Connection, *, task_run_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM worker_run_terminal_facts WHERE task_run_id = ?",
        (int(task_run_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def _retry_link_diagnostic(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    finding: str,
    details: Mapping[str, Any],
) -> None:
    import json

    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            task_id,
            "retry_link_rejected",
            json.dumps({"finding": finding, **details}, sort_keys=True),
            int(time.time()),
        ),
    )


def record_retry_link(
    conn: sqlite3.Connection,
    *,
    task_run_id: int,
    retry_of_task_run_id: int,
    retry_class: str,
    triggering_event_id: int,
    board: str | None = None,
) -> None:
    """Attach a new run to an exact predecessor and exact trigger event."""
    dimensions = _runtime_dimensions(conn, task_run_id, board)
    task_id = dimensions["task_id"]
    details = {
        "task_run_id": int(task_run_id),
        "retry_of_task_run_id": int(retry_of_task_run_id),
        "retry_class": retry_class,
        "triggering_event_id": int(triggering_event_id),
    }
    predecessor = conn.execute(
        "SELECT task_id, ended_at FROM task_runs WHERE id = ?",
        (int(retry_of_task_run_id),),
    ).fetchone()
    event = conn.execute(
        "SELECT task_id FROM task_events WHERE id = ?", (int(triggering_event_id),)
    ).fetchone()
    finding = None
    if retry_class not in RETRY_CLASSES:
        finding = "unsupported_retry_class"
    elif predecessor is None:
        finding = "missing_predecessor"
    elif predecessor["task_id"] != task_id:
        finding = "foreign_task_predecessor"
    elif predecessor["ended_at"] is None:
        finding = "active_predecessor"
    elif int(retry_of_task_run_id) >= int(task_run_id):
        finding = "cyclic_or_non_previous_predecessor"
    elif event is None or event["task_id"] != task_id:
        finding = "foreign_or_missing_trigger_event"
    if finding is not None:
        _retry_link_diagnostic(conn, task_id=task_id, finding=finding, details=details)
        raise ValueError(f"retry link rejected: {finding}")
    conn.execute(
        """
        INSERT INTO worker_run_retry_links (
            task_run_id, retry_of_task_run_id, retry_class,
            triggering_event_id, task_id, board
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_run_id) DO NOTHING
        """,
        (
            int(task_run_id), int(retry_of_task_run_id), retry_class,
            int(triggering_event_id), task_id, dimensions["board"],
        ),
    )


def get_retry_link(conn: sqlite3.Connection, *, task_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM worker_run_retry_links WHERE task_run_id = ?",
        (int(task_run_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def stage_retry_link(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    retry_of_task_run_id: int,
    retry_class: str,
    triggering_event_id: int,
    board: str | None = None,
) -> None:
    """Stage exact lifecycle evidence for the next run; this owns no retry policy."""
    if retry_class not in RETRY_CLASSES:
        raise ValueError(f"unsupported retry class: {retry_class}")
    conn.execute(
        """
        INSERT INTO pending_worker_run_retry_links (
            task_id, retry_of_task_run_id, retry_class, triggering_event_id, board
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            retry_of_task_run_id = excluded.retry_of_task_run_id,
            retry_class = excluded.retry_class,
            triggering_event_id = excluded.triggering_event_id,
            board = excluded.board
        """,
        (
            task_id, int(retry_of_task_run_id), retry_class,
            int(triggering_event_id),
            board or os.environ.get("HERMES_KANBAN_BOARD") or "default",
        ),
    )


def consume_staged_retry_link(
    conn: sqlite3.Connection, *, task_id: str, task_run_id: int
) -> None:
    pending = conn.execute(
        "SELECT * FROM pending_worker_run_retry_links WHERE task_id = ?", (task_id,)
    ).fetchone()
    if pending is None:
        return
    record_retry_link(
        conn,
        task_run_id=task_run_id,
        retry_of_task_run_id=int(pending["retry_of_task_run_id"]),
        retry_class=str(pending["retry_class"]),
        triggering_event_id=int(pending["triggering_event_id"]),
        board=str(pending["board"]),
    )
    conn.execute("DELETE FROM pending_worker_run_retry_links WHERE task_id = ?", (task_id,))


def record_event(
    conn: sqlite3.Connection,
    *,
    task_run_id: int,
    event_kind: str,
    observed_at_ms: int,
    source: str,
    board: str | None = None,
    preserve_first: bool = False,
) -> bool:
    """Persist one observed event without inventing or backdating facts.

    A run has at most one row per event kind.  A duplicate timestamp is a
    no-op; an out-of-order older callback cannot overwrite a newer observation.
    ``preserve_first`` is for semantic firsts (first request/token), which
    retain their first observed occurrence for the entire worker run.
    """
    if event_kind not in EVENT_KINDS:
        raise ValueError(f"unsupported runtime event kind: {event_kind}")
    if not source:
        raise ValueError("runtime event source is required")
    if observed_at_ms < 0:
        raise ValueError("runtime timestamps must be UTC epoch milliseconds")
    dimensions = _runtime_dimensions(conn, task_run_id, board)
    cursor = conn.execute(
        """
        INSERT INTO worker_run_timeline_events (
            task_run_id, event_kind, observed_at_ms, source,
            task_id, board, chain_root_id, profile
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_run_id, event_kind) DO UPDATE SET
            observed_at_ms = excluded.observed_at_ms,
            source = excluded.source
        WHERE ? = 0
          AND excluded.observed_at_ms > worker_run_timeline_events.observed_at_ms
        """,
        (
            int(task_run_id),
            event_kind,
            int(observed_at_ms),
            source,
            dimensions["task_id"],
            dimensions["board"],
            dimensions["chain_root_id"],
            dimensions["profile"],
            int(preserve_first),
        ),
    )
    return cursor.rowcount == 1


def record_claimed_timeline(
    conn: sqlite3.Connection,
    *,
    task_run_id: int,
    claimed_at_seconds: int,
    board: str | None = None,
) -> None:
    """Record exact queue creation and claim facts after a run has an id.

    ``tasks.created_at`` is the only retained queue-entry source available for
    a newly created task; retry-specific queue entries are intentionally absent
    until an explicit boundary records them.
    """
    dimensions = _runtime_dimensions(conn, task_run_id, board)
    created_row = conn.execute(
        "SELECT created_at FROM tasks WHERE id = ?", (dimensions["task_id"],)
    ).fetchone()
    if created_row is not None and created_row["created_at"] is not None:
        record_event(
            conn,
            task_run_id=task_run_id,
            event_kind="queued",
            observed_at_ms=int(created_row["created_at"]) * 1000,
            source="tasks.created_at",
            board=board,
        )
    record_event(
        conn,
        task_run_id=task_run_id,
        event_kind="claimed",
        observed_at_ms=int(claimed_at_seconds) * 1000,
        source="kanban_db.claim_task",
        board=board,
    )


def _current_tmux_locator(pane: str) -> tuple[str, str, str] | None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}\t#{window_index}\t#{pane_id}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.rstrip("\n").split("\t")
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def record_locator(
    conn: sqlite3.Connection,
    *,
    task_run_id: int,
    pid: int,
    env: Mapping[str, str] | None = None,
    tmux_display: Callable[[str], tuple[str, str, str] | None] = _current_tmux_locator,
    board: str | None = None,
) -> bool:
    """Store a bounded runtime locator, never argv, environment, or logs."""
    dimensions = _runtime_dimensions(conn, task_run_id, board)
    pane = (env if env is not None else os.environ).get("TMUX_PANE")
    tmux = tmux_display(pane) if pane else None
    if tmux is None:
        locator = ("pid", int(pid), None, None, None)
    else:
        session, window, actual_pane = tmux
        locator = ("tmux_pane", int(pid), session, window, actual_pane)
    cursor = conn.execute(
        """
        INSERT INTO worker_run_runtime_locators (
            task_run_id, locator_type, pid, tmux_session, tmux_window, tmux_pane,
            task_id, board, chain_root_id, profile
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_run_id) DO NOTHING
        """,
        (int(task_run_id), *locator, *dimensions.values()),
    )
    return cursor.rowcount == 1


def get_locator(conn: sqlite3.Connection, *, task_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT locator_type, pid, tmux_session, tmux_window, tmux_pane
          FROM worker_run_runtime_locators
         WHERE task_run_id = ?
        """,
        (int(task_run_id),),
    ).fetchone()
    if row is None:
        return None
    result = {"locator_type": row["locator_type"], "pid": row["pid"]}
    if row["locator_type"] == "tmux_pane":
        result.update(
            tmux_session=row["tmux_session"],
            tmux_window=row["tmux_window"],
            tmux_pane=row["tmux_pane"],
        )
    return result


def list_timeline(conn: sqlite3.Connection, *, task_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT task_run_id, event_kind, observed_at_ms, source,
               task_id, board, chain_root_id, profile
          FROM worker_run_timeline_events
         WHERE task_run_id = ?
         ORDER BY observed_at_ms, event_kind
        """,
        (int(task_run_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def calculate_latencies(conn: sqlite3.Connection, *, task_run_id: int) -> dict[str, int | None]:
    """Return only duration pairs whose two recorded observations exist."""
    points = {
        row["event_kind"]: row["observed_at_ms"]
        for row in list_timeline(conn, task_run_id=task_run_id)
    }

    def delta(start: str, end: str) -> int | None:
        if start not in points or end not in points:
            return None
        return int(points[end]) - int(points[start])

    return {
        "queue_to_claimed_ms": delta("queued", "claimed"),
        "claim_to_spawn_started_ms": delta("claimed", "spawn_started"),
        "spawn_to_process_started_ms": delta("spawn_started", "process_started"),
        "request_to_first_token_ms": delta("first_llm_request", "first_token"),
        "run_duration_ms": delta("claimed", "ended"),
    }


def record_event_from_environment(
    event_kind: str,
    *,
    source: str,
    env: Mapping[str, str] | None = None,
    observed_at_ms: int | None = None,
    preserve_first: bool = False,
) -> bool:
    """Best-effort worker-process event write using its injected run identity."""
    values = env if env is not None else os.environ
    raw_run_id = values.get("HERMES_KANBAN_RUN_ID")
    db_path = values.get("HERMES_KANBAN_DB")
    if not raw_run_id or not db_path:
        return False
    try:
        run_id = int(raw_run_id)
    except ValueError:
        return False
    try:
        with sqlite3.connect(db_path, timeout=1) as conn:
            conn.row_factory = sqlite3.Row
            record_event(
                conn,
                task_run_id=run_id,
                event_kind=event_kind,
                observed_at_ms=(
                    int(time.time() * 1000) if observed_at_ms is None else int(observed_at_ms)
                ),
                source=source,
                board=values.get("HERMES_KANBAN_BOARD"),
                preserve_first=preserve_first,
            )
        return True
    except (OSError, sqlite3.Error, ValueError):
        return False

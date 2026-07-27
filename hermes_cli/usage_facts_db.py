"""SQLite persistence for fork-owned agent usage facts and redacted traces.

This module is deliberately independent from the Kanban database.  It stores
nullable observations: absence means unknown, never a synthetic zero.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from agent.redact import redact_sensitive_text

DEFAULT_USAGE_FACTS_DB = Path("/mnt/data/hermes-observability/usage_facts.db")
DEFAULT_TRACE_RETENTION_DAYS = 180

RUN_FACT_COLUMNS = (
    "origin",
    "provider",
    "model",
    "requested_provider",
    "requested_model",
    "model_source",
    "fallback_depth",
    "lane",
    "profile",
    "wall_ms",
    "call_kind",
    "billing_mode",
    "serving_tier",
    "reasoning_effort",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "tool_call_count",
    "tool_output_chars",
    "finish_reason",
    "error_type",
    "first_token_ms",
    "duration_ms",
    "tool_duration_ms",
    "context_window_limit",
    "context_window_limit_source",
    "context_window_used",
    "llm_call_count",
    "temperature",
    "top_p",
    "captured_at",
    "source",
)

LLM_CALL_COLUMNS = (
    "origin",
    "provider",
    "model",
    "requested_model",
    "model_source",
    "serving_tier",
    "reasoning_effort",
    "response_id",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "finish_reason",
    "error_type",
    "first_token_ms",
    "duration_ms",
    "tool_duration_ms",
    "context_window_used",
    "tool_call_count",
    "tool_output_chars",
    "temperature",
    "top_p",
)

_SOURCE_RANK = {"unknown": 0, "derived": 1, "measured": 2}
_VALID_ORIGINS = frozenset(
    {
        "hermes_agent",
        "hermes_aux",
        "claude_code",
        "codex_cli",
        "kimi_cli",
        "grok_cli",
        "qwen_cli",
    }
)
_SECRET_ENV_MARKERS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "CREDENTIAL")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_usage_facts (
    run_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'hermes_agent',
    provider TEXT,
    model TEXT,
    requested_provider TEXT,
    requested_model TEXT,
    model_source TEXT,
    fallback_depth INTEGER,
    lane TEXT,
    profile TEXT,
    wall_ms INTEGER,
    call_kind TEXT,
    billing_mode TEXT,
    serving_tier TEXT,
    reasoning_effort TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    tool_call_count INTEGER,
    tool_output_chars INTEGER,
    finish_reason TEXT,
    error_type TEXT,
    first_token_ms REAL,
    duration_ms REAL,
    tool_duration_ms INTEGER,
    context_window_limit INTEGER,
    context_window_limit_source TEXT
        CHECK (context_window_limit_source IN ('derived')),
    context_window_used INTEGER,
    llm_call_count INTEGER,
    temperature REAL,
    top_p REAL,
    captured_at TEXT,
    source TEXT NOT NULL DEFAULT 'unknown'
        CHECK (source IN ('measured', 'derived', 'unknown'))
);

CREATE TABLE IF NOT EXISTS run_llm_calls (
    run_id TEXT NOT NULL,
    call_index INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'hermes_agent',
    provider TEXT,
    model TEXT,
    requested_model TEXT,
    model_source TEXT,
    serving_tier TEXT,
    reasoning_effort TEXT,
    response_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    finish_reason TEXT,
    error_type TEXT,
    first_token_ms REAL,
    duration_ms REAL,
    tool_duration_ms INTEGER,
    context_window_used INTEGER,
    tool_call_count INTEGER,
    tool_output_chars INTEGER,
    temperature REAL,
    top_p REAL,
    PRIMARY KEY (run_id, call_index)
);

CREATE TABLE IF NOT EXISTS run_traces (
    run_id TEXT NOT NULL,
    call_index INTEGER,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_fingerprint TEXT,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_traces_captured_at
    ON run_traces(captured_at);
CREATE INDEX IF NOT EXISTS idx_run_traces_run_call
    ON run_traces(run_id, call_index);
"""

_FACT_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_run_usage_facts_rollup
        ON run_usage_facts(
            origin, profile, lane, model, provider, billing_mode
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_run_usage_facts_origin_model
        ON run_usage_facts(origin, model)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_run_usage_facts_captured_at
        ON run_usage_facts(captured_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_run_llm_calls_origin_model
        ON run_llm_calls(origin, model)
    """,
)

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_IDENTITIES: dict[Path, tuple[int, int]] = {}


def utc_now_iso() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def usage_facts_db_path(path: Optional[os.PathLike[str] | str] = None) -> Path:
    """Resolve the explicit path, environment override, or /mnt/data default."""
    if path is not None:
        return Path(path)
    configured = os.environ.get("HERMES_USAGE_FACTS_DB")
    return Path(configured) if configured else DEFAULT_USAGE_FACTS_DB


def _connect(path: Optional[os.PathLike[str] | str] = None) -> sqlite3.Connection:
    resolved = usage_facts_db_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        identity = resolved.stat()
        schema_key = resolved.resolve()
        schema_identity = (identity.st_dev, identity.st_ino)
        with _SCHEMA_LOCK:
            if _SCHEMA_IDENTITIES.get(schema_key) != schema_identity:
                conn.executescript(_SCHEMA)
                for table in ("run_usage_facts", "run_llm_calls"):
                    columns = {
                        row[1]
                        for row in conn.execute(f"PRAGMA table_info({table})")
                    }
                    if "tool_output_chars" not in columns:
                        conn.execute(
                            f"ALTER TABLE {table} "
                            "ADD COLUMN tool_output_chars INTEGER"
                        )
                run_fact_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(run_usage_facts)"
                    )
                }
                if "context_window_limit_source" not in run_fact_columns:
                    conn.execute(
                        "ALTER TABLE run_usage_facts "
                        "ADD COLUMN context_window_limit_source TEXT "
                        "CHECK (context_window_limit_source IN ('derived'))"
                    )
                additions = {
                    "run_usage_facts": (
                        (
                            "origin",
                            "TEXT NOT NULL DEFAULT 'hermes_agent'",
                        ),
                        ("profile", "TEXT"),
                        ("wall_ms", "INTEGER"),
                        ("call_kind", "TEXT"),
                        ("tool_duration_ms", "INTEGER"),
                    ),
                    "run_llm_calls": (
                        (
                            "origin",
                            "TEXT NOT NULL DEFAULT 'hermes_agent'",
                        ),
                        ("tool_duration_ms", "INTEGER"),
                    ),
                }
                for table, table_additions in additions.items():
                    columns = {
                        row[1]
                        for row in conn.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    for column, declaration in table_additions:
                        if column not in columns:
                            conn.execute(
                                f"ALTER TABLE {table} ADD COLUMN "
                                f"{column} {declaration}"
                            )
                trace_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(run_traces)")
                }
                if "message_fingerprint" not in trace_columns:
                    conn.execute(
                        "ALTER TABLE run_traces "
                        "ADD COLUMN message_fingerprint TEXT"
                    )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_run_traces_message_fingerprint "
                    "ON run_traces(run_id, message_fingerprint) "
                    "WHERE message_fingerprint IS NOT NULL"
                )
                for statement in _FACT_INDEXES:
                    conn.execute(statement)
                conn.commit()
                _SCHEMA_IDENTITIES[schema_key] = schema_identity
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def _connection(
    path: Optional[os.PathLike[str] | str] = None,
) -> Iterator[sqlite3.Connection]:
    """Commit or roll back and always close one short-lived connection."""
    conn = _connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_usage_facts_db(
    path: Optional[os.PathLike[str] | str] = None,
) -> Path:
    """Create the database and schema, returning the resolved path."""
    with _connection(path):
        pass
    return usage_facts_db_path(path)


def _clean_fields(
    fields: Optional[Mapping[str, Any]],
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    if not fields:
        return {}
    return {
        key: value
        for key, value in fields.items()
        if key in allowed and value is not None
    }


def _source(value: Any) -> str:
    return value if value in _SOURCE_RANK else "unknown"


def _origin(value: Any) -> str:
    normalized = str(value).strip()
    if normalized not in _VALID_ORIGINS:
        raise ValueError(f"unsupported usage fact origin: {normalized!r}")
    return normalized


def _upsert_run_facts(
    conn: sqlite3.Connection,
    run_id: str,
    fields: Optional[Mapping[str, Any]] = None,
) -> None:
    values = _clean_fields(fields, RUN_FACT_COLUMNS)
    if "origin" in values:
        values["origin"] = _origin(values["origin"])
    incoming_source = _source(values.pop("source", None))
    values.setdefault("captured_at", utc_now_iso())

    columns = ["run_id", *values, "source"]
    params = [run_id, *values.values(), incoming_source]
    updates = []
    for column in values:
        if column == "origin":
            updates.append(
                "origin=CASE "
                "WHEN run_usage_facts.origin='hermes_aux' "
                "AND excluded.origin<>'hermes_aux' THEN excluded.origin "
                "WHEN excluded.origin='hermes_aux' "
                "AND run_usage_facts.origin<>'hermes_aux' "
                "THEN run_usage_facts.origin "
                "ELSE COALESCE(excluded.origin, run_usage_facts.origin) END"
            )
        elif column == "call_kind":
            updates.append(
                "call_kind=CASE "
                "WHEN excluded.call_kind='main_loop' THEN 'main_loop' "
                "WHEN run_usage_facts.call_kind='main_loop' THEN 'main_loop' "
                "ELSE COALESCE(excluded.call_kind, run_usage_facts.call_kind) END"
            )
        else:
            updates.append(
                f"{column}=COALESCE(excluded.{column}, run_usage_facts.{column})"
            )
    updates.append(
        "source=CASE "
        "WHEN excluded.source='measured' THEN 'measured' "
        "WHEN excluded.source='derived' AND run_usage_facts.source='unknown' "
        "THEN 'derived' ELSE run_usage_facts.source END"
    )
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"""
        INSERT INTO run_usage_facts ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(run_id) DO UPDATE SET {", ".join(updates)}
        """,
        params,
    )


def upsert_run_facts(
    run_id: str,
    fields: Optional[Mapping[str, Any]] = None,
    *,
    path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Upsert nullable run metadata without replacing observations with NULL."""
    if not str(run_id).strip():
        raise ValueError("run_id must be non-empty")
    with _connection(path) as conn:
        _upsert_run_facts(conn, str(run_id), fields)


def _refresh_run_aggregates(conn: sqlite3.Connection, run_id: str) -> None:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS llm_call_count,
            -- Token totals are useful only when every call was observed;
            -- partial sums would understate usage.
            CASE WHEN COUNT(input_tokens)=COUNT(*) THEN SUM(input_tokens) END
                AS input_tokens,
            CASE WHEN COUNT(output_tokens)=COUNT(*) THEN SUM(output_tokens) END
                AS output_tokens,
            CASE WHEN COUNT(cache_read_tokens)=COUNT(*) THEN SUM(cache_read_tokens) END
                AS cache_read_tokens,
            CASE WHEN COUNT(cache_write_tokens)=COUNT(*) THEN SUM(cache_write_tokens) END
                AS cache_write_tokens,
            CASE WHEN COUNT(reasoning_tokens)=COUNT(*) THEN SUM(reasoning_tokens) END
                AS reasoning_tokens,
            CASE WHEN COUNT(tool_call_count)>0 THEN SUM(tool_call_count) END
                AS tool_call_count,
            CASE WHEN COUNT(tool_output_chars)>0 THEN SUM(tool_output_chars) END
                AS tool_output_chars,
            CASE WHEN COUNT(tool_duration_ms)>0 THEN SUM(tool_duration_ms) END
                AS tool_duration_ms,
            -- Duration is additive, but a partial sum would understate the run.
            CASE WHEN COUNT(duration_ms)=COUNT(*) THEN SUM(duration_ms) END
                AS duration_ms,
            -- First-token latency is the earliest available measurement; later
            -- calls without a measurement do not invalidate that observation.
            MIN(first_token_ms) AS first_token_ms,
            -- Context usage is the maximum available observation, not a sum.
            MAX(context_window_used) AS context_window_used
        FROM run_llm_calls
        WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    if row is None or row["llm_call_count"] == 0:
        return
    columns = tuple(row.keys())
    assignments = ", ".join(
        f"{column}=COALESCE(?, {column})" for column in columns
    )
    conn.execute(
        f"UPDATE run_usage_facts SET {assignments}, captured_at=? WHERE run_id=?",
        [*(row[column] for column in columns), utc_now_iso(), run_id],
    )


def record_llm_call(
    run_id: str,
    call_index: int,
    fields: Optional[Mapping[str, Any]] = None,
    *,
    run_fields: Optional[Mapping[str, Any]] = None,
    path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Upsert one model call and refresh strictly nullable run aggregates."""
    if not str(run_id).strip():
        raise ValueError("run_id must be non-empty")
    call_index = int(call_index)
    if call_index < 0:
        raise ValueError("call_index must be non-negative")

    values = _clean_fields(fields, LLM_CALL_COLUMNS)
    if "origin" in values:
        values["origin"] = _origin(values["origin"])
    columns = ["run_id", "call_index", *values]
    params = [str(run_id), call_index, *values.values()]
    updates = [
        f"{column}=COALESCE(excluded.{column}, run_llm_calls.{column})"
        for column in values
    ]
    placeholders = ", ".join("?" for _ in columns)
    conflict = (
        f"DO UPDATE SET {', '.join(updates)}"
        if updates
        else "DO NOTHING"
    )

    with _connection(path) as conn:
        _upsert_run_facts(conn, str(run_id), run_fields)
        conn.execute(
            f"""
            INSERT INTO run_llm_calls ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(run_id, call_index) {conflict}
            """,
            params,
        )
        _refresh_run_aggregates(conn, str(run_id))


def increment_tool_call(
    run_id: str,
    call_index: int,
    *,
    error_type: Optional[str] = None,
    tool_output_chars: Optional[int] = None,
    tool_duration_ms: Optional[int] = None,
    run_fields: Optional[Mapping[str, Any]] = None,
    path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Count a tool execution exactly once and preserve unknown output size."""
    if not str(run_id).strip():
        raise ValueError("run_id must be non-empty")
    call_index = int(call_index)
    if tool_duration_ms is not None:
        tool_duration_ms = int(tool_duration_ms)
        if tool_duration_ms < 0:
            raise ValueError("tool_duration_ms must be non-negative")
    with _connection(path) as conn:
        _upsert_run_facts(conn, str(run_id), run_fields)
        conn.execute(
            """
            INSERT INTO run_llm_calls (
                run_id, call_index, tool_call_count, tool_output_chars,
                tool_duration_ms, error_type
            ) VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(run_id, call_index) DO UPDATE SET
                tool_call_count=CASE
                    WHEN run_llm_calls.tool_call_count IS NULL THEN 1
                    ELSE run_llm_calls.tool_call_count + 1
                END,
                tool_output_chars=CASE
                    WHEN excluded.tool_output_chars IS NULL
                        THEN run_llm_calls.tool_output_chars
                    WHEN run_llm_calls.tool_output_chars IS NULL
                        THEN excluded.tool_output_chars
                    ELSE run_llm_calls.tool_output_chars
                        + excluded.tool_output_chars
                END,
                tool_duration_ms=CASE
                    WHEN excluded.tool_duration_ms IS NULL
                        THEN run_llm_calls.tool_duration_ms
                    WHEN run_llm_calls.tool_duration_ms IS NULL
                        THEN excluded.tool_duration_ms
                    ELSE run_llm_calls.tool_duration_ms
                        + excluded.tool_duration_ms
                END,
                error_type=COALESCE(excluded.error_type, run_llm_calls.error_type)
            """,
            (
                str(run_id),
                call_index,
                tool_output_chars,
                tool_duration_ms,
                error_type,
            ),
        )
        _refresh_run_aggregates(conn, str(run_id))


def record_tool_result(
    run_id: str,
    call_index: int,
    *,
    error_type: Optional[str] = None,
    tool_output_chars: Optional[int] = None,
    tool_duration_ms: Optional[int] = None,
    run_fields: Optional[Mapping[str, Any]] = None,
    path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Attach tool result facts without incrementing an already-counted call."""
    if not str(run_id).strip():
        raise ValueError("run_id must be non-empty")
    call_index = int(call_index)
    if tool_duration_ms is not None:
        tool_duration_ms = int(tool_duration_ms)
        if tool_duration_ms < 0:
            raise ValueError("tool_duration_ms must be non-negative")
    with _connection(path) as conn:
        _upsert_run_facts(conn, str(run_id), run_fields)
        conn.execute(
            """
            INSERT INTO run_llm_calls (
                run_id, call_index, tool_output_chars, tool_duration_ms,
                error_type
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, call_index) DO UPDATE SET
                tool_output_chars=CASE
                    WHEN excluded.tool_output_chars IS NULL
                        THEN run_llm_calls.tool_output_chars
                    WHEN run_llm_calls.tool_output_chars IS NULL
                        THEN excluded.tool_output_chars
                    ELSE run_llm_calls.tool_output_chars
                        + excluded.tool_output_chars
                END,
                tool_duration_ms=CASE
                    WHEN excluded.tool_duration_ms IS NULL
                        THEN run_llm_calls.tool_duration_ms
                    WHEN run_llm_calls.tool_duration_ms IS NULL
                        THEN excluded.tool_duration_ms
                    ELSE run_llm_calls.tool_duration_ms
                        + excluded.tool_duration_ms
                END,
                error_type=COALESCE(excluded.error_type, run_llm_calls.error_type)
            """,
            (
                str(run_id),
                call_index,
                tool_output_chars,
                tool_duration_ms,
                error_type,
            ),
        )
        _refresh_run_aggregates(conn, str(run_id))


def _serialize_trace_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def redact_trace_content(content: Any) -> str:
    """Serialize and redact trace content before it can cross the DB boundary."""
    text = _serialize_trace_content(content)
    secret_values = sorted(
        {
            value
            for name, value in os.environ.items()
            if value
            and len(value) >= 4
            and any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
        },
        key=len,
        reverse=True,
    )
    try:
        for secret in secret_values:
            text = text.replace(secret, "«redacted-secret»")
        text = redact_sensitive_text(
            text,
            force=True,
            redact_url_credentials=True,
        )
        for secret in secret_values:
            text = text.replace(secret, "«redacted-secret»")
        return text
    except Exception:
        # A redaction failure must omit content, never fail open with raw text.
        return "«redacted:trace-unavailable»"


def record_trace(
    run_id: str,
    call_index: Optional[int],
    role: str,
    content: Any,
    *,
    message_fingerprint: Optional[str] = None,
    captured_at: Optional[str] = None,
    path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Persist a trace only after mandatory fail-closed redaction."""
    if not str(run_id).strip():
        raise ValueError("run_id must be non-empty")
    if not str(role).strip():
        raise ValueError("role must be non-empty")
    redacted = redact_trace_content(content)
    with _connection(path) as conn:
        conn.execute(
            """
            INSERT INTO run_traces (
                run_id, call_index, role, content, message_fingerprint,
                captured_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, message_fingerprint)
                WHERE message_fingerprint IS NOT NULL
                DO NOTHING
            """,
            (
                str(run_id),
                int(call_index) if call_index is not None else None,
                str(role),
                redacted,
                str(message_fingerprint)
                if message_fingerprint is not None
                else None,
                captured_at or utc_now_iso(),
            ),
        )


def purge_expired_traces(
    *,
    retention_days: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[os.PathLike[str] | str] = None,
) -> int:
    """Delete expired trace text only; usage facts and calls are immutable here."""
    if retention_days is None:
        configured = os.environ.get("HERMES_USAGE_TRACE_RETENTION_DAYS")
        retention_days = (
            int(configured)
            if configured is not None
            else DEFAULT_TRACE_RETENTION_DAYS
        )
    retention_days = int(retention_days)
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = (current.astimezone(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with _connection(path) as conn:
        cursor = conn.execute(
            "DELETE FROM run_traces WHERE captured_at < ?",
            (cutoff,),
        )
        return int(cursor.rowcount)

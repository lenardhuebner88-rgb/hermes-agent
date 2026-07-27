from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from hermes_cli.usage_facts_db import (
    LLM_CALL_COLUMNS,
    RUN_FACT_COLUMNS,
    initialize_usage_facts_db,
    purge_expired_traces,
    record_llm_call,
    record_trace,
)


def _row(path, query: str):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query).fetchone()


def test_schema_contains_complete_contract(tmp_path):
    path = tmp_path / "facts.db"
    initialize_usage_facts_db(path)

    with sqlite3.connect(path) as conn:
        run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(run_usage_facts)")
        }
        call_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(run_llm_calls)")
        }
        trace_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(run_traces)")
        }

    assert run_columns == {"run_id", *RUN_FACT_COLUMNS}
    assert call_columns == {"run_id", "call_index", *LLM_CALL_COLUMNS}
    assert trace_columns == {
        "run_id",
        "call_index",
        "role",
        "content",
        "captured_at",
    }


def test_unknown_observations_remain_null(tmp_path):
    path = tmp_path / "facts.db"
    record_llm_call(
        "run-null",
        1,
        {"provider": "anthropic"},
        run_fields={"provider": "anthropic", "source": "derived"},
        path=path,
    )

    fact = _row(path, "SELECT * FROM run_usage_facts WHERE run_id='run-null'")
    call = _row(path, "SELECT * FROM run_llm_calls WHERE run_id='run-null'")

    assert fact["source"] == "derived"
    assert fact["llm_call_count"] == 1
    assert fact["input_tokens"] is None
    assert fact["output_tokens"] is None
    assert fact["tool_call_count"] is None
    assert fact["duration_ms"] is None
    assert call["input_tokens"] is None
    assert call["temperature"] is None


def test_call_facts_aggregate_without_partial_zero_fill(tmp_path):
    path = tmp_path / "facts.db"
    common = {
        "cache_read_tokens": 2,
        "cache_write_tokens": 3,
        "reasoning_tokens": 4,
        "tool_call_count": 1,
        "tool_output_tokens": 5,
        "duration_ms": 20,
        "first_token_ms": 8,
        "context_window_used": 100,
    }
    record_llm_call(
        "run-sum",
        1,
        {"input_tokens": 10, "output_tokens": 6, **common},
        run_fields={"source": "measured"},
        path=path,
    )
    record_llm_call(
        "run-sum",
        2,
        {
            "input_tokens": 20,
            "output_tokens": 7,
            **common,
            "first_token_ms": 5,
            "context_window_used": 120,
        },
        run_fields={"source": "measured"},
        path=path,
    )

    fact = _row(path, "SELECT * FROM run_usage_facts WHERE run_id='run-sum'")
    assert fact["llm_call_count"] == 2
    assert fact["input_tokens"] == 30
    assert fact["output_tokens"] == 13
    assert fact["cache_read_tokens"] == 4
    assert fact["cache_write_tokens"] == 6
    assert fact["reasoning_tokens"] == 8
    assert fact["tool_call_count"] == 2
    assert fact["tool_output_tokens"] == 10
    assert fact["duration_ms"] == 40
    assert fact["first_token_ms"] == 5
    assert fact["context_window_used"] == 120


def test_trace_redaction_happens_before_sqlite_write(monkeypatch, tmp_path):
    path = tmp_path / "facts.db"
    secret = "langfuse-super-secret-value"
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", secret)

    record_trace(
        "run-secret",
        1,
        "tool_result",
        {
            "nested": f"authorization payload {secret}",
            "authorization": f"Bearer {secret}",
        },
        path=path,
    )

    trace = _row(path, "SELECT content FROM run_traces")["content"]
    assert secret not in trace
    assert "redacted" in trace.lower()


def test_retention_deletes_only_expired_trace_rows(tmp_path):
    path = tmp_path / "facts.db"
    record_llm_call(
        "run-retain",
        1,
        {"input_tokens": 7},
        run_fields={"source": "measured"},
        path=path,
    )
    record_trace(
        "run-retain",
        1,
        "user",
        "old",
        captured_at="2025-01-01T00:00:00+00:00",
        path=path,
    )
    record_trace(
        "run-retain",
        1,
        "assistant",
        "new",
        captured_at="2026-07-26T00:00:00+00:00",
        path=path,
    )

    deleted = purge_expired_traces(
        retention_days=180,
        now=datetime(2026, 7, 27, tzinfo=timezone.utc),
        path=path,
    )

    with sqlite3.connect(path) as conn:
        assert deleted == 1
        assert conn.execute("SELECT COUNT(*) FROM run_traces").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_llm_calls").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_usage_facts").fetchone()[0] == 1


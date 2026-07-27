from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from hermes_cli.usage_facts_db import (
    LLM_CALL_COLUMNS,
    RUN_FACT_COLUMNS,
    initialize_usage_facts_db,
    purge_expired_traces,
    record_llm_call,
    record_trace,
    upsert_run_facts,
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
        "message_fingerprint",
        "captured_at",
    }


def test_large_fact_tables_have_additive_read_path_indexes(tmp_path):
    path = tmp_path / "facts.db"
    initialize_usage_facts_db(path)
    initialize_usage_facts_db(path)

    with sqlite3.connect(path) as conn:
        run_indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(run_usage_facts)")
        }
        call_indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(run_llm_calls)")
        }

    assert {
        "idx_run_usage_facts_rollup",
        "idx_run_usage_facts_origin_model",
        "idx_run_usage_facts_captured_at",
    } <= run_indexes
    assert "idx_run_llm_calls_origin_model" in call_indexes


def test_usage_rollup_query_plan_uses_dimension_index(tmp_path):
    path = tmp_path / "facts.db"
    for index in range(20):
        upsert_run_facts(
            f"run-index-{index}",
            {
                "origin": "claude_code",
                "profile": f"profile-{index % 2}",
                "lane": f"lane-{index % 3}",
                "model": f"fixture-model-{index % 4}",
                "provider": "fixture-provider",
                "billing_mode": "fixture-metered",
                "input_tokens": index,
                "captured_at": "2026-07-27T00:00:00+00:00",
                "source": "measured",
            },
            path=path,
        )

    with sqlite3.connect(path) as conn:
        plan = [
            row[3]
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT origin, profile, lane, model, provider, billing_mode,
                       SUM(input_tokens)
                  FROM run_usage_facts
                 WHERE origin = ?
                   AND input_tokens IS NOT NULL
                 GROUP BY origin, profile, lane, model, provider, billing_mode
                """,
                ("claude_code",),
            )
        ]

    assert any("idx_run_usage_facts_rollup" in detail for detail in plan)


def test_origin_and_new_fact_dimensions_round_trip_through_allowlists(tmp_path):
    path = tmp_path / "facts.db"

    record_llm_call(
        "run-new-dimensions",
        7,
        {
            "origin": "claude_code",
            "tool_duration_ms": 17,
        },
        run_fields={
            "origin": "claude_code",
            "profile": "reviewer",
            "wall_ms": 1234,
            "call_kind": "main_loop",
            "tool_duration_ms": 17,
            "source": "measured",
        },
        path=path,
    )

    fact = _row(
        path,
        "SELECT * FROM run_usage_facts WHERE run_id='run-new-dimensions'",
    )
    call = _row(
        path,
        "SELECT * FROM run_llm_calls WHERE run_id='run-new-dimensions'",
    )

    assert fact["origin"] == "claude_code"
    assert fact["profile"] == "reviewer"
    assert fact["wall_ms"] == 1234
    assert fact["call_kind"] == "main_loop"
    assert fact["tool_duration_ms"] == 17
    assert call["origin"] == "claude_code"
    assert call["tool_duration_ms"] == 17


def test_main_run_identity_wins_over_aux_regardless_of_write_order(tmp_path):
    main = {"origin": "hermes_agent", "call_kind": "main_loop"}
    auxiliary = {"origin": "hermes_aux", "call_kind": "aux"}

    for run_id, first, second in (
        ("run-aux-first", auxiliary, main),
        ("run-main-first", main, auxiliary),
    ):
        path = tmp_path / f"{run_id}.db"
        record_llm_call(run_id, 1, {}, run_fields=first, path=path)
        record_llm_call(run_id, 2, {}, run_fields=second, path=path)

        fact = _row(
            path,
            f"SELECT origin, call_kind FROM run_usage_facts WHERE run_id='{run_id}'",
        )
        assert tuple(fact) == ("hermes_agent", "main_loop")


def test_standalone_aux_run_keeps_aux_identity(tmp_path):
    path = tmp_path / "aux.db"

    record_llm_call(
        "aux-only",
        1,
        {},
        run_fields={"origin": "hermes_aux", "call_kind": "aux"},
        path=path,
    )

    fact = _row(
        path,
        "SELECT origin, call_kind FROM run_usage_facts WHERE run_id='aux-only'",
    )
    assert tuple(fact) == ("hermes_aux", "aux")


def test_origin_is_write_validated_without_changing_source_check(tmp_path):
    path = tmp_path / "facts.db"
    initialize_usage_facts_db(path)

    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO run_usage_facts (run_id, origin, source) "
            "VALUES ('run-claude', 'claude_code', 'measured')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO run_usage_facts (run_id, origin, source) "
                "VALUES ('run-invalid-source', 'claude_code', 'claude_code')"
            )

    with pytest.raises(ValueError, match="unsupported usage fact origin"):
        upsert_run_facts(
            "run-invalid-origin",
            {"origin": "other_cli"},
            path=path,
        )
    with pytest.raises(ValueError, match="unsupported usage fact origin"):
        record_llm_call(
            "run-invalid-call-origin",
            1,
            {"origin": "other_cli"},
            path=path,
        )


def test_trace_message_fingerprint_is_idempotent_per_run(tmp_path):
    path = tmp_path / "facts.db"

    for call_index in (1, 2):
        record_trace(
            "run-deduplicated",
            call_index,
            "user",
            "same request message",
            message_fingerprint="stable-message-fingerprint",
            path=path,
        )

    assert _row(path, "SELECT COUNT(*) AS count FROM run_traces")[
        "count"
    ] == 1


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
        "tool_output_chars": 5,
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
    assert fact["tool_output_chars"] == 10
    assert fact["duration_ms"] == 40
    assert fact["first_token_ms"] == 5
    assert fact["context_window_used"] == 120


def test_tool_aggregates_sum_known_rows_while_token_aggregates_stay_strict(
    tmp_path,
):
    path = tmp_path / "facts.db"
    calls = (
        {
            "input_tokens": 30,
            "output_tokens": 6,
        },
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_tokens": 2,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "tool_call_count": 1,
            "tool_output_chars": 4,
        },
        {
            "input_tokens": 20,
            "output_tokens": 5,
            "cache_read_tokens": 3,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "tool_call_count": 1,
            "tool_output_chars": 6,
        },
    )
    for call_index, fields in enumerate(calls, start=1):
        record_llm_call(
            "run-three-calls",
            call_index,
            fields,
            run_fields={"source": "measured"},
            path=path,
        )

    fact = _row(
        path,
        "SELECT * FROM run_usage_facts WHERE run_id='run-three-calls'",
    )
    assert fact["llm_call_count"] == 3
    assert fact["tool_call_count"] == 2
    assert fact["tool_output_chars"] == 10
    assert fact["input_tokens"] == 60
    assert fact["output_tokens"] == 15
    assert fact["cache_read_tokens"] is None
    assert fact["cache_write_tokens"] is None
    assert fact["reasoning_tokens"] is None


def test_first_token_uses_earliest_available_call_measurement(tmp_path):
    path = tmp_path / "facts.db"
    for call_index in range(1, 5):
        fields = {"input_tokens": call_index}
        if call_index == 4:
            fields["first_token_ms"] = 1322.96
        record_llm_call(
            "run-sparse-first-token",
            call_index,
            fields,
            path=path,
        )

    fact = _row(
        path,
        "SELECT first_token_ms FROM run_usage_facts "
        "WHERE run_id='run-sparse-first-token'",
    )

    assert fact["first_token_ms"] == 1322.96


def test_refresh_aggregates_never_replaces_an_observation_with_null(tmp_path):
    path = tmp_path / "facts.db"
    upsert_run_facts(
        "run-preserved-observation",
        {"duration_ms": 4321, "source": "measured"},
        path=path,
    )

    record_llm_call(
        "run-preserved-observation",
        1,
        {"input_tokens": 10},
        path=path,
    )

    fact = _row(
        path,
        "SELECT duration_ms FROM run_usage_facts "
        "WHERE run_id='run-preserved-observation'",
    )
    assert fact["duration_ms"] == 4321


def test_schema_initialization_is_cached_per_database_file(
    monkeypatch,
    tmp_path,
):
    import hermes_cli.usage_facts_db as usage_facts_db

    path = tmp_path / "facts.db"
    initialize_usage_facts_db(path)
    monkeypatch.setattr(
        usage_facts_db,
        "_SCHEMA",
        "THIS WOULD FAIL IF EXECUTED AGAIN",
    )

    record_llm_call(
        "run-cached-schema",
        1,
        {"input_tokens": 1},
        path=path,
    )

    assert _row(path, "SELECT COUNT(*) AS count FROM run_llm_calls")[
        "count"
    ] == 1


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

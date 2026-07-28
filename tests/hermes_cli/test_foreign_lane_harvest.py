"""Tests for foreign-lane usage harvest + foreign.sh event parser fix."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.foreign_lane_harvest import (
    ORIGIN_CODEX,
    ORIGIN_GROK,
    ORIGIN_KIMI,
    ORIGIN_QWEN,
    distill_foreign_events,
    extract_codex_rollout,
    extract_grok_inference_event,
    extract_kimi_wire,
    extract_qwen_row,
    harvest_all,
    legacy_line_parser,
    load_kimi_usage_for_handle,
    parse_json_event_stream,
    resolve_kimi_wire_path,
    sum_codex_last_token_usage,
    write_extracted_run,
)
from hermes_cli.usage_facts_db import initialize_usage_facts_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "foreign_lane_harvest"


@pytest.fixture()
def empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db)
    return db


@pytest.fixture()
def kimi_index(tmp_path: Path) -> Path:
    """Materialize the captured Kimi index with this checkout's fixture path."""
    record = json.loads((FIXTURES / "kimi" / "session_index.jsonl").read_text())
    record["sessionDir"] = str(FIXTURES / "kimi" / "session_dir")
    index = tmp_path / "session_index.jsonl"
    index.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return index


def _count_origin(db: Path, origin: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM run_usage_facts WHERE origin=?",
            (origin,),
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# foreign.sh parser: real pretty-printed Grok fixture
# ---------------------------------------------------------------------------


def test_legacy_parser_fails_on_pretty_printed_grok_events():
    text = (FIXTURES / "foreign_parser" / "grok-events-pretty.jsonl").read_text(
        encoding="utf-8"
    )
    # Structural proof that this is multi-line pretty JSON, not NDJSON.
    assert text.lstrip().startswith("{")
    assert "\n  " in text
    assert not text.splitlines()[0].rstrip().endswith("}")

    legacy_objs = legacy_line_parser(text)
    assert legacy_objs == [], "old line parser must yield nothing on pretty JSON"

    expected = json.loads(
        (FIXTURES / "foreign_parser" / "expected_usage.json").read_text()
    )
    objs = parse_json_event_stream(text)
    assert len(objs) == 1
    assert objs[0]["usage"] == expected["usage"]
    assert objs[0]["sessionId"] == expected["sessionId"]
    assert objs[0]["num_turns"] == expected["num_turns"]
    assert objs[0]["total_cost_usd"] == expected["total_cost_usd"]

    distilled = distill_foreign_events("grok", text)
    assert distilled["usage"] == expected["usage"]
    assert distilled["handle"] == expected["sessionId"]
    assert distilled["objects"] == 1


def test_parse_json_event_stream_still_accepts_ndjson():
    text = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":1}}\n'
    )
    objs = parse_json_event_stream(text)
    assert len(objs) == 2
    d = distill_foreign_events("codex", text)
    assert d["handle"] == "t1"
    assert d["usage"] == {"input_tokens": 3, "output_tokens": 1}


# ---------------------------------------------------------------------------
# Codex golden rollout — cumulative trap
# ---------------------------------------------------------------------------


def test_codex_golden_aggregation_last_total_equals_sum_last():
    rollout = FIXTURES / "codex" / "rollout-golden.jsonl"
    expected = json.loads((FIXTURES / "codex" / "expected.json").read_text())

    extracted = extract_codex_rollout(rollout)
    assert extracted is not None
    assert extracted.origin == ORIGIN_CODEX
    assert extracted.run_id == f"codex_cli:{expected['session_id']}"
    assert extracted.run_fields["input_tokens"] == expected["final_total_input_tokens"]
    assert extracted.run_fields["output_tokens"] == expected["final_total_output_tokens"]
    assert (
        extracted.run_fields["cache_read_tokens"]
        == expected["final_total_cached_input_tokens"]
    )
    assert (
        extracted.run_fields["reasoning_tokens"]
        == expected["final_total_reasoning_output_tokens"]
    )
    assert extracted.run_fields["model"] == expected["model"]
    assert extracted.run_fields["context_window_limit"] == expected["model_context_window"]
    assert extracted.rate_limit is not None
    assert expected["has_rate_limits"] is True

    summed = sum_codex_last_token_usage(rollout)
    assert summed["events"] == expected["token_count_events"]
    assert summed["input_tokens"] == expected["sum_last_input_tokens"]
    # Complete-session invariant: sum(last) == final total. Naive sum of every
    # total_token_usage would multi-count; the harvester uses final total.
    assert summed["input_tokens"] == expected["final_total_input_tokens"]
    assert summed["output_tokens"] == expected["final_total_output_tokens"]


def test_codex_naive_sum_of_totals_overcounts(tmp_path: Path):
    """Document the trap: summing total_token_usage multiplies usage."""
    rollout = FIXTURES / "codex" / "rollout-golden.jsonl"
    expected = json.loads((FIXTURES / "codex" / "expected.json").read_text())
    naive_in = 0
    for line in rollout.read_text().splitlines():
        obj = json.loads(line)
        payload = obj.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        total = (payload.get("info") or {}).get("total_token_usage") or {}
        naive_in += int(total.get("input_tokens") or 0)
    assert naive_in > expected["final_total_input_tokens"]
    assert naive_in != expected["final_total_input_tokens"]


def test_codex_write_persists_rate_limit_sidecar(empty_db: Path, tmp_path: Path):
    rollout = FIXTURES / "codex" / "rollout-golden.jsonl"
    extracted = extract_codex_rollout(rollout)
    assert extracted is not None
    rl = tmp_path / "rate.jsonl"
    written, calls, rl_n = write_extracted_run(
        extracted, db_path=empty_db, rate_limit_path=rl
    )
    assert written == 1
    assert calls == 0  # default: session row only
    assert rl_n == 1
    row = json.loads(rl.read_text().splitlines()[0])
    assert row["run_id"] == extracted.run_id
    assert row["origin"] == ORIGIN_CODEX
    assert "primary" in row["rate_limits"] or "limit_id" in row["rate_limits"]
    # optional per-turn calls
    w_calls, n_calls, _ = write_extracted_run(
        extracted, db_path=empty_db, rate_limit_path=rl, force=True, include_calls=True
    )
    assert w_calls == 1
    assert n_calls == len(extracted.calls)
    # second write is idempotent without force
    w2, c2, r2 = write_extracted_run(extracted, db_path=empty_db, rate_limit_path=rl)
    assert (w2, c2, r2) == (0, 0, 0)
    assert _count_origin(empty_db, ORIGIN_CODEX) == 1


# ---------------------------------------------------------------------------
# Kimi: resume_handle → index → wire
# ---------------------------------------------------------------------------


def test_kimi_resume_handle_to_wire_extraction(kimi_index: Path):
    expected = json.loads((FIXTURES / "kimi" / "expected.json").read_text())
    sid = expected["sessionId"]

    wire = resolve_kimi_wire_path(sid, index_path=kimi_index)
    assert wire is not None
    assert wire.is_file()

    extracted = extract_kimi_wire(wire, session_id=sid)
    assert extracted is not None
    assert extracted.origin == ORIGIN_KIMI
    assert extracted.run_id == f"kimi_cli:{sid}"
    assert extracted.run_fields["input_tokens"] == expected["total_input_tokens"]
    assert extracted.run_fields["output_tokens"] == expected["total_output_tokens"]
    assert extracted.run_fields["cache_read_tokens"] == expected["cache_read_tokens"]
    assert extracted.run_fields["cache_write_tokens"] == expected["cache_write_tokens"]
    assert extracted.run_fields["model"] == expected["model"]
    assert extracted.run_fields["llm_call_count"] == expected["turn_sums"]["turn_count"]

    via_handle = load_kimi_usage_for_handle(sid, index_path=kimi_index)
    assert via_handle is not None
    assert via_handle["input_tokens"] == expected["total_input_tokens"]


def test_kimi_input_formula_other_plus_cache():
    """inputOther + inputCacheRead + inputCacheCreation = total input."""
    usage = {
        "inputOther": 100,
        "inputCacheRead": 50,
        "inputCacheCreation": 7,
        "output": 3,
    }
    wire = {
        "type": "usage.record",
        "model": "kimi-code/k3",
        "usage": usage,
        "usageScope": "turn",
        "time": 1,
    }
    # session scope must not be summed with turns
    session_scope = {
        "type": "usage.record",
        "model": "kimi-code/k3",
        "usage": {
            "inputOther": 999999,
            "inputCacheRead": 0,
            "inputCacheCreation": 0,
            "output": 0,
        },
        "usageScope": "session",
        "time": 2,
    }
    path = Path("/tmp")  # overwritten below
    # use fixture write via extract on temp file — create in test
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "wire.jsonl"
        p.write_text(
            json.dumps(wire) + "\n" + json.dumps(session_scope) + "\n",
            encoding="utf-8",
        )
        extracted = extract_kimi_wire(p, session_id="session_test")
        assert extracted is not None
        assert extracted.run_fields["input_tokens"] == 100 + 50 + 7
        assert extracted.run_fields["output_tokens"] == 3
        assert extracted.run_fields["llm_call_count"] == 1


def test_kimi_distill_pulls_wire_via_handle(kimi_index: Path):
    expected = json.loads((FIXTURES / "kimi" / "expected.json").read_text())
    sid = expected["sessionId"]
    # Mimic stream-json events: handle only, no usage field.
    events = (
        json.dumps({"role": "assistant", "content": "hi", "session_id": sid}) + "\n"
    )
    distilled = distill_foreign_events(
        "kimi",
        events,
        kimi_session_index=kimi_index,
    )
    assert distilled["handle"] == sid
    assert distilled["usage"] is not None
    assert distilled["usage"]["input_tokens"] == expected["total_input_tokens"]


# ---------------------------------------------------------------------------
# Qwen schemaVersion
# ---------------------------------------------------------------------------


def test_qwen_schema_version_and_fields():
    rows = (FIXTURES / "qwen" / "token-usage.jsonl").read_text().splitlines()
    expected = json.loads((FIXTURES / "qwen" / "expected.json").read_text())
    assert len(rows) == expected["rows"]
    first = json.loads(rows[0])
    assert first["schemaVersion"] == 1
    extracted = extract_qwen_row(first)
    assert extracted is not None
    assert extracted.origin == ORIGIN_QWEN
    assert extracted.run_id == f"qwen_cli:{first['id']}"
    assert extracted.run_fields["input_tokens"] == first["inputTokens"]
    assert extracted.run_fields["output_tokens"] == first["outputTokens"]
    assert extracted.run_fields["cache_read_tokens"] == first["cachedTokens"]
    assert extracted.run_fields["reasoning_tokens"] == first["thoughtsTokens"]
    assert extracted.run_fields["duration_ms"] == float(first["apiDurationMs"])
    # Fixture row is a captured Qwen rollout with authType="openai".  That
    # identifies the access protocol, not its billing route.
    billing_mode = extracted.run_fields["billing_mode"]
    assert billing_mode in {
        "subscription_included",
        "metered",
        "official_models_api",
        "official_docs_snapshot",
        "unknown",
    }
    assert billing_mode == "subscription_included"
    assert billing_mode != first["authType"]

    bad = dict(first, schemaVersion=99)
    assert extract_qwen_row(bad) is None


# ---------------------------------------------------------------------------
# Grok unified log
# ---------------------------------------------------------------------------


def test_grok_inference_done_tokens_and_missing_model():
    lines = (FIXTURES / "grok" / "unified-sample.jsonl").read_text().splitlines()
    assert lines
    obj = json.loads(lines[0])
    extracted = extract_grok_inference_event(obj)
    assert extracted is not None
    assert extracted.origin == ORIGIN_GROK
    ctx = obj["ctx"]
    assert extracted.run_fields["input_tokens"] == ctx["prompt_tokens"]
    assert extracted.run_fields["output_tokens"] == ctx["completion_tokens"]
    assert extracted.run_fields["cache_read_tokens"] == ctx["cached_prompt_tokens"]
    assert extracted.run_fields["reasoning_tokens"] == ctx["reasoning_tokens"]
    # Model is honestly unavailable on this log line.
    assert extracted.run_fields["model"] is None
    assert extracted.rate_limit is None

    noise = {"msg": "billing: fetched credits config", "sid": "x", "ctx": {}}
    assert extract_grok_inference_event(noise) is None


# ---------------------------------------------------------------------------
# End-to-end harvest: incremental / idempotent
# ---------------------------------------------------------------------------


def test_harvest_all_idempotent_second_pass(
    tmp_path: Path, empty_db: Path, kimi_index: Path
):
    # Mini source trees
    codex_root = tmp_path / "codex" / "2026" / "07" / "27"
    codex_root.mkdir(parents=True)
    golden = (FIXTURES / "codex" / "rollout-golden.jsonl").read_text()
    (codex_root / "rollout-golden.jsonl").write_text(golden)

    qwen_dir = tmp_path / "qwen"
    qwen_dir.mkdir()
    (qwen_dir / "token-usage-2026-07.jsonl").write_text(
        (FIXTURES / "qwen" / "token-usage.jsonl").read_text()
    )
    grok_path = tmp_path / "unified.jsonl"
    grok_path.write_text((FIXTURES / "grok" / "unified-sample.jsonl").read_text())

    state = tmp_path / "state.json"
    common = dict(
        db_path=empty_db,
        state_path=state,
        rate_limit_path=tmp_path / "rl.jsonl",
        codex_sessions=tmp_path / "codex",
        kimi_index=kimi_index,
        qwen_usage_dir=qwen_dir,
        grok_unified=grok_path,
    )
    first = harvest_all(**common)
    assert first["written_runs_total"] > 0
    assert first["origins"][ORIGIN_CODEX]["written_runs"] == 1
    assert first["origins"][ORIGIN_KIMI]["written_runs"] == 1
    assert first["origins"][ORIGIN_QWEN]["written_runs"] == 5
    assert first["origins"][ORIGIN_GROK]["written_runs"] >= 1

    second = harvest_all(**common)
    assert second["written_runs_total"] == 0
    for origin in (ORIGIN_CODEX, ORIGIN_KIMI, ORIGIN_QWEN, ORIGIN_GROK):
        assert second["origins"][origin]["written_runs"] == 0

    # Counts stable
    assert _count_origin(empty_db, ORIGIN_CODEX) == 1
    assert _count_origin(empty_db, ORIGIN_KIMI) == 1
    assert _count_origin(empty_db, ORIGIN_QWEN) == 5

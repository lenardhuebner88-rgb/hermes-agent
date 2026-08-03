"""Tests for foreign-lane usage harvest + foreign.sh event parser fix."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes_cli.foreign_lane_harvest import (
    CORRELATION_SOURCE_META,
    CORRELATION_SOURCE_TRANSCRIPT,
    ExtractedRun,
    ORIGIN_CODEX,
    ORIGIN_GROK,
    ORIGIN_KIMI,
    ORIGIN_QWEN,
    STATE_VERSION,
    backfill_session_from_transcripts,
    correlate_from_run_metas,
    distill_foreign_events,
    extract_codex_rollout,
    extract_grok_inference_event,
    extract_kimi_wire,
    extract_qwen_row,
    find_run_ids_for_handle,
    harvest_all,
    legacy_line_parser,
    load_kimi_usage_for_handle,
    load_state,
    parse_foreign_spawn_command,
    parse_json_event_stream,
    parse_meta_file,
    resume_handle_from_run_id,
    resolve_kimi_wire_path,
    sum_codex_last_token_usage,
    write_extracted_run,
)
from hermes_cli.usage_facts_db import initialize_usage_facts_db, upsert_run_facts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "foreign_lane_harvest"
LOCAL_TZ = ZoneInfo("Europe/Berlin")


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
    usage_row = _row(empty_db, extracted.run_id)
    # Codex has no tool measurement source: NULL stays NULL (unknown, not 0).
    assert usage_row["tool_call_count"] is None
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
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    common = dict(
        db_path=empty_db,
        state_path=state,
        rate_limit_path=tmp_path / "rl.jsonl",
        codex_sessions=tmp_path / "codex",
        kimi_index=kimi_index,
        qwen_usage_dir=qwen_dir,
        grok_unified=grok_path,
        runs_root=runs_root,
        correlate_sessions=False,
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


# ---------------------------------------------------------------------------
# Session correlation helpers
# ---------------------------------------------------------------------------


def _write_meta(run_dir: Path, **fields: object) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "meta"
    lines = [f"{k}={v}" for k, v in fields.items()]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return meta


def _insert_run(
    db: Path,
    run_id: str,
    *,
    origin: str,
    lane: str,
    session_id: str | None = None,
    correlation_source: str | None = None,
    profile: str | None = None,
    input_tokens: int = 10,
    output_tokens: int = 2,
    task_run_id: str | None = None,
) -> None:
    fields: dict = {
        "origin": origin,
        "lane": lane,
        "call_kind": "foreign_cli",
        "provider": "test",
        "model": "test-model",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "captured_at": "2026-07-31T12:00:00+00:00",
        "source": "measured",
    }
    if session_id is not None:
        fields["session_id"] = session_id
    if correlation_source is not None:
        fields["correlation_source"] = correlation_source
    if profile is not None:
        fields["profile"] = profile
    if task_run_id is not None:
        fields["task_run_id"] = task_run_id
    upsert_run_facts(run_id, fields, path=db)


def _row(db: Path, run_id: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id=?", (run_id,)
        ).fetchone()
        assert row is not None, f"missing run_id={run_id}"
        return dict(row)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "origin",
    [ORIGIN_CODEX, ORIGIN_GROK, ORIGIN_KIMI, ORIGIN_QWEN],
)
def test_terminal_foreign_run_without_tool_source_keeps_null_tool_count(
    empty_db: Path,
    origin: str,
) -> None:
    # No extractor saw a tool measurement source: NULL must stay NULL.
    # A fabricated 0 would claim "no tools used" on uninstrumented lanes.
    extracted = ExtractedRun(
        run_id=f"{origin}-without-tools",
        origin=origin,
        run_fields={"origin": origin, "source": "measured"},
    )

    written, calls, _rate_limits = write_extracted_run(
        extracted,
        db_path=empty_db,
    )

    assert written == 1
    assert calls == 0
    assert _row(empty_db, extracted.run_id)["tool_call_count"] is None


def test_terminal_run_with_tool_source_finalizes_observed_zero(
    empty_db: Path,
) -> None:
    # A lane with a real measurement source (Qwen `tools` object present but
    # no totalCalls) yields an observed zero for the terminal session.
    extracted = ExtractedRun(
        run_id="qwen_cli-with-tools-object",
        origin=ORIGIN_QWEN,
        run_fields={"origin": ORIGIN_QWEN, "source": "measured"},
        tool_count_observed=True,
    )

    written, calls, _rate_limits = write_extracted_run(
        extracted,
        db_path=empty_db,
    )

    assert written == 1
    assert calls == 0
    assert _row(empty_db, extracted.run_id)["tool_call_count"] == 0


def _checksum(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(input_tokens),0) AS in_tok, "
            "COALESCE(SUM(output_tokens),0) AS out_tok, "
            "SUM(CASE WHEN task_run_id IS NOT NULL THEN 1 ELSE 0 END) AS task_n, "
            "SUM(CASE WHEN session_id IS NOT NULL THEN 1 ELSE 0 END) AS sid_n "
            "FROM run_usage_facts"
        ).fetchone()
        return {
            "n": row[0],
            "in_tok": row[1],
            "out_tok": row[2],
            "task_n": row[3],
            "sid_n": row[4],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# done-when 4: run_id suffix → resume_handle per lane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id,expected",
    [
        ("codex_cli:019f9aba-39ff-73c1-8f8d-61d16ac50528", "019f9aba-39ff-73c1-8f8d-61d16ac50528"),
        (
            "kimi_cli:session_90e4ccf8-e8fd-4b18-98b3-5ad0a9462de5",
            "session_90e4ccf8-e8fd-4b18-98b3-5ad0a9462de5",
        ),
        ("qwen_cli:2811611d-89ab-4f13-8206-13f67f0aca5c", "2811611d-89ab-4f13-8206-13f67f0aca5c"),
        (
            "grok_cli:019f695f-9076-7e33-b484-447468419c3c:loop17",
            "019f695f-9076-7e33-b484-447468419c3c",
        ),
    ],
)
def test_resume_handle_from_run_id_per_lane(run_id: str, expected: str):
    assert resume_handle_from_run_id(run_id) == expected


def test_resume_handle_grok_loop_not_part_of_session_id():
    handle = resume_handle_from_run_id(
        "grok_cli:019f695f-9076-7e33-b484-447468419c3c:loop17"
    )
    assert handle is not None
    assert ":loop" not in handle
    assert handle == "019f695f-9076-7e33-b484-447468419c3c"


# ---------------------------------------------------------------------------
# done-when 1 + 2: meta with/without claude_session_id
# ---------------------------------------------------------------------------


def test_meta_with_claude_session_id_correlates_usage_row(
    empty_db: Path, tmp_path: Path
):
    """Real key=value meta format → session_id + foreign_run_meta."""
    handle = "019fb847-9429-7922-9818-8fa2762450ab"
    claude = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    runs = tmp_path / "runs"
    run_dir = runs / "20260731T150526-codex-cache-write-feldname-fix"
    # Exact shape measured from production meta files.
    _write_meta(
        run_dir,
        resume_handle=handle,
        usage='{"input_tokens": 100}',
        lane="codex",
        exit=0,
        duration_s=432,
        dir="/home/piet/.hermes/hermes-agent/.worktrees/cachewrite-fieldname",
        run=str(run_dir),
        read_only=0,
        claude_session_id=claude,
    )
    # Round-trip the on-disk format.
    parsed = parse_meta_file(run_dir / "meta")
    assert parsed["resume_handle"] == handle
    assert parsed["claude_session_id"] == claude
    assert parsed["lane"] == "codex"

    run_id = f"codex_cli:{handle}"
    _insert_run(empty_db, run_id, origin=ORIGIN_CODEX, lane="codex", input_tokens=42)

    report = correlate_from_run_metas(
        db_path=empty_db, runs_root=runs, apply=True, local_tz=LOCAL_TZ
    )
    assert report["rows_filled"] == 1
    assert report["correlation_source"] == CORRELATION_SOURCE_META
    assert report["unassignable_no_handle"] == 0

    row = _row(empty_db, run_id)
    assert row["session_id"] == claude
    assert row["correlation_source"] == CORRELATION_SOURCE_META
    assert row["input_tokens"] == 42


def test_meta_without_claude_session_id_leaves_null(empty_db: Path, tmp_path: Path):
    handle = "019fb81c-2c05-7c23-bca4-0d89275f05b1"
    runs = tmp_path / "runs"
    _write_meta(
        runs / "20260731T141801-codex-usage-korrelation-backfill",
        resume_handle=handle,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=100,
        dir="/tmp",
        run="/tmp/run",
        read_only=0,
        # deliberately no claude_session_id
    )
    run_id = f"codex_cli:{handle}"
    _insert_run(empty_db, run_id, origin=ORIGIN_CODEX, lane="codex")

    report = correlate_from_run_metas(
        db_path=empty_db, runs_root=runs, apply=True, local_tz=LOCAL_TZ
    )
    assert report["metas_without_claude"] == 1
    assert report["metas_with_claude"] == 0
    assert report["rows_filled"] == 0
    assert report["unique_handles"] == 0

    row = _row(empty_db, run_id)
    assert row["session_id"] is None
    assert row["correlation_source"] is None


# ---------------------------------------------------------------------------
# done-when 3: resume_handle=none (Grok)
# ---------------------------------------------------------------------------


def test_resume_handle_none_unassignable_no_crash(empty_db: Path, tmp_path: Path):
    runs = tmp_path / "runs"
    _write_meta(
        runs / "20260731T155708-grok-occurred-at-zeitstempel",
        resume_handle="none",
        usage="unavailable",
        lane="grok",
        exit=0,
        duration_s=3577,
        dir="/tmp",
        run="/tmp/run",
        read_only=0,
        claude_session_id="claude-sess-grok-1",
    )
    # Grok usage rows exist but cannot be tied via none-handle.
    _insert_run(
        empty_db,
        "grok_cli:019fb876-ea27-7372-9e21-b73614ac9878:loop17",
        origin=ORIGIN_GROK,
        lane="grok",
    )

    report = correlate_from_run_metas(
        db_path=empty_db, runs_root=runs, apply=True, local_tz=LOCAL_TZ
    )
    assert report["unassignable_no_handle"] == 1
    assert report["rows_filled"] == 0
    assert report["unique_handles"] == 0
    row = _row(empty_db, "grok_cli:019fb876-ea27-7372-9e21-b73614ac9878:loop17")
    assert row["session_id"] is None


# ---------------------------------------------------------------------------
# done-when 4 (matching side): find_run_ids_for_handle per lane
# ---------------------------------------------------------------------------


def test_find_run_ids_for_handle_all_lanes(empty_db: Path):
    codex_h = "019f9aba-39ff-73c1-8f8d-61d16ac50528"
    kimi_h = "session_90e4ccf8-e8fd-4b18-98b3-5ad0a9462de5"
    grok_h = "019f695f-9076-7e33-b484-447468419c3c"
    qwen_h = "07e807e0-87a9-4ca2-bff4-cdb22286b68d"

    _insert_run(empty_db, f"codex_cli:{codex_h}", origin=ORIGIN_CODEX, lane="codex")
    _insert_run(empty_db, f"kimi_cli:{kimi_h}", origin=ORIGIN_KIMI, lane="kimi")
    _insert_run(
        empty_db,
        f"grok_cli:{grok_h}:loop0",
        origin=ORIGIN_GROK,
        lane="grok",
    )
    _insert_run(
        empty_db,
        f"grok_cli:{grok_h}:loop17",
        origin=ORIGIN_GROK,
        lane="grok",
    )
    # Qwen: call-level run_id + sessionId on profile
    _insert_run(
        empty_db,
        "qwen_cli:6fa0d5d0-9677-43ba-932c-3fab09793f87",
        origin=ORIGIN_QWEN,
        lane="qwen",
        profile=qwen_h,
    )

    conn = sqlite3.connect(str(empty_db))
    conn.row_factory = sqlite3.Row
    try:
        assert find_run_ids_for_handle(conn, codex_h) == [f"codex_cli:{codex_h}"]
        assert find_run_ids_for_handle(conn, kimi_h) == [f"kimi_cli:{kimi_h}"]
        grok_ids = find_run_ids_for_handle(conn, grok_h)
        assert set(grok_ids) == {
            f"grok_cli:{grok_h}:loop0",
            f"grok_cli:{grok_h}:loop17",
        }
        qwen_ids = find_run_ids_for_handle(conn, qwen_h)
        assert qwen_ids == ["qwen_cli:6fa0d5d0-9677-43ba-932c-3fab09793f87"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# done-when 5: transcript backfill unique vs ambiguous
# ---------------------------------------------------------------------------


def _write_transcript_spawn(
    path: Path,
    *,
    session_id: str,
    timestamp: str,
    lane: str,
    brief: str,
    slug: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = f"~/.claude/tools/foreign.sh {lane} --brief {brief}"
    if slug:
        cmd += f" --slug {slug}"
    cmd += " --dir /tmp/work"
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_test",
                    "name": "Bash",
                    "input": {
                        "command": cmd,
                        "description": f"Spawn {lane}",
                    },
                }
            ],
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def test_transcript_backfill_unique_match(empty_db: Path, tmp_path: Path):
    handle = "019fac5a-e7fd-7a02-832e-46e495ed2871"
    claude = "20849fe8-e0b1-468e-883b-c95f94445eea"
    runs = tmp_path / "runs"
    projects = tmp_path / "projects" / "proj"

    # Run dir at local 2026-07-29 08:19:50 Europe/Berlin = 06:19:50 UTC
    _write_meta(
        runs / "20260729T081950-codex-bp-1-nachbesserung",
        resume_handle=handle,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=100,
        dir="/tmp",
        run="/tmp/run",
        read_only=0,
    )
    spawn_ts = "2026-07-29T06:19:46.000Z"  # a few seconds before local run stamp
    _write_transcript_spawn(
        projects / f"{claude}.jsonl",
        session_id=claude,
        timestamp=spawn_ts,
        lane="codex",
        brief="/home/piet/.hermes/briefs/bp-1-nachbesserung.md",
    )
    run_id = f"codex_cli:{handle}"
    _insert_run(empty_db, run_id, origin=ORIGIN_CODEX, lane="codex", input_tokens=99)

    report = backfill_session_from_transcripts(
        db_path=empty_db,
        runs_root=runs,
        projects_root=tmp_path / "projects",
        apply=True,
        local_tz=LOCAL_TZ,
    )
    assert report["matched_spawns"] == 1
    assert report["ambiguous_spawns"] == 0
    assert report["rows_filled"] == 1
    assert report["correlation_source"] == CORRELATION_SOURCE_TRANSCRIPT
    row = _row(empty_db, run_id)
    assert row["session_id"] == claude
    assert row["correlation_source"] == CORRELATION_SOURCE_TRANSCRIPT
    assert row["input_tokens"] == 99


def test_transcript_backfill_ambiguous_candidates_stay_null(
    empty_db: Path, tmp_path: Path
):
    handle_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    handle_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    claude = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    runs = tmp_path / "runs"
    projects = tmp_path / "projects" / "proj"

    # Two run dirs, same lane+slug, both inside the window → ambiguous.
    _write_meta(
        runs / "20260729T081950-codex-shared-slug",
        resume_handle=handle_a,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=10,
        dir="/tmp",
        run="/tmp/a",
        read_only=0,
    )
    _write_meta(
        runs / "20260729T082100-codex-shared-slug",
        resume_handle=handle_b,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=10,
        dir="/tmp",
        run="/tmp/b",
        read_only=0,
    )
    _write_transcript_spawn(
        projects / f"{claude}.jsonl",
        session_id=claude,
        timestamp="2026-07-29T06:19:40.000Z",
        lane="codex",
        brief="/tmp/shared-slug.md",
    )
    _insert_run(
        empty_db, f"codex_cli:{handle_a}", origin=ORIGIN_CODEX, lane="codex"
    )
    _insert_run(
        empty_db, f"codex_cli:{handle_b}", origin=ORIGIN_CODEX, lane="codex"
    )

    report = backfill_session_from_transcripts(
        db_path=empty_db,
        runs_root=runs,
        projects_root=tmp_path / "projects",
        apply=True,
        local_tz=LOCAL_TZ,
    )
    assert report["ambiguous_spawns"] == 1
    assert report["matched_spawns"] == 0
    assert report["rows_filled"] == 0
    assert _row(empty_db, f"codex_cli:{handle_a}")["session_id"] is None
    assert _row(empty_db, f"codex_cli:{handle_b}")["session_id"] is None


def test_parse_foreign_spawn_command_respects_slug_override():
    cmd = (
        "~/.claude/tools/foreign.sh codex "
        "--brief /home/piet/.hermes/briefs/ae-1-anker-entkoppeln-20260729.md "
        "--slug ae-1-anker-entkoppeln --dir /tmp/wt"
    )
    parsed = parse_foreign_spawn_command(cmd)
    assert parsed is not None
    assert parsed["lane"] == "codex"
    assert parsed["slug"] == "ae-1-anker-entkoppeln"


# ---------------------------------------------------------------------------
# done-when 6 + 7 + 8: preview no-write, apply scopes, idempotent, no overwrite
# ---------------------------------------------------------------------------


def test_backfill_preview_writes_nothing(empty_db: Path, tmp_path: Path):
    handle = "019fac5a-e7fd-7a02-832e-46e495ed2871"
    claude = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    runs = tmp_path / "runs"
    projects = tmp_path / "projects" / "p"
    _write_meta(
        runs / "20260729T081950-codex-preview-slug",
        resume_handle=handle,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=1,
        dir="/tmp",
        run="/tmp/r",
        read_only=0,
    )
    _write_transcript_spawn(
        projects / f"{claude}.jsonl",
        session_id=claude,
        timestamp="2026-07-29T06:19:46.000Z",
        lane="codex",
        brief="/tmp/preview-slug.md",
    )
    _insert_run(
        empty_db,
        f"codex_cli:{handle}",
        origin=ORIGIN_CODEX,
        lane="codex",
        input_tokens=77,
        output_tokens=5,
        task_run_id="task-keep-me",
    )
    before = _checksum(empty_db)

    report = backfill_session_from_transcripts(
        db_path=empty_db,
        runs_root=runs,
        projects_root=tmp_path / "projects",
        apply=False,  # preview
        local_tz=LOCAL_TZ,
    )
    assert report["applied"] is False
    assert report["rows_to_fill"] == 1
    assert report["rows_filled"] == 0
    assert report["before"] == report["after"]
    assert _checksum(empty_db) == before
    assert _row(empty_db, f"codex_cli:{handle}")["session_id"] is None
    assert _row(empty_db, f"codex_cli:{handle}")["task_run_id"] == "task-keep-me"


def test_backfill_apply_only_touches_session_fields(empty_db: Path, tmp_path: Path):
    handle = "019fac5a-e7fd-7a02-832e-46e495ed2871"
    claude = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    runs = tmp_path / "runs"
    projects = tmp_path / "projects" / "p"
    _write_meta(
        runs / "20260729T081950-codex-apply-slug",
        resume_handle=handle,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=1,
        dir="/tmp",
        run="/tmp/r",
        read_only=0,
    )
    _write_transcript_spawn(
        projects / f"{claude}.jsonl",
        session_id=claude,
        timestamp="2026-07-29T06:19:46.000Z",
        lane="codex",
        brief="/tmp/apply-slug.md",
    )
    run_id = f"codex_cli:{handle}"
    _insert_run(
        empty_db,
        run_id,
        origin=ORIGIN_CODEX,
        lane="codex",
        input_tokens=123,
        output_tokens=9,
        task_run_id="task-immutable",
    )
    before_row = _row(empty_db, run_id)

    report = backfill_session_from_transcripts(
        db_path=empty_db,
        runs_root=runs,
        projects_root=tmp_path / "projects",
        apply=True,
        local_tz=LOCAL_TZ,
    )
    assert report["rows_filled"] == 1
    after = _row(empty_db, run_id)
    assert after["session_id"] == claude
    assert after["correlation_source"] == CORRELATION_SOURCE_TRANSCRIPT
    # Untouched columns
    assert after["input_tokens"] == before_row["input_tokens"] == 123
    assert after["output_tokens"] == before_row["output_tokens"] == 9
    assert after["task_run_id"] == "task-immutable"
    assert after["captured_at"] == before_row["captured_at"]
    assert after["lane"] == "codex"


def test_backfill_second_run_idempotent(empty_db: Path, tmp_path: Path):
    handle = "019fac5a-e7fd-7a02-832e-46e495ed2871"
    claude = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    runs = tmp_path / "runs"
    projects = tmp_path / "projects" / "p"
    _write_meta(
        runs / "20260729T081950-codex-idem-slug",
        resume_handle=handle,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=1,
        dir="/tmp",
        run="/tmp/r",
        read_only=0,
    )
    _write_transcript_spawn(
        projects / f"{claude}.jsonl",
        session_id=claude,
        timestamp="2026-07-29T06:19:46.000Z",
        lane="codex",
        brief="/tmp/idem-slug.md",
    )
    _insert_run(
        empty_db, f"codex_cli:{handle}", origin=ORIGIN_CODEX, lane="codex"
    )

    first = backfill_session_from_transcripts(
        db_path=empty_db,
        runs_root=runs,
        projects_root=tmp_path / "projects",
        apply=True,
        local_tz=LOCAL_TZ,
    )
    assert first["rows_filled"] == 1

    second = backfill_session_from_transcripts(
        db_path=empty_db,
        runs_root=runs,
        projects_root=tmp_path / "projects",
        apply=True,
        local_tz=LOCAL_TZ,
    )
    assert second["rows_filled"] == 0
    assert second["rows_to_fill"] == 0
    assert second["rows_already_set"] == 1


def test_existing_session_id_never_overwritten(empty_db: Path, tmp_path: Path):
    handle = "019fac5a-e7fd-7a02-832e-46e495ed2871"
    original = "already-set-session-id"
    other = "should-not-win"
    runs = tmp_path / "runs"
    _write_meta(
        runs / "20260731T150526-codex-no-overwrite",
        resume_handle=handle,
        usage="unavailable",
        lane="codex",
        exit=0,
        duration_s=1,
        dir="/tmp",
        run="/tmp/r",
        read_only=0,
        claude_session_id=other,
    )
    run_id = f"codex_cli:{handle}"
    _insert_run(
        empty_db,
        run_id,
        origin=ORIGIN_CODEX,
        lane="codex",
        session_id=original,
        correlation_source="session_model_usage_run",
    )

    report = correlate_from_run_metas(
        db_path=empty_db, runs_root=runs, apply=True, local_tz=LOCAL_TZ
    )
    assert report["rows_filled"] == 0
    assert report["rows_already_set"] == 1
    row = _row(empty_db, run_id)
    assert row["session_id"] == original
    assert row["correlation_source"] == "session_model_usage_run"


def test_usage_unavailable_is_not_an_error(empty_db: Path, tmp_path: Path):
    """Grok/Kimi meta with usage=unavailable must not invent token numbers."""
    runs = tmp_path / "runs"
    _write_meta(
        runs / "20260731T152231-kimi-review-usage-slices",
        resume_handle="session_660bd75d-c736-4b04-a385-c074ee76be21",
        usage="unavailable",
        lane="kimi",
        exit=0,
        duration_s=1681,
        dir="/tmp",
        run="/tmp/r",
        read_only=1,
        claude_session_id="claude-kimi-1",
    )
    handle = "session_660bd75d-c736-4b04-a385-c074ee76be21"
    _insert_run(
        empty_db,
        f"kimi_cli:{handle}",
        origin=ORIGIN_KIMI,
        lane="kimi",
        input_tokens=0,
        output_tokens=0,
    )
    report = correlate_from_run_metas(
        db_path=empty_db, runs_root=runs, apply=True, local_tz=LOCAL_TZ
    )
    assert report["rows_filled"] == 1
    row = _row(empty_db, f"kimi_cli:{handle}")
    assert row["session_id"] == "claude-kimi-1"
    # Tokens stay as harvested (zeros here) — correlation did not invent usage.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


# ---------------------------------------------------------------------------
# STATE_VERSION 2 — codex cache-write backfill (real rollout fixtures)
# ---------------------------------------------------------------------------

# Real Codex rollouts captured 2026-07-30 (field present, observed 0) and
# 2026-07-10 (field absent from the source); session metadata redacted.
CODEX_ROLLOUT_WITH_FIELD = FIXTURES / "codex" / "rollout-cache-write-real.jsonl"
CODEX_ROLLOUT_WITHOUT_FIELD = (
    FIXTURES / "codex" / "rollout-no-cache-write-field.jsonl"
)
RUN_WITH_FIELD = "codex_cli:019fb059-5754-70b3-aead-caf3fafd691b"
RUN_WITHOUT_FIELD = "codex_cli:019f48ea-fd03-75d1-b84b-c3910fb5a758"


def test_codex_real_rollout_extracts_observed_cache_write_zero() -> None:
    """Real 2026-07-30 token_count event carries the field with observed 0.

    An observed zero is a measurement, not a missing field: it must survive
    extraction as 0 so the readmodel's split gate treats the row as complete.
    """
    extracted = extract_codex_rollout(CODEX_ROLLOUT_WITH_FIELD)
    assert extracted is not None
    assert extracted.run_id == RUN_WITH_FIELD
    assert extracted.run_fields["cache_write_tokens"] == 0
    assert extracted.run_fields["cache_read_tokens"] == 466432
    assert extracted.run_fields["input_tokens"] == 514209


def test_codex_real_rollout_without_field_extracts_none() -> None:
    """Real 2026-07-10 rollout predates the field: unknown stays NULL."""
    extracted = extract_codex_rollout(CODEX_ROLLOUT_WITHOUT_FIELD)
    assert extracted is not None
    assert extracted.run_id == RUN_WITHOUT_FIELD
    assert extracted.run_fields["cache_write_tokens"] is None
    assert extracted.run_fields["cache_read_tokens"] == 13147392


def test_state_version_bump_resets_fingermarks_and_marks_resume(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {"version": 1, "sources": {"codex_cli": {"files": {"x": "y"}}}}
        ),
        encoding="utf-8",
    )
    state = load_state(state_file)
    assert state["version"] == STATE_VERSION
    assert state["sources"] == {}
    assert state["resumed_from_version"] == 1
    # Current-version state is kept untouched.
    state_file.write_text(
        json.dumps({"version": STATE_VERSION, "sources": {"a": {}}}),
        encoding="utf-8",
    )
    unchanged = load_state(state_file)
    assert unchanged["sources"] == {"a": {}}
    assert "resumed_from_version" not in unchanged


def test_cache_write_backfill_fills_only_rows_with_source_field(
    empty_db: Path, tmp_path: Path
) -> None:
    """v2 re-harvest fills NULL cache_write where the rollout carries the
    field and leaves rows whose source lacks it at NULL (never a 0)."""
    # Pre-v2 stored state: both runs exist with cache_write NULL.
    for run_id in (RUN_WITH_FIELD, RUN_WITHOUT_FIELD):
        upsert_run_facts(
            run_id,
            {
                "origin": ORIGIN_CODEX,
                "provider": "openai",
                "billing_mode": "subscription_included",
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 50,
                "source": "measured",
            },
            path=empty_db,
        )
    sessions = tmp_path / "sessions"
    (sessions / "2026" / "07" / "30").mkdir(parents=True)
    (sessions / "2026" / "07" / "10").mkdir(parents=True)
    import shutil

    shutil.copy(
        CODEX_ROLLOUT_WITH_FIELD,
        sessions / "2026" / "07" / "30" / CODEX_ROLLOUT_WITH_FIELD.name,
    )
    shutil.copy(
        CODEX_ROLLOUT_WITHOUT_FIELD,
        sessions / "2026" / "07" / "10" / CODEX_ROLLOUT_WITHOUT_FIELD.name,
    )
    state_file = tmp_path / "foreign_state.json"
    state_file.write_text(json.dumps({"version": 1, "sources": {}}))

    first = harvest_all(
        db_path=empty_db,
        state_path=state_file,
        codex_sessions=sessions,
        kimi_index=tmp_path / "missing-kimi-index.jsonl",
        qwen_usage_dir=tmp_path / "missing-qwen",
        grok_unified=tmp_path / "missing-grok.jsonl",
        origins=[ORIGIN_CODEX],
    )
    assert first["origins"][ORIGIN_CODEX]["written_runs"] == 2
    with_row = _row(empty_db, RUN_WITH_FIELD)
    without_row = _row(empty_db, RUN_WITHOUT_FIELD)
    assert with_row["cache_write_tokens"] == 0
    assert without_row["cache_write_tokens"] is None
    # Re-harvest re-derives token fields from source (v9 precedent):
    # incoming non-NULL values replace the stale stored totals.
    assert with_row["input_tokens"] == 514209
    assert without_row["input_tokens"] == 13618252

    # Second pass: state is v2 with fingerprints, nothing is rewritten.
    second = harvest_all(
        db_path=empty_db,
        state_path=state_file,
        codex_sessions=sessions,
        kimi_index=tmp_path / "missing-kimi-index.jsonl",
        qwen_usage_dir=tmp_path / "missing-qwen",
        grok_unified=tmp_path / "missing-grok.jsonl",
        origins=[ORIGIN_CODEX],
    )
    assert second["origins"][ORIGIN_CODEX]["written_runs"] == 0
    assert _row(empty_db, RUN_WITHOUT_FIELD)["cache_write_tokens"] is None

"""Buzz-agent usage attribution from real CLI workspace signals."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import hermes_cli.claude_code_harvester as claude_harvester
from hermes_cli.claude_code_harvester import (
    backfill_provider_attribution,
    draft_to_fields,
    merge_assistant_fragment,
)
from hermes_cli.fleet_metrics_readmodel import build_fleet_metrics_payload
from hermes_cli.foreign_lane_harvest import (
    ExtractedCall,
    ExtractedRun,
    ORIGIN_CODEX,
    ORIGIN_GROK,
    ORIGIN_KIMI,
    ORIGIN_QWEN,
    extract_codex_rollout,
    extract_grok_inference_event,
    extract_kimi_wire,
    extract_qwen_row,
    load_grok_session_workspaces,
    load_kimi_session_workspaces,
    load_qwen_session_workspaces,
    load_state,
    write_extracted_run,
)
from hermes_cli.usage_facts_buzz_attribution import (
    BUZZ_AGENT_ORIGIN,
    buzz_agent_unit_for_workspace,
    source_origin_for_run,
)
from hermes_cli.usage_facts_db import (
    initialize_usage_facts_db,
    record_llm_call,
    upsert_run_facts,
)
from hermes_cli.usage_facts_readmodel import (
    build_attributed_usage_payload,
    build_usage_facts_payload,
)

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "usage_facts_buzz_attribution"
)
BUZZ_ROOT = Path("/mnt/data/services/buzz-agent-workspaces")


def _origin_rows(db_path: Path, run_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db_path) as conn:
        run_origin = conn.execute(
            "SELECT origin FROM run_usage_facts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        call = conn.execute(
            "SELECT origin FROM run_llm_calls WHERE run_id=?", (run_id,)
        ).fetchone()
    return run_origin, call[0] if call is not None else None


def test_workspace_classifier_is_strict_and_fail_closed() -> None:
    assert buzz_agent_unit_for_workspace(BUZZ_ROOT / "codex") == "codex"
    assert buzz_agent_unit_for_workspace(BUZZ_ROOT / "qwen" / "REPOS" / "hermes") == "qwen"
    assert buzz_agent_unit_for_workspace(BUZZ_ROOT) is None
    assert buzz_agent_unit_for_workspace("relative/buzz-agent-workspaces/codex") is None
    assert buzz_agent_unit_for_workspace(BUZZ_ROOT / "codex" / ".." / ".." / "piet") is None
    assert buzz_agent_unit_for_workspace(BUZZ_ROOT / "codex" / ".." / "qwen") is None
    assert buzz_agent_unit_for_workspace("/home/piet") is None
    assert buzz_agent_unit_for_workspace(None) is None
    assert source_origin_for_run(
        BUZZ_AGENT_ORIGIN, "codex_cli:real-session"
    ) == ORIGIN_CODEX
    assert source_origin_for_run(
        BUZZ_AGENT_ORIGIN, "unknown-source:real-session"
    ) == BUZZ_AGENT_ORIGIN


def test_real_kimi_wire_line_separates_buzz_from_interactive() -> None:
    index_record = json.loads(
        (FIXTURES / "kimi_session_index.jsonl").read_text(encoding="utf-8")
    )
    wire = FIXTURES / "kimi_wire.jsonl"

    buzz = extract_kimi_wire(
        wire,
        session_id=index_record["sessionId"],
        workspace=index_record["workDir"],
    )
    interactive = extract_kimi_wire(
        wire,
        session_id="piet-interactive",
        workspace="/home/piet",
    )

    assert buzz is not None and buzz.origin == BUZZ_AGENT_ORIGIN
    assert buzz.run_fields["input_tokens"] == 29637
    assert buzz.run_fields["output_tokens"] == 213
    assert interactive is not None and interactive.origin == ORIGIN_KIMI


def test_all_foreign_extractors_apply_workspace_signal_fail_closed(tmp_path: Path) -> None:
    codex_source = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "foreign_lane_harvest"
        / "codex"
        / "rollout-golden.jsonl"
    )
    records = [json.loads(line) for line in codex_source.read_text().splitlines()]
    session_meta = next(row for row in records if row.get("type") == "session_meta")
    session_meta["payload"]["cwd"] = str(BUZZ_ROOT / "codex")
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    codex = extract_codex_rollout(rollout)
    assert codex is not None and codex.origin == BUZZ_AGENT_ORIGIN
    assert codex.rate_limit is not None
    assert codex.rate_limit.origin == ORIGIN_CODEX
    conflicting_records = [*records, {**session_meta, "payload": {**session_meta["payload"], "cwd": "/home/piet"}}]
    conflicting_rollout = tmp_path / "rollout-conflicting.jsonl"
    conflicting_rollout.write_text(
        "\n".join(json.dumps(row) for row in conflicting_records) + "\n"
    )
    conflicting_codex = extract_codex_rollout(conflicting_rollout)
    assert conflicting_codex is not None and conflicting_codex.origin == ORIGIN_CODEX

    qwen_row = {
        "schemaVersion": 1,
        "id": "qwen-buzz-row",
        "sessionId": "qwen-buzz-session",
        "model": "qwen3-coder-plus",
        "inputTokens": 3,
        "outputTokens": 2,
    }
    assert extract_qwen_row(qwen_row, workspace=str(BUZZ_ROOT / "qwen")).origin == BUZZ_AGENT_ORIGIN
    assert extract_qwen_row(qwen_row).origin == ORIGIN_QWEN

    grok_row = {
        "msg": "shell.turn.inference_done",
        "sid": "grok-buzz-session",
        "ctx": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    assert extract_grok_inference_event(grok_row, workspace=str(BUZZ_ROOT / "grok")).origin == BUZZ_AGENT_ORIGIN
    assert extract_grok_inference_event(grok_row).origin == ORIGIN_GROK


def test_qwen_and_grok_ambiguous_session_mapping_fails_closed(tmp_path: Path) -> None:
    sid = "same-session"
    qwen_projects = tmp_path / "qwen-projects"
    for project, cwd in (("buzz", BUZZ_ROOT / "qwen"), ("human", Path("/home/piet"))):
        chat = qwen_projects / project / "chats" / f"{sid}.jsonl"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"sessionId": sid, "cwd": str(cwd)}) + "\n")
    assert load_qwen_session_workspaces(qwen_projects, {sid}) == {}

    grok_sessions = tmp_path / "grok-sessions"
    for cwd in (BUZZ_ROOT / "grok", Path("/home/piet")):
        (grok_sessions / quote(str(cwd), safe="") / sid).mkdir(parents=True)
    assert load_grok_session_workspaces(grok_sessions, {sid}) == {}

    kimi_index = tmp_path / "kimi-index.jsonl"
    kimi_index.write_text(
        "\n".join(
            json.dumps({"sessionId": sid, "workDir": str(cwd)})
            for cwd in (BUZZ_ROOT / "kimi", Path("/home/piet"))
        )
        + "\n"
    )
    assert load_kimi_session_workspaces(kimi_index) == {}


def test_unreadable_grok_workspace_is_skipped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "grok-sessions"
    unreadable = root / quote(str(BUZZ_ROOT / "grok"), safe="")
    unreadable.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):
        if path == unreadable:
            raise PermissionError("fixture unreadable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    assert load_grok_session_workspaces(root, {"session"}) == {}


def test_existing_run_reclassification_updates_calls_once(tmp_path: Path) -> None:
    db_path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db_path)
    run_id = "codex_cli:existing-buzz-session"
    original = ExtractedRun(
        run_id=run_id,
        origin=ORIGIN_CODEX,
        run_fields={"origin": ORIGIN_CODEX, "source": "measured"},
        calls=[ExtractedCall(call_index=1, fields={"origin": ORIGIN_CODEX})],
    )
    assert write_extracted_run(original, db_path=db_path, include_calls=True)[:2] == (1, 1)

    reclassified = ExtractedRun(
        run_id=run_id,
        origin=BUZZ_AGENT_ORIGIN,
        run_fields={"origin": BUZZ_AGENT_ORIGIN, "source": "measured"},
    )
    assert write_extracted_run(reclassified, db_path=db_path) == (1, 0, 0)
    assert _origin_rows(db_path, run_id) == (BUZZ_AGENT_ORIGIN, BUZZ_AGENT_ORIGIN)
    assert write_extracted_run(reclassified, db_path=db_path) == (0, 0, 0)

    # Missing later evidence is not negative evidence: normal and forced
    # replays cannot erase a previously proven Buzz classification.
    assert write_extracted_run(original, db_path=db_path) == (0, 0, 0)
    assert _origin_rows(db_path, run_id) == (BUZZ_AGENT_ORIGIN, BUZZ_AGENT_ORIGIN)
    assert write_extracted_run(original, db_path=db_path, force=True) == (1, 0, 0)
    assert _origin_rows(db_path, run_id) == (BUZZ_AGENT_ORIGIN, BUZZ_AGENT_ORIGIN)


def test_claude_cwd_classification_and_family_backfill_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db_path)
    draft = merge_assistant_fragment(
        None,
        {
            "type": "assistant",
            "cwd": str(BUZZ_ROOT / "codex"),
            "sessionId": "buzz-claude-session",
            "requestId": "request-1",
            "message": {
                "id": "message-1",
                "model": "qwen3-coder-plus",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    )
    assert draft is not None
    call_fields, run_fields = draft_to_fields(draft)
    assert call_fields["origin"] == BUZZ_AGENT_ORIGIN
    assert run_fields["origin"] == BUZZ_AGENT_ORIGIN
    record_llm_call(draft.run_id, 0, call_fields, run_fields=run_fields, path=db_path)

    # A Buzz Codex row must not be mistaken for a Claude-family row merely
    # because both share the new class origin.
    record_llm_call(
        "codex_cli:not-claude",
        0,
        {"origin": BUZZ_AGENT_ORIGIN, "provider": "wrong", "model": "qwen3"},
        run_fields={"origin": BUZZ_AGENT_ORIGIN, "provider": "wrong", "model": "qwen3"},
        path=db_path,
    )
    report = backfill_provider_attribution(db_path=db_path, apply=True)
    assert report["totals"]["rows_checked"] == 2
    with sqlite3.connect(db_path) as conn:
        untouched = conn.execute(
            "SELECT provider FROM run_usage_facts WHERE run_id='codex_cli:not-claude'"
        ).fetchone()[0]
    assert untouched == "wrong"

    conflicting = merge_assistant_fragment(
        draft,
        {
            "type": "assistant",
            "cwd": "/home/piet",
            "sessionId": "buzz-claude-session",
            "requestId": "request-1",
            "message": {"id": "message-1", "model": "qwen3-coder-plus"},
        },
    )
    assert conflicting is not None
    conflicting_call, _conflicting_run = draft_to_fields(conflicting)
    assert conflicting_call["origin"] == "claude_code"


def test_claude_batch_replay_without_cwd_preserves_buzz_origin(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db_path)
    base = {
        "type": "assistant",
        "sessionId": "batch-session",
        "requestId": "batch-request",
        "message": {
            "id": "batch-message",
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    buzz = merge_assistant_fragment(None, {**base, "cwd": str(BUZZ_ROOT / "claude")})
    missing = merge_assistant_fragment(None, base)
    assert buzz is not None and missing is not None

    claude_harvester.write_call(buzz, db_path=db_path)
    claude_harvester.write_call(missing, db_path=db_path)

    assert _origin_rows(db_path, buzz.run_id) == (
        BUZZ_AGENT_ORIGIN,
        BUZZ_AGENT_ORIGIN,
    )


def test_old_foreign_state_version_resets_for_reclassification(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"version": 1, "sources": {ORIGIN_CODEX: {"files": {"old": "fp"}}}})
    )
    # Qwen's v2 cache-write backfill rides the same version bump: the reset
    # additionally carries the resumed-from marker. The reclassification
    # re-read happens either way (sources are cleared).
    assert load_state(state_path) == {
        "version": 2,
        "sources": {},
        "resumed_from_version": 1,
    }


def test_readmodel_keeps_source_token_semantics_inside_buzz_class(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db_path)
    common = {
        "origin": BUZZ_AGENT_ORIGIN,
        "provider": "same-provider",
        "model": "same-model",
        "lane": "same-lane",
        "billing_mode": "subscription_included",
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_tokens": 5,
        "cache_write_tokens": 2,
        "llm_call_count": 1,
        "source": "measured",
    }
    upsert_run_facts("claude_code:buzz-claude", common, path=db_path)
    upsert_run_facts("codex_cli:buzz-codex", common, path=db_path)

    payload = build_usage_facts_payload(
        db_path,
        origins=[BUZZ_AGENT_ORIGIN],
        rate_limit_path=tmp_path / "missing-rate-limits.jsonl",
    )
    groups = {
        group["key"]["source_origin"]: group
        for group in payload["groups"]
    }

    assert set(groups) == {ORIGIN_CODEX, "claude_code"}
    assert groups["claude_code"]["key"]["origin"] == BUZZ_AGENT_ORIGIN
    assert groups["claude_code"]["tokens"]["context_input"]["tokens"] == 17
    assert groups[ORIGIN_CODEX]["tokens"]["context_input"]["tokens"] == 10
    assert payload["summary"]["workload"]["unknown"] == {
        "fact_rows": 2,
        "context_input_tokens": 27,
        "coverage": {
            "status": "complete",
            "observed_rows": 2,
            "denominator_rows": 2,
        },
    }
    assert payload["normalization"]["origins"][BUZZ_AGENT_ORIGIN][
        "input_semantics"
    ] == "source_dependent"
    assert payload["normalization"]["origins"][BUZZ_AGENT_ORIGIN][
        "source_origins"
    ][ORIGIN_CODEX]["input_semantics"] == "cache_inclusive"
    assert set(
        payload["normalization"]["origins"][BUZZ_AGENT_ORIGIN][
            "source_origins"
        ]
    ) == {"claude_code", ORIGIN_CODEX, ORIGIN_GROK, ORIGIN_KIMI, ORIGIN_QWEN}

    attributed = build_attributed_usage_payload(
        db_path,
        origins=[BUZZ_AGENT_ORIGIN],
    )
    assert (
        attributed["models"]["buckets"][0]["tokens"]["context_input_tokens"]
        == 27
    )


def test_fleet_metrics_keep_buzz_source_freshness_and_contracts_separate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(db_path)
    for run_id, captured_at, duration_ms in (
        ("claude_code:stale-buzz", "2026-07-30T09:00:00+00:00", None),
        ("qwen_cli:fresh-buzz", "2026-07-30T12:59:00+00:00", 25),
    ):
        upsert_run_facts(
            run_id,
            {
                "origin": BUZZ_AGENT_ORIGIN,
                "provider": "fixture-provider",
                "model": "fixture-model",
                "input_tokens": 1,
                "output_tokens": 1,
                "duration_ms": duration_ms,
                "captured_at": captured_at,
                "source": "measured",
            },
            path=db_path,
        )

    payload = build_fleet_metrics_payload(
        db_path,
        tmp_path / "missing-board.db",
        generated_at="2026-07-30T13:00:00+00:00",
        sentinel_status_path=tmp_path / "missing-sentinel.json",
    )
    freshness = {
        item["source_origin"]: item
        for item in payload["alerts"]["data_freshness"]["sources"]
    }
    assert freshness["claude_code"]["status"] == "warning"
    assert freshness[ORIGIN_QWEN]["status"] == "ok"

    coverage = {
        group["key"]["source_origin"]: group
        for group in payload["provider_model_coverage"]["groups"]
    }
    assert coverage["claude_code"]["source_contract"]["duration"] == (
        "unavailable_source"
    )
    assert coverage[ORIGIN_QWEN]["source_contract"]["duration"] == "measured"

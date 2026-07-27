"""Tests for the Claude Code transcript → usage_facts ETL harvester."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import hermes_cli.claude_code_harvester as harvester_mod
from hermes_cli.claude_code_harvester import (
    CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION,
    NO_REQUEST_ID,
    draft_to_fields,
    harvest,
    load_agent_meta,
    make_run_id,
    merge_assistant_fragment,
    parse_transcript_file,
    request_id_or_fallback,
)
from hermes_cli.usage_facts_db import initialize_usage_facts_db

FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "claude_code_harvest"
)
PROJECTS = FIXTURES / "projects"
GOLDEN = json.loads((FIXTURES / "golden" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "usage_facts.db"
    initialize_usage_facts_db(path)
    return path


@pytest.fixture(autouse=True)
def _no_host_access_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transcript fixtures independent from the host's access settings."""
    monkeypatch.setattr(harvester_mod, "_subscription_plan_label", lambda: None)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_format_version_pinned_to_golden_fixture() -> None:
    assert GOLDEN["format_version"] == CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION
    assert CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION == 3


def test_request_id_fallback_is_stable() -> None:
    assert request_id_or_fallback(None) == NO_REQUEST_ID
    assert request_id_or_fallback("") == NO_REQUEST_ID
    assert request_id_or_fallback("  ") == NO_REQUEST_ID
    assert request_id_or_fallback("req_abc") == "req_abc"
    assert make_run_id("msg_1", None).endswith(f":{NO_REQUEST_ID}")


def test_streaming_fragments_merge_without_double_counting_tokens() -> None:
    flat = (
        PROJECTS
        / "flat-project"
        / "7df0db96-dab8-4f17-92bc-a3a57004b32d.jsonl"
    )
    drafts, stats = parse_transcript_file(flat, projects_root=PROJECTS)
    expected = GOLDEN["flat_streaming"]
    run_id = make_run_id(expected["message_id"], expected["request_id"])
    assert run_id in drafts
    draft = drafts[run_id]
    assert draft.fragment_count == expected["fragments"]
    assert draft.output_tokens == expected["output_tokens"]
    assert draft.input_tokens == expected["input_tokens"]
    assert draft.cache_read_tokens == expected["cache_read_input_tokens"]
    assert draft.cache_write_tokens == expected["cache_creation_input_tokens"]
    # Must not sum the two identical usage blobs (would be 528).
    assert draft.output_tokens != expected["output_tokens"] * 2
    assert draft.tool_call_count == 1
    assert draft.tool_output_chars == 32
    assert stats.calls_merged >= 1


def test_golden_file_harvest_writes_expected_measured_fields(db_path: Path, tmp_path: Path) -> None:
    state = tmp_path / "hwm.json"
    stats = harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=state,
    )
    assert stats.calls_written >= 1
    expected = GOLDEN["flat_streaming"]
    run_id = make_run_id(expected["message_id"], expected["request_id"])
    with _connect(db_path) as conn:
        call = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert call is not None
    assert run is not None
    assert call["origin"] == "claude_code"
    assert run["origin"] == "claude_code"
    assert call["model"] == expected["model"]
    assert call["response_id"] == expected["message_id"]
    assert call["input_tokens"] == expected["input_tokens"]
    assert call["output_tokens"] == expected["output_tokens"]
    assert call["cache_read_tokens"] == expected["cache_read_input_tokens"]
    assert call["cache_write_tokens"] == expected["cache_creation_input_tokens"]
    assert call["finish_reason"] == expected["stop_reason"]
    assert call["serving_tier"] == expected["service_tier"]
    assert call["tool_call_count"] == 1
    assert call["tool_output_chars"] == 32
    # Unavailable observations must stay NULL — never estimated.
    assert call["first_token_ms"] is None
    assert call["duration_ms"] is None
    assert call["context_window_used"] is None
    assert run["source"] == "measured"
    # Current transcripts carry no explicit billing metadata.  Inference from
    # userType or service_tier would be a guess.  The test's access config is
    # deliberately absent, so the unknown is explicit.
    assert run["billing_mode"] == "unknown"
    assert run["billing_mode_source"] is None


def test_explicit_raw_billing_mode_is_preserved() -> None:
    record = {
        "type": "assistant",
        "requestId": "req_1",
        "billing_mode": "raw-direct-mode",
        "message": {
            "id": "msg_1",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }

    draft = merge_assistant_fragment(None, record)

    assert draft is not None
    _, run_fields = draft_to_fields(draft)
    assert run_fields["billing_mode"] == "raw-direct-mode"
    assert run_fields["billing_mode_source"] == "transcript"


def test_billing_mode_requires_subscription_and_no_metered_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration inference must not inspect or retain any credential value."""
    monkeypatch.setattr(harvester_mod, "_subscription_plan_label", lambda: "plan")
    monkeypatch.setattr(harvester_mod, "_has_configured_metered_access", lambda: False)

    assert harvester_mod._billing_mode_from_access_configuration() == (
        "subscription_included",
        "access_configuration",
    )

    monkeypatch.setattr(harvester_mod, "_has_configured_metered_access", lambda: True)
    assert harvester_mod._billing_mode_from_access_configuration() == ("unknown", None)

    monkeypatch.setattr(harvester_mod, "_subscription_plan_label", lambda: None)
    monkeypatch.setattr(harvester_mod, "_has_configured_metered_access", lambda: False)
    assert harvester_mod._billing_mode_from_access_configuration() == ("unknown", None)


def test_harvest_persists_access_configuration_billing_provenance(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harvester_mod, "_subscription_plan_label", lambda: "plan")
    monkeypatch.setattr(harvester_mod, "_has_configured_metered_access", lambda: False)

    harvest(projects_root=PROJECTS, db_path=db_path)

    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT billing_mode, billing_mode_source
            FROM run_usage_facts
            WHERE origin = ?
            LIMIT 1
            """,
            ("claude_code",),
        ).fetchone()

    assert tuple(row) == ("subscription_included", "access_configuration")


def test_metered_access_detection_uses_configuration_keys_not_values(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    api_key_name = f"{harvester_mod.PROVIDER.upper()}_API_KEY"
    config.write_text(f"{api_key_name}: configured\n", encoding="utf-8")

    class PresenceOnlyEnv(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            raise AssertionError(f"credential value read for {key}")

    assert harvester_mod._has_configured_metered_access(
        environ=PresenceOnlyEnv({api_key_name: "configured"}), config_paths=()
    )
    assert harvester_mod._has_configured_metered_access(
        environ=PresenceOnlyEnv(), config_paths=(config,)
    )


def test_reharvest_backfills_billing_mode_without_changing_tokens(
    db_path: Path, tmp_path: Path
) -> None:
    state = tmp_path / "hwm.json"
    harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=state,
    )
    expected = GOLDEN["flat_streaming"]
    run_id = make_run_id(expected["message_id"], expected["request_id"])
    with _connect(db_path) as conn:
        before = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens FROM run_llm_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        conn.execute(
            "UPDATE run_usage_facts SET billing_mode=NULL WHERE run_id=?",
            (run_id,),
        )
        conn.commit()

    # A parser-format bump forces an actual re-harvest of otherwise HWM-skipped
    # files, so existing rows receive the new non-null metadata.
    persisted_hwm = json.loads(state.read_text(encoding="utf-8"))
    persisted_hwm["format_version"] = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION - 1
    state.write_text(json.dumps(persisted_hwm), encoding="utf-8")
    second = harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=state,
    )

    with _connect(db_path) as conn:
        after = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens FROM run_llm_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        billing_mode = conn.execute(
            "SELECT billing_mode FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()["billing_mode"]
    assert second.files_processed >= 1
    assert billing_mode == "unknown"
    assert after == before


def test_update_existing_only_backfill_skips_new_raw_facts(
    db_path: Path, tmp_path: Path
) -> None:
    projects_root = tmp_path / "projects"
    shutil.copytree(PROJECTS / "flat-project", projects_root)
    state = tmp_path / "hwm.json"
    harvest(projects_root=projects_root, db_path=db_path, state_path=state)

    expected = GOLDEN["flat_streaming"]
    existing_run_id = make_run_id(expected["message_id"], expected["request_id"])
    new_message_id = "new-message-not-in-snapshot"
    new_run_id = make_run_id(new_message_id, expected["request_id"])
    source = next(projects_root.glob("*.jsonl"))
    (projects_root / "new-raw-fact.jsonl").write_text(
        source.read_text(encoding="utf-8").replace(
            expected["message_id"], new_message_id
        ),
        encoding="utf-8",
    )
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE run_usage_facts SET billing_mode=NULL WHERE run_id=?",
            (existing_run_id,),
        )
        before = conn.execute(
            "SELECT COUNT(*) AS rows, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens FROM run_usage_facts"
        ).fetchone()
        conn.commit()

    persisted_hwm = json.loads(state.read_text(encoding="utf-8"))
    persisted_hwm["format_version"] = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION - 1
    state.write_text(json.dumps(persisted_hwm), encoding="utf-8")
    harvest(
        projects_root=projects_root,
        db_path=db_path,
        state_path=state,
        update_existing_only=True,
    )

    with _connect(db_path) as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS rows, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens FROM run_usage_facts"
        ).fetchone()
        billing_mode = conn.execute(
            "SELECT billing_mode FROM run_usage_facts WHERE run_id=?",
            (existing_run_id,),
        ).fetchone()["billing_mode"]
        new_fact = conn.execute(
            "SELECT 1 FROM run_usage_facts WHERE run_id=?", (new_run_id,)
        ).fetchone()

    assert billing_mode == "unknown"
    assert after == before
    assert new_fact is None


def test_resume_pair_is_idempotent_across_sessions(db_path: Path, tmp_path: Path) -> None:
    """Real resume pair: same message.id + requestId, two session files."""
    mid = GOLDEN["resume_message_id"]
    state = tmp_path / "hwm.json"
    harvest(
        projects_root=PROJECTS / "resume-project",
        db_path=db_path,
        state_path=state,
    )
    # Second full harvest (HWM would skip; force re-read by clearing state).
    state.unlink()
    harvest(
        projects_root=PROJECTS / "resume-project",
        db_path=db_path,
        state_path=state,
    )
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, response_id, origin FROM run_llm_calls "
            "WHERE response_id=?",
            (mid,),
        ).fetchall()
        facts = conn.execute(
            "SELECT run_id FROM run_usage_facts WHERE run_id LIKE ?",
            (f"claude_code:{mid}:%",),
        ).fetchall()
    assert len(rows) == 1, rows
    assert len(facts) == 1
    assert rows[0]["origin"] == "claude_code"


def test_flat_session_layout_is_processed(db_path: Path, tmp_path: Path) -> None:
    stats = harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=tmp_path / "hwm.json",
    )
    assert stats.files_processed >= 1
    with _connect(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM run_llm_calls WHERE origin='claude_code'"
        ).fetchone()["c"]
    assert n >= 1


def test_session_dir_subagent_layout_and_sparse_meta(db_path: Path, tmp_path: Path) -> None:
    meta_path = (
        PROJECTS
        / "session-dir-project"
        / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        / "subagents"
        / "agent-fixture0001.meta.json"
    )
    meta = load_agent_meta(meta_path)
    assert list(meta.keys()) == ["agentType"]
    assert "spawnDepth" not in meta
    assert "toolUseId" not in meta

    harvest(
        projects_root=PROJECTS / "session-dir-project",
        db_path=db_path,
        state_path=tmp_path / "hwm.json",
    )
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_llm_calls WHERE origin='claude_code'"
        ).fetchall()
        runs = conn.execute(
            "SELECT * FROM run_usage_facts WHERE origin='claude_code'"
        ).fetchall()
    assert len(rows) == 1
    assert len(runs) == 1
    assert runs[0]["call_kind"] == "subagent"
    assert runs[0]["profile"] == "general-purpose"
    assert runs[0]["lane"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # Parent toolUseId absent → association falls back to parent session path.
    assert rows[0]["model_source"] == (
        "parent_session:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )


def test_missing_request_id_uses_fallback_and_is_idempotent(
    db_path: Path, tmp_path: Path
) -> None:
    mid = GOLDEN["missing_request_id_message_id"]
    root = PROJECTS / "flat-project"
    # Isolate only the missing-request-id file.
    alone = tmp_path / "alone"
    alone.mkdir()
    src = root / "missing-request-id.jsonl"
    target = alone / "missing-request-id.jsonl"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    state = tmp_path / "hwm.json"
    harvest(projects_root=alone, db_path=db_path, state_path=state)
    harvest(projects_root=alone, db_path=db_path, state_path=state)
    run_id = make_run_id(mid, NO_REQUEST_ID)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id=?",
            (run_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["response_id"] == mid
    assert rows[0]["origin"] == "claude_code"


def test_high_water_mark_second_run_writes_zero_rows(
    db_path: Path, tmp_path: Path
) -> None:
    state = tmp_path / "hwm.json"
    first = harvest(
        projects_root=PROJECTS,
        db_path=db_path,
        state_path=state,
    )
    assert first.calls_written >= 1
    assert first.files_processed >= 1
    second = harvest(
        projects_root=PROJECTS,
        db_path=db_path,
        state_path=state,
    )
    assert second.calls_written == 0
    assert second.files_processed == 0
    assert second.files_skipped_hwm == first.files_seen


def test_lines_without_type_are_skipped_not_fatal() -> None:
    flat = (
        PROJECTS
        / "flat-project"
        / "7df0db96-dab8-4f17-92bc-a3a57004b32d.jsonl"
    )
    drafts, stats = parse_transcript_file(flat, projects_root=PROJECTS)
    assert stats.lines_skipped >= 1
    assert drafts  # still produced calls from assistant lines


def test_merge_assistant_fragment_unions_tool_uses() -> None:
    base = {
        "type": "assistant",
        "requestId": "req_1",
        "sessionId": "sess",
        "message": {
            "id": "msg_1",
            "model": "claude-test",
            "stop_reason": None,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 10,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "service_tier": "standard",
            },
            "content": [{"type": "thinking", "thinking": "x"}],
        },
    }
    second = json.loads(json.dumps(base))
    second["message"]["content"] = [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}
    ]
    second["message"]["stop_reason"] = "tool_use"
    draft = merge_assistant_fragment(None, base)
    draft = merge_assistant_fragment(draft, second)
    assert draft is not None
    assert draft.fragment_count == 2
    assert draft.output_tokens == 10
    assert draft.tool_call_count == 1
    assert draft.stop_reason == "tool_use"

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
from hermes_cli.usage_facts_db import initialize_usage_facts_db, upsert_run_facts

FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "claude_code_harvest"
)
PROJECTS = FIXTURES / "projects"
GOLDEN = json.loads((FIXTURES / "golden" / "expected.json").read_text(encoding="utf-8"))
REAL_ROWS = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "usage_state_backfill"
        / "real_rows.json"
    ).read_text(encoding="utf-8")
)


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
    assert CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION == 6


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
    assert run["session_id"] == expected["session_id"]


def test_exact_claude_session_correlation_is_persisted(
    db_path: Path, tmp_path: Path
) -> None:
    kanban = tmp_path / "kanban.db"
    with sqlite3.connect(kanban) as conn:
        conn.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            (
                42,
                "task-exact",
                "critic",
                json.dumps(
                    {
                        "claude_session_id": GOLDEN["flat_streaming"]["session_id"],
                        "chain_id": "chain-exact",
                    }
                ),
            ),
        )

    stats = harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=tmp_path / "correlated-hwm.json",
        kanban_paths=[kanban],
    )

    run_id = make_run_id(
        GOLDEN["flat_streaming"]["message_id"],
        GOLDEN["flat_streaming"]["request_id"],
    )
    with _connect(db_path) as conn:
        run = conn.execute(
            "SELECT task_run_id, task_id, chain_id, board, session_id, "
            "correlation_source, profile FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert tuple(run) == (
        "42",
        "task-exact",
        "chain-exact",
        None,
        GOLDEN["flat_streaming"]["session_id"],
        "claude_session_id_run",
        "critic",
    )
    assert stats.sessions_correlated == 1
    assert stats.calls_correlated >= 1


def test_same_session_multiple_runs_keeps_exact_task_without_guessing_run(
    tmp_path: Path,
) -> None:
    from hermes_cli.usage_fact_correlation import load_claude_session_correlations

    session_id = "session-reused"
    kanban = tmp_path / "custom.db"
    with sqlite3.connect(kanban) as conn:
        conn.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        conn.executemany(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            [
                (1, "task-one", "coder", json.dumps({"claude_session_id": session_id})),
                (2, "task-one", "coder", json.dumps({"claude_session_id": session_id})),
            ],
        )

    correlation = load_claude_session_correlations(
        [session_id],
        kanban_paths=[kanban],
    )[session_id]

    assert correlation.task_id == "task-one"
    assert correlation.task_run_id is None
    assert correlation.board is None
    assert correlation.source == "claude_session_id_task"


def test_unreadable_board_scan_does_not_invent_ambiguity(tmp_path: Path) -> None:
    from hermes_cli.usage_fact_correlation import scan_claude_session_correlations

    old_board = tmp_path / "old-board.db"
    with sqlite3.connect(old_board) as connection:
        connection.execute("CREATE TABLE legacy_only (id INTEGER PRIMARY KEY)")

    scan = scan_claude_session_correlations(
        ["session-unresolved"],
        kanban_paths=[old_board],
    )

    assert scan.correlations == {}
    assert scan.ambiguous_session_ids == frozenset()
    assert scan.databases_scanned == 0
    assert scan.databases_failed == 1


def test_hwm_skipped_transcript_is_recorrelated_after_task_metadata_lands(
    db_path: Path,
    tmp_path: Path,
) -> None:
    state = tmp_path / "recorrelation-hwm.json"
    first = harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=state,
        kanban_paths=[],
    )
    assert first.calls_recorrelated == 0

    kanban = tmp_path / "kanban.db"
    with sqlite3.connect(kanban) as conn:
        conn.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            (
                42,
                "task-late",
                "critic",
                json.dumps(
                    {"claude_session_id": GOLDEN["flat_streaming"]["session_id"]}
                ),
            ),
        )

    second = harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=state,
        kanban_paths=[kanban],
    )

    run_id = make_run_id(
        GOLDEN["flat_streaming"]["message_id"],
        GOLDEN["flat_streaming"]["request_id"],
    )
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT task_run_id, task_id, correlation_source "
            "FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert second.files_skipped_hwm >= 1
    assert second.calls_recorrelated >= 1
    assert tuple(row) == ("42", "task-late", "claude_session_id_run")


def test_real_review_worktree_path_backfills_exact_run_without_session_match(
    db_path: Path,
    tmp_path: Path,
) -> None:
    fixture = REAL_ROWS["claude_worktree"]
    transcript_path = Path(fixture["transcript_path"])
    project = tmp_path / transcript_path.parent.name
    project.mkdir()
    (project / transcript_path.name).touch()

    task_run = fixture["task_run"]
    kanban = tmp_path / "kanban.db"
    with sqlite3.connect(kanban) as connection:
        connection.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            (
                task_run["id"],
                task_run["task_id"],
                task_run["profile"],
                json.dumps(task_run["metadata"]),
            ),
        )
    upsert_run_facts(
        "claude-real-path",
        {
            "origin": "claude_code",
            "session_id": fixture["session_id"],
            "captured_at": "2026-07-31T09:15:00Z",
        },
        path=db_path,
    )

    report = harvester_mod.backfill_existing_correlations(
        db_path=db_path,
        kanban_paths=[kanban],
        projects_root=tmp_path,
        apply=True,
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, correlation_source "
            "FROM run_usage_facts WHERE run_id='claude-real-path'"
        ).fetchone()
    assert tuple(row) == (
        "8896",
        "t_c8adf037",
        "claude_worktree_path_run",
    )
    assert report["updated_facts"] == 1


def test_worktree_path_requires_matching_run_and_task(tmp_path: Path) -> None:
    from hermes_cli.usage_fact_correlation import scan_claude_session_correlations

    fixture = REAL_ROWS["claude_worktree"]
    transcript = Path(fixture["transcript_path"])
    task_run = fixture["task_run"]
    kanban = tmp_path / "kanban.db"
    with sqlite3.connect(kanban) as connection:
        connection.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            (task_run["id"], "t_deadbeef", task_run["profile"], "{}"),
        )

    scan = scan_claude_session_correlations(
        [fixture["session_id"]],
        kanban_paths=[kanban],
        transcript_paths=[transcript],
    )

    assert scan.correlations == {}
    assert scan.ambiguous_session_ids == frozenset()


def test_discovery_uses_shared_kanban_home_and_never_invents_custom_board(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hermes_cli.usage_fact_correlation import (
        _board_for_path,
        discover_kanban_databases,
    )

    shared = tmp_path / "shared"
    default_db = shared / "kanban.db"
    named_db = shared / "kanban" / "boards" / "research" / "kanban.db"
    custom_db = tmp_path / "external" / "kanban.db"
    for path in (default_db, named_db, custom_db):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(shared))
    monkeypatch.setenv("HERMES_HOME", str(shared / "profiles" / "critic"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)

    discovered = discover_kanban_databases()

    assert discovered == (default_db.resolve(), named_db.resolve())
    assert _board_for_path(default_db) == "default"
    assert _board_for_path(named_db) == "research"
    assert _board_for_path(custom_db) is None


def test_ambiguous_claude_session_never_guesses_a_task(
    db_path: Path, tmp_path: Path
) -> None:
    session_id = GOLDEN["flat_streaming"]["session_id"]
    kanban = tmp_path / "ambiguous.db"
    with sqlite3.connect(kanban) as conn:
        conn.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        conn.executemany(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            [
                (1, "task-a", "critic", json.dumps({"claude_session_id": session_id})),
                (2, "task-b", "critic", json.dumps({"claude_session_id": session_id})),
            ],
        )

    harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=tmp_path / "ambiguous-hwm.json",
        kanban_paths=[kanban],
    )

    run_id = make_run_id(
        GOLDEN["flat_streaming"]["message_id"],
        GOLDEN["flat_streaming"]["request_id"],
    )
    with _connect(db_path) as conn:
        run = conn.execute(
            "SELECT task_id, correlation_source, session_id "
            "FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert tuple(run) == (None, None, session_id)


def test_recorrelation_clears_a_link_that_later_becomes_ambiguous(
    db_path: Path,
) -> None:
    session_id = "session-later-reused"
    upsert_run_facts(
        "claude-exact",
        {
            "origin": "claude_code",
            "session_id": session_id,
            "task_run_id": "42",
            "task_id": "task-before",
            "chain_id": "chain-before",
            "board": "default",
            "correlation_source": "claude_session_id_run",
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )

    changed = harvester_mod.recorrelate_existing_calls(
        {},
        db_path=db_path,
        dry_run=False,
        ambiguous_session_ids={session_id},
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, chain_id, board, "
            "correlation_source FROM run_usage_facts WHERE run_id='claude-exact'"
        ).fetchone()
    assert changed == 1
    assert tuple(row) == (None, None, None, None, None)


def test_recorrelation_clears_parent_sourced_subagent_link_when_ambiguous(
    db_path: Path,
) -> None:
    parent_session_id = "parent-session-later-reused"
    upsert_run_facts(
        "claude-parent-exact",
        {
            "origin": "claude_code",
            "session_id": "child-session",
            "lane": parent_session_id,
            "call_kind": "subagent",
            "task_run_id": "77",
            "task_id": "task-before",
            "chain_id": "chain-before",
            "board": "default",
            "correlation_source": "claude_parent_session_id_run",
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )

    changed = harvester_mod.recorrelate_existing_calls(
        {},
        db_path=db_path,
        dry_run=False,
        ambiguous_session_ids={parent_session_id},
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, chain_id, board, "
            "correlation_source FROM run_usage_facts "
            "WHERE run_id='claude-parent-exact'"
        ).fetchone()
    assert changed == 1
    assert tuple(row) == (None, None, None, None, None)


def test_hwm_parent_subagent_link_is_recorrelated_from_lane(
    db_path: Path,
) -> None:
    from hermes_cli.usage_fact_correlation import WorkerCorrelation

    parent_session_id = "parent-session-late"
    upsert_run_facts(
        "claude-parent-late",
        {
            "origin": "claude_code",
            "session_id": "child-session-late",
            "lane": parent_session_id,
            "call_kind": "subagent",
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )
    correlation = WorkerCorrelation(
        session_id=parent_session_id,
        task_run_id="88",
        task_id="task-late",
        chain_id="chain-late",
        board="default",
    )

    changed = harvester_mod.recorrelate_existing_calls(
        {parent_session_id: correlation},
        db_path=db_path,
        dry_run=False,
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, chain_id, board, "
            "correlation_source FROM run_usage_facts "
            "WHERE run_id='claude-parent-late'"
        ).fetchone()
    assert changed == 1
    assert tuple(row) == (
        "88",
        "task-late",
        "chain-late",
        "default",
        "claude_parent_session_id_run",
    )


@pytest.mark.parametrize("reverse", (False, True))
def test_hwm_recorrelation_rejects_conflicting_parent_and_child_runs(
    db_path: Path,
    reverse: bool,
) -> None:
    from hermes_cli.usage_fact_correlation import WorkerCorrelation

    parent_id = "parent-conflicting-run"
    child_id = "child-conflicting-run"
    upsert_run_facts(
        "claude-conflict",
        {
            "origin": "claude_code",
            "session_id": child_id,
            "lane": parent_id,
            "call_kind": "subagent",
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )
    pairs = [
        (
            parent_id,
            WorkerCorrelation(
                session_id=parent_id,
                task_run_id="101",
                task_id="task-parent",
                chain_id="chain-parent",
                board="default",
            ),
        ),
        (
            child_id,
            WorkerCorrelation(
                session_id=child_id,
                task_run_id="202",
                task_id="task-child",
                chain_id="chain-child",
                board="default",
            ),
        ),
    ]
    if reverse:
        pairs.reverse()

    changed = harvester_mod.recorrelate_existing_calls(
        dict(pairs),
        db_path=db_path,
        dry_run=False,
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, chain_id, board, "
            "correlation_source FROM run_usage_facts "
            "WHERE run_id='claude-conflict'"
        ).fetchone()
    assert changed == 0
    assert tuple(row) == (None, None, None, None, None)


def test_unresolved_session_never_clears_an_existing_exact_link(
    db_path: Path,
) -> None:
    session_id = "session-board-unavailable"
    upsert_run_facts(
        "claude-exact",
        {
            "origin": "claude_code",
            "session_id": session_id,
            "task_run_id": "42",
            "task_id": "task-before",
            "chain_id": "chain-before",
            "board": "default",
            "correlation_source": "claude_session_id_run",
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )

    changed = harvester_mod.recorrelate_existing_calls(
        {},
        db_path=db_path,
        dry_run=False,
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, chain_id, board, "
            "correlation_source FROM run_usage_facts WHERE run_id='claude-exact'"
        ).fetchone()
    assert changed == 0
    assert tuple(row) == (
        "42",
        "task-before",
        "chain-before",
        "default",
        "claude_session_id_run",
    )


def test_task_level_recorrelation_never_downgrades_an_exact_run(
    db_path: Path,
) -> None:
    from hermes_cli.usage_fact_correlation import WorkerCorrelation

    session_id = "session-continuation"
    upsert_run_facts(
        "claude-exact",
        {
            "origin": "claude_code",
            "session_id": session_id,
            "task_run_id": "42",
            "task_id": "task-one",
            "chain_id": "chain-one",
            "board": "default",
            "correlation_source": "claude_session_id_run",
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )

    changed = harvester_mod.recorrelate_existing_calls(
        {
            session_id: WorkerCorrelation(
                session_id=session_id,
                task_id="task-one",
                source="claude_session_id_task",
            )
        },
        db_path=db_path,
        dry_run=False,
    )

    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id, chain_id, board, "
            "correlation_source FROM run_usage_facts WHERE run_id='claude-exact'"
        ).fetchone()
    assert changed == 0
    assert tuple(row) == (
        "42",
        "task-one",
        "chain-one",
        "default",
        "claude_session_id_run",
    )


def test_explicit_correlation_backfill_reports_before_after_and_board_errors(
    db_path: Path,
    tmp_path: Path,
) -> None:
    sessions = {
        "run": "session-exact-run",
        "task": "session-exact-task",
        "ambiguous": "session-ambiguous",
        "unresolved": "session-unresolved",
    }
    for name, session_id in sessions.items():
        upsert_run_facts(
            f"claude-{name}",
            {
                "origin": "claude_code",
                "session_id": session_id,
                "captured_at": "2026-07-29T00:00:00Z",
            },
            path=db_path,
        )

    board = tmp_path / "kanban.db"
    with sqlite3.connect(board) as connection:
        connection.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        connection.executemany(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            [
                (1, "task-run", "coder", json.dumps({"claude_session_id": sessions["run"]})),
                (2, "task-only", "coder", json.dumps({"claude_session_id": sessions["task"]})),
                (3, "task-only", "coder", json.dumps({"claude_session_id": sessions["task"]})),
                (4, "task-a", "coder", json.dumps({"claude_session_id": sessions["ambiguous"]})),
                (5, "task-b", "coder", json.dumps({"claude_session_id": sessions["ambiguous"]})),
            ],
        )
    old_board = tmp_path / "old.db"
    with sqlite3.connect(old_board) as connection:
        connection.execute("CREATE TABLE legacy_only (id INTEGER PRIMARY KEY)")

    report = harvester_mod.backfill_existing_correlations(
        db_path=db_path,
        kanban_paths=[board, old_board],
        apply=True,
    )

    assert report["before"] == {
        "candidate_sessions": 4,
        "exact_run_links": 0,
        "task_only_links": 0,
        "ambiguous_sessions": 1,
        "unresolved_sessions": 4,
    }
    assert report["after"] == {
        "candidate_sessions": 4,
        "exact_run_links": 1,
        "task_only_links": 1,
        "ambiguous_sessions": 1,
        "unresolved_sessions": 2,
    }
    assert report["updated_facts"] == 2
    assert report["applied"] is True
    assert [item["status"] for item in report["board_databases"]] == ["ok", "error"]
    assert report["board_databases"][1]["error"] == "OperationalError"
    assert "session" not in json.dumps(report["board_databases"])

    second = harvester_mod.backfill_existing_correlations(
        db_path=db_path,
        kanban_paths=[board, old_board],
        apply=True,
    )
    assert second["updated_facts"] == 0
    assert second["after"] == report["after"]


def test_correlation_backfill_dry_run_is_read_only(db_path: Path, tmp_path: Path) -> None:
    session_id = "dry-run-session"
    upsert_run_facts(
        "claude-dry-run",
        {
            "origin": "claude_code",
            "session_id": session_id,
            "captured_at": "2026-07-29T00:00:00Z",
        },
        path=db_path,
    )
    board = tmp_path / "kanban.db"
    with sqlite3.connect(board) as connection:
        connection.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            (1, "task-one", "coder", json.dumps({"claude_session_id": session_id})),
        )

    report = harvester_mod.backfill_existing_correlations(
        db_path=db_path,
        kanban_paths=[board],
        apply=False,
    )

    assert report["applied"] is False
    assert report["updated_facts"] == 1
    assert report["after"]["exact_run_links"] == 1
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_run_id, task_id FROM run_usage_facts "
            "WHERE run_id='claude-dry-run'"
        ).fetchone()
    assert tuple(row) == (None, None)


def test_backfill_cli_is_explicit_and_preview_only_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_backfill(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"applied": kwargs["apply"], "updated_facts": 7}

    monkeypatch.setattr(harvester_mod, "backfill_existing_correlations", fake_backfill)

    assert harvester_mod.main(["--backfill-correlations"]) == 0
    assert calls == [
        {
            "db_path": None,
            "projects_root": harvester_mod.DEFAULT_PROJECTS_ROOT,
            "apply": False,
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "updated_facts": 7,
    }


def test_harvest_writes_occurred_at_from_fixture_timestamp(
    db_path: Path, tmp_path: Path
) -> None:
    """Source event time must be the transcript timestamp, not harvest wall clock.

    The flat streaming fixture has two assistant fragments; merge keeps the last
    non-empty ``timestamp`` (same as ``draft.timestamp``).
    """
    flat = (
        PROJECTS
        / "flat-project"
        / "7df0db96-dab8-4f17-92bc-a3a57004b32d.jsonl"
    )
    # Last assistant fragment timestamp, literal from the fixture file.
    fixture_timestamp = "2026-07-12T06:21:53.803Z"
    drafts, _stats = parse_transcript_file(flat, projects_root=PROJECTS)
    expected = GOLDEN["flat_streaming"]
    run_id = make_run_id(expected["message_id"], expected["request_id"])
    assert drafts[run_id].timestamp == fixture_timestamp

    harvest(
        projects_root=PROJECTS / "flat-project",
        db_path=db_path,
        state_path=tmp_path / "occurred-hwm.json",
    )
    with _connect(db_path) as conn:
        call = conn.execute(
            "SELECT occurred_at FROM run_llm_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT first_call_at, last_call_at, captured_at "
            "FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert call is not None
    assert call["occurred_at"] == fixture_timestamp
    assert run["first_call_at"] == fixture_timestamp
    assert run["last_call_at"] == fixture_timestamp
    # Write time is not the transcript event time.
    assert run["captured_at"] != fixture_timestamp


def test_draft_without_timestamp_yields_null_occurred_at() -> None:
    draft = merge_assistant_fragment(
        None,
        {
            "type": "assistant",
            "requestId": "req_no_ts",
            "message": {
                "id": "msg_no_ts",
                "model": "claude-test",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            # no timestamp key
        },
    )
    assert draft is not None
    assert draft.timestamp is None
    call_fields, _run_fields = draft_to_fields(draft)
    assert call_fields.get("occurred_at") is None


def test_timestamp_backfill_preview_writes_nothing(
    db_path: Path, tmp_path: Path
) -> None:
    expected = GOLDEN["flat_streaming"]
    run_id = make_run_id(expected["message_id"], expected["request_id"])
    # Pre-migration shape: fact rows without event times (NULL).
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO run_usage_facts "
            "(run_id, origin, source, input_tokens, captured_at) "
            "VALUES (?, 'claude_code', 'measured', 2, '2026-07-29T00:00:00Z')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO run_llm_calls "
            "(run_id, call_index, origin, input_tokens, occurred_at) "
            "VALUES (?, 0, 'claude_code', 2, NULL)",
            (run_id,),
        )
        conn.commit()
        before_calls = conn.execute(
            "SELECT COUNT(*) FROM run_llm_calls"
        ).fetchone()[0]
        before_null = conn.execute(
            "SELECT COUNT(*) FROM run_llm_calls WHERE occurred_at IS NULL"
        ).fetchone()[0]
        before_captured = conn.execute(
            "SELECT captured_at FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]

    report = harvester_mod.backfill_occurred_at(
        db_path=db_path,
        projects_root=PROJECTS / "flat-project",
        apply=False,
    )

    assert report["applied"] is False
    assert report["calls_to_fill"] == 1
    assert report["runs_to_fill"] == 1
    assert report["calls_filled"] == 0
    assert report["runs_filled"] == 0
    with _connect(db_path) as conn:
        after_calls = conn.execute(
            "SELECT COUNT(*) FROM run_llm_calls"
        ).fetchone()[0]
        after_null = conn.execute(
            "SELECT COUNT(*) FROM run_llm_calls WHERE occurred_at IS NULL"
        ).fetchone()[0]
        after_captured = conn.execute(
            "SELECT captured_at, first_call_at, last_call_at "
            "FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert after_calls == before_calls
    assert after_null == before_null
    assert after_captured["captured_at"] == before_captured
    assert after_captured["first_call_at"] is None
    assert after_captured["last_call_at"] is None


def test_timestamp_backfill_apply_fills_only_null_time_columns(
    db_path: Path, tmp_path: Path
) -> None:
    expected = GOLDEN["flat_streaming"]
    run_id = make_run_id(expected["message_id"], expected["request_id"])
    fixture_timestamp = "2026-07-12T06:21:53.803Z"
    prefilled_call_time = "2019-06-01T00:00:00Z"
    prefilled_first = "2019-01-01T00:00:00Z"
    # One NULL call to fill, one prefilled call that must stay untouched.
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO run_usage_facts "
            "(run_id, origin, source, input_tokens, captured_at, "
            "first_call_at, task_id, correlation_source) "
            "VALUES (?, 'claude_code', 'measured', 99, "
            "'2026-07-29T00:00:00Z', ?, 'task-keep', 'claude_session_id_run')",
            (run_id, prefilled_first),
        )
        conn.execute(
            "INSERT INTO run_llm_calls "
            "(run_id, call_index, origin, input_tokens, occurred_at) "
            "VALUES (?, 0, 'claude_code', 99, NULL)",
            (run_id,),
        )
        other_run = "claude_code:prefilled:req"
        conn.execute(
            "INSERT INTO run_usage_facts "
            "(run_id, origin, source, input_tokens, captured_at, "
            "first_call_at, last_call_at) "
            "VALUES (?, 'claude_code', 'measured', 7, "
            "'2026-07-28T00:00:00Z', ?, ?)",
            (other_run, prefilled_call_time, prefilled_call_time),
        )
        conn.execute(
            "INSERT INTO run_llm_calls "
            "(run_id, call_index, origin, input_tokens, occurred_at) "
            "VALUES (?, 0, 'claude_code', 7, ?)",
            (other_run, prefilled_call_time),
        )
        conn.commit()
        before_other = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id=?",
            (other_run,),
        ).fetchone()
        before_main_non_time = conn.execute(
            "SELECT input_tokens, task_id, correlation_source, captured_at "
            "FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()

    report = harvester_mod.backfill_occurred_at(
        db_path=db_path,
        projects_root=PROJECTS / "flat-project",
        apply=True,
    )

    assert report["applied"] is True
    assert report["calls_filled"] == 1
    assert report["runs_filled"] == 1
    with _connect(db_path) as conn:
        call = conn.execute(
            "SELECT occurred_at, input_tokens FROM run_llm_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT first_call_at, last_call_at, captured_at, input_tokens, "
            "task_id, correlation_source FROM run_usage_facts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        other_call = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id=?",
            (other_run,),
        ).fetchone()
        other_run_row = conn.execute(
            "SELECT first_call_at, last_call_at, captured_at "
            "FROM run_usage_facts WHERE run_id=?",
            (other_run,),
        ).fetchone()

    assert call["occurred_at"] == fixture_timestamp
    assert call["input_tokens"] == 99
    # first_call_at was prefilled — COALESCE must keep it.
    assert run["first_call_at"] == prefilled_first
    assert run["last_call_at"] == fixture_timestamp
    assert run["captured_at"] == before_main_non_time["captured_at"]
    assert run["input_tokens"] == before_main_non_time["input_tokens"]
    assert run["task_id"] == before_main_non_time["task_id"]
    assert run["correlation_source"] == before_main_non_time["correlation_source"]
    assert other_call["occurred_at"] == before_other["occurred_at"]
    assert other_call["input_tokens"] == before_other["input_tokens"]
    assert other_run_row["first_call_at"] == prefilled_call_time
    assert other_run_row["last_call_at"] == prefilled_call_time
    assert other_run_row["captured_at"] == "2026-07-28T00:00:00Z"


def test_timestamp_backfill_cli_preview_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_backfill(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "applied": kwargs["apply"],
            "calls_to_fill": 3,
            "runs_to_fill": 2,
            "calls_filled": 0,
            "runs_filled": 0,
        }

    monkeypatch.setattr(harvester_mod, "backfill_occurred_at", fake_backfill)

    assert harvester_mod.main(["--backfill-timestamps"]) == 0
    assert calls == [
        {
            "db_path": None,
            "projects_root": harvester_mod.DEFAULT_PROJECTS_ROOT,
            "apply": False,
        }
    ]
    assert json.loads(capsys.readouterr().out)["calls_to_fill"] == 3


def _seed_parent_binding_backfill_rows(db_path: Path) -> dict[str, str]:
    run_ids = {
        "binding": make_run_id(
            "msg_parent_binding_fixture", "req_parent_binding_fixture"
        ),
        "session": "claude_code:legacy-parent-session:req",
        "main": "claude_code:legacy-main:req",
        "null_source": "claude_code:legacy-null-source:req",
        "prefilled": "claude_code:prefilled-conflict:req",
        "foreign": "codex_cli:foreign-row",
    }
    call_rows = (
        (
            run_ids["binding"],
            "claude_code",
            "claude-opus-4-8",
            "parent_tool_use:toolu_01ParentBindingFixture",
            101,
        ),
        (
            run_ids["session"],
            "claude_code",
            "claude-sonnet-4-6",
            "parent_session:legacy-parent-session",
            102,
        ),
        (
            run_ids["main"],
            "claude_code",
            "claude-sonnet-4-6",
            "session",
            103,
        ),
        (
            run_ids["null_source"],
            "claude_code",
            "claude-sonnet-4-6",
            None,
            106,
        ),
        (
            run_ids["prefilled"],
            "claude_code",
            "claude-sonnet-4-6",
            "parent_session:encoded-parent",
            104,
        ),
        (
            run_ids["foreign"],
            "codex_cli",
            "gpt-5.6-codex",
            "parent_session:must-not-change",
            105,
        ),
    )
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO run_llm_calls "
            "(run_id, call_index, origin, model, model_source, input_tokens, "
            "occurred_at) VALUES (?, 0, ?, ?, ?, ?, "
            "'2026-07-31T12:00:00Z')",
            call_rows,
        )
        conn.executemany(
            "INSERT INTO run_usage_facts "
            "(run_id, origin, model, model_source, input_tokens, captured_at, "
            "first_call_at, last_call_at, task_id, correlation_source, source) "
            "VALUES (?, ?, ?, ?, ?, '2026-07-31T12:01:00Z', "
            "'2026-07-31T12:00:00Z', '2026-07-31T12:00:00Z', "
            "'task-preserve', 'correlation-preserve', 'measured')",
            call_rows,
        )
        for table in ("run_llm_calls", "run_usage_facts"):
            conn.execute(
                f"UPDATE {table} SET parent_session_id='keep-existing' "
                "WHERE run_id=?",
                (run_ids["prefilled"],),
            )
        conn.commit()
    return run_ids


def _fact_table_snapshot(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    with _connect(db_path) as conn:
        return {
            "run_llm_calls": [
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM run_llm_calls ORDER BY run_id, call_index"
                )
            ],
            "run_usage_facts": [
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM run_usage_facts ORDER BY run_id"
                )
            ],
        }


def test_parent_binding_backfill_preview_is_fully_read_only(
    db_path: Path,
) -> None:
    _seed_parent_binding_backfill_rows(db_path)
    before = _fact_table_snapshot(db_path)

    report = harvester_mod.backfill_parent_bindings(
        db_path=db_path,
        projects_root=PROJECTS / "parent-binding-project",
        apply=False,
    )

    after = _fact_table_snapshot(db_path)
    assert after == before
    assert report["applied"] is False
    for table in ("run_llm_calls", "run_usage_facts"):
        assert report[table]["parent_tool_use_id_to_fill"] == 1
        assert report[table]["parent_agent_id_to_fill"] == 1
        assert report[table]["parent_session_id_to_fill"] == 1
        assert report[table]["spawn_depth_to_fill"] == 1
        assert report[table]["model_source_to_update"] == 4
        assert all(
            value == 0
            for key, value in report[table].items()
            if key.endswith("_filled") or key.endswith("_updated")
        )


def test_parent_binding_backfill_apply_preserves_non_binding_facts_and_is_idempotent(
    db_path: Path,
) -> None:
    run_ids = _seed_parent_binding_backfill_rows(db_path)
    with _connect(db_path) as conn:
        protected_calls = {
            row["run_id"]: tuple(row)
            for row in conn.execute(
                "SELECT run_id, input_tokens, occurred_at FROM run_llm_calls"
            )
        }
        protected_runs = {
            row["run_id"]: tuple(row)
            for row in conn.execute(
                "SELECT run_id, input_tokens, captured_at, first_call_at, "
                "last_call_at, task_id, correlation_source "
                "FROM run_usage_facts"
            )
        }

    report = harvester_mod.backfill_parent_bindings(
        db_path=db_path,
        projects_root=PROJECTS / "parent-binding-project",
        apply=True,
    )

    with _connect(db_path) as conn:
        calls = {
            row["run_id"]: row
            for row in conn.execute("SELECT * FROM run_llm_calls")
        }
        runs = {
            row["run_id"]: row
            for row in conn.execute("SELECT * FROM run_usage_facts")
        }
        after_protected_calls = {
            row["run_id"]: tuple(row)
            for row in conn.execute(
                "SELECT run_id, input_tokens, occurred_at FROM run_llm_calls"
            )
        }
        after_protected_runs = {
            row["run_id"]: tuple(row)
            for row in conn.execute(
                "SELECT run_id, input_tokens, captured_at, first_call_at, "
                "last_call_at, task_id, correlation_source "
                "FROM run_usage_facts"
            )
        }

    expected_binding = (
        "toolu_01ParentBindingFixture",
        "agent-parent-observed",
        None,
        2,
        "transcript",
    )
    columns = (
        "parent_tool_use_id",
        "parent_agent_id",
        "parent_session_id",
        "spawn_depth",
        "model_source",
    )
    assert tuple(calls[run_ids["binding"]][column] for column in columns) == (
        expected_binding
    )
    assert tuple(runs[run_ids["binding"]][column] for column in columns) == (
        expected_binding
    )
    for table_rows in (calls, runs):
        assert table_rows[run_ids["session"]]["parent_session_id"] == (
            "legacy-parent-session"
        )
        assert table_rows[run_ids["session"]]["model_source"] == "transcript"
        assert table_rows[run_ids["main"]]["model_source"] == "transcript"
        assert table_rows[run_ids["null_source"]]["model_source"] == "transcript"
        assert table_rows[run_ids["prefilled"]]["parent_session_id"] == (
            "keep-existing"
        )
        assert table_rows[run_ids["prefilled"]]["model_source"] == (
            "parent_session:encoded-parent"
        )
        assert table_rows[run_ids["foreign"]]["model_source"] == (
            "parent_session:must-not-change"
        )
        assert table_rows[run_ids["foreign"]]["parent_session_id"] is None

    assert after_protected_calls == protected_calls
    assert after_protected_runs == protected_runs
    assert report["applied"] is True
    for table in ("run_llm_calls", "run_usage_facts"):
        assert report[table]["parent_tool_use_id_filled"] == 1
        assert report[table]["parent_agent_id_filled"] == 1
        assert report[table]["parent_session_id_filled"] == 1
        assert report[table]["spawn_depth_filled"] == 1
        assert report[table]["model_source_updated"] == 4

    second = harvester_mod.backfill_parent_bindings(
        db_path=db_path,
        projects_root=PROJECTS / "parent-binding-project",
        apply=True,
    )
    for table in ("run_llm_calls", "run_usage_facts", "totals"):
        assert all(
            value == 0
            for key, value in second[table].items()
            if key.endswith("_filled") or key.endswith("_updated")
        )


def test_parent_binding_backfill_cli_previews_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_backfill(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"applied": kwargs["apply"], "totals": {"spawn_depth_filled": 0}}

    monkeypatch.setattr(harvester_mod, "backfill_parent_bindings", fake_backfill)

    assert harvester_mod.main(["--backfill-parent-bindings"]) == 0
    assert calls == [
        {
            "db_path": None,
            "projects_root": harvester_mod.DEFAULT_PROJECTS_ROOT,
            "apply": False,
        }
    ]
    assert json.loads(capsys.readouterr().out)["applied"] is False


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
        calls = conn.execute(
            "SELECT parent_tool_use_id, parent_agent_id, parent_session_id, "
            "spawn_depth, model_source FROM run_llm_calls "
            "WHERE origin='claude_code'"
        ).fetchall()
        runs = conn.execute(
            "SELECT parent_tool_use_id, parent_agent_id, parent_session_id, "
            "spawn_depth, call_kind, model_source FROM run_usage_facts "
            "WHERE origin='claude_code'"
        ).fetchall()
    assert calls
    assert runs
    assert all(tuple(row)[:4] == (None, None, None, None) for row in calls)
    assert all(tuple(row)[:4] == (None, None, None, None) for row in runs)
    assert all(row["call_kind"] == "main" for row in runs)
    assert all(row["model_source"] == "transcript" for row in calls)
    assert all(row["model_source"] == "transcript" for row in runs)


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
    # The one documented exception: parent_session_id is observed from the
    # enclosing session path even though this sparse meta file lacks binding.
    expected_binding = (
        None,
        None,
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        None,
    )
    assert tuple(rows[0][column] for column in (
        "parent_tool_use_id",
        "parent_agent_id",
        "parent_session_id",
        "spawn_depth",
    )) == expected_binding
    assert tuple(runs[0][column] for column in (
        "parent_tool_use_id",
        "parent_agent_id",
        "parent_session_id",
        "spawn_depth",
    )) == expected_binding
    assert rows[0]["model_source"] == "transcript"
    assert runs[0]["model_source"] == "transcript"


def test_real_subagent_fixture_writes_each_parent_binding_observation(
    db_path: Path,
    tmp_path: Path,
) -> None:
    harvest(
        projects_root=PROJECTS / "parent-binding-project",
        db_path=db_path,
        state_path=tmp_path / "parent-binding-hwm.json",
    )
    columns = (
        "parent_tool_use_id",
        "parent_agent_id",
        "parent_session_id",
        "spawn_depth",
        "model_source",
    )
    with _connect(db_path) as conn:
        call = conn.execute(
            f"SELECT {', '.join(columns)} FROM run_llm_calls "
            "WHERE response_id='msg_parent_binding_fixture'"
        ).fetchone()
        run = conn.execute(
            f"SELECT {', '.join(columns)}, call_kind FROM run_usage_facts "
            "WHERE run_id LIKE 'claude_code:msg_parent_binding_fixture:%'"
        ).fetchone()

    expected = (
        "toolu_01ParentBindingFixture",
        "agent-parent-observed",
        "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        2,
        "transcript",
    )
    assert tuple(call[column] for column in columns) == expected
    assert tuple(run[column] for column in columns) == expected
    assert run["call_kind"] == "subagent"


def test_missing_meta_keeps_only_path_observed_parent_session(
    db_path: Path,
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    shutil.copytree(PROJECTS / "parent-binding-project", projects_root)
    meta_path = next(projects_root.rglob("agent-parent-binding.meta.json"))
    meta_path.unlink()

    harvest(
        projects_root=projects_root,
        db_path=db_path,
        state_path=tmp_path / "missing-meta-hwm.json",
    )
    with _connect(db_path) as conn:
        call = conn.execute(
            "SELECT parent_tool_use_id, parent_agent_id, parent_session_id, "
            "spawn_depth FROM run_llm_calls "
            "WHERE response_id='msg_parent_binding_fixture'"
        ).fetchone()
        run = conn.execute(
            "SELECT parent_tool_use_id, parent_agent_id, parent_session_id, "
            "spawn_depth FROM run_usage_facts "
            "WHERE run_id LIKE 'claude_code:msg_parent_binding_fixture:%'"
        ).fetchone()

    expected = (None, None, "bbbbbbbb-cccc-dddd-eeee-ffffffffffff", None)
    assert tuple(call) == expected
    assert tuple(run) == expected


def test_meta_without_spawn_depth_keeps_spawn_null_and_other_bindings(
    db_path: Path,
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    shutil.copytree(PROJECTS / "parent-binding-project", projects_root)
    meta_path = next(projects_root.rglob("agent-parent-binding.meta.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("spawnDepth")
    meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

    harvest(
        projects_root=projects_root,
        db_path=db_path,
        state_path=tmp_path / "missing-spawn-hwm.json",
    )
    with _connect(db_path) as conn:
        call = conn.execute(
            "SELECT parent_tool_use_id, parent_agent_id, parent_session_id, "
            "spawn_depth FROM run_llm_calls "
            "WHERE response_id='msg_parent_binding_fixture'"
        ).fetchone()
        run = conn.execute(
            "SELECT parent_tool_use_id, parent_agent_id, parent_session_id, "
            "spawn_depth FROM run_usage_facts "
            "WHERE run_id LIKE 'claude_code:msg_parent_binding_fixture:%'"
        ).fetchone()

    expected = (
        "toolu_01ParentBindingFixture",
        "agent-parent-observed",
        "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        None,
    )
    assert tuple(call) == expected
    assert tuple(run) == expected


def test_subagent_uses_exact_parent_session_correlation(
    db_path: Path,
    tmp_path: Path,
) -> None:
    parent_session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    source_root = PROJECTS / "session-dir-project"
    projects_root = tmp_path / "projects"
    session_root = projects_root / parent_session_id
    shutil.copytree(source_root / parent_session_id, session_root)

    transcript = session_root / "subagents" / "agent-fixture0001.jsonl"
    payload = json.loads(transcript.read_text(encoding="utf-8"))
    payload["sessionId"] = "child-session-not-recorded-by-kanban"
    transcript.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    kanban = tmp_path / "kanban.db"
    with sqlite3.connect(kanban) as conn:
        conn.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, metadata TEXT)"
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
            (
                77,
                "task-parent-exact",
                "coder",
                json.dumps(
                    {
                        "claude_session_id": parent_session_id,
                        "chain_id": "chain-parent-exact",
                    }
                ),
            ),
        )

    stats = harvest(
        projects_root=projects_root,
        db_path=db_path,
        state_path=tmp_path / "parent-correlation-hwm.json",
        kanban_paths=[kanban],
    )

    with _connect(db_path) as conn:
        run = conn.execute(
            "SELECT task_run_id, task_id, chain_id, session_id, "
            "correlation_source FROM run_usage_facts "
            "WHERE call_kind='subagent'",
        ).fetchone()
    assert tuple(run) == (
        "77",
        "task-parent-exact",
        "chain-parent-exact",
        "child-session-not-recorded-by-kanban",
        "claude_parent_session_id_run",
    )
    assert stats.sessions_correlated == 1
    assert stats.calls_correlated == 1


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

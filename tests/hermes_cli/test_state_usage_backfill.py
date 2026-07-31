"""Tests for exact July reconstruction from session_model_usage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_cli.state_usage_backfill import backfill_state_usage
from hermes_cli.usage_facts_db import initialize_usage_facts_db, upsert_run_facts

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "usage_state_backfill"
        / "real_rows.json"
    ).read_text(encoding="utf-8")
)["state_usage"]


def _create_kanban(path: Path, *, cost_only: bool = False) -> None:
    task_run = FIXTURE["task_run"]
    metadata = dict(task_run["metadata"])
    if cost_only:
        session_id = metadata.pop("worker_session_id")
        metadata["cost_session_ids"] = [f"{path.parent / 'state.db'}::{session_id}"]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, "
            "started_at REAL, input_tokens INTEGER, output_tokens INTEGER, "
            "metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_run["id"],
                task_run["task_id"],
                task_run["profile"],
                1783191729,
                task_run["input_tokens"],
                task_run["output_tokens"],
                json.dumps(metadata),
            ),
        )
        # The real task has sibling runs. Preserve that aggregate shape so the
        # task-level double-count guard compares like with like.
        connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                6000,
                task_run["task_id"],
                task_run["profile"],
                1780000000,
                10000,
                0,
                "{}",
            ),
        )


def _create_state(path: Path) -> None:
    row = FIXTURE["session_model_usage"]
    columns = list(row)
    declarations = {
        key: "REAL" if key in {"estimated_cost_usd", "actual_cost_usd", "first_seen", "last_seen"} else "INTEGER"
        if key in {
            "api_call_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        }
        else "TEXT"
        for key in columns
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE session_model_usage ("
            + ", ".join(f"{key} {declarations[key]}" for key in columns)
            + ")"
        )
        connection.execute(
            f"INSERT INTO session_model_usage ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[key] for key in columns],
        )


def test_real_state_row_round_trips_all_six_token_fields(tmp_path: Path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    state = tmp_path / "state.db"
    initialize_usage_facts_db(usage)
    _create_kanban(kanban)
    _create_state(state)

    preview = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=False,
    )
    applied = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=True,
    )

    with sqlite3.connect(usage) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM run_usage_facts "
            "WHERE run_id='hermes_state_backfill:6193'"
        ).fetchone()
    source = FIXTURE["session_model_usage"]
    assert preview["reconstructable_runs"] == 1
    assert preview["plausibility_violation_count"] == 0
    assert applied["rows_written"] == 1
    assert row["task_run_id"] == "6193"
    assert row["origin"] == "hermes_state_backfill"
    assert row["correlation_source"] == "session_model_usage_run"
    for column in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    ):
        assert row[column] == source[column]
    assert row["total_tokens"] == FIXTURE["expected_total_tokens"]
    assert row["cost_status"] == source["cost_status"]
    assert row["cost_source"] == source["cost_source"]

    second = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=True,
    )
    assert second["candidate_runs"] == 0
    assert second["rows_written"] == 0


def test_cost_session_receipt_does_not_seed_run_identity(tmp_path: Path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    state = tmp_path / "state.db"
    initialize_usage_facts_db(usage)
    _create_kanban(kanban, cost_only=True)
    _create_state(state)

    report = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=True,
    )

    assert report["runs_with_session_reference"] == 1
    assert report["reconstructable_runs"] == 0
    assert report["unreconstructable_with_session_reference"] == 1
    assert report["rows_written"] == 0


def test_archive_is_used_only_when_active_state_has_no_session(tmp_path: Path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    active = tmp_path / "active.db"
    archive = tmp_path / "archive.db"
    initialize_usage_facts_db(usage)
    _create_kanban(kanban)
    with sqlite3.connect(active) as connection:
        connection.execute("CREATE TABLE session_model_usage (session_id TEXT)")
    _create_state(archive)

    report = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[active],
        archive_state_paths=[archive],
        apply=False,
    )

    assert report["reconstructable_runs"] == 1
    assert report["active_reconstructed_runs"] == 0
    assert report["archive_reconstructed_runs"] == 1


def test_existing_numeric_run_fact_blocks_backfill(tmp_path: Path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    state = tmp_path / "state.db"
    initialize_usage_facts_db(usage)
    _create_kanban(kanban)
    _create_state(state)
    upsert_run_facts(
        str(FIXTURE["task_run"]["id"]),
        {"origin": "hermes_agent", "source": "measured"},
        path=usage,
    )

    report = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=True,
    )

    assert report["candidate_runs"] == 0
    assert report["rows_written"] == 0


def test_session_reused_by_non_candidate_run_is_ambiguous(tmp_path: Path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    state = tmp_path / "state.db"
    initialize_usage_facts_db(usage)
    _create_kanban(kanban)
    _create_state(state)
    task_run = FIXTURE["task_run"]
    with sqlite3.connect(kanban) as connection:
        connection.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                6001,
                task_run["task_id"],
                task_run["profile"],
                1780000000,
                0,
                0,
                json.dumps(task_run["metadata"]),
            ),
        )

    report = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=True,
    )

    assert report["ambiguous_runs"] == 1
    assert report["reconstructable_runs"] == 0
    assert report["rows_written"] == 0


def test_plausibility_guard_prevents_task_overcount(tmp_path: Path) -> None:
    usage = tmp_path / "usage.db"
    kanban = tmp_path / "kanban.db"
    state = tmp_path / "state.db"
    initialize_usage_facts_db(usage)
    _create_kanban(kanban)
    _create_state(state)
    task_id = FIXTURE["task_run"]["task_id"]
    upsert_run_facts(
        "existing-task-usage",
        {
            "origin": "hermes_agent",
            "task_id": task_id,
            "input_tokens": 20000,
            "output_tokens": 0,
            "source": "measured",
        },
        path=usage,
    )

    report = backfill_state_usage(
        db_path=usage,
        kanban_path=kanban,
        hermes_home=tmp_path,
        active_state_paths=[state],
        archive_state_paths=[],
        apply=True,
    )

    assert report["plausibility_violation_count"] == 1
    assert report["rows_written"] == 0

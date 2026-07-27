from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import hermes_cli.kanban_db as kb
from hermes_cli.kanban import run_slash
from hermes_cli.kanban_score_hygiene import SYNTHETIC_RETRY_HEAVY_TASK_IDS
from hermes_cli.langfuse_scores_export import _score_payload


def _state_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('session-1', 'test-model', 100, 40, 25, 10)"
    )
    conn.commit()
    conn.close()


def test_backfill_metrics_uses_fact_layer_without_score_materialization(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_USAGE_FACTS_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    _state_db(home / "state.db")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="cli materialize",
            assignee="coder",
            session_id="session-1",
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', created_at = 700 WHERE id = ?",
            (task_id,),
        )
        run_id = int(
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, status, started_at, ended_at, outcome, cost_usd, metadata) "
                "VALUES (?, 'done', 1000, 1100, 'completed', 1.0, ?)",
                (task_id, json.dumps({"cost_usd_equivalent": 2.0})),
            ).lastrowid
        )
        conn.executemany(
            "INSERT INTO task_events "
            "(task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, '{}', ?)",
            [
                (task_id, run_id, "freigabe_vetoed", 1010),
                (task_id, run_id, "submitted_for_review", 1020),
                (task_id, run_id, "review_approved", 1030),
            ],
        )
        for synthetic_id in SYNTHETIC_RETRY_HEAVY_TASK_IDS:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) "
                "VALUES (?, 'retry-heavy fixture', 'done', 700)",
                (synthetic_id,),
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, status, started_at, ended_at) "
                "VALUES (?, 'done', 1000, 1100)",
                (synthetic_id,),
            )

    first = run_slash("backfill-metrics")
    with kb.connect_closing() as conn:
        names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT DISTINCT name FROM scores WHERE task_id = ?", (task_id,)
            )
        }
        synthetic_attempts = int(
            conn.execute(
                "SELECT COUNT(*) FROM scores "
                "WHERE task_id IN (?, ?) AND name = 'run_attempt_index'",
                SYNTHETIC_RETRY_HEAVY_TASK_IDS,
            ).fetchone()[0]
        )
        materialized_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM scores "
                "WHERE source = 'score_materialization'"
            ).fetchone()[0]
        )
    second = run_slash("backfill-metrics")

    assert "Backfilled run-metric scores for " in first
    assert "usage facts remain authoritative" in first.lower()
    assert names == {
        "run_attempt_index",
        "run_cost_usd",
        "run_duration_seconds",
    }
    assert synthetic_attempts == len(SYNTHETIC_RETRY_HEAVY_TASK_IDS)
    assert materialized_rows == 0
    assert second == (
        "Backfilled run-metric scores for 0 row(s).\n"
        "Agent usage facts remain authoritative in "
        "/mnt/data/hermes-observability/usage_facts.db; "
        "no analytics scores were materialized."
    )


def test_task_outcome_payload_is_categorical():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 1 AS id, 'task_outcome' AS name, 'done' AS value, NULL AS outcome"
    ).fetchone()
    assert _score_payload(row, "trace-1") == {
        "id": "hermes-board-score-1",
        "traceId": "trace-1",
        "name": "task_outcome",
        "value": "done",
        "dataType": "CATEGORICAL",
    }

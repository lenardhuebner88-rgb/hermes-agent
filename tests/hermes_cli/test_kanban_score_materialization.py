from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import hermes_cli.kanban_db as kb
from hermes_cli.langfuse_scores_export import _score_payload
from hermes_cli.kanban import run_slash
from hermes_cli.kanban_score_materialization import materialize_scores
from hermes_cli.kanban_score_hygiene import SYNTHETIC_RETRY_HEAVY_TASK_IDS


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


def test_materialize_scores_derives_events_outcomes_latency_and_usage(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    state_db = tmp_path / "state.db"
    _state_db(state_db)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="materialize", assignee="coder", session_id="session-1")
        conn.execute("UPDATE tasks SET status = 'done', created_at = 700 WHERE id = ?", (task_id,))
        run_id = int(conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
            "VALUES (?, 'done', 1000, 1100, 'completed')",
            (task_id,),
        ).lastrowid)
        conn.executemany(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, '{}', ?)",
            [
                (task_id, run_id, "freigabe_vetoed", 1010),
                (task_id, run_id, "submitted_for_review", 1020),
                (task_id, run_id, "submitted_for_review", 1030),
                (task_id, run_id, "review_approved", 1040),
            ],
        )

        first = materialize_scores(conn, state_db_paths=[state_db], created_at=1200)
        assert first["inserted"] == 9
        assert first["queue_latency_excluded"] == 0
        assert first["reasoning_tokens_subset_of_output"] is True

        rows = conn.execute(
            "SELECT name, value, value_type FROM scores WHERE task_id = ? ORDER BY name", (task_id,)
        ).fetchall()
        actual = {row["name"]: (row["value"], row["value_type"]) for row in rows}
        assert actual == {
            "cache_hit_ratio": (0.2, "numeric"),
            "cache_read_tokens": (25.0, "numeric"),
            "operator_veto": (1.0, "binary"),
            "queue_latency_seconds": (300.0, "numeric"),
            "reasoning_tokens": (10.0, "numeric"),
            "review_submissions_to_approval": (2.0, "numeric"),
            "run_outcome_kind": ("completed", "categorical"),
            "task_outcome": ("done", "categorical"),
            "task_runs_to_done": (1.0, "numeric"),
        }
        # cache_read_tokens, cache_hit_ratio and reasoning_tokens are run-level;
        # the task-level output is attached to the same final run trace.
        assert len(rows) == 9

        second = materialize_scores(conn, state_db_paths=[state_db], created_at=1201)
        assert second["inserted"] == 0
        assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 9


def test_materialize_scores_excludes_scheduled_and_waiting_queue_latency(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        due = kb.create_task(conn, title="due", assignee="coder")
        waiting = kb.create_task(conn, title="waiting", assignee="coder")
        conn.execute("UPDATE tasks SET due_at = 900, created_at = 700 WHERE id = ?", (due,))
        conn.execute("UPDATE tasks SET wait_for = '{}', created_at = 700 WHERE id = ?", (waiting,))
        for task_id in (due, waiting):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 1000)",
                (task_id,),
            )

        report = materialize_scores(conn, state_db_paths=[], created_at=1200)
        assert report["queue_latency_excluded"] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM scores WHERE name = 'queue_latency_seconds'"
        ).fetchone()[0] == 0


def test_materialize_scores_uses_real_metadata_json_for_effective_cost(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        task_ids = {
            title: kb.create_task(conn, title=title, assignee="coder")
            for title in (
                "actual-and-equivalent",
                "equivalent-only",
                "blind",
                "invalid-equivalent",
            )
        }
        rows = [
            (
                task_ids["actual-and-equivalent"],
                1.25,
                json.dumps({"cost_usd_equivalent": "2.75"}),
            ),
            (
                task_ids["equivalent-only"],
                None,
                json.dumps({"cost_usd_equivalent": 3.5}),
            ),
            (task_ids["blind"], 0.0, json.dumps({"unrelated": True})),
            (
                task_ids["invalid-equivalent"],
                None,
                json.dumps({"cost_usd_equivalent": "not-a-number"}),
            ),
        ]
        conn.executemany(
            "INSERT INTO task_runs "
            "(task_id, status, started_at, ended_at, cost_usd, metadata) "
            "VALUES (?, 'done', 1000, 1100, ?, ?)",
            rows,
        )

        first = materialize_scores(conn, state_db_paths=[], created_at=1200)
        effective = conn.execute(
            "SELECT task_id, value FROM scores "
            "WHERE name = 'run_cost_effective_usd' ORDER BY task_id"
        ).fetchall()
        second = materialize_scores(conn, state_db_paths=[], created_at=1201)

    assert first["inserted"] > 0
    assert {row["task_id"]: row["value"] for row in effective} == {
        task_ids["actual-and-equivalent"]: 4.0,
        task_ids["equivalent-only"]: 3.5,
    }
    assert second["inserted"] == 0


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
    state_db = home / "state.db"
    _state_db(state_db)

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
    row = conn.execute("SELECT 1 AS id, 'task_outcome' AS name, 'done' AS value, NULL AS outcome").fetchone()
    assert _score_payload(row, "trace-1") == {
        "id": "hermes-board-score-1",
        "traceId": "trace-1",
        "name": "task_outcome",
        "value": "done",
        "dataType": "CATEGORICAL",
    }

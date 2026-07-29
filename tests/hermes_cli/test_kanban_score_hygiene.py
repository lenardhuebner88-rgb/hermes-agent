from __future__ import annotations

import sqlite3
from pathlib import Path

import hermes_cli.kanban_db as kb
from hermes_cli.kanban_score_hygiene import (
    RUN_CLASS_FIXTURE,
    RUN_CLASS_NEVER_RAN,
    RUN_CLASS_PRODUCTIVE,
    RUN_CLASSES,
    SYNTHETIC_RETRY_HEAVY_TASK_IDS,
    backfill_run_metric_scores_with_classification,
    classify_metric_run,
    is_metric_test_fixture,
    metric_run_class_counts,
    metric_run_classification_relation,
    metric_scores_relation,
)


def _init_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()


def test_metric_run_classification_is_ordered_and_profile_null_is_neutral():
    assert (
        classify_metric_run("t_bbb65f0e", profile="coder")
        == RUN_CLASS_FIXTURE
    )
    assert (
        classify_metric_run("any-task", profile="w")
        == RUN_CLASS_FIXTURE
    )
    assert (
        classify_metric_run(
            "never-ran",
            profile="coder",
            started_at=2,
            ended_at=2,
        )
        == RUN_CLASS_NEVER_RAN
    )
    assert (
        classify_metric_run(
            "profileless-real-run",
            profile=None,
            input_tokens=10,
            output_tokens=2,
            started_at=1,
            ended_at=2,
        )
        == RUN_CLASS_PRODUCTIVE
    )
    assert (
        classify_metric_run(
            "title-only",
            profile="coder",
            active_model="test-model",
            started_at=1,
            ended_at=2,
        )
        == RUN_CLASS_PRODUCTIVE
    )


def test_metric_run_class_counts_accepts_a_naked_sqlite_connection():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                profile TEXT,
                active_model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                started_at REAL,
                ended_at REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO task_runs "
            "(task_id, profile, active_model, input_tokens, output_tokens, "
            " cost_usd, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ("real", None, "model", 10, 2, 0.1, 1, 2),
                ("never", "coder", None, None, None, None, 2, 2),
            ),
        )

        assert metric_run_class_counts(conn) == {
            RUN_CLASS_PRODUCTIVE: 1,
            RUN_CLASS_FIXTURE: 0,
            RUN_CLASS_NEVER_RAN: 1,
        }
    finally:
        conn.close()


def test_backfill_measures_fixture_and_productive_runs_without_deleting_scores(
    tmp_path,
    monkeypatch,
):
    _init_board(tmp_path, monkeypatch)

    with kb.connect_closing() as conn:
        task_ids = (
            *SYNTHETIC_RETRY_HEAVY_TASK_IDS,
            "profile-fixture",
            "real-task",
        )
        for task_id in task_ids:
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at) "
                "VALUES (?, ?, ?, 'done', 900)",
                (
                    task_id,
                    task_id,
                    "w" if task_id == "profile-fixture" else "coder",
                ),
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, "
                " input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, 'done', 1000, 1100, 10, 5, 1.25)",
                (
                    task_id,
                    "w" if task_id == "profile-fixture" else "coder",
                ),
            )

        first = backfill_run_metric_scores_with_classification(conn)
        second = backfill_run_metric_scores_with_classification(conn)
        by_task = {
            str(row["task_id"]): int(row["count"])
            for row in conn.execute(
                "SELECT task_id, COUNT(*) AS count FROM scores "
                "GROUP BY task_id"
            )
        }

    assert first == 16
    assert second == 0
    assert by_task == {task_id: 4 for task_id in task_ids}


def test_run_classes_are_exhaustive_and_special_classes_have_no_telemetry(
    tmp_path,
    monkeypatch,
):
    _init_board(tmp_path, monkeypatch)

    with kb.connect_closing() as conn:
        task_rows = (
            ("t_bbb65f0e", "fixture-id", "coder"),
            ("fixture-profile", "fixture-profile", "w"),
            ("never-ran", "never-ran", "coder"),
            ("profileless-real", "profileless-real", None),
        )
        conn.executemany(
            "INSERT INTO tasks (id, title, assignee, status, created_at) "
            "VALUES (?, ?, ?, 'done', 1)",
            task_rows,
        )
        run_ids = {}
        run_ids["fixture-id"] = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at) "
            "VALUES ('t_bbb65f0e', 'coder', 'done', 1, 1)"
        ).lastrowid
        run_ids["fixture-profile"] = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at) "
            "VALUES ('fixture-profile', 'w', 'reclaimed', 1, 1)"
        ).lastrowid
        run_ids["never-ran"] = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at) "
            "VALUES ('never-ran', 'coder', 'done', 2, 2)"
        ).lastrowid
        run_ids["profileless-real"] = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, "
            " input_tokens, output_tokens, cost_usd) "
            "VALUES ('profileless-real', NULL, 'done', 1, 2, 10, 5, 0.25)"
        ).lastrowid

        relation = metric_run_classification_relation(conn)
        classes = {
            int(row["id"]): str(row["run_class"])
            for row in conn.execute(
                f"SELECT id, run_class FROM {relation}"
            )
        }
        counts = metric_run_class_counts(conn)
        telemetry = {
            str(row["run_class"]): (
                int(row["models"]),
                int(row["tokens"]),
                float(row["cost"]),
            )
            for row in conn.execute(
                "SELECT run_class, COUNT(active_model) AS models, "
                "COALESCE(SUM(COALESCE(input_tokens, 0) + "
                "COALESCE(output_tokens, 0)), 0) AS tokens, "
                "COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost "
                f"FROM {relation} GROUP BY run_class"
            )
        }

    assert classes == {
        int(run_ids["fixture-id"]): RUN_CLASS_FIXTURE,
        int(run_ids["fixture-profile"]): RUN_CLASS_FIXTURE,
        int(run_ids["never-ran"]): RUN_CLASS_NEVER_RAN,
        int(run_ids["profileless-real"]): RUN_CLASS_PRODUCTIVE,
    }
    assert counts == {
        RUN_CLASS_PRODUCTIVE: 1,
        RUN_CLASS_FIXTURE: 2,
        RUN_CLASS_NEVER_RAN: 1,
    }
    assert set(counts) == set(RUN_CLASSES)
    assert sum(counts.values()) == 4
    assert telemetry[RUN_CLASS_FIXTURE] == (0, 0, 0.0)
    assert telemetry[RUN_CLASS_NEVER_RAN] == (0, 0, 0.0)


def test_metric_scores_relation_keeps_all_rows_and_exposes_class_counts(
    tmp_path,
    monkeypatch,
):
    _init_board(tmp_path, monkeypatch)

    with kb.connect_closing() as conn:
        for task_id, profile in (
            ("real-task", "coder"),
            ("fixture-task", "w"),
        ):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at) "
                "VALUES (?, ?, ?, 'done', 1)",
                (task_id, task_id, profile),
            )
        real_run = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, active_model) "
            "VALUES ('real-task', 'coder', 'done', 1, 2, 'model')"
        ).lastrowid
        fixture_run = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at) "
            "VALUES ('fixture-task', 'w', 'done', 1, 2)"
        ).lastrowid
        conn.executemany(
            "INSERT INTO scores "
            "(run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?, ?, 'run_duration_seconds', ?, "
            "        'numeric', 'test', 2)",
            [
                (real_run, "real-task", 222.0),
                (fixture_run, "fixture-task", 0.0),
            ],
        )

        relation = metric_scores_relation(conn)
        aggregate = conn.execute(
            f"SELECT COUNT(*) AS count, AVG(CAST(value AS REAL)) AS average "
            f"FROM {relation} WHERE name = 'run_duration_seconds'"
        ).fetchone()
        by_class = {
            str(row["run_class"]): int(row["count"])
            for row in conn.execute(
                f"SELECT run_class, COUNT(*) AS count FROM {relation} "
                "GROUP BY run_class"
            )
        }
        persisted = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]

    assert tuple(aggregate) == (2, 111.0)
    assert by_class == {
        RUN_CLASS_PRODUCTIVE: 1,
        RUN_CLASS_FIXTURE: 1,
    }
    assert sum(by_class.values()) == persisted == 2


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_classify_never_ran_when_started_at_none_but_ended_at_present():
    """Kill comparison_swap L60: is->is not would skip the None check and
    hit TypeError on float(None), caught as False -> PRODUCTIVE."""
    result = classify_metric_run(
        "task-x",
        active_model=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        started_at=None,
        ended_at=100.0,
    )
    assert result == RUN_CLASS_NEVER_RAN


def test_classify_productive_when_timestamps_not_floatable():
    """Kill boolean_flip L65: False->True would make non-floatable timestamps
    count as zero_or_missing_duration -> NEVER_RAN instead of PRODUCTIVE."""
    result = classify_metric_run(
        "task-y",
        active_model=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        started_at="not-a-number",
        ended_at="also-not",
    )
    assert result == RUN_CLASS_PRODUCTIVE


def test_is_metric_test_fixture_false_for_normal_task():
    """Kill comparison_swap L79: ==->!= would invert the fixture check."""
    assert is_metric_test_fixture("normal-task-123") is False

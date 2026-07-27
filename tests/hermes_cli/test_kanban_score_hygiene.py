from __future__ import annotations

from pathlib import Path

import hermes_cli.kanban_db as kb
from hermes_cli.kanban_score_hygiene import (
    SYNTHETIC_RETRY_HEAVY_TASK_IDS,
    backfill_run_metric_scores_without_retry_fixtures,
    is_metric_test_fixture,
    metric_scores_relation,
)


def test_metric_fixture_predicate_is_named_and_specific():
    assert is_metric_test_fixture("t_bbb65f0e", "coder", "production")
    assert is_metric_test_fixture("copied-id", "w", "time-travel")
    assert is_metric_test_fixture("copied-id", "w", "retry-heavy")
    assert not is_metric_test_fixture("real-task", "w", "production")
    assert not is_metric_test_fixture("real-task", "coder", "retry-heavy")


def test_backfill_quarantines_retry_fixtures_and_preserves_existing_scores(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        for task_id in (
            *SYNTHETIC_RETRY_HEAVY_TASK_IDS,
            "copied-time-travel",
            "real-task",
        ):
            title = "time-travel" if task_id == "copied-time-travel" else task_id
            assignee = "w" if task_id == "copied-time-travel" else None
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at) "
                "VALUES (?, ?, ?, 'done', 900)",
                (task_id, title, assignee),
            )
        run_ids: dict[str, int] = {}
        for task_id in (
            *SYNTHETIC_RETRY_HEAVY_TASK_IDS,
            "copied-time-travel",
            "real-task",
        ):
            run_ids[task_id] = int(
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, started_at, ended_at, "
                    " input_tokens, output_tokens, cost_usd) "
                    "VALUES (?, ?, 'done', 1000, 1100, 10, 5, 1.25)",
                    (
                        task_id,
                        "w" if task_id == "copied-time-travel" else "coder",
                    ),
                ).lastrowid
            )
        conn.executemany(
            "INSERT INTO scores "
            "(run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?, ?, 'run_duration_seconds', 100, "
            "        'numeric', 'board-metrics', 1100)",
            [
                (run_ids[task_id], task_id)
                for task_id in SYNTHETIC_RETRY_HEAVY_TASK_IDS
            ],
        )

        first = backfill_run_metric_scores_without_retry_fixtures(conn)
        second = backfill_run_metric_scores_without_retry_fixtures(conn)
        quarantined = conn.execute(
            "SELECT task_id, name FROM scores "
            "WHERE task_id IN (?, ?) ORDER BY task_id, name",
            SYNTHETIC_RETRY_HEAVY_TASK_IDS,
        ).fetchall()
        real_names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM scores WHERE task_id = 'real-task'"
            )
        }
        copied_fixture_scores = int(
            conn.execute(
                "SELECT COUNT(*) FROM scores "
                "WHERE task_id = 'copied-time-travel'"
            ).fetchone()[0]
        )

    assert first == 4
    assert second == 0
    assert [tuple(row) for row in quarantined] == [
        ("t_8ec520d3", "run_duration_seconds"),
        ("t_bbb65f0e", "run_duration_seconds"),
    ]
    assert real_names == {
        "run_attempt_index",
        "run_cost_usd",
        "run_duration_seconds",
        "run_tokens_total",
    }
    assert copied_fixture_scores == 0


def test_metric_scores_relation_excludes_fixtures_without_deleting_them(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        fixture_id = SYNTHETIC_RETRY_HEAVY_TASK_IDS[0]
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at) "
            "VALUES (?, 'retry-heavy', 'w', 'done', 1)",
            (fixture_id,),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at) "
            "VALUES ('real-task', 'production', 'coder', 'done', 1)"
        )
        fixture_run = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at) "
            "VALUES (?, 'w', 'done', 1, 2)",
            (fixture_id,),
        ).lastrowid
        real_run = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at) "
            "VALUES ('real-task', 'coder', 'done', 1, 223)"
        ).lastrowid
        conn.executemany(
            "INSERT INTO scores "
            "(run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?, ?, 'run_duration_seconds', ?, 'numeric', 'test', 2)",
            [
                (fixture_run, fixture_id, 0.0),
                (real_run, "real-task", 222.0),
            ],
        )

        relation = metric_scores_relation(conn)
        filtered = conn.execute(
            f"SELECT COUNT(*), AVG(CAST(value AS REAL)) FROM {relation} "
            "WHERE name = 'run_duration_seconds'"
        ).fetchone()
        persisted = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]

    assert tuple(filtered) == (1, 222.0)
    assert persisted == 2

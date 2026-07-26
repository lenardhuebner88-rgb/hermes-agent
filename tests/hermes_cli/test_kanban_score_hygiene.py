from __future__ import annotations

from pathlib import Path

import hermes_cli.kanban_db as kb
from hermes_cli.kanban_score_hygiene import (
    SYNTHETIC_RETRY_HEAVY_TASK_IDS,
    backfill_run_metric_scores_without_retry_fixtures,
)


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
        for task_id in (*SYNTHETIC_RETRY_HEAVY_TASK_IDS, "real-task"):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) "
                "VALUES (?, ?, 'done', 900)",
                (task_id, task_id),
            )
        run_ids: dict[str, int] = {}
        for task_id in (*SYNTHETIC_RETRY_HEAVY_TASK_IDS, "real-task"):
            run_ids[task_id] = int(
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, status, started_at, ended_at, "
                    " input_tokens, output_tokens, cost_usd) "
                    "VALUES (?, 'done', 1000, 1100, 10, 5, 1.25)",
                    (task_id,),
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

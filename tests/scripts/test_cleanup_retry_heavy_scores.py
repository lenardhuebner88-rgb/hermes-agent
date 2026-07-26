from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.cleanup_retry_heavy_scores import (
    CleanupError,
    cleanup_retry_heavy_scores,
)
from hermes_cli.kanban_score_hygiene import SYNTHETIC_RETRY_HEAVY_TASK_IDS


TASK_IDS = SYNTHETIC_RETRY_HEAVY_TASK_IDS


def _database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE scores ("
        "id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT, name TEXT, "
        "value REAL, value_type TEXT, source TEXT, created_at INTEGER)"
    )
    for task_id in TASK_IDS:
        conn.executemany(
            "INSERT INTO scores "
            "(run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?, ?, ?, ?, 'numeric', 'test', 1)",
            [
                (1, task_id, "run_attempt_index", 1000.0),
                (2, task_id, "run_attempt_index", 1000.0),
                (1, task_id, "run_duration_seconds", 5.0),
            ],
        )
    conn.execute(
        "INSERT INTO scores "
        "(run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (3, 'real-task', 'run_attempt_index', 1.0, 'numeric', 'test', 1)"
    )
    conn.commit()
    conn.close()


def test_cleanup_backs_up_all_scores_and_deletes_only_target_attempts(tmp_path):
    db_path = tmp_path / "kanban.db"
    backup_path = tmp_path / "scores-before.sql"
    _database(db_path)

    report = cleanup_retry_heavy_scores(
        db_path,
        backup_path,
        expected_rows=4,
    )

    assert report["deleted_rows"] == 4
    assert report["before"] == {"rows": 5, "mean": 800.2, "max": 1000.0}
    assert report["after"] == {"rows": 1, "mean": 1.0, "max": 1.0}

    conn = sqlite3.connect(db_path)
    remaining = conn.execute(
        "SELECT task_id, name, value FROM scores ORDER BY task_id, name"
    ).fetchall()
    conn.close()
    assert remaining == [
        ("real-task", "run_attempt_index", 1.0),
        ("t_8ec520d3", "run_duration_seconds", 5.0),
        ("t_bbb65f0e", "run_duration_seconds", 5.0),
    ]

    restored = sqlite3.connect(":memory:")
    restored.executescript(backup_path.read_text(encoding="utf-8"))
    assert restored.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 7
    assert (
        restored.execute(
            "SELECT COUNT(*) FROM scores WHERE task_id IN (?, ?) "
            "AND name = 'run_attempt_index'",
            TASK_IDS,
        ).fetchone()[0]
        == 4
    )
    restored.close()


def test_cleanup_aborts_before_backup_or_delete_on_cardinality_mismatch(tmp_path):
    db_path = tmp_path / "kanban.db"
    backup_path = tmp_path / "scores-before.sql"
    _database(db_path)

    with pytest.raises(CleanupError, match="expected 2000 targeted rows, found 4"):
        cleanup_retry_heavy_scores(db_path, backup_path)

    assert not backup_path.exists()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 7
    conn.close()

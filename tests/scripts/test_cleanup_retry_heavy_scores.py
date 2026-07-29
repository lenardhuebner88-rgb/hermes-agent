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


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------

def test_cli_requires_db_and_backup_flags():
    """--db and --backup are both REQUIRED — argparse must exit with a
    usage error before anything touches the filesystem."""
    from scripts.cleanup_retry_heavy_scores import main

    with pytest.raises(SystemExit):
        main(["--apply", "--backup", "y.sql"])  # --db missing
    with pytest.raises(SystemExit):
        main(["--apply", "--db", "x.db"])  # --backup missing


def test_cli_exit_codes_distinguish_user_error_from_runtime_failure(tmp_path):
    """Missing --apply is a usage error (2); a missing database is a
    runtime failure (1) — scripts key off the difference."""
    from scripts.cleanup_retry_heavy_scores import main

    assert main(["--db", "x", "--backup", "y"]) == 2
    assert main(
        [
            "--apply",
            "--db", str(tmp_path / "missing.db"),
            "--backup", str(tmp_path / "dump.sql"),
        ]
    ) == 1


def test_cli_apply_prints_sorted_json_report_and_returns_zero(tmp_path, capsys):
    """A successful --apply run exits 0 and prints the report as
    sort_keys JSON — deterministic output the cron can diff."""
    import json as _json

    from scripts.cleanup_retry_heavy_scores import main

    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE scores ("
        "id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT, name TEXT, "
        "value REAL, value_type TEXT, source TEXT, created_at INTEGER)"
    )
    conn.executemany(
        "INSERT INTO scores "
        "(run_id, task_id, name, value, value_type, source, created_at) "
        "VALUES (?, ?, 'run_attempt_index', 1000.0, 'numeric', 'test', 1)",
        [(i, TASK_IDS[0]) for i in range(2000)],
    )
    conn.commit()
    conn.close()

    rc = main(
        ["--apply", "--db", str(db), "--backup", str(tmp_path / "dump.sql")]
    )

    assert rc == 0
    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert out.strip() == _json.dumps(payload, sort_keys=True)
    assert payload["deleted_rows"] == 2000

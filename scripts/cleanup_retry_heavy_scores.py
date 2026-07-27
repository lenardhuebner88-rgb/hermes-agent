#!/usr/bin/env python3
"""One-time cleanup for synthetic ``retry-heavy`` attempt-index scores.

The cleanup is intentionally narrower than a generic score deleter:

* exactly two archived task IDs are eligible;
* only their ``run_attempt_index`` rows are removed;
* the expected live cardinality is exactly 2,000 rows;
* a restorable SQL dump of the complete ``scores`` table is fsynced first.

The normal ``backfill-metrics`` path permanently quarantines the same two task
IDs via ``hermes_cli.kanban_score_hygiene`` so this one-time cleanup is not
undone later.

Run from the repository root:

    .venv/bin/python scripts/cleanup_retry_heavy_scores.py \
        --db /home/piet/.hermes/kanban.db \
        --backup .s2-scores-before-cleanup.sql \
        --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from hermes_cli.kanban_score_hygiene import SYNTHETIC_RETRY_HEAVY_TASK_IDS


_TASK_IDS = SYNTHETIC_RETRY_HEAVY_TASK_IDS
_SCORE_NAME = "run_attempt_index"
_EXPECTED_ROWS = 2_000


class CleanupError(RuntimeError):
    """Abort without deleting when a cleanup precondition is not met."""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _write_scores_dump(conn: sqlite3.Connection, path: Path) -> None:
    if path.exists():
        raise CleanupError(f"backup already exists: {path}")
    if not path.parent.is_dir():
        raise CleanupError(f"backup directory does not exist: {path.parent}")

    schema_rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE tbl_name = 'scores' AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
    ).fetchall()
    table_sql = next(
        (str(row["sql"]) for row in schema_rows if row["type"] == "table"),
        None,
    )
    if table_sql is None:
        raise CleanupError("scores table schema is missing")

    columns = [
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(scores)").fetchall()
    ]
    if not columns:
        raise CleanupError("scores table has no columns")
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    quoted_values = ", ".join(
        f"quote({_quote_identifier(column)})" for column in columns
    )

    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write("-- Complete pre-cleanup dump of the scores table.\n")
            handle.write("BEGIN TRANSACTION;\n")
            handle.write(table_sql.rstrip(";") + ";\n")
            for row in conn.execute(
                f"SELECT {quoted_values} FROM scores ORDER BY rowid"
            ):
                values = ", ".join(str(value) for value in row)
                handle.write(
                    f"INSERT INTO scores ({quoted_columns}) VALUES ({values});\n"
                )
            for row in schema_rows:
                if row["type"] != "table":
                    handle.write(str(row["sql"]).rstrip(";") + ";\n")
            handle.write("COMMIT;\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _attempt_stats(conn: sqlite3.Connection) -> dict[str, int | float | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS rows, AVG(CAST(value AS REAL)) AS mean, "
        "MAX(CAST(value AS REAL)) AS maximum "
        "FROM scores WHERE name = ?",
        (_SCORE_NAME,),
    ).fetchone()
    return {
        "rows": int(row["rows"]),
        "mean": float(row["mean"]) if row["mean"] is not None else None,
        "max": float(row["maximum"]) if row["maximum"] is not None else None,
    }


def cleanup_retry_heavy_scores(
    db_path: Path,
    backup_path: Path,
    *,
    expected_rows: int = _EXPECTED_ROWS,
) -> dict[str, Any]:
    """Back up ``scores`` and delete only the two synthetic attempt series."""
    if not db_path.is_file():
        raise CleanupError(f"database does not exist: {db_path}")

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = _attempt_stats(conn)
        target_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM scores "
                "WHERE task_id IN (?, ?) AND name = ?",
                (*_TASK_IDS, _SCORE_NAME),
            ).fetchone()[0]
        )
        if target_rows != expected_rows:
            raise CleanupError(
                f"expected {expected_rows} targeted rows, found {target_rows}; "
                "nothing deleted"
            )

        _write_scores_dump(conn, backup_path)
        deleted = int(
            conn.execute(
                "DELETE FROM scores "
                "WHERE task_id IN (?, ?) AND name = ?",
                (*_TASK_IDS, _SCORE_NAME),
            ).rowcount
        )
        if deleted != target_rows:
            raise CleanupError(
                f"delete cardinality changed from {target_rows} to {deleted}"
            )
        remaining_target_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM scores "
                "WHERE task_id IN (?, ?) AND name = ?",
                (*_TASK_IDS, _SCORE_NAME),
            ).fetchone()[0]
        )
        if remaining_target_rows != 0:
            raise CleanupError(
                f"{remaining_target_rows} targeted rows remain; rolling back"
            )
        after = _attempt_stats(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "database": str(db_path),
        "backup": str(backup_path),
        "task_ids": list(_TASK_IDS),
        "score_name": _SCORE_NAME,
        "deleted_rows": deleted,
        "before": before,
        "after": after,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up scores and remove the two synthetic retry-heavy attempt series."
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required acknowledgement for the scoped DELETE.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.apply:
        print("error: --apply is required; nothing deleted", file=sys.stderr)
        return 2
    try:
        report = cleanup_retry_heavy_scores(
            args.db.expanduser().resolve(),
            args.backup.expanduser().resolve(),
        )
    except (CleanupError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

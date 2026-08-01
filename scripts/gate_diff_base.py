#!/usr/bin/env python3
"""Resolve the default worker-gate diff base without mutating Kanban state."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _candidate_from_first_run() -> str:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return ""

    configured_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    db_path = (
        Path(configured_db).expanduser()
        if configured_db
        else Path.home() / ".hermes" / "kanban.db"
    )
    if not db_path.is_file():
        return ""

    try:
        with sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        ) as connection:
            row = connection.execute(
                """
                SELECT pre_run_commit_sha
                FROM task_runs
                WHERE id = (
                    SELECT MIN(id)
                    FROM task_runs
                    WHERE task_id = ?
                      AND pre_run_commit_sha IS NOT NULL
                      AND TRIM(pre_run_commit_sha) <> ''
                )
                """,
                (task_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    return str(row[0]).strip() if row else ""


def _verified_commit(repo_root: Path, candidate: str) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "--verify",
                f"{candidate}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def resolve(repo_root: Path) -> str:
    explicit = os.environ.get("HERMES_GATE_DIFF_BASE", "").strip()
    candidate = explicit or _candidate_from_first_run()
    if not candidate:
        return ""

    resolved = _verified_commit(repo_root, candidate)
    if resolved:
        return resolved

    source = "HERMES_GATE_DIFF_BASE" if explicit else "first task run"
    print(
        f"gate-diff-base: {source} value {candidate!r} is not a commit in "
        f"{repo_root}; falling back to merge-base HEAD main",
        file=sys.stderr,
    )
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        resolved = resolve(args.repo_root.resolve())
    except Exception:
        # This helper is advisory: every unexpected failure keeps the historical
        # merge-base fallback available to the calling gate.
        return 0
    if resolved:
        print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

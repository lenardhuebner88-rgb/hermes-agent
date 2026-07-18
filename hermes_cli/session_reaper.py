"""Expire abandoned open sessions using source-specific inactivity windows.

The command is dry-run by default. Pass ``--apply`` to create a consistent
SQLite backup and then mark eligible sessions with ``end_reason='expired'``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from hermes_cli.backup import _safe_copy_db
from hermes_constants import get_hermes_home

_CRON_EXPIRY_SECONDS = 6 * 60 * 60
_OTHER_EXPIRY_SECONDS = 48 * 60 * 60
_LAST_ACTIVE_SQL = "COALESCE(MAX(m.timestamp), s.started_at)"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expire inactive open sessions (dry-run by default)."
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=get_hermes_home() / "state.db",
        help="Path to state.db",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a backup, then apply expirations",
    )
    parser.add_argument(
        "--now",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _find_candidates(
    conn: sqlite3.Connection, *, now: float
) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT s.id, s.source, s.title, s.display_name,
               {_LAST_ACTIVE_SQL} AS last_active
        FROM sessions AS s
        LEFT JOIN messages AS m ON m.session_id = s.id
        WHERE s.ended_at IS NULL
        GROUP BY s.id, s.source, s.title, s.display_name, s.started_at
        HAVING {_LAST_ACTIVE_SQL} < CASE
            WHEN s.source = 'cron' THEN ?
            ELSE ?
        END
        ORDER BY last_active ASC, s.id ASC
        """,
        (
            now - _CRON_EXPIRY_SECONDS,
            now - _OTHER_EXPIRY_SECONDS,
        ),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "display_name": row["display_name"],
            "last_active": float(row["last_active"]),
            "age_hours": (now - float(row["last_active"])) / 3600.0,
        }
        for row in rows
    ]


def _backup_path(state_db: Path, *, now: float) -> Path:
    timestamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        state_db.parent
        / "backups"
        / f"{state_db.name}.{timestamp}.before-session-reaper.db"
    )


def _open_read_only(state_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _expire_candidates(state_db: Path, *, now: float) -> list[dict[str, object]]:
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidates = _find_candidates(conn, now=now)
        for candidate in candidates:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = 'expired' "
                "WHERE id = ? AND ended_at IS NULL",
                (candidate["last_active"], candidate["id"]),
            )
        conn.commit()
        return candidates
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _print_candidates(candidates: list[dict[str, object]]) -> None:
    for candidate in candidates:
        label = candidate["title"] or candidate["display_name"] or "(untitled)"
        print(
            f"{candidate['id']} source={candidate['source']} "
            f"age={candidate['age_hours']:.1f}h {label}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    state_db = args.state_db.expanduser().resolve()
    now = time.time() if args.now is None else float(args.now)

    try:
        if not args.apply:
            with _open_read_only(state_db) as conn:
                candidates = _find_candidates(conn, now=now)
            _print_candidates(candidates)
            print(f"expired {len(candidates)} sessions (dry-run)")
            return 0

        backup_path = _backup_path(state_db, now=now)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if not _safe_copy_db(state_db, backup_path):
            print(f"Backup failed: {backup_path}", file=sys.stderr)
            return 1

        candidates = _expire_candidates(state_db, now=now)
        print(f"backup: {backup_path}")
        _print_candidates(candidates)
        print(f"expired {len(candidates)} sessions (applied)")
        return 0
    except (OSError, sqlite3.Error) as exc:
        print(f"Session reaper failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

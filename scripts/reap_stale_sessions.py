#!/usr/bin/env python3
"""Administratively close stale open sessions in state.db.

Default is dry-run: list candidates only. Pass ``--apply`` to write
``ended_at`` / ``end_reason='stale_sweep'`` (never DELETE). A SQLite
backup is taken before any apply; abort if backup fails.

Nightly path: systemd oneshot/timer under scripts/systemd/ (not jobs.json).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo root on sys.path so `import hermes_state` works when invoked as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_constants import get_hermes_home  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

DEFAULT_DAYS = 7
NOTIFY_SCRIPT = Path("/home/piet/orchestration/bin/notify-discord.sh")


def _safe_copy_db(src: Path, dst: Path) -> bool:
    """Consistent snapshot of a (possibly live WAL) SQLite DB.

    Mirrors ``hermes_cli.backup._safe_copy_db``: ``mode=ro`` + ``conn.backup``,
    never ``shutil.copy2`` as the primary path on a live WAL database.
    """
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        backup_conn = sqlite3.connect(str(dst))
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
            conn.close()
        return True
    except Exception as exc:
        print(f"SQLite safe copy failed for {src}: {exc}", file=sys.stderr)
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as exc2:
            print(f"Raw copy also failed for {src}: {exc2}", file=sys.stderr)
            return False


def _default_state_db() -> Path:
    return get_hermes_home() / "state.db"


def _backup_path(hermes_home: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = hermes_home / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    return backups / f"state.db.{stamp}.before-stale-sweep.db"


def _label(cand: dict) -> str:
    title = (cand.get("title") or "").strip()
    if title:
        return title
    display = (cand.get("display_name") or "").strip()
    if display:
        return display
    return "(untitled)"


def _short_id(session_id: str, n: int = 12) -> str:
    if not session_id:
        return "?"
    return session_id if len(session_id) <= n else session_id[:n]


def _notify_discord(n: int, days: int) -> None:
    if not NOTIFY_SCRIPT.is_file():
        print(f"notify script missing: {NOTIFY_SCRIPT}", file=sys.stderr)
        return
    msg = f"🧹 session-reaper · closed {n} stale sessions (>{days}d)"
    try:
        subprocess.run(
            [str(NOTIFY_SCRIPT), msg],
            check=False,
            timeout=30,
        )
    except Exception as exc:
        print(f"notify-discord failed (ignored): {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Close open Hermes sessions with no activity for N days."
    )
    p.add_argument(
        "--days",
        type=float,
        default=DEFAULT_DAYS,
        help=f"Stale threshold in days (default {DEFAULT_DAYS})",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually close candidates (default: dry-run)",
    )
    p.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="Path to state.db (default: ~/.hermes/state.db)",
    )
    p.add_argument(
        "--notify",
        action="store_true",
        help="Discord ping when applied count > 0",
    )
    p.add_argument(
        "--now",
        type=float,
        default=None,
        help=argparse.SUPPRESS,  # test/injectable wall clock
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.days < 0:
        print("--days must be >= 0", file=sys.stderr)
        return 2

    state_db = Path(args.state_db) if args.state_db else _default_state_db()
    if not state_db.is_file():
        print(f"state.db not found: {state_db}", file=sys.stderr)
        return 2

    older_than_seconds = int(args.days * 86400)
    dry_run = not args.apply
    wall = args.now if args.now is not None else time.time()

    if args.apply:
        hermes_home = state_db.parent
        backup_dst = _backup_path(hermes_home)
        if not _safe_copy_db(state_db, backup_dst):
            print("Backup failed — aborting apply (no writes).", file=sys.stderr)
            return 1
        print(f"backup: {backup_dst}")

    db = SessionDB(db_path=state_db)
    try:
        candidates = db.close_stale_sessions(
            older_than_seconds=older_than_seconds,
            now=wall,
            dry_run=dry_run,
        )
    finally:
        db.close()

    for cand in candidates:
        age = cand["age_days"]
        print(
            f"  {_short_id(cand['id'])}  {_label(cand)}  "
            f"{age:.1f}d"
        )

    mode = "dry-run" if dry_run else "applied"
    n = len(candidates)
    print(f"reaped {n} sessions ({mode})")

    if args.notify and args.apply and n > 0:
        try:
            _notify_discord(n, int(args.days) if args.days == int(args.days) else args.days)
        except Exception:
            pass  # || true semantics

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

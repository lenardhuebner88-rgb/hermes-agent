#!/usr/bin/env python3
"""One-shot backfill of sessions.display_name from the first user message.

Default mode is dry-run. Pass ``--apply`` to write. Only fills rows where
both ``display_name`` and ``title`` are NULL and at least one user message
exists. Updates use ``COALESCE(display_name, ?)`` so existing labels are
never overwritten (idempotent / race-safe).

Does NOT touch ``title`` (app-unique). Presentational only.

Examples::

    scripts/backfill_session_labels.py
    scripts/backfill_session_labels.py --apply
    scripts/backfill_session_labels.py --state-db /tmp/state-copy.db --limit 50
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli.backup import _safe_copy_db  # noqa: E402
from hermes_constants import get_hermes_home  # noqa: E402
from hermes_state import derive_session_label  # noqa: E402


Candidate = Tuple[str, Optional[str], Optional[str]]  # id, first_content, label


def _default_state_db() -> Path:
    return get_hermes_home() / "state.db"


def _utc_stamp(now: Optional[datetime] = None) -> str:
    when = now if now is not None else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_path(stamp: str) -> Path:
    return get_hermes_home() / "backups" / f"state.db.{stamp}.before-label-backfill.db"


def backup_state_db(src: Path, *, now: Optional[datetime] = None) -> Path:
    """Snapshot ``src`` via sqlite backup API before any apply writes.

    Destination: ``~/.hermes/backups/state.db.<UTC>.before-label-backfill.db``.
    Raises ``RuntimeError`` if the copy fails (caller must abort apply).
    """
    dst = _backup_path(_utc_stamp(now))
    dst.parent.mkdir(parents=True, exist_ok=True)
    ok = _safe_copy_db(Path(src), dst)
    if not ok:
        raise RuntimeError(f"backup failed: {src} -> {dst}")
    return dst


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def iter_candidates(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[Candidate]:
    """Sessions with neither display_name nor title, that have a user message."""
    sql = """
        SELECT s.id AS id,
               (
                   SELECT m.content
                   FROM messages m
                   WHERE m.session_id = s.id
                     AND m.role = 'user'
                   ORDER BY m.timestamp ASC, m.id ASC
                   LIMIT 1
               ) AS first_content
        FROM sessions s
        WHERE s.display_name IS NULL
          AND s.title IS NULL
          AND EXISTS (
              SELECT 1 FROM messages m
              WHERE m.session_id = s.id AND m.role = 'user'
              LIMIT 1
          )
        ORDER BY s.started_at ASC
    """
    params: Sequence[Any] = ()
    if limit is not None and limit >= 0:
        sql += " LIMIT ?"
        params = (int(limit),)

    rows = conn.execute(sql, params).fetchall()
    out: List[Candidate] = []
    for row in rows:
        content = row["first_content"]
        label = derive_session_label(content)
        out.append((row["id"], content, label))
    return out


def apply_labels(
    conn: sqlite3.Connection,
    candidates: Iterable[Candidate],
) -> int:
    """Write labels with COALESCE guard. Returns number of rows updated."""
    updated = 0
    for session_id, _content, label in candidates:
        if not label:
            continue
        cur = conn.execute(
            """UPDATE sessions
               SET display_name = COALESCE(display_name, ?)
               WHERE id = ?
                 AND display_name IS NULL
                 AND title IS NULL""",
            (label, session_id),
        )
        updated += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return updated


def _format_examples(candidates: Sequence[Candidate], *, max_examples: int = 20) -> List[str]:
    lines: List[str] = []
    for session_id, _content, label in candidates:
        if not label:
            continue
        lines.append(f"  {session_id}: {label}")
        if len(lines) >= max_examples:
            break
    return lines


def run(
    *,
    state_db: Path,
    apply: bool = False,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Execute dry-run or apply. Returns process exit code."""
    state_db = Path(state_db)
    if not state_db.exists():
        print(f"error: state db not found: {state_db}", file=sys.stderr)
        return 2

    # Always read candidates via RO connection first.
    with _open_ro(state_db) as ro:
        candidates = iter_candidates(ro, limit=limit)

    labelable = [(sid, c, lab) for sid, c, lab in candidates if lab]
    mode = "applied" if apply else "dry-run"
    n = len(labelable)

    if apply:
        try:
            backup_path = backup_state_db(state_db, now=now)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        print(f"backup: {backup_path}")

        with _open_rw(state_db) as rw:
            # Re-select under write connection so we only touch still-null rows.
            fresh = iter_candidates(rw, limit=limit)
            updated = apply_labels(rw, fresh)
        # Report intended labelable count; actual updates may be lower if races.
        print(f"labeled {updated} sessions ({mode})")
        examples = _format_examples(fresh)
    else:
        print(f"labeled {n} sessions ({mode})")
        examples = _format_examples(labelable)

    for line in examples:
        print(line)

    skipped_empty = sum(1 for _s, _c, lab in candidates if not lab)
    if skipped_empty:
        print(f"(skipped {skipped_empty} candidates with empty/symbol-only first user message)")

    return 0


def _parse_now(raw: str) -> datetime:
    """Parse ``--now`` for deterministic backup stamps in tests."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Unix epoch seconds
        dt = datetime.fromtimestamp(float(text), tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill sessions.display_name from the first user message "
            "(COALESCE-safe; never touches title)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes (default: dry-run)",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="path to state.db (default: $HERMES_HOME/state.db)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max candidate sessions (default: unlimited)",
    )
    parser.add_argument(
        "--now",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,  # test hook: fixed UTC stamp for backup filename
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    state_db = args.state_db if args.state_db is not None else _default_state_db()
    now: Optional[datetime] = None
    if hasattr(args, "now"):
        now = _parse_now(str(args.now))

    return run(
        state_db=state_db,
        apply=bool(args.apply),
        limit=args.limit,
        now=now,
    )


if __name__ == "__main__":
    raise SystemExit(main())

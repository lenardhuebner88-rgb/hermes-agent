#!/usr/bin/env python3
"""Render blocked Kanban cards from read-only snapshots of every board.

The board CLI omits fields needed by this operator view.  This script therefore
reads each configured board database directly, but only through SQLite's
``mode=ro`` URI and an in-memory backup.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, NamedTuple, Sequence

DEFAULT_REASON_LIMIT = 500
MISSING = "fehlt"


class BoardDatabase(NamedTuple):
    slug: str
    database: Path


class BoardReadError(RuntimeError):
    """A configured board could not be discovered or read."""


def _parse_payload(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _cap_text(value: str, limit: int) -> str:
    """Use the repository's established visible truncation convention."""
    if len(value) <= limit:
        return value
    return value[:limit] + f"… [truncated, {len(value) - limit} chars omitted]"


def discover_board_databases(hermes_home: Path) -> list[BoardDatabase]:
    """Return databases for all board.json entries in deterministic slug order."""
    boards_root = hermes_home / "kanban" / "boards"
    if not boards_root.exists():
        return []

    boards: list[BoardDatabase] = []
    for config_path in sorted(boards_root.glob("*/board.json")):
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BoardReadError(f"cannot read board config {config_path}: {exc}") from exc
        slug = config.get("slug") if isinstance(config, dict) else None
        if not isinstance(slug, str) or not slug:
            raise BoardReadError(f"board config has no slug: {config_path}")
        database = (
            hermes_home / "kanban.db"
            if slug == "default"
            else boards_root / slug / "kanban.db"
        )
        boards.append(BoardDatabase(slug=slug, database=database))
    return sorted(boards, key=lambda board: board.slug)


def _copy_readonly_snapshot(path: Path) -> sqlite3.Connection:
    """Back up a read-only board connection into an isolated memory database."""
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)
    finally:
        source.close()
    snapshot.row_factory = sqlite3.Row
    return snapshot


_BLOCKED_CARDS_SQL = """
SELECT t.id,
       t.title,
       t.block_kind,
       (
           SELECT e.payload
             FROM task_events e
            WHERE e.task_id = t.id
              AND e.kind = 'blocked'
            ORDER BY e.id DESC
            LIMIT 1
       ) AS blocked_payload,
       (
           SELECT e.kind
             FROM task_events e
            WHERE e.task_id = t.id
              AND (
                  (
                      e.kind = 'operator_escalation'
                      AND COALESCE(
                          json_extract(e.payload, '$.evidence.trigger_outcome'), ''
                      ) != 'nonspawnable_assignee'
                  )
                  OR e.kind IN ('unblocked', 'promoted_manual')
              )
            ORDER BY e.id DESC
            LIMIT 1
       ) AS halt_event
  FROM tasks t
 WHERE t.status = 'blocked'
 ORDER BY t.id
"""


def _reason_from_payload(raw: str | None) -> str:
    reason = _parse_payload(raw).get("reason")
    if reason is None or reason == "":
        return MISSING
    return str(reason)


def render_board(board: BoardDatabase, *, reason_limit: int) -> list[str]:
    """Render every blocked card from one board snapshot."""
    try:
        snapshot = _copy_readonly_snapshot(board.database)
    except (OSError, sqlite3.Error) as exc:
        raise BoardReadError(
            f"cannot read board {board.slug} at {board.database}: {exc}"
        ) from exc

    try:
        rows = snapshot.execute(_BLOCKED_CARDS_SQL).fetchall()
    except sqlite3.Error as exc:
        raise BoardReadError(
            f"cannot query board {board.slug} at {board.database}: {exc}"
        ) from exc
    finally:
        snapshot.close()

    lines: list[str] = []
    for row in rows:
        block_kind = MISSING if row["block_kind"] is None else str(row["block_kind"])
        reason = _cap_text(_reason_from_payload(row["blocked_payload"]), reason_limit)
        operator_halt = "ja" if row["halt_event"] == "operator_escalation" else "nein"
        lines.append(
            f"[{board.slug}] {row['id']} — {row['title']} | "
            f"Blocktyp: {block_kind} | Grund: {reason} | "
            f"Operator-Halt: {operator_halt}"
        )
    return lines


def render_all_boards(
    hermes_home: Path, *, reason_limit: int = DEFAULT_REASON_LIMIT
) -> list[str]:
    """Render blocked cards from every configured board."""
    if reason_limit < 0:
        raise ValueError("reason_limit must not be negative")
    lines: list[str] = []
    for board in discover_board_databases(hermes_home):
        lines.extend(render_board(board, reason_limit=reason_limit))
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render blocked Kanban cards from read-only board snapshots."
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home containing kanban.db and kanban/boards (default: ~/.hermes)",
    )
    parser.add_argument(
        "--reason-limit",
        type=int,
        default=DEFAULT_REASON_LIMIT,
        help=f"maximum literal reason characters before truncation (default: {DEFAULT_REASON_LIMIT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        lines = render_all_boards(args.hermes_home, reason_limit=args.reason_limit)
    except (BoardReadError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Queryable "is it landed?" truth for kanban tasks (fork-owned consumer).

Reads the board read-only by default; the only writes go to the fork-owned
``task_landed_state`` table via ``--materialize``/``--sweep``, and only
against the live board when ``--write`` is given explicitly.  Nothing here
touches ``tasks``/``task_events``.

Examples:

    # one task, live truth (no board write)
    scripts/kanban_landed_check.py --task t_b7a9208a

    # everything materialized that disagrees with a naive "done" reading
    scripts/kanban_landed_check.py --stale

    # re-verify done tasks and persist states (writes the fork table only)
    scripts/kanban_landed_check.py --sweep --write --json

The nightly watcher around ``--sweep`` is a DRAFT in
``docs/kanban/2026-07-26-landed-state-design.md`` §5 — this script never
schedules itself; activation is an operator decision.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_landed as kl  # noqa: E402

DEFAULT_BOARD = Path.home() / ".hermes" / "kanban.db"

JOB_DRAFT = {
    "name": "kanban-landed-watch",
    "schedule": "17 3 * * *",
    "command": "scripts/kanban_landed_check.py --sweep --write --json",
    "note": "DRAFT — not installed. Activation is an operator decision.",
}


def open_board(path: Path, *, write: bool) -> sqlite3.Connection:
    uri = f"file:{path}" + ("" if write else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    return conn


def _state_to_dict(state: kl.LandedState) -> dict:
    return {
        "task_id": state.task_id,
        "state": state.state,
        "merge_commit": state.merge_commit,
        "target": state.target,
        "branch": state.branch,
        "detail": state.detail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD,
                        help="kanban board path (default: %(default)s)")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT,
                        help="git repository to verify against (default: this checkout)")
    parser.add_argument("--target", default="main",
                        help="merge target branch (default: %(default)s)")
    parser.add_argument("--task", help="classify a single task (read-only)")
    parser.add_argument("--materialize", nargs="+", metavar="TASK_ID",
                        help="classify + persist these tasks")
    parser.add_argument("--sweep", action="store_true",
                        help="re-verify done tasks with branch/witness and persist")
    parser.add_argument("--stale", action="store_true",
                        help="list materialized states disagreeing with 'done'")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap sweep candidate count")
    parser.add_argument("--write", action="store_true",
                        help="permit writes to the live board's fork table")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--print-job-draft", action="store_true",
                        help="print the (uninstalled) watcher cron draft")
    args = parser.parse_args(argv)

    if args.print_job_draft:
        print(json.dumps(JOB_DRAFT, indent=2))
        return 0

    board = args.board.expanduser().resolve()
    if not board.exists():
        print(f"error: board not found: {board}", file=sys.stderr)
        return 1
    print(f"board: {board}", file=sys.stderr)
    print(f"repo:  {args.repo}", file=sys.stderr)

    is_live = board == DEFAULT_BOARD.expanduser().resolve()
    wants_write = bool(args.sweep or args.materialize)
    if wants_write and is_live and not args.write:
        print(
            "error: refusing to write the live board without --write "
            "(reads are always allowed; this guard exists because the "
            "board is production)",
            file=sys.stderr,
        )
        return 1

    conn = open_board(board, write=wants_write)
    try:
        if args.task:
            state = kl.classify_task(conn, args.repo, args.task, target=args.target)
            payload = _state_to_dict(state)
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"{state.task_id}: {state.state}")
                for key, value in state.detail.items():
                    print(f"  {key}: {value}")
            return 0

        if args.materialize:
            results = kl.materialize(
                conn, args.repo, args.materialize, target=args.target
            )
            payload = [_state_to_dict(s) for s in results]
            print(json.dumps(payload, indent=2) if args.json else
                  "\n".join(f"{s.task_id}: {s.state}" for s in results))
            return 0

        if args.sweep:
            transitions = kl.sweep(
                conn, args.repo, target=args.target, limit=args.limit
            )
            if args.json:
                print(json.dumps(transitions, indent=2))
            else:
                if not transitions:
                    print("no state changes")
                for row in transitions:
                    print(f"{row['task_id']}: {row['old']} -> {row['new']}")
            return 0

        if args.stale:
            rows = kl.list_disagreements(conn)
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no materialized disagreements "
                      "(run --sweep --write first to populate)")
                return 0
            for row in rows:
                print(
                    f"{row['task_id']} [{row['state']}] "
                    f"task_status={row['task_status']} "
                    f"branch={row['branch']} target={row['target']}"
                )
            return 0

        parser.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

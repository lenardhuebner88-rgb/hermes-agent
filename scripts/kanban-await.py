#!/usr/bin/env python3
"""Wait for a matching Hermes kanban task event without model-side polling."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
from typing import Any, Sequence


DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_INTERVAL_SECONDS = 1.0
EXIT_TIMEOUT = 2
EXIT_RUNTIME_ERROR = 1


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wait for a matching event in the Hermes kanban database.",
    )
    parser.add_argument("--task", required=True, help="task id to wait for")
    parser.add_argument(
        "--until",
        required=True,
        help="comma-separated event kinds; unknown kinds are allowed",
    )
    parser.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"maximum wait in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--since-id",
        type=int,
        help="consider every matching event with id greater than this cursor",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"poll interval in seconds (default: {DEFAULT_INTERVAL_SECONDS:g})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="accepted for explicitness; successful output is always one JSON line",
    )
    return parser


def _database_path() -> Path:
    configured = os.environ.get("HERMES_KANBAN_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".hermes" / "kanban.db"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _latest_event_id(path: Path) -> int:
    with closing(_connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events"
        ).fetchone()
    return int(row[0])


def _decode_payload(raw_payload: Any) -> Any:
    if raw_payload is None:
        return None
    try:
        return json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError):
        return raw_payload


def _first_matching_event(
    path: Path,
    *,
    cursor: int,
    task_id: str,
    kinds: tuple[str, ...],
) -> dict[str, Any] | None:
    placeholders = ", ".join("?" for _ in kinds)
    query = (
        "SELECT id, task_id, kind, payload, created_at FROM task_events "
        f"WHERE id > ? AND task_id = ? AND kind IN ({placeholders}) "
        "ORDER BY id ASC LIMIT 1"
    )
    with closing(_connect_read_only(path)) as connection:
        row = connection.execute(query, (cursor, task_id, *kinds)).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "task_id": row[1],
        "kind": row[2],
        "payload": _decode_payload(row[3]),
        "created_at": row[4],
    }


def _wait(
    *,
    path: Path,
    task_id: str,
    kinds: tuple[str, ...],
    since_id: int | None,
    timeout: float,
    interval: float,
    stopped: threading.Event,
    interrupted_by: dict[str, int],
) -> int:
    cursor = since_id if since_id is not None else _latest_event_id(path)
    deadline = time.monotonic() + timeout

    while True:
        if stopped.is_set():
            signum = interrupted_by["signum"]
            signal_name = signal.Signals(signum).name
            print(f"kanban-await: interrupted by {signal_name}", file=sys.stderr)
            return 128 + signum

        event = _first_matching_event(
            path,
            cursor=cursor,
            task_id=task_id,
            kinds=kinds,
        )
        if event is not None:
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            return 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            expected = ",".join(kinds)
            print(
                f"kanban-await: timeout after {timeout:g}s waiting for task "
                f"{task_id!r} and event kind(s) {expected!r} after id {cursor}",
                file=sys.stderr,
            )
            return EXIT_TIMEOUT
        stopped.wait(min(interval, remaining))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    kinds = tuple(dict.fromkeys(kind.strip() for kind in args.until.split(",") if kind.strip()))
    if not kinds:
        parser.error("--until must contain at least one event kind")

    stopped = threading.Event()
    interrupted_by: dict[str, int] = {"signum": int(signal.SIGINT)}

    def stop(signum: int, _frame: Any) -> None:
        interrupted_by["signum"] = signum
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        return _wait(
            path=_database_path(),
            task_id=args.task,
            kinds=kinds,
            since_id=args.since_id,
            timeout=args.timeout,
            interval=args.interval,
            stopped=stopped,
            interrupted_by=interrupted_by,
        )
    except KeyboardInterrupt:
        print("kanban-await: interrupted by SIGINT", file=sys.stderr)
        return 128 + int(signal.SIGINT)
    except (OSError, sqlite3.Error) as exc:
        print(f"kanban-await: database error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

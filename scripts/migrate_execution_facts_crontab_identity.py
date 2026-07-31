#!/usr/bin/env python3
"""Retire crontab shadow facts recorded under the pre-2026-07-31 identity.

Until 2026-07-31 a crontab execution was identified as ``crontab:BOOT:PID``.
That identity is wrong twice over: a recycled PID merges unrelated runs into
one fact, and the fact's observed time was derived with ``min()`` over a
journal window that rotates underneath the collector -- so a retained fact
could be restated, which is what jammed the live collector.

The identity is now ``crontab:BOOT:PID:FIRST_RECORD_MS``. Both generations
otherwise look alike to the projection, so leaving the old rows in place
double-counts every crontab execution in the window.

These are shadow-only facts with ``activation_effect: none``, and the
collector rebuilds them from the journal on its next pass, so dropping the
superseded generation loses no measurement -- it removes a known-wrong one.
The append-only guarantee still holds for every identity that is still in
use: nothing is rewritten, only the retired generation is deleted.

Read-only by default; pass --apply to write, which always takes a backup
first.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sqlite3
import sys

# Rows of the retired generation have exactly two colons; the current one
# appends the first record's epoch-ms and therefore has three.
LEGACY_PREDICATE = (
    "source = 'crontab_invocation' "
    "AND source_execution_id NOT GLOB 'crontab:*:*:*'"
)


def _counts(connection: sqlite3.Connection) -> tuple[int, int]:
    legacy = connection.execute(
        f"SELECT COUNT(*) FROM execution_events WHERE {LEGACY_PREDICATE}"
    ).fetchone()[0]
    current = connection.execute(
        "SELECT COUNT(*) FROM execution_events "
        "WHERE source = 'crontab_invocation' "
        "AND source_execution_id GLOB 'crontab:*:*:*'"
    ).fetchone()[0]
    return int(legacy), int(current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/mnt/data/hermes-observability/execution_facts.db"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the deletion (default: report only)",
    )
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"no such database: {args.db}", file=sys.stderr)
        return 1

    with sqlite3.connect(args.db) as connection:
        legacy, current = _counts(connection)

    print(f"database              : {args.db}")
    print(f"retired identity rows : {legacy}")
    print(f"current identity rows : {current}")

    if legacy == 0:
        print("nothing to migrate")
        return 0
    if not args.apply:
        print("dry run - pass --apply to delete the retired generation")
        return 0

    backup = args.db.with_suffix(f"{args.db.suffix}.pre-crontab-identity")
    shutil.copy2(args.db, backup)
    print(f"backup                : {backup}")

    with sqlite3.connect(args.db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Clear the dedupe registry entries first so the identities cannot be
        # resurrected by a later append carrying the old fingerprint.
        connection.execute(
            "DELETE FROM execution_event_dedupe WHERE (source, "
            "idempotency_key) IN (SELECT source, idempotency_key "
            f"FROM execution_events WHERE {LEGACY_PREDICATE})"
        )
        deleted = connection.execute(
            f"DELETE FROM execution_events WHERE {LEGACY_PREDICATE}"
        ).rowcount
        connection.commit()

    print(f"deleted               : {deleted}")
    print("run `execution_facts.py rebuild` to refresh the projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Drop crontab shadow facts recorded under a superseded identity scheme.

The crontab identity changed twice on 2026-07-31:

1. ``crontab:BOOT:PID`` -- wrong twice over. A recycled PID merged unrelated
   runs into one fact, and the fact's time came from ``min()`` over a journal
   window that rotates underneath the collector, so a retained fact could be
   restated. That restatement is what jammed the live collector.
2. ``crontab:BOOT:PID:FIRST_MS`` -- stable, but still anonymous: it counted
   every CRON journal line, and on this host 65% of those are PAM session
   bookkeeping and 2% are daemon notices, not job runs.
3. ``crontab:USER-LABEL-DIGEST:RUN_MS`` (current) -- the journal states which
   command ran, so a run is attributed to a named job.

Generations otherwise look alike to the projection, so leaving superseded rows
in place double-counts executions. These are shadow-only facts with
``activation_effect: none`` and the collector rebuilds the whole source from
the journal on its next pass, so clearing the source loses no measurement --
it removes known-wrong ones. The append-only guarantee is unaffected for every
identity still in use: nothing is rewritten.

Read-only by default; --apply always takes a backup first.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sqlite3
import sys

SOURCE = "crontab_invocation"
# Current scheme: exactly one colon between the prefix and the run timestamp.
CURRENT_IDENTITY_GLOB = "crontab:*:*"
SUPERSEDED_PREDICATE = (
    f"source = '{SOURCE}' AND ("
    f"source_execution_id NOT GLOB '{CURRENT_IDENTITY_GLOB}' "
    "OR source_execution_id GLOB 'crontab:*:*:*')"
)


def _counts(connection: sqlite3.Connection) -> tuple[int, int]:
    superseded = connection.execute(
        f"SELECT COUNT(*) FROM execution_events WHERE {SUPERSEDED_PREDICATE}"
    ).fetchone()[0]
    total = connection.execute(
        "SELECT COUNT(*) FROM execution_events WHERE source = ?", (SOURCE,)
    ).fetchone()[0]
    return int(superseded), int(total)


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
        superseded, total = _counts(connection)

    print(f"database         : {args.db}")
    print(f"crontab rows     : {total}")
    print(f"superseded rows  : {superseded}")

    if superseded == 0:
        print("nothing to migrate")
        return 0
    if not args.apply:
        print("dry run - pass --apply to delete the superseded generation")
        return 0

    backup = args.db.with_suffix(f"{args.db.suffix}.pre-crontab-identity")
    shutil.copy2(args.db, backup)
    print(f"backup           : {backup}")

    with sqlite3.connect(args.db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Clear the dedupe registry first so a superseded identity cannot be
        # resurrected by a later append carrying its old fingerprint.
        connection.execute(
            "DELETE FROM execution_event_dedupe WHERE (source, "
            "idempotency_key) IN (SELECT source, idempotency_key "
            f"FROM execution_events WHERE {SUPERSEDED_PREDICATE})"
        )
        deleted = connection.execute(
            f"DELETE FROM execution_events WHERE {SUPERSEDED_PREDICATE}"
        ).rowcount
        connection.commit()

    print(f"deleted          : {deleted}")
    print("run `execution_facts.py rebuild --db <db>` to refresh projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

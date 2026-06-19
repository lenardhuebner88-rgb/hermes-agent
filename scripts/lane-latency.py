#!/usr/bin/env python3
"""Canonical, NULL-safe read-only telemetry helper for worker-lane latency.

Replaces the fragile inline DB heredocs that analysis/dogfood specs used to
embed. The root cause those heredocs hit: an ``output_tokens=NULL`` row crashed
``max(o,1)`` / ``i/o`` with a TypeError, so every lane improvised its own query
and produced divergent numbers. This script is the one tested source of truth.

Reads the last N completed ``task_runs`` of a lane (profile) from the live
kanban board — strictly read-only (``?mode=ro``) — and reports per run the
duration (``ended_at - started_at``), input/output tokens and the in:out ratio,
plus aggregates (median/max duration, median in:out, n). Every per-run field is
NULL-safe: a missing ``output_tokens`` becomes a clean ``n/a`` ratio, never a
TypeError.

Usage::

    scripts/lane-latency.py --lane coder
    scripts/lane-latency.py --lane coder-claude --limit 20 --json
    scripts/lane-latency.py --lane verifier --db /path/to/kanban.db

See PlanSpec ``2026-06-19-verifier-acceptance-task-class-aware-planspec.md``
(Slice B1, AC-B-helper) and the handoff
``2026-06-19-worker-latency-findings-HANDOFF.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from pathlib import Path

DEFAULT_DB = Path(os.path.expanduser("~/.hermes/kanban.db"))


def _ratio(itok, otok):
    """in:out ratio, NULL/zero-safe. Returns float, or None when not defined."""
    if not itok or not otok:  # None or 0 on either side -> n/a, never divide
        return None
    return itok / otok


def lane_report(db_path, lane: str, limit: int = 10) -> dict:
    """Read the last ``limit`` completed runs of ``lane`` from ``db_path``.

    Read-only. Completed = both ``started_at`` and ``ended_at`` present. Returns
    a dict: ``n``, ``runs`` (most-recent first), ``median_dur``, ``max_dur``,
    ``median_in_out`` (None if no run has a defined ratio).
    """
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT task_id, started_at, ended_at, input_tokens, output_tokens
            FROM task_runs
            WHERE profile = ?
              AND started_at IS NOT NULL
              AND ended_at   IS NOT NULL
            ORDER BY ended_at DESC
            LIMIT ?
            """,
            (lane, limit),
        ).fetchall()
    finally:
        con.close()

    runs = []
    for r in rows:
        dur = r["ended_at"] - r["started_at"]
        itok = r["input_tokens"]
        otok = r["output_tokens"]
        runs.append(
            {
                "task_id": r["task_id"],
                "dur": dur,
                "input_tokens": itok,
                "output_tokens": otok,
                "in_out": _ratio(itok, otok),
            }
        )

    durs = [x["dur"] for x in runs]
    ratios = [x["in_out"] for x in runs if x["in_out"] is not None]
    return {
        "lane": lane,
        "n": len(runs),
        "runs": runs,
        "median_dur": int(statistics.median(durs)) if durs else 0,
        "max_dur": max(durs) if durs else 0,
        "median_in_out": round(statistics.median(ratios), 2) if ratios else None,
    }


def _fmt_text(report: dict) -> str:
    lane = report["lane"]
    if report["n"] == 0:
        return f"lane {lane!r}: no completed runs found."
    lines = [
        f"lane {lane!r}  (n={report['n']}, last {report['n']} completed runs)",
        f"  median_dur = {report['median_dur']}s   max_dur = {report['max_dur']}s"
        f"   median_in:out = "
        + (f"{report['median_in_out']}:1" if report["median_in_out"] is not None else "n/a"),
        "",
        f"  {'task_id':<14}{'dur':>7}{'in':>10}{'out':>9}{'in:out':>9}",
    ]
    for run in report["runs"]:
        io = f"{run['in_out']:.1f}:1" if run["in_out"] is not None else "n/a"
        lines.append(
            f"  {str(run['task_id']):<14}"
            f"{run['dur']:>6}s"
            f"{(run['input_tokens'] if run['input_tokens'] is not None else 0):>10}"
            f"{(run['output_tokens'] if run['output_tokens'] is not None else 0):>9}"
            f"{io:>9}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="NULL-safe read-only worker-lane latency report.")
    p.add_argument("--lane", required=True, help="lane / profile name, e.g. coder, coder-claude, verifier")
    p.add_argument("--limit", type=int, default=10, help="number of most-recent completed runs (default 10)")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"board path (default {DEFAULT_DB})")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args(argv)

    report = lane_report(args.db, args.lane, limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_fmt_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

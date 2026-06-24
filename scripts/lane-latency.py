#!/usr/bin/env python3
"""Canonical, NULL-safe read-only telemetry helper for worker-lane latency.

This script replaces fragile inline DB heredocs used by analysis/dogfood specs.
The bug it pins: ``output_tokens=NULL`` crashed ad-hoc ``i/o`` or
``max(o, 1)`` snippets and made every lane improvise its own telemetry query.

It reads completed ``task_runs`` for a lane from a Kanban SQLite board in
read-only mode and reports per-run duration, token counts, in:out ratio and
aggregates. Public compatibility names are intentionally kept:

- ``collect`` / ``format_text`` / ``format_json`` are the current API.
- ``lane_report`` / ``_fmt_text`` remain aliases for older tests/scripts.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.path.expanduser("~/.hermes/kanban.db"))


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only and return a Row-enabled connection."""
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _ratio(itok: int | None, otok: int | None) -> float | None:
    """Return input:output ratio, NULL/zero-safe."""
    if not itok or not otok:
        return None
    return itok / otok


def collect(db_path: str | Path, lane: str, limit: int = 10) -> dict[str, Any]:
    """Read the last ``limit`` completed runs of ``lane`` from ``db_path``.

    Completed means both ``started_at`` and ``ended_at`` are present. The query is
    strictly read-only. Token fields are NULL-safe: output/input NULL renders as
    ``0`` for display while the ratio remains ``None`` (``n/a``), never a
    division error.
    """
    con = connect_ro(db_path)
    try:
        rows = con.execute(
            """
            SELECT task_id, started_at, ended_at, input_tokens, output_tokens
            FROM task_runs
            WHERE profile = ?
              AND started_at IS NOT NULL
              AND ended_at   IS NOT NULL
            ORDER BY ended_at DESC, started_at DESC, task_id DESC
            LIMIT ?
            """,
            (lane, limit),
        ).fetchall()
    finally:
        con.close()

    runs: list[dict[str, Any]] = []
    for r in rows:
        started = r["started_at"]
        ended = r["ended_at"]
        itok_raw = r["input_tokens"]
        otok_raw = r["output_tokens"]
        ratio = _ratio(itok_raw, otok_raw)
        itok = int(itok_raw or 0)
        otok = int(otok_raw or 0)
        runs.append(
            {
                "task_id": r["task_id"],
                "dur": int(ended - started),
                "input_tokens": itok,
                "output_tokens": otok,
                "in_out_ratio": ratio,
                # Back-compat for older callers/tests.
                "in_out": ratio,
            }
        )

    durs = [x["dur"] for x in runs]
    ratios = [x["in_out_ratio"] for x in runs if x["in_out_ratio"] is not None]
    return {
        "lane": lane,
        "n": len(runs),
        "runs": runs,
        "median_dur": statistics.median(durs) if durs else None,
        "max_dur": max(durs) if durs else None,
        "median_in_out": round(statistics.median(ratios), 2) if ratios else None,
    }


def lane_report(db_path: str | Path, lane: str, limit: int = 10) -> dict[str, Any]:
    """Backward-compatible alias for ``collect``."""
    return collect(db_path=db_path, lane=lane, limit=limit)


def format_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2)


def format_text(report: dict[str, Any]) -> str:
    lane = report["lane"]
    if report["n"] == 0:
        return f"lane {lane!r}: no completed runs found."
    median_dur = report["median_dur"]
    max_dur = report["max_dur"]
    median_ratio = report["median_in_out"]
    lines = [
        f"lane {lane!r}  (n={report['n']}, last {report['n']} completed runs)",
        f"  median_dur = {int(median_dur)}s   max_dur = {int(max_dur)}s"
        f"   median_in:out = "
        + (f"{median_ratio}:1" if median_ratio is not None else "n/a"),
        "",
        f"  {'task_id':<14}{'dur':>7}{'in':>10}{'out':>9}{'in:out':>9}",
    ]
    for run in report["runs"]:
        ratio = run.get("in_out_ratio", run.get("in_out"))
        io = f"{ratio:.1f}:1" if ratio is not None else "n/a"
        lines.append(
            f"  {str(run['task_id']):<14}"
            f"{run['dur']:>6}s"
            f"{run['input_tokens']:>10}"
            f"{run['output_tokens']:>9}"
            f"{io:>9}"
        )
    return "\n".join(lines)


def _fmt_text(report: dict[str, Any]) -> str:
    """Backward-compatible alias for ``format_text``."""
    return format_text(report)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="NULL-safe read-only worker-lane latency report.")
    p.add_argument("--lane", required=True, help="lane / profile name, e.g. coder, premium, reviewer")
    p.add_argument("--limit", type=int, default=10, help="number of most-recent completed runs (default 10)")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"board path (default {DEFAULT_DB})")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args(argv)

    report = collect(args.db, args.lane, limit=args.limit)
    if args.json:
        print(format_json(report))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

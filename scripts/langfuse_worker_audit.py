#!/usr/bin/env python3
"""Audit worker observability coverage without mutating Hermes or Langfuse."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from hermes_cli.usage_facts_db import usage_facts_db_path  # noqa: E402
from hermes_cli.kanban_db import kanban_home  # noqa: E402

CORRELATION_FIELDS = (
    "task_run_id",
    "task_id",
    "chain_id",
    "board",
    "session_id",
    "correlation_source",
)
SIGNAL_FIELDS = (
    "provider",
    "model",
    "profile",
    "lane",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "tool_call_count",
    "error_type",
    "first_token_ms",
    "duration_ms",
    "context_window_used",
    "billing_mode",
)


def _read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _ratio(value: int, total: int) -> float:
    return round(value / total, 4) if total else 0.0


def _coverage_by_origin(
    connection: sqlite3.Connection,
    *,
    since: str,
    columns: set[str],
) -> dict[str, dict[str, Any]]:
    fields = [
        field for field in (*CORRELATION_FIELDS, *SIGNAL_FIELDS) if field in columns
    ]
    projections = ", ".join(
        f"SUM(CASE WHEN {field} IS NOT NULL THEN 1 ELSE 0 END) AS {field}"
        for field in fields
    )
    query = (
        "SELECT origin, COUNT(*) AS rows"
        + (f", {projections}" if projections else "")
        + " FROM run_usage_facts WHERE captured_at >= ? GROUP BY origin ORDER BY rows DESC"
    )
    result: dict[str, dict[str, Any]] = {}
    for row in connection.execute(query, (since,)):
        total = int(row["rows"])
        coverage = {
            field: {
                "present": int(row[field]),
                "ratio": _ratio(int(row[field]), total),
            }
            for field in fields
        }
        result[str(row["origin"])] = {"rows": total, "coverage": coverage}
    return result


def _non_null_ids(
    connection: sqlite3.Connection,
    *,
    column: str,
    since: str,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT DISTINCT {column} FROM run_usage_facts "
            f"WHERE captured_at >= ? AND {column} IS NOT NULL",
            (since,),
        )
    }


def _board_coverage(
    path: Path | None,
    *,
    since_epoch: int,
    correlated_run_ids: set[str],
) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"available": False, "reason": "kanban_db_unavailable"}
    try:
        with closing(_read_only(path)) as connection:
            recent_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM task_runs WHERE started_at >= ?",
                    (since_epoch,),
                )
            }
    except sqlite3.Error as exc:
        return {
            "available": False,
            "reason": f"kanban_query_failed:{type(exc).__name__}",
        }
    matched = len(recent_ids & correlated_run_ids)
    return {
        "available": True,
        "recent_task_runs": len(recent_ids),
        "matched_task_runs": matched,
        "coverage_ratio": _ratio(matched, len(recent_ids)),
        "unmatched_task_runs": len(recent_ids) - matched,
    }


def _recommendations(
    *,
    schema_missing: Iterable[str],
    origins: dict[str, dict[str, Any]],
    board: dict[str, Any],
) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    missing = list(schema_missing)
    if missing:
        recommendations.append({
            "priority": "P0",
            "adapter": "usage-facts additive schema",
            "reason": "missing correlation columns: " + ", ".join(missing),
        })
    for origin, data in origins.items():
        coverage = data["coverage"]
        task_ratio = float(coverage.get("task_id", {}).get("ratio", 0.0))
        if origin in {"hermes_agent", "claude_code"} and task_ratio < 0.8:
            adapter = (
                "runtime hook correlation"
                if origin == "hermes_agent"
                else "exact claude_session_id join"
            )
            recommendations.append({
                "priority": "P1",
                "adapter": adapter,
                "reason": f"{origin} task correlation is {task_ratio:.1%}",
            })
        model_ratio = float(coverage.get("model", {}).get("ratio", 0.0))
        if data["rows"] and model_ratio < 0.8:
            recommendations.append({
                "priority": "P2",
                "adapter": f"{origin} model field adapter",
                "reason": f"model coverage is {model_ratio:.1%}",
            })
    if (
        board.get("available")
        and int(board.get("recent_task_runs", 0)) > 0
        and float(board.get("coverage_ratio", 0.0)) < 0.8
    ):
        recommendations.append({
            "priority": "P1",
            "adapter": "worker correlation backfill",
            "reason": (
                f"{board.get('unmatched_task_runs', 0)} recent Kanban runs "
                "lack an exact usage-fact run link"
            ),
        })
    return recommendations


def audit(
    *,
    usage_path: Path,
    kanban_path: Path | None,
    days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a denominator-explicit coverage report for a rolling window."""
    if days < 1:
        raise ValueError("days must be positive")
    current = now or datetime.now(timezone.utc)
    since_dt = current - timedelta(days=days)
    since = since_dt.isoformat()
    since_epoch = int(since_dt.timestamp())

    with closing(_read_only(usage_path)) as connection:
        columns = _columns(connection, "run_usage_facts")
        schema_missing = sorted(set(CORRELATION_FIELDS) - columns)
        origins = _coverage_by_origin(connection, since=since, columns=columns)
        correlated_run_ids = (
            _non_null_ids(connection, column="task_run_id", since=since)
            if "task_run_id" in columns
            else set()
        )
        total_runs = sum(int(data["rows"]) for data in origins.values())

    board = _board_coverage(
        kanban_path,
        since_epoch=since_epoch,
        correlated_run_ids=correlated_run_ids,
    )
    return {
        "audit_version": 1,
        "window": {
            "days": days,
            "from": since,
            "to": current.isoformat(),
        },
        "usage_db": {
            "available": True,
            "total_runs": total_runs,
            "schema_missing": schema_missing,
        },
        "origins": origins,
        "kanban": board,
        "recommendations": _recommendations(
            schema_missing=schema_missing,
            origins=origins,
            board=board,
        ),
        "structural_unknowns": {
            "fallback_depth": "no provider-chain position is emitted by the hook",
            "top_p": "configuration-dependent; absence is not zero",
            "foreign_ttft": "only sources that emit first-token timing can populate it",
        },
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usage-db", type=Path, default=usage_facts_db_path())
    parser.add_argument("--kanban-db", type=Path, default=kanban_home() / "kanban.db")
    parser.add_argument("--days", type=int, default=7)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        report = audit(
            usage_path=args.usage_db,
            kanban_path=args.kanban_db,
            days=args.days,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

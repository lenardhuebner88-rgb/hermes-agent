"""Export exact-correlated foreign worker facts to the existing Langfuse project."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from hermes_cli.langfuse_scores_export import _credentials, _request_with_retry
from hermes_cli.usage_facts_db import usage_facts_db_path
from hermes_constants import get_hermes_home

_LEDGER_NAME = "langfuse-worker-export-ledger.json"
_DEFAULT_BATCH_RUNS = 50
_REQUIRED_COLUMNS = {
    "run_id",
    "origin",
    "task_id",
    "session_id",
    "correlation_source",
    "captured_at",
}
_LIVE_ORIGINS = frozenset({"hermes_agent", "hermes_aux"})


@dataclass(frozen=True)
class _EligibilitySnapshot:
    selected_rows: list[sqlite3.Row]
    total_runs: int
    exact_correlated_runs: int
    eligible_foreign_runs: int
    pending_runs: int
    skipped_live_origins: int
    origins: Counter[str]


def _read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _ledger_path(path: Path | None) -> Path:
    return Path(path) if path is not None else get_hermes_home() / _LEDGER_NAME


def _read_ledger(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"worker export ledger is unreadable: {path}") from exc
    if not isinstance(value, dict) or not isinstance(
        value.get("exported_run_ids", []), list
    ):
        raise RuntimeError(f"worker export ledger has an invalid shape: {path}")
    return {str(item) for item in value.get("exported_run_ids", []) if item is not None}


def _write_ledger(path: Path, run_ids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"exported_run_ids": sorted(run_ids)}, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def trace_id_for_usage_run(run_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"hermes-usage-fact:{run_id}"))


def _event_id(kind: str, run_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"hermes-usage-fact-{kind}:{run_id}"))


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _usage_details(row: sqlite3.Row) -> dict[str, int]:
    aliases = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cache_read_input_tokens": "cache_read_tokens",
        "cache_creation_input_tokens": "cache_write_tokens",
        "reasoning": "reasoning_tokens",
    }
    details = {
        target: int(row[column])
        for target, column in aliases.items()
        if column in row.keys() and row[column] is not None
    }
    if "input" in details and "output" in details:
        details["total"] = sum(
            details.get(name, 0)
            for name in (
                "input",
                "output",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
    return details


def _safe_metadata(row: sqlite3.Row) -> dict[str, Any]:
    allowed = (
        "origin",
        "task_run_id",
        "task_id",
        "chain_id",
        "board",
        "session_id",
        "correlation_source",
        "provider",
        "model",
        "requested_provider",
        "requested_model",
        "profile",
        "lane",
        "call_kind",
        "billing_mode",
        "serving_tier",
        "reasoning_effort",
        "finish_reason",
        "error_type",
        "first_token_ms",
        "duration_ms",
        "tool_duration_ms",
        "tool_call_count",
        "context_window_used",
    )
    return {
        key: row[key] for key in allowed if key in row.keys() and row[key] is not None
    }


def events_for_row(row: sqlite3.Row) -> list[dict[str, Any]]:
    """Build two metadata-only, deterministic ingestion events."""
    run_id = str(row["run_id"])
    timestamp = _timestamp(row["captured_at"])
    if timestamp is None:
        raise ValueError(f"usage fact {run_id} has no valid captured_at")
    task_id = str(row["task_id"])
    chain_id = row["chain_id"] if "chain_id" in row.keys() else None
    profile = (
        row["profile"]
        if "profile" in row.keys() and row["profile"]
        else row["lane"]
        if "lane" in row.keys() and row["lane"]
        else row["origin"]
    )
    board = str(row["board"]) if "board" in row.keys() and row["board"] else "unknown"
    metadata = _safe_metadata(row)
    tags = [
        "kanban-worker-usage",
        "usage-facts-backfill",
        f"origin:{row['origin']}",
        f"board:{board}",
    ]
    if profile:
        tags.append(f"profile:{profile}")

    trace_id = trace_id_for_usage_run(run_id)
    trace = {
        "id": _event_id("trace", run_id),
        "type": "trace-create",
        "timestamp": timestamp,
        "body": {
            "id": trace_id,
            "timestamp": timestamp,
            "name": "foreign-worker-usage",
            "sessionId": str(chain_id or task_id),
            "userId": str(profile),
            "environment": "usage-facts-backfill",
            "tags": tags,
            "metadata": metadata,
        },
    }
    generation_body: dict[str, Any] = {
        "id": _event_id("generation", run_id),
        "traceId": trace_id,
        "name": "foreign-worker-llm-call",
        "startTime": timestamp,
        "environment": "usage-facts-backfill",
        "metadata": metadata,
    }
    if "model" in row.keys() and row["model"]:
        generation_body["model"] = str(row["model"])
    usage = _usage_details(row)
    if usage:
        generation_body["usageDetails"] = usage
    generation = {
        "id": _event_id("generation-event", run_id),
        "type": "generation-create",
        "timestamp": timestamp,
        "body": generation_body,
    }
    return [trace, generation]


def _eligibility_snapshot(
    path: Path,
    *,
    exported: set[str],
    run_limit: int | None,
) -> _EligibilitySnapshot:
    with closing(_read_only(path)) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(run_usage_facts)")
        }
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            raise RuntimeError(
                "usage facts schema lacks worker correlation columns: "
                + ", ".join(missing)
            )
        total = int(
            connection.execute("SELECT COUNT(*) FROM run_usage_facts").fetchone()[0]
        )
        origins = Counter({
            str(row["origin"]): int(row["count"])
            for row in connection.execute(
                "SELECT origin, COUNT(*) AS count FROM run_usage_facts "
                "WHERE task_id IS NOT NULL "
                "AND correlation_source IS NOT NULL "
                "AND captured_at IS NOT NULL "
                "GROUP BY origin"
            )
        })
        exact_correlated = sum(origins.values())
        skipped_live = sum(origins.get(origin, 0) for origin in _LIVE_ORIGINS)
        eligible_foreign = exact_correlated - skipped_live
        live_placeholders = ", ".join("?" for _origin in _LIVE_ORIGINS)
        cursor = connection.execute(
            "SELECT * FROM run_usage_facts "
            "WHERE task_id IS NOT NULL "
            "AND correlation_source IS NOT NULL "
            "AND captured_at IS NOT NULL "
            f"AND origin NOT IN ({live_placeholders}) "
            "ORDER BY captured_at, run_id",
            tuple(sorted(_LIVE_ORIGINS)),
        )
        selected_rows: list[sqlite3.Row] = []
        pending_runs = 0
        for row in cursor:
            if str(row["run_id"]) in exported:
                continue
            pending_runs += 1
            if run_limit is None or len(selected_rows) < run_limit:
                selected_rows.append(row)
    return _EligibilitySnapshot(
        selected_rows=selected_rows,
        total_runs=total,
        exact_correlated_runs=exact_correlated,
        eligible_foreign_runs=eligible_foreign,
        pending_runs=pending_runs,
        skipped_live_origins=skipped_live,
        origins=origins,
    )


def export_worker_facts(
    *,
    usage_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    run_limit: int | None = 100,
    batch_runs: int = _DEFAULT_BATCH_RUNS,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Export only exact-correlated foreign facts; live Hermes traces stay native."""
    if run_limit is not None and run_limit < 1:
        raise ValueError("run_limit must be positive or None")
    if batch_runs < 1:
        raise ValueError("batch_runs must be positive")
    selected = Path(usage_path) if usage_path is not None else usage_facts_db_path()
    ledger = _ledger_path(ledger_path)
    exported = _read_ledger(ledger)
    eligibility = _eligibility_snapshot(
        selected,
        exported=exported,
        run_limit=run_limit,
    )
    selected_rows = eligibility.selected_rows
    report: dict[str, Any] = {
        "total_usage_runs": eligibility.total_runs,
        "exact_correlated_runs": eligibility.exact_correlated_runs,
        "eligible_foreign_runs": eligibility.eligible_foreign_runs,
        "pending_runs": eligibility.pending_runs,
        "selected_runs": len(selected_rows),
        "posted_runs": 0,
        "posted_events": 0,
        "skipped_live_origins": eligibility.skipped_live_origins,
        "by_origin": dict(sorted(eligibility.origins.items())),
        "run_limit": run_limit,
    }
    if dry_run:
        report["would_post_events"] = len(selected_rows) * 2
        return report
    if not selected_rows:
        return report

    host, authorization = _credentials(env if env is not None else os.environ)
    for offset in range(0, len(selected_rows), batch_runs):
        batch = selected_rows[offset : offset + batch_runs]
        events = [event for row in batch for event in events_for_row(row)]
        response = _request_with_retry(
            f"{host}/api/public/ingestion",
            authorization,
            method="POST",
            payload={"batch": events},
        )
        if response.get("errors"):
            raise RuntimeError(
                "Langfuse ingestion rejected one or more worker usage events"
            )
        exported.update(str(row["run_id"]) for row in batch)
        _write_ledger(ledger, exported)
        report["posted_runs"] += len(batch)
        report["posted_events"] += len(events)
    report["pending_runs"] = eligibility.pending_runs - report["posted_runs"]
    return report

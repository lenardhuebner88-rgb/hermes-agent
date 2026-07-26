"""Read-only export of Kanban evaluation scores and trace backfill to Langfuse."""
from __future__ import annotations

import base64
import json
import math
import os
import re
import sqlite3
import statistics
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from hermes_cli.kanban_db import kanban_db_path
from hermes_constants import get_hermes_home

_PAGE_SIZE = 100
_TRACE_BATCH_SIZE = 50
_SCORE_BATCH_SIZE = 50
_SCORE_LEDGER_NAME = "langfuse-score-export-ledger.json"
DEFAULT_CRON_BACKFILL_EVENT_LIMIT = 100
_CRON_BACKFILL_EVENT_LIMIT_ENV = "HERMES_LANGFUSE_CRON_BACKFILL_EVENT_LIMIT"
_OUTCOME_NAMES = {
    1.0: "completed", 2.0: "blocked", 3.0: "iteration_budget_exhausted",
    4.0: "spawn_failed", 5.0: "gave_up", 6.0: "crashed", 7.0: "reclaimed",
    8.0: "scheduled", 9.0: "spawn_retry", 10.0: "stale", 11.0: "timed_out",
    12.0: "operator_review_required",
}


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _credentials(env: Mapping[str, str]) -> tuple[str, str]:
    # AC-3: canonical env names are *_BASE_URL; *_HOST accepted as a
    # backward-compatible fallback so existing deployments keep working.
    host = env.get("HERMES_LANGFUSE_BASE_URL", "").rstrip("/")
    if not host:
        host = env.get("HERMES_LANGFUSE_HOST", "").rstrip("/")
    public_key = env.get("HERMES_LANGFUSE_PUBLIC_KEY", "")
    secret_key = env.get("HERMES_LANGFUSE_SECRET_KEY", "")
    if not host or not public_key or not secret_key:
        raise RuntimeError(
            "Langfuse credentials missing: set HERMES_LANGFUSE_BASE_URL, "
            "HERMES_LANGFUSE_PUBLIC_KEY, and HERMES_LANGFUSE_SECRET_KEY "
            "(HERMES_LANGFUSE_HOST accepted as legacy fallback)"
        )
    basic = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return host, f"Basic {basic}"


def _request(
    url: str,
    authorization: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", authorization)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 -- explicitly configured Langfuse host
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Langfuse request failed ({exc.code}) at {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Langfuse is unreachable at {url}: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Langfuse returned invalid JSON from {url}") from exc
    return result if isinstance(result, dict) else {}


def _request_with_retry(
    url: str,
    authorization: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    retries: int = 4,
    backoff: float = 0.5,
) -> dict[str, Any]:
    """Retry transient Langfuse responses while preserving permanent errors."""
    for attempt in range(retries + 1):
        try:
            return _request(url, authorization, method=method, payload=payload)
        except RuntimeError as exc:
            message = str(exc)
            status_match = re.search(r"\((\d{3})\)", message)
            status = int(status_match.group(1)) if status_match else None
            transient = status == 429 or (status is not None and status >= 500)
            if not transient or attempt >= retries:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise AssertionError("retry loop did not return or raise")


def _score_ledger_path(path: Path | None = None) -> Path:
    return Path(path) if path is not None else get_hermes_home() / _SCORE_LEDGER_NAME


def _read_score_ledger(path: Path) -> set[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict):
        values = raw.get("exported_score_ids", raw.get("scores", []))
    else:
        values = []
    return {str(value) for value in values if value is not None}


def _write_score_ledger(path: Path, score_ids: set[str]) -> None:
    """Atomically persist the resume ledger after each successful batch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"exported_score_ids": sorted(score_ids)}, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _remote_score_ids(host: str, authorization: str) -> set[str]:
    """Read all existing score IDs once so old exports seed idempotency."""
    score_ids: set[str] = set()
    page = 1
    while True:
        query = urllib.parse.urlencode({"page": page, "limit": _PAGE_SIZE})
        response = _request_with_retry(f"{host}/api/public/scores?{query}", authorization)
        data: Any = response.get("data", [])
        if isinstance(data, dict):
            data = data.get("data", data.get("scores", []))
        if not isinstance(data, list):
            raise RuntimeError("Langfuse scores response has no data list")
        score_ids.update(
            str(score["id"])
            for score in data
            if isinstance(score, dict) and score.get("id") is not None
        )
        meta = response.get("meta")
        total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
        if not data or (total_pages is not None and page >= int(total_pages)):
            return score_ids
        if total_pages is None and len(data) < _PAGE_SIZE:
            return score_ids
        page += 1


def _trace_records(host: str, authorization: str) -> list[dict[str, Any]]:
    """Return Kanban traces, retaining fields needed for live/backfill checks."""
    records: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"page": page, "limit": _PAGE_SIZE, "tags": "kanban-worker"})
        response = _request(f"{host}/api/public/traces?{query}", authorization)
        traces = response.get("data", [])
        if not isinstance(traces, list):
            raise RuntimeError("Langfuse traces response has no data list")
        records.extend(trace for trace in traces if isinstance(trace, dict) and isinstance(trace.get("id"), str))
        meta = response.get("meta")
        total_pages = meta.get("totalPages", 1) if isinstance(meta, dict) else 1
        if page >= int(total_pages) or not traces:
            return records
        page += 1


def _trace_ids(host: str, authorization: str) -> tuple[dict[str, str], dict[str, str]]:
    by_run: dict[str, str] = {}
    by_task: dict[str, str] = {}
    for trace in _trace_records(host, authorization):
        metadata = _metadata(trace.get("metadata"))
        run_id, task_id = metadata.get("kanban_run_id"), metadata.get("kanban_task_id")
        if run_id is not None:
            by_run[str(run_id)] = trace["id"]
        if task_id is not None:
            by_task[str(task_id)] = trace["id"]
    return by_run, by_task


def trace_id_for_run(run_id: int | str) -> str:
    """Return a Langfuse-safe, stable trace id for a Kanban run."""
    return str(uuid5(NAMESPACE_URL, f"hermes-kanban-run:{run_id}"))


def trace_id_for_task(task_id: str) -> str:
    """Return a stable trace id for historical scores that outlived their run."""
    return str(uuid5(NAMESPACE_URL, f"hermes-kanban-task:{task_id}"))


def _event_id(kind: str, run_id: int | str) -> str:
    return str(uuid5(NAMESPACE_URL, f"hermes-kanban-{kind}:{run_id}"))


def _utc_timestamp(epoch: Any) -> str:
    """Convert Kanban's epoch-seconds integer without the common 1970 bug."""
    try:
        seconds = float(epoch)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid started_at epoch: {epoch!r}") from exc
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _row_value(row: sqlite3.Row, *names: str) -> Any:
    keys = row.keys()
    for name in names:
        if name in keys and row[name] is not None:
            return row[name]
    return None


def _mapping_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def _usage_details(metadata: Mapping[str, Any]) -> dict[str, Any]:
    usage = metadata.get("usage_details") or metadata.get("usage") or metadata
    if not isinstance(usage, Mapping):
        return {}
    aliases = {
        "input": ("input", "input_tokens", "prompt_tokens"),
        "output": ("output", "output_tokens", "completion_tokens"),
        "cache_read_input_tokens": ("cache_read_input_tokens", "cache_read_tokens", "cacheReadInputTokens"),
        "reasoning_tokens": ("reasoning_tokens", "reasoningTokens"),
    }
    result: dict[str, Any] = {}
    for target, keys in aliases.items():
        value = _mapping_value(usage, keys)
        if value is not None:
            result[target] = value
    return result


def _chain_roots(connection: sqlite3.Connection) -> dict[str, str]:
    """Resolve task-link roots once, tolerating old databases without task_links."""
    try:
        links = connection.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except sqlite3.OperationalError:
        return {}
    parents: dict[str, list[str]] = {}
    for parent, child in links:
        parents.setdefault(str(child), []).append(str(parent))
    roots: dict[str, str] = {}
    task_ids = set(parents) | {str(value) for values in parents.values() for value in values}
    for task_id in task_ids:
        current = task_id
        seen: set[str] = set()
        while parents.get(current) and current not in seen:
            seen.add(current)
            current = sorted(parents[current])[0]
        roots[task_id] = current
    return roots


def _retention_preflight(host: str, authorization: str, env: Mapping[str, str]) -> dict[str, Any]:
    """Ask the configured Langfuse endpoint whether retention is active.

    Self-hosted Langfuse versions expose different admin endpoints, so the endpoint
    is configurable. A missing endpoint is recorded as ``unknown`` and is not
    mistaken for an active policy; an explicit active response is write-blocking.
    """
    endpoint = env.get("HERMES_LANGFUSE_RETENTION_URL", "").strip()
    if not endpoint:
        endpoint = f"{host}/api/public/retention"
    try:
        response = _request(endpoint, authorization)
    except RuntimeError as exc:
        if "(404)" in str(exc):
            return {"checked": True, "status": "unknown", "active": False, "endpoint": endpoint}
        return {"checked": False, "status": "error", "active": False, "endpoint": endpoint, "error": str(exc)}
    policy: Any = response.get("data", response) if isinstance(response, Mapping) else response
    for key in ("retention", "retentionPolicy", "policy"):
        if isinstance(policy, Mapping) and isinstance(policy.get(key), Mapping):
            policy = policy[key]
            break
    active = False
    if isinstance(policy, Mapping):
        for key in ("active", "enabled", "isActive"):
            if isinstance(policy.get(key), bool):
                active = policy[key]
                break
        if not active:
            for key in ("retention_days", "days", "period_days"):
                value = policy.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    active = True
                    break
    return {"checked": True, "status": "active" if active else "inactive", "active": active, "endpoint": endpoint}


def _trace_events(row: sqlite3.Row, *, board: str, chain_root: str) -> list[dict[str, Any]]:
    run_id = row["id"]
    metadata = _metadata(row["metadata"])
    profile = str(_row_value(row, "profile") or metadata.get("profile") or "unknown")
    model = str(_row_value(row, "active_model") or metadata.get("model") or "unknown")
    outcome = str(_row_value(row, "outcome") or row["status"] or "unknown")
    timestamp = _utc_timestamp(row["started_at"])
    trace_id = trace_id_for_run(run_id)
    trace_metadata = dict(metadata)
    trace_metadata.update({"kanban_run_id": run_id, "kanban_task_id": row["task_id"]})
    tags = ["kanban-worker", f"board:{board}", f"profile:{profile}", f"model:{model}", f"outcome:{outcome}"]
    trace_body = {
        "id": trace_id, "timestamp": timestamp, "name": "kanban-worker-backfill",
        "sessionId": chain_root, "userId": profile, "environment": "backfill",
        "tags": tags, "metadata": trace_metadata,
    }
    generation_body: dict[str, Any] = {
        "id": _event_id("generation", run_id), "traceId": trace_id,
        "name": "kanban-worker", "startTime": timestamp, "model": model,
    }
    usage = _usage_details(metadata)
    if usage:
        generation_body["usageDetails"] = usage
    cost = _mapping_value(metadata, ("cost_usd_equivalent", "cost_usd"))
    if cost is None:
        cost = _row_value(row, "score_cost_usd", "cost_usd", "run_cost_usd")
    if cost is not None:
        generation_body["costDetails"] = {"total": cost}
    return [
        {"id": _event_id("trace", run_id), "type": "trace-create", "timestamp": timestamp, "body": trace_body},
        {"id": _event_id("generation", run_id), "type": "generation-create", "timestamp": timestamp, "body": generation_body},
    ]


def _score_coverage(
    db_path: Path,
    run_traces: Mapping[str, str],
    task_traces: Mapping[str, str],
) -> dict[str, Any]:
    """Count score rows resolvable through the trace anchors for the receipt."""
    connection = sqlite3.connect(db_path)
    try:
        try:
            rows = connection.execute("SELECT run_id, task_id FROM scores").fetchall()
        except sqlite3.OperationalError:
            return {"total_rows": 0, "matched": 0, "ratio": 0.0, "available": False}
    finally:
        connection.close()
    matched = sum(
        1 for run_id, task_id in rows
        if (run_id is not None and str(run_id) in run_traces)
        or (task_id is not None and str(task_id) in task_traces)
    )
    total = len(rows)
    return {"total_rows": total, "matched": matched,
            "ratio": (matched / total if total else 0.0), "available": True}


def synthesize_traces(
    *,
    db_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    batch_size: int = _TRACE_BATCH_SIZE,
    resume_after: int | None = None,
    resume_path: Path | None = None,
) -> dict[str, Any]:
    """Backfill one deterministic Langfuse trace + generation per task run."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    runtime_env = env or os.environ
    if resume_path and resume_path.exists() and resume_after is None:
        try:
            resume_after = int(resume_path.read_text().strip())
        except (OSError, ValueError):
            resume_after = None
    selected_db = db_path or kanban_db_path()
    connection = sqlite3.connect(selected_db)
    connection.row_factory = sqlite3.Row
    try:
        roots = _chain_roots(connection)
        try:
            rows = connection.execute("""
                SELECT r.*, t.tenant,
                       (SELECT s.value FROM scores AS s
                        WHERE s.run_id = r.id AND s.name = 'run_cost_usd'
                        ORDER BY s.id DESC LIMIT 1) AS score_cost_usd
                FROM task_runs AS r LEFT JOIN tasks AS t ON t.id = r.task_id
                WHERE (? IS NULL OR r.id > ?)
                ORDER BY r.id
            """, (resume_after, resume_after)).fetchall()
        except sqlite3.OperationalError:
            rows = connection.execute("""
                SELECT id, task_id, profile, status, started_at, outcome, metadata, active_model
                FROM task_runs WHERE (? IS NULL OR id > ?) ORDER BY id
            """, (resume_after, resume_after)).fetchall()
    finally:
        connection.close()
    host, authorization = _credentials(runtime_env)
    retention = _retention_preflight(host, authorization, runtime_env)
    report: dict[str, Any] = {
        "total_runs": len(rows), "posted": 0, "synthesized": 0,
        "skipped_live": 0, "skipped_seen": 0, "resume_after": resume_after,
        "retention": retention,
    }
    if retention["active"] or not retention["checked"]:
        report["stopped"] = "active_retention_policy"
        if not retention["active"]:
            report["stopped"] = "retention_preflight_failed"
        return report
    traces = _trace_records(host, authorization)
    existing_by_run: dict[str, list[dict[str, Any]]] = {}
    existing_ids = {str(trace["id"]) for trace in traces}
    before_run_traces: dict[str, str] = {}
    before_task_traces: dict[str, str] = {}
    for trace in traces:
        metadata = _metadata(trace.get("metadata"))
        if metadata.get("kanban_run_id") is not None:
            run_key = str(metadata["kanban_run_id"])
            existing_by_run.setdefault(run_key, []).append(trace)
            before_run_traces[run_key] = str(trace["id"])
        if metadata.get("kanban_task_id") is not None:
            before_task_traces[str(metadata["kanban_task_id"])] = str(trace["id"])
    coverage_before = _score_coverage(selected_db, before_run_traces, before_task_traces)
    board = runtime_env.get("HERMES_KANBAN_BOARD", "default")
    pending: list[tuple[int, list[dict[str, Any]]]] = []
    for row in rows:
        run_id = str(row["id"])
        seen = existing_by_run.get(run_id, [])
        if trace_id_for_run(row["id"]) in existing_ids or any(
            _metadata(t.get("metadata")).get("environment") == "backfill" or t.get("environment") == "backfill"
            for t in seen
        ):
            report["skipped_seen"] += 1
            report["resume_after"] = row["id"]
            continue
        if any(
            _metadata(t.get("metadata")).get("environment", t.get("environment")) != "backfill"
            for t in seen
        ):
            report["skipped_live"] += 1
            report["resume_after"] = row["id"]
            continue
        metadata = _metadata(row["metadata"])
        chain_root = (
            metadata.get("plan_spec_chain_root") or metadata.get("chain_root")
            or metadata.get("chain_id") or metadata.get("workflow_id")
            or roots.get(str(row["task_id"]), str(row["task_id"]))
        )
        run_board = str(metadata.get("board") or board)
        pending.append((row["id"], _trace_events(row, board=run_board, chain_root=str(chain_root))))
    if dry_run:
        report["would_post"] = sum(len(events) for _, events in pending)
        if pending:
            report["resume_after"] = pending[-1][0]
        after_run_traces = dict(before_run_traces)
        after_task_traces = dict(before_task_traces)
        for row_id, _events in pending:
            row = next(row for row in rows if row["id"] == row_id)
            after_run_traces[str(row_id)] = trace_id_for_run(row_id)
            after_task_traces[str(row["task_id"])] = trace_id_for_run(row_id)
        report["coverage_before"] = coverage_before
        report["coverage_after"] = _score_coverage(selected_db, after_run_traces, after_task_traces)
        return report
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        payload = {"batch": [event for _, events in batch for event in events]}
        _request(f"{host}/api/public/ingestion", authorization, method="POST", payload=payload)
        report["posted"] += len(payload["batch"])
        report["synthesized"] += len(batch)
        report["resume_after"] = batch[-1][0]
        if resume_path:
            resume_path.parent.mkdir(parents=True, exist_ok=True)
            resume_path.write_text(str(batch[-1][0]))
    after_run_traces = dict(before_run_traces)
    after_task_traces = dict(before_task_traces)
    for row_id, _events in pending:
        row = next(row for row in rows if row["id"] == row_id)
        after_run_traces[str(row_id)] = trace_id_for_run(row_id)
        after_task_traces[str(row["task_id"])] = trace_id_for_run(row_id)
    report["coverage_before"] = coverage_before
    report["coverage_after"] = _score_coverage(selected_db, after_run_traces, after_task_traces)
    return report


# Descriptive aliases retained for callers that use the export terminology.
export_traces = synthesize_traces
export_trace_backfill = synthesize_traces


CATEGORICAL_SCORE_NAMES = frozenset({"review_verdict", "run_outcome_kind", "task_outcome"})


def _score_payload(row: sqlite3.Row, trace_id: str) -> dict[str, Any]:
    name, value = str(row["name"]), row["value"]
    payload: dict[str, Any] = {"id": f"hermes-board-score-{row['id']}", "traceId": trace_id,
                                "name": name, "value": value}
    # Langfuse /api/public/scores requires CATEGORICAL values to be the category
    # STRING itself (numeric value -> HTTP 400 "expected string, received number").
    if name == "review_verdict":
        payload.update({"value": "APPROVED" if float(value or 0) == 1.0 else "NEEDS_REVISION",
                        "dataType": "CATEGORICAL"})
    elif name == "run_outcome_kind":
        payload.update({"value": row["outcome"] or _OUTCOME_NAMES.get(float(value or 0), "unknown"),
                        "dataType": "CATEGORICAL"})
    elif name in CATEGORICAL_SCORE_NAMES:
        payload.update({"value": str(value), "dataType": "CATEGORICAL"})
    else:
        payload["dataType"] = "NUMERIC"
    return payload


def _score_event(row: sqlite3.Row, trace_id: str) -> dict[str, Any]:
    payload = _score_payload(row, trace_id)
    timestamp = _score_timestamp(row)
    return {
        "id": str(uuid5(NAMESPACE_URL, f"hermes-kanban-score-event:{row['id']}")),
        "timestamp": timestamp,
        "type": "score-create",
        "body": payload,
    }


def _score_timestamp(row: sqlite3.Row) -> str:
    if "created_at" in row.keys() and row["created_at"] is not None:
        return _utc_timestamp(row["created_at"])
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _score_anchor_event(row: sqlite3.Row, trace_id: str) -> dict[str, Any]:
    """Create a task-scoped trace for scores whose original run was pruned.

    The task ID and event ID are deterministic, so retrying an ingestion batch
    cannot multiply anchors. Missing historical profile data follows the run
    trace contract and is represented as ``unknown`` rather than fabricated.
    """
    task_id = str(row["task_id"])
    metadata: dict[str, Any] = {
        "kanban_task_id": task_id,
        "kanban_score_anchor": True,
    }
    if row["name"] == "run_cost_usd" and isinstance(row["value"], (int, float)):
        cost = float(row["value"])
        if math.isfinite(cost):
            metadata["score_cost_usd"] = cost
    timestamp = _score_timestamp(row)
    body = {
        "id": trace_id,
        "timestamp": timestamp,
        "name": "kanban-score-backfill",
        "sessionId": task_id,
        "userId": str(_row_value(row, "profile") or "unknown"),
        "environment": "backfill",
        "tags": ["kanban-worker", "score-anchor"],
        "metadata": metadata,
    }
    return {
        "id": _event_id("score-anchor", task_id),
        "timestamp": timestamp,
        "type": "trace-create",
        "body": body,
    }


def _export_score_backfill(
    rows: list[sqlite3.Row],
    *,
    host: str,
    authorization: str,
    env: Mapping[str, str],
    dry_run: bool,
    ledger_path: Path | None,
    batch_size: int,
    event_limit: int | None,
    cron_backfill_limit: int | None = None,
) -> dict[str, Any]:
    """Backfill scores through ingestion with a durable, idempotent ledger."""
    path = _score_ledger_path(ledger_path)
    known_ids = _read_score_ledger(path)
    # This seed is deliberately done before matching: deterministic IDs from
    # exports performed before the ledger existed must count as seen.
    known_ids.update(_remote_score_ids(host, authorization))

    by_run, by_task = _trace_ids(host, authorization)
    pending: list[tuple[sqlite3.Row | None, dict[str, Any]]] = []
    anchored_tasks: set[str] = set()
    matchable = unmatched = skipped_seen = anchored_rows = 0
    for row in rows:
        task_id = str(row["task_id"])
        trace_id = by_run.get(str(row["run_id"])) if row["run_id"] is not None else None
        trace_id = trace_id or by_task.get(str(row["task_id"]))
        if not trace_id:
            trace_id = trace_id_for_task(task_id)
            needs_anchor = True
            anchored_rows += 1
        else:
            needs_anchor = False
        matchable += 1
        event = _score_event(row, trace_id)
        score_id = str(event["body"]["id"])
        if score_id in known_ids:
            skipped_seen += 1
        else:
            if needs_anchor and task_id not in anchored_tasks:
                anchored_tasks.add(task_id)
                pending.append((None, _score_anchor_event(row, trace_id)))
            pending.append((row, event))

    planned = pending if event_limit is None else pending[:event_limit]
    planned_anchors = sum(1 for row, _event in planned if row is None)
    planned_scores = len(planned) - planned_anchors
    report: dict[str, Any] = {
        "total_rows": len(rows),
        "matchable": matchable,
        "posted_new": 0,
        "skipped_seen": skipped_seen,
        "unmatched": unmatched,
        "anchor_count": len(anchored_tasks),
        "anchored_rows": anchored_rows,
        "pending_events": len(pending),
        "planned_events": len(planned),
        "planned_anchors": planned_anchors,
        "planned_scores": planned_scores,
        "remaining_events": len(pending) - len(planned),
        # Keep the original result vocabulary for callers that only display
        # the generic export summary.
        "matched": matchable,
        "posted": 0,
    }
    if cron_backfill_limit is not None and report["remaining_events"]:
        report["backfill_limit_message"] = (
            f"cron backfill capped at {cron_backfill_limit} events; "
            f"{report['remaining_events']} event(s) remain"
        )
    if dry_run:
        report["would_post"] = len(planned)
        return report

    if planned_anchors:
        retention = _retention_preflight(host, authorization, env)
        report["retention"] = retention
        if retention["active"] or not retention["checked"]:
            report["stopped"] = (
                "active_retention_policy" if retention["active"] else "retention_preflight_failed"
            )
            return report

    # Preserve the old unbounded behavior, including seeding the local ledger
    # from remote score IDs before it begins. A bounded batch instead writes
    # only after an acknowledged ingestion request, so a failed canary cannot
    # advance its cursor.
    if event_limit is None:
        _write_score_ledger(path, known_ids)

    effective_batch_size = max(1, batch_size)
    for offset in range(0, len(planned), effective_batch_size):
        batch = planned[offset:offset + effective_batch_size]
        _request_with_retry(
            f"{host}/api/public/ingestion",
            authorization,
            method="POST",
            payload={"batch": [event for _row, event in batch]},
        )
        score_events = [(row, event) for row, event in batch if row is not None]
        known_ids.update(str(event["body"]["id"]) for _row, event in score_events)
        # Persist after every acknowledged batch. If a later batch fails, the
        # next invocation resumes at exactly this boundary.
        _write_score_ledger(path, known_ids)
        report["posted_new"] += len(score_events)
    report["posted"] = report["posted_new"]
    return report


def _cron_env_float(name: str, default: float) -> float:
    """Read a finite positive alert threshold without breaking a cron run."""
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _cron_backfill_event_limit(env: Mapping[str, str]) -> int:
    """Read a positive cron backfill limit without letting bad env break cron."""
    try:
        value = int(env.get(_CRON_BACKFILL_EVENT_LIMIT_ENV, DEFAULT_CRON_BACKFILL_EVENT_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_CRON_BACKFILL_EVENT_LIMIT
    return value if value > 0 else DEFAULT_CRON_BACKFILL_EVENT_LIMIT


def _nearest_rank_p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def cron_export_alert(
    *,
    db_path: Path | None = None,
    now: int | None = None,
    cost_p95_multiplier: float | None = None,
    cache_hit_ratio_threshold: float | None = None,
) -> str | None:
    """Return one compact daily alert, or ``None`` when the cron should stay quiet.

    The cost baseline intentionally uses seven *completed* UTC days, so a
    partially accrued current day cannot inflate its own reference median.
    """
    cost_p95_multiplier = (
        cost_p95_multiplier
        if cost_p95_multiplier is not None
        else _cron_env_float("HERMES_LANGFUSE_COST_P95_MULTIPLIER", 3.0)
    )
    cache_hit_ratio_threshold = (
        cache_hit_ratio_threshold
        if cache_hit_ratio_threshold is not None
        else _cron_env_float("HERMES_LANGFUSE_CACHE_HIT_RATIO_THRESHOLD", 0.20)
    )
    if cost_p95_multiplier <= 0 or cache_hit_ratio_threshold <= 0:
        return None

    current_time = int(time.time()) if now is None else int(now)
    day_seconds = 24 * 60 * 60
    today_start = current_time // day_seconds * day_seconds
    path = Path(db_path) if db_path is not None else kanban_db_path()
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            def _values(name: str, start: int, end: int) -> list[float]:
                rows = conn.execute(
                    "SELECT value FROM scores "
                    "WHERE name = ? AND created_at >= ? AND created_at < ? "
                    "AND value IS NOT NULL",
                    (name, start, end),
                ).fetchall()
                return [float(row["value"]) for row in rows]

            current_cost_p95 = _nearest_rank_p95(
                _values("run_cost_usd", today_start, today_start + day_seconds)
            )
            historical_p95s = [
                p95
                for offset in range(1, 8)
                if (p95 := _nearest_rank_p95(_values(
                    "run_cost_usd",
                    today_start - offset * day_seconds,
                    today_start - (offset - 1) * day_seconds,
                ))) is not None
            ]
            cache_values = _values(
                "cache_hit_ratio", today_start, today_start + day_seconds
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    messages: list[str] = []
    if current_cost_p95 is not None and len(historical_p95s) == 7:
        baseline = float(statistics.median(historical_p95s))
        limit = baseline * cost_p95_multiplier
        if current_cost_p95 > limit:
            messages.append(
                f"cost p95 ${current_cost_p95:.4g} exceeds "
                f"{cost_p95_multiplier:.4g}x 7d median ${baseline:.4g}"
            )
    if cache_values:
        cache_hit_ratio = statistics.fmean(cache_values)
        if cache_hit_ratio < cache_hit_ratio_threshold:
            messages.append(
                f"cache hit ratio {cache_hit_ratio:.1%} below "
                f"{cache_hit_ratio_threshold:.1%}"
            )
    return "; ".join(messages) or None


def export_scores(
    *,
    db_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    backfill: bool = False,
    ledger_path: Path | None = None,
    batch_size: int = _SCORE_BATCH_SIZE,
    event_limit: int | None = None,
    cron: bool = False,
) -> dict[str, Any]:
    """Export active-board scores without mutating the Kanban database."""
    selected_db = db_path or kanban_db_path()
    connection = sqlite3.connect(selected_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT s.id, s.run_id, s.task_id, s.name, s.value, s.value_type, s.source, s.created_at,
                   r.profile, r.active_model, r.outcome
            FROM scores AS s LEFT JOIN task_runs AS r ON r.id = s.run_id
            ORDER BY s.id
        """).fetchall()
    finally:
        connection.close()

    names = Counter(str(row["name"]) for row in rows)
    # Dry-run still resolves real traces, but never writes scores.
    runtime_env = env or os.environ
    host, authorization = _credentials(runtime_env)
    if event_limit is not None:
        if not backfill:
            raise ValueError("event_limit requires backfill=True")
        if event_limit < 1:
            raise ValueError("event_limit must be at least 1")
    if backfill:
        cron_backfill_limit = (
            _cron_backfill_event_limit(runtime_env)
            if cron and event_limit is None
            else None
        )
        return _export_score_backfill(
            rows,
            host=host,
            authorization=authorization,
            env=runtime_env,
            dry_run=dry_run,
            ledger_path=ledger_path,
            batch_size=batch_size,
            event_limit=event_limit if event_limit is not None else cron_backfill_limit,
            cron_backfill_limit=cron_backfill_limit,
        )
    by_run, by_task = _trace_ids(host, authorization)
    matched = unmatched = posted = 0
    for row in rows:
        trace_id = by_run.get(str(row["run_id"])) if row["run_id"] is not None else None
        trace_id = trace_id or by_task.get(str(row["task_id"]))
        if not trace_id:
            unmatched += 1
            continue
        matched += 1
        if not dry_run:
            _request(f"{host}/api/public/scores", authorization, method="POST", payload=_score_payload(row, trace_id))
            posted += 1
    return {"matched": matched, "unmatched": unmatched, "posted": posted, "names": dict(names)}

"""Read-only export of Kanban evaluation scores to Langfuse's Scores API."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.kanban_db import kanban_db_path

_PAGE_SIZE = 100
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


def _request(url: str, authorization: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
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


def _trace_ids(host: str, authorization: str) -> tuple[dict[str, str], dict[str, str]]:
    by_run: dict[str, str] = {}
    by_task: dict[str, str] = {}
    page = 1
    while True:
        query = urllib.parse.urlencode({"page": page, "limit": _PAGE_SIZE, "tags": "kanban-worker"})
        response = _request(f"{host}/api/public/traces?{query}", authorization)
        traces = response.get("data", [])
        if not isinstance(traces, list):
            raise RuntimeError("Langfuse traces response has no data list")
        for trace in traces:
            if not isinstance(trace, dict) or not isinstance(trace.get("id"), str):
                continue
            metadata = _metadata(trace.get("metadata"))
            run_id, task_id = metadata.get("kanban_run_id"), metadata.get("kanban_task_id")
            if run_id is not None:
                by_run[str(run_id)] = trace["id"]
            if task_id is not None:
                by_task[str(task_id)] = trace["id"]
        meta = response.get("meta")
        total_pages = meta.get("totalPages", 1) if isinstance(meta, dict) else 1
        if page >= int(total_pages) or not traces:
            return by_run, by_task
        page += 1


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
    else:
        payload["dataType"] = "NUMERIC"
    return payload


def export_scores(*, db_path: Path | None = None, env: Mapping[str, str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Export active-board scores without mutating the Kanban database."""
    selected_db = db_path or kanban_db_path()
    connection = sqlite3.connect(selected_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT s.id, s.run_id, s.task_id, s.name, s.value, s.value_type, s.source,
                   r.profile, r.active_model, r.outcome
            FROM scores AS s LEFT JOIN task_runs AS r ON r.id = s.run_id
            ORDER BY s.id
        """).fetchall()
    finally:
        connection.close()

    names = Counter(str(row["name"]) for row in rows)
    # Dry-run still resolves real traces, but never writes scores.
    host, authorization = _credentials(env or os.environ)
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

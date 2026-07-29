#!/usr/bin/env python3
"""Audit worker observability coverage without mutating Hermes or Langfuse."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

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
WORKER_DIMENSION_FIELDS = (
    "task_run_id",
    "task_id",
    "chain_id",
    "board",
    "profile",
    "lane",
    "session_id",
    "origin",
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
LIVE_SMOKE_CONTRACT_VERSION = 1
WORKER_RUNTIME_FACTS_SCHEMA_VERSION = 1
USAGE_CORRELATION_SCHEMA_VERSION = 1


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


def _present(value: Any) -> bool:
    """Return whether a value is a stored fact rather than an empty placeholder."""
    return value is not None and bool(str(value).strip())


def _worker_release_invariant(
    usage_connection: sqlite3.Connection,
    *,
    usage_columns: set[str],
    kanban_path: Path | None,
    since: str,
    since_epoch: int,
) -> dict[str, Any]:
    """Measure only evidence that proves one complete worker-run identity.

    A task or session id alone is deliberately insufficient: retries and chain
    workers make either ambiguous. This preserves a fail-closed release signal
    when a partial rollout omits an identity dimension.
    """
    missing = sorted(set(WORKER_DIMENSION_FIELDS) - usage_columns)
    selected = [field for field in WORKER_DIMENSION_FIELDS if field in usage_columns]
    rows = (
        [
            dict(row)
            for row in usage_connection.execute(
                "SELECT " + ", ".join(selected)
                + " FROM run_usage_facts WHERE captured_at >= ?",
                (since,),
            )
        ]
        if selected
        else []
    )
    observed_run_ids = {
        str(row["task_run_id"]).strip()
        for row in rows
        if _present(row.get("task_run_id"))
    }
    sessions: dict[str, set[str]] = defaultdict(set)
    session_facts: dict[str, int] = defaultdict(int)
    for row in rows:
        if _present(row.get("session_id")) and _present(row.get("task_run_id")):
            session_id = str(row["session_id"]).strip()
            sessions[session_id].add(str(row["task_run_id"]).strip())
            session_facts[session_id] += 1
    ambiguous_sessions = {
        session_id for session_id, run_ids in sessions.items() if len(run_ids) > 1
    }

    board_runs: dict[str, str] = {}
    board_available = False
    task_identity_available = False
    if kanban_path is not None and kanban_path.is_file():
        try:
            with closing(_read_only(kanban_path)) as board_connection:
                board_columns = _columns(board_connection, "task_runs")
                task_identity_available = "task_id" in board_columns
                projections = "id, task_id" if task_identity_available else "id"
                board_runs = {
                    str(row["id"]): (
                        str(row["task_id"]) if task_identity_available else ""
                    )
                    for row in board_connection.execute(
                        f"SELECT {projections} FROM task_runs WHERE started_at >= ?",
                        (since_epoch,),
                    )
                }
                board_available = True
        except sqlite3.Error:
            # The legacy board summary carries the operational cause. This
            # invariant remains zero rather than inventing an exact link.
            pass

    exact_ids: set[str] = set()
    task_only_count = 0
    if not missing and board_available and task_identity_available:
        known_task_ids = set(board_runs.values())
        for row in rows:
            run_id = (
                str(row["task_run_id"]).strip()
                if _present(row.get("task_run_id"))
                else ""
            )
            task_id = (
                str(row["task_id"]).strip()
                if _present(row.get("task_id"))
                else ""
            )
            if not run_id:
                if task_id and task_id in known_task_ids:
                    task_only_count += 1
                continue
            if all(_present(row.get(field)) for field in WORKER_DIMENSION_FIELDS) and (
                board_runs.get(run_id) == task_id
            ):
                exact_ids.add(run_id)

    denominator = len(board_runs)
    return {
        "schema_capable": {"available": not missing, "missing_fields": missing},
        "observed_worker_runs": {
            "count": len(observed_run_ids),
            "denominator_usage_facts": len(rows),
        },
        "exact_run_links": {
            "count": len(exact_ids),
            "denominator_recent_task_runs": denominator,
            "coverage_ratio": _ratio(len(exact_ids), denominator),
        },
        "exact_task_only_links": {
            "count": task_only_count,
            "denominator_usage_facts": len(rows),
        },
        "ambiguous_session_ids": {
            "count": len(ambiguous_sessions),
            "usage_facts": sum(session_facts[session] for session in ambiguous_sessions),
        },
        "unresolved_runs": {
            "count": len(set(board_runs) - exact_ids),
            "denominator_recent_task_runs": denominator,
        },
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
        worker_release_invariant = _worker_release_invariant(
            connection,
            usage_columns=columns,
            kanban_path=kanban_path,
            since=since,
            since_epoch=since_epoch,
        )

    board = _board_coverage(
        kanban_path,
        since_epoch=since_epoch,
        correlated_run_ids=correlated_run_ids,
    )
    return {
        "audit_version": 2,
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
        "worker_release_invariant": worker_release_invariant,
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


_LIFECYCLE_EVENTS = (
    "queued", "claimed", "spawn_started", "process_started",
    "first_llm_request", "first_token", "ended",
)


class LangfuseCredentialsMissing(RuntimeError):
    """The live probe cannot start without configured endpoint credentials."""


def _short_identifier(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    return text if len(text) <= 12 else f"{text[:8]}…{text[-4:]}"


def _configured_langfuse_trace_probe() -> list[dict[str, Any]]:
    """Read the authenticated public trace API without exposing credentials."""
    from hermes_cli.langfuse_scores_export import _credentials, _trace_records

    try:
        host, authorization = _credentials(os.environ)
    except RuntimeError:
        raise LangfuseCredentialsMissing from None
    return _trace_records(host, authorization)


def _langfuse_failure_receipt(exc: Exception) -> dict[str, Any]:
    """Classify a probe failure without copying exception text or response bodies."""
    cause: BaseException = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    receipt: dict[str, Any] = {
        "credentials_configured": None,
        "tcp_http_reachable": False,
        "authenticated_public_api_readable": False,
        "exact_trace_link": False,
        "trace_id_short": None,
        "error_type": type(cause).__name__,
    }
    if isinstance(exc, LangfuseCredentialsMissing):
        receipt["credentials_configured"] = False
    elif isinstance(cause, urllib.error.HTTPError):
        receipt["credentials_configured"] = True
        receipt["tcp_http_reachable"] = True
        receipt["http_status"] = cause.code
    elif isinstance(cause, json.JSONDecodeError):
        receipt["credentials_configured"] = True
        receipt["tcp_http_reachable"] = True
    elif isinstance(cause, urllib.error.URLError):
        receipt["credentials_configured"] = True
    return receipt


def _control_surface_dashboard_request(url: str, token: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urlopen(request, timeout=30.0) as response:  # noqa: S310 - validated below
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("dashboard_payload_invalid")
    return payload


def _validated_dashboard_url(base_url: str, *, days: int) -> str:
    parsed = urlparse(base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("dashboard_url_invalid")
    return (
        f"{base_url.rstrip('/')}/api/plugins/kanban/stats/observability"
        f"?{urlencode({'days': days})}"
    )


def _langfuse_page(
    host: str,
    authorization: str,
    *,
    page: int,
    limit: int,
    from_start_time: str,
    request_json: Callable[..., dict[str, Any]],
) -> tuple[list[dict[str, Any]], int | None]:
    url = (
        f"{host}/api/public/observations?"
        f"{urlencode({'page': page, 'limit': limit, 'fromStartTime': from_start_time})}"
    )
    payload = request_json(url, authorization, timeout=30.0)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("langfuse_payload_invalid")
    meta = payload.get("meta")
    total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
    if not isinstance(total_pages, int) or total_pages < 0:
        total_pages = None
    return [row for row in rows if isinstance(row, dict)], total_pages


def _scan_langfuse_pages(
    host: str,
    authorization: str,
    *,
    page_size: int,
    max_pages: int,
    from_start_time: str,
    request_json: Callable[..., dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, bool]:
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        current, total_pages = _langfuse_page(
            host,
            authorization,
            page=page,
            limit=page_size,
            from_start_time=from_start_time,
            request_json=request_json,
        )
        rows.extend(current)
        if total_pages is not None and page >= total_pages:
            return rows, page, True
        if total_pages is None and len(current) < page_size:
            return rows, page, True
    return rows, max_pages, False


def _scan_receipt(
    rows: list[dict[str, Any]],
    *,
    days: int,
    pages: int,
    complete: bool,
    reason: str | None,
) -> dict[str, Any]:
    from plugins.kanban.dashboard.langfuse_read_model import _aggregate

    aggregate = _aggregate(
        rows,
        days=days,
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        truncation_reason=reason,
    )
    count = aggregate["metrics"]["generation_calls"]
    coverage = aggregate["coverage"]
    return {
        "state": "fresh" if complete else "partial",
        "reason": reason,
        "summary": {
            "count": {
                "value": count["value"],
                "lower_bound": count["value_semantics"] == "lower_bound",
            }
        },
        "coverage": {
            "scan_pages": pages,
            "scan_truncated": not complete,
            "scanned_generations": len(rows),
            "window_coverage": (
                1.0 if complete else None
            ),
            "counts_semantics": coverage["counts_semantics"],
        },
    }


def build_control_surface_live_smoke(
    *,
    dashboard_base_url: str,
    dashboard_token: str,
    days: int = 7,
    warm_calls: int = 5,
    page_size: int = 100,
    env: Mapping[str, str] | None = None,
    langfuse_request: Callable[..., dict[str, Any]] | None = None,
    dashboard_request: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the separately approved, secret-safe Langfuse/dashboard live smoke."""
    from plugins.kanban.dashboard import langfuse_read_model

    if warm_calls < 5 or not dashboard_token:
        raise ValueError("dashboard authentication and at least five warm calls are required")
    if days < 1 or page_size < 1:
        raise ValueError("days and page_size must be positive")
    dashboard_url = _validated_dashboard_url(dashboard_base_url, days=days)
    request_json = langfuse_request or langfuse_read_model._request_json
    dashboard_probe = dashboard_request or _control_surface_dashboard_request
    source_env = os.environ if env is None else env
    resolution = langfuse_read_model._resolve_config(source_env)

    report: dict[str, Any] = {
        "contract_version": 1,
        "status": "fail",
        "secrets_redacted": True,
    }
    if resolution.config is None:
        report["langfuse"] = {
            "state": "absent",
            "reason": resolution.error or "configuration_missing",
            "configured_host_checked": False,
        }
    else:
        host, authorization = langfuse_read_model._credentials(resolution.config)
        from_start_time = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat().replace("+00:00", "Z")
        try:
            first_page, _ = _langfuse_page(
                host,
                authorization,
                page=1,
                limit=page_size,
                from_start_time=from_start_time,
                request_json=request_json,
            )
            full_rows, full_pages, full_complete = _scan_langfuse_pages(
                host,
                authorization,
                page_size=page_size,
                max_pages=1_000,
                from_start_time=from_start_time,
                request_json=request_json,
            )
            limited_rows, limited_pages, _ = _scan_langfuse_pages(
                host,
                authorization,
                page_size=1,
                max_pages=1,
                from_start_time=from_start_time,
                request_json=request_json,
            )
            report["langfuse"] = {
                "state": "fresh" if full_complete else "partial",
                "configured_host_checked": True,
                "public_api_page": {
                    "authenticated": True,
                    "rows": len(first_page),
                },
                "full_scan": _scan_receipt(
                    full_rows,
                    days=days,
                    pages=full_pages,
                    complete=full_complete,
                    reason=None if full_complete else "smoke_page_limit",
                ),
                "limited_scan": _scan_receipt(
                    limited_rows,
                    days=days,
                    pages=limited_pages,
                    complete=False,
                    reason="intentional_smoke_limit",
                ),
            }
        except Exception as exc:
            report["langfuse"] = {
                "state": "absent",
                "reason": "unreachable",
                "error_type": type(exc).__name__,
                "configured_host_checked": True,
            }

    try:
        dashboard_probe(dashboard_url, dashboard_token)
        wall_samples: list[float] = []
        cpu_samples: list[float] = []
        payload: dict[str, Any] = {}
        for _ in range(warm_calls):
            wall_started = time.perf_counter()
            cpu_started = time.process_time()
            payload = dashboard_probe(dashboard_url, dashboard_token)
            cpu_samples.append((time.process_time() - cpu_started) * 1_000)
            wall_samples.append((time.perf_counter() - wall_started) * 1_000)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        langfuse = (
            payload.get("langfuse") if isinstance(payload.get("langfuse"), dict) else {}
        )
        summary = usage.get("summary") if isinstance(usage.get("summary"), dict) else {}
        mean_wall = statistics.fmean(wall_samples)
        mean_cpu = statistics.fmean(cpu_samples)
        report["dashboard"] = {
            "authenticated": True,
            "warm_calls": warm_calls,
            "wall_ms": {
                "median": statistics.median(wall_samples),
                "maximum": max(wall_samples),
                "mean": mean_wall,
            },
            "cpu_ms": {
                "median": statistics.median(cpu_samples),
                "maximum": max(cpu_samples),
                "mean": mean_cpu,
            },
            "fact_rows": summary.get("fact_rows"),
            "usage_state": usage.get("state"),
            "langfuse_state": langfuse.get("state"),
            "budget": {
                "cpu_mean_limit_ms": 200,
                "wall_mean_limit_ms": 300,
                "passed": mean_cpu < 200 and mean_wall < 300,
            },
        }
    except Exception:
        report["dashboard"] = {
            "authenticated": False,
            "state": "unreachable",
        }

    langfuse_ok = report["langfuse"].get("state") == "fresh"
    dashboard_ok = bool(report["dashboard"].get("budget", {}).get("passed"))
    if langfuse_ok and dashboard_ok:
        report["status"] = "pass"
    return report


def _trace_metadata(trace: dict[str, Any]) -> dict[str, Any]:
    metadata = trace.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            decoded = json.loads(metadata)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def build_live_smoke_contract(
    *,
    task_run_id: int,
    usage_path: Path,
    kanban_path: Path,
    trace_probe: Callable[[], list[dict[str, Any]]] = _configured_langfuse_trace_probe,
) -> dict[str, Any]:
    """Build a fail-closed, content-free receipt for one approved worker run."""
    run_id = str(int(task_run_id))
    exact_trace: dict[str, Any] | None = None
    try:
        for trace in trace_probe():
            metadata = _trace_metadata(trace)
            observed = metadata.get("task_run_id", metadata.get("kanban_run_id"))
            if observed is not None and str(observed) == run_id:
                exact_trace = trace
                break
        langfuse: dict[str, Any] = {
            "credentials_configured": True,
            "tcp_http_reachable": True,
            "authenticated_public_api_readable": True,
            "exact_trace_link": exact_trace is not None,
            "trace_id_short": _short_identifier(
                exact_trace.get("id") if exact_trace is not None else None
            ),
        }
    except Exception as exc:  # fail closed; never serialize a possibly sensitive message
        langfuse = _langfuse_failure_receipt(exc)

    with closing(_read_only(usage_path)) as connection:
        usage_columns = _columns(connection, "run_usage_facts")
        usage_schema_missing = sorted(
            {"task_run_id", "task_id", "captured_at"} - usage_columns
        )
        usage_rows = [] if usage_schema_missing else connection.execute(
            "SELECT task_id FROM run_usage_facts WHERE task_run_id = ?", (run_id,)
        ).fetchall()
        usage_task_ids = {str(row["task_id"]) for row in usage_rows if row["task_id"]}


    with closing(_read_only(kanban_path)) as connection:
        run = connection.execute(
            "SELECT task_id, outcome, worker_exit_code, metadata FROM task_runs WHERE id = ?",
            (int(task_run_id),),
        ).fetchone()
        if run is None:
            raise ValueError(f"unknown task run {task_run_id}")
        observed_events = {
            str(row["event_kind"])
            for row in connection.execute(
                "SELECT event_kind FROM worker_run_timeline_events WHERE task_run_id = ?",
                (int(task_run_id),),
            )
        }
        terminal = connection.execute(
            "SELECT worker_exit_kind, worker_exit_code, worker_protocol_state, "
            "task_outcome, end_reason FROM worker_run_terminal_facts WHERE task_run_id = ?",
            (int(task_run_id),),
        ).fetchone()


    try:
        run_metadata = json.loads(run["metadata"] or "{}")
    except (TypeError, json.JSONDecodeError):
        run_metadata = {}
    if not isinstance(run_metadata, dict):
        run_metadata = {}
    worker_runtime = str(run_metadata.get("worker_runtime") or "unknown")
    runtime_eligible = worker_runtime == "hermes"
    first_token_required = runtime_eligible and "first_llm_request" in observed_events
    if runtime_eligible:
        required_events = [
            event for event in _LIFECYCLE_EVENTS
            if event != "first_token" or first_token_required
        ]
        missing_events = [event for event in required_events if event not in observed_events]
        lifecycle_assessment = "pass" if not missing_events else "fail"
    else:
        missing_events = []
        lifecycle_assessment = "not_applicable"
    usage_exact = bool(usage_rows) and usage_task_ids == {str(run["task_id"])}
    terminal_report = dict(terminal) if terminal is not None else {
        "worker_exit_kind": None,
        "worker_exit_code": None,
        "worker_protocol_state": None,
        "task_outcome": None,
        "end_reason": None,
    }
    passed = all((
        langfuse["tcp_http_reachable"],
        langfuse["authenticated_public_api_readable"],
        langfuse["exact_trace_link"],
        not usage_schema_missing,
        usage_exact,
        runtime_eligible,
        not missing_events,
        terminal is not None,
        terminal_report["worker_exit_code"] == 0,
        terminal_report["task_outcome"] == "completed",
    ))
    return {
        "contract_version": LIVE_SMOKE_CONTRACT_VERSION,
        "status": "pass" if passed else "fail",
        "task_run_id": int(task_run_id),
        "worker_runtime": worker_runtime,
        "runtime_eligible": runtime_eligible,
        "schema": {
            "worker_runtime_facts_version": WORKER_RUNTIME_FACTS_SCHEMA_VERSION,
            "usage_correlation_version": USAGE_CORRELATION_SCHEMA_VERSION,
        },
        "langfuse": langfuse,
        "usage_fact": {
            "exact_rows": len(usage_rows),
            "exact_task_match": usage_exact,
            "schema_missing": usage_schema_missing,
        },
        "lifecycle": {
            "assessment": lifecycle_assessment,
            "observed_events": [event for event in _LIFECYCLE_EVENTS if event in observed_events],
            "missing_events": missing_events,
            "first_token_required": first_token_required,
        },
        "terminal": terminal_report,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usage-db", type=Path, default=usage_facts_db_path())
    parser.add_argument("--kanban-db", type=Path, default=kanban_home() / "kanban.db")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--require-exact-run-links",
        action="store_true",
        help="exit non-zero unless every observed recent board run has a complete exact link",
    )
    parser.add_argument(
        "--live-smoke-run-id",
        type=int,
        help="emit a fail-closed receipt for one operator-approved real worker run",
    )
    parser.add_argument(
        "--control-surface-smoke-url",
        help=(
            "run the separately approved Langfuse/dashboard smoke against this "
            "dashboard base URL; reads HERMES_DASHBOARD_TOKEN from the environment"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        if args.control_surface_smoke_url is not None:
            report = build_control_surface_live_smoke(
                dashboard_base_url=args.control_surface_smoke_url,
                dashboard_token=os.environ.get("HERMES_DASHBOARD_TOKEN", ""),
                days=args.days,
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["status"] == "pass" else 3
        if args.live_smoke_run_id is not None:
            report = build_live_smoke_contract(
                task_run_id=args.live_smoke_run_id,
                usage_path=args.usage_db,
                kanban_path=args.kanban_db,
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["status"] == "pass" else 3
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
    if args.require_exact_run_links:
        invariant = report["worker_release_invariant"]
        exact = invariant["exact_run_links"]
        if not (
            invariant["schema_capable"]["available"]
            and exact["denominator_recent_task_runs"] > 0
            and exact["count"] == exact["denominator_recent_task_runs"]
        ):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

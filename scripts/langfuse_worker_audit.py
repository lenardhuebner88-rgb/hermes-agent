#!/usr/bin/env python3
"""Audit worker observability coverage without mutating Hermes or Langfuse."""

from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import inspect
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPCookieProcessor,
    Request,
    build_opener,
)

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
LIVE_SMOKE_CONTRACT_VERSION = 2
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


class LangfuseGenerationProbeIncomplete(RuntimeError):
    """A bounded generation lookup could not establish a complete result."""


@dataclass
class _LangfuseProbeBudget:
    """One shared deadline and scan budget for trace plus generation proof."""

    deadline: float
    pages_remaining: int = 40
    rows_remaining: int = 4_000
    candidate_traces_remaining: int = 5

    @classmethod
    def create(cls, timeout_seconds: float = 4.0) -> "_LangfuseProbeBudget":
        return cls(deadline=time.monotonic() + timeout_seconds)

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LangfuseGenerationProbeIncomplete("deadline")
        return min(4.0, max(0.1, remaining))

    def consume_page(self) -> None:
        self.timeout()
        if self.pages_remaining <= 0:
            raise LangfuseGenerationProbeIncomplete("page_limit")
        self.pages_remaining -= 1

    def consume_rows(self, count: int) -> None:
        self.rows_remaining -= max(0, int(count))
        if self.rows_remaining < 0:
            raise LangfuseGenerationProbeIncomplete("row_limit")

    def consume_candidate_trace(self) -> None:
        if self.candidate_traces_remaining <= 0:
            raise LangfuseGenerationProbeIncomplete("candidate_trace_limit")
        self.candidate_traces_remaining -= 1


_CORRELATION_METADATA_KEYS = frozenset(
    {
        "task_run_id",
        "kanban_run_id",
        "task_id",
        "kanban_task_id",
        "board",
        "kanban_board",
        "chain_id",
        "profile",
        "lane",
        "origin",
    }
)
_LANGFUSE_RESPONSE_MAX_BYTES = 2 * 1024 * 1024


def _short_identifier(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    return text if len(text) <= 12 else f"{text[:8]}…{text[-4:]}"


def _correlation_metadata(value: Any) -> dict[str, Any]:
    """Project arbitrary Langfuse metadata to non-content correlation keys."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in _CORRELATION_METADATA_KEYS
        if key in value
        and isinstance(value[key], (str, int, float, bool))
    }


class _RejectLangfuseRedirectHandler(HTTPRedirectHandler):
    """Never forward an authenticated public-API request across a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LangfusePayloadInvalid("langfuse_url_invalid")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def _configured_langfuse_trace_probe(
    *,
    budget: _LangfuseProbeBudget | None = None,
    request_json: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Read bounded, correlation-only authenticated public trace pages."""
    from hermes_cli.langfuse_scores_export import _credentials

    try:
        host, authorization = _credentials(os.environ)
    except RuntimeError:
        raise LangfuseCredentialsMissing from None
    budget = budget or _LangfuseProbeBudget.create()
    request_json = request_json or _langfuse_public_request
    records: list[dict[str, Any]] = []
    page = 1
    while True:
        budget.consume_page()
        query = urlencode({"page": page, "limit": 100, "tags": "kanban-worker"})
        response = request_json(
            f"{host}/api/public/traces?{query}",
            authorization,
            timeout=budget.timeout(),
            expected_origin=_http_origin(host),
        )
        traces = response.get("data")
        if not isinstance(traces, list):
            raise LangfusePayloadInvalid("langfuse_traces_payload_invalid")
        budget.consume_rows(len(traces))
        for trace in traces:
            if isinstance(trace, dict) and isinstance(trace.get("id"), str):
                records.append(
                    {
                        "id": trace["id"],
                        "metadata": _correlation_metadata(trace.get("metadata")),
                    }
                )
        meta = response.get("meta")
        total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
        try:
            total_pages = int(total_pages) if total_pages is not None else None
        except (TypeError, ValueError):
            total_pages = None
        if (
            not traces
            or (total_pages is not None and page >= total_pages)
            or (total_pages is None and len(traces) < 100)
        ):
            return records
        page += 1


def _langfuse_public_request(
    url: str,
    authorization: str,
    *,
    timeout: float,
    expected_origin: tuple[str, str, int],
    max_bytes: int = _LANGFUSE_RESPONSE_MAX_BYTES,
) -> dict[str, Any]:
    """Read one bounded page without redirects or credential-bearing URLs."""
    if _http_origin(url) != expected_origin:
        raise LangfusePayloadInvalid("langfuse_origin_mismatch")
    request = Request(url)
    request.add_header("Authorization", authorization)
    request.add_header("Accept", "application/json")
    opener = build_opener(_RejectLangfuseRedirectHandler())
    with opener.open(request, timeout=timeout) as response:  # noqa: S310 -- validated configured service
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise LangfuseGenerationProbeIncomplete("response_too_large")
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise LangfusePayloadInvalid("langfuse_generations_payload_invalid")
    return payload


def _configured_langfuse_generation_probe(
    trace_id: str,
    *,
    budget: _LangfuseProbeBudget | None = None,
    request_json: Callable[..., dict[str, Any]] | None = None,
    max_pages: int = 20,
    max_rows: int = 2_000,
    timeout_seconds: float = 4.0,
) -> list[dict[str, Any]]:
    """Read bounded, content-free generation records for one exact trace."""
    from hermes_cli.langfuse_scores_export import _credentials

    try:
        host, authorization = _credentials(os.environ)
    except RuntimeError:
        raise LangfuseCredentialsMissing from None

    request_json = request_json or _langfuse_public_request
    records: list[dict[str, Any]] = []
    if budget is None:
        budget = _LangfuseProbeBudget.create(timeout_seconds)
        budget.pages_remaining = max(1, int(max_pages))
        budget.rows_remaining = max(1, int(max_rows))
    page = 1
    while True:
        budget.consume_page()
        query = urlencode({
            "page": page,
            "limit": 100,
            "type": "GENERATION",
            "traceId": trace_id,
        })
        response = request_json(
            f"{host}/api/public/observations?{query}",
            authorization,
            timeout=budget.timeout(),
            expected_origin=_http_origin(host),
        )
        observations = response.get("data")
        if not isinstance(observations, list):
            raise LangfusePayloadInvalid("langfuse_generations_payload_invalid")
        budget.consume_rows(len(observations))
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observation_id = observation.get("id")
            observation_trace_id = observation.get("traceId")
            if (
                isinstance(observation_id, str)
                and observation_id
                and str(observation_trace_id or "") == trace_id
            ):
                records.append({
                    "id": observation_id,
                    "traceId": observation_trace_id,
                    "metadata": _correlation_metadata(
                        observation.get("metadata")
                    ),
                })
        meta = response.get("meta")
        total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
        try:
            total_pages = int(total_pages) if total_pages is not None else None
        except (TypeError, ValueError):
            total_pages = None
        if (
            not observations
            or (total_pages is not None and page >= total_pages)
            or (total_pages is None and len(observations) < 100)
        ):
            return records
        page += 1


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
        "exact_generation_link": False,
        "generation_id_short": None,
        "generation_trace_match": False,
        "error_type": type(cause).__name__,
    }
    if isinstance(exc, LangfuseCredentialsMissing):
        receipt["credentials_configured"] = False
    elif isinstance(exc, LangfuseGenerationProbeIncomplete):
        receipt["credentials_configured"] = True
        receipt["tcp_http_reachable"] = True
        receipt["authenticated_public_api_readable"] = True
        receipt["reason"] = str(exc)
    elif isinstance(exc, LangfusePayloadInvalid):
        receipt["credentials_configured"] = True
        receipt["tcp_http_reachable"] = True
        receipt["reason"] = "payload_invalid"
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


class DashboardProbeFailed(RuntimeError):
    """An expected authenticated dashboard HTTP/payload failure."""


class _RejectDashboardRedirectHandler(HTTPRedirectHandler):
    """Fail closed instead of forwarding dashboard cookies or tokens."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _authenticated_dashboard_request(
    base_url: str,
    *,
    provider: str,
    username: str,
    password_env: str,
    no_prompt: bool,
    timeout: float = 30.0,
) -> Callable[[str], dict[str, Any]]:
    """Reuse the dashboard's canonical password/cookie smoke authentication."""
    _validated_dashboard_url(base_url, days=1)

    from scripts import smoke_health_status_auth as auth_smoke

    opener = build_opener(
        HTTPCookieProcessor(CookieJar()),
        _RejectDashboardRedirectHandler(),
    )
    try:
        password = auth_smoke._read_password(password_env, no_prompt=no_prompt)
        login = auth_smoke._json_request(
            opener,
            "POST",
            f"{base_url.rstrip('/')}/auth/password-login",
            payload={
                "provider": provider,
                "username": username,
                "password": password,
            },
            timeout=timeout,
        )
        if login.get("ok") is not True:
            raise ValueError("dashboard_authentication_failed")
        control_html = auth_smoke._text_request(
            opener,
            f"{base_url.rstrip('/')}/control",
            timeout=timeout,
        )
    except auth_smoke.SmokeError:
        raise ValueError("dashboard_authentication_failed") from None
    headers: dict[str, str] = {}
    if "window.__HERMES_SESSION_TOKEN__" in control_html:
        headers["X-Hermes-Session-Token"] = auth_smoke._session_token_from_html(
            control_html
        )

    def request_dashboard(url: str) -> dict[str, Any]:
        try:
            return auth_smoke._json_request(
                opener,
                "GET",
                url,
                extra_headers=headers,
                timeout=timeout,
            )
        except auth_smoke.SmokeError:
            raise DashboardProbeFailed("dashboard_request_failed") from None

    return request_dashboard


def _validated_dashboard_url(base_url: str, *, days: int) -> str:
    parsed = urlparse(base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
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


class LangfusePayloadInvalid(RuntimeError):
    """A Langfuse response was reachable but did not satisfy the public API shape."""


def _classified_langfuse_failure(exc: BaseException) -> dict[str, Any] | None:
    """Classify only the known, secret-safe failures from the production client."""
    cause: BaseException = exc
    while isinstance(cause, RuntimeError) and cause.__cause__ is not None:
        cause = cause.__cause__
    receipt: dict[str, Any] = {
        "state": "absent",
        "configured_host_checked": True,
    }
    if isinstance(cause, urllib.error.HTTPError):
        receipt.update(reason="http_error", http_status=cause.code)
    elif isinstance(cause, (urllib.error.URLError, OSError, TimeoutError)):
        receipt["reason"] = "unreachable"
    elif isinstance(
        cause,
        (json.JSONDecodeError, UnicodeDecodeError, LangfusePayloadInvalid),
    ) or (
        cause is exc
        and isinstance(exc, RuntimeError)
        and exc.args == ("Langfuse returned a non-object payload",)
    ):
        receipt["reason"] = "payload_invalid"
    else:
        return None
    return receipt


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
        raise LangfusePayloadInvalid("langfuse_payload_invalid")
    meta = payload.get("meta")
    total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
    if not isinstance(total_pages, int) or total_pages < 0:
        total_pages = None
    return [row for row in rows if isinstance(row, dict)], total_pages


def _scan_langfuse_pages(
    host: str,
    auth_header: str,
    *,
    page_size: int,
    max_pages: int,
    max_rows: int,
    deadline: float,
    from_start_time: str,
    request_json: Callable[..., dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, str | None]:
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        if time.monotonic() >= deadline:
            return rows, page - 1, "deadline"
        current, total_pages = _langfuse_page(
            host,
            auth_header,
            page=page,
            limit=page_size,
            from_start_time=from_start_time,
            request_json=request_json,
        )
        remaining = max_rows - len(rows)
        rows.extend(current[:remaining])
        if len(current) > remaining or len(rows) >= max_rows:
            return rows, page, "row_limit"
        if total_pages is not None and page >= total_pages:
            return rows, page, None
        if total_pages is None and len(current) < page_size:
            return rows, page, None
    return rows, max_pages, "page_limit"


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
    days: int = 7,
    warm_calls: int = 10,
    page_size: int = 100,
    env: Mapping[str, str] | None = None,
    langfuse_request: Callable[..., dict[str, Any]] | None = None,
    dashboard_request: Callable[[str], dict[str, Any]] | None = None,
    scan_deadline_seconds: float = 30.0,
    scan_max_rows: int = 100_000,
) -> dict[str, Any]:
    """Run the separately approved, secret-safe Langfuse/dashboard live smoke."""
    from plugins.kanban.dashboard import langfuse_read_model

    if warm_calls < 5 or dashboard_request is None:
        raise ValueError("an authenticated dashboard request and at least five warm calls are required")
    if days < 1 or page_size < 1 or scan_deadline_seconds <= 0 or scan_max_rows < 1:
        raise ValueError("days, page size, scan deadline, and row limit must be positive")
    dashboard_url = _validated_dashboard_url(dashboard_base_url, days=days)
    request_json = langfuse_request or langfuse_read_model._request_json
    dashboard_probe = dashboard_request
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
            full_rows, full_pages, full_reason = _scan_langfuse_pages(
                host,
                authorization,
                page_size=page_size,
                max_pages=1_000,
                max_rows=scan_max_rows,
                deadline=time.monotonic() + scan_deadline_seconds,
                from_start_time=from_start_time,
                request_json=request_json,
            )
            limited_rows, limited_pages, _ = _scan_langfuse_pages(
                host,
                authorization,
                page_size=1,
                max_pages=1,
                max_rows=1,
                deadline=time.monotonic() + scan_deadline_seconds,
                from_start_time=from_start_time,
                request_json=request_json,
            )
            report["langfuse"] = {
                "state": "fresh" if full_reason is None else "partial",
                "configured_host_checked": True,
                "public_api_page": {
                    "authenticated": True,
                    "rows": len(first_page),
                },
                "full_scan": _scan_receipt(
                    full_rows,
                    days=days,
                    pages=full_pages,
                    complete=full_reason is None,
                    reason=full_reason,
                ),
                "limited_scan": _scan_receipt(
                    limited_rows,
                    days=days,
                    pages=limited_pages,
                    complete=False,
                    reason="intentional_limit",
                ),
            }
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            LangfusePayloadInvalid,
            RuntimeError,
        ) as exc:
            classified = _classified_langfuse_failure(exc)
            if classified is None:
                raise
            report["langfuse"] = classified

    try:
        dashboard_probe(dashboard_url)
        wall_samples: list[float] = []
        cpu_samples: list[float] = []
        cache_ages: list[int | float | None] = []
        payload: dict[str, Any] = {}
        for _ in range(warm_calls):
            wall_started = time.perf_counter()
            cpu_started = time.process_time()
            payload = dashboard_probe(dashboard_url)
            cpu_samples.append((time.process_time() - cpu_started) * 1_000)
            wall_samples.append((time.perf_counter() - wall_started) * 1_000)
            measured_usage = (
                payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            )
            measured_cache = (
                measured_usage.get("cache")
                if isinstance(measured_usage.get("cache"), dict)
                else {}
            )
            cache_ages.append(measured_cache.get("age_seconds"))
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        langfuse = (
            payload.get("langfuse") if isinstance(payload.get("langfuse"), dict) else {}
        )
        summary = usage.get("summary") if isinstance(usage.get("summary"), dict) else {}
        mean_wall = statistics.fmean(wall_samples)
        maximum_wall = max(wall_samples)
        mean_cpu = statistics.fmean(cpu_samples)
        fact_rows = summary.get("fact_rows")
        usage_acceptable = usage.get("state") == "fresh" and isinstance(fact_rows, int)
        report["dashboard"] = {
            "authenticated": True,
            "warm_calls": warm_calls,
            "sample_count": len(wall_samples),
            "wall_ms": {
                "median": statistics.median(wall_samples),
                "maximum": maximum_wall,
                "mean": mean_wall,
            },
            "client_cpu_ms": {
                "median": statistics.median(cpu_samples),
                "maximum": max(cpu_samples),
                "mean": mean_cpu,
            },
            "fact_rows": fact_rows,
            "cache_age_seconds": cache_ages,
            "usage_state": usage.get("state"),
            "langfuse_state": langfuse.get("state"),
            "budget": {
                "wall_maximum_limit_ms": 300,
                "server_cpu_budget": "not_observable_over_http",
                "server_cpu_budget_test": "LRM-4 in-process",
                "passed": maximum_wall < 300,
            },
            "usage_acceptable": usage_acceptable,
        }
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, DashboardProbeFailed):
        report["dashboard"] = {
            "authenticated": False,
            "state": "unreachable",
        }

    langfuse_ok = report["langfuse"].get("state") == "fresh"
    dashboard_ok = bool(
        report["dashboard"].get("budget", {}).get("passed")
        and report["dashboard"].get("usage_acceptable")
    )
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
    trace_probe: Callable[..., list[dict[str, Any]]] = _configured_langfuse_trace_probe,
    generation_probe: Callable[..., list[dict[str, Any]]] = _configured_langfuse_generation_probe,
) -> dict[str, Any]:
    """Build a fail-closed, content-free receipt for one approved worker run."""
    run_id = str(int(task_run_id))
    with closing(_read_only(kanban_path)) as connection:
        run = connection.execute(
            "SELECT task_id, outcome, worker_exit_code, metadata "
            "FROM task_runs WHERE id = ?",
            (int(task_run_id),),
        ).fetchone()
        if run is None:
            raise ValueError(f"unknown task run {task_run_id}")
        observed_events = {
            str(row["event_kind"])
            for row in connection.execute(
                "SELECT event_kind FROM worker_run_timeline_events "
                "WHERE task_run_id = ?",
                (int(task_run_id),),
            )
        }
        terminal = connection.execute(
            "SELECT worker_exit_kind, worker_exit_code, worker_protocol_state, "
            "task_outcome, end_reason, board "
            "FROM worker_run_terminal_facts WHERE task_run_id = ?",
            (int(task_run_id),),
        ).fetchone()
        locator = None
        if terminal is None or not terminal["board"]:
            locator_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='worker_run_runtime_locators'"
            ).fetchone()
            if locator_table is not None:
                locator = connection.execute(
                    "SELECT board FROM worker_run_runtime_locators "
                    "WHERE task_run_id = ?",
                    (int(task_run_id),),
                ).fetchone()
    expected_task_id = str(run["task_id"])
    expected_board = str(
        (terminal["board"] if terminal is not None else None)
        or (locator["board"] if locator is not None else None)
        or ""
    )

    def metadata_matches(metadata: Mapping[str, Any]) -> bool:
        observed_run = metadata.get(
            "task_run_id", metadata.get("kanban_run_id")
        )
        observed_task = metadata.get(
            "task_id", metadata.get("kanban_task_id")
        )
        observed_board = metadata.get(
            "board", metadata.get("kanban_board")
        )
        return (
            str(observed_run or "") == run_id
            and str(observed_task or "") == expected_task_id
            and bool(expected_board)
            and str(observed_board or "") == expected_board
        )

    exact_trace: dict[str, Any] | None = None
    exact_generation: dict[str, Any] | None = None
    try:
        budget = _LangfuseProbeBudget.create()
        def invoke_probe(probe: Callable[..., Any], *args: Any) -> Any:
            parameters = inspect.signature(probe).parameters.values()
            accepts_budget = any(
                parameter.name == "budget"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            return (
                probe(*args, budget=budget)
                if accepts_budget
                else probe(*args)
            )

        exact_traces: list[dict[str, Any]] = []
        for trace in invoke_probe(trace_probe):
            metadata = _trace_metadata(trace)
            if (
                metadata_matches(metadata)
                and isinstance(trace.get("id"), str)
                and trace["id"]
            ):
                budget.consume_candidate_trace()
                exact_traces.append(trace)
        for candidate in exact_traces:
            candidate_trace_id = candidate["id"]
            for generation in invoke_probe(
                generation_probe,
                candidate_trace_id,
            ):
                metadata = _trace_metadata(generation)
                if (
                    metadata_matches(metadata)
                    and isinstance(generation.get("id"), str)
                    and generation["id"]
                    and str(generation.get("traceId") or "") == candidate_trace_id
                ):
                    exact_trace = candidate
                    exact_generation = generation
                    break
            if exact_generation is not None:
                break
        if exact_trace is None and exact_traces:
            exact_trace = exact_traces[0]
        trace_id = exact_trace.get("id") if exact_trace is not None else None
        generation_trace_id = (
            exact_generation.get("traceId")
            if exact_generation is not None
            else None
        )
        langfuse: dict[str, Any] = {
            "credentials_configured": True,
            "tcp_http_reachable": True,
            "authenticated_public_api_readable": True,
            "exact_trace_link": exact_trace is not None,
            "exact_task_board_link": exact_trace is not None,
            "trace_id_short": _short_identifier(trace_id),
            "exact_generation_link": exact_generation is not None,
            "generation_id_short": _short_identifier(
                exact_generation.get("id")
                if exact_generation is not None
                else None
            ),
            "generation_trace_match": bool(
                trace_id
                and generation_trace_id
                and str(trace_id) == str(generation_trace_id)
            ),
        }
    except Exception as exc:  # fail closed; never serialize a possibly sensitive message
        langfuse = _langfuse_failure_receipt(exc)

    with closing(_read_only(usage_path)) as connection:
        usage_columns = _columns(connection, "run_usage_facts")
        usage_schema_missing = sorted(
            {"task_run_id", "task_id", "board", "captured_at"} - usage_columns
        )
        usage_rows = [] if usage_schema_missing else connection.execute(
            "SELECT task_id, board FROM run_usage_facts "
            "WHERE task_run_id = ? AND task_id = ? AND board = ?",
            (run_id, expected_task_id, expected_board),
        ).fetchall()

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
    usage_exact = bool(usage_rows)
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
        langfuse.get("exact_task_board_link"),
        langfuse["exact_generation_link"],
        langfuse["generation_trace_match"],
        not usage_schema_missing,
        usage_exact,
        runtime_eligible,
        not missing_events,
        terminal is not None,
        terminal_report["worker_exit_kind"] == "exited",
        terminal_report["worker_exit_code"] == 0,
        terminal_report["worker_protocol_state"] == "completed",
        terminal_report["task_outcome"] == "completed",
        terminal_report["end_reason"] == "kanban_complete",
    ))
    return {
        "contract_version": LIVE_SMOKE_CONTRACT_VERSION,
        "status": "pass" if passed else "fail",
        "task_run_id": int(task_run_id),
        "task_id": expected_task_id,
        "board": expected_board or None,
        "worker_runtime": worker_runtime,
        "runtime_eligible": runtime_eligible,
        "schema": {
            "worker_runtime_facts_version": WORKER_RUNTIME_FACTS_SCHEMA_VERSION,
            "usage_correlation_version": USAGE_CORRELATION_SCHEMA_VERSION,
        },
        "langfuse": langfuse,
        "usage_fact": {
            "exact_rows": len(usage_rows),
            "exact_task_board_match": usage_exact,
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
            "loopback dashboard base URL using password login and an in-memory cookie"
        ),
    )
    parser.add_argument(
        "--dashboard-auth-provider",
        default=os.environ.get("HERMES_DASHBOARD_AUTH_PROVIDER", "basic"),
    )
    parser.add_argument(
        "--dashboard-username",
        default=os.environ.get("HERMES_DASHBOARD_USERNAME", ""),
    )
    parser.add_argument("--dashboard-password-env", default="HERMES_DASHBOARD_PASSWORD")
    parser.add_argument(
        "--warm-calls",
        type=int,
        default=10,
        help="authenticated measured reads after warm-up (minimum 5, default 10)",
    )
    parser.add_argument("--no-prompt", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        if args.control_surface_smoke_url is not None:
            if args.warm_calls < 5:
                raise ValueError("warm_calls_below_minimum")
            _validated_dashboard_url(
                args.control_surface_smoke_url,
                days=args.days,
            )
            dashboard_probe = _authenticated_dashboard_request(
                args.control_surface_smoke_url,
                provider=args.dashboard_auth_provider,
                username=args.dashboard_username,
                password_env=args.dashboard_password_env,
                no_prompt=args.no_prompt,
            )
            report = build_control_surface_live_smoke(
                dashboard_base_url=args.control_surface_smoke_url,
                days=args.days,
                warm_calls=args.warm_calls,
                dashboard_request=dashboard_probe,
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

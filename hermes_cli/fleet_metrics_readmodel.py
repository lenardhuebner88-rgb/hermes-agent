"""Read-only Fleet metrics contract for reliability, value, and trust.

The projection composes the usage-facts ledger with exact Kanban runtime facts.
Every rate carries its numerator and denominator; unsupported source metrics
are marked not-applicable instead of depressing coverage or becoming zero.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote

from hermes_cli.telemetry_contracts import (
    metric_is_eligible,
    telemetry_contract,
    telemetry_contracts,
)
from hermes_cli.usage_facts_readmodel import build_attributed_usage_payload

CONTRACT_VERSION = "fleet-metrics.v1"
DEFAULT_SENTINEL_STATUS_PATH = Path(
    "/mnt/data/hermes-observability/sentinel-status.json"
)


def sentinel_status_path_default() -> Path:
    """Resolve the sentinel status file, overridable like the other stores.

    Without the override every caller — including tests — reads the live
    host file, so a real sentinel run silently changes what a test observes.
    """
    override = os.environ.get("HERMES_SENTINEL_STATUS_PATH")
    return Path(override) if override else DEFAULT_SENTINEL_STATUS_PATH
TERMINAL_TASK_STATUSES = frozenset(
    {"done", "completed", "failed", "blocked", "canceled", "archived"}
)

_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def build_fleet_metrics_payload(
    usage_facts_path: str | Path,
    kanban_path: str | Path,
    *,
    days: int = 7,
    generated_at: str | None = None,
    origins: Sequence[str] | None = None,
    bucket_limit: int = 100,
    sentinel_status_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the additive cockpit backend contract without mutating inputs."""
    safe_days = max(1, min(int(days), 30))
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    observed_at = _parse_datetime(generated) or datetime.now(timezone.utc)
    captured_from = (observed_at - timedelta(days=safe_days)).isoformat()

    usage = build_attributed_usage_payload(
        usage_facts_path,
        origins=origins,
        captured_from=captured_from,
        generated_at=generated,
        limit=bucket_limit,
        include_task_cost_population=True,
    )
    with _read_only_connection(Path(usage_facts_path)) as connection:
        provider_coverage = _provider_model_coverage(
            connection,
            origins=origins,
            captured_from=captured_from,
        )
        source_freshness = _usage_source_freshness(
            connection,
            origins=origins,
            captured_from=captured_from,
            observed_at=observed_at,
        )

    # The attributed readmodel constructs this only for fleet's full-population
    # alert calculation.  It must never leak into the display-limited API body.
    task_cost_population = usage["tasks"].pop("_cost_population", [])
    comparison_eligibility = _comparison_eligibility(
        usage,
        task_cost_population=task_cost_population,
    )

    kanban = Path(kanban_path)
    kanban_readable = kanban.is_file()
    if kanban_readable:
        with _read_only_connection(kanban) as connection:
            reviews = _review_projection(
                connection,
                cutoff=int((observed_at - timedelta(days=safe_days)).timestamp()),
            )
            retries = _retry_projection(
                connection,
                cutoff=int((observed_at - timedelta(days=safe_days)).timestamp()),
            )
            queue = _queue_projection(
                connection,
                now_seconds=int(observed_at.timestamp()),
                cutoff_ms=int(
                    (observed_at - timedelta(days=safe_days)).timestamp() * 1000
                ),
            )
    else:
        reviews = _unavailable("kanban_database_unavailable")
        retries = _unavailable("kanban_database_unavailable")
        queue = _unavailable("kanban_database_unavailable")

    sentinel = _sentinel_projection(
        Path(sentinel_status_path)
        if sentinel_status_path is not None
        else sentinel_status_path_default(),
        observed_at=observed_at,
    )
    alerts = {
        "data_freshness": _freshness_alert(
            source_freshness,
        ),
        "sentinel": _sentinel_alert(sentinel),
        "retry_spike": _retry_alert(retries),
        "queue_congestion": _queue_alert(queue),
        "cost_outlier": _cost_outlier_alert(
            task_cost_population,
            comparison_eligibility=comparison_eligibility,
        ),
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": generated,
        "window_days": safe_days,
        "telemetry_contracts": telemetry_contracts(),
        "usage": usage,
        "provider_model_coverage": provider_coverage,
        "source_freshness": source_freshness,
        "comparison_eligibility": comparison_eligibility,
        "reliability": {
            "retries": retries,
            "queue": queue,
            "sentinel": sentinel,
        },
        "quality": {"reviews": reviews},
        "alerts": alerts,
    }


def _usage_source_freshness(
    connection: sqlite3.Connection,
    *,
    origins: Sequence[str] | None,
    captured_from: str,
    observed_at: datetime,
) -> dict[str, Any]:
    """Describe freshness per origin; a recent source cannot mask a stale one."""

    conditions: list[str] = []
    # Freshness is about the most recent fact for an origin, not merely facts
    # that happened to fall inside the display window.  A source that stopped
    # before the window remains stale instead of disappearing from the alert.
    params: list[Any] = [captured_from]
    selected_origins = sorted(
        {str(origin).strip() for origin in origins or () if str(origin).strip()}
    )
    if selected_origins:
        placeholders = ", ".join("?" for _ in selected_origins)
        conditions.append(f"origin IN ({placeholders})")
        params.extend(selected_origins)
    rows = connection.execute(
        f"""
        SELECT origin,
               SUM(CASE WHEN captured_at >= ? THEN 1 ELSE 0 END) AS fact_rows,
               MAX(captured_at) AS latest_captured_at
          FROM run_usage_facts
         {"WHERE " + " AND ".join(conditions) if conditions else ""}
         GROUP BY origin
         ORDER BY origin
        """,
        tuple(params),
    ).fetchall()
    threshold = 2 * 60 * 60
    sources: list[dict[str, Any]] = []
    for row in rows:
        latest = row["latest_captured_at"]
        parsed = _parse_datetime(latest)
        if parsed is None:
            sources.append(
                {
                    "origin": str(row["origin"]),
                    "fact_rows": int(row["fact_rows"] or 0),
                    "latest_captured_at": latest,
                    "age_seconds": None,
                    "threshold_seconds": threshold,
                    "status": "unknown",
                    "unknown_reason": "latest_capture_unknown",
                }
            )
            continue
        age = max(0, int((observed_at - parsed).total_seconds()))
        sources.append(
            {
                "origin": str(row["origin"]),
                "fact_rows": int(row["fact_rows"] or 0),
                "latest_captured_at": latest,
                "age_seconds": age,
                "threshold_seconds": threshold,
                "status": "warning" if age > threshold else "ok",
                "unknown_reason": None,
            }
        )
    return {
        "window_basis": "run_usage_facts.captured_at",
        "sources": sources,
        "source_count": len(sources),
        "threshold_seconds": threshold,
    }


def _provider_model_coverage(
    connection: sqlite3.Connection,
    *,
    origins: Sequence[str] | None,
    captured_from: str,
) -> dict[str, Any]:
    conditions = ["captured_at >= ?"]
    params: list[Any] = [captured_from]
    selected_origins = sorted(
        {str(origin).strip() for origin in origins or () if str(origin).strip()}
    )
    if selected_origins:
        placeholders = ", ".join("?" for _ in selected_origins)
        conditions.append(f"origin IN ({placeholders})")
        params.extend(selected_origins)
    token_present = "(" + " OR ".join(
        f"{column} IS NOT NULL" for column in _TOKEN_COLUMNS
    ) + ")"
    rows = connection.execute(
        f"""
        SELECT origin, provider, model,
               COUNT(*) AS fact_rows,
               SUM(CASE WHEN {token_present} THEN 1 ELSE 0 END) AS token_rows,
               COUNT(model) AS model_rows,
               COUNT(duration_ms) AS duration_rows,
               COUNT(first_token_ms) AS ttft_rows,
               MAX(captured_at) AS latest_captured_at
          FROM run_usage_facts
         WHERE {" AND ".join(conditions)}
         GROUP BY origin, provider, model
         ORDER BY origin, provider, model
        """,
        tuple(params),
    ).fetchall()

    groups: list[dict[str, Any]] = []
    totals = {
        "fact_rows": 0,
        "tokens": [0, 0],
        "model_all": [0, 0],
        "model_eligible": [0, 0],
        "duration_all": [0, 0],
        "duration_eligible": [0, 0],
        "ttft_all": [0, 0],
        "ttft_eligible": [0, 0],
    }
    for row in rows:
        origin = str(row["origin"])
        fact_rows = int(row["fact_rows"] or 0)
        observed = {
            "tokens": int(row["token_rows"] or 0),
            "model": int(row["model_rows"] or 0),
            "duration": int(row["duration_rows"] or 0),
            "ttft": int(row["ttft_rows"] or 0),
        }
        metric_coverage = {
            metric: _metric_coverage(
                origin,
                metric,
                observed_rows=observed[metric],
                fact_rows=fact_rows,
            )
            for metric in ("tokens", "model", "duration", "ttft")
        }
        groups.append(
            {
                "key": {
                    "origin": origin,
                    "provider": row["provider"],
                    "model": row["model"],
                },
                "fact_rows": fact_rows,
                "latest_captured_at": row["latest_captured_at"],
                "source_contract": {
                    metric: telemetry_contract(origin)[metric]
                    for metric in ("tokens", "model", "duration", "ttft")
                },
                "coverage": metric_coverage,
            }
        )
        totals["fact_rows"] += fact_rows
        totals["tokens"][0] += observed["tokens"]
        totals["tokens"][1] += fact_rows
        totals["model_all"][0] += observed["model"]
        totals["model_all"][1] += fact_rows
        if metric_is_eligible(origin, "model"):
            totals["model_eligible"][0] += observed["model"]
            totals["model_eligible"][1] += fact_rows
        for metric in ("duration", "ttft"):
            totals[f"{metric}_all"][0] += observed[metric]
            totals[f"{metric}_all"][1] += fact_rows
            if metric_is_eligible(origin, metric):
                totals[f"{metric}_eligible"][0] += observed[metric]
                totals[f"{metric}_eligible"][1] += fact_rows

    return {
        "fact_rows": totals["fact_rows"],
        "coverage": {
            "tokens": _ratio(*totals["tokens"]),
            "model": {
                "all_sources": _ratio(*totals["model_all"]),
                "eligible_sources": _ratio(*totals["model_eligible"]),
            },
            "duration": {
                "all_sources": _ratio(*totals["duration_all"]),
                "eligible_sources": _ratio(*totals["duration_eligible"]),
            },
            "ttft": {
                "all_sources": _ratio(*totals["ttft_all"]),
                "eligible_sources": _ratio(*totals["ttft_eligible"]),
            },
        },
        "groups": groups,
    }


def _metric_coverage(
    origin: str,
    metric: str,
    *,
    observed_rows: int,
    fact_rows: int,
) -> dict[str, Any]:
    eligible = metric_is_eligible(origin, metric)
    return {
        "all_sources": _ratio(observed_rows, fact_rows),
        "eligible_sources": (
            _ratio(observed_rows, fact_rows)
            if eligible
            else {
                "observed_rows": observed_rows,
                "denominator_rows": 0,
                "ratio": None,
                "status": "not_applicable",
            }
        ),
    }


def _review_projection(
    connection: sqlite3.Connection,
    *,
    cutoff: int,
) -> dict[str, Any]:
    if not _table_exists(connection, "scores"):
        return _unavailable("scores_table_unavailable")
    rows = connection.execute(
        """
        SELECT s.task_id, s.value, s.created_at, s.id,
               COALESCE(
                   CASE WHEN json_valid(tr.metadata)
                        THEN json_extract(
                            tr.metadata, '$.plan_spec_chain_root'
                        ) END,
                   CASE WHEN json_valid(tr.metadata)
                        THEN json_extract(tr.metadata, '$.chain_root') END,
                   CASE WHEN json_valid(tr.metadata)
                        THEN json_extract(tr.metadata, '$.chain_id') END,
                   s.task_id
               ) AS chain_id
          FROM scores s
          LEFT JOIN task_runs tr ON tr.id = s.run_id
         WHERE s.name = 'review_verdict' AND s.created_at >= ?
         ORDER BY s.created_at, s.id
        """,
        (cutoff,),
    ).fetchall()
    verdicts = [
        {
            "task_id": str(row["task_id"]),
            "chain_id": str(row["chain_id"] or row["task_id"]),
            "approved": float(row["value"] or 0) >= 0.5,
            "created_at": int(row["created_at"]),
            "score_id": int(row["id"]),
        }
        for row in rows
        if row["value"] is not None
    ]
    approvals = sum(1 for item in verdicts if item["approved"])
    changes = len(verdicts) - approvals
    tasks = {item["task_id"] for item in verdicts}
    final_by_task: dict[str, bool] = {}
    rework_by_task: dict[str, int] = {}
    chain_builders: dict[str, dict[str, Any]] = {}
    for item in verdicts:
        final_by_task[item["task_id"]] = item["approved"]
        if not item["approved"]:
            rework_by_task[item["task_id"]] = (
                rework_by_task.get(item["task_id"], 0) + 1
            )
        chain = chain_builders.setdefault(
            item["chain_id"],
            {"chain_id": item["chain_id"], "approvals": 0, "request_changes": 0},
        )
        key = "approvals" if item["approved"] else "request_changes"
        chain[key] += 1
    chains = sorted(
        (
            {
                **item,
                "verdict_rounds": item["approvals"] + item["request_changes"],
            }
            for item in chain_builders.values()
        ),
        key=lambda item: (
            -item["request_changes"],
            -item["verdict_rounds"],
            item["chain_id"],
        ),
    )
    return {
        "available": True,
        "verdict_rounds": len(verdicts),
        "reviewed_tasks": len(tasks),
        "approvals": approvals,
        "request_changes": changes,
        "approval_rate": approvals / len(verdicts) if verdicts else None,
        "approval_rate_unit": "verdict_rounds",
        "final_approved_tasks": sum(final_by_task.values()),
        "final_request_changes_tasks": (
            len(final_by_task) - sum(final_by_task.values())
        ),
        "final_task_approval_rate": (
            sum(final_by_task.values()) / len(final_by_task)
            if final_by_task
            else None
        ),
        "tasks_with_rework": len(rework_by_task),
        "rework_rounds": sum(rework_by_task.values()),
        "rework_round_definition": "request_changes_verdict_rows",
        "mean_rework_rounds_per_reviewed_task": (
            sum(rework_by_task.values()) / len(tasks) if tasks else None
        ),
        "coverage": {
            "observed_verdicts": len(verdicts),
            "denominator": None,
            "status": "observed_only",
            "reason": "no_stable_required-review_denominator",
        },
        "chains": chains[:100],
        "chains_truncated": len(chains) > 100,
        "source": "scores.name=review_verdict",
        "window_basis": "scores.created_at",
        "latest_verdict_at": (
            datetime.fromtimestamp(
                max(item["created_at"] for item in verdicts),
                tz=timezone.utc,
            ).isoformat()
            if verdicts
            else None
        ),
    }


def _retry_projection(
    connection: sqlite3.Connection,
    *,
    cutoff: int,
) -> dict[str, Any]:
    if not _table_exists(connection, "worker_run_retry_links"):
        return _retry_unavailable("retry_links_table_unavailable")
    if not _table_exists(connection, "worker_run_timeline_events"):
        return _retry_unavailable("retry_instrumentation_cohort_unavailable")
    all_runs = int(
        connection.execute(
            "SELECT COUNT(*) FROM task_runs WHERE started_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    )
    instrumented_runs = int(
        connection.execute(
            """
            SELECT COUNT(*)
              FROM task_runs runs
             WHERE runs.started_at >= ?
               AND (
                   EXISTS (
                       SELECT 1
                         FROM worker_run_timeline_events timeline
                        WHERE timeline.task_run_id = runs.id
                   )
                   OR EXISTS (
                       SELECT 1
                         FROM worker_run_retry_links retry_link
                        WHERE retry_link.task_run_id = runs.id
                   )
               )
            """,
            (cutoff,),
        ).fetchone()[0]
    )
    rows = connection.execute(
        """
        SELECT links.retry_class, COUNT(*) AS retry_runs,
               COUNT(DISTINCT links.task_id) AS tasks
          FROM worker_run_retry_links links
          JOIN task_runs runs ON runs.id = links.task_run_id
         WHERE runs.started_at >= ?
         GROUP BY links.retry_class
         ORDER BY links.retry_class
        """,
        (cutoff,),
    ).fetchall()
    by_class = {
        str(row["retry_class"]): {
            "retry_runs": int(row["retry_runs"]),
            "tasks": int(row["tasks"]),
        }
        for row in rows
    }
    retry_runs = sum(item["retry_runs"] for item in by_class.values())
    return {
        "available": True,
        # ``runs`` is retained as the rate denominator for v1 consumers.  The
        # all-history count is now explicit so a pre-instrumentation backlog
        # cannot dilute a current retry rate.
        "runs": instrumented_runs,
        "denominator_runs": instrumented_runs,
        "all_runs": all_runs,
        "retry_runs": retry_runs,
        "retry_rate": (
            retry_runs / instrumented_runs if instrumented_runs else None
        ),
        "by_class": by_class,
        "cohort_source": (
            "task_runs with worker_run_timeline_events or worker_run_retry_links"
        ),
        "instrumentation_adoption": {
            "observed_runs": instrumented_runs,
            "denominator_runs": all_runs,
            "ratio": instrumented_runs / all_runs if all_runs else None,
            "status": (
                "complete"
                if all_runs > 0 and instrumented_runs == all_runs
                else "partial"
                if instrumented_runs > 0
                else "unknown"
            ),
            "reason": (
                None
                if all_runs > 0 and instrumented_runs == all_runs
                else "pre_instrumentation_or_uninstrumented_runs_present"
                if instrumented_runs > 0
                else "no_instrumented_runs_in_window"
            ),
        },
        "source": "worker_run_retry_links",
    }


def _retry_unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "runs": None,
        "denominator_runs": None,
        "all_runs": None,
        "retry_runs": None,
        "retry_rate": None,
        "by_class": {},
        "cohort_source": None,
        "instrumentation_adoption": {
            "observed_runs": None,
            "denominator_runs": None,
            "ratio": None,
            "status": "unknown",
            "reason": reason,
        },
        "source": "worker_run_retry_links",
    }


def _queue_projection(
    connection: sqlite3.Connection,
    *,
    now_seconds: int,
    cutoff_ms: int,
) -> dict[str, Any]:
    if not _table_exists(connection, "tasks"):
        return _unavailable("tasks_table_unavailable")
    columns = _columns(connection, "tasks")
    pending_statuses = ("ready", "scheduled")
    freigabe_filter = (
        "AND COALESCE(t.freigabe, '') != 'operator'"
        if "freigabe" in columns
        else ""
    )
    due_filter = (
        "AND (t.due_at IS NULL OR t.due_at <= ?)"
        if "due_at" in columns
        else ""
    )
    current_run_filter = (
        "AND t.current_run_id IS NULL" if "current_run_id" in columns else ""
    )
    params: list[Any] = [*pending_statuses]
    if due_filter:
        params.append(now_seconds)
    terminal = sorted(TERMINAL_TASK_STATUSES)
    terminal_placeholders = ", ".join("?" for _ in terminal)
    params.extend(terminal)
    backlog = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
              FROM tasks t
             WHERE t.status IN (?, ?)
               {current_run_filter}
               {freigabe_filter}
               {due_filter}
               AND NOT EXISTS (
                   SELECT 1
                     FROM task_links links
                     JOIN tasks parent ON parent.id = links.parent_id
                    WHERE links.child_id = t.id
                      AND parent.status NOT IN ({terminal_placeholders})
               )
            """,
            tuple(params),
        ).fetchone()[0]
    )
    waits: list[int] = []
    queued_runs = 0
    if _table_exists(connection, "worker_run_timeline_events"):
        rows = connection.execute(
            """
            SELECT task_run_id,
                   MAX(CASE WHEN event_kind='queued' THEN observed_at_ms END)
                       AS queued_at,
                   MAX(CASE WHEN event_kind='claimed' THEN observed_at_ms END)
                       AS claimed_at
              FROM worker_run_timeline_events
             GROUP BY task_run_id
            HAVING queued_at >= ?
            """,
            (cutoff_ms,),
        ).fetchall()
        queued_runs = len(rows)
        waits = [
            int(row["claimed_at"]) - int(row["queued_at"])
            for row in rows
            if row["claimed_at"] is not None
            and int(row["claimed_at"]) >= int(row["queued_at"])
        ]
    return {
        "available": True,
        "eligible_backlog": backlog,
        "queue_wait_ms": {
            "p50": _percentile(waits, 0.50),
            "p95": _percentile(waits, 0.95),
            "observed_runs": len(waits),
            "queued_runs": queued_runs,
            "coverage": _ratio(len(waits), queued_runs),
        },
        "source": "tasks plus worker_run_timeline_events",
    }


def _sentinel_projection(path: Path, *, observed_at: datetime) -> dict[str, Any]:
    if not path.is_file():
        return _unavailable("sentinel_status_unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _unavailable("sentinel_status_invalid")
    if not isinstance(payload, Mapping):
        return _unavailable("sentinel_status_invalid")
    timestamp = payload.get("last_success_at") or payload.get("checked_at")
    parsed = _parse_datetime(timestamp)
    return {
        "available": True,
        "status": str(payload.get("status") or "unknown"),
        "last_success_at": payload.get("last_success_at"),
        "checked_at": payload.get("checked_at"),
        "age_seconds": (
            max(0, int((observed_at - parsed).total_seconds()))
            if parsed is not None
            else None
        ),
        "task_run_id": payload.get("task_run_id"),
        "contract_version": payload.get("contract_version"),
    }


def _freshness_alert(source_freshness: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize source freshness without allowing a recent origin to mask one."""

    sources = list(source_freshness.get("sources") or [])
    threshold = int(source_freshness.get("threshold_seconds") or 0)
    if not sources:
        result = _alert(
            "unknown",
            None,
            0,
            threshold,
            "no_usage_origins_in_window",
        )
        result["sources"] = []
        return result
    statuses = {str(source.get("status") or "unknown") for source in sources}
    # Unknown remains visible, but an observed stale source is still surfaced as
    # a warning rather than being hidden behind a fresh Hermes worker.
    status = (
        "warning"
        if "warning" in statuses
        else "unknown"
        if "unknown" in statuses
        else "ok"
    )
    reason = (
        "one_or_more_usage_origins_stale"
        if status == "warning"
        else "one_or_more_usage_origins_unknown"
        if status == "unknown"
        else "all_usage_origins_within_freshness_budget"
    )
    result = _alert(
        status,
        {
            "fresh_sources": sum(
                1 for source in sources if source.get("status") == "ok"
            ),
            "stale_sources": sum(
                1 for source in sources if source.get("status") == "warning"
            ),
            "unknown_sources": sum(
                1 for source in sources if source.get("status") == "unknown"
            ),
        },
        len(sources),
        threshold,
        reason,
    )
    result["sources"] = sources
    return result


def _sentinel_alert(sentinel: Mapping[str, Any]) -> dict[str, Any]:
    threshold = 8 * 24 * 60 * 60
    if not sentinel.get("available"):
        return _alert(
            "unknown",
            None,
            0,
            threshold,
            str(sentinel.get("reason") or "sentinel_unknown"),
        )
    age = sentinel.get("age_seconds")
    if age is None:
        return _alert("unknown", None, 0, threshold, "sentinel_time_unknown")
    failed = sentinel.get("status") != "passed"
    stale = int(age) > threshold
    return _alert(
        "critical" if failed else "warning" if stale else "ok",
        age,
        1,
        threshold,
        "sentinel_failed"
        if failed
        else "sentinel_stale"
        if stale
        else "sentinel_current",
    )


def _retry_alert(retries: Mapping[str, Any]) -> dict[str, Any]:
    threshold = 0.15
    runs = int(retries.get("runs") or 0)
    rate = retries.get("retry_rate")
    if not retries.get("available") or runs < 10 or rate is None:
        return _alert(
            "unknown",
            rate,
            runs,
            threshold,
            "minimum_10_runs_required",
        )
    return _alert(
        "warning" if float(rate) > threshold else "ok",
        rate,
        runs,
        threshold,
        "retry_rate_above_threshold"
        if float(rate) > threshold
        else "retry_rate_normal",
    )


def _queue_alert(queue: Mapping[str, Any]) -> dict[str, Any]:
    backlog_threshold = 10
    wait_threshold_ms = 15 * 60 * 1000
    if not queue.get("available"):
        return _alert(
            "unknown",
            None,
            0,
            {
                "eligible_backlog": backlog_threshold,
                "p95_wait_ms": wait_threshold_ms,
            },
            str(queue.get("reason") or "queue_unknown"),
        )
    backlog = int(queue.get("eligible_backlog") or 0)
    waits = queue.get("queue_wait_ms") or {}
    wait = waits.get("p95")
    samples = int(waits.get("observed_runs") or 0)
    congested = backlog > backlog_threshold or (
        wait is not None and int(wait) > wait_threshold_ms
    )
    if not congested and samples < 5:
        return _alert(
            "unknown",
            {
                "eligible_backlog": backlog,
                "p95_wait_ms": wait,
            },
            samples,
            {
                "eligible_backlog": backlog_threshold,
                "p95_wait_ms": wait_threshold_ms,
                "minimum_wait_samples": 5,
            },
            "minimum_5_queue_wait_samples_required",
        )
    return _alert(
        "warning" if congested else "ok",
        {"eligible_backlog": backlog, "p95_wait_ms": wait},
        samples,
        {
            "eligible_backlog": backlog_threshold,
            "p95_wait_ms": wait_threshold_ms,
            "minimum_wait_samples": 5,
        },
        "queue_threshold_exceeded" if congested else "queue_normal",
    )


def _comparison_eligibility(
    usage: Mapping[str, Any],
    *,
    task_cost_population: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Gate comparisons until all required evidence populations are known."""

    population_size = len(task_cost_population)
    evaluable = sum(
        1 for item in task_cost_population if item.get("amount_usd") is not None
    )
    adoption = dict(usage.get("execution_adoption") or {})
    checks = {
        "minimum_sample": {
            "status": "complete" if evaluable >= 5 else "unknown",
            "observed": evaluable,
            "required": 5,
            "reason": None if evaluable >= 5 else "minimum_5_priced_tasks_required",
        },
        "pricing_coverage": {
            "status": (
                "complete"
                if population_size > 0 and evaluable == population_size
                else "unknown"
            ),
            "observed": evaluable,
            "denominator": population_size,
            "reason": (
                None
                if population_size > 0 and evaluable == population_size
                else "priced_task_population_incomplete"
            ),
        },
        "result_coverage": {
            "status": "unknown",
            "observed": None,
            "denominator": None,
            "reason": "result_outcome_projection_unavailable",
        },
        "instrumentation_adoption": {
            "status": str(adoption.get("status") or "unknown"),
            "observed": adoption.get("observed_executions"),
            "denominator": adoption.get("denominator_executions"),
            "reason": adoption.get("reason"),
        },
    }
    incomplete = [
        name for name, check in checks.items() if check["status"] != "complete"
    ]
    return {
        "status": "complete" if not incomplete else "unknown",
        "reason": None if not incomplete else "comparison_coverage_incomplete",
        "incomplete_checks": incomplete,
        "checks": checks,
    }


def _cost_outlier_alert(
    task_cost_population: Sequence[Mapping[str, Any]],
    *,
    comparison_eligibility: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate all priced task buckets using Decimal, never the UI limit."""

    samples: list[tuple[Decimal, Mapping[str, Any]]] = []
    for item in task_cost_population:
        raw = item.get("amount_usd")
        try:
            amount = Decimal(str(raw)) if raw is not None else None
        except (InvalidOperation, TypeError, ValueError):
            amount = None
        if amount is not None:
            samples.append((amount, item.get("key", {})))

    population_size = len(task_cost_population)
    evaluable = len(samples)
    result: dict[str, Any]
    if evaluable < 5:
        result = _alert(
            "unknown",
            _decimal_string(max((item[0] for item in samples), default=None)),
            evaluable,
            {"minimum_samples": 5, "method": "median_plus_6_mad"},
            "minimum_5_priced_tasks_required",
        )
        result.update(
            {
                "population_size": population_size,
                "evaluable": evaluable,
                "outlier_count": 0,
                "truncated": False,
                "comparison_eligibility": dict(comparison_eligibility),
            }
        )
        return result

    amounts = [item[0] for item in samples]
    center = median(amounts)
    mad = median(abs(amount - center) for amount in amounts)
    threshold = max(Decimal("0.01"), center * 3, center + (6 * mad))
    outliers = [
        {"key": key, "api_equivalent_usd": _decimal_string(amount)}
        for amount, key in samples
        if amount > threshold
    ]
    computed_status = "warning" if outliers else "ok"
    gate_complete = comparison_eligibility.get("status") == "complete"
    result = _alert(
        computed_status if gate_complete else "unknown",
        _decimal_string(max(amounts)),
        evaluable,
        {
            "amount_usd": _decimal_string(threshold),
            "method": "max(0.01, median*3, median+6*MAD)",
        },
        (
            "task_cost_outlier_detected"
            if gate_complete and outliers
            else "task_costs_normal"
            if gate_complete
            else "comparison_coverage_incomplete"
        ),
    )
    result.update(
        {
            "computed_status": computed_status,
            "population_size": population_size,
            "evaluable": evaluable,
            "outlier_count": len(outliers),
            "truncated": len(outliers) > 20,
            "outliers": outliers[:20],
            "median_usd": _decimal_string(center),
            "mad_usd": _decimal_string(mad),
            "comparison_eligibility": dict(comparison_eligibility),
        }
    )
    return result


def _decimal_string(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _alert(
    status: str,
    observed: Any,
    denominator: Any,
    threshold: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "observed": observed,
        "denominator": denominator,
        "threshold": threshold,
        "reason": reason,
    }


def _ratio(observed_rows: int, denominator_rows: int) -> dict[str, Any]:
    return {
        "observed_rows": int(observed_rows),
        "denominator_rows": int(denominator_rows),
        "ratio": (
            observed_rows / denominator_rows if denominator_rows else None
        ),
        "status": (
            "complete"
            if denominator_rows > 0 and observed_rows >= denominator_rows
            else "partial"
            if observed_rows > 0
            else "unknown"
        ),
    }


def _percentile(values: Sequence[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(
            len(ordered) - 1,
            int((len(ordered) * fraction) + 0.999999) - 1,
        ),
    )
    return int(ordered[index])


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    absolute = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{quote(str(absolute), safe='/')}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        yield connection
    finally:
        connection.close()


__all__ = (
    "CONTRACT_VERSION",
    "DEFAULT_SENTINEL_STATUS_PATH",
    "build_fleet_metrics_payload",
)

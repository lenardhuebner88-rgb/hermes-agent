"""Hermes-only observability projection backed by Usage Facts and Langfuse."""

from __future__ import annotations

import copy
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

from fastapi import Query
from hermes_cli.kanban_score_hygiene import (
    RUN_CLASS_PRODUCTIVE,
    metric_scores_relation,
)
from hermes_cli.usage_facts_db import usage_facts_db_path
from hermes_cli.usage_facts_readmodel import (
    aggregate_normalized_tokens,
    build_usage_facts_payload,
)
from hermes_constants import get_hermes_home
from plugins.kanban.dashboard.langfuse_read_model import (
    CONTRACT_VERSION,
    get_langfuse_snapshot,
)

# extension_runtime.load_api_extension injects observability_routes, Query,
# Optional, _resolve_board and kanban_db from the parent dashboard API context.

_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

# Longer than the dashboard's normal 60 second poll interval: an unchanged
# seven-day projection is reused for at least one poll, while file identities
# below invalidate it immediately when an input changes.
_USAGE_CACHE_TTL_SECONDS = 120
_usage_cache_condition = threading.Condition(threading.RLock())
_usage_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_usage_cache_latest: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_usage_cache_inflight: set[tuple[Any, ...]] = set()


def _path_identity(path: Path) -> tuple[Any, ...]:
    resolved = path.expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None)
    return (
        str(resolved),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


def _sqlite_identity(path: Path) -> tuple[Any, ...]:
    """Identify a SQLite snapshot without opening or mutating the database."""
    return tuple(
        _path_identity(Path(f"{path}{suffix}")) for suffix in ("", "-wal", "-shm")
    )


def _profiles_identity(root: Path) -> tuple[Any, ...]:
    """Identify profile state/config inputs used for provider reconstruction."""
    if not root.is_dir():
        return (_path_identity(root),)
    candidates: list[Path] = []
    for profile_dir in sorted(root.iterdir(), key=lambda item: item.name):
        if not profile_dir.is_dir():
            continue
        candidates.extend(
            profile_dir / name
            for name in ("config.yaml", "state.db", "state.db-wal", "state.db-shm")
        )
    return tuple(_path_identity(path) for path in candidates)


def _usage_cache_identity(*, board: str, days: int) -> tuple[Any, ...]:
    usage_path = usage_facts_db_path()
    board_path = kanban_db.kanban_db_path(board=board)
    profiles_root = get_hermes_home() / "profiles"
    return (
        board,
        days,
        _sqlite_identity(usage_path),
        _sqlite_identity(board_path),
        _profiles_identity(profiles_root),
    )


def _usage_cache_scope(identity: tuple[Any, ...]) -> tuple[Any, ...]:
    # Board and time window are the semantic scope. The remaining identity
    # components are input revisions; an older revision is eligible only as an
    # explicitly stale fallback when a refresh fails.
    return identity[:2]


def _with_usage_cache_metadata(
    payload: Mapping[str, Any], *, created_at: float, state: str | None = None
) -> dict[str, Any]:
    result = copy.deepcopy(dict(payload))
    if state is not None:
        result["state"] = state
    result["cache"] = {
        "ttl_seconds": _USAGE_CACHE_TTL_SECONDS,
        "age_seconds": max(0, int(time.monotonic() - created_at)),
    }
    return result


def clear_usage_projection_cache() -> None:
    """Clear process-local projection state (primarily for isolated tests)."""
    with _usage_cache_condition:
        _usage_cache.clear()
        _usage_cache_latest.clear()
        _usage_cache_inflight.clear()
        _usage_cache_condition.notify_all()


def _token_filter(columns: set[str]) -> str:
    available = [name for name in _TOKEN_COLUMNS if name in columns]
    if not available:
        return "0"
    return "(" + " OR ".join(f"{name} IS NOT NULL" for name in available) + ")"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _group_tokens(group: Mapping[str, Any], field: str) -> int | None:
    value = _nested(group, "tokens", field, "tokens")
    number = _number(value)
    return int(number) if number is not None else None


def _group_cost_coverage(groups: list[dict[str, Any]]) -> dict[str, Any]:
    amount = Decimal("0")
    has_amount = False
    observed = denominator = lower_bounds = 0
    for group in groups:
        cost = _nested(group, "billing", "metered", "metered_usd")
        if not isinstance(cost, Mapping):
            continue
        priced = int(_number(cost.get("priced_breakdowns")) or 0)
        unpriced = int(_number(cost.get("unpriced_breakdowns")) or 0)
        observed += priced
        denominator += priced + unpriced
        lower_bounds += int(_number(cost.get("lower_bound_breakdowns")) or 0)
        raw_amount = cost.get("known_amount_usd")
        if raw_amount is not None:
            try:
                amount += Decimal(str(raw_amount))
                has_amount = True
            except InvalidOperation:
                pass
    status = (
        "complete"
        if denominator > 0 and observed == denominator and lower_bounds == 0
        else "partial"
        if observed > 0
        else "unknown"
    )
    return {
        "known_amount_usd": f"{amount:.6f}" if has_amount else None,
        "observed_facts": observed,
        "denominator_facts": denominator,
        "lower_bound_facts": lower_bounds,
        "status": status,
    }


def _dimension_row(groups: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = aggregate_normalized_tokens(group["tokens"] for group in groups)
    return {
        "fact_rows": sum(int(_number(group.get("fact_rows")) or 0) for group in groups),
        **tokens,
        "cost": _group_cost_coverage(groups),
    }


def _usage_dimensions(
    groups: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    origins: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"groups": [], "models": set()}
    )
    models: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"groups": [], "origins": set()}
    )
    for group in groups:
        key = group.get("key")
        if not isinstance(key, Mapping):
            continue
        origin = str(key.get("origin") or "unknown")
        model = str(key.get("model") or "nicht_zuordenbar")
        origins[origin]["groups"].append(group)
        origins[origin]["models"].add(model)
        models[model]["groups"].append(group)
        models[model]["origins"].add(origin)

    origin_rows = [
        {
            "name": name,
            **_dimension_row(values["groups"]),
            "model_count": len(values["models"]),
        }
        for name, values in origins.items()
    ]
    model_rows = [
        {
            "name": name,
            **_dimension_row(values["groups"]),
            "origin_count": len(values["origins"]),
        }
        for name, values in models.items()
    ]
    origin_rows.sort(key=lambda row: (-int(row["fact_rows"]), str(row["name"])))
    model_rows.sort(key=lambda row: (-int(row["fact_rows"]), str(row["name"])))
    return origin_rows, model_rows


def _read_exact_coverage(
    path: Path,
    *,
    captured_from: str,
    captured_to: str,
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "available": False,
            "reason": "usage_facts_missing",
            "fact_rows": 0,
            "exact_task_run_links": 0,
            "task_links": 0,
            "model_known": 0,
            "duration_known": 0,
            "ttft_known": 0,
            "missing_columns": [],
        }
    connection: sqlite3.Connection | None = None
    try:
        resolved = path.expanduser().resolve()
        connection = sqlite3.connect(
            f"file:{quote(str(resolved), safe='/')}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(run_usage_facts)")
        }
        desired = {
            "task_run_id",
            "task_id",
            "chain_id",
            "board",
            "session_id",
            "correlation_source",
            "origin",
            "profile",
            "model",
            "duration_ms",
            "first_token_ms",
        }
        token_filter = _token_filter(columns)
        missing = sorted(desired - columns)
        selectors = {
            name: (
                f"SUM(CASE WHEN {name} IS NOT NULL AND "
                f"TRIM(CAST({name} AS TEXT)) <> '' THEN 1 ELSE 0 END) AS {name}"
                if name in columns
                else f"0 AS {name}"
            )
            for name in desired
        }
        row = connection.execute(
            "SELECT COUNT(*) AS fact_rows, "
            + ", ".join(selectors.values())
            + " FROM run_usage_facts "
            f"WHERE {token_filter} "
            "AND captured_at >= ? AND captured_at < ?",
            (captured_from, captured_to),
        ).fetchone()
        return {
            "available": True,
            "reason": None,
            "fact_rows": int(row["fact_rows"] or 0),
            "exact_task_run_links": int(row["task_run_id"] or 0),
            "task_links": int(row["task_id"] or 0),
            "model_known": int(row["model"] or 0),
            "duration_known": int(row["duration_ms"] or 0),
            "ttft_known": int(row["first_token_ms"] or 0),
            "missing_columns": missing,
        }
    except sqlite3.Error:
        return {
            "available": False,
            "reason": "usage_facts_unreadable",
            "fact_rows": 0,
            "exact_task_run_links": 0,
            "task_links": 0,
            "model_known": 0,
            "duration_known": 0,
            "ttft_known": 0,
            "missing_columns": [],
        }
    finally:
        if connection is not None:
            connection.close()


def _read_usage_daily(
    path: Path,
    *,
    captured_from: str,
    captured_to: str,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    connection: sqlite3.Connection | None = None
    try:
        resolved = path.expanduser().resolve()
        connection = sqlite3.connect(
            f"file:{quote(str(resolved), safe='/')}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(run_usage_facts)")
        }
        token_filter = _token_filter(columns)
        rows = connection.execute(
            "SELECT SUBSTR(captured_at, 1, 10) AS date, "
            "COUNT(*) AS fact_rows, "
            "COUNT(*) AS token_rows "
            "FROM run_usage_facts "
            f"WHERE {token_filter} "
            "AND captured_at >= ? AND captured_at < ? "
            "GROUP BY SUBSTR(captured_at, 1, 10) ORDER BY date",
            (captured_from, captured_to),
        )
        return [
            {
                "date": str(row["date"] or ""),
                "fact_rows": int(row["fact_rows"] or 0),
                "token_rows": int(row["token_rows"] or 0),
            }
            for row in rows
            if row["date"]
        ]
    except sqlite3.Error:
        return []
    finally:
        if connection is not None:
            connection.close()


def _usage_projection(
    *,
    board: str,
    days: int,
    generated_at: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    captured_from = (
        now - timedelta(days=days)
    ).isoformat().replace("+00:00", "Z")
    path = usage_facts_db_path()
    try:
        payload = build_usage_facts_payload(
            path,
            kanban_path=kanban_db.kanban_db_path(board=board),
            profiles_root=get_hermes_home() / "profiles",
            captured_from=captured_from,
            captured_to=generated_at,
        )
    except Exception as exc:
        return {
            "available": False,
            "state": "absent",
            "reason": f"read_failed:{type(exc).__name__}",
            "captured_at": None,
            "summary": {},
            "origins": [],
            "models": [],
            "coverage": _read_exact_coverage(
                path,
                captured_from=captured_from,
                captured_to=generated_at,
            ),
        }

    groups = [
        dict(group)
        for group in payload.get("groups", [])
        if isinstance(group, Mapping)
    ]
    origins, models = _usage_dimensions(groups)
    summary = payload.get("summary")
    summary = dict(summary) if isinstance(summary, Mapping) else {}
    tokens = summary.get("tokens")
    tokens = dict(tokens) if isinstance(tokens, Mapping) else {}
    metered_price = _nested(
        summary,
        "billing",
        "metered",
        "metered_usd",
    )
    unclassified = _nested(summary, "billing", "unclassified")
    coverage = _read_exact_coverage(
        path,
        captured_from=captured_from,
        captured_to=generated_at,
    )
    return {
        "available": True,
        "state": "fresh",
        "reason": None,
        "captured_at": payload.get("generated_at") or generated_at,
        "summary": {
            "fact_rows": int(_number(summary.get("fact_rows")) or 0),
            "context_input_tokens": _optional_int(tokens.get("context_input_tokens")),
            "new_input_tokens": _optional_int(tokens.get("new_input_tokens")),
            "output_tokens": _optional_int(tokens.get("output_tokens")),
            "cache_read_tokens": _optional_int(tokens.get("cache_read_tokens")),
            "cache_write_tokens": _optional_int(tokens.get("cache_write_tokens")),
            "coverage": dict(tokens.get("coverage") or {}),
            "cost": _group_cost_coverage(groups),
            "known_metered_usd": (
                metered_price.get("known_amount_usd")
                if isinstance(metered_price, Mapping)
                else None
            ),
            "price_status": (
                metered_price.get("status")
                if isinstance(metered_price, Mapping)
                else "unknown"
            ),
            "priced_breakdowns": int(
                _number(
                    metered_price.get("priced_breakdowns")
                    if isinstance(metered_price, Mapping)
                    else None
                )
                or 0
            ),
            "unpriced_breakdowns": int(
                _number(
                    metered_price.get("unpriced_breakdowns")
                    if isinstance(metered_price, Mapping)
                    else None
                )
                or 0
            ),
            "unclassified_fact_rows": int(
                _number(
                    unclassified.get("fact_rows")
                    if isinstance(unclassified, Mapping)
                    else None
                )
                or 0
            ),
        },
        "origins": origins,
        "models": models,
        # captured_at is an ingestion/event timestamp with mixed producer
        # semantics. The UI labels this series as import activity, never as
        # worker throughput.
        "daily_imports": _read_usage_daily(
            path,
            captured_from=captured_from,
            captured_to=generated_at,
        ),
        "coverage": coverage,
    }


def _cached_usage_projection(
    *,
    board: str,
    days: int,
    generated_at: str,
) -> dict[str, Any]:
    """Return an identity-bound projection with TTL and per-key single-flight."""
    identity = _usage_cache_identity(board=board, days=days)
    scope = _usage_cache_scope(identity)
    while True:
        with _usage_cache_condition:
            cached = _usage_cache.get(identity)
            now = time.monotonic()
            if cached is not None and now - cached[0] <= _USAGE_CACHE_TTL_SECONDS:
                return _with_usage_cache_metadata(cached[1], created_at=cached[0])
            if identity not in _usage_cache_inflight:
                _usage_cache_inflight.add(identity)
                break
            _usage_cache_condition.wait()

    payload: dict[str, Any] | None = None
    failure_reason: str | None = None
    try:
        payload = _usage_projection(
            board=board,
            days=days,
            generated_at=generated_at,
        )
        if not payload.get("available"):
            failure_reason = str(payload.get("reason") or "refresh_unavailable")
    except Exception as exc:
        failure_reason = f"refresh_failed:{type(exc).__name__}"

    with _usage_cache_condition:
        try:
            if payload is not None and failure_reason is None:
                created_at = time.monotonic()
                entry = (created_at, copy.deepcopy(payload))
                for cached_identity in list(_usage_cache):
                    if _usage_cache_scope(cached_identity) == scope:
                        _usage_cache.pop(cached_identity, None)
                _usage_cache[identity] = entry
                _usage_cache_latest[scope] = entry
                return _with_usage_cache_metadata(payload, created_at=created_at)

            stale = _usage_cache_latest.get(scope)
            if stale is not None:
                result = _with_usage_cache_metadata(
                    stale[1], created_at=stale[0], state="stale"
                )
                result["reason"] = failure_reason
                return result

            unavailable = payload or {
                "available": False,
                "state": "absent",
                "reason": failure_reason,
                "captured_at": None,
                "summary": {},
                "origins": [],
                "models": [],
                "coverage": {},
            }
            return _with_usage_cache_metadata(
                unavailable,
                created_at=time.monotonic(),
            )
        finally:
            _usage_cache_inflight.discard(identity)
            _usage_cache_condition.notify_all()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction) + 0.999999) - 1))
    return ordered[index]


def _run_duration_projection(
    *,
    board: str,
    days: int,
) -> dict[str, Any]:
    values: list[float] = []
    connection: sqlite3.Connection | None = None
    try:
        connection = _conn(board)
        relation = metric_scores_relation(connection)
        cutoff = int(time.time()) - (days * 86400)
        rows = connection.execute(
            f"SELECT value FROM {relation} "
            "WHERE name='run_duration_seconds' AND created_at >= ? "
            "AND run_class=?",
            (cutoff, RUN_CLASS_PRODUCTIVE),
        )
        values = [
            number
            for row in rows
            if (number := _number(row["value"])) is not None
        ]
    except sqlite3.Error:
        values = []
    finally:
        if connection is not None:
            connection.close()
    return {
        "p50_seconds": _percentile(values, 0.50),
        "p95_seconds": _percentile(values, 0.95),
        "count": len(values),
        "source": "kanban.metric_scores.run_duration_seconds",
    }


@observability_routes.get("/stats/observability")
def get_observability_stats(
    board: Optional[str] = Query(
        None,
        description="Kanban board slug (omit for current)",
    ),
    days: int = Query(7, ge=1, le=30),
):
    """Return sanitized data-plane aggregates for /control/statistik only."""
    resolved_board = _resolve_board(board)
    generated_at = (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "window_days": days,
        "generated_at": generated_at,
        "langfuse": get_langfuse_snapshot(days=days),
        "usage": _cached_usage_projection(
            board=resolved_board,
            days=days,
            generated_at=generated_at,
        ),
        "run_duration": _run_duration_projection(
            board=resolved_board,
            days=days,
        ),
        "checked_at": int(time.time()),
    }


__all__ = ("clear_usage_projection_cache", "get_observability_stats")

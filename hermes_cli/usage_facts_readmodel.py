"""Read-only aggregate projection over the unified usage-facts database.

The persisted ``input_tokens`` bucket is intentionally source-native.  Some
origins store cache tokens beside it while others store cache reads inside it.
This module is the single read-time boundary that converts both shapes into
comparable context/new/uncached input totals before pricing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

from hermes_cli.active_provider_facts import (
    classification_counts,
    reconstruction_source_counts,
    resolve_active_provider_facts,
)
from hermes_cli.usage_facts_buzz_attribution import (
    BUZZ_AGENT_ORIGIN,
    BUZZ_SOURCE_ORIGIN_PREFIXES,
    BUZZ_SOURCE_ORIGIN_SQL,
)
from hermes_cli.usage_facts_pricing import (
    PRICE_COMPONENTS as _PRICE_COMPONENTS,
)
from hermes_cli.usage_facts_pricing import (
    CostResult,
    UsageFactsUsage,
    estimate_equivalent_cost,
    estimate_usage_cost,
)

CONTRACT_VERSION = "usage-facts.v1"
ATTRIBUTED_CONTRACT_VERSION = "attributed-usage.v1"
NORMALIZATION_VERSION = "origin-input.v1"

INPUT_CACHE_EXCLUSIVE = "cache_exclusive"
INPUT_CACHE_INCLUSIVE = "cache_inclusive"
INPUT_NOT_DERIVABLE = "not_derivable"

STATUS_EXACT = "exact"
STATUS_LOWER_BOUND = "lower_bound"
STATUS_UNAVAILABLE = "unavailable"

BILLING_METERED = "metered"
BILLING_QUOTA = "quota"
BILLING_UNCLASSIFIED = "unclassified"

UNATTRIBUTED_MODEL_LABEL = "nicht_zuordenbar"

_CACHE_EXCLUSIVE_ORIGINS = frozenset(
    {"claude_code", "hermes_agent", "hermes_aux"}
)
_CACHE_INCLUSIVE_ORIGINS = frozenset(
    {"codex_cli", "kimi_cli", "grok_cli", "qwen_cli"}
)
_SUBSCRIPTION_BILLING_MODE = "subscription_included"
_UNKNOWN_BILLING_MODES = frozenset({"", "unknown"})
_ONE_MILLION = Decimal("1000000")
_MAIN_CALL_KINDS = frozenset({"main", "main_loop"})
_SUBAGENT_CALL_KIND = "subagent"
_WORKLOAD_ROLES = ("main", "subagent", "unknown")
DEFAULT_RATE_LIMIT_SIDECAR_PATH = Path(
    "/mnt/data/hermes-observability/foreign_rate_limit_snapshots.jsonl"
)
RATE_LIMIT_STALE_AFTER_SECONDS = 60 * 60

_TOKEN_COLUMNS = _PRICE_COMPONENTS


@dataclass(frozen=True, slots=True)
class _PriceVector:
    rates_per_million: Mapping[str, Decimal | None]
    request_rate: Decimal | None
    status: str
    source: str
    fetched_at: str | None
    pricing_version: str | None
    notes: tuple[str, ...]


def input_semantics(origin: str) -> str:
    """Return the documented persisted-input contract for one origin."""

    if origin in _CACHE_EXCLUSIVE_ORIGINS:
        return INPUT_CACHE_EXCLUSIVE
    if origin in _CACHE_INCLUSIVE_ORIGINS:
        return INPUT_CACHE_INCLUSIVE
    return INPUT_NOT_DERIVABLE


def normalization_contract() -> dict[str, Any]:
    """Stable, machine-readable derivation table shipped with every payload."""

    source_origins = sorted(
        _CACHE_EXCLUSIVE_ORIGINS | _CACHE_INCLUSIVE_ORIGINS
    )
    origin_contracts = {
        origin: {
            "input_semantics": input_semantics(origin),
            "context_input": (
                "input_tokens + cache_read_tokens + cache_write_tokens"
                if input_semantics(origin) == INPUT_CACHE_EXCLUSIVE
                else "input_tokens"
            ),
            "new_input": (
                "input_tokens + cache_write_tokens"
                if input_semantics(origin) == INPUT_CACHE_EXCLUSIVE
                else "input_tokens - cache_read_tokens"
            ),
            "uncached_input": (
                "input_tokens"
                if input_semantics(origin) == INPUT_CACHE_EXCLUSIVE
                else "input_tokens - cache_read_tokens - cache_write_tokens"
            ),
        }
        for origin in source_origins
    }
    origin_contracts[BUZZ_AGENT_ORIGIN] = {
        "input_semantics": "source_dependent",
        "source_origin_field": "groups[].key.source_origin",
        "source_origins": {
            origin: origin_contracts[origin]
            for origin in BUZZ_SOURCE_ORIGIN_PREFIXES
        },
    }
    return {
        "version": NORMALIZATION_VERSION,
        "fields": {
            "context_input": (
                "all tokens presented as prompt context; a lower_bound is "
                "returned when an exclusive source omitted a cache bucket"
            ),
            "new_input": (
                "context input excluding cache reads; for metered routes this "
                "contains standard input plus cache writes"
            ),
            "uncached_input": (
                "standard-rate input excluding cache reads and cache writes"
            ),
        },
        "origins": origin_contracts,
    }


def normalize_token_totals(
    origin: str,
    *,
    token_rows: int,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    reasoning_tokens: int | None,
    input_observed_rows: int,
    output_observed_rows: int,
    cache_read_observed_rows: int,
    cache_write_observed_rows: int,
    reasoning_observed_rows: int,
    cache_write_1h_tokens: int | None = None,
    cache_write_5m_tokens: int | None = None,
    cache_write_1h_observed_rows: int = 0,
    cache_write_5m_observed_rows: int = 0,
    priced_subset_rows: int = 0,
    priced_subset_input_tokens: int | None = None,
    priced_subset_cache_read_tokens: int | None = None,
    priced_subset_cache_write_tokens: int | None = None,
    priced_subset_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Normalize one aggregate without filling unknown buckets with zero."""

    semantics = input_semantics(origin)
    input_known = int(input_tokens or 0)
    cache_read_known = int(cache_read_tokens or 0)
    cache_write_known = int(cache_write_tokens or 0)
    all_input = token_rows > 0 and input_observed_rows == token_rows
    all_cache_read = (
        token_rows > 0 and cache_read_observed_rows == token_rows
    )
    all_cache_write = (
        token_rows > 0 and cache_write_observed_rows == token_rows
    )

    if semantics == INPUT_CACHE_EXCLUSIVE:
        context_tokens = input_known + cache_read_known + cache_write_known
        context_status = (
            STATUS_EXACT
            if all_input and all_cache_read and all_cache_write
            else STATUS_LOWER_BOUND
            if token_rows > 0
            else STATUS_UNAVAILABLE
        )
        new_tokens = input_known + cache_write_known
        new_status = (
            STATUS_EXACT
            if all_input and all_cache_write
            else STATUS_LOWER_BOUND
            if token_rows > 0
            else STATUS_UNAVAILABLE
        )
        uncached_tokens: int | None = input_known if all_input else None
        uncached_status = (
            STATUS_EXACT if all_input else STATUS_UNAVAILABLE
        )
    elif semantics == INPUT_CACHE_INCLUSIVE:
        context_tokens = input_known if all_input else None
        context_status = (
            STATUS_EXACT if all_input else STATUS_UNAVAILABLE
        )
        new_tokens = (
            max(0, input_known - cache_read_known)
            if all_input and all_cache_read
            else None
        )
        new_status = (
            STATUS_EXACT
            if new_tokens is not None
            else STATUS_UNAVAILABLE
        )
        uncached_tokens = (
            max(
                0,
                input_known - cache_read_known - cache_write_known,
            )
            if all_input and all_cache_read and all_cache_write
            else None
        )
        uncached_status = (
            STATUS_EXACT
            if uncached_tokens is not None
            else STATUS_UNAVAILABLE
        )
    else:
        context_tokens = None
        context_status = STATUS_UNAVAILABLE
        new_tokens = None
        new_status = STATUS_UNAVAILABLE
        uncached_tokens = None
        uncached_status = STATUS_UNAVAILABLE

    # Cache-inclusive groups with mixed observability: the rows carrying a
    # complete token split stay exactly priceable instead of being dragged
    # into the all-or-nothing gate by rows whose source lacks a field.
    priced_subset: Optional[dict[str, Any]] = None
    if (
        semantics == INPUT_CACHE_INCLUSIVE
        and 0 < int(priced_subset_rows or 0) < int(token_rows or 0)
        and priced_subset_input_tokens is not None
        and priced_subset_cache_read_tokens is not None
        and priced_subset_cache_write_tokens is not None
        and priced_subset_output_tokens is not None
    ):
        subset_input = int(priced_subset_input_tokens)
        subset_cache_read = int(priced_subset_cache_read_tokens)
        subset_cache_write = int(priced_subset_cache_write_tokens)
        priced_subset = {
            "rows": int(priced_subset_rows),
            "unpriced_rows": int(token_rows) - int(priced_subset_rows),
            "uncached_input": max(
                0, subset_input - subset_cache_read - subset_cache_write
            ),
            "output_tokens": int(priced_subset_output_tokens),
            "cache_read_tokens": subset_cache_read,
            "cache_write_tokens": subset_cache_write,
        }

    return {
        "input_semantics": semantics,
        "raw_input_tokens": input_tokens,
        "context_input": {
            "tokens": context_tokens,
            "status": context_status,
        },
        "new_input": {"tokens": new_tokens, "status": new_status},
        "uncached_input": {
            "tokens": uncached_tokens,
            "status": uncached_status,
        },
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        # TTL split of cache writes. Present as a pair or not at all on
        # run aggregates; kept verbatim like every other observation.
        "cache_write_1h_tokens": cache_write_1h_tokens,
        "cache_write_5m_tokens": cache_write_5m_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "observed_rows": {
            "token_rows": token_rows,
            "input": input_observed_rows,
            "cache_read": cache_read_observed_rows,
            "cache_write": cache_write_observed_rows,
            "cache_write_1h": cache_write_1h_observed_rows,
            "cache_write_5m": cache_write_5m_observed_rows,
            "output": output_observed_rows,
            "reasoning": reasoning_observed_rows,
        },
        "priced_subset": priced_subset,
    }


def build_usage_facts_payload(
    usage_facts_path: str | Path,
    *,
    kanban_path: str | Path | None = None,
    profiles_root: str | Path | None = None,
    rate_limit_path: str | Path | None = None,
    origins: Sequence[str] | None = None,
    captured_from: str | None = None,
    captured_to: str | None = None,
    generated_at: str | None = None,
    immutable_evidence: bool = False,
) -> dict[str, Any]:
    """Build the complete S7 payload from read-only SQLite inputs."""

    started = time.perf_counter()
    payload_generated_at = generated_at or _utc_now_iso()
    usage_path = Path(usage_facts_path)
    where_sql, params = _usage_filters(
        origins=origins,
        captured_from=captured_from,
        captured_to=captured_to,
    )

    query_started = time.perf_counter()
    with _read_only_connection(usage_path) as connection:
        database_counts = _database_counts(connection, where_sql, params)
        rows = connection.execute(
            _aggregate_sql(where_sql),
            params,
        ).fetchall()
    query_ms = (time.perf_counter() - query_started) * 1000
    workload = _workload_rollup(rows)

    pricing_cache: dict[tuple[str, str, str, str | None], _PriceVector] = {}
    group_builders: dict[
        tuple[str, str, str | None, str | None, str | None],
        dict[str, Any],
    ] = {}

    for row in rows:
        raw = _row_metrics(row)
        origin = str(row["origin"])
        source_origin = str(row["source_origin"])
        key = (
            origin,
            source_origin,
            row["profile"],
            row["lane"],
            row["model"],
        )
        group = group_builders.setdefault(
            key,
            {
                "raw": _empty_metrics(),
                "breakdowns": [],
            },
        )
        _add_metrics(group["raw"], raw)
        normalized = _normalize_metrics(source_origin, raw)
        billing_category = _billing_category(row["billing_mode"])
        charge = _charge_for_breakdown(
            billing_category,
            provider=row["provider"],
            model=row["model"],
            origin=source_origin,
            normalized=normalized,
            raw=raw,
            pricing_cache=pricing_cache,
        )
        group["breakdowns"].append(
            {
                "provider": row["provider"],
                "billing_mode": row["billing_mode"],
                "category": billing_category,
                "fact_rows": raw["fact_rows"],
                "tokens": normalized,
                "charge": charge,
            }
        )

    groups: list[dict[str, Any]] = []
    for key in sorted(group_builders, key=_sort_group_key):
        origin, source_origin, profile, lane, model = key
        builder = group_builders[key]
        normalized = _normalize_metrics(source_origin, builder["raw"])
        group_key = {
            "origin": origin,
            "profile": profile,
            "lane": lane,
            "model": model,
            "model_label": model or UNATTRIBUTED_MODEL_LABEL,
        }
        if origin == BUZZ_AGENT_ORIGIN:
            group_key["source_origin"] = source_origin
        groups.append(
            {
                "key": group_key,
                "fact_rows": builder["raw"]["fact_rows"],
                "tokens": normalized,
                "billing": _billing_rollup(
                    builder["breakdowns"],
                    include_empty=False,
                ),
                "_billing_breakdown": builder["breakdowns"],
            }
        )

    summary = _payload_summary(groups)
    summary["workload"] = workload
    for group in groups:
        group.pop("_billing_breakdown", None)
    kanban_started = time.perf_counter()
    kanban = _kanban_projection(
        kanban_path=Path(kanban_path) if kanban_path is not None else None,
        usage_facts_path=usage_path,
        profiles_root=(
            Path(profiles_root) if profiles_root is not None else None
        ),
        immutable_evidence=immutable_evidence,
    )
    kanban_ms = (time.perf_counter() - kanban_started) * 1000
    rate_limits = _rate_limit_projection(
        (
            Path(rate_limit_path)
            if rate_limit_path is not None
            else DEFAULT_RATE_LIMIT_SIDECAR_PATH
        ),
        observed_at=_parse_utc_datetime(payload_generated_at)
        or datetime.now(timezone.utc),
    )

    finished = time.perf_counter()
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": payload_generated_at,
        "scope": {
            "origins": sorted(set(origins)) if origins else None,
            "captured_from": captured_from,
            "captured_to": captured_to,
        },
        "normalization": normalization_contract(),
        "summary": summary,
        "groups": groups,
        "unattributed": _unattributed_payload(groups),
        "kanban": kanban,
        "rate_limits": rate_limits,
        "database": database_counts,
        "timing_ms": {
            "facts_query": round(query_ms, 3),
            "kanban_projection": round(kanban_ms, 3),
            "total": round((finished - started) * 1000, 3),
        },
    }


def build_attributed_usage_payload(
    usage_facts_path: str | Path,
    *,
    origins: Sequence[str] | None = None,
    captured_from: str | None = None,
    captured_to: str | None = None,
    generated_at: str | None = None,
    limit: int = 100,
    include_task_cost_population: bool = False,
) -> dict[str, Any]:
    """Roll exact usage into task, chain, and provider/model buckets.

    Task and chain buckets deliberately require ``task_run_id``.  A task-only
    correlation remains visible in the attribution coverage, but is not
    promoted into price/performance output where it could charge the wrong run.
    """
    safe_limit = max(1, min(int(limit), 500))
    payload_generated_at = generated_at or _utc_now_iso()
    observed_at = (
        _parse_utc_datetime(payload_generated_at)
        or datetime.now(timezone.utc)
    )
    where_sql, params = _usage_scope_filters(
        origins=origins,
        captured_from=captured_from,
        captured_to=captured_to,
    )
    with _read_only_connection(Path(usage_facts_path)) as connection:
        coverage_row = connection.execute(
            _attribution_coverage_sql(where_sql),
            params,
        ).fetchone()
        rows = connection.execute(
            _attributed_aggregate_sql(where_sql),
            params,
        ).fetchall()

    pricing_cache: dict[tuple[str, str, str, str | None], _PriceVector] = {}
    builders: dict[str, dict[Any, dict[str, Any]]] = {
        "tasks": {},
        "chains": {},
        "models": {},
    }
    for row in rows:
        raw = _row_metrics(row)
        origin = str(row["origin"])
        source_origin = str(row["source_origin"])
        normalized = _normalize_metrics(source_origin, raw)
        category = _billing_category(row["billing_mode"])
        charge = _charge_for_breakdown(
            category,
            provider=row["provider"],
            model=row["model"],
            origin=source_origin,
            normalized=normalized,
            raw=raw,
            pricing_cache=pricing_cache,
        )
        atom = {
            "fact_rows": raw["fact_rows"],
            "run_count": int(row["distinct_runs"]),
            "token_rows": raw["token_rows"],
            "model_rows": raw["fact_rows"] if row["model"] is not None else 0,
            "tokens": normalized,
            "billing": {
                "provider": row["provider"],
                "billing_mode": row["billing_mode"],
                "category": category,
                "fact_rows": raw["fact_rows"],
                "tokens": normalized,
                "charge": charge,
            },
            "latest_captured_at": row["latest_captured_at"],
        }
        _add_attributed_atom(
            builders["models"],
            (
                origin,
                row["provider"],
                row["model"],
            ),
            {
                "origin": origin,
                "provider": row["provider"],
                "model": row["model"],
                "model_label": row["model"] or UNATTRIBUTED_MODEL_LABEL,
            },
            atom,
        )
        if row["task_run_id"] is None or row["task_id"] is None:
            continue
        _add_attributed_atom(
            builders["tasks"],
            (row["board"], row["task_id"]),
            {
                "board": row["board"],
                "task_id": row["task_id"],
            },
            atom,
            run_identity=str(row["task_run_id"]),
        )
        if row["chain_id"] is not None:
            _add_attributed_atom(
                builders["chains"],
                (row["board"], row["chain_id"]),
                {
                    "board": row["board"],
                    "chain_id": row["chain_id"],
                },
                atom,
                run_identity=str(row["task_run_id"]),
            )

    bucket_payloads = {
        kind: _finish_attributed_buckets(
            kind_builders,
            observed_at=observed_at,
            limit=safe_limit,
            include_cost_population=(
                include_task_cost_population and kind == "tasks"
            ),
        )
        for kind, kind_builders in builders.items()
    }
    coverage = _attribution_coverage(coverage_row)
    model_builders = list(builders["models"].values())
    summary = {
        "fact_rows": coverage["fact_rows"],
        "tokens": _sum_normalized_tokens(
            token_item
            for builder in model_builders
            for token_item in builder["tokens"]
        ),
        "billing": _billing_rollup(
            (
                billing_item
                for builder in model_builders
                for billing_item in builder["billing"]
            ),
        ),
    }
    return {
        "contract_version": ATTRIBUTED_CONTRACT_VERSION,
        "generated_at": payload_generated_at,
        "scope": {
            "origins": sorted(set(origins)) if origins else None,
            "captured_from": captured_from,
            "captured_to": captured_to,
            "task_and_chain_attribution": "exact_task_run_id_only",
        },
        "summary": summary,
        "coverage": coverage,
        # ``coverage`` remains for v1 consumers.  Its denominator is facts,
        # not executions; retain it under an explicit name so consumers do
        # not mistake a well-harvested history for collector adoption.
        "fact_coverage": {
            **coverage,
            "denominator_kind": "usage_fact_rows",
        },
        "execution_adoption": {
            "observed_executions": None,
            "denominator_executions": None,
            "ratio": None,
            "status": "unknown",
            "reason": "universal_execution_denominator_unavailable",
        },
        "unknown": {
            "task_run_unattributed_rows": (
                coverage["fact_rows"]
                - coverage["exact_task_run"]["observed_rows"]
            ),
            "task_only_rows_not_promoted": coverage["task_only_rows"],
            "chain_unknown_rows": (
                coverage["fact_rows"]
                - coverage["exact_chain"]["observed_rows"]
            ),
            "model_unknown_rows": (
                coverage["fact_rows"] - coverage["model"]["observed_rows"]
            ),
            "token_unknown_rows": (
                coverage["fact_rows"] - coverage["tokens"]["observed_rows"]
            ),
        },
        **bucket_payloads,
    }


def _usage_filters(
    *,
    origins: Sequence[str] | None,
    captured_from: str | None,
    captured_to: str | None,
) -> tuple[str, tuple[Any, ...]]:
    conditions = [
        "("
        + " OR ".join(f"{column} IS NOT NULL" for column in _TOKEN_COLUMNS)
        + ")"
    ]
    params: list[Any] = []
    normalized_origins = sorted(
        {str(origin).strip() for origin in origins or () if str(origin).strip()}
    )
    if normalized_origins:
        placeholders = ", ".join("?" for _ in normalized_origins)
        conditions.append(f"origin IN ({placeholders})")
        params.extend(normalized_origins)
    if captured_from:
        conditions.append("captured_at >= ?")
        params.append(captured_from)
    if captured_to:
        conditions.append("captured_at < ?")
        params.append(captured_to)
    return " WHERE " + " AND ".join(conditions), tuple(params)


def _usage_scope_filters(
    *,
    origins: Sequence[str] | None,
    captured_from: str | None,
    captured_to: str | None,
) -> tuple[str, tuple[Any, ...]]:
    conditions: list[str] = []
    params: list[Any] = []
    normalized_origins = sorted(
        {str(origin).strip() for origin in origins or () if str(origin).strip()}
    )
    if normalized_origins:
        placeholders = ", ".join("?" for _ in normalized_origins)
        conditions.append(f"origin IN ({placeholders})")
        params.extend(normalized_origins)
    if captured_from:
        conditions.append("captured_at >= ?")
        params.append(captured_from)
    if captured_to:
        conditions.append("captured_at < ?")
        params.append(captured_to)
    return (
        (" WHERE " + " AND ".join(conditions)) if conditions else "",
        tuple(params),
    )


def _token_present_sql() -> str:
    return "(" + " OR ".join(
        f"{column} IS NOT NULL" for column in _TOKEN_COLUMNS
    ) + ")"


def _attribution_coverage_sql(where_sql: str) -> str:
    token_present = _token_present_sql()
    return f"""
        SELECT COUNT(*) AS fact_rows,
               SUM(CASE WHEN {token_present} THEN 1 ELSE 0 END) AS token_rows,
               SUM(CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) AS model_rows,
               SUM(CASE WHEN task_run_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS exact_task_run_rows,
               SUM(CASE WHEN task_run_id IS NOT NULL AND task_id IS NOT NULL
                        THEN 1 ELSE 0 END) AS exact_task_rows,
               SUM(CASE WHEN task_run_id IS NULL AND task_id IS NOT NULL
                        THEN 1 ELSE 0 END) AS task_only_rows,
               SUM(CASE WHEN task_run_id IS NOT NULL
                             AND task_id IS NOT NULL
                             AND chain_id IS NOT NULL
                        THEN 1 ELSE 0 END) AS exact_chain_rows,
               MAX(captured_at) AS latest_captured_at
          FROM run_usage_facts
          {where_sql}
    """


def _attributed_aggregate_sql(where_sql: str) -> str:
    observed = ",\n       ".join(
        item
        for pair in (
            (
                f"SUM({column}) AS {column}",
                f"COUNT({column}) AS {column}_observed_rows",
            )
            for column in _TOKEN_COLUMNS
        )
        for item in pair
    )
    token_present = _token_present_sql()
    return f"""
        SELECT task_run_id, task_id, chain_id, board, origin,
               {BUZZ_SOURCE_ORIGIN_SQL} AS source_origin, provider, model,
               billing_mode,
               COUNT(*) AS fact_rows,
               COUNT(DISTINCT run_id) AS distinct_runs,
               SUM(CASE WHEN {token_present} THEN 1 ELSE 0 END) AS token_rows,
               {observed},
               SUM(COALESCE(llm_call_count, 1)) AS request_count,
               MAX(captured_at) AS latest_captured_at
          FROM run_usage_facts
          {where_sql}
         GROUP BY task_run_id, task_id, chain_id, board, origin,
                  source_origin, provider, model, billing_mode
    """


def _aggregate_sql(where_sql: str) -> str:
    observed = ",\n       ".join(
        item
        for pair in (
            (
                f"SUM({column}) AS {column}",
                f"COUNT({column}) AS {column}_observed_rows",
            )
            for column in _TOKEN_COLUMNS
        )
        for item in pair
    )
    priced_subset_cond = (
        "input_tokens IS NOT NULL AND output_tokens IS NOT NULL "
        "AND cache_read_tokens IS NOT NULL AND cache_write_tokens IS NOT NULL"
    )
    priced_subset = ",\n       ".join(
        f"SUM(CASE WHEN {priced_subset_cond} THEN {column} END) "
        f"AS priced_subset_{column}"
        for column in (
            "input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
        )
    )
    return f"""
        SELECT origin, {BUZZ_SOURCE_ORIGIN_SQL} AS source_origin, call_kind,
               profile, lane, model, provider, billing_mode,
               COUNT(*) AS fact_rows,
               COUNT(*) AS token_rows,
               {observed},
               SUM(CASE WHEN {priced_subset_cond} THEN 1 ELSE 0 END)
                   AS priced_subset_rows,
               {priced_subset},
               SUM(COALESCE(llm_call_count, 1)) AS request_count
          FROM run_usage_facts
          {where_sql}
         GROUP BY origin, source_origin, call_kind, profile, lane, model,
                  provider, billing_mode
         ORDER BY origin, source_origin, call_kind, profile, lane, model,
                  provider, billing_mode
    """


def _database_counts(
    connection: sqlite3.Connection,
    where_sql: str,
    params: Sequence[Any],
) -> dict[str, int]:
    total_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM run_usage_facts"
        ).fetchone()[0]
    )
    token_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM run_usage_facts" + where_sql,
            params,
        ).fetchone()[0]
    )
    return {
        "run_usage_facts_rows": total_rows,
        "selected_token_rows": token_rows,
        "rows_without_tokens": total_rows
        - int(
            connection.execute(
                "SELECT COUNT(*) FROM run_usage_facts WHERE "
                + "("
                + " OR ".join(
                    f"{column} IS NOT NULL" for column in _TOKEN_COLUMNS
                )
                + ")"
            ).fetchone()[0]
        ),
    }


def _row_metrics(row: sqlite3.Row) -> dict[str, Any]:
    metrics = _empty_metrics()
    metrics["fact_rows"] = int(row["fact_rows"])
    metrics["token_rows"] = int(row["token_rows"])
    metrics["request_count"] = int(row["request_count"] or 0)
    for column in _TOKEN_COLUMNS:
        metrics[column] = (
            int(row[column]) if row[column] is not None else None
        )
        metrics[f"{column}_observed_rows"] = int(
            row[f"{column}_observed_rows"]
        )
    # Priced-subset columns exist only in the main aggregate SQL; the
    # attributed aggregate keeps the legacy shape.
    keys = set(row.keys())
    if "priced_subset_rows" in keys:
        metrics["priced_subset_rows"] = int(row["priced_subset_rows"] or 0)
        for column in (
            "input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
        ):
            value = row[f"priced_subset_{column}"]
            metrics[f"priced_subset_{column}"] = (
                int(value) if value is not None else None
            )
    return metrics


def _workload_rollup(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
    """Partition normalized context by main, discovered subagent, and unknown."""

    registered_subagent_types = {
        profile
        for row in rows
        if _normalized_call_value(row["call_kind"]) == _SUBAGENT_CALL_KIND
        if (profile := _normalized_call_value(row["profile"])) is not None
    }
    normalized_by_role: dict[str, list[dict[str, Any]]] = {
        role: [] for role in _WORKLOAD_ROLES
    }
    fact_rows_by_role = {role: 0 for role in _WORKLOAD_ROLES}
    for row in rows:
        role = _workload_role(
            row["call_kind"], registered_subagent_types
        )
        raw = _row_metrics(row)
        fact_rows_by_role[role] += raw["fact_rows"]
        normalized_by_role[role].append(
            _normalize_metrics(str(row["source_origin"]), raw)
        )

    role_totals = {
        role: _sum_normalized_tokens(normalized_by_role[role])
        for role in _WORKLOAD_ROLES
    }
    result = {
        role: {
            "fact_rows": fact_rows_by_role[role],
            "context_input_tokens": role_totals[role]["context_input_tokens"],
            "coverage": role_totals[role]["coverage"]["context_input_tokens"],
        }
        for role in _WORKLOAD_ROLES
    }
    all_totals = _sum_normalized_tokens(
        item for role in _WORKLOAD_ROLES for item in normalized_by_role[role]
    )
    classified_totals = _sum_normalized_tokens(
        item
        for role in ("main", "subagent")
        for item in normalized_by_role[role]
    )
    all_context = all_totals["context_input_tokens"]
    classified_context = classified_totals["context_input_tokens"]
    subagent_context = result["subagent"]["context_input_tokens"]
    result["subagent_share"] = {
        "context_input_tokens": subagent_context,
        "all_context_input_tokens": all_context,
        "of_all_context": (
            subagent_context / all_context
            if subagent_context is not None
            and all_context
            and all_totals["coverage"]["context_input_tokens"]["status"] == "complete"
            else None
        ),
        "classified_context_input_tokens": classified_context,
        "of_classified_context": (
            subagent_context / classified_context
            if subagent_context is not None
            and classified_context
            and classified_totals["coverage"]["context_input_tokens"]["status"]
            == "complete"
            else None
        ),
        "classification_status": (
            "partial" if fact_rows_by_role["unknown"] else "complete"
        ),
    }
    return result


def _normalized_call_value(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return text or None


def _workload_role(
    call_kind: Any, registered_subagent_types: set[str]
) -> str:
    normalized = _normalized_call_value(call_kind)
    if normalized in _MAIN_CALL_KINDS:
        return "main"
    if (
        normalized == _SUBAGENT_CALL_KIND
        or normalized in registered_subagent_types
    ):
        return "subagent"
    return "unknown"


def _empty_metrics() -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "fact_rows": 0,
        "token_rows": 0,
        "request_count": 0,
        "priced_subset_rows": 0,
    }
    for column in _TOKEN_COLUMNS:
        metrics[column] = None
        metrics[f"{column}_observed_rows"] = 0
    for column in (
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
    ):
        metrics[f"priced_subset_{column}"] = None
    return metrics


def _add_metrics(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key in ("fact_rows", "token_rows", "request_count", "priced_subset_rows"):
        target[key] += int(source.get(key) or 0)
    for column in _TOKEN_COLUMNS:
        value = source.get(column)
        if value is not None:
            target[column] = int(target[column] or 0) + int(value)
        target[f"{column}_observed_rows"] += int(
            source.get(f"{column}_observed_rows") or 0
        )
    for column in (
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
    ):
        key = f"priced_subset_{column}"
        value = source.get(key)
        if value is not None:
            target[key] = int(target.get(key) or 0) + int(value)


def _normalize_metrics(
    origin: str, metrics: Mapping[str, Any]
) -> dict[str, Any]:
    return normalize_token_totals(
        origin,
        token_rows=int(metrics["token_rows"]),
        input_tokens=metrics["input_tokens"],
        output_tokens=metrics["output_tokens"],
        cache_read_tokens=metrics["cache_read_tokens"],
        cache_write_tokens=metrics["cache_write_tokens"],
        reasoning_tokens=metrics["reasoning_tokens"],
        input_observed_rows=int(metrics["input_tokens_observed_rows"]),
        output_observed_rows=int(metrics["output_tokens_observed_rows"]),
        cache_read_observed_rows=int(
            metrics["cache_read_tokens_observed_rows"]
        ),
        cache_write_observed_rows=int(
            metrics["cache_write_tokens_observed_rows"]
        ),
        reasoning_observed_rows=int(
            metrics["reasoning_tokens_observed_rows"]
        ),
        cache_write_1h_tokens=metrics["cache_write_1h_tokens"],
        cache_write_5m_tokens=metrics["cache_write_5m_tokens"],
        cache_write_1h_observed_rows=int(
            metrics["cache_write_1h_tokens_observed_rows"]
        ),
        cache_write_5m_observed_rows=int(
            metrics["cache_write_5m_tokens_observed_rows"]
        ),
        priced_subset_rows=int(metrics.get("priced_subset_rows") or 0),
        priced_subset_input_tokens=metrics.get("priced_subset_input_tokens"),
        priced_subset_cache_read_tokens=metrics.get(
            "priced_subset_cache_read_tokens"
        ),
        priced_subset_cache_write_tokens=metrics.get(
            "priced_subset_cache_write_tokens"
        ),
        priced_subset_output_tokens=metrics.get(
            "priced_subset_output_tokens"
        ),
    )


def _billing_category(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == _SUBSCRIPTION_BILLING_MODE:
        return BILLING_QUOTA
    if normalized in _UNKNOWN_BILLING_MODES:
        return BILLING_UNCLASSIFIED
    return BILLING_METERED


def _charge_for_breakdown(
    category: str,
    *,
    provider: str | None,
    model: str | None,
    origin: str | None,
    normalized: Mapping[str, Any],
    raw: Mapping[str, Any],
    pricing_cache: dict[tuple[str, str, str, str | None], _PriceVector],
) -> dict[str, Any]:
    equivalent = _price_normalized_usage(
        "equivalent",
        provider=provider,
        model=model,
        origin=origin,
        normalized=normalized,
        raw=raw,
        pricing_cache=pricing_cache,
    )
    if category == BILLING_UNCLASSIFIED:
        return {
            "kind": "unclassified",
            "amount_usd": None,
            "status": "billing_mode_unknown",
            "api_equivalent_usd": equivalent,
        }
    if category == BILLING_QUOTA:
        return {
            "kind": "quota",
            "marginal_usd": "0",
            "list_equivalent_usd": equivalent,
            "api_equivalent_usd": equivalent,
        }
    return {
        "kind": "metered",
        "metered_usd": _price_normalized_usage(
            "metered",
            provider=provider,
            model=model,
            origin=origin,
            normalized=normalized,
            raw=raw,
            pricing_cache=pricing_cache,
        ),
        "api_equivalent_usd": equivalent,
    }


def _price_priced_subset(
    kind: str,
    *,
    subset: Mapping[str, Any],
    provider: str | None,
    model: str | None,
    origin: str | None,
    pricing_cache: dict[tuple[str, str, str, str | None], _PriceVector],
) -> dict[str, Any]:
    """Price the fully observed subset of a mixed-observability group.

    Rows whose source lacks the cache-write field stay a named unknown
    subset instead of dragging the whole group into the all-or-nothing
    split gate.
    """
    cache_key = (kind, provider, model, origin)
    vector = pricing_cache.get(cache_key)
    if vector is None:
        estimator = (
            estimate_equivalent_cost
            if kind == "equivalent"
            else estimate_usage_cost
        )
        vector = _pricing_vector(
            estimator, provider=provider, model=model, origin=origin
        )
        pricing_cache[cache_key] = vector

    token_values = {
        "input_tokens": int(subset["uncached_input"] or 0),
        "output_tokens": int(subset["output_tokens"] or 0),
        "cache_read_tokens": int(subset["cache_read_tokens"] or 0),
        "cache_write_tokens": int(subset["cache_write_tokens"] or 0),
    }
    amount = Decimal("0")
    missing: list[str] = []
    for component, tokens in token_values.items():
        rate = vector.rates_per_million[component]
        if tokens and rate is None:
            missing.append(component)
        elif rate is not None:
            amount += Decimal(tokens) * rate / _ONE_MILLION
    if missing:
        return {
            **_price_metadata(vector),
            "amount_usd": None,
            "status": "unknown",
            "token_coverage": "partial",
            "reason": "pricing_component_unavailable",
            "unpriced_components": sorted(set(missing)),
            "priced_rows": 0,
            "unpriced_rows": int(subset["rows"])
            + int(subset["unpriced_rows"]),
        }
    return {
        **_price_metadata(vector),
        "amount_usd": _decimal_string(amount),
        "status": vector.status,
        "token_coverage": "partial",
        "reason": None,
        "unpriced_components": [],
        "priced_rows": int(subset["rows"]),
        "unpriced_rows": int(subset["unpriced_rows"]),
        "unpriced_reason": "cache_write_source_field_missing",
    }


def _price_normalized_usage(
    kind: str,
    *,
    provider: str | None,
    model: str | None,
    origin: str | None,
    normalized: Mapping[str, Any],
    raw: Mapping[str, Any],
    pricing_cache: dict[tuple[str, str, str, str | None], _PriceVector],
) -> dict[str, Any]:
    if not model:
        return _unknown_price("model_missing")
    if not provider:
        return _unknown_price("provider_missing")

    uncached = normalized["uncached_input"]
    if uncached["status"] != STATUS_EXACT:
        subset = normalized.get("priced_subset")
        if subset:
            return _price_priced_subset(
                kind,
                subset=subset,
                provider=provider,
                model=model,
                origin=origin,
                pricing_cache=pricing_cache,
            )
        return _unknown_price("input_cache_split_unavailable")

    cache_key = (kind, provider, model, origin)
    vector = pricing_cache.get(cache_key)
    if vector is None:
        estimator = (
            estimate_equivalent_cost
            if kind == "equivalent"
            else estimate_usage_cost
        )
        vector = _pricing_vector(
            estimator, provider=provider, model=model, origin=origin
        )
        pricing_cache[cache_key] = vector

    token_values = {
        "input_tokens": int(uncached["tokens"] or 0),
        "output_tokens": int(normalized["output_tokens"] or 0),
        "cache_read_tokens": int(normalized["cache_read_tokens"] or 0),
        "reasoning_tokens": int(normalized["reasoning_tokens"] or 0),
    }
    # Cache-write is priced from the TTL split where it was fully
    # observed, and only then from the total (register Leseregel-Nachtrag:
    # the split is the more complete observation).
    observed = normalized["observed_rows"]
    split_observed = (
        observed["token_rows"] > 0
        and observed["cache_write_1h"] == observed["token_rows"]
        and observed["cache_write_5m"] == observed["token_rows"]
    )
    if split_observed:
        token_values["cache_write_1h_tokens"] = int(
            normalized["cache_write_1h_tokens"] or 0
        )
        token_values["cache_write_5m_tokens"] = int(
            normalized["cache_write_5m_tokens"] or 0
        )
    else:
        token_values["cache_write_tokens"] = int(
            normalized["cache_write_tokens"] or 0
        )
    amount = Decimal("0")
    missing: list[str] = []
    for component, tokens in token_values.items():
        rate = vector.rates_per_million[component]
        if tokens and rate is None:
            missing.append(component)
        elif rate is not None:
            amount += Decimal(tokens) * rate / _ONE_MILLION
    if raw["request_count"] and vector.request_rate is None:
        missing.append("request_count")
    elif vector.request_rate is not None:
        amount += Decimal(raw["request_count"]) * vector.request_rate

    if missing:
        return {
            **_price_metadata(vector),
            "amount_usd": None,
            "status": "unknown",
            "token_coverage": _price_token_coverage(normalized),
            "reason": "pricing_component_unavailable",
            "unpriced_components": sorted(set(missing)),
        }
    return {
        **_price_metadata(vector),
        "amount_usd": _decimal_string(amount),
        "status": vector.status,
        "token_coverage": _price_token_coverage(normalized),
        "reason": None,
        "unpriced_components": [],
    }


def _pricing_vector(
    estimator: Callable[..., CostResult],
    *,
    provider: str,
    model: str,
    origin: str | None,
) -> _PriceVector:
    rates: dict[str, Decimal | None] = {}
    results: list[CostResult] = []
    for component in _PRICE_COMPONENTS:
        usage_kwargs = {name: 0 for name in _PRICE_COMPONENTS}
        # Probes must not claim an observed TTL split: a 0 split would
        # route the cache_write probe through the split path and hide
        # the total-column rate.
        usage_kwargs["cache_write_1h_tokens"] = None
        usage_kwargs["cache_write_5m_tokens"] = None
        usage_kwargs[component] = int(_ONE_MILLION)
        result = estimator(
            model,
            UsageFactsUsage(**usage_kwargs, request_count=0),
            provider=provider,
            origin=origin,
        )
        results.append(result)
        rates[component] = result.amount_usd

    request_result = estimator(
        model,
        UsageFactsUsage(request_count=1),
        provider=provider,
        origin=origin,
    )
    results.append(request_result)
    representative = next(
        (result for result in results if result.source != "none"),
        results[0],
    )
    statuses = {
        result.status for result in results if result.status != "unknown"
    }
    if not statuses:
        status = "unknown"
    elif "equivalent" in statuses:
        status = "equivalent"
    elif "estimated" in statuses:
        status = "estimated"
    elif statuses == {"included"}:
        status = "included"
    else:
        status = sorted(statuses)[0]
    return _PriceVector(
        rates_per_million=rates,
        request_rate=request_result.amount_usd,
        status=status,
        source=representative.source,
        fetched_at=(
            representative.fetched_at.isoformat()
            if representative.fetched_at is not None
            else None
        ),
        pricing_version=representative.pricing_version,
        notes=tuple(
            dict.fromkeys(
                note for result in results for note in result.notes
            )
        ),
    )


def _unknown_price(reason: str) -> dict[str, Any]:
    return {
        "amount_usd": None,
        "status": "unknown",
        "token_coverage": "unavailable",
        "source": "none",
        "fetched_at": None,
        "pricing_version": None,
        "notes": [],
        "reason": reason,
        "unpriced_components": [],
    }


def _price_metadata(vector: _PriceVector) -> dict[str, Any]:
    return {
        "source": vector.source,
        "fetched_at": vector.fetched_at,
        "pricing_version": vector.pricing_version,
        "notes": list(vector.notes),
    }


def _price_token_coverage(normalized: Mapping[str, Any]) -> str:
    return (
        "complete"
        if normalized["context_input"]["status"] == STATUS_EXACT
        and normalized["new_input"]["status"] == STATUS_EXACT
        else "lower_bound"
    )


def _billing_rollup(
    breakdowns: Iterable[Mapping[str, Any]],
    *,
    include_empty: bool = True,
) -> dict[str, Any]:
    items = list(breakdowns)
    result = {
        category: _billing_category_rollup(
            category,
            [item for item in items if item["category"] == category],
        )
        for category in (
            BILLING_METERED,
            BILLING_QUOTA,
            BILLING_UNCLASSIFIED,
        )
        if include_empty
        or any(item["category"] == category for item in items)
    }
    result["api_equivalent_usd"] = _sum_prices(
        item["charge"]["api_equivalent_usd"] for item in items
    )
    return result


def _billing_category_rollup(
    category: str,
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    tokens = _sum_normalized_tokens(item["tokens"] for item in items)
    result: dict[str, Any] = {
        "fact_rows": sum(int(item["fact_rows"]) for item in items),
        "tokens": tokens,
        "api_equivalent_usd": _sum_prices(
            item["charge"]["api_equivalent_usd"] for item in items
        ),
    }
    if category == BILLING_QUOTA:
        prices = [
            item["charge"]["list_equivalent_usd"] for item in items
        ]
        result["marginal_usd"] = "0"
        result["list_equivalent_usd"] = _sum_prices(prices)
    elif category == BILLING_METERED:
        prices = [item["charge"]["metered_usd"] for item in items]
        result["metered_usd"] = _sum_prices(prices)
    else:
        result["reason"] = "billing_mode_unknown"
    return result


def _sum_prices(prices: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(prices)
    if not items:
        return {
            "known_amount_usd": None,
            "status": "unknown",
            "priced_breakdowns": 0,
            "unpriced_breakdowns": 0,
            "lower_bound_breakdowns": 0,
        }
    known = [
        Decimal(str(item["amount_usd"]))
        for item in items
        if item.get("amount_usd") is not None
    ]
    unknown_count = sum(
        1 for item in items if item.get("amount_usd") is None
    )
    lower_bound_count = sum(
        1
        for item in items
        if item.get("amount_usd") is not None
        and item.get("token_coverage") != "complete"
    )
    # Named row-level partial coverage from priced-subset breakdowns:
    # rows whose source lacks a token field stay unknown but counted.
    priced_rows = sum(int(item.get("priced_rows") or 0) for item in items)
    unpriced_rows = sum(
        int(item.get("unpriced_rows") or 0) for item in items
    )
    return {
        "known_amount_usd": (
            _decimal_string(sum(known, Decimal("0"))) if known else None
        ),
        "status": (
            "unknown"
            if items and not known
            else "partial"
            if unknown_count or lower_bound_count or unpriced_rows
            else "complete"
        ),
        "priced_breakdowns": len(known),
        "unpriced_breakdowns": unknown_count,
        "lower_bound_breakdowns": lower_bound_count,
        "priced_rows": priced_rows,
        "unpriced_rows": unpriced_rows,
    }


def _coverage_ratio(observed_rows: int, denominator_rows: int) -> dict[str, Any]:
    return {
        "observed_rows": observed_rows,
        "denominator_rows": denominator_rows,
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


def _attribution_coverage(row: sqlite3.Row) -> dict[str, Any]:
    fact_rows = int(row["fact_rows"] or 0)
    return {
        "fact_rows": fact_rows,
        "latest_captured_at": row["latest_captured_at"],
        "tokens": _coverage_ratio(int(row["token_rows"] or 0), fact_rows),
        "model": _coverage_ratio(int(row["model_rows"] or 0), fact_rows),
        "exact_task_run": _coverage_ratio(
            int(row["exact_task_run_rows"] or 0),
            fact_rows,
        ),
        "exact_task": _coverage_ratio(
            int(row["exact_task_rows"] or 0),
            fact_rows,
        ),
        "exact_chain": _coverage_ratio(
            int(row["exact_chain_rows"] or 0),
            fact_rows,
        ),
        "task_only_rows": int(row["task_only_rows"] or 0),
    }


def _add_attributed_atom(
    builders: dict[Any, dict[str, Any]],
    key: Any,
    key_payload: Mapping[str, Any],
    atom: Mapping[str, Any],
    run_identity: str | None = None,
) -> None:
    builder = builders.setdefault(
        key,
        {
            "key": dict(key_payload),
            "fact_rows": 0,
            "run_count": 0,
            "run_identities": set(),
            "token_rows": 0,
            "model_rows": 0,
            "tokens": [],
            "billing": [],
            "latest_captured_at": None,
        },
    )
    for field in ("fact_rows", "token_rows", "model_rows"):
        builder[field] += int(atom[field])
    if run_identity is None:
        builder["run_count"] += int(atom["run_count"])
    else:
        builder["run_identities"].add(run_identity)
    builder["tokens"].append(atom["tokens"])
    builder["billing"].append(atom["billing"])
    captured_at = atom["latest_captured_at"]
    if captured_at and (
        builder["latest_captured_at"] is None
        or captured_at > builder["latest_captured_at"]
    ):
        builder["latest_captured_at"] = captured_at


def _finish_attributed_buckets(
    builders: Mapping[Any, Mapping[str, Any]],
    *,
    observed_at: datetime,
    limit: int,
    include_cost_population: bool = False,
) -> dict[str, Any]:
    buckets: list[dict[str, Any]] = []
    for builder in builders.values():
        latest = builder["latest_captured_at"]
        latest_datetime = _parse_utc_datetime(latest)
        age_seconds = (
            max(0, int((observed_at - latest_datetime).total_seconds()))
            if latest_datetime is not None
            else None
        )
        buckets.append(
            {
                "key": builder["key"],
                "fact_rows": builder["fact_rows"],
                "run_count": (
                    len(builder["run_identities"])
                    if builder["run_identities"]
                    else builder["run_count"]
                ),
                "tokens": _sum_normalized_tokens(builder["tokens"]),
                "billing": _billing_rollup(
                    builder["billing"],
                    include_empty=False,
                ),
                "coverage": {
                    "tokens": _coverage_ratio(
                        builder["token_rows"],
                        builder["fact_rows"],
                    ),
                    "model": _coverage_ratio(
                        builder["model_rows"],
                        builder["fact_rows"],
                    ),
                },
                "freshness": {
                    "latest_captured_at": latest,
                    "age_seconds": age_seconds,
                    "status": (
                        "unknown"
                        if age_seconds is None
                        else "fresh"
                        if age_seconds <= 24 * 60 * 60
                        else "stale"
                    ),
                },
            }
        )
    def sort_key(bucket: Mapping[str, Any]) -> tuple[Any, ...]:
        context_tokens = bucket["tokens"]["context_input_tokens"]
        return (
            context_tokens is None,
            -int(context_tokens or 0),
            -int(bucket["fact_rows"]),
            json.dumps(bucket["key"], sort_keys=True),
        )

    buckets.sort(key=sort_key)
    result: dict[str, Any] = {
        "total_buckets": len(buckets),
        "returned_buckets": min(len(buckets), limit),
        "truncated": len(buckets) > limit,
        "sort": "context_input_tokens_desc",
        "buckets": buckets[:limit],
    }
    if include_cost_population:
        # This private-to-the-fleet call path deliberately uses every task
        # bucket, not the display limit.  The fleet readmodel removes the
        # collection before serializing its public contract.
        result["_cost_population"] = [
            {
                "key": dict(bucket["key"]),
                "amount_usd": (
                    (bucket.get("billing") or {})
                    .get("api_equivalent_usd", {})
                    .get("known_amount_usd")
                ),
            }
            for bucket in buckets
        ]
    return result


def _derived_observed_rows(item: Mapping[str, Any], metric: str) -> int:
    observed = item.get("observed_rows") or {}
    semantics = item.get("semantics")
    if metric == "context_input_tokens":
        required = (
            ("input", "cache_read", "cache_write")
            if semantics == INPUT_CACHE_EXCLUSIVE
            else ("input",)
        )
    else:
        required = (
            ("input", "cache_write")
            if semantics == INPUT_CACHE_EXCLUSIVE
            else ("input", "cache_read")
        )
    if semantics == INPUT_NOT_DERIVABLE:
        return 0
    return min(int(observed.get(name) or 0) for name in required)


def aggregate_normalized_tokens(
    normalized_items: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Sum normalized token facts without manufacturing zeroes for missing data."""
    items = list(normalized_items)
    specs = {
        "raw_input_tokens": (
            lambda item: item.get("raw_input_tokens"),
            lambda item: int((item.get("observed_rows") or {}).get("input") or 0),
        ),
        "context_input_tokens": (
            lambda item: (item.get("context_input") or {}).get("tokens"),
            lambda item: _derived_observed_rows(item, "context_input_tokens"),
        ),
        "new_input_tokens": (
            lambda item: (item.get("new_input") or {}).get("tokens"),
            lambda item: _derived_observed_rows(item, "new_input_tokens"),
        ),
        "cache_read_tokens": (
            lambda item: item.get("cache_read_tokens"),
            lambda item: int((item.get("observed_rows") or {}).get("cache_read") or 0),
        ),
        "cache_write_tokens": (
            lambda item: item.get("cache_write_tokens"),
            lambda item: int((item.get("observed_rows") or {}).get("cache_write") or 0),
        ),
        "output_tokens": (
            lambda item: item.get("output_tokens"),
            lambda item: int((item.get("observed_rows") or {}).get("output") or 0),
        ),
        "reasoning_tokens": (
            lambda item: item.get("reasoning_tokens"),
            lambda item: int((item.get("observed_rows") or {}).get("reasoning") or 0),
        ),
    }
    denominator = sum(
        int((item.get("observed_rows") or {}).get("token_rows") or 0)
        for item in items
    )
    result: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    for metric, (value_of, observed_of) in specs.items():
        values = [value_of(item) for item in items]
        known_values = [int(value) for value in values if value is not None]
        observed_rows = sum(observed_of(item) for item in items)
        status = (
            "complete"
            if denominator > 0 and observed_rows >= denominator
            else "partial"
            if observed_rows > 0
            else "unknown"
        )
        result[metric] = sum(known_values) if known_values else None
        coverage[metric] = {
            "observed_rows": observed_rows,
            "denominator_rows": denominator,
            "status": status,
        }
    result["coverage"] = coverage
    result["has_lower_bounds"] = any(
        item.get("context_input", {}).get("status") != STATUS_EXACT
        or item.get("new_input", {}).get("status") != STATUS_EXACT
        for item in items
    )
    return result


def _sum_normalized_tokens(
    normalized_items: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    return aggregate_normalized_tokens(normalized_items)


def _payload_summary(groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tokens = _sum_normalized_tokens(group["tokens"] for group in groups)
    all_breakdowns = [
        breakdown
        for group in groups
        for breakdown in group["_billing_breakdown"]
    ]
    return {
        "group_count": len(groups),
        "fact_rows": sum(int(group["fact_rows"]) for group in groups),
        "raw_input_tokens": tokens["raw_input_tokens"],
        "tokens": tokens,
        "billing": _billing_rollup(all_breakdowns),
    }


def _unattributed_payload(
    groups: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    unattributed = [group for group in groups if group["key"]["model"] is None]
    by_origin: list[dict[str, Any]] = []
    for origin in sorted({group["key"]["origin"] for group in unattributed}):
        selected = [
            group for group in unattributed if group["key"]["origin"] == origin
        ]
        by_origin.append(
            {
                "origin": origin,
                "fact_rows": sum(int(group["fact_rows"]) for group in selected),
                "tokens": _sum_normalized_tokens(
                    group["tokens"] for group in selected
                ),
            }
        )
    return {
        "label": UNATTRIBUTED_MODEL_LABEL,
        "reason": "model_missing",
        "fact_rows": sum(int(group["fact_rows"]) for group in unattributed),
        "tokens": _sum_normalized_tokens(
            group["tokens"] for group in unattributed
        ),
        "by_origin": by_origin,
    }


def _kanban_projection(
    *,
    kanban_path: Path | None,
    usage_facts_path: Path,
    profiles_root: Path | None,
    immutable_evidence: bool,
) -> dict[str, Any]:
    if kanban_path is None or not kanban_path.is_file():
        return {
            "available": False,
            "reason": "kanban_database_unavailable",
        }
    facts = resolve_active_provider_facts(
        kanban_path,
        usage_facts_path=usage_facts_path,
        profiles_root=profiles_root,
        immutable_kanban=immutable_evidence,
        immutable_state=immutable_evidence,
        immutable_usage_facts=immutable_evidence,
    )
    run_ids = [str(fact.run_id) for fact in facts]
    fact_rows, token_rows = _usage_coverage_by_run(
        usage_facts_path, run_ids
    )
    attribution = Counter(
        (fact.classification, fact.provider) for fact in facts
    )
    projection = {
        "available": True,
        "scope": "all_board_runs",
        "total_runs": len(facts),
        "provider_classification": classification_counts(facts),
        "reconstruction_sources": reconstruction_source_counts(facts),
        "by_classification_and_provider": [
            {
                "classification": classification,
                "provider": provider,
                "provider_label": provider or UNATTRIBUTED_MODEL_LABEL,
                "runs": count,
            }
            for (classification, provider), count in sorted(
                attribution.items(),
                key=lambda item: (item[0][0], item[0][1] or ""),
            )
        ],
        "usage_coverage": {
            "token_bearing_runs": len(token_rows),
            "provider_only_fact_runs": len(fact_rows - token_rows),
            "runs_without_fact": len(facts) - len(fact_rows),
            "state": (
                "thin"
                if len(token_rows) < max(1, len(facts) // 2)
                else "covered"
            ),
        },
    }
    calibration = _route_change_calibration(kanban_path)
    if calibration is not None:
        projection["route_change_calibration"] = calibration
    return projection


def _route_change_calibration(kanban_path: Path) -> dict[str, Any] | None:
    """Measure model/provider route changes from their authoritative events."""

    try:
        with _read_only_connection(kanban_path) as connection:
            rows = connection.execute(
                """
                SELECT payload
                  FROM task_events
                 WHERE kind = 'model_route_changed'
                """
            ).fetchall()
    except sqlite3.OperationalError:
        # Older/incomplete board snapshots cannot establish this calibration.
        return None

    observed_events = 0
    route_changed_events = 0
    for row in rows:
        routes = _event_routes(row["payload"])
        if routes is None:
            continue
        old_route, new_route = routes
        observed_events += 1
        if old_route != new_route:
            route_changed_events += 1

    return {
        "source": "task_events.kind=model_route_changed",
        "scope": "all events with complete old and new provider/model routes",
        "observed_events": observed_events,
        "route_changed_events": route_changed_events,
        "route_unchanged_events": observed_events - route_changed_events,
        "route_change_rate": (
            route_changed_events / observed_events
            if observed_events
            else None
        ),
    }


def _event_routes(
    payload: Any,
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    try:
        event = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(event, Mapping):
        return None
    old_route = _complete_route(event.get("old"))
    new_route = _complete_route(event.get("new"))
    if old_route is None or new_route is None:
        return None
    return old_route, new_route


def _complete_route(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    provider = value.get("provider")
    model = value.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    provider = provider.strip()
    model = model.strip()
    return (provider, model) if provider and model else None


def _usage_coverage_by_run(
    usage_facts_path: Path,
    run_ids: Sequence[str],
) -> tuple[set[str], set[str]]:
    fact_rows: set[str] = set()
    token_rows: set[str] = set()
    if not run_ids:
        return fact_rows, token_rows
    with _read_only_connection(usage_facts_path) as connection:
        for start in range(0, len(run_ids), 500):
            chunk = run_ids[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT run_id,
                       CASE WHEN {
                           " OR ".join(
                               f"{column} IS NOT NULL"
                               for column in _TOKEN_COLUMNS
                           )
                       } THEN 1 ELSE 0 END AS has_tokens
                  FROM run_usage_facts
                 WHERE run_id IN ({placeholders})
                """,
                chunk,
            )
            for row in rows:
                run_id = str(row["run_id"])
                fact_rows.add(run_id)
                if row["has_tokens"]:
                    token_rows.add(run_id)
    return fact_rows, token_rows


def _rate_limit_projection(
    path: Path,
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    """Stream the sidecar and retain only the newest valid snapshot per origin."""

    latest: dict[str, tuple[datetime, str, Mapping[str, Any]]] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                origin = record.get("origin")
                captured_at = record.get("captured_at")
                rate_limits = record.get("rate_limits")
                if (
                    not isinstance(origin, str)
                    or not isinstance(captured_at, str)
                    or not isinstance(rate_limits, Mapping)
                ):
                    continue
                origin = origin.strip()
                captured = _parse_utc_datetime(captured_at)
                if not origin or captured is None:
                    continue
                current = latest.get(origin)
                if current is None or captured > current[0]:
                    latest[origin] = (captured, captured_at, rate_limits)
    except OSError:
        return {
            "available": False,
            "reason": "sidecar_unavailable",
            "snapshots": {},
        }

    snapshots: dict[str, dict[str, Any]] = {}
    for origin in sorted(latest):
        captured, captured_at, rate_limits = latest[origin]
        age_seconds = max(0, int((observed_at - captured).total_seconds()))
        snapshots[origin] = {
            "captured_at": captured_at,
            "age_seconds": age_seconds,
            "freshness": (
                "stale"
                if age_seconds >= RATE_LIMIT_STALE_AFTER_SECONDS
                else "fresh"
            ),
            "rate_limits": dict(rate_limits),
        }
    return {"available": True, "snapshots": snapshots}


def _parse_utc_datetime(value: str | None) -> datetime | None:
    # Rows written before the capture timestamp was mandatory still carry
    # NULL, and a bucket built only from those has no latest timestamp at all.
    # An unparsable stamp is an unknown age, never an exception.
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    absolute = path.expanduser().resolve()
    uri = f"file:{quote(str(absolute), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        yield connection
    finally:
        connection.close()


def _sort_group_key(
    key: tuple[str, str, str | None, str | None, str | None],
) -> tuple[str, str, str, str, str]:
    return tuple(part or "" for part in key)


def _decimal_string(value: Decimal) -> str:
    """Serialize the exact derived decimal without float conversion/rounding."""

    return format(value, "f")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

"""Read-only source adapters for the universal execution-facts ledger.

These adapters deliberately consume injected SQLite connections or already
parsed records.  They never alter a source database, invoke a process manager,
capture terminal content, or read command lines.  Reconciliation is therefore
safe to run after source work has committed and can be repeated idempotently.
"""

from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
import time
from typing import Any

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionEvent,
    ExecutionSurface,
    LifecyclePhase,
    SpanKind,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_execution_id,
    stable_idempotency_key,
    stable_span_id,
)
from hermes_cli.execution_facts_ledger import AppendResult, ExecutionFactsLedger
from hermes_cli.usage_facts_pricing import (
    CostResult,
    UsageFactsUsage,
    estimate_equivalent_cost,
    estimate_usage_cost,
)
from hermes_cli.usage_facts_readmodel import (
    INPUT_CACHE_EXCLUSIVE,
    INPUT_CACHE_INCLUSIVE,
    input_semantics,
)

_KANBAN_PHASES = {
    "queued": LifecyclePhase.QUEUED,
    "claimed": LifecyclePhase.CLAIMED,
    "spawn_started": LifecyclePhase.SPAWN_STARTED,
    "process_started": LifecyclePhase.PROCESS_STARTED,
    "first_llm_request": LifecyclePhase.FIRST_REQUEST,
    "first_token": LifecyclePhase.FIRST_TOKEN,
    "ended": LifecyclePhase.ENDED,
}
_LOOP_PHASES = {
    "plan": LifecyclePhase.RUNNING,
    "build": LifecyclePhase.RUNNING,
    "verify": LifecyclePhase.REVIEW_STARTED,
    "review": LifecyclePhase.REVIEW_STARTED,
    "land": LifecyclePhase.INTEGRATION_STARTED,
}
_SAFE_CLASS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,126}$")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cursor = connection.execute(sql)
    names = [description[0] for description in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _safe_class(value: object | None) -> str | None:
    text = str(value or "").strip()
    return text if _SAFE_CLASS.fullmatch(text) else None


def _epoch_ms(value: object | None) -> int | None:
    """Normalize seconds, milliseconds, or an ISO-8601 timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric if numeric >= 10_000_000_000 else numeric * 1000
    text = str(value).strip()
    try:
        numeric = int(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1000)
    return numeric if numeric >= 10_000_000_000 else numeric * 1000


def _decimal_string(value: object | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return None


def _content_revision_token(value: Mapping[str, Any]) -> str:
    """Version a mutable source row without putting its payload in the key."""

    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return stable_idempotency_key("execution-facts-content-revision.v1", canonical)


def _make_source_event(
    *,
    source: str,
    source_execution_id: str,
    event_key_parts: Sequence[object],
    event_type: EventType,
    validity: Validity,
    observed_at_ms: int,
    workload_kind: WorkloadKind,
    execution_surface: ExecutionSurface,
    trigger_kind: TriggerKind,
    execution_id: str | None = None,
    **fields: Any,
) -> ExecutionEvent:
    idempotency_key = stable_idempotency_key(
        source, source_execution_id, *(str(part) for part in event_key_parts)
    )
    return make_event(
        source=source,
        source_execution_id=source_execution_id,
        execution_id=execution_id
        or stable_execution_id(source, source_execution_id),
        idempotency_key=idempotency_key,
        event_type=event_type,
        validity=validity,
        observed_at_ms=observed_at_ms,
        # The first append persists the actual reconciliation observation
        # time. Later retries dedupe on immutable source facts.
        recorded_at_ms=int(time.time() * 1000),
        workload_kind=workload_kind,
        execution_surface=execution_surface,
        trigger_kind=trigger_kind,
        **fields,
    )


def _usage_surface(origin: str) -> ExecutionSurface:
    return {
        "hermes_agent": ExecutionSurface.HERMES,
        "hermes_aux": ExecutionSurface.HERMES,
        "claude_code": ExecutionSurface.CLAUDE_CLI,
        "codex_cli": ExecutionSurface.CODEX_CLI,
        "grok_cli": ExecutionSurface.GROK_CLI,
        "kimi_cli": ExecutionSurface.KIMI_CLI,
        "qwen_cli": ExecutionSurface.QWEN_CLI,
    }.get(origin, ExecutionSurface.UNKNOWN)


def _canonical_usage(row: Mapping[str, Any]) -> tuple[UsageFactsUsage, Validity]:
    origin = str(row.get("origin") or "unknown")
    input_tokens = row.get("input_tokens")
    output_tokens = row.get("output_tokens")
    cache_read = row.get("cache_read_tokens")
    cache_write = row.get("cache_write_tokens")
    cache_write_1h = row.get("cache_write_1h_tokens")
    cache_write_5m = row.get("cache_write_5m_tokens")
    reasoning = row.get("reasoning_tokens")
    if input_tokens is None or output_tokens is None:
        return UsageFactsUsage(request_count=0), Validity.UNKNOWN
    semantics = input_semantics(origin)
    if semantics not in {INPUT_CACHE_EXCLUSIVE, INPUT_CACHE_INCLUSIVE}:
        return UsageFactsUsage(request_count=0), Validity.UNKNOWN
    if (
        semantics == INPUT_CACHE_INCLUSIVE
        and (cache_read is None or cache_write is None)
    ):
        # The total context is known, but its price buckets are not. Treating
        # the entire inclusive input as standard-rate input would be neither an
        # exact value nor a lower bound, so the canonical/pricing view stays
        # unknown while the source-native fields remain on the usage event.
        return UsageFactsUsage(request_count=0), Validity.UNKNOWN
    normalized_input = int(input_tokens)
    if semantics == INPUT_CACHE_INCLUSIVE:
        normalized_input = max(
            0,
            normalized_input - int(cache_read) - int(cache_write),
        )
    validity = (
        Validity.EXACT
        if cache_read is not None and cache_write is not None
        else Validity.LOWER_BOUND
    )
    return (
        UsageFactsUsage(
            input_tokens=normalized_input,
            output_tokens=int(output_tokens),
            cache_read_tokens=int(cache_read or 0),
            cache_write_tokens=int(cache_write or 0),
            # TTL split stays None unless observed; pricing falls back to
            # the total where the split is absent (register Leseregel).
            cache_write_1h_tokens=(
                int(cache_write_1h) if cache_write_1h is not None else None
            ),
            cache_write_5m_tokens=(
                int(cache_write_5m) if cache_write_5m is not None else None
            ),
            reasoning_tokens=int(reasoning or 0),
        ),
        validity,
    )


def _cost_amount(result: CostResult) -> str | None:
    return (
        format(result.amount_usd, "f")
        if result.amount_usd is not None
        else None
    )


def _allocation_key(row: Mapping[str, Any]) -> str | None:
    captured_at = _epoch_ms(row.get("captured_at"))
    if captured_at is None:
        return None
    return datetime.fromtimestamp(captured_at / 1000).strftime("%Y-%m")


def _subscription_fee_key(
    fees: Mapping[str, Decimal],
    *,
    month: str,
    provider: str | None,
    origin: str,
) -> str | None:
    candidates = (
        f"{month}:{provider}" if provider is not None else None,
        f"{month}:{origin}",
        f"{month}:*",
        provider,
        origin,
        "*",
    )
    return next(
        (candidate for candidate in candidates if candidate in fees),
        None,
    )


def reconcile_usage_facts(
    connection: sqlite3.Connection,
    *,
    subscription_fees: Mapping[str, Decimal | str] | None = None,
    fee_version: str | None = None,
    limit: int | None = None,
    known_task_run_ids: Collection[str] | None = None,
    equivalent_estimator: Callable[..., CostResult] = estimate_equivalent_cost,
    actual_estimator: Callable[..., CostResult] = estimate_usage_cost,
) -> list[ExecutionEvent]:
    """Bridge existing nullable usage facts into usage and cost events.

    ``subscription_fees`` is explicit operator configuration keyed by provider,
    origin, or ``"*"``. The monthly fee is allocated within each calendar
    month in proportion to known API-equivalent value. Missing prices or fees
    stay missing; they are never converted to zero.
    """
    if not _table_exists(connection, "run_usage_facts"):
        return []
    if limit is not None and limit < 1:
        raise ValueError("usage reconciliation limit must be positive")
    if subscription_fees and limit is not None:
        raise ValueError(
            "subscription fee allocation requires an unbounded monthly cohort"
        )
    # The cache-write TTL split exists since the S10 fact layer; older
    # shapes of the table simply have no split to price.
    has_split_columns = connection.execute(
        "SELECT 1 FROM pragma_table_info('run_usage_facts') "
        "WHERE name='cache_write_1h_tokens'"
    ).fetchone()
    split_select = (
        "cache_write_1h_tokens, cache_write_5m_tokens,"
        if has_split_columns
        else "NULL AS cache_write_1h_tokens, NULL AS cache_write_5m_tokens,"
    )
    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
    order_direction = "DESC" if limit is not None else "ASC"
    rows = _rows(
        connection,
        f"""
        SELECT run_id, origin, task_run_id, task_id, chain_id, board,
               provider, model, profile, billing_mode, serving_tier,
               reasoning_effort, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens,
               {split_select}
               reasoning_tokens,
               finish_reason, error_type, duration_ms, captured_at, source
          FROM run_usage_facts
         ORDER BY captured_at {order_direction}, origin, run_id
         {limit_sql}
        """,
    )
    priced: list[dict[str, Any]] = []
    fees = {
        key: Decimal(str(value))
        for key, value in (subscription_fees or {}).items()
    }
    if fees and not fee_version:
        raise ValueError("fee_version is required with subscription_fees")
    if any(value < 0 for value in fees.values()):
        raise ValueError("subscription fees must be non-negative")
    for row in rows:
        usage, usage_validity = _canonical_usage(row)
        source_validity = {
            "measured": Validity.EXACT,
            "derived": Validity.DERIVED,
            "unknown": Validity.UNKNOWN,
        }.get(str(row.get("source") or "unknown"), Validity.UNKNOWN)
        if source_validity in {Validity.DERIVED, Validity.UNKNOWN}:
            usage_validity = source_validity
        model = _safe_class(row.get("model"))
        provider = _safe_class(row.get("provider"))
        origin = _safe_class(row.get("origin"))
        equivalent = (
            equivalent_estimator(
                model, usage, provider=provider, origin=origin
            )
            if model is not None and usage_validity is not Validity.UNKNOWN
            else CostResult(None, "unknown", "none", "n/a")
        )
        actual = (
            actual_estimator(
                model, usage, provider=provider, origin=origin
            )
            if model is not None and usage_validity is not Validity.UNKNOWN
            else CostResult(None, "unknown", "none", "n/a")
        )
        priced.append(
            {
                "row": row,
                "usage": usage,
                "usage_validity": usage_validity,
                "model": model,
                "provider": provider,
                "equivalent": equivalent,
                "actual": actual,
                "allocated": None,
                "allocation_month": None,
                "allocation_population": None,
                "allocation_fee": None,
            }
        )

    allocation_groups: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = {}
    for item in priced:
        row = item["row"]
        if str(row.get("billing_mode") or "") != "subscription_included":
            continue
        month = _allocation_key(row)
        fee_key = _subscription_fee_key(
            fees,
            month=month,
            provider=item["provider"],
            origin=str(row.get("origin") or "unknown"),
        )
        if month is None or fee_key is None:
            continue
        allocation_groups.setdefault((month, fee_key), []).append(item)
    for (_month, fee_key), items in allocation_groups.items():
        # A monthly fee belongs to the complete eligible group. If even one
        # member lacks an API-equivalent basis, allocating the full fee across
        # the known subset would overstate those runs and lie about population.
        if any(item["equivalent"].amount_usd is None for item in items):
            continue
        total_equivalent = sum(
            (item["equivalent"].amount_usd for item in items),
            Decimal(0),
        )
        if total_equivalent <= 0:
            continue
        fee = fees[fee_key]
        assigned = Decimal(0)
        positive_items = [
            item
            for item in items
            if item["equivalent"].amount_usd > 0
        ]
        last_positive = positive_items[-1]
        for item in items:
            if item is last_positive:
                allocation = fee - assigned
            elif item["equivalent"].amount_usd == 0:
                allocation = Decimal(0)
            else:
                allocation = (
                    fee * item["equivalent"].amount_usd / total_equivalent
                ).quantize(Decimal("0.00000001"))
                assigned += allocation
            item["allocated"] = allocation
            item["allocation_month"] = _month
            item["allocation_population"] = len(items)
            item["allocation_fee"] = fee

    events: list[ExecutionEvent] = []
    for item in priced:
        row = item["row"]
        observed_at_ms = _epoch_ms(row.get("captured_at"))
        if observed_at_ms is None:
            continue
        origin = _safe_class(row.get("origin")) or "unknown"
        run_id = str(row["run_id"])
        run_identity = stable_idempotency_key(
            "usage-run-identity.v1",
            origin,
            run_id,
        )
        source_execution_id = f"usage:{origin}:{run_identity}"
        task_run_id = _safe_class(row.get("task_run_id"))
        if task_run_id is None and known_task_run_ids and run_id in known_task_run_ids:
            # Some writers record the kanban run in `run_id` and leave
            # `task_run_id` empty. Without this the spend forms its own
            # execution instead of joining the work it paid for -- 346 live
            # rows, and 16.5% of one chain's cost. Membership in the real
            # run set is required: only 346 of 1720 numeric run_ids are
            # task runs, so the shape of the id proves nothing by itself.
            task_run_id = _safe_class(run_id)
        execution_id = (
            stable_execution_id(
                "kanban_timeline", f"task_run:{task_run_id}"
            )
            if task_run_id is not None
            else stable_execution_id("usage_facts", source_execution_id)
        )
        usage = item["usage"]
        attributes: dict[str, Any] = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            value = row.get(key)
            if value is not None:
                attributes[key] = int(value)
        if item["usage_validity"] is not Validity.UNKNOWN:
            attributes["total_tokens"] = usage.total_tokens
        for source_key, target_key in (
            ("model", "agent_model"),
            ("provider", "agent_provider"),
            ("billing_mode", "billing_mode"),
            ("serving_tier", "serving_tier"),
            ("reasoning_effort", "reasoning_effort"),
            ("finish_reason", "finish_reason"),
        ):
            value = _safe_class(row.get(source_key))
            if value is not None:
                attributes[target_key] = value
        duration = row.get("duration_ms")
        if duration is not None:
            attributes["duration_ms"] = max(0, int(float(duration)))
        usage_revision = _content_revision_token(
            {
                "attributes": attributes,
                "board": _safe_class(row.get("board")),
                "chain_id": _safe_class(row.get("chain_id")),
                "error_class": _safe_class(row.get("error_type")),
                "observed_at_ms": observed_at_ms,
                "profile": _safe_class(row.get("profile")),
                "task_id": _safe_class(row.get("task_id")),
                "task_run_id": task_run_id,
                "validity": item["usage_validity"].value,
            }
        )
        events.append(
            _make_source_event(
                source="usage_facts",
                source_execution_id=source_execution_id,
                execution_id=execution_id,
                event_key_parts=("usage", usage_revision),
                event_type=EventType.USAGE_OBSERVED,
                validity=item["usage_validity"],
                observed_at_ms=observed_at_ms,
                workload_kind=(
                    WorkloadKind.KANBAN_TASK
                    if task_run_id is not None
                    else WorkloadKind.INTERACTIVE
                ),
                execution_surface=_usage_surface(origin),
                trigger_kind=(
                    TriggerKind.KANBAN
                    if task_run_id is not None
                    else TriggerKind.RECONCILIATION
                ),
                task_run_id=task_run_id,
                task_id=_safe_class(row.get("task_id")),
                chain_id=_safe_class(row.get("chain_id")),
                board=_safe_class(row.get("board")),
                profile=_safe_class(row.get("profile")),
                error_class=_safe_class(row.get("error_type")),
                evidence_ref=f"usage:run_usage_facts:{run_identity}",
                attributes=attributes,
            )
        )
        cost_attributes: dict[str, Any] = {}
        equivalent_amount = _cost_amount(item["equivalent"])
        actual_amount = _cost_amount(item["actual"])
        if equivalent_amount is not None:
            cost_attributes["api_equivalent_cost"] = equivalent_amount
        billing_mode = str(row.get("billing_mode") or "")
        if actual_amount is not None:
            if billing_mode == "subscription_included":
                # The recorded billing mode outranks the pricing route's
                # guess: the route keys off the provider label, and Claude
                # Code records `anthropic`, which reads as metered API usage.
                # One more request on a flat subscription costs nothing
                # extra, whatever the list price would have been -- that
                # price stays in `api_equivalent_cost`, and the real money is
                # the monthly fee, attributed via
                # `allocated_subscription_cost`.
                cost_attributes["marginal_cost"] = "0"
            else:
                cost_attributes["metered_cost"] = actual_amount
                cost_attributes["marginal_cost"] = actual_amount
        if item["allocated"] is not None:
            cost_attributes["allocated_subscription_cost"] = format(
                item["allocated"], "f"
            )
            cost_attributes["allocation_formula"] = (
                "monthly_fee_x_run_api_equivalent_over_eligible_month_"
                "api_equivalent"
            )
            cost_attributes["allocation_month"] = item["allocation_month"]
            cost_attributes["population_size"] = item[
                "allocation_population"
            ]
            cost_attributes["fee_amount"] = format(
                item["allocation_fee"], "f"
            )
            if fee_version is not None:
                cost_attributes["fee_version"] = fee_version
        pricing_version = (
            item["equivalent"].pricing_version
            or item["actual"].pricing_version
        )
        if pricing_version is not None:
            cost_attributes["pricing_version"] = pricing_version
        if cost_attributes:
            cost_attributes["currency"] = "USD"
            cost_validity = (
                Validity.UNKNOWN
                if item["usage_validity"] is Validity.UNKNOWN
                else Validity.DERIVED
            )
            cost_revision = _content_revision_token(
                {
                    "attributes": cost_attributes,
                    "board": _safe_class(row.get("board")),
                    "chain_id": _safe_class(row.get("chain_id")),
                    "observed_at_ms": observed_at_ms,
                    "profile": _safe_class(row.get("profile")),
                    "task_id": _safe_class(row.get("task_id")),
                    "task_run_id": task_run_id,
                    "validity": cost_validity.value,
                }
            )
            events.append(
                _make_source_event(
                    source="usage_facts",
                    source_execution_id=source_execution_id,
                    execution_id=execution_id,
                    event_key_parts=("cost", cost_revision),
                    event_type=EventType.COST_OBSERVED,
                    validity=cost_validity,
                    observed_at_ms=observed_at_ms,
                    workload_kind=(
                        WorkloadKind.KANBAN_TASK
                        if task_run_id is not None
                        else WorkloadKind.INTERACTIVE
                    ),
                    execution_surface=_usage_surface(origin),
                    trigger_kind=(
                        TriggerKind.KANBAN
                        if task_run_id is not None
                        else TriggerKind.RECONCILIATION
                    ),
                    task_run_id=task_run_id,
                    task_id=_safe_class(row.get("task_id")),
                    chain_id=_safe_class(row.get("chain_id")),
                    board=_safe_class(row.get("board")),
                    profile=_safe_class(row.get("profile")),
                    evidence_ref=(
                        f"usage:run_usage_facts:{run_identity}:cost"
                    ),
                    attributes=cost_attributes,
                )
            )
    return events


def reconcile_kanban(connection: sqlite3.Connection) -> list[ExecutionEvent]:
    """Translate exact runtime tables plus honest legacy fallbacks."""
    events: list[ExecutionEvent] = []
    seen_phases: set[tuple[str, str]] = set()
    timeline_rows = (
        _rows(
            connection,
            """
            SELECT task_run_id, event_kind, observed_at_ms, source, task_id,
                   board, chain_root_id, profile
              FROM worker_run_timeline_events
             ORDER BY observed_at_ms, task_run_id, event_kind
            """,
        )
        if _table_exists(connection, "worker_run_timeline_events")
        else []
    )
    retry_link_rows = (
        _rows(
            connection,
            """
            SELECT task_run_id, retry_of_task_run_id, retry_class,
                   triggering_event_id, task_id, board
              FROM worker_run_retry_links
             ORDER BY task_run_id
            """,
        )
        if _table_exists(connection, "worker_run_retry_links")
        else []
    )
    retry_instrumented_run_ids = {
        str(row["task_run_id"]) for row in timeline_rows
    } | {str(row["task_run_id"]) for row in retry_link_rows}

    if timeline_rows:
        for row in timeline_rows:
            kind = str(row["event_kind"])
            phase = _KANBAN_PHASES.get(kind)
            observed_at_ms = _epoch_ms(row["observed_at_ms"])
            if phase is None or observed_at_ms is None:
                continue
            source_execution_id = f"task_run:{row['task_run_id']}"
            seen_phases.add((source_execution_id, phase.value))
            events.append(
                _make_source_event(
                    source="kanban_timeline",
                    source_execution_id=source_execution_id,
                    event_key_parts=("timeline", kind),
                    event_type=EventType.LIFECYCLE,
                    validity=Validity.EXACT,
                    observed_at_ms=observed_at_ms,
                    workload_kind=WorkloadKind.KANBAN_TASK,
                    execution_surface=ExecutionSurface.HERMES,
                    trigger_kind=TriggerKind.KANBAN,
                    lifecycle_phase=phase,
                    task_run_id=str(row["task_run_id"]),
                    task_id=str(row["task_id"]),
                    chain_id=(
                        str(row["chain_root_id"])
                        if row["chain_root_id"] is not None
                        else None
                    ),
                    profile=_safe_class(row["profile"]),
                    board=_safe_class(row["board"]),
                    evidence_ref=f"kanban:timeline:{row['task_run_id']}:{kind}",
                    attributes={"retry_instrumented": True},
                )
            )

    run_columns = _columns(connection, "task_runs")
    if {"id", "task_id", "started_at"} <= run_columns:
        selected = [
            name
            for name in (
                "id",
                "task_id",
                "profile",
                "status",
                "started_at",
                "ended_at",
                "outcome",
                "worker_exit_kind",
                "worker_exit_code",
            )
            if name in run_columns
        ]
        for row in _rows(
            connection,
            f"SELECT {', '.join(selected)} FROM task_runs ORDER BY id",
        ):
            source_execution_id = f"task_run:{row['id']}"
            retry_instrumented = (
                str(row["id"]) in retry_instrumented_run_ids
            )
            safe_profile = _safe_class(row.get("profile"))
            safe_status = _safe_class(row.get("status"))
            for column, phase in (
                ("started_at", LifecyclePhase.PROCESS_STARTED),
                ("ended_at", LifecyclePhase.ENDED),
            ):
                observed_at_ms = _epoch_ms(row.get(column))
                if (
                    observed_at_ms is None
                    or (source_execution_id, phase.value) in seen_phases
                ):
                    continue
                revision = _content_revision_token(
                    {
                        "column": column,
                        "observed_at_ms": observed_at_ms,
                        "profile": safe_profile,
                        "retry_instrumented": retry_instrumented,
                        "status": safe_status,
                        "task_id": str(row["task_id"]),
                    }
                )
                events.append(
                    _make_source_event(
                        source="kanban_timeline",
                        source_execution_id=source_execution_id,
                        event_key_parts=("legacy", column, revision),
                        event_type=EventType.LIFECYCLE,
                        validity=Validity.DERIVED,
                        observed_at_ms=observed_at_ms,
                        workload_kind=WorkloadKind.KANBAN_TASK,
                        execution_surface=ExecutionSurface.HERMES,
                        trigger_kind=TriggerKind.KANBAN,
                        lifecycle_phase=phase,
                        task_run_id=str(row["id"]),
                        task_id=str(row["task_id"]),
                        profile=safe_profile,
                        status=safe_status,
                        evidence_ref=f"kanban:task_runs:{row['id']}:{column}",
                        attributes={"retry_instrumented": retry_instrumented},
                    )
                )
            ended_at_ms = _epoch_ms(row.get("ended_at"))
            if ended_at_ms is not None:
                safe_outcome = _safe_class(
                    row.get("outcome") or row.get("status")
                )
                safe_error = _safe_class(row.get("worker_exit_kind"))
                exit_code = (
                    int(row["worker_exit_code"])
                    if row.get("worker_exit_code") is not None
                    else None
                )
                revision = _content_revision_token(
                    {
                        "ended_at_ms": ended_at_ms,
                        "error_class": safe_error,
                        "exit_code": exit_code,
                        "profile": safe_profile,
                        "retry_instrumented": retry_instrumented,
                        "status": safe_outcome,
                        "task_id": str(row["task_id"]),
                    }
                )
                events.append(
                    _make_source_event(
                        source="kanban_timeline",
                        source_execution_id=source_execution_id,
                        event_key_parts=("outcome", revision),
                        event_type=EventType.OUTCOME_OBSERVED,
                        validity=Validity.EXACT,
                        observed_at_ms=ended_at_ms,
                        workload_kind=WorkloadKind.KANBAN_TASK,
                        execution_surface=ExecutionSurface.HERMES,
                        trigger_kind=TriggerKind.KANBAN,
                        lifecycle_phase=LifecyclePhase.ENDED,
                        task_run_id=str(row["id"]),
                        task_id=str(row["task_id"]),
                        profile=safe_profile,
                        status=safe_outcome,
                        error_class=safe_error,
                        exit_code=exit_code,
                        evidence_ref=f"kanban:task_runs:{row['id']}:outcome",
                        attributes={"retry_instrumented": retry_instrumented},
                    )
                )

    if _table_exists(connection, "worker_run_terminal_facts"):
        ended_by_run = {
            str(event.task_run_id): event.observed_at_ms
            for event in events
            if event.lifecycle_phase is LifecyclePhase.ENDED
            and event.task_run_id is not None
        }
        for row in _rows(
            connection,
            """
            SELECT task_run_id, worker_exit_kind, worker_exit_code,
                   worker_protocol_state, task_outcome, end_reason,
                   task_id, board
              FROM worker_run_terminal_facts
             ORDER BY task_run_id
            """,
        ):
            task_run_id = str(row["task_run_id"])
            observed_at_ms = ended_by_run.get(task_run_id)
            if observed_at_ms is None:
                continue
            safe_board = _safe_class(row["board"])
            safe_status = _safe_class(
                row["task_outcome"] or row["worker_protocol_state"]
            )
            safe_end_reason = _safe_class(row["end_reason"])
            safe_error = _safe_class(row["worker_exit_kind"])
            exit_code = (
                int(row["worker_exit_code"])
                if row["worker_exit_code"] is not None
                else None
            )
            retry_instrumented = (
                task_run_id in retry_instrumented_run_ids
            )
            revision = _content_revision_token(
                {
                    "board": safe_board,
                    "end_reason": safe_end_reason,
                    "error_class": safe_error,
                    "exit_code": exit_code,
                    "observed_at_ms": observed_at_ms,
                    "retry_instrumented": retry_instrumented,
                    "status": safe_status,
                    "task_id": str(row["task_id"]),
                }
            )
            events.append(
                _make_source_event(
                    source="kanban_timeline",
                    source_execution_id=f"task_run:{task_run_id}",
                    event_key_parts=("terminal_fact", revision),
                    event_type=EventType.OUTCOME_OBSERVED,
                    validity=Validity.EXACT,
                    observed_at_ms=observed_at_ms,
                    workload_kind=WorkloadKind.KANBAN_TASK,
                    execution_surface=ExecutionSurface.HERMES,
                    trigger_kind=TriggerKind.KANBAN,
                    lifecycle_phase=LifecyclePhase.ENDED,
                    task_run_id=task_run_id,
                    task_id=str(row["task_id"]),
                    board=safe_board,
                    status=safe_status,
                    end_reason=safe_end_reason,
                    error_class=safe_error,
                    exit_code=exit_code,
                    evidence_ref=f"kanban:terminal:{task_run_id}",
                    attributes={"retry_instrumented": retry_instrumented},
                )
            )

    if retry_link_rows:
        claimed_by_run = {
            str(event.task_run_id): event.observed_at_ms
            for event in events
            if event.lifecycle_phase in {
                LifecyclePhase.QUEUED,
                LifecyclePhase.CLAIMED,
                LifecyclePhase.PROCESS_STARTED,
            }
            and event.task_run_id is not None
        }
        for row in retry_link_rows:
            task_run_id = str(row["task_run_id"])
            observed_at_ms = claimed_by_run.get(task_run_id)
            if observed_at_ms is None:
                continue
            events.append(
                _make_source_event(
                    source="kanban_timeline",
                    source_execution_id=f"task_run:{task_run_id}",
                    event_key_parts=(
                        "retry",
                        row["retry_of_task_run_id"],
                        row["triggering_event_id"],
                    ),
                    event_type=EventType.OUTCOME_OBSERVED,
                    validity=Validity.EXACT,
                    observed_at_ms=observed_at_ms,
                    workload_kind=WorkloadKind.KANBAN_TASK,
                    execution_surface=ExecutionSurface.HERMES,
                    trigger_kind=TriggerKind.RETRY,
                    lifecycle_phase=LifecyclePhase.QUEUED,
                    task_run_id=task_run_id,
                    task_id=str(row["task_id"]),
                    board=_safe_class(row["board"]),
                    retry_class=_safe_class(row["retry_class"]),
                    evidence_ref=(
                        f"kanban:retry:{task_run_id}:"
                        f"{row['retry_of_task_run_id']}"
                    ),
                    attributes={"retry_instrumented": True},
                )
            )
    return events


def reconcile_cron(
    connection: sqlite3.Connection,
    *,
    profile: str | None = None,
) -> list[ExecutionEvent]:
    """Translate Hermes cron execution rows without persisting error text."""
    required = {
        "id",
        "job_id",
        "status",
        "claimed_at",
        "started_at",
        "finished_at",
    }
    columns = _columns(connection, "executions")
    if not required <= columns:
        return []
    optional = [
        name
        for name in (
            "error",
            "retry_of_execution_id",
            "retry_class",
            "retry_instrumented",
        )
        if name in columns
    ]
    rows = _rows(
        connection,
        f"""
        SELECT id, job_id, status, claimed_at, started_at, finished_at
               {''.join(f', {name}' for name in optional)}
          FROM executions
         ORDER BY COALESCE(claimed_at, started_at, finished_at), id
        """,
    )
    events: list[ExecutionEvent] = []
    for row in rows:
        source_execution_id = (
            f"cron:{profile}:{row['id']}"
            if profile is not None
            else f"cron:{row['id']}"
        )
        retry_of = row.get("retry_of_execution_id")
        retry_class = _safe_class(row.get("retry_class"))
        explicit_retry_instrumented = row.get("retry_instrumented")
        retry_instrumented = (
            explicit_retry_instrumented is True
            or (
                isinstance(explicit_retry_instrumented, int)
                and not isinstance(explicit_retry_instrumented, bool)
                and explicit_retry_instrumented == 1
            )
            or retry_of is not None
            or retry_class is not None
        )
        safe_profile = _safe_class(profile)
        agent_kind = _safe_class(row["job_id"]) or "unknown"
        for column, phase in (
            ("claimed_at", LifecyclePhase.CLAIMED),
            ("started_at", LifecyclePhase.PROCESS_STARTED),
            ("finished_at", LifecyclePhase.ENDED),
        ):
            observed_at_ms = _epoch_ms(row[column])
            if observed_at_ms is None:
                continue
            status = _safe_class(row["status"]) if phase is LifecyclePhase.ENDED else None
            error_class = (
                "reported_error"
                if phase is LifecyclePhase.ENDED and row.get("error")
                else None
            )
            revision = _content_revision_token(
                {
                    "agent_kind": agent_kind,
                    "column": column,
                    "error_class": error_class,
                    "observed_at_ms": observed_at_ms,
                    "profile": safe_profile,
                    "retry_class": retry_class,
                    "retry_instrumented": retry_instrumented,
                    "retry_of": retry_of,
                    "status": status,
                }
            )
            events.append(
                _make_source_event(
                    source="hermes_cron",
                    source_execution_id=source_execution_id,
                    event_key_parts=(column, revision),
                    event_type=(
                        EventType.OUTCOME_OBSERVED
                        if phase is LifecyclePhase.ENDED
                        else EventType.LIFECYCLE
                    ),
                    validity=Validity.EXACT,
                    observed_at_ms=observed_at_ms,
                    workload_kind=WorkloadKind.CRON_JOB,
                    execution_surface=ExecutionSurface.PROCESS,
                    trigger_kind=(
                        TriggerKind.RETRY
                        if retry_of is not None
                        else TriggerKind.HERMES_CRON
                    ),
                    lifecycle_phase=phase,
                    cron_execution_id=str(row["id"]),
                    profile=safe_profile,
                    status=status,
                    retry_class=retry_class,
                    # Error presence is useful; the raw message is forbidden.
                    error_class=error_class,
                    evidence_ref=(
                        f"cron:executions:{row['id']}:{column}"
                        + (
                            f":retry_of:{retry_of}"
                            if retry_of is not None
                            else ""
                        )
                    ),
                    attributes={
                        "agent_kind": agent_kind,
                        "retry_instrumented": retry_instrumented,
                    },
                )
            )
    return events


LEDGER_CURRENCY = "USD"


def convert_to_ledger_currency(
    amount: str,
    currency: str,
    fx_rates: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """Convert into the ledger currency, or report that we cannot.

    Returns None when no rate is available: aggregating mixed currencies is
    wrong, and inventing a rate is worse than reporting the amount in the
    currency it was actually recorded in.
    """
    if currency == LEDGER_CURRENCY:
        return amount, currency
    rate = (fx_rates or {}).get(currency)
    if rate is None:
        return None
    try:
        converted = Decimal(str(amount)) * Decimal(str(rate))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return format(converted, "f"), LEDGER_CURRENCY


def reconcile_loop_records(
    records: Iterable[Mapping[str, Any]],
    *,
    fx_rates: Mapping[str, Any] | None = None,
    fx_version: str | None = None,
) -> list[ExecutionEvent]:
    """Translate structured loop JSONL records through a content-free allowlist."""
    events: list[ExecutionEvent] = []
    for ordinal, record in enumerate(records):
        observed_at_ms = _epoch_ms(record.get("ts"))
        pack = _safe_class(record.get("pack"))
        phase_name = _safe_class(record.get("phase"))
        if observed_at_ms is None or pack is None:
            raise ValueError(
                f"loop record {ordinal} lacks a valid ts or pack identity"
            )
        round_number = (
            int(record["round"])
            if isinstance(record.get("round"), int)
            and not isinstance(record.get("round"), bool)
            and int(record["round"]) >= 0
            else None
        )
        explicit_run_id = _safe_class(record.get("loop_run_id"))
        date_bucket = datetime.fromtimestamp(observed_at_ms / 1000).date().isoformat()
        source_execution_id = (
            f"loop:{pack}:{explicit_run_id}"
            if explicit_run_id
            else (
                f"loop:{pack}:{date_bucket}:"
                f"r{round_number if round_number is not None else 0}"
            )
        )
        validity = Validity.EXACT if explicit_run_id else Validity.DERIVED
        event_name = _safe_class(record.get("event")) or "ledger_event"
        lifecycle_phase = _LOOP_PHASES.get(phase_name or "")
        verdict = _safe_class(
            record.get("verdict") or record.get("status")
        )
        if verdict == "landed":
            lifecycle_phase = LifecyclePhase.LANDED
        attributes: dict[str, Any] = {}
        for source_key, target_key in (
            ("engine", "agent_provider"),
            ("model", "agent_model"),
            ("billing", "billing_mode"),
            ("input_tokens", "input_tokens"),
            ("cached_input_tokens", "cache_read_tokens"),
            ("output_tokens", "output_tokens"),
            ("reasoning_tokens", "reasoning_tokens"),
            ("total_tokens", "total_tokens"),
            ("verdict", "verdict"),
        ):
            value = record.get(source_key)
            if value is not None:
                attributes[target_key] = value
        metered = _decimal_string(record.get("metered_cost_eur"))
        if metered is not None:
            converted = convert_to_ledger_currency(metered, "EUR", fx_rates)
            if converted is None:
                attributes["metered_cost"] = metered
                attributes["currency"] = "EUR"
            else:
                attributes["metered_cost"], attributes["currency"] = converted
                if fx_version is not None:
                    attributes["fx_version"] = fx_version
        duration = record.get("secs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            attributes["duration_ms"] = max(0, int(float(duration) * 1000))
        span_id = (
            stable_span_id(
                "loop_ledger",
                f"{source_execution_id}:{phase_name}:"
                f"{round_number if round_number is not None else 0}",
            )
            if phase_name is not None
            else None
        )
        events.append(
            _make_source_event(
                source="loop_ledger",
                source_execution_id=source_execution_id,
                event_key_parts=(
                    event_name,
                    phase_name or "",
                    round_number if round_number is not None else "",
                    observed_at_ms,
                    ordinal,
                ),
                event_type=(
                    EventType.USAGE_OBSERVED
                    if event_name == "phase_usage"
                    else EventType.OUTCOME_OBSERVED
                    if verdict is not None
                    else EventType.LIFECYCLE
                ),
                validity=validity,
                observed_at_ms=observed_at_ms,
                workload_kind=WorkloadKind.LOOP,
                execution_surface=ExecutionSurface.LOOP,
                trigger_kind=TriggerKind.LOOP_PHASE,
                lifecycle_phase=lifecycle_phase,
                span_id=span_id,
                span_kind=SpanKind.AGENT if span_id is not None else None,
                loop_run_id=explicit_run_id,
                loop_phase=phase_name,
                loop_round=round_number,
                status=verdict,
                error_class=_safe_class(record.get("fail_kind")),
                evidence_ref=f"loop:{pack}:{date_bucket}:{ordinal}",
                attributes=attributes,
            )
        )
    return events


@dataclass(frozen=True, slots=True)
class SystemInvocationObservation:
    """Content-free observation supplied by a systemd/crontab reader."""

    source_execution_id: str
    surface: ExecutionSurface
    observed_at_ms: int
    phase: LifecyclePhase
    status: str | None = None
    error_class: str | None = None
    exit_code: int | None = None
    validity: Validity = Validity.EXACT

    def __post_init__(self) -> None:
        if self.surface not in {ExecutionSurface.SYSTEMD, ExecutionSurface.CRONTAB}:
            raise ValueError("system observation surface must be systemd or crontab")


def reconcile_system_invocations(
    observations: Iterable[SystemInvocationObservation],
) -> list[ExecutionEvent]:
    events: list[ExecutionEvent] = []
    for observation in observations:
        trigger = (
            TriggerKind.SYSTEMD
            if observation.surface is ExecutionSurface.SYSTEMD
            else TriggerKind.CRONTAB
        )
        events.append(
            _make_source_event(
                source=f"{observation.surface.value}_invocation",
                source_execution_id=observation.source_execution_id,
                event_key_parts=(observation.phase.value,),
                event_type=(
                    EventType.OUTCOME_OBSERVED
                    if observation.phase is LifecyclePhase.ENDED
                    else EventType.LIFECYCLE
                ),
                validity=observation.validity,
                observed_at_ms=observation.observed_at_ms,
                workload_kind=WorkloadKind.SYSTEM_JOB,
                execution_surface=observation.surface,
                trigger_kind=trigger,
                lifecycle_phase=observation.phase,
                status=_safe_class(observation.status),
                error_class=_safe_class(observation.error_class),
                exit_code=observation.exit_code,
                evidence_ref=(
                    f"{observation.surface.value}:"
                    f"{observation.source_execution_id}:"
                    f"{observation.phase.value}"
                ),
            )
        )
    return events


def append_reconciled(
    ledger: ExecutionFactsLedger,
    events: Iterable[ExecutionEvent],
) -> tuple[AppendResult, ...]:
    """Append in stable chunks while preserving source idempotency."""
    ordered = sorted(
        events,
        key=lambda event: (
            event.observed_at_ms,
            event.source,
            event.source_execution_id,
            event.event_id,
        ),
    )
    return ledger.append_batch(ordered)

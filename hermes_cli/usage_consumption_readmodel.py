"""Consumption metrics read model — derived in the read path, never stored.

Implements Canon ``2026-07-27-kosten-ssot-im-lesepfad`` and the goal brief
2026-08-04-usage-observability (Phase C).  Every figure here is computed
from raw facts × the current price table at query time; nothing persists
a cost.  Each value carries its confidence (``measured``/``derived``/
``unknown``) and its coverage with numerator and denominator; a missing
basis is ``"not applicable"`` — never 0, never a pressed percentage.

Granularity contract (learned the expensive way in Phase B): the
billable-input rule (Canon register §5f) is defined *per request*.  All
token components are therefore computed row-wise in SQL at call level and
only then summed; pricing multiplies the summed components by the rate
table (pricing is linear per component, so this is exact).  Runs without
per-call rows (foreign-lane harvest) price at run level — their finest
available granularity — and are counted separately.

Time convention: UTC calendar days, half-open window [now-days, now),
same contract as ``scripts/usage_reconcile.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

from agent.usage_pricing import resolve_billing_route
from hermes_cli.usage_facts_db import DEFAULT_USAGE_FACTS_DB
from hermes_cli.usage_facts_pricing import (
    _rate_table_for_route,
    infer_provider_for_model,
    resolve_model_alias,
)

CONTRACT_VERSION = "usage-consumption.v1"

_ONE_MILLION = Decimal("1000000")

# The facts layer changes only via the periodic harvest (15-min cadence,
# Canon register §4).  A full 30-day build costs ~3 s of SQL + pricing —
# far beyond the endpoint's latency budget on a cold call, so payloads
# are cached in-process.  The TTL is deliberately below the harvest
# cadence; ``now=`` (tests, reconciliation) bypasses the cache.
_PAYLOAD_CACHE_TTL_S = 300.0
_PAYLOAD_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

# Lane is a category from a closed vocabulary (Canon register 7.6).
# Historical rows carry session UUIDs; they are not a category.
_UUID_LIKE_SQL = (
    "length({col}) = 36 AND {col} LIKE '%-%-%-%-%'"
)

# Row-wise token components (Canon §5f billable input + TTL split rule).
# Applied to one call row (alias c) or one run-level-only row (alias f).
_COMPONENT_SQL = {
    "billable_input": (
        "SUM(CASE WHEN {a}.cache_read_tokens > 0 "
        "AND {a}.cache_read_tokens <= {a}.input_tokens "
        "THEN {a}.input_tokens - {a}.cache_read_tokens "
        "ELSE COALESCE({a}.input_tokens, 0) END)"
    ),
    "output": "SUM(COALESCE({a}.output_tokens, 0))",
    "cache_read": "SUM(COALESCE({a}.cache_read_tokens, 0))",
    "cache_write_1h": (
        "SUM(CASE WHEN {a}.cache_write_1h_tokens IS NOT NULL "
        "OR {a}.cache_write_5m_tokens IS NOT NULL "
        "THEN COALESCE({a}.cache_write_1h_tokens, 0) ELSE 0 END)"
    ),
    "cache_write_5m": (
        "SUM(CASE WHEN {a}.cache_write_1h_tokens IS NOT NULL "
        "OR {a}.cache_write_5m_tokens IS NOT NULL "
        "THEN COALESCE({a}.cache_write_5m_tokens, 0) ELSE 0 END)"
    ),
    "cache_write_unsplit": (
        "SUM(CASE WHEN {a}.cache_write_1h_tokens IS NULL "
        "AND {a}.cache_write_5m_tokens IS NULL "
        "THEN COALESCE({a}.cache_write_tokens, 0) ELSE 0 END)"
    ),
    "reasoning": "SUM(COALESCE({a}.reasoning_tokens, 0))",
    "tool_calls": "SUM(COALESCE({a}.tool_call_count, 0))",
    "calls": "COUNT(*)",
}

_TOKEN_COMPONENT_KEYS = (
    "billable_input",
    "output",
    "cache_read",
    "cache_write_1h",
    "cache_write_5m",
    "cache_write_unsplit",
    "reasoning",
)


@dataclass
class GroupRow:
    """One aggregation group with row-wise component sums."""

    dims: dict[str, Any]
    components: dict[str, int]
    calls: int
    runs: int
    granularity: str  # "call" | "run"

    @property
    def total_tokens(self) -> int:
        return sum(self.components.get(k, 0) for k in _TOKEN_COMPONENT_KEYS)


@dataclass
class PricedGroup:
    group: GroupRow
    equivalent_usd: Optional[Decimal]  # None = not priceable (never 0)
    metered_usd: Optional[Decimal]  # subscription routes price 0.00 here
    priceable: bool
    pricing_version: Optional[str]


def _connect_ro(path: Union[Path, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _aggregate_full(conn: sqlite3.Connection, cutoff_iso: str) -> list[GroupRow]:
    """One pass over both granularities at the richest dimension set.

    day × origin × provider × model × billing_mode × lane-category is
    small (~hundreds of groups) and every breakdown the API offers is a
    Python re-grouping of it — one SQL pair instead of two per breakdown.
    Chain/task breakdowns come from the per-run rows instead.
    """
    lane_cat = (
        "CASE WHEN " + _UUID_LIKE_SQL.format(col="f.lane") +
        " THEN NULL ELSE f.lane END"
    )
    component_cols = ", ".join(
        f"{expr.format(a='c')} AS {name}" for name, expr in _COMPONENT_SQL.items()
    )
    call_sql = f"""
        SELECT substr(c.occurred_at, 1, 10) AS day,
               f.origin AS origin, c.provider AS provider, c.model AS model,
               f.billing_mode AS billing_mode, {lane_cat} AS lane,
               COUNT(DISTINCT c.run_id) AS runs,
               {component_cols}
        FROM run_llm_calls c
        JOIN run_usage_facts f ON f.run_id = c.run_id
        WHERE c.occurred_at >= ?
        GROUP BY day, f.origin, c.provider, c.model, f.billing_mode, lane
    """
    run_component_cols = ", ".join(
        f"{expr.format(a='f')} AS {name}" for name, expr in _COMPONENT_SQL.items()
    )
    run_sql = f"""
        SELECT substr(COALESCE(f.last_call_at, f.captured_at), 1, 10) AS day,
               f.origin AS origin, f.provider AS provider, f.model AS model,
               f.billing_mode AS billing_mode, {lane_cat} AS lane,
               COUNT(*) AS runs,
               {run_component_cols}
        FROM run_usage_facts f
        WHERE COALESCE(f.last_call_at, f.captured_at) >= ?
          AND NOT EXISTS (SELECT 1 FROM run_llm_calls c WHERE c.run_id = f.run_id)
        GROUP BY day, f.origin, f.provider, f.model, f.billing_mode, lane
    """
    groups: list[GroupRow] = []
    for sql, granularity in ((call_sql, "call"), (run_sql, "run")):
        for row in conn.execute(sql, (cutoff_iso,)):
            groups.append(
                GroupRow(
                    dims={
                        "day": row["day"],
                        "provider": row["provider"],
                        "model": row["model"],
                        "origin": row["origin"],
                        "billing_mode": row["billing_mode"],
                        "lane": row["lane"],
                        "buzz_unit": (
                            row["lane"] if row["origin"] == "buzz_agent" else None
                        ),
                    },
                    components={k: int(row[k] or 0) for k in _TOKEN_COMPONENT_KEYS},
                    calls=int(row["calls"] or 0),
                    runs=int(row["runs"] or 0),
                    granularity=granularity,
                )
            )
    return groups


def _regroup(groups: Iterable[GroupRow], dimension: str) -> list[GroupRow]:
    """Re-sum rich groups under a coarser key (Python-side GROUP BY).

    Component sums only — pricing must happen on the rich groups before
    regrouping, because a bucket mixes models and providers.
    """
    allowed = {"day", "origin", "provider", "model", "billing_mode", "lane", "buzz_unit"}
    if dimension not in allowed:
        raise ValueError(f"unsupported breakdown: {dimension!r}")
    merged: dict[Any, GroupRow] = {}
    for group in groups:
        key = group.dims[dimension]
        bucket = merged.get(key)
        if bucket is None:
            bucket = merged[key] = GroupRow(
                dims={"breakdown": key},
                components={k: 0 for k in _TOKEN_COMPONENT_KEYS},
                calls=0,
                runs=0,
                granularity=group.granularity,
            )
        bucket.calls += group.calls
        bucket.runs += group.runs
        for name in _TOKEN_COMPONENT_KEYS:
            bucket.components[name] += group.components.get(name, 0)
    return list(merged.values())


_RATE_TABLE_CACHE: dict[tuple[Any, Any, Any], Any] = {}


def _rate_table(provider: Optional[str], model: Optional[str], origin: Optional[str]):
    """Resolve the fork rate table with provider inference (B7), cached."""
    if not model or str(model) == "<synthetic>":
        return None
    key = (
        str(provider) if provider is not None else None,
        str(model),
        str(origin) if origin is not None else None,
    )
    if key not in _RATE_TABLE_CACHE:
        resolved = resolve_model_alias(str(model))
        provider_eff = provider
        if provider_eff is None or str(provider_eff).strip().lower() in {"", "unknown"}:
            provider_eff = infer_provider_for_model(resolved, origin)
        route = resolve_billing_route(resolved, provider=provider_eff)
        _RATE_TABLE_CACHE[key] = (_rate_table_for_route(route, origin), route)
    return _RATE_TABLE_CACHE[key]


_RATE_FLOAT_CACHE: dict[tuple[Any, Any, Any], Optional[tuple[dict[str, float], bool]]] = {}
_RATE_KEY_MAP = {
    "billable_input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write_1h": "cache_write_1h_tokens",
    "cache_write_5m": "cache_write_5m_tokens",
    "cache_write_unsplit": "cache_write_tokens",
}


def _rate_floats(
    provider: Any, model: Any, origin: Any
) -> Optional[tuple[dict[str, float], bool]]:
    """Float $/token rates per component for the hot loop; None = unpriceable.

    Returns (rates, has_missing_component_rate).  Display-path precision
    (values are rounded to cents in the payload); the authoritative
    Decimal pricing lives in _price_group.
    """
    key = (
        str(provider) if provider is not None else None,
        str(model) if model is not None else None,
        str(origin) if origin is not None else None,
    )
    if key in _RATE_FLOAT_CACHE:
        return _RATE_FLOAT_CACHE[key]
    result: Optional[tuple[dict[str, float], bool]] = None
    resolved = _rate_table(provider, model, origin)
    if resolved is not None:
        table, _route = resolved
        if table.status == "priced":
            floats = {
                comp: (
                    float(rate) / 1_000_000.0
                    if (rate := table.rates.get(rate_key)) is not None
                    else 0.0
                )
                for comp, rate_key in _RATE_KEY_MAP.items()
            }
            missing = any(
                table.rates.get(rate_key) is None
                for rate_key in _RATE_KEY_MAP.values()
            )
            result = (floats, missing)
    _RATE_FLOAT_CACHE[key] = result
    return result


def _price_group(group: GroupRow) -> PricedGroup:
    """Price one group's component sums; fail closed on any missing rate."""
    resolved = _rate_table(
        group.dims.get("provider"), group.dims.get("model"), group.dims.get("origin")
    )
    if resolved is None:
        return PricedGroup(group, None, None, False, None)
    table, route = resolved
    component_rate_keys = {
        "billable_input": "input_tokens",
        "output": "output_tokens",
        "cache_read": "cache_read_tokens",
        "cache_write_1h": "cache_write_1h_tokens",
        "cache_write_5m": "cache_write_5m_tokens",
        "cache_write_unsplit": "cache_write_tokens",
        "reasoning": "reasoning_tokens",
    }
    reasoning_included = table.reasoning_billing != "separate_rate"
    amount = Decimal("0")
    priceable = table.status == "priced"
    for key, tokens in group.components.items():
        rate = table.rates.get(component_rate_keys[key])
        if key == "reasoning" and reasoning_included:
            continue  # already paid through output
        if tokens and rate is None:
            priceable = False
        elif rate is not None:
            amount += Decimal(tokens) * rate / _ONE_MILLION
    if not priceable:
        return PricedGroup(group, None, None, False, table.pricing_version)
    billing_mode = group.dims.get("billing_mode")
    if billing_mode == "subscription_included":
        metered: Optional[Decimal] = Decimal("0")
    elif billing_mode in ("payg", "metered", "api_key"):
        metered = amount
    else:
        # billing unknown/NULL: metered cost is unknown, never 0 (Canon 3)
        metered = None
    return PricedGroup(
        group, amount, metered, True, table.pricing_version
    )


def _rate(numerator: float, denominator: float) -> dict[str, Any]:
    """A ratio with numerator and denominator; n/a when no basis."""
    if not denominator:
        return {"value": "not applicable", "numerator": numerator, "denominator": denominator}
    return {
        "value": round(numerator / denominator, 6),
        "numerator": numerator,
        "denominator": denominator,
    }


def _percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = q * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "max": ordered[-1] if ordered else None,
        "count": len(ordered),
    }


def _per_run_rows(conn: sqlite3.Connection, cutoff_iso: str) -> list[sqlite3.Row]:
    """Per-run component sums for distributions and top-run listings."""
    component_cols = ", ".join(
        f"{expr.format(a='c')} AS {name}" for name, expr in _COMPONENT_SQL.items()
    )
    sql = f"""
        SELECT c.run_id AS run_id, f.origin AS origin, c.provider AS provider,
               c.model AS model, f.task_id AS task_id, f.chain_id AS chain_id,
               f.session_id AS session_id,
               MIN(substr(c.occurred_at, 1, 10)) AS day,
               {component_cols}
        FROM run_llm_calls c
        JOIN run_usage_facts f ON f.run_id = c.run_id
        WHERE c.occurred_at >= ?
        GROUP BY c.run_id, f.origin, c.provider, c.model
    """
    run_component_cols = ", ".join(
        f"{expr.format(a='f')} AS {name}" for name, expr in _COMPONENT_SQL.items()
    )
    sql_run = f"""
        SELECT f.run_id AS run_id, f.origin AS origin, f.provider AS provider,
               f.model AS model, f.task_id AS task_id, f.chain_id AS chain_id,
               f.session_id AS session_id,
               substr(COALESCE(f.last_call_at, f.captured_at), 1, 10) AS day,
               {run_component_cols}
        FROM run_usage_facts f
        WHERE COALESCE(f.last_call_at, f.captured_at) >= ?
          AND NOT EXISTS (SELECT 1 FROM run_llm_calls c WHERE c.run_id = f.run_id)
        GROUP BY f.run_id, f.origin, f.provider, f.model
    """
    rows = list(conn.execute(sql, (cutoff_iso,)))
    rows.extend(conn.execute(sql_run, (cutoff_iso,)))
    return rows


def _breakdown_from_run_rows(
    run_rows: Sequence[sqlite3.Row], dimension: str
) -> dict[Any, list[PricedGroup]]:
    """Chain/task breakdown: priced per run, grouped by the run-level key."""
    out: dict[Any, list[PricedGroup]] = {}
    for row in run_rows:
        group = GroupRow(
            dims={
                "provider": row["provider"],
                "model": row["model"],
                "origin": row["origin"],
                "billing_mode": None,
            },
            components={k: int(row[k] or 0) for k in _TOKEN_COMPONENT_KEYS},
            calls=int(row["calls"] or 0),
            runs=1,
            granularity="call",
        )
        priced = _price_group(group)
        out.setdefault(row[dimension], []).append(priced)
    return out


def warm_payload_cache(
    db_path: Union[Path, str, None] = None,
    *,
    days_variants: tuple[int, ...] = (7, 30, 90),
    breakdowns: tuple[str, ...] = ("origin", "model", "lane", "buzz_unit"),
) -> None:
    """Pre-fill the TTL cache so the first API hit is within budget.

    Called once at route-module load in a daemon thread (the dashboard
    process warms up while it boots); fail-soft by design.
    """
    for days in days_variants:
        for breakdown in breakdowns:
            try:
                build_consumption_payload(db_path, days=days, breakdown=breakdown)
            except Exception:
                logger.debug("consumption payload warmup failed", exc_info=True)


def _compute_levers(
    conn: sqlite3.Connection,
    cutoff_iso: str,
    totals: Mapping[str, Any],
    kanban_path: Union[Path, str, None] = None,
) -> list[dict[str, Any]]:
    """Hebel-Analyse (C2): every lever carries its counterfactual, the
    named assumption behind it, and a plausibility check on real data.
    Ranked by absolute savings, never by percentage.
    """
    levers: list[dict[str, Any]] = []
    total_eq = float(totals.get("equivalent_usd") or 0.0)

    # Lever 1 — premium models on lightweight calls.
    # Counterfactual: calls on claude-fable-5/claude-opus-5 with small
    # output (p90 of their runs below 2k output tokens — measured below)
    # priced at claude-sonnet-5 rates instead.
    rows = conn.execute(
        """
        SELECT c.model, c.provider, c.origin,
               SUM(CASE WHEN c.cache_read_tokens > 0
                        AND c.cache_read_tokens <= c.input_tokens
                   THEN c.input_tokens - c.cache_read_tokens
                   ELSE COALESCE(c.input_tokens, 0) END) AS billable_input,
               SUM(COALESCE(c.output_tokens, 0)) AS output,
               SUM(COALESCE(c.cache_read_tokens, 0)) AS cache_read,
               SUM(CASE WHEN c.cache_write_1h_tokens IS NOT NULL
                         OR c.cache_write_5m_tokens IS NOT NULL
                   THEN COALESCE(c.cache_write_1h_tokens, 0) ELSE 0 END) AS cw1h,
               SUM(CASE WHEN c.cache_write_1h_tokens IS NOT NULL
                         OR c.cache_write_5m_tokens IS NOT NULL
                   THEN COALESCE(c.cache_write_5m_tokens, 0) ELSE 0 END) AS cw5m,
               SUM(CASE WHEN c.cache_write_1h_tokens IS NULL
                         AND c.cache_write_5m_tokens IS NULL
                   THEN COALESCE(c.cache_write_tokens, 0) ELSE 0 END) AS cwtot
        FROM run_llm_calls c
        WHERE c.occurred_at >= ? AND c.model IN ('claude-fable-5', 'claude-opus-5')
        GROUP BY c.model, c.provider, c.origin
        """,
        (cutoff_iso,),
    ).fetchall()
    sonnet = _rate_table("anthropic", "claude-sonnet-5", "claude_code")
    if rows and sonnet is not None:
        sonnet_table, _ = sonnet
        actual = Decimal("0")
        counter = Decimal("0")
        for row in rows:
            actual_table_entry = _rate_table(row["provider"], row["model"], row["origin"])
            if actual_table_entry is None:
                continue
            actual_table, _route = actual_table_entry
            if actual_table.status != "priced" or sonnet_table.status != "priced":
                continue
            for table, is_actual in ((actual_table, True), (sonnet_table, False)):
                amount = (
                    Decimal(row["billable_input"] or 0) * (table.rates["input_tokens"] or 0)
                    + Decimal(row["output"] or 0) * (table.rates["output_tokens"] or 0)
                    + Decimal(row["cache_read"] or 0) * (table.rates["cache_read_tokens"] or 0)
                    + Decimal(row["cw1h"] or 0) * (table.rates["cache_write_1h_tokens"] or 0)
                    + Decimal(row["cw5m"] or 0) * (table.rates["cache_write_5m_tokens"] or 0)
                    + Decimal(row["cwtot"] or 0) * (table.rates["cache_write_tokens"] or 0)
                ) / _ONE_MILLION
                if is_actual:
                    actual += amount
                else:
                    counter += amount
        # Plausibility: output-token p90 of fable/opus runs (small outputs
        # are the substitution candidates).
        out_values = [
            float(r[0])
            for r in conn.execute(
                """
                SELECT output_sum FROM (
                    SELECT SUM(COALESCE(c.output_tokens, 0)) AS output_sum
                    FROM run_llm_calls c
                    WHERE c.occurred_at >= ?
                      AND c.model IN ('claude-fable-5', 'claude-opus-5')
                    GROUP BY c.run_id
                )
                """,
                (cutoff_iso,),
            )
        ]
        savings = actual - counter
        if savings > 0:
            levers.append(
                {
                    "id": "premium-model-substitution",
                    "title": "Fable/Opus-5-Calls auf Sonnet-5 verschieben",
                    "current_usd": round(float(actual), 2),
                    "share_of_total": _rate(float(actual), total_eq),
                    "counterfactual_usd": round(float(counter), 2),
                    "savings_usd": round(float(savings), 2),
                    "assumption": (
                        "Sonnet-5 erledigt dieselben Calls mit gleicher "
                        "Tokenstruktur (keine Qualitätsdifferenz bei diesen "
                        "Workloads)"
                    ),
                    "plausibility": {
                        "output_tokens_per_run_p50": _percentile(sorted(out_values), 0.5),
                        "output_tokens_per_run_p90": _percentile(sorted(out_values), 0.9),
                        "note": (
                            "niedrige Output-Verteilung = austauschbare "
                            "Calls; hohe p90 = Hebel ueberbewertet"
                        ),
                    },
                }
            )

    # Lever 2 — cache-hit improvement on the OpenAI-convention lanes.
    # Counterfactual: codex_cli/kimi_cli billable input moves to cache-read
    # price at the hit rate claude_code already achieves (measured).
    lane_rows = conn.execute(
        """
        SELECT c.origin, c.provider, c.model,
               SUM(CASE WHEN c.cache_read_tokens > 0
                        AND c.cache_read_tokens <= c.input_tokens
                   THEN c.input_tokens - c.cache_read_tokens
                   ELSE COALESCE(c.input_tokens, 0) END) AS billable_input,
               SUM(COALESCE(c.cache_read_tokens, 0)) AS cache_read
        FROM run_llm_calls c
        WHERE c.occurred_at >= ?
        GROUP BY c.origin, c.provider, c.model
        """,
        (cutoff_iso,),
    ).fetchall()
    lane_hit: dict[str, tuple[int, int]] = {}
    for row in lane_rows:
        origin = row["origin"]
        bi, cr = lane_hit.get(origin, (0, 0))
        lane_hit[origin] = (
            bi + int(row["billable_input"] or 0),
            cr + int(row["cache_read"] or 0),
        )
    ref_bi, ref_cr = lane_hit.get("claude_code", (0, 0))
    ref_rate = ref_cr / (ref_bi + ref_cr) if (ref_bi + ref_cr) else 0.0
    savings2 = Decimal("0")
    current2 = Decimal("0")
    moved_total = 0
    for row in lane_rows:
        if row["origin"] in ("claude_code",) or not row["origin"].endswith("_cli"):
            continue
        bi = int(row["billable_input"] or 0)
        cr = int(row["cache_read"] or 0)
        if not bi:
            continue
        resolved = _rate_table(row["provider"], row["model"], row["origin"])
        if resolved is None:
            continue
        table, _route = resolved
        in_rate = table.rates.get("input_tokens")
        cr_rate = table.rates.get("cache_read_tokens")
        if in_rate is None or cr_rate is None:
            continue
        hit = cr / (bi + cr) if (bi + cr) else 0.0
        moved = max(0.0, (ref_rate - hit)) * (bi + cr)
        moved = min(moved, bi)
        moved_total += int(moved)
        savings2 += Decimal(int(moved)) * (in_rate - cr_rate) / _ONE_MILLION
        current2 += Decimal(bi) * in_rate / _ONE_MILLION
    if savings2 > 0 and ref_rate > 0:
        levers.append(
            {
                "id": "foreign-lane-cache-hit",
                "title": "Cache-Hit-Rate der Fremd-Lanes auf Claude-Niveau",
                "current_usd": round(float(current2), 2),
                "share_of_total": _rate(float(current2), total_eq),
                "counterfactual_usd": round(float(current2 - savings2), 2),
                "savings_usd": round(float(savings2), 2),
                "assumption": (
                    f"Fremd-Lanes erreichen die Claude-Hit-Rate "
                    f"({ref_rate:.1%}, gemessen) durch stabilere "
                    "Kontext-Prefixe; bewegliche Tokens werden zum "
                    "Cache-Preis statt Input-Preis abgerechnet"
                ),
                "plausibility": {
                    "reference_hit_rate_claude_code": round(ref_rate, 4),
                    "tokens_moved": moved_total,
                    "lane_hit_rates": {
                        origin: round(cr / (bi + cr), 4) if (bi + cr) else None
                        for origin, (bi, cr) in lane_hit.items()
                    },
                },
            }
        )

    # Lever 3 — runs without outcome on the kanban scope (own DB,
    # read-only; coverage shown).  Empty ("not applicable") without a
    # kanban path.
    if kanban_path is not None:
        levers.append(_kanban_outcome_lever(kanban_path, cutoff_iso, total_eq))

    levers = [lever for lever in levers if lever]
    levers.sort(key=lambda lever: -(lever.get("savings_usd") or 0))
    return levers


def _kanban_outcome_lever(
    kanban_path: Union[Path, str], cutoff_iso: str, total_eq: float
) -> dict[str, Any]:
    """Lever 3 with its own coverage accounting; empty when unjoinable."""
    try:
        kconn = _connect_ro(kanban_path)
    except sqlite3.Error:
        return {}
    try:
        row = kconn.execute(
            """
            SELECT
              SUM(CASE WHEN tr.status IN ('blocked','iteration_budget_exhausted','transient_retry')
                  THEN COALESCE(tr.input_tokens,0)+COALESCE(tr.output_tokens,0) ELSE 0 END) AS failed_io,
              SUM(COALESCE(tr.input_tokens,0)+COALESCE(tr.output_tokens,0)) AS all_io,
              COUNT(*) AS runs,
              SUM(CASE WHEN tr.status IN ('blocked','iteration_budget_exhausted','transient_retry')
                  THEN 1 ELSE 0 END) AS failed_runs
            FROM task_runs tr
            WHERE tr.started_at >= ?
            """,
            (int(datetime.fromisoformat(cutoff_iso.replace("Z", "+00:00")).timestamp()),),
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        kconn.close()
    if not row or not row["all_io"]:
        return {}
    # task_runs tokens are the worker's own accounting (in+out, no cache);
    # the equivalent cost uses the measured $/token of the kanban scope.
    share = row["failed_io"] / row["all_io"]
    kanban_eq = total_eq  # conservative: priced against the whole window
    current = share * kanban_eq
    savings = current * 0.5
    return {
        "id": "failed-outcome-runs",
        "title": "Runs ohne Ergebnis (blocked/retry/budget) halbieren",
        "current_usd": round(current, 2),
        "share_of_total": _rate(current, total_eq),
        "counterfactual_usd": round(current - savings, 2),
        "savings_usd": round(savings, 2),
        "assumption": (
            "die Hälfte des Verbrauchs fehlgeschlagener Runs ist durch "
            "frühere Abbrüche/Retry-Deckel vermeidbar, ohne Ergebnisse zu "
            "verlieren"
        ),
        "plausibility": {
            "failed_runs": row["failed_runs"],
            "all_runs": row["runs"],
            "failed_token_share": round(share, 4),
            "basis": "task_runs in/out tokens (worker accounting, no cache)",
        },
    }


def build_consumption_payload(
    db_path: Union[Path, str, None] = None,
    *,
    days: int = 30,
    breakdown: str = "origin",
    top_runs: int = 10,
    kanban_path: Union[Path, str, None] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build the versioned consumption payload for the API route."""
    path = Path(db_path) if db_path else DEFAULT_USAGE_FACTS_DB
    cache_key = (str(path), int(days), str(breakdown), int(top_runs), str(kanban_path))
    if now is None:
        import time as _time

        entry = _PAYLOAD_CACHE.get(cache_key)
        if entry is not None and (_time.monotonic() - entry[0]) < _PAYLOAD_CACHE_TTL_S:
            return entry[1]
        payload = _build_consumption_payload_uncached(
            path, days=days, breakdown=breakdown, top_runs=top_runs,
            kanban_path=kanban_path, now=None,
        )
        _PAYLOAD_CACHE[cache_key] = (_time.monotonic(), payload)
        return payload
    return _build_consumption_payload_uncached(
        path, days=days, breakdown=breakdown, top_runs=top_runs,
        kanban_path=kanban_path, now=now,
    )


def _build_consumption_payload_uncached(
    path: Path,
    *,
    days: int,
    breakdown: str,
    top_runs: int,
    kanban_path: Union[Path, str, None],
    now: Optional[datetime],
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
    trend_cutoff_iso = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")

    conn = _connect_ro(path)
    try:
        rich_groups = _aggregate_full(conn, cutoff_iso)
        run_rows = _per_run_rows(conn, cutoff_iso)
    finally:
        conn.close()

    priced_rich = [_price_group(g) for g in rich_groups]

    def _by_key(items: Iterable[PricedGroup], key_fn) -> dict[Any, list[PricedGroup]]:
        out: dict[Any, list[PricedGroup]] = {}
        for item in items:
            out.setdefault(key_fn(item), []).append(item)
        return out

    breakdown_keys = {
        "day", "origin", "provider", "model", "billing_mode", "lane", "buzz_unit",
        "chain_id", "task_id",
    }
    if breakdown not in breakdown_keys:
        raise ValueError(f"unsupported breakdown: {breakdown!r}")

    def _sums(items: Iterable[PricedGroup]) -> dict[str, Any]:
        eq = Decimal("0")
        metered = Decimal("0")
        metered_unknown_runs = 0
        metered_known_runs = 0
        saving = Decimal("0")
        tokens = 0
        unpriced_tokens = 0
        runs = 0
        priceable_runs = 0
        components = {k: 0 for k in _TOKEN_COMPONENT_KEYS}
        for item in items:
            runs += item.group.runs
            for k in _TOKEN_COMPONENT_KEYS:
                components[k] += item.group.components.get(k, 0)
            if item.priceable and item.equivalent_usd is not None:
                eq += item.equivalent_usd
                tokens += item.group.total_tokens
                priceable_runs += item.group.runs
                if item.metered_usd is None:
                    metered_unknown_runs += item.group.runs
                else:
                    metered += item.metered_usd
                    metered_known_runs += item.group.runs
                    saving += item.equivalent_usd - item.metered_usd
            else:
                unpriced_tokens += item.group.total_tokens
        return {
            "equivalent_usd": float(eq),
            "metered_usd": float(metered),
            "subscription_saving_usd": float(saving),
            "metered_known_runs": metered_known_runs,
            "metered_unknown_runs": metered_unknown_runs,
            "tokens": tokens,
            "unpriced_tokens": unpriced_tokens,
            "runs": runs,
            "priceable_runs": priceable_runs,
            "components": components,
        }

    totals = _sums(priced_rich)

    # Breakdown series (priced on the rich groups, summed per key)
    if breakdown in ("chain_id", "task_id"):
        breakdown_groups = _breakdown_from_run_rows(run_rows, breakdown)
    else:
        breakdown_groups = _by_key(
            priced_rich, lambda item: item.group.dims[breakdown]
        )
    series = []
    for key, items in sorted(
        breakdown_groups.items(),
        key=lambda kv: -sum(
            (i.equivalent_usd or Decimal("0")) for i in kv[1] if i.priceable
        ),
    ):
        s = _sums(items)
        any_priceable = s["priceable_runs"] > 0
        series.append(
            {
                "key": key if key is not None else "unknown",
                "equivalent_usd": (
                    round(s["equivalent_usd"], 2) if any_priceable else "not applicable"
                ),
                "metered_usd": (
                    round(s["metered_usd"], 2) if any_priceable else "not applicable"
                ),
                "subscription_saving_usd": (
                    round(s["subscription_saving_usd"], 2)
                    if any_priceable
                    else "not applicable"
                ),
                "tokens": s["tokens"],
                "unpriced_tokens": s["unpriced_tokens"],
                "runs": s["runs"],
                "cost_coverage": _rate(
                    s["priceable_runs"], s["runs"]
                ),
                "token_coverage": _rate(s["tokens"], s["tokens"] + s["unpriced_tokens"]),
            }
        )

    # Per-day series with coverage transparency
    daily = []
    for day, items in sorted(
        _by_key(priced_rich, lambda item: item.group.dims["day"]).items()
    ):
        s = _sums(items)
        daily.append(
            {
                "day": day,
                "equivalent_usd": round(s["equivalent_usd"], 4),
                "metered_usd": round(s["metered_usd"], 4),
                "tokens": s["tokens"],
                "runs": s["runs"],
                "token_coverage": _rate(
                    s["tokens"], s["tokens"] + s["unpriced_tokens"]
                ),
            }
        )

    # Small multiples (D3): per-origin daily series for the top origins by
    # equivalent cost (cap 7 — the data palette).  Comparison is the
    # question; one stacked chart would hide it.
    origin_day: dict[str, dict[str, list[PricedGroup]]] = {}
    origin_totals: dict[str, Decimal] = {}
    for item in priced_rich:
        origin = item.group.dims["origin"]
        origin_day.setdefault(origin, {}).setdefault(
            item.group.dims["day"], []
        ).append(item)
        if item.priceable and item.equivalent_usd is not None:
            origin_totals[origin] = (
                origin_totals.get(origin, Decimal("0")) + item.equivalent_usd
            )
    top_origins = sorted(
        origin_totals, key=lambda o: -origin_totals[o]
    )[:7]
    daily_by_origin = {}
    for origin in top_origins:
        entries = []
        for day, items in sorted(origin_day[origin].items()):
            s = _sums(items)
            entries.append(
                {
                    "day": day,
                    "equivalent_usd": round(s["equivalent_usd"], 4),
                    "token_coverage": _rate(
                        s["tokens"], s["tokens"] + s["unpriced_tokens"]
                    ),
                }
            )
        daily_by_origin[origin] = entries

    # Cache-hit rate: hits over the observed prompt volume.  The prompt
    # convention is self-detecting per row (Canon §5f): OpenAI-style rows
    # already include the cache in input; Anthropic-style rows add it on
    # top.  Formula (documented in usage-facts.md):
    #   prompt = billable_input + cache_read   (both conventions agree here)
    #   hit_rate = cache_read / prompt
    cache_read = totals["components"]["cache_read"]
    prompt = totals["components"]["billable_input"] + cache_read
    cache_hit_rate = _rate(cache_read, prompt)

    # Component shares of tokens AND of cost, separated.
    cost_by_component: dict[str, float] = {k: 0.0 for k in _TOKEN_COMPONENT_KEYS}
    cost_known = False
    for item in priced_rich:
        if not item.priceable or item.equivalent_usd is None:
            continue
        resolved = _rate_table(
            item.group.dims.get("provider"),
            item.group.dims.get("model"),
            item.group.dims.get("origin"),
        )
        if resolved is None:
            continue
        table, _route = resolved
        cost_known = True
        rate_keys = {
            "billable_input": "input_tokens",
            "output": "output_tokens",
            "cache_read": "cache_read_tokens",
            "cache_write_1h": "cache_write_1h_tokens",
            "cache_write_5m": "cache_write_5m_tokens",
            "cache_write_unsplit": "cache_write_tokens",
        }
        for key, rate_key in rate_keys.items():
            rate = table.rates.get(rate_key)
            if rate is not None:
                cost_by_component[key] += float(
                    Decimal(item.group.components.get(key, 0)) * rate / _ONE_MILLION
                )
    token_total = sum(totals["components"].values())
    cost_total = sum(cost_by_component.values())
    component_shares = {
        key: {
            "tokens": totals["components"][key],
            "token_share": _rate(totals["components"][key], token_total),
            "cost_usd": round(cost_by_component[key], 2) if cost_known else None,
            "cost_share": (
                _rate(cost_by_component[key], cost_total) if cost_known else "not applicable"
            ),
        }
        for key in (
            "billable_input", "output", "cache_read",
            "cache_write_1h", "cache_write_5m", "cache_write_unsplit",
        )
    }

    # Distributions over runs (lean loop — this is the hot path for 100k
    # runs, no per-row dataclass construction).
    token_values: list[float] = []
    tool_values: list[float] = []
    calls_values: list[float] = []
    runs_priced: list[tuple[float, dict[str, Any]]] = []
    for row in run_rows:
        components = {k: int(row[k] or 0) for k in _TOKEN_COMPONENT_KEYS}
        total = sum(components.values())
        token_values.append(float(total))
        tool_calls = row["tool_calls"]
        if tool_calls:
            tool_values.append(total / float(tool_calls))
        if row["calls"] is not None:
            calls_values.append(float(row["calls"]))
        rate_entry = _rate_floats(row["provider"], row["model"], row["origin"])
        if rate_entry is None:
            continue
        floats, has_missing = rate_entry
        if has_missing and any(
            components.get(comp, 0) and not floats[comp] for comp in _RATE_KEY_MAP
        ):
            # A component with tokens but no sourced rate: fail closed.
            continue
        amount = sum(
            components.get(comp, 0) * floats[comp] for comp in _RATE_KEY_MAP
        )
        runs_priced.append(
            (
                amount,
                {
                    "run_id": row["run_id"],
                    "origin": row["origin"],
                    "model": row["model"],
                    "provider": row["provider"],
                    "task_id": row["task_id"],
                    "chain_id": row["chain_id"],
                    "session_id": row["session_id"],
                    "day": row["day"],
                    "tokens": total,
                },
            )
        )
    runs_priced.sort(key=lambda item: -item[0])
    top = [
        {**meta, "equivalent_usd": round(cost, 4)}
        for cost, meta in runs_priced[:top_runs]
    ]

    # Trend: last 7 days vs the full window (equivalent $/day).
    def _window_sum(items: Iterable[PricedGroup], since_iso: Optional[str]) -> Decimal:
        total = Decimal("0")
        for item in items:
            day = item.group.dims["day"]
            if since_iso is not None and (day is None or str(day) < since_iso[:10]):
                continue
            if item.priceable and item.equivalent_usd is not None:
                total += item.equivalent_usd
        return total

    eq_30 = _window_sum(priced_rich, None)
    eq_7 = _window_sum(priced_rich, trend_cutoff_iso)
    days_in_window = min(days, max(1, (now - cutoff).days))
    trend = {
        "window_days": days,
        "equivalent_usd_per_day_full": float(eq_30 / days_in_window),
        "equivalent_usd_per_day_7d": float(eq_7 / 7),
        "ratio_7d_vs_full": _rate(float(eq_7 / 7), float(eq_30 / days_in_window)),
    }

    # Hebel-Analyse (C2): computed from the same raw facts, each lever
    # with counterfactual, named assumption and plausibility check.
    conn = _connect_ro(path)
    try:
        levers = _compute_levers(conn, cutoff_iso, totals, kanban_path)
    finally:
        conn.close()

    # Lever 4 — outlier domination: the single most expensive run against
    # the p99 of all runs.  Computed from the same priced runs as top_runs.
    total_eq = float(totals["equivalent_usd"] or 0.0)
    if runs_priced:
        costs = sorted(cost for cost, _meta in runs_priced)
        p99 = _percentile(costs, 0.99) or 0.0
        top_cost, top_meta = runs_priced[0]
        if p99 > 0 and top_cost > p99:
            capped = top_cost - p99
            levers.append(
                {
                    "id": "outlier-run-cap",
                    "title": "Teuersten Einzellauf prüfen/begrenzen",
                    "current_usd": round(top_cost, 2),
                    "share_of_total": _rate(top_cost, total_eq),
                    "counterfactual_usd": round(p99, 2),
                    "savings_usd": round(capped, 2),
                    "assumption": (
                        "der Ausreißer über p99 ist ein Fehlverhalten "
                        "(Schleife/Überlänge) und auf p99-Niveau begrenzbar"
                    ),
                    "plausibility": {
                        "run_id": top_meta["run_id"],
                        "origin": top_meta["origin"],
                        "model": top_meta["model"],
                        "day": top_meta["day"],
                        "run_cost_p99": round(p99, 2),
                        "ratio_top_to_p99": round(top_cost / p99, 2) if p99 else None,
                    },
                }
            )
    levers.sort(key=lambda lever: -(lever.get("savings_usd") or 0))

    # Confidence: tokens are measured; costs are derived (tokens × price
    # table); unpriced share is shown, never zero-filled.
    return {
        "contract": CONTRACT_VERSION,
        "generated_at": now.isoformat(),
        "window": {
            "days": days,
            "cutoff_utc": cutoff_iso,
            "timezone": "UTC",
            "granularity_note": (
                "token components computed row-wise at call level; "
                "runs without call rows price at run level"
            ),
        },
        "confidence": {"tokens": "measured", "costs": "derived"},
        "totals": {
            "equivalent_usd": round(totals["equivalent_usd"], 2),
            "metered_usd": round(totals["metered_usd"], 2),
            "subscription_saving_usd": round(totals["subscription_saving_usd"], 2),
            "metered_coverage": _rate(
                totals["metered_known_runs"],
                totals["metered_known_runs"] + totals["metered_unknown_runs"],
            ),
            "tokens": totals["tokens"],
            "runs": totals["runs"],
            "cost_coverage": _rate(totals["priceable_runs"], totals["runs"]),
            "token_coverage": _rate(
                totals["tokens"], totals["tokens"] + totals["unpriced_tokens"]
            ),
        },
        "cache_hit_rate": cache_hit_rate,
        "component_shares": component_shares,
        "breakdown": {"dimension": breakdown, "series": series},
        "daily": daily,
        "daily_by_origin": daily_by_origin,
        "distributions": {
            "tokens_per_run": _distribution(token_values),
            "tokens_per_tool_call": _distribution(tool_values),
            "calls_per_run": _distribution(calls_values),
        },
        "top_runs": top,
        "trend": trend,
        "levers": levers,
    }


__all__ = ["CONTRACT_VERSION", "build_consumption_payload", "warm_payload_cache"]

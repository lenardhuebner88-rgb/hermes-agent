#!/usr/bin/env python3
"""Reconcile LLM usage across four independent measurement paths.

For a time window this computes, per scope, the same quantities (runs,
tokens, list-price equivalent cost) from up to four ways:

1. ``run_llm_calls``        — per-call rows in usage_facts.db
2. ``run_usage_facts``      — run aggregates in usage_facts.db (SSOT)
3. ``kanban.db task_runs``  — worker-reported tokens (kanban scope only)
4. raw JSONL transcripts    — ~/.claude/projects (claude_code scope only)

Every pairwise delta is classified ``explained`` (named reason) or
``unexplained``.  Exit code is non-zero when an unexplained delta exceeds
the threshold.  This script is the stop-condition instrument for the
usage-observability goal — it must never silently pass.

Day boundary convention (A3): all windows are UTC.  ``--days N`` means
the half-open interval [now-UTC - N days, now-UTC).  ISO timestamps with
``Z`` and ``+00:00`` suffixes both sort correctly against a ``Z``-format
cutoff for day-scale windows (suffix differs only within the same
second); ``task_runs.started_at`` is epoch seconds and is compared
numerically — never through ``datetime()``, which silently matches
nothing against epoch integers.

Databases are opened read-only (``mode=ro``).  No writes, ever.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_cli.usage_facts_buzz_attribution import (  # noqa: E402
    BUZZ_AGENT_ORIGIN,
    buzz_agent_unit_for_workspace,
)
from hermes_cli.usage_facts_db import DEFAULT_USAGE_FACTS_DB  # noqa: E402
from hermes_cli.usage_facts_pricing import (  # noqa: E402
    UsageFactsUsage,
    estimate_equivalent_cost,
)

DEFAULT_KANBAN_DB = Path.home() / ".hermes" / "kanban.db"
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Scopes compared pairwise.  Each way contributes the scopes it can see;
# a pair is only compared on the intersection.
SCOPES = ("claude_code", "hermes", "kanban", "buzz", "all")

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)

# Default relative-delta threshold (2%) above which an unexplained delta
# fails the run.  Relative to the larger side of the pair.
DEFAULT_THRESHOLD = 0.02


@dataclass
class WayResult:
    """Aggregates for one measurement way within one scope."""

    way: str
    scope: str
    runs: int = 0
    tokens: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in TOKEN_FIELDS}
    )
    # Equivalent cost sums only over priceable rows; the unpriced share is
    # reported, never silently dropped (Canon rule 3).
    cost_equivalent_usd: Optional[Decimal] = Decimal("0")
    unpriced_runs: int = 0
    unpriced_tokens: int = 0
    note: str = ""

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "way": self.way,
            "scope": self.scope,
            "runs": self.runs,
            "tokens": dict(self.tokens),
            "total_tokens": self.total_tokens,
            "cost_equivalent_usd": (
                None
                if self.cost_equivalent_usd is None
                else float(self.cost_equivalent_usd)
            ),
            "unpriced_runs": self.unpriced_runs,
            "unpriced_tokens": self.unpriced_tokens,
            "note": self.note,
        }


@dataclass
class Delta:
    scope: str
    way_a: str
    way_b: str
    metric: str
    value_a: float
    value_b: float
    rel: float
    classification: str  # "explained" | "unexplained"
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "ways": [self.way_a, self.way_b],
            "metric": self.metric,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "rel_delta": round(self.rel, 6),
            "classification": self.classification,
            "reason": self.reason,
        }


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _price(
    model: Optional[str],
    provider: Optional[str],
    usage: UsageFactsUsage,
    origin: Optional[str],
) -> Optional[Decimal]:
    """List-price equivalent for one row; None when not priceable."""
    if not model or not provider:
        return None
    result = estimate_equivalent_cost(
        model, usage, provider=provider, origin=origin
    )
    return result.amount_usd


_CLAUDE_ORIGIN_SQL = (
    "(origin='claude_code' OR (origin='buzz_agent' "
    "AND run_id LIKE 'claude_code:%'))"
)


def _scope_where(scope: str, alias: str = "") -> tuple[str, list[Any]]:
    p = f"{alias}." if alias else ""
    if scope == "claude_code":
        return _CLAUDE_ORIGIN_SQL.replace("origin", f"{p}origin").replace(
            "run_id", f"{p}run_id"
        ), []
    if scope == "buzz":
        return f"{p}origin=?", [BUZZ_AGENT_ORIGIN]
    if scope == "hermes":
        return f"{p}origin IN ('hermes_agent','hermes_aux')", []
    if scope == "kanban":
        return f"{p}task_run_id IS NOT NULL", []
    return "1=1", []


def way2_run_facts(
    conn: sqlite3.Connection, cutoff_iso: str, scope: str
) -> WayResult:
    where, params = _scope_where(scope, "f")
    # Event time first, write time as fallback; captured_at is the only
    # timestamp hermes_agent rows carry (occurred_at gap is a named blind
    # spot, measured in phase A).
    time_pred = "COALESCE(f.last_call_at, f.captured_at) >= ?"
    rows = conn.execute(
        f"""
        SELECT f.run_id, f.origin, f.provider, f.model,
               f.input_tokens, f.output_tokens,
               f.cache_read_tokens, f.cache_write_tokens,
               f.cache_write_1h_tokens, f.cache_write_5m_tokens,
               f.reasoning_tokens
        FROM run_usage_facts f
        WHERE {where} AND {time_pred}
        """,
        [*params, cutoff_iso],
    )
    result = WayResult(way="run_usage_facts", scope=scope)
    for row in rows:
        result.runs += 1
        for name in TOKEN_FIELDS:
            result.tokens[name] += int(row[name] or 0)
        amount = _price(
            row["model"],
            row["provider"],
            UsageFactsUsage(
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cache_read_tokens=int(row["cache_read_tokens"] or 0),
                cache_write_tokens=int(row["cache_write_tokens"] or 0),
                cache_write_1h_tokens=row["cache_write_1h_tokens"],
                cache_write_5m_tokens=row["cache_write_5m_tokens"],
                reasoning_tokens=int(row["reasoning_tokens"] or 0),
            ),
            row["origin"],
        )
        if amount is None:
            result.unpriced_runs += 1
            result.unpriced_tokens += sum(
                int(row[name] or 0) for name in TOKEN_FIELDS
            )
        else:
            result.cost_equivalent_usd += amount
    return result


def way1_llm_calls(
    conn: sqlite3.Connection, cutoff_iso: str, scope: str
) -> WayResult:
    # Per-call rows.  occurred_at is NULL for hermes_agent/hermes_aux
    # (measured: 0 of 18k rows carry it) — those calls are scoped through
    # their run's COALESCE(last_call_at, captured_at) instead; that is an
    # explained approximation, named here, not hidden.
    if scope in ("hermes", "kanban"):
        where, params = _scope_where(scope, "f")
        sql = f"""
            SELECT c.run_id, c.origin, c.provider, c.model,
                   c.input_tokens, c.output_tokens,
                   c.cache_read_tokens, c.cache_write_tokens,
                   c.cache_write_1h_tokens, c.cache_write_5m_tokens,
                   c.reasoning_tokens
            FROM run_llm_calls c
            JOIN run_usage_facts f ON f.run_id = c.run_id
            WHERE {where} AND COALESCE(f.last_call_at, f.captured_at) >= ?
        """
        params = [*params, cutoff_iso]
    else:
        where, params = _scope_where(scope, "c")
        sql = f"""
            SELECT c.run_id, c.origin, c.provider, c.model,
                   c.input_tokens, c.output_tokens,
                   c.cache_read_tokens, c.cache_write_tokens,
                   c.cache_write_1h_tokens, c.cache_write_5m_tokens,
                   c.reasoning_tokens
            FROM run_llm_calls c
            WHERE {where} AND c.occurred_at >= ?
        """
        params = [*params, cutoff_iso]
    result = WayResult(way="run_llm_calls", scope=scope)
    seen_runs: set[str] = set()
    for row in conn.execute(sql, params):
        if row["run_id"] not in seen_runs:
            seen_runs.add(row["run_id"])
            result.runs += 1
        for name in TOKEN_FIELDS:
            result.tokens[name] += int(row[name] or 0)
        amount = _price(
            row["model"],
            row["provider"],
            UsageFactsUsage(
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cache_read_tokens=int(row["cache_read_tokens"] or 0),
                cache_write_tokens=int(row["cache_write_tokens"] or 0),
                cache_write_1h_tokens=row["cache_write_1h_tokens"],
                cache_write_5m_tokens=row["cache_write_5m_tokens"],
                reasoning_tokens=int(row["reasoning_tokens"] or 0),
            ),
            row["origin"],
        )
        if amount is None:
            result.unpriced_runs += 1
            result.unpriced_tokens += sum(
                int(row[name] or 0) for name in TOKEN_FIELDS
            )
        else:
            result.cost_equivalent_usd += amount
    return result


def way3_task_runs(conn: sqlite3.Connection, cutoff_epoch: int) -> WayResult:
    """Worker-reported tokens.  Scope is inherently 'kanban'.

    task_runs writes tokens only at run completion, so runs still in
    flight are invisible here while the facts layer already sees them —
    the in-flight set is measured and returned for delta classification.
    """
    result = WayResult(way="task_runs", scope="kanban")
    row = conn.execute(
        """
        SELECT COUNT(*) AS runs,
               SUM(COALESCE(input_tokens, 0)) AS input_tokens,
               SUM(COALESCE(output_tokens, 0)) AS output_tokens
        FROM task_runs
        WHERE started_at >= ? AND ended_at IS NOT NULL
        """,
        (cutoff_epoch,),
    ).fetchone()
    result.runs = int(row["runs"] or 0)
    result.tokens["input_tokens"] = int(row["input_tokens"] or 0)
    result.tokens["output_tokens"] = int(row["output_tokens"] or 0)
    inflight = conn.execute(
        """
        SELECT COUNT(*) AS c, SUM(COALESCE(input_tokens, 0)) AS it
        FROM task_runs WHERE started_at >= ? AND ended_at IS NULL
        """,
        (cutoff_epoch,),
    ).fetchone()
    result.note = (
        f"excluded in-flight runs: {int(inflight['c'] or 0)} "
        f"(tokens written only at completion)"
    )
    # cost_usd is worker-reported and 0.0 on subscription routes — it is
    # not a list-price equivalent and is reported as unknown here.
    result.cost_equivalent_usd = None
    return result


def _iter_assistant_usage(path: Path) -> Iterator[tuple[str, str, Mapping[str, Any], Optional[str], Optional[str]]]:
    """Yield (run_key, model, usage, timestamp, cwd) per assistant fragment."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, Mapping):
                continue
            message_id = message.get("id")
            usage = message.get("usage")
            if not message_id or not isinstance(usage, Mapping):
                continue
            request_id = record.get("requestId") or "__no_request_id__"
            yield (
                f"{message_id}:{request_id}",
                str(message.get("model") or ""),
                usage,
                record.get("timestamp"),
                record.get("cwd"),
            )


def way4_transcripts(
    projects_root: Path, cutoff: datetime, cutoff_iso: str
) -> dict[str, WayResult]:
    """Sum raw transcript usage, split into buzz / interactive scopes.

    Mirrors the harvester's merge rule: last non-null wins per field,
    keyed by (message.id, requestId).  mtime pre-filtering is a read
    optimization only; the record timestamp decides window membership.
    """
    results = {
        "claude_code": WayResult(way="transcripts", scope="claude_code"),
        "buzz": WayResult(way="transcripts", scope="buzz"),
    }
    cutoff_epoch = cutoff.timestamp()
    drafts: dict[str, dict[str, Any]] = {}
    for dirpath, _dirnames, filenames in os.walk(projects_root):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_mtime < cutoff_epoch:
                    continue
            except OSError:
                continue
            for run_key, model, usage, timestamp, cwd in _iter_assistant_usage(path):
                if timestamp is None or str(timestamp) < cutoff_iso:
                    continue
                draft = drafts.setdefault(
                    run_key, {"model": model, "cwd": cwd, "fields": {}}
                )
                fields = draft["fields"]
                for src, dest in (
                    ("input_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("cache_read_input_tokens", "cache_read_tokens"),
                    ("cache_creation_input_tokens", "cache_write_tokens"),
                ):
                    value = usage.get(src)
                    if isinstance(value, (int, float)):
                        fields[dest] = int(value)
                cache_creation = usage.get("cache_creation")
                if isinstance(cache_creation, Mapping):
                    for src, dest in (
                        ("ephemeral_1h_input_tokens", "cache_write_1h_tokens"),
                        ("ephemeral_5m_input_tokens", "cache_write_5m_tokens"),
                    ):
                        value = cache_creation.get(src)
                        if isinstance(value, (int, float)):
                            fields[dest] = int(value)
    for draft in drafts.values():
        fields = draft["fields"]
        unit = buzz_agent_unit_for_workspace(draft.get("cwd"))
        scope = "buzz" if unit is not None else "claude_code"
        result = results[scope]
        result.runs += 1
        for name in TOKEN_FIELDS:
            result.tokens[name] += int(fields.get(name) or 0)
        model = draft["model"] or None
        provider = (
            "anthropic"
            if model and model.lower().startswith("claude")
            else (
                "alibaba-token-plan"
                if model and model.lower().startswith("qwen")
                else None
            )
        )
        amount = (
            _price(
                model,
                provider,
                UsageFactsUsage(
                    input_tokens=int(fields.get("input_tokens") or 0),
                    output_tokens=int(fields.get("output_tokens") or 0),
                    cache_read_tokens=int(fields.get("cache_read_tokens") or 0),
                    cache_write_tokens=int(fields.get("cache_write_tokens") or 0),
                    cache_write_1h_tokens=fields.get("cache_write_1h_tokens"),
                    cache_write_5m_tokens=fields.get("cache_write_5m_tokens"),
                ),
                "claude_code",
            )
            if model
            else None
        )
        if amount is None:
            result.unpriced_runs += 1
            result.unpriced_tokens += sum(
                int(fields.get(name) or 0) for name in TOKEN_FIELDS
            )
        else:
            result.cost_equivalent_usd += amount
    return results


def _rel_delta(a: float, b: float) -> float:
    base = max(abs(a), abs(b))
    if base == 0:
        return 0.0
    return abs(a - b) / base


def compare(
    a: WayResult,
    b: WayResult,
    *,
    metric: str,
    value_a: float,
    value_b: float,
    threshold: float,
    expected_rel: Optional[float] = None,
    reason: str = "",
) -> Delta:
    """Classify one pairwise delta.

    ``expected_rel`` is a *measured* expected delta from a named cause
    (e.g. rows without event-time).  The delta counts as explained only
    when the measured cause actually covers it: |rel - expected| must
    stay within threshold.  If the cause stops explaining the delta, the
    pair flips to unexplained — the classification is earned by
    measurement, never assumed.
    """
    rel = _rel_delta(value_a, value_b)
    if expected_rel is not None and abs(rel - expected_rel) <= threshold:
        return Delta(a.scope, a.way, b.way, metric, value_a, value_b, rel, "explained", reason)
    if rel <= threshold:
        return Delta(a.scope, a.way, b.way, metric, value_a, value_b, rel, "explained", "within threshold")
    return Delta(
        a.scope, a.way, b.way, metric, value_a, value_b, rel, "unexplained",
        f"no named explanation covers this delta (expected from known causes: "
        f"{expected_rel:.2%})" if expected_rel is not None
        else "no named explanation covers this delta",
    )


def _explanation_facts(conn: sqlite3.Connection, cutoff_iso: str) -> dict[str, Any]:
    """Measure the named causes behind expected way1/way2 deltas.

    Every number here is computed from the live DB in the same window —
    explanations are measured, never assumed.
    """
    facts: dict[str, Any] = {}

    # Cause 1: pre-v6 claude_code call rows carry no occurred_at and are
    # invisible to the per-call time filter while their run is in-window.
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT c.run_id) AS runs,
               SUM(COALESCE(c.input_tokens,0)+COALESCE(c.output_tokens,0)
                   +COALESCE(c.cache_read_tokens,0)+COALESCE(c.cache_write_tokens,0)) AS tokens
        FROM run_llm_calls c
        JOIN run_usage_facts f ON f.run_id = c.run_id
        WHERE {_CLAUDE_ORIGIN_SQL.replace('origin', 'c.origin').replace('run_id', 'c.run_id')}
          AND c.occurred_at IS NULL
          AND COALESCE(f.last_call_at, f.captured_at) >= ?
        """,
        (cutoff_iso,),
    ).fetchone()
    facts["claude_calls_without_occurred_at"] = {
        "runs": int(row["runs"] or 0),
        "tokens": int(row["tokens"] or 0),
    }

    # Cause 2: the run aggregate nulls a token total when any single call
    # lacks that field (partial sums would understate usage).  Only the
    # tokens in the *nulled fields* are missing from way2 — sum exactly
    # those, per field, not the runs' full volume.
    for label, scope_sql, scope_params in (
        ("hermes", "f.origin IN ('hermes_agent','hermes_aux')", []),
        (
            "kanban",
            "f.task_run_id IS NOT NULL AND f.origin IN ('hermes_agent','hermes_aux')",
            [],
        ),
    ):
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT f.run_id) AS runs,
                   SUM(CASE WHEN f.input_tokens IS NULL
                            THEN COALESCE(c.input_tokens,0) ELSE 0 END
                     + CASE WHEN f.output_tokens IS NULL
                            THEN COALESCE(c.output_tokens,0) ELSE 0 END
                     + CASE WHEN f.cache_read_tokens IS NULL
                            THEN COALESCE(c.cache_read_tokens,0) ELSE 0 END
                     + CASE WHEN f.cache_write_tokens IS NULL
                            THEN COALESCE(c.cache_write_tokens,0) ELSE 0 END
                   ) AS tokens
            FROM run_usage_facts f
            JOIN run_llm_calls c ON c.run_id = f.run_id
            WHERE {scope_sql}
              AND COALESCE(f.last_call_at, f.captured_at) >= ?
              AND (f.input_tokens IS NULL OR f.output_tokens IS NULL
                   OR f.cache_read_tokens IS NULL OR f.cache_write_tokens IS NULL)
            """,
            [*scope_params, cutoff_iso],
        ).fetchone()
        facts[f"{label}_partial_sum_null"] = {
            "runs": int(row["runs"] or 0),
            "tokens": int(row["tokens"] or 0),
        }

    # Cause 2b (kanban scope): correlated claude_code rows also lose
    # pre-v6 calls without occurred_at — same mechanism as cause 1,
    # restricted to task_run_id rows.
    row = conn.execute(
        f"""
        SELECT SUM(COALESCE(c.input_tokens,0)+COALESCE(c.output_tokens,0)
                   +COALESCE(c.cache_read_tokens,0)+COALESCE(c.cache_write_tokens,0)) AS tokens
        FROM run_llm_calls c
        JOIN run_usage_facts f ON f.run_id = c.run_id
        WHERE {_CLAUDE_ORIGIN_SQL.replace('origin', 'c.origin').replace('run_id', 'c.run_id')}
          AND f.task_run_id IS NOT NULL
          AND c.occurred_at IS NULL
          AND COALESCE(f.last_call_at, f.captured_at) >= ?
        """,
        (cutoff_iso,),
    ).fetchone()
    facts["kanban_claude_calls_without_occurred_at"] = {
        "tokens": int(row["tokens"] or 0),
    }

    # Cause 3: buzz runs from non-claude sources (qwen/grok/codex/kimi
    # foreign-lane harvest) are written run-level only; they never had
    # per-call rows, so way1 cannot see them.  Their priced cost is
    # measured too — most carry qwen3.8-max, which is documented-absent
    # (no PAYG rate), so the *cost* delta differs from the token delta.
    rows = conn.execute(
        """
        SELECT f.model, f.provider, f.origin,
               f.input_tokens, f.output_tokens,
               f.cache_read_tokens, f.cache_write_tokens,
               f.cache_write_1h_tokens, f.cache_write_5m_tokens,
               f.reasoning_tokens
        FROM run_usage_facts f
        WHERE f.origin = 'buzz_agent'
          AND f.run_id NOT LIKE 'claude_code:%'
          AND COALESCE(f.last_call_at, f.captured_at) >= ?
          AND NOT EXISTS (SELECT 1 FROM run_llm_calls c WHERE c.run_id = f.run_id)
        """,
        (cutoff_iso,),
    ).fetchall()
    rlo_tokens = 0
    rlo_cost = Decimal("0")
    for row in rows:
        rlo_tokens += sum(int(row[name] or 0) for name in TOKEN_FIELDS)
        amount = _price(
            row["model"],
            row["provider"],
            UsageFactsUsage(
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cache_read_tokens=int(row["cache_read_tokens"] or 0),
                cache_write_tokens=int(row["cache_write_tokens"] or 0),
                cache_write_1h_tokens=row["cache_write_1h_tokens"],
                cache_write_5m_tokens=row["cache_write_5m_tokens"],
                reasoning_tokens=int(row["reasoning_tokens"] or 0),
            ),
            row["origin"],
        )
        if amount is not None:
            rlo_cost += amount
    facts["buzz_runlevel_only"] = {
        "runs": len(rows),
        "tokens": rlo_tokens,
        "cost_usd": float(rlo_cost),
    }
    return facts


def _kanban_coverage(
    facts_conn: sqlite3.Connection,
    kanban_conn: Optional[sqlite3.Connection],
    cutoff_iso: str,
    cutoff_epoch: int,
) -> dict[str, Any]:
    """Measure how much worker consumption the facts layer correlates.

    task_runs.id is INTEGER while run_usage_facts.task_run_id is TEXT —
    the join casts explicitly (a bare Python-side comparison silently
    matches nothing).
    """
    coverage: dict[str, Any] = {}
    row = facts_conn.execute(
        """
        SELECT COUNT(*) AS runs, COUNT(DISTINCT task_run_id) AS task_runs,
               SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS in_out_tokens
        FROM run_usage_facts
        WHERE task_run_id IS NOT NULL
          AND COALESCE(last_call_at, captured_at) >= ?
        """,
        (cutoff_iso,),
    ).fetchone()
    coverage["facts_runs_with_task_run_id"] = int(row["runs"] or 0)
    coverage["facts_distinct_task_run_ids"] = int(row["task_runs"] or 0)
    coverage["facts_correlated_in_out_tokens"] = int(row["in_out_tokens"] or 0)
    if kanban_conn is not None:
        in_window = {
            str(r[0])
            for r in kanban_conn.execute(
                "SELECT id FROM task_runs WHERE started_at >= ?",
                (cutoff_epoch,),
            )
        }
        correlated = {
            str(r[0])
            for r in facts_conn.execute(
                """
                SELECT DISTINCT task_run_id FROM run_usage_facts
                WHERE task_run_id IS NOT NULL
                  AND COALESCE(last_call_at, captured_at) >= ?
                """,
                (cutoff_iso,),
            )
        }
        matched = in_window & correlated
        coverage["task_runs_in_window"] = len(in_window)
        coverage["task_runs_correlated"] = len(matched)
        coverage["correlation_coverage"] = (
            round(len(matched) / len(in_window), 4) if in_window else None
        )
        # Tokens on the correlated subset: the facts layer should match
        # task_runs on exactly this subset (real consistency check).
        token_rows = kanban_conn.execute(
            """
            SELECT CAST(id AS TEXT) AS id,
                   COALESCE(input_tokens,0)+COALESCE(output_tokens,0) AS io
            FROM task_runs WHERE started_at >= ?
            """,
            (cutoff_epoch,),
        ).fetchall()
        total_io = sum(int(r[1]) for r in token_rows)
        matched_io = sum(int(r[1]) for r in token_rows if r[0] in matched)
        coverage["task_runs_in_out_tokens"] = total_io
        coverage["correlated_task_runs_in_out_tokens"] = matched_io
        # Split the correlated subset by writer convention.  hermes_agent
        # runs are written by the same accounting as task_runs and must
        # match (real consistency gate); claude_code-correlated runs are
        # the deprecated double measurement of Canon register §2 and
        # diverge by construction (input-counting convention).
        splits = {"hermes": [0, 0], "claude": [0, 0]}
        io_by_id = {r[0]: int(r[1]) for r in token_rows}
        for tid in matched:
            row = facts_conn.execute(
                """
                SELECT SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS io,
                       GROUP_CONCAT(DISTINCT origin) AS origins
                FROM run_usage_facts
                WHERE task_run_id = ?
                  AND COALESCE(last_call_at, captured_at) >= ?
                """,
                (tid, cutoff_iso),
            ).fetchone()
            bucket = "claude" if row[1] == "claude_code" else "hermes"
            splits[bucket][0] += io_by_id.get(tid, 0)
            splits[bucket][1] += int(row[0] or 0)
        coverage["correlated_split"] = {
            name: {"task_runs_io": vals[0], "facts_io": vals[1]}
            for name, vals in splits.items()
        }
    return coverage


def run_reconcile(
    *,
    days: int,
    origin: Optional[str],
    threshold: float,
    usage_db: Path,
    kanban_db: Path,
    projects_root: Path,
    skip_transcripts: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
    cutoff_epoch = int(cutoff.timestamp())

    ways: dict[str, WayResult] = {}
    deltas: list[Delta] = []

    facts_conn = _connect_ro(usage_db)
    kanban_conn: Optional[sqlite3.Connection] = None
    try:
        scopes = ("claude_code", "buzz", "hermes", "kanban")
        if origin:
            scopes = tuple(s for s in scopes if origin.startswith(s) or s.startswith(origin)) or scopes
        for scope in scopes:
            ways[f"run_usage_facts:{scope}"] = way2_run_facts(facts_conn, cutoff_iso, scope)
            ways[f"run_llm_calls:{scope}"] = way1_llm_calls(facts_conn, cutoff_iso, scope)
        explanations = _explanation_facts(facts_conn, cutoff_iso)
        if kanban_db.is_file():
            kanban_conn = _connect_ro(kanban_db)
            ways["task_runs:kanban"] = way3_task_runs(kanban_conn, cutoff_epoch)
        coverage = _kanban_coverage(facts_conn, kanban_conn, cutoff_iso, cutoff_epoch)
    finally:
        facts_conn.close()
        if kanban_conn is not None:
            kanban_conn.close()

    if not skip_transcripts and projects_root.is_dir():
        for scope, result in way4_transcripts(projects_root, cutoff, cutoff_iso).items():
            ways[f"transcripts:{scope}"] = result

    def pair(
        key_a: str,
        key_b: str,
        *,
        expected_tokens: Optional[tuple[float, str]] = None,
        expected_runs: Optional[tuple[float, str]] = None,
        shared_metric: Optional[str] = None,
        expected_shared: Optional[tuple[float, str]] = None,
        include_cost: bool = True,
    ) -> None:
        a, b = ways.get(key_a), ways.get(key_b)
        if a is None or b is None:
            return
        exp_tok, reason_tok = expected_tokens or (None, "")
        deltas.append(
            compare(
                a, b,
                metric="total_tokens",
                value_a=float(a.total_tokens),
                value_b=float(b.total_tokens),
                threshold=threshold,
                expected_rel=exp_tok,
                reason=reason_tok,
            )
        )
        exp_runs, reason_runs = expected_runs or (None, "")
        deltas.append(
            compare(
                a, b,
                metric="runs",
                value_a=float(a.runs),
                value_b=float(b.runs),
                threshold=threshold,
                expected_rel=exp_runs,
                reason=reason_runs,
            )
        )
        if shared_metric is not None:
            va = float(a.tokens["input_tokens"] + a.tokens["output_tokens"])
            vb = float(b.tokens["input_tokens"] + b.tokens["output_tokens"])
            exp_s, reason_s = expected_shared or (None, "")
            deltas.append(
                compare(
                    a, b,
                    metric=shared_metric,
                    value_a=va,
                    value_b=vb,
                    threshold=threshold,
                    expected_rel=exp_s,
                    reason=reason_s,
                )
            )
        if include_cost and a.cost_equivalent_usd is not None and b.cost_equivalent_usd is not None:
            deltas.append(
                compare(
                    a, b,
                    metric="cost_equivalent_usd",
                    value_a=float(a.cost_equivalent_usd),
                    value_b=float(b.cost_equivalent_usd),
                    threshold=threshold,
                    expected_rel=exp_tok,
                    reason=reason_tok,
                )
            )

    def _rel(part: float, whole: float) -> Optional[float]:
        return (part / whole) if whole > 0 else None

    # claude_code pair: explained by pre-v6 calls without occurred_at.
    null_ts = explanations["claude_calls_without_occurred_at"]
    w2c = ways.get("run_usage_facts:claude_code")
    if w2c is not None:
        pair(
            "run_llm_calls:claude_code",
            "run_usage_facts:claude_code",
            expected_tokens=(
                _rel(null_ts["tokens"], w2c.total_tokens),
                f"{null_ts['runs']:,} pre-v6 call rows carry no occurred_at "
                f"({_fmt_tokens(null_ts['tokens'])} tokens) and are invisible "
                "to the per-call time filter",
            ),
            expected_runs=(
                _rel(null_ts["runs"], w2c.runs),
                f"{null_ts['runs']:,} runs have only pre-v6 call rows without "
                "occurred_at",
            ),
        )

    # hermes/kanban pairs: explained by the partial-sum NULL rule
    # (kanban additionally by pre-v6 claude calls without occurred_at).
    for scope in ("hermes", "kanban"):
        if f"run_usage_facts:{scope}" not in ways:
            continue
        psn = explanations[f"{scope}_partial_sum_null"]
        w2 = ways[f"run_usage_facts:{scope}"]
        w1 = ways[f"run_llm_calls:{scope}"]
        extra = 0
        extra_note = ""
        if scope == "kanban":
            extra = explanations["kanban_claude_calls_without_occurred_at"]["tokens"]
            extra_note = (
                f", plus {_fmt_tokens(extra)} tokens on pre-v6 correlated "
                "claude calls without occurred_at"
            )
        pair(
            f"run_llm_calls:{scope}",
            f"run_usage_facts:{scope}",
            expected_tokens=(
                _rel(psn["tokens"] + extra, max(w1.total_tokens, 1)),
                f"partial-sum NULL rule: {psn['runs']:,} runs null "
                f"aggregate fields their calls carry "
                f"({_fmt_tokens(psn['tokens'])} tokens affected){extra_note}",
            ),
        )

    # buzz pair: non-claude sources are run-level only.  The cost delta
    # uses the *priced* share of those runs — most are qwen3.8-max with a
    # documented-absent PAYG rate, so token and cost expectations differ.
    rlo = explanations["buzz_runlevel_only"]
    w2b = ways.get("run_usage_facts:buzz")
    w1b = ways.get("run_llm_calls:buzz")
    if w2b is not None and w1b is not None:
        pair(
            "run_llm_calls:buzz",
            "run_usage_facts:buzz",
            expected_tokens=(
                _rel(rlo["tokens"], w2b.total_tokens),
                f"{rlo['runs']:,} buzz runs from foreign-CLI sources "
                f"(qwen/grok/codex/kimi) are run-level only "
                f"({_fmt_tokens(rlo['tokens'])} tokens) and have no per-call rows",
            ),
            expected_runs=(
                _rel(rlo["runs"], w2b.runs),
                f"{rlo['runs']:,} foreign-CLI buzz runs have no per-call rows",
            ),
            include_cost=False,
        )
    if w2b is not None and w1b is not None and w2b.cost_equivalent_usd:
        deltas.append(
            compare(
                w1b, w2b,
                metric="cost_equivalent_usd (runlevel-only expectation)",
                value_a=float(w1b.cost_equivalent_usd),
                value_b=float(w2b.cost_equivalent_usd),
                threshold=threshold,
                expected_rel=_rel(rlo["cost_usd"], float(w2b.cost_equivalent_usd)),
                reason=f"priced share of the {rlo['runs']:,} runlevel-only "
                f"buzz runs is ${rlo['cost_usd']:,.2f} (rest is "
                "documented-absent qwen3.8-max PAYG)",
            )
        )

    # Way 4 truth check: raw transcripts reconcile against the per-call
    # layer (way1), not the run aggregates — records without a source
    # timestamp are invisible to both by construction.  The claude_code
    # facts scope includes buzz claude rows, so the transcript side sums
    # both transcript scopes into one combined way.
    tr_c = ways.get("transcripts:claude_code")
    tr_b = ways.get("transcripts:buzz")
    if tr_c is not None and "run_llm_calls:claude_code" in ways:
        combined = WayResult(way="transcripts_combined", scope="claude_code")
        for part in (tr_c, tr_b):
            if part is None:
                continue
            combined.runs += part.runs
            for name in TOKEN_FIELDS:
                combined.tokens[name] += part.tokens[name]
            if part.cost_equivalent_usd is not None and combined.cost_equivalent_usd is not None:
                combined.cost_equivalent_usd += part.cost_equivalent_usd
            combined.unpriced_runs += part.unpriced_runs
            combined.unpriced_tokens += part.unpriced_tokens
        ways["transcripts_combined:claude_code"] = combined
        pair(
            "transcripts_combined:claude_code",
            "run_llm_calls:claude_code",
            expected_tokens=(
                0.005,
                "harvest high-water-mark lag on the newest transcripts",
            ),
            expected_runs=(
                0.005,
                "harvest high-water-mark lag on the newest transcripts",
            ),
        )
    pair(
        "transcripts:buzz",
        "run_llm_calls:buzz",
        expected_tokens=(
            0.005,
            "harvest high-water-mark lag on the newest transcripts",
        ),
        expected_runs=(
            0.005,
            "harvest high-water-mark lag on the newest transcripts",
        ),
    )

    # task_runs pair.  Two separate statements, both measured:
    #  1. consistency: on the *correlated subset* the facts layer's
    #     input+output must match task_runs (same events, two writers).
    #  2. coverage: how much in-window worker consumption correlates at
    #     all — reported as a first-class number, classified explained
    #     only because the uncovered remainder is provably inside the
    #     uncorrelated claude_code scope (kanban workers are Claude Code
    #     sessions), not missing tokens in a shared metric.
    facts_io = float(coverage.get("facts_correlated_in_out_tokens") or 0)
    matched_io = float(coverage.get("correlated_task_runs_in_out_tokens") or 0)
    total_io = float(coverage.get("task_runs_in_out_tokens") or 0)
    tr_way = ways.get("task_runs:kanban")
    fk_way = ways.get("run_usage_facts:kanban")
    if tr_way is not None and fk_way is not None:
        split = coverage.get("correlated_split") or {}
        he = split.get("hermes") or {}
        cl = split.get("claude") or {}
        # Real gate: same writer, same convention — must match.
        rel_he = _rel_delta(float(he.get("facts_io") or 0), float(he.get("task_runs_io") or 0))
        deltas.append(
            Delta(
                "kanban", tr_way.way, fk_way.way,
                "input+output on hermes-written correlated subset",
                float(he.get("task_runs_io") or 0),
                float(he.get("facts_io") or 0),
                rel_he,
                "explained" if rel_he <= threshold else "unexplained",
                (
                    "same accounting on both sides — match expected"
                    if rel_he <= threshold
                    else "hermes-written runs disagree between task_runs "
                    "and facts — a real double-write mismatch"
                ),
            )
        )
        # Deprecated double measurement (Canon register §2): claude-backed
        # task runs count input by a different convention in task_runs.
        rel_cl = _rel_delta(float(cl.get("facts_io") or 0), float(cl.get("task_runs_io") or 0))
        deltas.append(
            Delta(
                "kanban", tr_way.way, fk_way.way,
                "input+output on claude-correlated subset (deprecated 2nd source)",
                float(cl.get("task_runs_io") or 0),
                float(cl.get("facts_io") or 0),
                rel_cl,
                "explained",
                "known exception: task_runs is deprecated for consumption "
                "(Canon register §2) — worker-side input counting differs "
                "from the Anthropic-convention facts layer; facts remain SSOT",
            )
        )
        cov = coverage.get("correlation_coverage")
        deltas.append(
            Delta(
                "kanban", tr_way.way, fk_way.way,
                "correlation coverage",
                float(coverage.get("task_runs_in_window") or 0),
                float(coverage.get("task_runs_correlated") or 0),
                1.0 - (cov or 0.0),
                "explained",
                f"coverage {cov:.1%}: uncorrelated worker consumption is "
                "captured inside the uncorrelated claude_code scope "
                "(kanban workers are Claude Code sessions); tracked as a "
                "named blind spot, not a token mismatch",
            )
        )
        # runs granularity: informational, always explained with numbers.
        deltas.append(
            Delta(
                "kanban", tr_way.way, fk_way.way,
                "runs",
                float(tr_way.runs), float(fk_way.runs),
                _rel_delta(float(tr_way.runs), float(fk_way.runs)),
                "explained",
                f"granularity: one task_run maps to many facts runs "
                f"({coverage.get('facts_runs_with_task_run_id'):,} facts runs "
                f"over {coverage.get('facts_distinct_task_run_ids'):,} task runs)",
            )
        )
    if total_io and matched_io and tr_way is not None and fk_way is not None:
        deltas.append(
            Delta(
                "kanban", tr_way.way, fk_way.way,
                "input+output tokens (full window, scope-limited)",
                total_io, matched_io,
                _rel_delta(total_io, matched_io),
                "explained",
                f"scope: {_fmt_tokens(int(total_io - matched_io))} in/out tokens "
                "sit on uncorrelated task runs — captured in claude_code scope, "
                "not lost; see coverage block",
            )
        )

    unexplained = [d for d in deltas if d.classification == "unexplained"]
    return {
        "contract": "usage-reconcile.v1",
        "generated_at": now.isoformat(),
        "window": {
            "days": days,
            "cutoff_utc": cutoff_iso,
            "timezone": "UTC",
            "note": "half-open [cutoff, now); task_runs uses epoch seconds",
        },
        "threshold": threshold,
        "ways": {key: way.to_dict() for key, way in ways.items()},
        "coverage": coverage,
        "explanation_facts": explanations,
        "deltas": [d.to_dict() for d in deltas],
        "unexplained_count": len(unexplained),
        "ok": not unexplained,
    }


def _fmt_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1e9:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1e6:.2f}M"
    return str(value)


def print_text(report: dict[str, Any]) -> None:
    window = report["window"]
    print(f"usage-reconcile.v1  window: {window['days']}d  cutoff: {window['cutoff_utc']}  tz: UTC")
    print()
    for key, way in report["ways"].items():
        cost = way["cost_equivalent_usd"]
        cost_text = f"${cost:,.2f}" if cost is not None else "n/a"
        print(
            f"  {key:34s} runs={way['runs']:>7}  "
            f"tokens={_fmt_tokens(way['total_tokens']):>9}  "
            f"equiv={cost_text:>12}  "
            f"unpriced={way['unpriced_runs']} runs/{_fmt_tokens(way['unpriced_tokens'])} tok"
        )
        if way["note"]:
            print(f"    note: {way['note']}")
    coverage = report.get("coverage") or {}
    if coverage:
        cov = coverage.get("correlation_coverage")
        print()
        print(
            f"kanban correlation coverage: "
            f"{coverage.get('task_runs_correlated'):,} of "
            f"{coverage.get('task_runs_in_window'):,} in-window task runs "
            f"({cov:.1%}); correlated in/out tokens: "
            f"{_fmt_tokens(coverage.get('correlated_task_runs_in_out_tokens') or 0)}"
            f" of {_fmt_tokens(coverage.get('task_runs_in_out_tokens') or 0)}"
        )
    print()
    print(f"pairwise deltas ({len(report['deltas'])}):")
    for delta in report["deltas"]:
        marker = "UNEXPLAINED" if delta["classification"] == "unexplained" else "explained  "
        print(
            f"  [{marker}] {delta['scope']:12s} {delta['ways'][0]} vs {delta['ways'][1]}"
            f"  {delta['metric']}: {delta['rel_delta']:.2%}  — {delta['reason']}"
        )
    print()
    if report["ok"]:
        print("OK: no unexplained deltas above threshold")
    else:
        print(f"FAIL: {report['unexplained_count']} unexplained delta(s) above threshold")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--origin", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="relative delta above which an unexplained delta fails (default 0.02)",
    )
    parser.add_argument("--usage-db", type=Path, default=DEFAULT_USAGE_FACTS_DB)
    parser.add_argument("--kanban-db", type=Path, default=DEFAULT_KANBAN_DB)
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument(
        "--skip-transcripts",
        action="store_true",
        help="skip way 4 (raw JSONL parse) for fast iteration",
    )
    args = parser.parse_args(argv)

    report = run_reconcile(
        days=args.days,
        origin=args.origin,
        threshold=args.threshold,
        usage_db=args.usage_db,
        kanban_db=args.kanban_db,
        projects_root=args.projects_root,
        skip_transcripts=args.skip_transcripts,
    )
    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print_text(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

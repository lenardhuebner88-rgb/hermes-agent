"""Record-only routing recommendations for comparable Kanban dispatches."""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

MIN_COMPARABLE_COMPLETIONS = 30
DEFAULT_WINDOW = 40
MAX_WINDOW = 50
_MANIFEST_KEYS = (
    "audience",
    "chars",
    "omitted_records",
    "payload_fingerprint",
    "phase",
    "profile",
    "renderer_version",
    "section_counts",
    "token_estimate",
)

ValueClassifier = Callable[..., str]


def normalize_routing_shadow_window(value: object) -> int:
    """Return a supported window, falling back instead of widening the query."""
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_WINDOW
    if not MIN_COMPARABLE_COMPLETIONS <= value <= MAX_WINDOW:
        return DEFAULT_WINDOW
    return value


def _manifest(metadata: object) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    brief = metadata.get("brief")
    if not isinstance(brief, dict):
        return None
    if brief.get("renderer_version") != "worker-brief-v1":
        return None
    if any(key not in brief or brief[key] is None for key in _MANIFEST_KEYS):
        return None
    if not all(
        isinstance(brief[key], str) and bool(brief[key])
        for key in ("audience", "payload_fingerprint", "phase", "profile")
    ):
        return None
    if len(brief["payload_fingerprint"]) != 64:
        return None
    if not all(
        isinstance(brief[key], int)
        and not isinstance(brief[key], bool)
        and brief[key] >= 0
        for key in ("chars", "omitted_records", "token_estimate")
    ):
        return None
    if not isinstance(brief["section_counts"], dict):
        return None
    return brief


def _route(provider: object, model: object) -> dict[str, str] | None:
    if not isinstance(provider, str) or not provider.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return {"provider": provider, "model": model}


def _current_candidate(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    value_classifier: ValueClassifier,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            t.kind,
            t.review_tier,
            t.workspace_kind,
            t.created_by,
            t.title,
            t.epic_id,
            r.metadata,
            r.requested_provider,
            r.requested_model,
            r.active_provider,
            r.active_model
        FROM tasks AS t
        JOIN task_runs AS r ON r.task_id = t.id
        WHERE t.id = ? AND r.id = ?
        """,
        (task_id, run_id),
    ).fetchone()
    if row is None or row["kind"] != "code":
        return None
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError):
        return None
    brief = _manifest(metadata)
    if brief is None:
        return None
    requested_route = _route(row["requested_provider"], row["requested_model"])
    actual_route = _route(row["active_provider"], row["active_model"])
    if requested_route is None or actual_route is None:
        return None
    return {
        "phase": brief["phase"],
        "review_tier": row["review_tier"],
        "workspace_kind": row["workspace_kind"],
        "context_profile": brief["profile"],
        "value_class": value_classifier(
            row["created_by"], title=row["title"], epic_id=row["epic_id"]
        ),
        "requested_route": requested_route,
        "actual_route": actual_route,
    }


def _comparable_completions(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    value_classifier: ValueClassifier,
) -> list[sqlite3.Row]:
    conn.create_function(
        "routing_shadow_value_class",
        3,
        lambda created_by, title, epic_id: value_classifier(
            created_by, title=title, epic_id=epic_id
        ),
    )
    # LIMIT 50 is a deliberate ceiling even when a caller bypasses config loading.
    return conn.execute(
        """
        SELECT
            r.active_provider,
            r.active_model,
            r.input_tokens,
            r.output_tokens
        FROM task_runs AS r
        JOIN tasks AS t ON t.id = r.task_id
        WHERE t.status = 'done'
          AND t.kind = 'code'
          AND r.status = 'done'
          AND r.outcome = 'completed'
          AND r.input_tokens IS NOT NULL
          AND r.output_tokens IS NOT NULL
          AND r.input_tokens >= 0
          AND r.output_tokens >= 0
          AND NULLIF(r.requested_provider, '') IS NOT NULL
          AND NULLIF(r.requested_model, '') IS NOT NULL
          AND NULLIF(r.active_provider, '') IS NOT NULL
          AND NULLIF(r.active_model, '') IS NOT NULL
          AND json_valid(r.metadata)
          AND json_extract(r.metadata, '$.brief.renderer_version') = 'worker-brief-v1'
          AND json_extract(r.metadata, '$.brief.phase') = ?
          AND json_extract(r.metadata, '$.brief.profile') = ?
          AND json_type(r.metadata, '$.brief.audience') = 'text'
          AND NULLIF(json_extract(r.metadata, '$.brief.audience'), '') IS NOT NULL
          AND json_type(r.metadata, '$.brief.chars') = 'integer'
          AND json_extract(r.metadata, '$.brief.chars') >= 0
          AND json_type(r.metadata, '$.brief.omitted_records') = 'integer'
          AND json_extract(r.metadata, '$.brief.omitted_records') >= 0
          AND json_type(r.metadata, '$.brief.payload_fingerprint') = 'text'
          AND length(json_extract(r.metadata, '$.brief.payload_fingerprint')) = 64
          AND json_type(r.metadata, '$.brief.section_counts') = 'object'
          AND json_type(r.metadata, '$.brief.token_estimate') = 'integer'
          AND json_extract(r.metadata, '$.brief.token_estimate') >= 0
          AND t.workspace_kind = ?
          AND (t.review_tier = ? OR (t.review_tier IS NULL AND ? IS NULL))
          AND routing_shadow_value_class(t.created_by, t.title, t.epic_id) = ?
        ORDER BY r.ended_at DESC, r.id DESC
        LIMIT 50
        """,
        (
            candidate["phase"],
            candidate["context_profile"],
            candidate["workspace_kind"],
            candidate["review_tier"],
            candidate["review_tier"],
            candidate["value_class"],
        ),
    ).fetchall()


def _recommendation(rows: list[sqlite3.Row]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    token_totals: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        route = (str(row["active_provider"]), str(row["active_model"]))
        token_totals[route].append(int(row["input_tokens"]) + int(row["output_tokens"]))
    summaries = []
    for (provider, model), totals in sorted(token_totals.items()):
        summaries.append(
            {
                "provider": provider,
                "model": model,
                "completions": len(totals),
                "median_total_tokens": statistics.median(totals),
            }
        )
    winner = min(
        summaries,
        key=lambda item: (
            item["median_total_tokens"],
            -item["completions"],
            item["provider"],
            item["model"],
        ),
    )
    return {"provider": winner["provider"], "model": winner["model"]}, summaries


def record_routing_shadow_decision(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    window: object = DEFAULT_WINDOW,
    value_classifier: ValueClassifier,
    now: int,
) -> dict[str, Any] | None:
    """Record one content-free recommendation without changing any route field."""
    if conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'routing_shadow_decision' LIMIT 1",
        (task_id, run_id),
    ).fetchone():
        return None
    candidate = _current_candidate(
        conn, task_id=task_id, run_id=run_id, value_classifier=value_classifier
    )
    if candidate is None:
        return None
    configured_window = normalize_routing_shadow_window(window)
    eligible = _comparable_completions(
        conn, candidate, value_classifier=value_classifier
    )
    if len(eligible) < MIN_COMPARABLE_COMPLETIONS:
        return None
    cohort = eligible[:configured_window]
    recommendation, route_observations = _recommendation(cohort)
    payload = {
        "version": 1,
        "comparison_key": {
            "phase": candidate["phase"],
            "review_tier": candidate["review_tier"],
            "workspace_kind": candidate["workspace_kind"],
            "context_profile": candidate["context_profile"],
            "value_class": candidate["value_class"],
        },
        "configured_window": configured_window,
        "eligible_completions_seen": len(eligible),
        "window_completions": len(cohort),
        "recommendation_basis": "lowest_median_total_tokens",
        "recommendation": recommendation,
        "requested_route": candidate["requested_route"],
        "actual_route": candidate["actual_route"],
        "route_observations": route_observations,
    }
    inserted = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "SELECT ?, ?, 'routing_shadow_decision', ?, ? "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'routing_shadow_decision'"
        ")",
        (
            task_id,
            run_id,
            json.dumps(payload, sort_keys=True),
            int(now),
            task_id,
            run_id,
        ),
    )
    return payload if inserted.rowcount == 1 else None

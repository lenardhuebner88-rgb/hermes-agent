"""Read-side helpers for the dedicated Strategist surface in /control (G1).

The Strategist (the Phase-1.5 ``strategist-cron``) drafts ROI-positive PlanSpecs,
self-gates them, and ingests the survivors with ``freigabe: operator`` so they
land *held* (root parked in ``scheduled``) instead of building. This module is
the read side the dashboard uses to surface those held proposals for fast
operator triage (approve → release, veto → dismiss).

Two contracts live here, deliberately decoupled from the writers:

1. **Annotation contract** (paired with I1, the strategist harness). Each held
   proposal carries a *Ziel-Kennzahl* (target metric), an *ROI* estimate and a
   *gepaarte Counter-Metrik* (the guardrail the lever must not regress). The
   strategist stamps these into the held **root task body** as a single
   machine-readable block — :data:`STRATEGIST_META_MARKER`. :func:`format_annotation`
   emits it (I1 imports this so both sides agree); :func:`parse_annotation`
   reads it back, tolerant of a missing block / missing keys (degrades to
   ``None`` so a proposal without annotations still shows up, just bare).

2. **Metrics snapshot contract** (paired with H1, the metrics CLI). H1 writes a
   distilled snapshot to ``~/.hermes/state/vision-metrics.json``;
   :func:`read_vision_metrics` reads it defensively (missing file / bad JSON →
   ``None``) so the surface degrades to "no snapshot yet" rather than 500.

Neither H1 nor I1 needs to have landed for this surface to work — absent inputs
degrade gracefully. The reviewer-join (J1) reconciles the exact shapes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

__all__ = [
    "STRATEGIST_META_MARKER",
    "format_annotation",
    "parse_annotation",
    "vision_metrics_path",
    "read_vision_metrics",
    "held_operator_proposals",
    "_parse_planspec_title",
    "_classify_source",
]

# The body marker the strategist stamps and the surface parses. An HTML comment
# so it never renders in the proposal's Markdown body, with ``key: value`` lines
# (or a single JSON object) between the fences.
STRATEGIST_META_MARKER = "strategist-meta"

# Canonical annotation keys, in display order.
_ANNOTATION_KEYS = ("target_metric", "roi", "counter_metric", "grounding")

# Accept a couple of natural aliases so a hand-written or slightly-different
# strategist emission still parses instead of silently dropping to bare.
_ANNOTATION_ALIASES = {
    "target_metric": "target_metric",
    "ziel": "target_metric",
    "ziel_kennzahl": "target_metric",
    "ziel-kennzahl": "target_metric",
    "target": "target_metric",
    "metric": "target_metric",
    "roi": "roi",
    "roi_estimate": "roi",
    "roi_schaetzung": "roi",
    "counter_metric": "counter_metric",
    "counter": "counter_metric",
    "counter-metric": "counter_metric",
    "gegen_metrik": "counter_metric",
    "guardrail": "counter_metric",
    "grounding": "grounding",
    "grounding_evidenz": "grounding",
    "evidenz": "grounding",
    "evidence": "grounding",
    "beleg": "grounding",
}

_BLOCK_RE = re.compile(
    r"<!--\s*" + re.escape(STRATEGIST_META_MARKER) + r"\s*(.*?)-->",
    re.DOTALL | re.IGNORECASE,
)

# Matches "PlanSpec <key>: <rest>" — the form the strategist-cron stamps on titles.
_PLANSPEC_TITLE_RE = re.compile(r"^PlanSpec\s+(\S+):\s*(.*)$", re.IGNORECASE)


def _parse_planspec_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """Return (key, rest) when *title* matches ``PlanSpec <key>: <rest>``.

    Returns ``(None, None)`` when the title does not match the pattern.
    """
    m = _PLANSPEC_TITLE_RE.match(title.strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _classify_source(key: Optional[str]) -> str:
    """Classify a parsed PlanSpec *key* into a source category.

    Pure function of the key string — no I/O.
    """
    if key is None:
        return "other"
    upper = key.upper()
    if key.lower().startswith("receipt-"):
        return "receipt"
    if "AUTOHEAL" in upper or upper.startswith("GREEN-GATE"):
        return "gate"
    return "metric"


def format_annotation(
    *,
    target_metric: Optional[str] = None,
    roi: Optional[str] = None,
    counter_metric: Optional[str] = None,
    grounding: Optional[str] = None,
) -> str:
    """Render the strategist annotation block for embedding in a root body.

    I1 imports this so the emit/parse round-trips. Keys with empty values are
    omitted; an all-empty call still emits the (empty) marker so the contract
    is visible. Values are single-lined to keep the block ``key: value`` clean.
    ``grounding`` (STRATEGIST-SELF-GROUNDING-S1) is the per-lever code/git-log
    evidence; it is surfaced like the other fields and omitted when empty.
    """
    def _clean(value: Optional[str]) -> str:
        return " ".join(str(value).split()) if value else ""

    lines = [f"<!-- {STRATEGIST_META_MARKER}"]
    for key, value in (
        ("target_metric", target_metric),
        ("roi", roi),
        ("counter_metric", counter_metric),
        ("grounding", grounding),
    ):
        cleaned = _clean(value)
        if cleaned:
            lines.append(f"{key}: {cleaned}")
    lines.append("-->")
    return "\n".join(lines)


def parse_annotation(body: Optional[str]) -> dict[str, Optional[str]]:
    """Extract the strategist annotation from a held root's body.

    Returns ``{"target_metric": ..., "roi": ..., "counter_metric": ...,
    "grounding": ...}`` with ``None`` for any key the body does not carry. A
    body without the marker yields an all-``None`` dict (the proposal still
    surfaces, just unannotated).
    The block may carry either ``key: value`` lines or a single JSON object.
    """
    result: dict[str, Optional[str]] = {key: None for key in _ANNOTATION_KEYS}
    if not body:
        return result
    match = _BLOCK_RE.search(body)
    if not match:
        return result
    inner = match.group(1).strip()
    if not inner:
        return result

    # JSON form: <!-- strategist-meta {"target_metric": "...", ...} -->
    if inner.startswith("{"):
        try:
            obj = json.loads(inner)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            for raw_key, raw_value in obj.items():
                canon = _ANNOTATION_ALIASES.get(str(raw_key).strip().lower())
                if canon and raw_value not in (None, ""):
                    result[canon] = " ".join(str(raw_value).split())
            return result

    # key: value lines
    for line in inner.splitlines():
        if ":" not in line:
            continue
        raw_key, _, raw_value = line.partition(":")
        canon = _ANNOTATION_ALIASES.get(raw_key.strip().lower())
        value = raw_value.strip()
        if canon and value:
            result[canon] = value
    return result


def vision_metrics_path() -> Path:
    """Resolve the distilled metrics file H1 writes.

    ``HERMES_VISION_METRICS_PATH`` is the explicit override; otherwise delegate to
    the writer (``vision_metrics.metrics_snapshot_path``) so the reader and the H1
    writer resolve the SAME path — including under ``HERMES_VISION_STATE_DIR``,
    which both now honour (tests/sandboxes set one var, not two).
    """
    import os

    override = os.environ.get("HERMES_VISION_METRICS_PATH", "").strip()
    if override:
        return Path(override)
    try:
        from hermes_cli import vision_metrics as _vm

        return _vm.metrics_snapshot_path()
    except Exception:
        return get_hermes_home() / "state" / "vision-metrics.json"


def read_vision_metrics() -> Optional[dict[str, Any]]:
    """Read the distilled metrics snapshot, or ``None`` if absent/unreadable.

    Defensive on purpose: a missing file (H1 not run yet), a partial write or
    malformed JSON must degrade to "no snapshot" context, never raise into the
    poll path.
    """
    path = vision_metrics_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def held_operator_proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the held ``freigabe: operator`` proposal roots for the surface.

    Held = root parked in ``scheduled`` with ``freigabe = operator`` (F1). Only
    the root row carries ``freigabe`` (children of a decomposed chain never do),
    so this filter is itself the root-guard — build-children can never appear.
    Each entry is annotated with the parsed target/ROI/counter-metric and the
    number of held subtasks the chain would dispatch on approval.
    """
    rows = conn.execute(
        "SELECT id, title, body, created_by, created_at "
        "FROM tasks "
        "WHERE status = 'scheduled' "
        "  AND LOWER(TRIM(COALESCE(freigabe, ''))) = 'operator' "
        "ORDER BY created_at DESC",
    ).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        task_id = row["id"]
        raw_title: str = row["title"] or ""
        # Children are linked as the root's parents (decompose link direction).
        subtask_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        annotation = parse_annotation(row["body"])

        # Provenance classification.
        key, rest = _parse_planspec_title(raw_title)
        source = _classify_source(key)
        display_title = rest if key is not None else raw_title

        # Origin lookup: for receipt-keyed proposals resolve the origin task title.
        origin: Optional[str] = None
        if source == "receipt" and key is not None:
            origin_id = key[len("receipt-"):]
            origin_row = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (origin_id,)
            ).fetchone()
            if origin_row:
                origin = origin_row["title"]

        proposals.append(
            {
                "id": task_id,
                "title": raw_title,
                "display_title": display_title,
                "source": source,
                "origin": origin,
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "subtask_count": len(subtask_rows),
                "target_metric": annotation["target_metric"],
                "roi": annotation["roi"],
                "counter_metric": annotation["counter_metric"],
                "grounding": annotation["grounding"],
            }
        )
    return proposals

"""Persist and reconcile Strategist PlanSpec decisions.

The Kanban board is authoritative for execution.  These helpers mirror an
explicit operator freigabe decision into the source PlanSpec without making the
board action depend on filesystem availability.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path
import re
from typing import Any

import yaml

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)

TERMINAL_DECISION_STATUSES = frozenset({"vetoed", "archived", "done"})
_DECISION_SUFFIX = re.compile(r"-[0-9a-f]{8}$", re.IGNORECASE)


def utc_now() -> str:
    """Return an unambiguous, frontmatter-safe UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("PlanSpec has no YAML frontmatter")
    closing = text.find("\n---", 4)
    if closing < 0:
        raise ValueError("PlanSpec frontmatter is not closed")
    frontmatter = yaml.safe_load(text[4:closing]) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("PlanSpec frontmatter must be a mapping")
    return frontmatter, text[closing + 4 :]


def update_spec_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    """Apply *updates* to a real PlanSpec frontmatter block, preserving its body."""
    path = Path(path)
    frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    frontmatter.update(updates)
    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{rendered}\n---{body}", encoding="utf-8")


def persist_task_decision(
    conn: Any,
    task_id: str,
    *,
    status: str,
    author: str,
    decided_at: str | None = None,
) -> Path | None:
    """Best-effort writeback for a successful Board decision.

    Missing, unreadable, or malformed source files deliberately only log a
    warning: the preceding Board transition is already committed and must not be
    undone by this audit mirror.
    """
    source = kb.planspec_source_for_task(conn, task_id)
    if not source:
        logger.info("strategist decision %s has no PlanSpec source", task_id)
        return None
    path = Path(source)
    try:
        update_spec_frontmatter(
            path,
            {
                "status": status,
                "decision_at": decided_at or utc_now(),
                "decision_by": author,
            },
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("strategist decision writeback failed for %s (%s): %s", task_id, path, exc)
        return None
    return path


def canonical_lever_key(value: object) -> str:
    """Remove the generated hash suffix used by the gate PlanSpec families."""
    return _DECISION_SUFFIX.sub("", str(value or "").strip().upper())


def _frontmatter_for_path(path: Path) -> dict[str, Any] | None:
    try:
        frontmatter, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return frontmatter


def has_terminal_decision_for_lever(plans_root: Path, lever_key: str) -> bool:
    """Whether a source PlanSpec already has a terminal decision for this lever.

    Gate specs append a random eight-hex digest to both their slice and filename;
    comparison therefore uses the frontmatter ``slice`` and canonicalizes only
    that suffix instead of treating filename identity as lever identity.
    """
    target = canonical_lever_key(lever_key)
    if not target:
        return False
    try:
        candidates = Path(plans_root).glob("*.md")
        for path in candidates:
            frontmatter = _frontmatter_for_path(path)
            if not frontmatter:
                continue
            if str(frontmatter.get("status") or "").strip().lower() not in TERMINAL_DECISION_STATUSES:
                continue
            if canonical_lever_key(frontmatter.get("slice")) == target:
                return True
    except OSError as exc:
        logger.warning("strategist decision dedupe scan failed for %s: %s", plans_root, exc)
    return False


def _board_status_for_source(conn: Any, path: Path) -> str | None:
    source = str(path.resolve(strict=False))
    rows = conn.execute(
        "SELECT id, status FROM tasks WHERE planspec_source = ?", (source,)
    ).fetchall()
    if not rows:
        return None
    task_ids = [str(row["id"]) for row in rows]
    placeholders = ", ".join("?" for _ in task_ids)
    event_rows = conn.execute(
        f"SELECT kind FROM task_events WHERE task_id IN ({placeholders})", task_ids
    ).fetchall()
    if any(str(row["kind"]) == "freigabe_vetoed" for row in event_rows):
        return "vetoed"
    statuses = {str(row["status"] or "") for row in rows}
    if "done" in statuses:
        return "done"
    if "archived" in statuses:
        return "archived"
    return None


def reconcile_proposed_specs(
    conn: Any,
    *,
    plans_root: Path,
    apply: bool = False,
    author: str = "strategist-reconciler",
) -> list[dict[str, str]]:
    """Classify proposed PlanSpecs against the Board, optionally writing statuses.

    A source absent from the Board is explicitly marked ``nie-eingespielt`` on
    apply so it is distinguishable from an unresolved, still-proposed decision.
    """
    results: list[dict[str, str]] = []
    for path in sorted(Path(plans_root).glob("*.md")):
        frontmatter = _frontmatter_for_path(path)
        if not frontmatter or str(frontmatter.get("status") or "").strip().lower() != "vorgeschlagen":
            continue
        board_status = _board_status_for_source(conn, path)
        status = board_status or "nie-eingespielt"
        result = {"path": str(path), "status": status, "action": "would-update" if apply else "dry-run"}
        if apply:
            try:
                update_spec_frontmatter(
                    path,
                    {
                        "status": status,
                        "reconciled_at": utc_now(),
                        "reconciled_by": author,
                    },
                )
                result["action"] = "updated"
            except (OSError, ValueError, yaml.YAMLError) as exc:
                logger.warning("strategist spec reconciliation failed for %s: %s", path, exc)
                result["action"] = "failed"
        results.append(result)
    return results

"""Family Organizer write-back edge for terminal Kanban completions.

The Kanban core emits a durable completion lifecycle hook. This project edge
owns the Family Organizer markdown convention and never participates in the
core completion transaction.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db


BACKLOG_IDEMPOTENCY_PREFIX = "fo-backlog:"
DEFAULT_BACKLOG_DIR = "/home/piet/projects/family-organizer/backlog/items"
BACKLOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESULT_MAX_CHARS = 500


def register_lifecycle_hooks() -> None:
    """Register the project write-back as an idempotent completion observer."""
    from hermes_cli.plugins import get_plugin_manager

    callbacks = get_plugin_manager()._hooks.setdefault("kanban_task_completed", [])
    if handle_task_completed not in callbacks:
        callbacks.append(handle_task_completed)


def _backlog_dir() -> Path:
    return Path(
        os.environ.get("FAMILY_ORGANIZER_BACKLOG_DIR", DEFAULT_BACKLOG_DIR)
    ).expanduser()


def _backlog_item_id(idempotency_key: Optional[str]) -> Optional[str]:
    raw_key = str(idempotency_key or "").strip()
    if not raw_key.startswith(BACKLOG_IDEMPOTENCY_PREFIX):
        return None
    item_id = raw_key[len(BACKLOG_IDEMPOTENCY_PREFIX) :].strip()
    if not item_id:
        return None
    if item_id.isdigit():
        item_id = item_id.zfill(4)
    return item_id if BACKLOG_ID_RE.fullmatch(item_id) else None


def _parse_flat_frontmatter(text: str) -> tuple[dict[str, str], list[str], int] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        return None
    data: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip():
            data[key.strip()] = value.strip()
    return data, lines, end


def _find_backlog_item_path(base: Path, item_id: str) -> Optional[Path]:
    try:
        root = base.resolve(strict=False)
    except OSError:
        return None
    if not root.is_dir():
        return None

    candidates = [root / f"{item_id}.md"]
    try:
        candidates.extend(sorted(root.glob(f"{item_id}-*.md")))
    except OSError:
        pass
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            if resolved.is_file() and resolved.is_relative_to(root):
                return resolved
        except OSError:
            continue

    try:
        entries = sorted(root.glob("*.md"))
    except OSError:
        return None
    for candidate in entries:
        try:
            resolved = candidate.resolve(strict=False)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                continue
            parsed = _parse_flat_frontmatter(resolved.read_text(encoding="utf-8"))
        except OSError:
            continue
        if parsed is None:
            continue
        frontmatter, _lines, _end = parsed
        frontmatter_id = str(frontmatter.get("id") or "").strip()
        if frontmatter_id.isdigit():
            frontmatter_id = frontmatter_id.zfill(4)
        if frontmatter_id == item_id:
            return resolved
    return None


def _single_line(value: Any) -> Optional[str]:
    text = " ".join(str(value).strip().split()) if value is not None else ""
    return text[:RESULT_MAX_CHARS] if text else None


def _result_text(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str],
    result: Optional[str],
    fallback_title: Optional[str],
) -> str:
    try:
        row = conn.execute(
            "SELECT summary FROM task_runs "
            "WHERE task_id = ? AND outcome = 'completed' AND status = 'review' "
            "  AND summary IS NOT NULL AND TRIM(summary) != '' "
            "ORDER BY COALESCE(ended_at, started_at) ASC, id ASC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            handoff = _single_line(row["summary"])
            if handoff:
                return handoff
    except sqlite3.Error:
        pass
    for candidate in (summary, result, fallback_title):
        text = _single_line(candidate)
        if text:
            return text
    return f"Hermes task {task_id} completed."


def _set_frontmatter_field(lines: list[str], end: int, key: str, value: str) -> int:
    for index in range(1, end):
        line = lines[index]
        if ":" in line and line.split(":", 1)[0].strip() == key:
            lines[index] = f"{key}: {value}"
            return end
    lines.insert(end, f"{key}: {value}")
    return end + 1


def _close_backlog_item(
    path: Path,
    *,
    completed_at: int,
    result_text: str,
) -> bool:
    parsed = _parse_flat_frontmatter(path.read_text(encoding="utf-8"))
    if parsed is None:
        return False
    _frontmatter, lines, end = parsed
    updated = dt.datetime.fromtimestamp(
        int(completed_at), tz=dt.timezone.utc
    ).date().isoformat()
    end = _set_frontmatter_field(lines, end, "status", "done")
    end = _set_frontmatter_field(lines, end, "updated", updated)
    _set_frontmatter_field(lines, end, "result", result_text)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def handle_task_completed(
    *,
    task_id: str,
    board: Optional[str] = None,
    run_id: Optional[int] = None,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    completed_at: Optional[int] = None,
    **_: Any,
) -> None:
    """Close a linked markdown backlog item after durable terminal completion."""
    try:
        with kanban_db.connect_closing(board=board) as conn:
            row = conn.execute(
                "SELECT title, tenant, idempotency_key, result FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row or row["tenant"] != "family-organizer":
                return
            item_id = _backlog_item_id(row["idempotency_key"])
            if item_id is None:
                return
            item_path = _find_backlog_item_path(_backlog_dir(), item_id)
            if item_path is None:
                return
            outcome = _result_text(
                conn,
                task_id,
                summary=summary,
                result=result if result is not None else row["result"],
                fallback_title=row["title"],
            )
            if not _close_backlog_item(
                item_path,
                completed_at=(
                    int(completed_at) if completed_at is not None else int(time.time())
                ),
                result_text=outcome,
            ):
                return
            kanban_db.add_event(
                conn,
                task_id,
                "family_organizer_backlog_closed",
                {
                    "item_id": item_id,
                    "path": str(item_path),
                    "status": "done",
                    "result": outcome,
                },
                run_id=run_id,
            )
    except Exception:
        # A project edge must never roll back or mask terminal core completion.
        return

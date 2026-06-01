"""Read-only Family-Organizer backlog board endpoint.

Serves the repo-native backlog of the family-organizer project as JSON for the
Hermes Control dashboard's read-only "Family Organizer" tab. Parses the
frontmatter contract documented in the family-organizer repo's
``backlog/README.md`` (section "Feld-Vertrag").

This is a human overview only: no writes, no second source of truth. The git
repo stays the SSoT; the git-claim (fast-forward push) stays the mutex for
parallel writers. Items are read fresh from the homeserver filesystem; no auth
is needed for the read itself, but the route lives under ``/api/`` so it inherits
the dashboard's session-token gate in gated mode.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI

_SCHEMA = "fo-backlog-v1"
_DEFAULT_DIR = "/home/piet/projects/family-organizer/backlog/items"
# in_progress/blocked items whose `updated` is older than this are flagged stale
# (mirrors the Stale-Claim-Sweep in family-organizer backlog/README.md).
_STALE_AFTER_S = 7 * 24 * 3600
_STATUSES = ("now", "next", "in_progress", "blocked", "later", "done")


def _backlog_dir() -> Path:
    return Path(os.environ.get("FAMILY_ORGANIZER_BACKLOG_DIR", _DEFAULT_DIR))


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Return the leading frontmatter block as a flat str->str dict.

    Parses line-by-line, splitting each ``key: value`` on the FIRST colon — the
    same semantics as the family-organizer validator (``scripts/backlog-check.mjs``).
    Deliberately NOT ``yaml.safe_load``: real items carry human-written values with
    embedded ``": "`` (e.g. ``result: ...; Follow-ups: (a) ...``), which is invalid
    YAML and would make a YAML parse drop the whole item. The backlog frontmatter is
    always flat single-line scalars, so first-colon splitting is correct and robust.
    A closing ``---`` is required; ``---`` rules in the body are ignored.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    data: dict[str, Any] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip()
        if key:
            data[key] = line[idx + 1:].strip()
    return data


def _updated_epoch(value: Any) -> int | None:
    """Parse an ISO ``YYYY-MM-DD`` value into UTC epoch seconds, else None."""
    if value is None:
        return None
    try:
        d = dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def _read_items_sync(now: int) -> dict[str, Any]:
    base = _backlog_dir().resolve()
    counts = {s: 0 for s in _STATUSES}
    if not base.is_dir():
        return {
            "schema": _SCHEMA,
            "checked_at": now,
            "items": [],
            "counts": counts,
            "source": {"dir": str(base), "count": 0},
            "error": f"backlog dir not found: {base}",
        }

    items: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.md")):
        rp = path.resolve()
        if not rp.is_relative_to(base):  # symlink-escape guard
            continue
        try:
            text = rp.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        if not fm:
            continue

        status = str(fm.get("status") or "").strip()
        updated = fm.get("updated")
        epoch = _updated_epoch(updated)
        stale = (
            status in ("in_progress", "blocked")
            and epoch is not None
            and (now - epoch) > _STALE_AFTER_S
        )
        items.append(
            {
                # Filename prefix is the canonical 4-digit id (the family-organizer
                # validator enforces id == filename); the YAML-parsed `id` is not
                # trustworthy — PyYAML coerces "0001" to the int 1 (YAML 1.1 octal).
                "id": path.stem[:4],
                "title": str(fm.get("title") or path.stem),
                "status": status,
                "owner": str(fm.get("owner") or "unassigned"),
                "risk": str(fm.get("risk") or ""),
                "area": str(fm.get("area") or ""),
                "updated": str(updated) if updated is not None else "",
                "lane": str(fm.get("lane")) if fm.get("lane") is not None else None,
                "result": str(fm.get("result")) if fm.get("result") is not None else None,
                "stale": bool(stale),
            }
        )
        if status in counts:
            counts[status] += 1

    items.sort(key=lambda it: it["id"])
    return {
        "schema": _SCHEMA,
        "checked_at": now,
        "items": items,
        "counts": counts,
        "source": {"dir": str(base), "count": len(items)},
        "error": None,
    }


async def _get_backlog() -> dict[str, Any]:
    now = int(time.time())
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return await loop.run_in_executor(executor, _read_items_sync, now)
    finally:
        executor.shutdown(wait=True)


def register_backlog_routes(app: FastAPI) -> None:
    """Register the read-only family-organizer backlog endpoint before the SPA catch-all."""

    @app.get("/api/family-organizer/backlog")
    async def family_organizer_backlog() -> dict[str, Any]:
        return await _get_backlog()

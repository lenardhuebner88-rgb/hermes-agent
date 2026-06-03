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
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from hermes_cli.error_sanitize import scrub_detail

_SCHEMA = "fo-backlog-v1"
_DEFAULT_DIR = "/home/piet/projects/family-organizer/backlog/items"
# in_progress/blocked items whose `updated` is older than this are flagged stale
# (mirrors the Stale-Claim-Sweep in family-organizer backlog/README.md).
_STALE_AFTER_S = 7 * 24 * 3600
_STATUSES = ("now", "next", "in_progress", "blocked", "later", "done")
_log = logging.getLogger(__name__)


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


def _ref() -> str:
    return os.environ.get("FAMILY_ORGANIZER_BACKLOG_REF", "origin/main")


def _git(root: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)


def _read_sources_from_git(base: Path) -> list[tuple[str, str]] | None:
    """(name, text) pairs from the committed ref (default ``origin/main``).

    Reads *committed* content, so a dirty or behind working tree in the canonical
    clone (concurrent agents leave it modified/stale) can't make the board show
    partial or out-of-date data. Returns ``None`` when ``base`` isn't inside a git
    repo or the ref is missing — the caller then falls back to the working tree.
    """
    try:
        top = _git(str(base), ["rev-parse", "--show-toplevel"])
        if top.returncode != 0:
            return None
        root = top.stdout.strip()
        rel = base.resolve().relative_to(Path(root).resolve()).as_posix()
        ref = _ref()
        listing = _git(root, ["ls-tree", "-r", "--name-only", ref, "--", rel])
        if listing.returncode != 0:
            return None
        sources: list[tuple[str, str]] = []
        for path in listing.stdout.splitlines():
            if not path.endswith(".md"):
                continue
            blob = _git(root, ["show", f"{ref}:{path}"])
            if blob.returncode == 0:
                sources.append((path.rsplit("/", 1)[-1], blob.stdout))
        return sources
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _read_sources_from_fs(base: Path) -> list[tuple[str, str]]:
    """(name, text) pairs from the working tree — fallback when the git read fails."""
    sources: list[tuple[str, str]] = []
    for path in sorted(base.glob("*.md")):
        rp = path.resolve()
        if not rp.is_relative_to(base):  # symlink-escape guard
            continue
        try:
            sources.append((path.name, rp.read_text(encoding="utf-8")))
        except OSError:
            continue
    return sources


def _read_items_sync(now: int) -> dict[str, Any]:
    base = _backlog_dir().resolve()
    counts = {s: 0 for s in _STATUSES}

    sources = _read_sources_from_git(base)
    source_ref = f"git:{_ref()}"
    if sources is None:
        if not base.is_dir():
            return {
                "schema": _SCHEMA,
                "checked_at": now,
                "items": [],
                "counts": counts,
                "source": {"dir": str(base), "ref": "missing", "count": 0},
                "error": scrub_detail(f"backlog dir not found: {base}"),
            }
        sources = _read_sources_from_fs(base)
        source_ref = "fs:working-tree"

    items: list[dict[str, Any]] = []
    for name, text in sources:
        stem = name[:-3] if name.endswith(".md") else name
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
                # Canonical 4-digit id from the filename (the family-organizer
                # validator enforces id == filename); the YAML-parsed `id` is not
                # trustworthy — PyYAML coerces "0001" to the int 1 (YAML 1.1 octal).
                "id": stem[:4],
                "title": str(fm.get("title") or stem),
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
        "source": {"dir": str(base), "ref": source_ref, "count": len(items)},
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


def _detail_error(message: str) -> dict[str, str]:
    return {"error": message}


def _validate_detail_id(item_id: str) -> tuple[str | None, dict[str, str] | None]:
    raw = str(item_id or "")
    if not raw.strip():
        return None, _detail_error("invalid id: expected 4 digits")
    if "/" in raw or "\\" in raw or ".." in raw:
        return None, _detail_error("invalid id")
    if len(raw) != 4 or not raw.isdigit():
        return None, _detail_error("invalid id: expected 4 digits")
    return raw, None


def _body_after_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return ""
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1:])
    return ""


def _read_item_detail_sync(item_id: str, now: int) -> dict[str, Any]:
    valid_id, error = _validate_detail_id(item_id)
    if error is not None:
        return error

    base = _backlog_dir().resolve()
    try:
        sources = _read_sources_from_git(base)
        if sources is None:
            return _detail_error("backlog source unavailable")

        for name, text in sources:
            if not (name.endswith(".md") and name.startswith(f"{valid_id}-")):
                continue

            stem = name[:-3]
            fm = _parse_frontmatter(text)
            if not fm:
                return _detail_error("item frontmatter not found")

            status = str(fm.get("status") or "").strip()
            updated = fm.get("updated")
            epoch = _updated_epoch(updated)
            stale = (
                status in ("in_progress", "blocked")
                and epoch is not None
                and (now - epoch) > _STALE_AFTER_S
            )
            return {
                "id": stem[:4],
                "title": str(fm.get("title") or stem),
                "status": status,
                "owner": str(fm.get("owner") or "unassigned"),
                "risk": str(fm.get("risk") or ""),
                "area": str(fm.get("area") or ""),
                "updated": str(updated) if updated is not None else "",
                "lane": str(fm.get("lane")) if fm.get("lane") is not None else None,
                "result": str(fm.get("result")) if fm.get("result") is not None else None,
                "stale": bool(stale),
                "body": _body_after_frontmatter(text),
            }
        return _detail_error("item not found")
    except Exception as exc:
        _log.exception("family organizer backlog detail unavailable")
        message = scrub_detail(str(exc)) or "backlog detail unavailable"
        return _detail_error(message)


async def _get_backlog_detail(item_id: str) -> dict[str, Any]:
    now = int(time.time())
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return await loop.run_in_executor(executor, _read_item_detail_sync, item_id, now)
    finally:
        executor.shutdown(wait=True)


def register_backlog_routes(app: FastAPI) -> None:
    """Register the read-only family-organizer backlog endpoint before the SPA catch-all."""

    @app.get("/api/family-organizer/backlog")
    async def family_organizer_backlog() -> dict[str, Any]:
        return await _get_backlog()

    @app.get("/api/family-organizer/backlog/{item_id:path}")
    async def family_organizer_backlog_detail(item_id: str) -> dict[str, Any]:
        return await _get_backlog_detail(item_id)

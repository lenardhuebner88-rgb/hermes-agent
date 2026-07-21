"""Read-only KI-News feed from the research-profile Frontier cron outputs.

Slice B2 (Jarvis-Produktreife): ``GET /api/pa/news?limit=`` serves the newest
Frontier Desk / Frontier Flash digests straight from the existing cron output
files — no cron rebuild. The KiLageTicker frontend is switched onto this
endpoint in a follow-up slice; its degraded mode (404 → usePaFeed) is already in
place, so this route is purely additive.

Contract (``pa-news/v1``)::

    {"version": "pa-news/v1",
     "items": [{"title", "ts", "tag", "summary", "markdown"}, ...],
     "errors": ["<deutsche Kurzinfo>", ...]}

Robustness is load-bearing here because grok is rebuilding the crons in
parallel, so the display names and section layout drift:

* Jobs are addressed by **ID only** (whitelist below) — never parsed by name.
* ``[SILENT]`` responses and gate-skip files (``wakeAgent=false``, no
  ``## Response`` section) are skipped silently — they yield no item and no
  error status.
* Empty state (holiday idle, all skipped) and a missing output directory both
  return ``200`` with ``items: []`` — never a 5xx. Structural problems are
  reported as short German notes in ``errors[]``.
* A 60 s module-level cache (thread-safe enough for FastAPI) keeps the file
  scans off the request hot path.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI

logger = logging.getLogger(__name__)

SCHEMA = "pa-news/v1"

# Frontier jobs, addressed by ID only. The display name inside the output files
# drifts (grok rebuilds the crons), so the fixed label here is the stable tag.
JOBS: dict[str, str] = {
    "5a2a54ac3dae": "Frontier Desk",
    "4c88cd4449a6": "Frontier Flash",
}

CACHE_TTL_S = 60.0
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
# Most recent files scanned per job — generous enough to fill MAX_LIMIT items
# even across many SILENT/gate-skip days, bounded for the 60 s cache.
MAX_FILES_PER_JOB = 25
MAX_MARKDOWN_BYTES = 8 * 1024  # 8 KB cap per item markdown.
TITLE_MAX = 120
SUMMARY_MAX = 200
_TRUNCATION_MARKER = "\n\n[… gekürzt]"

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_RUN_TIME_RE = re.compile(r"\*\*Run Time:\*\*\s*([^\n]+)")
_RESPONSE_HEADING_RE = re.compile(r"^##\s+Response\s*$")

# Module-level cache. ``_cache_lock`` guards the pair; a torn read of the tuple
# reference is harmless (worst case two threads both refresh after a miss).
_cache_lock = threading.Lock()
_cache_at: float = 0.0
_cache_data: Optional[tuple[list[dict[str, Any]], list[str]]] = None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _output_root() -> Path:
    """Root dir holding the per-job cron output folders.

    The outputs live in the RESEARCH profile, not under ``get_hermes_home()``'s
    default profile. ``HERMES_RESEARCH_CRON_OUTPUT`` overrides outright; else
    ``HERMES_HOME`` (honoured for tests) or ``~/.hermes`` is the base.
    """
    override = os.environ.get("HERMES_RESEARCH_CRON_OUTPUT")
    if override:
        return Path(override)
    base = os.environ.get("HERMES_HOME")
    root = Path(base) if base else Path.home() / ".hermes"
    return root / "profiles" / "research" / "cron" / "output"


# ---------------------------------------------------------------------------
# Parsing helpers (defensive against format drift — never raise)
# ---------------------------------------------------------------------------


def _extract_response(text: str) -> Optional[str]:
    """Return the agent answer under ``## Response`` (to next ``## `` or EOF).

    Code fences are tracked so a ``## `` line inside a fenced Script Output
    block neither starts nor terminates the response. Returns ``None`` for
    gate-skip files / missing or empty section.
    """
    lines = text.splitlines()
    start: Optional[int] = None
    in_fence = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and _RESPONSE_HEADING_RE.match(line):
            start = i + 1
            break
    if start is None:
        return None

    body: list[str] = []
    in_fence = False
    for line in lines[start:]:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            body.append(line)
            continue
        # A level-2 heading ("## ") ends the response; "### " does not start
        # with "## " so section sub-headings inside the answer are kept.
        if not in_fence and line.startswith("## "):
            break
        body.append(line)
    response = "\n".join(body).strip()
    return response or None


def _strip_markdown(text: str) -> str:
    """Reduce inline Markdown markup to plain text (links, code, emphasis)."""
    t = text
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)  # images → alt text
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # links → label
    t = re.sub(r"`([^`]*)`", r"\1", t)  # inline code
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)  # bold **
    t = re.sub(r"__([^_]+)__", r"\1", t)  # bold __
    t = re.sub(r"(?<!\*)\*([^*\s][^*]*?)\*(?!\*)", r"\1", t)  # italic *
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _clip(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars at a word boundary, appending an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def _derive_title(response: str) -> str:
    """Title = first non-empty line (the digest headline in the real format).

    The Frontier answers open with a bold headline (``[FYI] **🧠 …**`` /
    ``**⚡ …**``), not a ``#`` heading; a heading is honoured if it ever leads.
    """
    for raw in response.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADING_RE.match(line)
        if m:
            line = m.group(1).strip()
        title = _strip_markdown(line)
        title = re.sub(r"^\[[A-Z]+\]\s*", "", title).strip()  # drop [FYI]-tag
        if title:
            return _clip(title, TITLE_MAX)
    return "KI-News"


def _is_metadata_line(text: str) -> bool:
    """Detect meta tuples like ``Tag: aktiv · Top: Qwen · Konfidenz: Medium``."""
    return text.count("·") >= 2 and len(text) < 100


def _derive_summary(response: str, title: str) -> str:
    """First prose paragraph after the headline, ~200 chars, markup stripped."""
    skipped_headline = False
    collected: list[str] = []
    total = 0
    for raw in response.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not skipped_headline:
            skipped_headline = True  # the headline is already the title
            continue
        if _HEADING_RE.match(line):
            continue
        cleaned = _strip_markdown(line)
        cleaned = re.sub(r"^[-*]\s+", "", cleaned)  # bullet marker
        cleaned = re.sub(r"^\d+\.\s+", "", cleaned)  # numbered marker
        cleaned = cleaned.strip()
        if not cleaned or cleaned == title or _is_metadata_line(cleaned):
            continue
        collected.append(cleaned)
        total += len(cleaned)
        if total >= SUMMARY_MAX:
            break
    return _clip(" ".join(collected), SUMMARY_MAX)


def _cap_markdown(text: str) -> str:
    """Cap the response body at 8 KB (UTF-8), appending a truncation marker."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_MARKDOWN_BYTES:
        return text
    budget = MAX_MARKDOWN_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
    truncated = encoded[:budget].decode("utf-8", errors="ignore")
    return truncated + _TRUNCATION_MARKER


def _to_unix(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    try:
        return int(datetime(y, mo, d, h, mi, s).timestamp())
    except (ValueError, OverflowError, OSError):
        return 0


def _parse_ts(text: str, filename: str) -> int:
    """Unix seconds from the Run-Time meta, falling back to the filename."""
    m_run = _RUN_TIME_RE.search(text)
    if m_run:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", m_run.group(1))
        if m:
            ts = _to_unix(*map(int, m.groups()))
            if ts:
                return ts
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", filename)
    if m:
        ts = _to_unix(*map(int, m.groups()))
        if ts:
            return ts
    return 0


# ---------------------------------------------------------------------------
# Collection + cache
# ---------------------------------------------------------------------------


def _build_item(tag: str, path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse one output file into an item. Skips yield ``(None, None)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{tag}: Cron-Output {path.name} nicht lesbar ({exc.__class__.__name__})."

    response = _extract_response(text)
    if not response:
        return None, None  # gate-skip or missing/empty Response section
    if response.strip() == "[SILENT]":
        return None, None  # silent run — no news

    title = _derive_title(response)
    item = {
        "title": title,
        "ts": _parse_ts(text, path.name),
        "tag": tag,
        "summary": _derive_summary(response, title),
        "markdown": _cap_markdown(response),
    }
    return item, None


def _collect_items() -> tuple[list[dict[str, Any]], list[str]]:
    """Scan both jobs' newest files; return (items newest-first, errors)."""
    root = _output_root()
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for job_id, tag in JOBS.items():
        job_dir = root / job_id  # job_id is whitelist-only → no traversal
        if not job_dir.is_dir():
            errors.append(f"{tag}: Cron-Output-Verzeichnis fehlt ({job_dir}).")
            continue
        try:
            files = sorted(job_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
        except OSError as exc:
            errors.append(f"{tag}: Cron-Output-Verzeichnis nicht lesbar ({exc.__class__.__name__}).")
            continue
        for path in files[:MAX_FILES_PER_JOB]:
            item, err = _build_item(tag, path)
            if err:
                errors.append(err)
            if item:
                items.append(item)
    items.sort(key=lambda it: it["ts"], reverse=True)
    return items, errors


def _get_items() -> tuple[list[dict[str, Any]], list[str]]:
    """Cache wrapper around ``_collect_items`` (60 s TTL)."""
    global _cache_at, _cache_data
    now = time.monotonic()
    with _cache_lock:
        if _cache_data is not None and (now - _cache_at) < CACHE_TTL_S:
            return _cache_data
    items, errors = _collect_items()
    with _cache_lock:
        _cache_at = time.monotonic()
        _cache_data = (items, errors)
    return items, errors


def _reset_cache() -> None:
    """Drop the cache (tests)."""
    global _cache_at, _cache_data
    with _cache_lock:
        _cache_at = 0.0
        _cache_data = None


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


def register_pa_news_routes(app: FastAPI) -> None:
    """Mount ``GET /api/pa/news`` (additive; inherits the /api/ session gate)."""

    @app.get("/api/pa/news")
    async def get_pa_news(limit: int = DEFAULT_LIMIT):
        limit = max(1, min(limit, MAX_LIMIT))
        try:
            items, errors = _get_items()
        except Exception as exc:  # defensive: never a 5xx into the dashboard
            logger.exception("pa_news: collection failed")
            return {
                "version": SCHEMA,
                "items": [],
                "errors": [f"KI-News konnten nicht geladen werden ({exc.__class__.__name__})."],
            }
        return {"version": SCHEMA, "items": items[:limit], "errors": errors}

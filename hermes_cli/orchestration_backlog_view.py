"""Read-only Orchestrator backlog board endpoint.

Serves the orchestration workspace's planning backlog (``~/orchestration/backlog/*.md``)
as JSON for the Hermes Control dashboard's read-only "Orchestrator" tab. Parses the
Backlog.md-style frontmatter (``status``, ``priority``, ``dependsOn``, ``planGate``)
— a *different* schema than the family-organizer board, so this is a deliberately
separate view/endpoint (no premature generalisation; see the slice's anti-scope note).
Known status counts are contract-checked, but unknown status values are preserved on
the item and reported as drift instead of being silently remapped.

Unlike the family-organizer board (which reads the committed ``origin/main`` of a pushed
product repo), this backlog is **living planning scratch** — often uncommitted
in-progress — so it reads the **working tree** directly. The route lives under ``/api/``
so it inherits the dashboard's session-token gate in gated mode. Read-only: no writes,
no status flips; the Markdown-in-git stays the single source of truth.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Coarse Markdown-stripping for excerpt extraction. Only the leading-line patterns matter:
# headings, list bullets, blockquote markers, and inline decoration (backticks, asterisks).
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")
_MD_LIST_RE = re.compile(r"^\s*[-*+]\s+|^\s*\d+\.\s+")
_MD_QUOTE_RE = re.compile(r"^\s*>\s*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
_MD_INLINE_RE = re.compile(r"`[^`]*`|\*+|_+")

from fastapi import FastAPI

from hermes_cli.error_sanitize import scrub_detail

_SCHEMA = "orchestration-backlog-v1"
_DEFAULT_DIR = "/home/piet/orchestration/backlog"
_STATUSES = ("backlog", "todo", "doing", "review", "done")
_PRIORITIES = ("low", "medium", "high")
_log = logging.getLogger(__name__)


def _backlog_dir() -> Path:
    return Path(os.environ.get("ORCHESTRATION_BACKLOG_DIR", _DEFAULT_DIR))


def _ref() -> str:
    """Committed ref to read, or empty for the working tree (the default).

    This backlog is living planning scratch, so the working tree is the right
    source. ``ORCHESTRATION_BACKLOG_REF`` is an optional escape hatch (e.g. to
    pin a committed ref) but is intentionally empty by default.
    """
    return os.environ.get("ORCHESTRATION_BACKLOG_REF", "").strip()


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Return the leading frontmatter block as a flat str->str dict.

    Parses line-by-line, splitting each ``key: value`` on the FIRST colon — the
    same line-based approach as ``family_organizer_view._parse_frontmatter`` (no
    YAML lib): backlog frontmatter carries human-written single-line scalars that
    a strict YAML parse would choke on. A closing ``---`` is required; ``---``
    rules in the body are ignored.
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


def _parse_depends_on(value: Any) -> list[str]:
    """Parse an inline-YAML list ``[a, b]`` into a clean list of strings.

    Strips the surrounding brackets, splits on commas, and drops empty entries.
    Tolerates a missing/empty value (``[]`` or absent → ``[]``) and a bare
    scalar without brackets.
    """
    if value is None:
        return []
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [part.strip() for part in s.split(",") if part.strip()]


def _parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1")


def _detail_error(message: str) -> dict[str, str]:
    return {"error": message}


def _extract_excerpt(text: str, max_len: int = 140) -> str:
    """First readable body line, coarsely stripped of Markdown markers, ≤max_len chars."""
    body = _body_after_frontmatter(text)
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"[-*_=]{3,}", stripped):
            continue  # skip HR/rule lines
        cleaned = _MD_HEADING_RE.sub("", stripped)
        cleaned = _MD_LIST_RE.sub("", cleaned)
        cleaned = _MD_QUOTE_RE.sub("", cleaned)
        cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
        cleaned = _MD_INLINE_RE.sub("", cleaned)
        cleaned = cleaned.strip()
        if cleaned:
            return cleaned[:max_len]
    return ""


def _extract_links(text: str, max_links: int = 8) -> list[dict[str, str]]:
    """Extract a small list of Markdown links for the detail drawer."""
    links: list[dict[str, str]] = []
    for label, href in _MD_LINK_RE.findall(_body_after_frontmatter(text)):
        clean_label = label.strip()
        clean_href = href.strip()
        if not clean_label or not clean_href:
            continue
        links.append({"label": clean_label[:120], "href": clean_href[:500]})
        if len(links) >= max_links:
            break
    return links


def _extract_proof_lines(text: str, fm: dict[str, Any], max_lines: int = 8) -> list[str]:
    """Evidence-oriented lines from frontmatter/body, bounded for UI display."""
    proofs: list[str] = []
    for key in ("closed", "receipt", "proof", "lastProof", "last_proof"):
        value = str(fm.get(key) or "").strip()
        if value:
            proofs.append(f"{key}: {value}")

    body = _body_after_frontmatter(text)
    for line in body.split("\n"):
        cleaned = line.strip().strip("*")
        if not cleaned:
            continue
        low = cleaned.lower()
        if any(token in low for token in ("receipt", "verifiziert", "verified", "deployed", "live", "commit", "gate")):
            cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
            cleaned = _MD_INLINE_RE.sub("", cleaned).strip()
            if cleaned:
                proofs.append(cleaned[:220])
        if len(proofs) >= max_lines:
            break

    deduped: list[str] = []
    seen: set[str] = set()
    for proof in proofs:
        if proof in seen:
            continue
        seen.add(proof)
        deduped.append(proof)
        if len(deduped) >= max_lines:
            break
    return deduped


def _last_proof(text: str, fm: dict[str, Any]) -> str:
    for key in ("lastProof", "last_proof", "receipt", "proof", "closed"):
        value = str(fm.get(key) or "").strip()
        if value:
            return value
    proofs = _extract_proof_lines(text, fm, max_lines=1)
    return proofs[0] if proofs else ""


def _body_after_frontmatter(text: str) -> str:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:])
    return ""


def _validate_backlog_item_id(item_id: str) -> str | None:
    clean = str(item_id or "").strip()
    if not clean:
        return "empty backlog id"
    if "/" in clean or "\\" in clean or ".." in clean:
        return "invalid backlog id"
    return None


def _read_detail_sync(item_id: str) -> dict[str, Any]:
    invalid = _validate_backlog_item_id(item_id)
    if invalid:
        return _detail_error(invalid)

    clean = item_id.strip()
    base = _backlog_dir().resolve()
    target = (base / f"{clean}.md").resolve()
    if not target.is_relative_to(base):
        return _detail_error("invalid backlog item path")
    if not target.is_file():
        return _detail_error("backlog item not found")

    text = target.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    if not fm:
        return _detail_error("backlog item frontmatter not found")

    detail: dict[str, Any] = {key: str(value) for key, value in fm.items()}
    detail.update(
        {
            "id": clean,
            "title": str(fm.get("title") or clean),
            "status": str(fm.get("status") or ""),
            "priority": str(fm.get("priority") or ""),
            "dependsOn": _parse_depends_on(fm.get("dependsOn")),
            "planGate": _parse_bool(fm.get("planGate")),
            "gate": str(fm.get("gate") or ""),
            "root": str(fm.get("root") or ""),
            "owner": str(fm.get("owner") or fm.get("assignee") or ""),
            "source": str(fm.get("source") or ""),
            "closed": str(fm.get("closed") or ""),
            "lastProof": _last_proof(text, fm),
            "proofs": _extract_proof_lines(text, fm),
            "links": _extract_links(text),
            "created": str(fm.get("created") or ""),
            "body": _body_after_frontmatter(text),
        }
    )
    return detail


def _created_epoch(value: Any) -> int | None:
    """Parse an ISO ``YYYY-MM-DD`` value into UTC epoch seconds, else None."""
    if value is None:
        return None
    try:
        d = dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def _git(root: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)


def _read_sources_from_git(base: Path, ref: str) -> list[tuple[str, str]] | None:
    """(name, text) pairs from a committed ref — only used when REF is set.

    Returns ``None`` when ``base`` isn't inside a git repo or the ref is missing,
    so the caller falls back to the working tree.
    """
    try:
        top = _git(str(base), ["rev-parse", "--show-toplevel"])
        if top.returncode != 0:
            return None
        root = top.stdout.strip()
        rel = base.resolve().relative_to(Path(root).resolve()).as_posix()
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
    """(name, text) pairs from the working tree — the default for this board."""
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
    empty_health = {
        "source_count": 0,
        "counted_sum": 0,
        "unknown_statuses": [],
        "invalid_priority_count": 0,
        "missing_dep_count": 0,
    }

    ref = _ref()
    sources: list[tuple[str, str]] | None = None
    source_ref = "fs:working-tree"
    if ref:
        sources = _read_sources_from_git(base, ref)
        if sources is not None:
            source_ref = f"git:{ref}"
    if sources is None:
        if not base.is_dir():
            return {
                "schema": _SCHEMA,
                "checked_at": now,
                "items": [],
                "counts": counts,
                "contract_health": empty_health,
                "source": {"dir": str(base), "ref": "missing", "count": 0},
                "error": scrub_detail(f"backlog dir not found: {base}"),
            }
        sources = _read_sources_from_fs(base)
        source_ref = "fs:working-tree"

    items: list[dict[str, Any]] = []
    unknown_status_ids: dict[str, list[str]] = {}
    invalid_priority_count = 0
    for name, text in sources:
        # id from the filename stem (README.md and any other file without valid
        # frontmatter is dropped below) — the YAML `id` is not relied upon.
        stem = name[:-3] if name.endswith(".md") else name
        fm = _parse_frontmatter(text)
        if not fm:
            continue

        status = str(fm.get("status") or "").strip()
        priority = str(fm.get("priority") or "").strip()
        depends_on = _parse_depends_on(fm.get("dependsOn"))
        created = fm.get("created")
        items.append(
            {
                "id": stem,
                "title": str(fm.get("title") or stem),
                "status": status,
                "priority": priority,
                "dependsOn": depends_on,
                "planGate": _parse_bool(fm.get("planGate")),
                "created": str(created) if created is not None else "",
                "created_epoch": _created_epoch(created),
                "root": str(fm.get("root") or ""),
                "owner": str(fm.get("owner") or fm.get("assignee") or ""),
                "source": str(fm.get("source") or ""),
                "lastProof": _last_proof(text, fm),
                "excerpt": _extract_excerpt(text),
            }
        )
        if status in counts:
            counts[status] += 1
        else:
            unknown_status_ids.setdefault(status or "(missing)", []).append(stem)
        if priority not in _PRIORITIES:
            invalid_priority_count += 1

    item_ids = {item["id"] for item in items}
    missing_dep_count = sum(
        1
        for item in items
        for dep_id in item.get("dependsOn", [])
        if dep_id not in item_ids
    )
    contract_health = {
        "source_count": len(items),
        "counted_sum": sum(counts.values()),
        "unknown_statuses": [
            {"status": status, "count": len(ids), "ids": sorted(ids)[:20]}
            for status, ids in sorted(unknown_status_ids.items())
        ],
        "invalid_priority_count": invalid_priority_count,
        "missing_dep_count": missing_dep_count,
    }
    items.sort(key=lambda it: it["id"])
    return {
        "schema": _SCHEMA,
        "checked_at": now,
        "items": items,
        "counts": counts,
        "contract_health": contract_health,
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


def register_orchestration_backlog_routes(app: FastAPI) -> None:
    """Register the read-only orchestration backlog endpoint before the SPA catch-all."""

    @app.get("/api/orchestration/backlog")
    async def orchestration_backlog() -> dict[str, Any]:
        return await _get_backlog()

    @app.get("/api/orchestration/backlog/{id:path}")
    async def orchestration_backlog_detail(id: str) -> dict[str, Any]:
        try:
            return _read_detail_sync(id)
        except Exception as exc:
            _log.exception("orchestration backlog detail unavailable")
            message = scrub_detail(str(exc).strip()) or exc.__class__.__name__
            return _detail_error(message)

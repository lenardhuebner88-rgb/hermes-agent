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
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Coarse Markdown-stripping for excerpt extraction (mirrors orchestration_backlog_view).
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")
_MD_LIST_RE = re.compile(r"^\s*[-*+]\s+|^\s*\d+\.\s+")
_MD_QUOTE_RE = re.compile(r"^\s*>\s*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
_MD_INLINE_RE = re.compile(r"`[^`]*`|\*+|_+")
from typing import Any

from fastapi import FastAPI

from hermes_cli.error_sanitize import scrub_detail

_SCHEMA = "fo-backlog-v2"
_DEFAULT_DIR = "/home/piet/projects/family-organizer/backlog/items"
# in_progress/blocked items whose `updated` is older than this are flagged stale
# (mirrors the Stale-Claim-Sweep in family-organizer backlog/README.md).
_STALE_AFTER_S = 7 * 24 * 3600
# active items older than this (but not yet stale) are flagged "aging" for freshness.
_AGING_AFTER_DAYS = 3
_STATUSES = ("now", "next", "in_progress", "blocked", "later", "done")
_RISKS = ("low", "medium", "high")
_OWNERS = ("hermes", "claude", "codex", "piet", "unassigned")
_AREAS = ("kitchen", "lists", "shopping", "admin", "calendar", "hermes-api", "db", "process")
# Titles too short or generic to describe a task (mirrors the client quality lens).
_WEAK_TITLES = frozenset({"fix", "bug", "todo", "misc", "cleanup"})
# A body bullet line (mirrors the client `bodyScopeScore` heuristic).
_BODY_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:[-*]|\d+\.)\s+")
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
    """(repo path, text) pairs from the committed ref (default ``origin/main``).

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
                sources.append((path, blob.stdout))
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


def _source_name(source: str) -> str:
    return source.rsplit("/", 1)[-1]


def _source_path(source: str, base: Path, source_ref: str) -> str:
    if "/" in source:
        return source
    if source_ref == "fs:working-tree":
        return str((base / source).resolve())
    return f"backlog/items/{source}"


def _clean_markdown_line(line: str) -> str:
    cleaned = _MD_HEADING_RE.sub("", line.strip())
    cleaned = _MD_LIST_RE.sub("", cleaned)
    cleaned = _MD_QUOTE_RE.sub("", cleaned)
    cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
    cleaned = _MD_INLINE_RE.sub("", cleaned)
    return cleaned.strip()


def _extract_section_lines(text: str, heading_tokens: tuple[str, ...], max_lines: int = 8) -> list[str]:
    body = _body_after_frontmatter(text)
    lines: list[str] = []
    in_section = False
    for raw in body.split("\n"):
        stripped = raw.strip()
        heading = _MD_HEADING_RE.match(stripped)
        if heading:
            title = _MD_HEADING_RE.sub("", stripped).lower()
            if any(token in title for token in heading_tokens):
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue
        cleaned = _clean_markdown_line(raw)
        if cleaned:
            lines.append(cleaned[:220])
        if len(lines) >= max_lines:
            break
    return lines


def _extract_links(text: str, max_links: int = 8) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for label, href in _MD_LINK_RE.findall(_body_after_frontmatter(text)):
        clean_label = _clean_markdown_line(label)
        clean_href = href.strip()
        if not clean_label or not clean_href:
            continue
        links.append({"label": clean_label[:120], "href": clean_href[:500]})
        if len(links) >= max_links:
            break
    return links


def _section_present_in_body(body: str, heading_tokens: tuple[str, ...]) -> bool:
    """True if a matching ``## heading`` section holds at least one content line.

    Body-level twin of ``_extract_section_lines(..., max_lines=1)`` — operates on a
    body string already extracted once per item so the read loop doesn't re-derive it.
    """
    in_section = False
    for raw in body.split("\n"):
        stripped = raw.strip()
        heading = _MD_HEADING_RE.match(stripped)
        if heading:
            title = _MD_HEADING_RE.sub("", stripped).lower()
            if any(token in title for token in heading_tokens):
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue
        if _clean_markdown_line(raw):
            return True
    return False


def _body_scope_score(body: str) -> int:
    """Coarse size proxy: bullet count + body length in 1500-char units (mirrors client)."""
    bullets = len(_BODY_BULLET_RE.findall(body))
    return bullets + len(body) // 1500


def _item_facts(
    text: str,
    *,
    title: str,
    status: str,
    owner: str,
    stale: bool,
    epoch: int | None,
    now: int,
) -> dict[str, Any]:
    """Derive the deterministic per-item operator facts (v2).

    Server is the single source of truth for *facts* (freshness, age, the quality-issue
    taxonomy, readiness); ranking/reason codes stay a client view concern. Computed from
    the body extracted once here, reused by both the list and detail read paths.
    """
    body = _body_after_frontmatter(text)
    has_acceptance = _section_present_in_body(body, ("akzeptanz", "acceptance", "criteria"))
    has_next_action = _section_present_in_body(
        body, ("next action", "next step", "nächster", "naechster", "vorgehen")
    )
    missing_acceptance = not has_acceptance
    missing_next_action = status != "done" and not has_next_action

    age_days = None if epoch is None else max(0, (now - epoch) // 86400)
    if epoch is None:
        freshness = "no_proof"
    elif status == "done":
        freshness = "fresh"
    elif stale:
        freshness = "stale"
    elif age_days is not None and age_days > _AGING_AFTER_DAYS:
        freshness = "aging"
    else:
        freshness = "fresh"

    issues: list[dict[str, str]] = []
    trimmed = (title or "").strip()
    if len(trimmed) < 12 or trimmed.lower() in _WEAK_TITLES:
        issues.append({"code": "weak_title", "severity": "warn"})
    if missing_acceptance:
        issues.append({"code": "missing_acceptance", "severity": "risk"})
    # Vereinheitlichtes Claim-Modell: ein fehlender/unklarer Owner ist erst dann
    # ein Defekt, wenn aktiv gearbeitet wird (in_progress) — die ruhige Queue
    # (now/next/later/blocked) darf unassigned bleiben.
    if status == "in_progress" and (not owner or owner == "unassigned" or owner not in _OWNERS):
        issues.append({"code": "unclear_owner", "severity": "risk"})
    if stale:
        issues.append({"code": "stale_update", "severity": "risk"})
    if _body_scope_score(body) >= 10:
        issues.append({"code": "large_scope", "severity": "warn"})
    if missing_next_action:
        issues.append({"code": "missing_next_action", "severity": "risk"})

    if status not in _STATUSES:
        readiness = "drift"
    elif status == "blocked":
        readiness = "blocked"
    elif status == "done":
        readiness = "ready"
    elif any(issue["severity"] == "risk" for issue in issues):
        readiness = "needs_grooming"
    else:
        readiness = "ready"

    return {
        "age_days": age_days,
        "freshness": freshness,
        "quality_issues": issues,
        "readiness": readiness,
        "missing_acceptance": missing_acceptance,
        "missing_next_action": missing_next_action,
    }


def _detail_sections(text: str) -> dict[str, Any]:
    decision = _extract_section_lines(text, ("decision", "why now", "warum jetzt"))
    acceptance = _extract_section_lines(text, ("akzeptanz", "acceptance", "criteria"), max_lines=12)
    proofs = _extract_section_lines(text, ("current evidence", "last proof", "proof", "beleg", "evidence", "ergebnis"))
    blockers = _extract_section_lines(text, ("blocker", "blockers", "blocked", "blockiert"))
    next_actions = _extract_section_lines(text, ("next action", "next step", "nächster", "naechster", "vorgehen"), max_lines=3)
    return {
        "decision": decision,
        "acceptance_criteria": acceptance,
        "proofs": proofs,
        "blockers": blockers,
        "next_action": next_actions[0] if next_actions else "",
        "links": _extract_links(text),
    }


def _read_items_sync(now: int) -> dict[str, Any]:
    base = _backlog_dir().resolve()
    counts = {s: 0 for s in _STATUSES}
    empty_health = {
        "source_count": 0,
        "counted_sum": 0,
        "unknown_statuses": [],
        "invalid_risk_count": 0,
        "invalid_owner_count": 0,
        "unowned_count": 0,
        "stale_count": 0,
        "missing_acceptance_count": 0,
        "missing_next_action_count": 0,
        "invalid_area_count": 0,
    }

    sources = _read_sources_from_git(base)
    source_ref = f"git:{_ref()}"
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
    invalid_risk_count = 0
    invalid_owner_count = 0
    unowned_count = 0
    stale_count = 0
    missing_acceptance_count = 0
    missing_next_action_count = 0
    invalid_area_count = 0
    for source, text in sources:
        name = _source_name(source)
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
        owner = str(fm.get("owner") or "unassigned")
        risk = str(fm.get("risk") or "")
        area = str(fm.get("area") or "")
        title = str(fm.get("title") or stem)
        facts = _item_facts(
            text, title=title, status=status, owner=owner, stale=bool(stale), epoch=epoch, now=now
        )
        missing_acceptance = facts["missing_acceptance"]
        missing_next_action = facts["missing_next_action"]
        items.append(
            {
                # Canonical 4-digit id from the filename (the family-organizer
                # validator enforces id == filename); the YAML-parsed `id` is not
                # trustworthy — PyYAML coerces "0001" to the int 1 (YAML 1.1 octal).
                "id": stem[:4],
                "title": title,
                "status": status,
                "owner": owner,
                "risk": risk,
                "area": area,
                "updated": str(updated) if updated is not None else "",
                "lane": str(fm.get("lane")) if fm.get("lane") is not None else None,
                "result": str(fm.get("result")) if fm.get("result") is not None else None,
                "stale": bool(stale),
                "excerpt": _extract_excerpt(text),
                "source_path": _source_path(source, base, source_ref),
                "missing_acceptance": missing_acceptance,
                "missing_next_action": missing_next_action,
                "age_days": facts["age_days"],
                "freshness": facts["freshness"],
                "quality_issues": facts["quality_issues"],
                "readiness": facts["readiness"],
            }
        )
        if status in counts:
            counts[status] += 1
        else:
            unknown_status_ids.setdefault(status or "(missing)", []).append(stem[:4])
        if risk not in _RISKS:
            invalid_risk_count += 1
        if owner not in _OWNERS:
            invalid_owner_count += 1
        # Owner-Gap nur fuer aktiv bearbeitete Items (in_progress) — die ruhige
        # Queue darf unowned sein (vereinheitlichtes Claim-Modell).
        if status == "in_progress" and owner == "unassigned":
            unowned_count += 1
        if area not in _AREAS:
            invalid_area_count += 1
        if stale:
            stale_count += 1
        if missing_acceptance:
            missing_acceptance_count += 1
        if missing_next_action:
            missing_next_action_count += 1

    items.sort(key=lambda it: it["id"])
    contract_health = {
        "source_count": len(items),
        "counted_sum": sum(counts.values()),
        "unknown_statuses": [
            {"status": status, "count": len(ids), "ids": sorted(ids)[:20]}
            for status, ids in sorted(unknown_status_ids.items())
        ],
        "invalid_risk_count": invalid_risk_count,
        "invalid_owner_count": invalid_owner_count,
        "unowned_count": unowned_count,
        "stale_count": stale_count,
        "missing_acceptance_count": missing_acceptance_count,
        "missing_next_action_count": missing_next_action_count,
        "invalid_area_count": invalid_area_count,
    }
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


def _extract_excerpt(text: str, max_len: int = 140) -> str:
    """First readable body line, coarsely stripped of Markdown markers, ≤max_len chars."""
    body = _body_after_frontmatter(text)
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"[-*_=]{3,}", stripped):
            continue
        cleaned = _MD_HEADING_RE.sub("", stripped)
        cleaned = _MD_LIST_RE.sub("", cleaned)
        cleaned = _MD_QUOTE_RE.sub("", cleaned)
        cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
        cleaned = _MD_INLINE_RE.sub("", cleaned)
        cleaned = cleaned.strip()
        if cleaned:
            return cleaned[:max_len]
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

        source_ref = f"git:{_ref()}"
        for source, text in sources:
            name = _source_name(source)
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
            title = str(fm.get("title") or stem)
            owner = str(fm.get("owner") or "unassigned")
            facts = _item_facts(
                text, title=title, status=status, owner=owner, stale=bool(stale), epoch=epoch, now=now
            )
            detail = {
                "id": stem[:4],
                "title": title,
                "status": status,
                "owner": owner,
                "risk": str(fm.get("risk") or ""),
                "area": str(fm.get("area") or ""),
                "updated": str(updated) if updated is not None else "",
                "lane": str(fm.get("lane")) if fm.get("lane") is not None else None,
                "result": str(fm.get("result")) if fm.get("result") is not None else None,
                "stale": bool(stale),
                "age_days": facts["age_days"],
                "freshness": facts["freshness"],
                "quality_issues": facts["quality_issues"],
                "readiness": facts["readiness"],
                "missing_acceptance": facts["missing_acceptance"],
                "missing_next_action": facts["missing_next_action"],
                "body": _body_after_frontmatter(text),
                "source_path": _source_path(source, base, source_ref),
                "source_ref": source_ref,
            }
            detail.update(_detail_sections(text))
            return detail
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

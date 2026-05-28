#!/usr/bin/env python3
"""Build and optionally send Hermes' daily research Discord post.

The script is intentionally deterministic for V1: it fetches configured RSS/Atom
sources, deduplicates entries, ranks system-relevant AI/agent signals, formats a
German Discord post, and can either print it for Hermes cron delivery or send it
via the existing ``send_message`` gateway tool.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

DEFAULT_CHANNEL_ID = "1491150772224659649"
DEFAULT_SCHEDULE = "0 7 * * *"
DEFAULT_MAX_ITEMS = 5
DEFAULT_LOOKBACK_HOURS = 72

LOGGER = logging.getLogger("daily_research_post")


@dataclass(frozen=True)
class SourceConfig:
    name: str
    url: str
    priority: str = "P2"
    enabled: bool = True
    timeout_seconds: int = 15


@dataclass
class ResearchItem:
    title: str
    source: str
    url: str
    priority: str = "P2"
    published: datetime | None = None
    summary: str = ""
    system_impact: str = ""
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)


@dataclass
class JobConfig:
    channel_id: str = DEFAULT_CHANNEL_ID
    schedule: str = DEFAULT_SCHEDULE
    max_items: int = DEFAULT_MAX_ITEMS
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    sources: list[SourceConfig] = field(default_factory=list)


DEFAULT_SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig("OpenAI News", "https://openai.com/news/rss.xml", "P1"),
    SourceConfig("Google DeepMind Blog", "https://deepmind.google/blog/rss.xml", "P1"),
    SourceConfig("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "P1"),
    SourceConfig("GitHub Changelog", "https://github.blog/changelog/feed/", "P2"),
    SourceConfig(
        "arXiv cs.AI/cs.CL recent",
        "https://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=20",
        "P2",
    ),
    SourceConfig("Simon Willison", "https://simonwillison.net/atom/everything/", "P2"),
    SourceConfig("The Decoder", "https://the-decoder.com/feed/", "P3"),
)

_KEYWORD_WEIGHTS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("agent", "agents", "agentic", "workflow", "orchestration", "tool-calling"), 22, "agent workflow"),
    (("mcp", "tool", "tools", "function calling", "tool use"), 18, "tool/mcp"),
    (("eval", "evaluation", "benchmark", "reliability", "regression"), 18, "evaluation"),
    (("context", "memory", "retrieval", "rag", "knowledge"), 14, "context/memory"),
    (("model routing", "routing", "inference", "latency", "cost", "local model"), 14, "model ops"),
    (("security", "safety", "hardening", "guardrail", "sandbox"), 14, "hardening"),
    (("discord", "gateway", "cron", "scheduler", "automation"), 10, "operations"),
)

_PRIORITY_SCORE = {"P1": 70, "P2": 45, "P3": 25}
_MIN_SIGNAL_SCORE = 55


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_text(text: str | None) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_datetime(text: str | None) -> datetime | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return _as_utc(parsedate_to_datetime(raw))
    except (TypeError, ValueError, IndexError):
        pass
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return _as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _xml_text(parent: ET.Element, names: Sequence[str]) -> str:
    for name in names:
        found = parent.find(name)
        if found is not None and found.text:
            return _clean_text(found.text)
    return ""


def _xml_link(parent: ET.Element) -> str:
    for tag in ("link", "{http://www.w3.org/2005/Atom}link"):
        found = parent.find(tag)
        if found is None:
            continue
        href = (found.attrib.get("href") or "").strip()
        if href:
            return href
        if found.text:
            return _clean_text(found.text)
    return ""


def _parse_feed_xml(xml_text: str, source: SourceConfig) -> list[ResearchItem]:
    root = ET.fromstring(xml_text)
    entry_nodes = list(root.findall(".//item")) + list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    items: list[ResearchItem] = []
    for node in entry_nodes:
        title = _xml_text(node, ("title", "{http://www.w3.org/2005/Atom}title"))
        if not title:
            continue
        url = _xml_link(node)
        summary = _xml_text(
            node,
            (
                "description",
                "summary",
                "content",
                "{http://www.w3.org/2005/Atom}summary",
                "{http://www.w3.org/2005/Atom}content",
            ),
        )
        published = _parse_datetime(
            _xml_text(
                node,
                (
                    "pubDate",
                    "published",
                    "updated",
                    "{http://www.w3.org/2005/Atom}published",
                    "{http://www.w3.org/2005/Atom}updated",
                ),
            )
        )
        items.append(
            ResearchItem(
                title=title,
                source=source.name,
                url=url,
                priority=source.priority,
                published=published,
                summary=summary,
            )
        )
    return items


def fetch_source(source: SourceConfig) -> list[ResearchItem]:
    """Fetch and parse one RSS/Atom source.

    Failures are logged and converted to an empty list so one broken feed does
    not prevent the morning post from being built.
    """
    if not source.enabled:
        return []
    request = urllib.request.Request(source.url, headers={"User-Agent": "Hermes-Daily-Research/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=source.timeout_seconds) as response:
            payload = response.read(2_000_000).decode("utf-8", errors="replace")
        parsed = _parse_feed_xml(payload, source)
        LOGGER.info("fetched source=%s items=%d", source.name, len(parsed))
        return parsed
    except (urllib.error.URLError, TimeoutError, ET.ParseError, UnicodeDecodeError, OSError) as exc:
        LOGGER.warning("source fetch failed source=%s url=%s error=%s", source.name, source.url, exc)
        return []


def fetch_sources(sources: Iterable[SourceConfig], *, pause_seconds: float = 0.2) -> list[ResearchItem]:
    items: list[ResearchItem] = []
    for index, source in enumerate(sources):
        if index and pause_seconds > 0:
            time.sleep(pause_seconds)
        items.extend(fetch_source(source))
    return items


def _normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _normalize_title(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _dedupe_key(item: ResearchItem) -> str:
    normalized_url = _normalize_url(item.url)
    if normalized_url:
        return f"url:{normalized_url}"
    return f"title:{_normalize_title(item.title)}"


def _impact_for(item: ResearchItem) -> str:
    text = f"{item.title} {item.summary}".lower()
    if any(word in text for word in ("mcp", "tool", "function calling", "tool use")):
        return "Schärft unsere Tool-/MCP-Grenzen: relevant für sicherere Agentenläufe, bessere Allowlist-Entscheidungen und Review-Gates."
    if any(word in text for word in ("agent", "workflow", "orchestration", "agentic")):
        return "Hilft beim Design robusterer Agenten-Workflows: bessere Planung, Übergaben, Evaluation und Debuggbarkeit im Hermes/OpenClaw-System."
    if any(word in text for word in ("eval", "benchmark", "evaluation", "reliability")):
        return "Gibt uns Messpunkte für Qualität: nützlich für Regressionstests, Modellwahl und belastbare Verbesserungsnachweise."
    if any(word in text for word in ("context", "memory", "retrieval", "rag")):
        return "Relevant für Kontext- und Memory-Schichten: kann helfen, Recall präziser und Startup-Kontext schlanker zu halten."
    if any(word in text for word in ("routing", "inference", "latency", "cost", "local model")):
        return "Nützlich für Modellrouting und Betriebskosten: kann Latenz, Fallbacks oder lokale Inferenzpfade verbessern."
    if any(word in text for word in ("security", "safety", "hardening", "guardrail", "sandbox")):
        return "Erhöht die Betriebsrobustheit: relevant für sichere Automatisierung, Sandbox-Grenzen und Incident-Prävention."
    return "Allgemeines Signal für die Roadmap: im Backlog prüfen, ob es bestehende Agenten-, Gateway- oder Ops-Flows vereinfacht."


def _score_item(item: ResearchItem, *, now: datetime) -> tuple[float, list[str]]:
    priority = (item.priority or "P2").upper()
    score = float(_PRIORITY_SCORE.get(priority, _PRIORITY_SCORE["P2"]))
    reasons = [priority]
    text = f"{item.title} {item.summary} {item.source}".lower()
    for keywords, weight, label in _KEYWORD_WEIGHTS:
        if any(keyword in text for keyword in keywords):
            score += weight
            reasons.append(label)
    published = _as_utc(item.published)
    if published:
        age_hours = max(0.0, (now - published).total_seconds() / 3600)
        if age_hours <= 36:
            score += 15
            reasons.append("fresh")
        elif age_hours <= 72:
            score += 8
            reasons.append("recent")
        else:
            score -= min(25, (age_hours - 72) / 24 * 5)
            reasons.append("older")
    else:
        score -= 3
        reasons.append("undated")
    return score, reasons


def select_research_items(
    candidates: Iterable[ResearchItem],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    now: datetime | None = None,
) -> list[ResearchItem]:
    """Deduplicate, rank, and return the top 3-5 daily research signals."""
    now = _as_utc(now) or _utc_now()
    best_by_key: dict[str, ResearchItem] = {}
    for candidate in candidates:
        if not candidate.title.strip():
            continue
        score, reasons = _score_item(candidate, now=now)
        candidate.score = score
        candidate.score_reasons = reasons
        candidate.system_impact = candidate.system_impact or _impact_for(candidate)
        key = _dedupe_key(candidate)
        existing = best_by_key.get(key)
        if existing is None or candidate.score > existing.score:
            best_by_key[key] = candidate

    ranked = sorted(
        best_by_key.values(),
        key=lambda item: (item.score, _as_utc(item.published) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    strong = [item for item in ranked if item.score >= _MIN_SIGNAL_SCORE]
    return strong[: max(0, max_items)]


def _format_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _shorten(text: str, limit: int) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def format_daily_post(
    items: Sequence[ResearchItem],
    *,
    generated_at: datetime | None = None,
    channel_id: str = DEFAULT_CHANNEL_ID,
) -> str:
    generated_at = _as_utc(generated_at) or _utc_now()
    header = [
        "🧭 Daily Research Radar",
        f"Stand: {_format_date(generated_at)} · Ziel-Channel: {channel_id}",
        "Fokus: Agenten, Tooling, Modellrouting, Evaluation, Memory/Kontext und Betriebsrobustheit.",
    ]
    if not items:
        return "\n".join(
            header
            + [
                "",
                "Heute kein belastbares Signal aus den priorisierten Quellen.",
                "Was bringt uns das im System? Keine neue Aktion; bestehende Roadmap/Incidents weiter abarbeiten und morgen erneut prüfen.",
            ]
        )

    lines = header + ["", "Top-Signale:"]
    for idx, item in enumerate(items, start=1):
        summary = _shorten(item.summary, 220)
        published = _as_utc(item.published)
        meta_bits = [f"Quelle: {item.source}", item.priority.upper()]
        if published:
            meta_bits.append(_format_date(published))
        if item.score:
            meta_bits.append(f"Score: {item.score:.0f}")
        lines.extend(
            [
                "",
                f"{idx}. **{item.title}**",
                f"   {' · '.join(meta_bits)}",
                f"   Link: {item.url or 'n/a'}",
            ]
        )
        if summary:
            lines.append(f"   Kurz: {summary}")
        lines.append(f"   Was bringt uns das im System? {item.system_impact or _impact_for(item)}")
    lines.extend(
        [
            "",
            "Auswahlregel: priorisierte Quellen, Dedupe nach URL/Titel, Boost für Agenten-/Tool-/Eval-/Ops-Relevanz, Frischebonus.",
        ]
    )
    return "\n".join(lines)


def _source_from_dict(raw: dict[str, Any]) -> SourceConfig:
    return SourceConfig(
        name=str(raw.get("name") or "").strip(),
        url=str(raw.get("url") or "").strip(),
        priority=str(raw.get("priority") or "P2").strip().upper(),
        enabled=bool(raw.get("enabled", True)),
        timeout_seconds=int(raw.get("timeout_seconds", 15)),
    )


def load_job_config(path: str | Path | None = None) -> JobConfig:
    """Load JSON config plus environment overrides.

    Supported JSON keys: ``channel_id``, ``schedule``, ``max_items``,
    ``lookback_hours``, and ``sources`` (list of objects with name/url/priority).
    Environment overrides: ``HERMES_DAILY_RESEARCH_CHANNEL_ID``,
    ``HERMES_DAILY_RESEARCH_SCHEDULE``, ``HERMES_DAILY_RESEARCH_MAX_ITEMS``,
    ``HERMES_DAILY_RESEARCH_LOOKBACK_HOURS``.
    """
    raw: dict[str, Any] = {}
    config_path = path or os.getenv("HERMES_DAILY_RESEARCH_CONFIG", "").strip()
    if config_path:
        with Path(config_path).expanduser().open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

    sources_raw = raw.get("sources") if isinstance(raw.get("sources"), list) else None
    sources = [_source_from_dict(entry) for entry in sources_raw] if sources_raw else list(DEFAULT_SOURCES)
    sources = [source for source in sources if source.name and source.url]

    return JobConfig(
        channel_id=os.getenv("HERMES_DAILY_RESEARCH_CHANNEL_ID", str(raw.get("channel_id") or DEFAULT_CHANNEL_ID)).strip(),
        schedule=os.getenv("HERMES_DAILY_RESEARCH_SCHEDULE", str(raw.get("schedule") or DEFAULT_SCHEDULE)).strip(),
        max_items=int(os.getenv("HERMES_DAILY_RESEARCH_MAX_ITEMS", raw.get("max_items") or DEFAULT_MAX_ITEMS)),
        lookback_hours=int(
            os.getenv("HERMES_DAILY_RESEARCH_LOOKBACK_HOURS", raw.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS)
        ),
        sources=sources,
    )


def build_daily_post(config: JobConfig, *, now: datetime | None = None) -> tuple[str, list[ResearchItem]]:
    now = _as_utc(now) or _utc_now()
    cutoff = now - timedelta(hours=max(1, config.lookback_hours))
    fetched = fetch_sources(config.sources)
    recent: list[ResearchItem] = []
    for item in fetched:
        published = _as_utc(item.published)
        if published is None or published >= cutoff:
            recent.append(item)
    selected = select_research_items(recent, max_items=config.max_items, now=now)
    return format_daily_post(selected, generated_at=now, channel_id=config.channel_id), selected


def post_to_discord(
    message: str,
    *,
    channel_id: str = DEFAULT_CHANNEL_ID,
    sender: Callable[[dict[str, str]], str] | None = None,
) -> dict[str, Any]:
    """Send via Hermes' existing ``send_message`` tool contract."""
    if sender is None:
        from tools.send_message_tool import send_message_tool

        sender = send_message_tool
    payload = {"action": "send", "target": f"discord:{channel_id}", "message": message}
    raw_result = sender(payload)
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else dict(raw_result)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Discord send returned invalid JSON: {raw_result!r}") from exc
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Hermes' Daily Research Radar Discord post.")
    parser.add_argument("--config", help="Path to JSON config. Defaults to HERMES_DAILY_RESEARCH_CONFIG or built-ins.")
    parser.add_argument("--channel-id", help=f"Discord channel id (default: {DEFAULT_CHANNEL_ID}).")
    parser.add_argument("--max-items", type=int, help=f"Maximum items to include (default: {DEFAULT_MAX_ITEMS}).")
    parser.add_argument("--lookback-hours", type=int, help=f"Fetch window in hours (default: {DEFAULT_LOOKBACK_HOURS}).")
    parser.add_argument("--send", action="store_true", help="Send via send_message_tool to discord:<channel-id>.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable metadata instead of the Discord message.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    try:
        config = load_job_config(args.config)
        if args.channel_id:
            config.channel_id = args.channel_id.strip()
        if args.max_items is not None:
            config.max_items = args.max_items
        if args.lookback_hours is not None:
            config.lookback_hours = args.lookback_hours

        message, selected = build_daily_post(config)
        send_result = None
        if args.send:
            send_result = post_to_discord(message, channel_id=config.channel_id)
            LOGGER.info("sent daily research post channel_id=%s selected=%d", config.channel_id, len(selected))

        if args.json:
            print(
                json.dumps(
                    {
                        "channel_id": config.channel_id,
                        "schedule": config.schedule,
                        "selected_count": len(selected),
                        "sent": bool(args.send),
                        "send_result": send_result,
                        "message": message,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(message)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level job runner should log cleanly
        LOGGER.exception("daily research post failed: %s", exc)
        print(f"Daily research post failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

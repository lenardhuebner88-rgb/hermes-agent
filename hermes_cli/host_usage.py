"""Read-only host-wide AI usage rollup for the dashboard start view.

The Kanban database only sees Hermes workers.  This module additionally reads
the native usage logs written by Claude Code, Codex, Kimi Code, Qwen and Grok
terminals plus every active Hermes profile ``state.db``.  No source is required:
one unavailable or malformed source degrades to ``errors[]`` while the others
remain useful.

Token semantics count active input plus output; cached input is excluded rather
than counted a second time.  Hermes stores cumulative usage per session/model, so
those totals are assigned to the local day of the session's last activity; the
terminal sources expose per-turn timestamps and are assigned directly.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from hermes_cli.config import get_hermes_home


_LOCAL_TZ = ZoneInfo("Europe/Berlin")
_PROVIDER_ORDER = ("claude", "codex", "kimi", "grok", "qwen", "api")
_PROVIDER_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "kimi": "Kimi",
    "grok": "Grok",
    "qwen": "Qwen",
    "api": "API / Router",
}
_CACHE_TTL_SECONDS = 45
_CACHE_LOCK = threading.Lock()
_CACHE_CONDITION = threading.Condition(_CACHE_LOCK)
_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_CACHE_IN_FLIGHT: set[int] = set()


@dataclass(frozen=True)
class HostUsagePaths:
    hermes_home: Path
    claude_root: Path
    codex_root: Path
    kimi_root: Path
    qwen_usage_root: Path
    grok_log: Path

    @classmethod
    def defaults(cls) -> "HostUsagePaths":
        home = Path.home()
        return cls(
            hermes_home=get_hermes_home(),
            claude_root=home / ".claude" / "projects",
            codex_root=home / ".codex" / "sessions",
            kimi_root=home / ".kimi-code" / "sessions",
            qwen_usage_root=home / ".qwen" / "usage",
            grok_log=home / ".grok" / "logs" / "unified.jsonl",
        )


@dataclass(frozen=True)
class UsageEvent:
    provider: str
    session_id: str
    source: str
    timestamp: float
    tokens: int


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        resolved = float(value)
        return resolved / 1000 if resolved > 10_000_000_000 else resolved
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _json_lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                yield item


def _recent_files(root: Path, pattern: str, cutoff: float) -> Iterable[Path]:
    if not root.exists():
        return ()
    files: list[Path] = []
    for path in root.rglob(pattern):
        try:
            if path.is_file() and path.stat().st_mtime >= cutoff:
                files.append(path)
        except OSError:
            continue
    return files


def _provider_from_model(model: Any, *, fallback: str) -> str:
    name = str(model or "").lower()
    if "qwen" in name:
        return "qwen"
    if "grok" in name:
        return "grok"
    if "kimi" in name or "moonshot" in name:
        return "kimi"
    if "claude" in name:
        return "claude"
    if name.startswith("gpt-") or "codex" in name:
        return "codex"
    return fallback


def _provider_from_billing(provider: Any, model: Any) -> str:
    billing = str(provider or "").lower()
    if "openai-codex" in billing or billing in {"openai", "chatgpt"}:
        return "codex"
    if "kimi" in billing or "moonshot" in billing:
        return "kimi"
    if "xai" in billing or "grok" in billing:
        return "grok"
    if "anthropic" in billing or "claude" in billing:
        return "claude"
    if "qwen" in billing:
        return "qwen"
    # Router/API accounts are intentionally kept separate from the model name:
    # the matrix answers where provider capacity was consumed, not which model
    # happened to be routed through that account.
    return "api"


def _hermes_events(paths: HostUsagePaths, cutoff: float) -> list[UsageEvent]:
    db_paths = [paths.hermes_home / "state.db"]
    db_paths.extend(sorted((paths.hermes_home / "profiles").glob("*/state.db")))
    events: list[UsageEvent] = []
    for path in db_paths:
        if not path.exists():
            continue
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_model_usage'"
            ).fetchone()
            if exists is None:
                continue
            rows = conn.execute(
                "SELECT session_id, model, billing_provider, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, last_seen "
                "FROM session_model_usage WHERE last_seen >= ?",
                (cutoff,),
            ).fetchall()
            db_scope = path.parent.name if path.parent.name == "profiles" else path.parent.name
            if path.parent.parent == paths.hermes_home / "profiles":
                db_scope = path.parent.name
            for row in rows:
                at = float(row["last_seen"] or 0)
                tokens = _number(row["input_tokens"]) + _number(row["output_tokens"])
                if at < cutoff or tokens <= 0:
                    continue
                events.append(
                    UsageEvent(
                        provider=_provider_from_billing(row["billing_provider"], row["model"]),
                        session_id=f"{db_scope}:{row['session_id']}",
                        source="hermes",
                        timestamp=at,
                        tokens=tokens,
                    )
                )
        except (OSError, sqlite3.DatabaseError):
            continue
        finally:
            if conn is not None:
                conn.close()
    return events


def _claude_stats_events(paths: HostUsagePaths, cutoff: float) -> tuple[list[UsageEvent], float]:
    """Use Claude's own daily cache, returning the raw-log continuation point."""
    stats_path = paths.claude_root.parent / "stats-cache.json"
    if not stats_path.exists():
        return [], cutoff
    try:
        payload = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return [], cutoff
    if not isinstance(payload, dict):
        return [], cutoff
    daily_tokens = payload.get("dailyModelTokens")
    daily_activity = payload.get("dailyActivity")
    if not isinstance(daily_tokens, list) or not isinstance(daily_activity, list):
        return [], cutoff
    sessions_by_date = {
        str(row.get("date")): _number(row.get("sessionCount"))
        for row in daily_activity
        if isinstance(row, dict) and row.get("date")
    }
    events: list[UsageEvent] = []
    for row in daily_tokens:
        if not isinstance(row, dict) or not isinstance(row.get("tokensByModel"), dict):
            continue
        date = str(row.get("date") or "")
        try:
            at = datetime.fromisoformat(f"{date}T12:00:00").replace(tzinfo=_LOCAL_TZ).timestamp()
        except ValueError:
            continue
        if at < cutoff:
            continue
        by_provider: dict[str, int] = defaultdict(int)
        for model, tokens in row["tokensByModel"].items():
            by_provider[_provider_from_model(model, fallback="claude")] += _number(tokens)
        by_provider = {key: value for key, value in by_provider.items() if value > 0}
        if not by_provider:
            continue
        session_count = max(len(by_provider), sessions_by_date.get(date, 0))
        allocation = {key: 1 for key in by_provider}
        remaining = session_count - len(allocation)
        total = sum(by_provider.values())
        if remaining > 0 and total > 0:
            ordered = sorted(by_provider, key=lambda key: (-by_provider[key], key))
            assigned = 0
            for key in ordered:
                share = int(remaining * by_provider[key] / total)
                allocation[key] += share
                assigned += share
            for index in range(remaining - assigned):
                allocation[ordered[index % len(ordered)]] += 1
        for provider, count in allocation.items():
            for index in range(count):
                events.append(
                    UsageEvent(
                        provider,
                        f"claude-cache:{date}:{provider}:{index}",
                        "terminal",
                        at,
                        by_provider[provider] if index == 0 else 0,
                    )
                )
    last_date = payload.get("lastComputedDate")
    try:
        continuation = (
            datetime.fromisoformat(str(last_date)).replace(tzinfo=_LOCAL_TZ)
            + timedelta(days=1)
        ).timestamp()
    except ValueError:
        continuation = cutoff
    return events, max(cutoff, continuation)


def _claude_events(paths: HostUsagePaths, cutoff: float) -> list[UsageEvent]:
    cached_events, raw_cutoff = _claude_stats_events(paths, cutoff)
    events: list[UsageEvent] = []
    seen: set[str] = set()
    for path in _recent_files(paths.claude_root, "*.jsonl", raw_cutoff):
        try:
            for item in _json_lines(path):
                if item.get("type") != "assistant":
                    continue
                at = _timestamp(item.get("timestamp"))
                message = item.get("message")
                if at is None or at < raw_cutoff or not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                event_id = str(item.get("uuid") or "")
                if event_id and event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)
                tokens = _number(usage.get("input_tokens")) + _number(usage.get("output_tokens"))
                if tokens <= 0:
                    continue
                session_id = str(item.get("sessionId") or path.stem)
                events.append(
                    UsageEvent(
                        provider=_provider_from_model(message.get("model"), fallback="claude"),
                        session_id=f"claude:{session_id}",
                        source="terminal",
                        timestamp=at,
                        tokens=tokens,
                    )
                )
        except OSError:
            continue
    return [*cached_events, *events]


def _codex_events(paths: HostUsagePaths, cutoff: float) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    for path in _recent_files(paths.codex_root, "*.jsonl", cutoff):
        session_id = path.stem
        pending: list[tuple[float, int]] = []
        try:
            for item in _json_lines(path):
                if item.get("type") == "session_meta":
                    payload = item.get("payload")
                    if isinstance(payload, dict) and payload.get("id"):
                        session_id = str(payload["id"])
                    continue
                payload = item.get("payload")
                if item.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                at = _timestamp(item.get("timestamp"))
                info = payload.get("info")
                last = info.get("last_token_usage") if isinstance(info, dict) else None
                if at is None or at < cutoff or not isinstance(last, dict):
                    continue
                input_tokens = _number(last.get("input_tokens"))
                cached_tokens = _number(last.get("cached_input_tokens"))
                output_tokens = _number(last.get("output_tokens"))
                tokens = max(0, input_tokens - cached_tokens) + output_tokens
                if tokens <= 0:
                    tokens = _number(last.get("total_tokens"))
                if tokens > 0:
                    pending.append((at, tokens))
        except OSError:
            continue
        events.extend(
            UsageEvent("codex", f"codex:{session_id}", "terminal", at, tokens)
            for at, tokens in pending
        )
    return events


def _kimi_events(paths: HostUsagePaths, cutoff: float) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    for path in _recent_files(paths.kimi_root, "wire.jsonl", cutoff):
        try:
            relative = path.relative_to(paths.kimi_root)
            session_id = relative.parts[0] if relative.parts else path.parent.name
            for item in _json_lines(path):
                if item.get("type") != "usage.record":
                    continue
                at = _timestamp(item.get("time"))
                usage = item.get("usage")
                if at is None or at < cutoff or not isinstance(usage, dict):
                    continue
                tokens = _number(usage.get("inputOther")) + _number(usage.get("output"))
                if tokens > 0:
                    events.append(UsageEvent("kimi", f"kimi:{session_id}", "terminal", at, tokens))
        except (OSError, ValueError):
            continue
    return events


def _qwen_events(paths: HostUsagePaths, cutoff: float) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    for path in _recent_files(paths.qwen_usage_root, "*.jsonl", cutoff):
        try:
            for item in _json_lines(path):
                at = _timestamp(item.get("timestamp"))
                if at is None or at < cutoff:
                    continue
                input_tokens = _number(item.get("inputTokens"))
                cached_tokens = _number(item.get("cachedTokens"))
                output_tokens = _number(item.get("outputTokens"))
                tokens = max(0, input_tokens - cached_tokens) + output_tokens
                if tokens <= 0:
                    tokens = _number(item.get("totalTokens"))
                if tokens <= 0:
                    continue
                session_id = str(item.get("sessionId") or path.stem)
                events.append(UsageEvent("qwen", f"qwen:{session_id}", "terminal", at, tokens))
        except OSError:
            continue
    return events


def _grok_events(paths: HostUsagePaths, cutoff: float) -> list[UsageEvent]:
    if not paths.grok_log.exists():
        return []
    events: list[UsageEvent] = []
    try:
        for item in _json_lines(paths.grok_log):
            if item.get("msg") != "shell.turn.inference_done":
                continue
            at = _timestamp(item.get("ts"))
            context = item.get("ctx")
            if at is None or at < cutoff or not isinstance(context, dict):
                continue
            # Cached prompt and reasoning are subsets of the prompt/completion
            # counters.  Exclude cached input and never add reasoning again.
            tokens = max(
                0,
                _number(context.get("prompt_tokens"))
                - _number(context.get("cached_prompt_tokens")),
            ) + _number(context.get("completion_tokens"))
            if tokens > 0:
                session_id = str(item.get("sid") or "unknown")
                events.append(UsageEvent("grok", f"grok:{session_id}", "terminal", at, tokens))
    except OSError:
        return []
    return events


def _active_tmux_panes() -> int:
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_name}\t#{pane_dead}",
            ],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.strip() and not line.endswith("\t1"))


def _date_axis(days: int, now: float) -> tuple[list[str], float]:
    current = datetime.fromtimestamp(now, _LOCAL_TZ)
    today = current.date()
    first = today - timedelta(days=days - 1)
    axis = [(first + timedelta(days=index)).isoformat() for index in range(days)]
    cutoff = datetime.combine(first, datetime.min.time(), _LOCAL_TZ).timestamp()
    return axis, cutoff


def build_host_usage(
    *,
    days: int = 7,
    now: float | None = None,
    paths: HostUsagePaths | None = None,
    active_tmux_panes: int | None = None,
) -> dict[str, Any]:
    """Build the host-wide usage response without mutating any source."""
    resolved_days = max(1, min(14, int(days)))
    resolved_now = float(now if now is not None else time.time())
    resolved_paths = paths or HostUsagePaths.defaults()
    dates, cutoff = _date_axis(resolved_days, resolved_now)

    collectors = (
        ("hermes", _hermes_events),
        ("claude", _claude_events),
        ("codex", _codex_events),
        ("kimi", _kimi_events),
        ("qwen", _qwen_events),
        ("grok", _grok_events),
    )
    events: list[UsageEvent] = []
    errors: list[str] = []
    for label, collector in collectors:
        try:
            events.extend(collector(resolved_paths, cutoff))
        except Exception:
            errors.append(f"{label}: Nutzung konnte nicht gelesen werden")

    provider_tokens: dict[str, int] = defaultdict(int)
    provider_sessions: dict[str, set[str]] = defaultdict(set)
    provider_daily_tokens: dict[tuple[str, str], int] = defaultdict(int)
    provider_daily_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    source_tokens: dict[str, int] = defaultdict(int)
    source_sessions: dict[str, set[str]] = defaultdict(set)

    for event in events:
        date = datetime.fromtimestamp(event.timestamp, _LOCAL_TZ).date().isoformat()
        if date not in dates:
            continue
        provider_tokens[event.provider] += event.tokens
        provider_sessions[event.provider].add(event.session_id)
        provider_daily_tokens[(event.provider, date)] += event.tokens
        provider_daily_sessions[(event.provider, date)].add(event.session_id)
        source_tokens[event.source] += event.tokens
        source_sessions[event.source].add(event.session_id)

    providers = []
    provider_keys = sorted(
        provider_tokens,
        key=lambda key: (
            _PROVIDER_ORDER.index(key) if key in _PROVIDER_ORDER else len(_PROVIDER_ORDER),
            key,
        ),
    )
    for key in provider_keys:
        providers.append(
            {
                "provider": key,
                "label": _PROVIDER_LABELS.get(key, key.title()),
                "total_tokens": provider_tokens[key],
                "sessions": len(provider_sessions[key]),
                "daily": [
                    {
                        "date": date,
                        "tokens": provider_daily_tokens[(key, date)],
                        "sessions": len(provider_daily_sessions[(key, date)]),
                    }
                    for date in dates
                ],
            }
        )

    total_sessions = len({event.session_id for event in events if datetime.fromtimestamp(event.timestamp, _LOCAL_TZ).date().isoformat() in dates})
    sources = [
        {
            "source": source,
            "label": "Hermes" if source == "hermes" else "Terminals",
            "tokens": source_tokens[source],
            "sessions": len(source_sessions[source]),
        }
        for source in ("hermes", "terminal")
        if source_tokens[source] > 0
    ]
    return {
        "generated_at": int(resolved_now),
        "days": resolved_days,
        "dates": dates,
        "total_tokens": sum(provider_tokens.values()),
        "total_sessions": total_sessions,
        "active_tmux_panes": _active_tmux_panes() if active_tmux_panes is None else max(0, int(active_tmux_panes)),
        "sources": sources,
        "providers": providers,
        "errors": errors,
        "accounting_note": "Aktive Ein-/Ausgabe ohne Cache; Hermes-Sessions am letzten Aktivitätstag",
    }


def get_host_usage(*, days: int = 7) -> dict[str, Any]:
    """Cached dashboard accessor (45 s), keyed by the requested window.

    The cold collector walks several terminal-history trees. Multiple browser
    tabs used to start the same scan concurrently after a dashboard restart,
    amplifying disk I/O until the frontend request timeout fired. Coalesce cold
    requests per window: one thread collects while the others reuse its result.
    """
    resolved_days = max(1, min(14, int(days)))
    now = time.monotonic()
    with _CACHE_CONDITION:
        cached = _CACHE.get(resolved_days)
        if cached is not None and cached[0] > now:
            return {**cached[1], "cached": True}
        while resolved_days in _CACHE_IN_FLIGHT:
            _CACHE_CONDITION.wait()
            cached = _CACHE.get(resolved_days)
            if cached is not None and cached[0] > time.monotonic():
                return {**cached[1], "cached": True}
        _CACHE_IN_FLIGHT.add(resolved_days)
    try:
        payload = build_host_usage(days=resolved_days)
    except BaseException:
        with _CACHE_CONDITION:
            _CACHE_IN_FLIGHT.discard(resolved_days)
            _CACHE_CONDITION.notify_all()
        raise
    with _CACHE_CONDITION:
        _CACHE[resolved_days] = (time.monotonic() + _CACHE_TTL_SECONDS, payload)
        _CACHE_IN_FLIGHT.discard(resolved_days)
        _CACHE_CONDITION.notify_all()
    return {**payload, "cached": False}


def _reset_host_usage_cache() -> None:
    with _CACHE_CONDITION:
        _CACHE.clear()
        _CACHE_IN_FLIGHT.clear()
        _CACHE_CONDITION.notify_all()

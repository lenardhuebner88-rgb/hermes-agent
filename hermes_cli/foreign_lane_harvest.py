"""ETL harvester for foreign-lane CLI usage into the usage-facts DB.

Sources (read-only):
  - Codex rollouts: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
  - Kimi wire logs: resume_handle → ``session_index.jsonl`` → ``wire.jsonl``
  - Qwen monthly usage: ``~/.qwen/usage/token-usage-YYYY-MM.jsonl``
  - Grok unified log: ``~/.grok/logs/unified.jsonl`` (``shell.turn.inference_done``)

Does not touch loop dispatch. Writes only into the S2 usage-facts schema
with ``origin`` in {codex_cli, kimi_cli, grok_cli, qwen_cli}.

Codex aggregation: session-level totals come from the **last**
``total_token_usage`` on a rollout (cumulative). Summing every
``total_token_usage`` would multi-count; summing ``last_token_usage`` yields
the same total on complete sessions and is used for per-turn call rows.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from hermes_cli.usage_facts_db import (
    record_llm_call,
    upsert_run_facts,
    usage_facts_db_path,
)

ORIGIN_CODEX = "codex_cli"
ORIGIN_KIMI = "kimi_cli"
ORIGIN_GROK = "grok_cli"
ORIGIN_QWEN = "qwen_cli"

DEFAULT_CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
DEFAULT_KIMI_INDEX = Path.home() / ".kimi-code" / "session_index.jsonl"
DEFAULT_QWEN_USAGE_DIR = Path.home() / ".qwen" / "usage"
DEFAULT_GROK_UNIFIED = Path.home() / ".grok" / "logs" / "unified.jsonl"

# Kimi usageScope values observed in production (2026-07-27 sample of 80 wires):
# turn=2740, session=1. Only "turn" is additive; never mix scopes.
KIMI_SUMMABLE_SCOPE = "turn"

# Qwen schemaVersion values this harvester understands.
QWEN_SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

STATE_VERSION = 1


@dataclass
class RateLimitSnapshot:
    """Rate-limit / quota snapshot captured where the source provides it."""

    origin: str
    run_id: str
    captured_at: str
    payload: dict[str, Any]
    context_window: Optional[int] = None


@dataclass
class ExtractedCall:
    call_index: int
    fields: dict[str, Any]


@dataclass
class ExtractedRun:
    run_id: str
    origin: str
    run_fields: dict[str, Any]
    calls: list[ExtractedCall] = field(default_factory=list)
    rate_limit: Optional[RateLimitSnapshot] = None
    provenance_path: Optional[str] = None
    source_fingerprint: Optional[str] = None


@dataclass
class HarvestStats:
    origin: str
    scanned: int = 0
    extracted: int = 0
    written_runs: int = 0
    written_calls: int = 0
    skipped_unchanged: int = 0
    skipped_existing: int = 0
    skipped_unsupported: int = 0
    errors: int = 0
    duration_s: float = 0.0
    rate_limits_written: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# JSON event stream parser (shared with foreign.sh.candidate contract)
# ---------------------------------------------------------------------------


def parse_json_event_stream(text: str) -> list[dict[str, Any]]:
    """Parse NDJSON **or** pretty-printed multi-object JSON text.

    The broken foreign.sh parser only accepted single-line objects whose first
    non-space character was ``{``. Pretty-printed Grok events fail that check
    on every continuation line and lose all fields (usage included).
    """
    if not text or not text.strip():
        return []

    stripped = text.strip()
    # Whole-document object or array (Grok pretty-printed single result).
    try:
        data = json.loads(stripped)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    objs: list[dict[str, Any]] = []
    # NDJSON first (fast path for Codex / Kimi stream-json).
    ndjson_hit = False
    for ln in text.splitlines():
        s = ln.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            objs.append(obj)
            ndjson_hit = True
    if ndjson_hit:
        return objs

    # Streaming multi-line objects via raw_decode.
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            nxt = text.find("{", idx + 1)
            if nxt < 0:
                break
            idx = nxt
            continue
        if isinstance(obj, dict):
            objs.append(obj)
        idx = end
    return objs


def distill_foreign_events(
    lane: str,
    events_text: str,
    *,
    kimi_session_index: Optional[Path] = None,
) -> dict[str, Any]:
    """Mirror of the foreign.sh distillation block (testable).

    Returns keys: usage, handle, text, parse_mode.
    """
    objs = parse_json_event_stream(events_text)
    usage: Any = None
    handle: Any = None
    text: Any = None

    if lane == "codex":
        for obj in objs:
            if obj.get("type") == "thread.started":
                handle = obj.get("thread_id")
            if obj.get("type") == "turn.completed" and "usage" in obj:
                usage = obj["usage"]
    elif lane == "kimi":
        for obj in objs:
            handle = obj.get("session_id") or handle
            if obj.get("role") == "assistant" and obj.get("content"):
                text = obj["content"]
            # Rare: usage embedded in the stream (not observed live 2026-07-27).
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            if obj.get("type") == "usage.record" and isinstance(obj.get("usage"), dict):
                usage = _kimi_usage_to_summary(obj["usage"])
        if usage is None and handle:
            usage = load_kimi_usage_for_handle(
                str(handle),
                index_path=kimi_session_index or DEFAULT_KIMI_INDEX,
            )
    elif lane == "grok":
        for obj in objs:
            for key in ("result", "text", "output", "content", "message"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    text = val
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            handle = obj.get("session_id") or obj.get("sessionId") or obj.get("thread_id") or handle
    else:
        raise ValueError(f"unsupported lane: {lane!r}")

    return {"usage": usage, "handle": handle, "text": text, "objects": len(objs)}


def legacy_line_parser(text: str) -> list[dict[str, Any]]:
    """The broken foreign.sh parser — kept for regression tests only."""
    objs: list[dict[str, Any]] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            objs.append(json.loads(ln))
        except Exception:
            pass
    return objs


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_codex_rollout(path: Path | str) -> Optional[ExtractedRun]:
    """Parse one Codex rollout JSONL into a run fact + optional turn calls.

    Aggregation: **last total_token_usage** for the session row. Per-turn
    ``last_token_usage`` values become ``run_llm_calls`` rows when present.
    """
    path = Path(path)
    session_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: Optional[str] = None
    captured_at: Optional[str] = None
    turns: list[dict[str, Any]] = []
    last_total: Optional[dict[str, Any]] = None
    last_rate_limits: Optional[dict[str, Any]] = None
    context_window: Optional[int] = None

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        top_type = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ts = obj.get("timestamp")

        if top_type == "session_meta":
            session_id = payload.get("session_id") or payload.get("id") or session_id
            provider = payload.get("model_provider") or provider
            captured_at = payload.get("timestamp") or ts or captured_at
            continue

        if top_type == "turn_context":
            model = payload.get("model") or model
            reasoning_effort = payload.get("effort") or reasoning_effort
            continue

        if payload.get("type") != "token_count":
            continue

        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
        if isinstance(info.get("model_context_window"), int):
            context_window = info["model_context_window"]
        if isinstance(payload.get("rate_limits"), dict):
            last_rate_limits = payload["rate_limits"]
        if total:
            last_total = total
        if last:
            turns.append({"last": last, "ts": ts})

    if not session_id:
        # Fallback: filename rollout-...-<uuid>.jsonl
        stem = path.stem
        if "rollout-" in stem:
            session_id = stem.split("-", 1)[-1]
            # better: last UUID-like segment
            parts = stem.split("-")
            if len(parts) >= 6:
                session_id = "-".join(parts[-5:])
    if not session_id or not last_total:
        return None

    run_id = f"codex_cli:{session_id}"
    run_fields = {
        "origin": ORIGIN_CODEX,
        "provider": provider or "openai",
        "model": model,
        "requested_model": model,
        "model_source": "rollout",
        "billing_mode": "subscription_included",
        "reasoning_effort": reasoning_effort,
        "input_tokens": _int_or_none(last_total.get("input_tokens")),
        "output_tokens": _int_or_none(last_total.get("output_tokens")),
        "cache_read_tokens": _int_or_none(last_total.get("cached_input_tokens")),
        "cache_write_tokens": _int_or_none(last_total.get("cache_write_input_tokens")),
        "reasoning_tokens": _int_or_none(last_total.get("reasoning_output_tokens")),
        "llm_call_count": len(turns) or None,
        "context_window_limit": context_window,
        "context_window_limit_source": "derived" if context_window is not None else None,
        "context_window_used": _int_or_none(last_total.get("total_tokens")),
        "captured_at": captured_at or _utc_now(),
        "source": "measured",
        "lane": "codex",
        "call_kind": "foreign_cli",
    }
    calls: list[ExtractedCall] = []
    for idx, turn in enumerate(turns, start=1):
        last = turn["last"]
        calls.append(
            ExtractedCall(
                call_index=idx,
                fields={
                    "origin": ORIGIN_CODEX,
                    "provider": provider or "openai",
                    "model": model,
                    "requested_model": model,
                    "model_source": "rollout",
                    "reasoning_effort": reasoning_effort,
                    "input_tokens": _int_or_none(last.get("input_tokens")),
                    "output_tokens": _int_or_none(last.get("output_tokens")),
                    "cache_read_tokens": _int_or_none(last.get("cached_input_tokens")),
                    "cache_write_tokens": _int_or_none(last.get("cache_write_input_tokens")),
                    "reasoning_tokens": _int_or_none(last.get("reasoning_output_tokens")),
                    "total_tokens": _int_or_none(last.get("total_tokens")),
                    "context_window_used": _int_or_none(last.get("total_tokens")),
                },
            )
        )

    rate = None
    if last_rate_limits is not None:
        rate = RateLimitSnapshot(
            origin=ORIGIN_CODEX,
            run_id=run_id,
            captured_at=run_fields["captured_at"],
            payload=last_rate_limits,
            context_window=context_window,
        )

    return ExtractedRun(
        run_id=run_id,
        origin=ORIGIN_CODEX,
        run_fields=run_fields,
        calls=calls,
        rate_limit=rate,
        provenance_path=str(path),
        source_fingerprint=_file_fingerprint(path),
    )


def sum_codex_last_token_usage(path: Path | str) -> dict[str, int]:
    """Sum per-turn last_token_usage — golden-test companion to last-total."""
    path = Path(path)
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "events": 0,
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
        if not last:
            continue
        totals["events"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            totals[key] += int(last.get(key) or 0)
    return totals


# ---------------------------------------------------------------------------
# Kimi
# ---------------------------------------------------------------------------


def _kimi_usage_to_summary(usage: Mapping[str, Any]) -> dict[str, int]:
    """Map Kimi field names onto a neutral token summary.

    Measured real keys: ``inputOther``, ``inputCacheRead``, ``inputCacheCreation``,
    ``output``. Total input = other + cache_read + cache_creation (Anthropic-style
    partition of the prompt; they are disjoint components of one request).
    """
    other = int(usage.get("inputOther") or 0)
    cache_read = int(usage.get("inputCacheRead") or 0)
    cache_write = int(usage.get("inputCacheCreation") or 0)
    output = int(usage.get("output") or 0)
    return {
        "input_tokens": other + cache_read + cache_write,
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "input_other_tokens": other,
    }


def load_kimi_session_index(index_path: Path | str) -> dict[str, dict[str, Any]]:
    """Map sessionId → latest index record (later lines win)."""
    index_path = Path(index_path)
    by_id: dict[str, dict[str, Any]] = {}
    try:
        text = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return by_id
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (TypeError, ValueError):
            continue
        sid = rec.get("sessionId")
        if isinstance(sid, str) and sid:
            by_id[sid] = rec
    return by_id


def resolve_kimi_wire_path(
    resume_handle: str,
    *,
    index_path: Path | str = DEFAULT_KIMI_INDEX,
    index: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Optional[Path]:
    """resume_handle → session_index.jsonl → agents/main/wire.jsonl."""
    if index is None:
        index = load_kimi_session_index(index_path)
    rec = index.get(resume_handle)
    if not rec:
        return None
    session_dir = rec.get("sessionDir")
    if not session_dir:
        return None
    wire = Path(str(session_dir)) / "agents" / "main" / "wire.jsonl"
    return wire if wire.is_file() else None


def extract_kimi_wire(
    wire_path: Path | str,
    *,
    session_id: str,
) -> Optional[ExtractedRun]:
    """Sum turn-scoped usage.record lines from a Kimi wire.jsonl."""
    wire_path = Path(wire_path)
    try:
        lines = wire_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    model: Optional[str] = None
    turn_count = 0
    scopes_seen: set[str] = set()
    calls: list[ExtractedCall] = []
    last_time: Optional[int] = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "usage.record":
            continue
        scope = obj.get("usageScope")
        if scope is not None:
            scopes_seen.add(str(scope))
        if scope != KIMI_SUMMABLE_SCOPE:
            continue
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
        if not usage:
            continue
        summary = _kimi_usage_to_summary(usage)
        model = obj.get("model") or model
        last_time = obj.get("time") if isinstance(obj.get("time"), int) else last_time
        turn_count += 1
        for key in totals:
            totals[key] += int(summary.get(key) or 0)
        calls.append(
            ExtractedCall(
                call_index=turn_count,
                fields={
                    "origin": ORIGIN_KIMI,
                    "provider": "kimi-code",
                    "model": obj.get("model") or model,
                    "requested_model": obj.get("model") or model,
                    "model_source": "wire",
                    "input_tokens": summary["input_tokens"],
                    "output_tokens": summary["output_tokens"],
                    "cache_read_tokens": summary["cache_read_tokens"],
                    "cache_write_tokens": summary["cache_write_tokens"],
                    "total_tokens": summary["input_tokens"] + summary["output_tokens"],
                },
            )
        )

    if turn_count == 0:
        return None

    captured_at = _utc_now()
    if last_time is not None:
        # Kimi times look like ms epoch; tolerate seconds.
        ts = last_time / 1000.0 if last_time > 10_000_000_000 else float(last_time)
        captured_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    run_id = f"kimi_cli:{session_id}"
    return ExtractedRun(
        run_id=run_id,
        origin=ORIGIN_KIMI,
        run_fields={
            "origin": ORIGIN_KIMI,
            "provider": "kimi-code",
            "model": model,
            "requested_model": model,
            "model_source": "wire",
            "billing_mode": "subscription_included",
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cache_read_tokens": totals["cache_read_tokens"],
            "cache_write_tokens": totals["cache_write_tokens"],
            "llm_call_count": turn_count,
            "captured_at": captured_at,
            "source": "measured",
            "lane": "kimi",
            "call_kind": "foreign_cli",
        },
        calls=calls,
        provenance_path=str(wire_path),
        source_fingerprint=_file_fingerprint(wire_path),
    )


def load_kimi_usage_for_handle(
    resume_handle: str,
    *,
    index_path: Path | str = DEFAULT_KIMI_INDEX,
) -> Optional[dict[str, int]]:
    wire = resolve_kimi_wire_path(resume_handle, index_path=index_path)
    if wire is None:
        return None
    extracted = extract_kimi_wire(wire, session_id=resume_handle)
    if extracted is None:
        return None
    rf = extracted.run_fields
    return {
        "input_tokens": int(rf.get("input_tokens") or 0),
        "output_tokens": int(rf.get("output_tokens") or 0),
        "cache_read_tokens": int(rf.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(rf.get("cache_write_tokens") or 0),
        "llm_call_count": int(rf.get("llm_call_count") or 0),
    }


# ---------------------------------------------------------------------------
# Qwen
# ---------------------------------------------------------------------------


def extract_qwen_row(obj: Mapping[str, Any]) -> Optional[ExtractedRun]:
    """Map one Qwen usage JSONL row (schemaVersion-aware)."""
    version = obj.get("schemaVersion")
    if version not in QWEN_SUPPORTED_SCHEMA_VERSIONS:
        return None
    row_id = obj.get("id")
    if not row_id:
        return None

    run_id = f"qwen_cli:{row_id}"
    model = obj.get("model")
    input_tokens = _int_or_none(obj.get("inputTokens"))
    output_tokens = _int_or_none(obj.get("outputTokens"))
    cached = _int_or_none(obj.get("cachedTokens"))
    thoughts = _int_or_none(obj.get("thoughtsTokens"))
    duration_ms = obj.get("apiDurationMs")
    try:
        duration_ms_f = float(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_ms_f = None

    tool_calls = None
    tools = obj.get("tools")
    if isinstance(tools, dict):
        tool_calls = _int_or_none(tools.get("totalCalls"))

    return ExtractedRun(
        run_id=run_id,
        origin=ORIGIN_QWEN,
        run_fields={
            "origin": ORIGIN_QWEN,
            "provider": "qwen",
            "model": model,
            "requested_model": model,
            "model_source": "usage_log",
            "billing_mode": obj.get("authType"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cached,
            "reasoning_tokens": thoughts,
            "duration_ms": duration_ms_f,
            "tool_call_count": tool_calls,
            "llm_call_count": 1,
            "captured_at": obj.get("timestamp") or _utc_now(),
            "source": "measured",
            "lane": "qwen",
            "call_kind": obj.get("source") or "foreign_cli",
            "profile": obj.get("sessionId"),
        },
        calls=[
            ExtractedCall(
                call_index=1,
                fields={
                    "origin": ORIGIN_QWEN,
                    "provider": "qwen",
                    "model": model,
                    "requested_model": model,
                    "model_source": "usage_log",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cached,
                    "reasoning_tokens": thoughts,
                    "total_tokens": _int_or_none(obj.get("totalTokens")),
                    "duration_ms": duration_ms_f,
                    "tool_call_count": tool_calls,
                },
            )
        ],
    )


def iter_qwen_usage_files(usage_dir: Path | str = DEFAULT_QWEN_USAGE_DIR) -> list[Path]:
    usage_dir = Path(usage_dir)
    if not usage_dir.is_dir():
        return []
    return sorted(usage_dir.glob("token-usage-*.jsonl"))


# ---------------------------------------------------------------------------
# Grok
# ---------------------------------------------------------------------------


def extract_grok_inference_event(obj: Mapping[str, Any]) -> Optional[ExtractedRun]:
    """One shell.turn.inference_done log line → one run (sid+loop).

    Tokens live in ``ctx`` (prompt_tokens, cached_prompt_tokens,
    completion_tokens, reasoning_tokens). No model id and no rate-limit
    snapshot are present on this event type — those fields stay unavailable.
    """
    if obj.get("msg") != "shell.turn.inference_done":
        return None
    sid = obj.get("sid")
    if not sid:
        return None
    ctx = obj.get("ctx") if isinstance(obj.get("ctx"), dict) else {}
    prompt = _int_or_none(ctx.get("prompt_tokens"))
    cached = _int_or_none(ctx.get("cached_prompt_tokens"))
    completion = _int_or_none(ctx.get("completion_tokens"))
    reasoning = _int_or_none(ctx.get("reasoning_tokens"))
    if prompt is None and completion is None:
        return None

    loop_index = ctx.get("loop_index")
    try:
        loop_i = int(loop_index) if loop_index is not None else 0
    except (TypeError, ValueError):
        loop_i = 0

    run_id = f"grok_cli:{sid}:loop{loop_i}"
    duration_ms = ctx.get("model_elapsed_ms")
    try:
        duration_ms_f = float(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_ms_f = None
    first_token_ms = ctx.get("ttft_ms")
    try:
        first_token_ms_f = float(first_token_ms) if first_token_ms is not None else None
    except (TypeError, ValueError):
        first_token_ms_f = None

    fields = {
        "origin": ORIGIN_GROK,
        "provider": "xai",
        "model": None,  # not present on inference_done
        "model_source": "unified_log",
        "billing_mode": "subscription_included",
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": cached,
        "reasoning_tokens": reasoning,
        "duration_ms": duration_ms_f,
        "first_token_ms": first_token_ms_f,
        "llm_call_count": 1,
        "captured_at": obj.get("ts") or _utc_now(),
        "source": "measured",
        "lane": "grok",
        "call_kind": "foreign_cli",
        "profile": str(sid),
    }
    return ExtractedRun(
        run_id=run_id,
        origin=ORIGIN_GROK,
        run_fields=fields,
        calls=[
            ExtractedCall(
                call_index=1,
                fields={
                    "origin": ORIGIN_GROK,
                    "provider": "xai",
                    "model_source": "unified_log",
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cache_read_tokens": cached,
                    "reasoning_tokens": reasoning,
                    "total_tokens": (prompt or 0) + (completion or 0)
                    if prompt is not None or completion is not None
                    else None,
                    "duration_ms": duration_ms_f,
                    "first_token_ms": first_token_ms_f,
                },
            )
        ],
    )


def iter_grok_inference_events(path: Path | str) -> Iterator[dict[str, Any]]:
    path = Path(path)
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("msg") == "shell.turn.inference_done":
                yield obj


# ---------------------------------------------------------------------------
# Persistence / incremental harvest
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_fingerprint(path: Path) -> str:
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "missing"


def load_state(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {"version": STATE_VERSION, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (TypeError, ValueError, OSError):
        return {"version": STATE_VERSION, "sources": {}}
    if not isinstance(data, dict):
        return {"version": STATE_VERSION, "sources": {}}
    data.setdefault("version", STATE_VERSION)
    data.setdefault("sources", {})
    return data


def save_state(path: Path | str, state: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _run_exists(db_path: Path, run_id: str) -> bool:
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM run_usage_facts WHERE run_id=? LIMIT 1",
            (run_id,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def write_extracted_run(
    extracted: ExtractedRun,
    *,
    db_path: Path | str,
    rate_limit_path: Optional[Path] = None,
    force: bool = False,
    include_calls: bool = False,
) -> tuple[int, int, int]:
    """Write one extracted run. Returns (runs_written, calls_written, rate_limits_written).

    Default is **session/run-level only**. Per-turn ``run_llm_calls`` rows are
    optional (``include_calls=True``) because Codex rollouts routinely carry
    dozens of token_count events; writing each through a separate connection
    makes a full backfill impractically slow while the session total already
    lives on ``run_usage_facts`` via last ``total_token_usage``.
    """
    db_path = Path(db_path)
    if not force and _run_exists(db_path, extracted.run_id):
        return 0, 0, 0

    calls_written = 0
    if include_calls and extracted.calls:
        for call in extracted.calls:
            record_llm_call(
                extracted.run_id,
                call.call_index,
                fields=call.fields,
                run_fields=extracted.run_fields,
                path=db_path,
            )
        # Re-assert session totals (Codex last-total) so call-sum refresh cannot
        # diverge if a turn was missing last_token_usage.
        upsert_run_facts(extracted.run_id, extracted.run_fields, path=db_path)
        calls_written = len(extracted.calls)
    else:
        upsert_run_facts(extracted.run_id, extracted.run_fields, path=db_path)

    rl_written = 0
    if extracted.rate_limit is not None and rate_limit_path is not None:
        _append_rate_limit(rate_limit_path, extracted.rate_limit)
        rl_written = 1
    return 1, calls_written, rl_written


def _append_rate_limit(path: Path, snapshot: RateLimitSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "origin": snapshot.origin,
        "run_id": snapshot.run_id,
        "captured_at": snapshot.captured_at,
        "context_window": snapshot.context_window,
        "rate_limits": snapshot.payload,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _source_state(state: dict[str, Any], origin: str) -> dict[str, Any]:
    sources = state.setdefault("sources", {})
    bucket = sources.setdefault(origin, {"files": {}})
    bucket.setdefault("files", {})
    return bucket


def _should_skip_file(bucket: Mapping[str, Any], path: Path) -> bool:
    files = bucket.get("files") if isinstance(bucket.get("files"), dict) else {}
    prev = files.get(str(path))
    return prev == _file_fingerprint(path)


def _mark_file(bucket: dict[str, Any], path: Path) -> None:
    files = bucket.setdefault("files", {})
    files[str(path)] = _file_fingerprint(path)


def harvest_codex(
    *,
    sessions_root: Path | str = DEFAULT_CODEX_SESSIONS,
    db_path: Path | str,
    state: dict[str, Any],
    rate_limit_path: Optional[Path] = None,
    force: bool = False,
    include_calls: bool = False,
) -> HarvestStats:
    t0 = time.perf_counter()
    stats = HarvestStats(origin=ORIGIN_CODEX)
    root = Path(sessions_root)
    bucket = _source_state(state, ORIGIN_CODEX)
    if not root.is_dir():
        stats.duration_s = time.perf_counter() - t0
        return stats

    # recursive=True is mandatory — bare **/glob returns empty (measured footgun).
    paths = sorted(root.glob("**/rollout-*.jsonl"))
    for path in paths:
        stats.scanned += 1
        if not force and _should_skip_file(bucket, path):
            stats.skipped_unchanged += 1
            continue
        try:
            extracted = extract_codex_rollout(path)
        except Exception:
            stats.errors += 1
            continue
        if extracted is None:
            stats.skipped_unsupported += 1
            _mark_file(bucket, path)
            continue
        stats.extracted += 1
        written, calls, rl = write_extracted_run(
            extracted,
            db_path=db_path,
            rate_limit_path=rate_limit_path,
            force=force,
            include_calls=include_calls,
        )
        if written == 0:
            stats.skipped_existing += 1
        else:
            stats.written_runs += written
            stats.written_calls += calls
            stats.rate_limits_written += rl
        _mark_file(bucket, path)

    stats.duration_s = time.perf_counter() - t0
    return stats


def harvest_kimi(
    *,
    index_path: Path | str = DEFAULT_KIMI_INDEX,
    db_path: Path | str,
    state: dict[str, Any],
    force: bool = False,
    include_calls: bool = False,
) -> HarvestStats:
    t0 = time.perf_counter()
    stats = HarvestStats(origin=ORIGIN_KIMI)
    index_path = Path(index_path)
    bucket = _source_state(state, ORIGIN_KIMI)
    index = load_kimi_session_index(index_path)
    for session_id, rec in index.items():
        stats.scanned += 1
        wire = resolve_kimi_wire_path(session_id, index=index)
        if wire is None:
            stats.skipped_unsupported += 1
            continue
        if not force and _should_skip_file(bucket, wire):
            stats.skipped_unchanged += 1
            continue
        try:
            extracted = extract_kimi_wire(wire, session_id=session_id)
        except Exception:
            stats.errors += 1
            continue
        if extracted is None:
            stats.skipped_unsupported += 1
            _mark_file(bucket, wire)
            continue
        stats.extracted += 1
        written, calls, _rl = write_extracted_run(
            extracted,
            db_path=db_path,
            force=force,
            include_calls=include_calls,
        )
        if written == 0:
            stats.skipped_existing += 1
        else:
            stats.written_runs += written
            stats.written_calls += calls
        _mark_file(bucket, wire)

    stats.duration_s = time.perf_counter() - t0
    return stats


def harvest_qwen(
    *,
    usage_dir: Path | str = DEFAULT_QWEN_USAGE_DIR,
    db_path: Path | str,
    state: dict[str, Any],
    force: bool = False,
    include_calls: bool = False,
) -> HarvestStats:
    t0 = time.perf_counter()
    stats = HarvestStats(origin=ORIGIN_QWEN)
    bucket = _source_state(state, ORIGIN_QWEN)
    for path in iter_qwen_usage_files(usage_dir):
        if not force and _should_skip_file(bucket, path):
            stats.scanned += 1
            stats.skipped_unchanged += 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats.errors += 1
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            stats.scanned += 1
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                stats.errors += 1
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("schemaVersion") not in QWEN_SUPPORTED_SCHEMA_VERSIONS:
                stats.skipped_unsupported += 1
                continue
            extracted = extract_qwen_row(obj)
            if extracted is None:
                stats.skipped_unsupported += 1
                continue
            stats.extracted += 1
            written, calls, _rl = write_extracted_run(
                extracted,
                db_path=db_path,
                force=force,
                include_calls=include_calls,
            )
            if written == 0:
                stats.skipped_existing += 1
            else:
                stats.written_runs += written
                stats.written_calls += calls
        _mark_file(bucket, path)
    stats.duration_s = time.perf_counter() - t0
    return stats


def harvest_grok(
    *,
    unified_path: Path | str = DEFAULT_GROK_UNIFIED,
    db_path: Path | str,
    state: dict[str, Any],
    force: bool = False,
    include_calls: bool = False,
) -> HarvestStats:
    t0 = time.perf_counter()
    stats = HarvestStats(origin=ORIGIN_GROK)
    path = Path(unified_path)
    bucket = _source_state(state, ORIGIN_GROK)
    if not path.is_file():
        stats.duration_s = time.perf_counter() - t0
        return stats
    if not force and _should_skip_file(bucket, path):
        # Still need line count? Treat whole file as unchanged → 0 writes.
        stats.scanned = 1
        stats.skipped_unchanged = 1
        stats.duration_s = time.perf_counter() - t0
        return stats

    for obj in iter_grok_inference_events(path):
        stats.scanned += 1
        try:
            extracted = extract_grok_inference_event(obj)
        except Exception:
            stats.errors += 1
            continue
        if extracted is None:
            stats.skipped_unsupported += 1
            continue
        stats.extracted += 1
        written, calls, _rl = write_extracted_run(
            extracted,
            db_path=db_path,
            force=force,
            include_calls=include_calls,
        )
        if written == 0:
            stats.skipped_existing += 1
        else:
            stats.written_runs += written
            stats.written_calls += calls
    _mark_file(bucket, path)
    stats.duration_s = time.perf_counter() - t0
    return stats


def harvest_all(
    *,
    db_path: Optional[Path | str] = None,
    state_path: Optional[Path | str] = None,
    rate_limit_path: Optional[Path | str] = None,
    codex_sessions: Path | str = DEFAULT_CODEX_SESSIONS,
    kimi_index: Path | str = DEFAULT_KIMI_INDEX,
    qwen_usage_dir: Path | str = DEFAULT_QWEN_USAGE_DIR,
    grok_unified: Path | str = DEFAULT_GROK_UNIFIED,
    origins: Optional[list[str]] = None,
    force: bool = False,
    include_calls: bool = False,
) -> dict[str, Any]:
    """Run selected harvesters; return per-origin stats + totals."""
    db = usage_facts_db_path(db_path)
    state_file = Path(state_path) if state_path else db.with_name("foreign_lane_harvest_state.json")
    rl_path = (
        Path(rate_limit_path)
        if rate_limit_path
        else db.with_name("foreign_rate_limit_snapshots.jsonl")
    )
    state = load_state(state_file)
    wanted = set(origins) if origins else {
        ORIGIN_CODEX,
        ORIGIN_KIMI,
        ORIGIN_GROK,
        ORIGIN_QWEN,
    }

    results: dict[str, HarvestStats] = {}
    if ORIGIN_CODEX in wanted:
        results[ORIGIN_CODEX] = harvest_codex(
            sessions_root=codex_sessions,
            db_path=db,
            state=state,
            rate_limit_path=rl_path,
            force=force,
            include_calls=include_calls,
        )
    if ORIGIN_KIMI in wanted:
        results[ORIGIN_KIMI] = harvest_kimi(
            index_path=kimi_index,
            db_path=db,
            state=state,
            force=force,
            include_calls=include_calls,
        )
    if ORIGIN_QWEN in wanted:
        results[ORIGIN_QWEN] = harvest_qwen(
            usage_dir=qwen_usage_dir,
            db_path=db,
            state=state,
            force=force,
            include_calls=include_calls,
        )
    if ORIGIN_GROK in wanted:
        results[ORIGIN_GROK] = harvest_grok(
            unified_path=grok_unified,
            db_path=db,
            state=state,
            force=force,
            include_calls=include_calls,
        )

    save_state(state_file, state)
    total_written = sum(s.written_runs for s in results.values())
    return {
        "db_path": str(db),
        "state_path": str(state_file),
        "rate_limit_path": str(rl_path),
        "written_runs_total": total_written,
        "origins": {name: stats.as_dict() for name, stats in results.items()},
    }


def default_state_path_for_db(db_path: Path | str) -> Path:
    return Path(db_path).with_name("foreign_lane_harvest_state.json")

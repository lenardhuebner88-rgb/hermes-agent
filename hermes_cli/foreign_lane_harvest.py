"""ETL harvester for foreign-lane CLI usage into the usage-facts DB.

Sources (read-only):
  - Codex rollouts: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
  - Kimi wire logs: resume_handle → ``session_index.jsonl`` → ``wire.jsonl``
  - Qwen monthly usage: ``~/.qwen/usage/token-usage-YYYY-MM.jsonl``
  - Grok unified log: ``~/.grok/logs/unified.jsonl`` (``shell.turn.inference_done``)
  - foreign.sh run metas: ``~/.hermes/runs/*/meta`` (session correlation)

Does not touch loop dispatch. Writes only into the S2 usage-facts schema
with ``origin`` in {codex_cli, kimi_cli, grok_cli, qwen_cli}.

Codex aggregation: session-level totals come from the **last**
``total_token_usage`` on a rollout (cumulative). Summing every
``total_token_usage`` would multi-count; summing ``last_token_usage`` yields
the same total on complete sessions and is used for per-turn call rows.

Session correlation (S8): map foreign-CLI usage rows back to the Claude Code
session that spawned them. Evidence sources, strongest first:

  1. ``foreign_run_meta`` — ``meta.claude_session_id`` + ``meta.resume_handle``
  2. ``foreign_transcript_spawn`` — transcript ``foreign.sh`` spawn + run dir

``NULL`` means unobserved. Ambiguous multi-candidate matches stay ``NULL``.
Never overwrite an already-set ``session_id``.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote

from hermes_cli.usage_facts_db import (
    finalize_run_tool_call_count,
    initialize_usage_facts_db,
    record_llm_call,
    upsert_run_facts,
    usage_facts_db_path,
)

ORIGIN_CODEX = "codex_cli"
ORIGIN_KIMI = "kimi_cli"
ORIGIN_GROK = "grok_cli"
ORIGIN_QWEN = "qwen_cli"

FOREIGN_ORIGINS = frozenset({ORIGIN_CODEX, ORIGIN_KIMI, ORIGIN_GROK, ORIGIN_QWEN})

# correlation_source values — keep distinguishable (different proof strength).
CORRELATION_SOURCE_META = "foreign_run_meta"
CORRELATION_SOURCE_TRANSCRIPT = "foreign_transcript_spawn"

DEFAULT_CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
DEFAULT_KIMI_INDEX = Path.home() / ".kimi-code" / "session_index.jsonl"
DEFAULT_QWEN_USAGE_DIR = Path.home() / ".qwen" / "usage"
DEFAULT_GROK_UNIFIED = Path.home() / ".grok" / "logs" / "unified.jsonl"
DEFAULT_RUNS_ROOT = Path.home() / ".hermes" / "runs"
DEFAULT_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Kimi usageScope values observed in production (2026-07-27 sample of 80 wires):
# turn=2740, session=1. Only "turn" is additive; never mix scopes.
KIMI_SUMMABLE_SCOPE = "turn"

# Qwen schemaVersion values this harvester understands.
QWEN_SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

STATE_VERSION = 1

# Run-dir timestamps from foreign.sh use local wall clock (`date +%Y%m%dT%H%M%S`).
# Transcripts are UTC. Compare both as aware UTC datetimes.
_RUN_DIR_NAME_RE = re.compile(
    r"^(?P<ts>\d{8}T\d{6})-(?P<lane>codex|kimi|grok|qwen)-(?P<slug>.+)$"
)
_FOREIGN_SH_RE = re.compile(
    r"(?:^|[\s;|&])(?:\S*/)?foreign\.sh\s+"
    r"(?P<lane>codex|kimi|grok|qwen)\b(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL,
)
# Spawn → run-dir matching window (seconds). Run dir is created at spawn time;
# allow small clock skew before, generous lag after (preflight + queue).
SPAWN_MATCH_WINDOW_BEFORE_S = 120
SPAWN_MATCH_WINDOW_AFTER_S = 7200

_INVALID_HANDLES = frozenset({"", "none", "null", "unavailable"})


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
            "billing_mode": "subscription_included",
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

    # Extracted sessions are terminal, so absence of tool events is an
    # observed zero.  The conditional finalizer preserves richer existing
    # measurements during replays and force-harvests.
    finalize_run_tool_call_count(extracted.run_id, path=db_path)

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
    runs_root: Path | str = DEFAULT_RUNS_ROOT,
    origins: Optional[list[str]] = None,
    force: bool = False,
    include_calls: bool = False,
    correlate_sessions: bool = True,
) -> dict[str, Any]:
    """Run selected harvesters; return per-origin stats + totals.

    After writing run facts, applies ``meta.claude_session_id`` correlation
    (when present) so newly harvested foreign-CLI rows pick up the spawning
    Claude session without a separate backfill pass.
    """
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

    correlation: Optional[dict[str, Any]] = None
    if correlate_sessions:
        # Apply meta-based session binding (no-op when claude_session_id absent).
        correlation = correlate_from_run_metas(
            db_path=db,
            runs_root=runs_root,
            apply=True,
        )

    return {
        "db_path": str(db),
        "state_path": str(state_file),
        "rate_limit_path": str(rl_path),
        "written_runs_total": total_written,
        "origins": {name: stats.as_dict() for name, stats in results.items()},
        "session_correlation": correlation,
    }


def default_state_path_for_db(db_path: Path | str) -> Path:
    return Path(db_path).with_name("foreign_lane_harvest_state.json")


# ---------------------------------------------------------------------------
# Session correlation: foreign.sh meta + transcript spawn backfill
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMetaRecord:
    """One ``~/.hermes/runs/<ts>-<lane>-<slug>/meta`` parse."""

    run_dir: Path
    dir_name: str
    lane: Optional[str]
    slug: Optional[str]
    started_at: Optional[datetime]
    resume_handle: Optional[str]
    claude_session_id: Optional[str]
    raw: dict[str, str]


@dataclass(frozen=True)
class ForeignSpawnRecord:
    """One Claude Code transcript record that invoked foreign.sh."""

    claude_session_id: str
    timestamp: datetime
    lane: str
    slug: str
    brief_path: Optional[str] = None
    source_path: Optional[str] = None


def parse_meta_file(path: Path | str) -> dict[str, str]:
    """Parse foreign.sh meta: ``key=value`` per line (first ``=`` splits).

    Real format measured 2026-07-31::

        resume_handle=019fb847-9429-7922-9818-8fa2762450ab
        usage=unavailable
        lane=codex
        exit=0
        duration_s=3577
        dir=/home/piet/...
        run=/home/piet/.hermes/runs/...
        read_only=0

    Optional future key (written by a parallel wrapper change)::

        claude_session_id=<uuid>
    """
    path = Path(path)
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def _is_valid_handle(handle: Optional[str]) -> bool:
    if handle is None:
        return False
    h = handle.strip()
    return bool(h) and h.lower() not in _INVALID_HANDLES


def _slugify_foreign(name: str) -> str:
    """Mirror foreign.sh: basename, strip .md, keep [A-Za-z0-9], collapse rest to -."""
    base = Path(str(name).strip().strip("\"'")).name
    if base.endswith(".md"):
        base = base[:-3]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-")
    return slug


def parse_run_dir_name(
    name: str,
    *,
    local_tz: Optional[Any] = None,
) -> Optional[tuple[datetime, str, str]]:
    """Parse ``YYYYMMDDTHHMMSS-<lane>-<slug>`` → (utc_ts, lane, slug).

    foreign.sh stamps the directory with local wall clock; convert to UTC for
    comparison with transcript timestamps.
    """
    m = _RUN_DIR_NAME_RE.match(name)
    if not m:
        return None
    tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    try:
        local_dt = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%S").replace(tzinfo=tz)
    except ValueError:
        return None
    return local_dt.astimezone(timezone.utc), m.group("lane").lower(), m.group("slug")


def resume_handle_from_run_id(run_id: str) -> Optional[str]:
    """Extract the foreign-CLI session handle from a usage ``run_id``.

    Forms (measured)::

        codex_cli:<uuid>
        kimi_cli:session_<uuid>
        qwen_cli:<call-or-session-uuid>
        grok_cli:<uuid>:loop17   ← third segment is NOT part of the handle
    """
    if not run_id or ":" not in run_id:
        return None
    origin, rest = run_id.split(":", 1)
    if origin not in FOREIGN_ORIGINS or not rest:
        return None
    if origin == ORIGIN_GROK:
        # strip trailing :loopN (loop index is not the session id)
        m = re.match(r"^(?P<handle>.+):loop\d+$", rest)
        if m:
            return m.group("handle")
        return rest
    return rest


def origin_for_lane(lane: str) -> Optional[str]:
    mapping = {
        "codex": ORIGIN_CODEX,
        "kimi": ORIGIN_KIMI,
        "grok": ORIGIN_GROK,
        "qwen": ORIGIN_QWEN,
    }
    return mapping.get(lane.lower())


def iter_run_metas(
    runs_root: Path | str = DEFAULT_RUNS_ROOT,
    *,
    local_tz: Optional[Any] = None,
) -> list[RunMetaRecord]:
    """Load every ``runs/*/meta`` file (skips non-run dirs like ``_briefs``)."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    records: list[RunMetaRecord] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        meta_path = child / "meta"
        if not meta_path.is_file():
            continue
        raw = parse_meta_file(meta_path)
        parsed = parse_run_dir_name(child.name, local_tz=local_tz)
        if parsed:
            started_at, lane_from_name, slug = parsed
        else:
            started_at, lane_from_name, slug = None, None, None
        lane = (raw.get("lane") or lane_from_name or "").strip().lower() or None
        handle = raw.get("resume_handle")
        claude = raw.get("claude_session_id")
        records.append(
            RunMetaRecord(
                run_dir=child,
                dir_name=child.name,
                lane=lane,
                slug=slug,
                started_at=started_at,
                resume_handle=handle.strip() if isinstance(handle, str) else handle,
                claude_session_id=(
                    claude.strip() if isinstance(claude, str) and claude.strip() else None
                ),
                raw=raw,
            )
        )
    return records


def build_handle_to_claude_from_metas(
    metas: Sequence[RunMetaRecord],
) -> dict[str, Any]:
    """Build resume_handle → claude_session_id from metas that carry both.

    Handles that are missing/invalid (``none``) are counted as unassignable.
    A handle mapping to **more than one** distinct claude session is ambiguous
    and is dropped (NULL rule).
    """
    handle_sessions: dict[str, set[str]] = {}
    unassignable_no_handle = 0
    metas_with_claude = 0
    metas_without_claude = 0

    for meta in metas:
        if meta.claude_session_id:
            metas_with_claude += 1
        else:
            metas_without_claude += 1
            continue
        if not _is_valid_handle(meta.resume_handle):
            unassignable_no_handle += 1
            continue
        handle = str(meta.resume_handle).strip()
        handle_sessions.setdefault(handle, set()).add(meta.claude_session_id)

    mapping: dict[str, str] = {}
    ambiguous_handles = 0
    for handle, sessions in handle_sessions.items():
        if len(sessions) == 1:
            mapping[handle] = next(iter(sessions))
        else:
            ambiguous_handles += 1

    return {
        "handle_to_session": mapping,
        "metas_with_claude": metas_with_claude,
        "metas_without_claude": metas_without_claude,
        "unassignable_no_handle": unassignable_no_handle,
        "ambiguous_handles": ambiguous_handles,
        "unique_handles": len(mapping),
    }


def _connect_usage_db(db_path: Path, *, readonly: bool) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    if readonly:
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    else:
        conn = sqlite3.connect(str(resolved), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def find_run_ids_for_handle(
    conn: sqlite3.Connection,
    handle: str,
    *,
    origins: Optional[Sequence[str]] = None,
) -> list[str]:
    """Locate ``run_usage_facts.run_id`` values for a foreign resume_handle.

    Matching rules (per-lane, measured)::

      codex_cli  → run_id == 'codex_cli:{handle}'
      kimi_cli   → run_id == 'kimi_cli:{handle}'
      grok_cli   → run_id LIKE 'grok_cli:{handle}:loop%'
      qwen_cli   → run_id == 'qwen_cli:{handle}'
                   OR profile == handle  (usage log stores sessionId in profile;
                   run_id carries the per-call id, not the session handle)
    """
    handle = handle.strip()
    if not handle:
        return []
    wanted = set(origins) if origins else set(FOREIGN_ORIGINS)
    found: list[str] = []

    if ORIGIN_CODEX in wanted:
        row = conn.execute(
            "SELECT run_id FROM run_usage_facts WHERE run_id=?",
            (f"{ORIGIN_CODEX}:{handle}",),
        ).fetchone()
        if row:
            found.append(str(row["run_id"]))

    if ORIGIN_KIMI in wanted:
        row = conn.execute(
            "SELECT run_id FROM run_usage_facts WHERE run_id=?",
            (f"{ORIGIN_KIMI}:{handle}",),
        ).fetchone()
        if row:
            found.append(str(row["run_id"]))

    if ORIGIN_GROK in wanted:
        # Third component (:loopN) is not part of the session handle.
        prefix = f"{ORIGIN_GROK}:{handle}:loop"
        rows = conn.execute(
            "SELECT run_id FROM run_usage_facts WHERE run_id LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
        for row in rows:
            found.append(str(row["run_id"]))

    if ORIGIN_QWEN in wanted:
        row = conn.execute(
            "SELECT run_id FROM run_usage_facts WHERE run_id=?",
            (f"{ORIGIN_QWEN}:{handle}",),
        ).fetchone()
        if row:
            found.append(str(row["run_id"]))
        # Session-level match: harvest stores Qwen sessionId on profile.
        rows = conn.execute(
            "SELECT run_id FROM run_usage_facts "
            "WHERE origin=? AND profile=? AND run_id != ?",
            (ORIGIN_QWEN, handle, f"{ORIGIN_QWEN}:{handle}"),
        ).fetchall()
        for row in rows:
            found.append(str(row["run_id"]))

    # de-dupe preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for rid in found:
        if rid not in seen:
            seen.add(rid)
            unique.append(rid)
    return unique


def _token_checksum(conn: sqlite3.Connection) -> dict[str, Any]:
    """Snapshot token/time columns — used to prove backfill does not touch them."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(input_tokens),0) AS in_tok, "
        "COALESCE(SUM(output_tokens),0) AS out_tok, "
        "COALESCE(SUM(cache_read_tokens),0) AS cache_tok, "
        "COALESCE(SUM(reasoning_tokens),0) AS reason_tok, "
        "COALESCE(SUM(duration_ms),0) AS dur, "
        "SUM(CASE WHEN task_run_id IS NOT NULL THEN 1 ELSE 0 END) AS task_n, "
        "SUM(CASE WHEN first_call_at IS NOT NULL THEN 1 ELSE 0 END) AS first_n, "
        "SUM(CASE WHEN last_call_at IS NOT NULL THEN 1 ELSE 0 END) AS last_n, "
        "SUM(CASE WHEN captured_at IS NOT NULL THEN 1 ELSE 0 END) AS cap_n "
        "FROM run_usage_facts WHERE origin IN (?,?,?,?)",
        (ORIGIN_CODEX, ORIGIN_KIMI, ORIGIN_GROK, ORIGIN_QWEN),
    ).fetchone()
    return {
        "rows": int(row["n"] or 0),
        "input_tokens": int(row["in_tok"] or 0),
        "output_tokens": int(row["out_tok"] or 0),
        "cache_read_tokens": int(row["cache_tok"] or 0),
        "reasoning_tokens": int(row["reason_tok"] or 0),
        "duration_ms": float(row["dur"] or 0),
        "task_run_id_set": int(row["task_n"] or 0),
        "first_call_at_set": int(row["first_n"] or 0),
        "last_call_at_set": int(row["last_n"] or 0),
        "captured_at_set": int(row["cap_n"] or 0),
    }


def apply_handle_session_map(
    *,
    db_path: Path | str,
    handle_to_session: Mapping[str, str],
    correlation_source: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply session_id fills from a handle→claude_session map.

    Only rows with ``session_id IS NULL`` are candidates. Already-set values
    are never overwritten (COALESCE / WHERE guard). Token, time, and
    ``task_run_id`` columns are not part of the UPDATE.
    """
    resolved = usage_facts_db_path(db_path)
    if not resolved.is_file():
        return {
            "applied": apply,
            "correlation_source": correlation_source,
            "handles": len(handle_to_session),
            "rows_to_fill": 0,
            "rows_filled": 0,
            "rows_already_set": 0,
            "handles_with_rows": 0,
            "handles_without_rows": 0,
            "before": None,
            "after": None,
        }

    if apply:
        initialize_usage_facts_db(resolved)

    conn = _connect_usage_db(resolved, readonly=not apply)
    try:
        before = _token_checksum(conn)
        updates: list[tuple[str, str, str]] = []  # (session_id, source, run_id)
        rows_already_set = 0
        handles_with_rows = 0
        handles_without_rows = 0

        for handle, session_id in handle_to_session.items():
            if not _is_valid_handle(handle) or not session_id:
                continue
            run_ids = find_run_ids_for_handle(conn, handle)
            if not run_ids:
                handles_without_rows += 1
                continue
            handles_with_rows += 1
            for run_id in run_ids:
                row = conn.execute(
                    "SELECT session_id FROM run_usage_facts WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    continue
                existing = row["session_id"]
                if existing is not None and str(existing).strip() != "":
                    rows_already_set += 1
                    continue
                updates.append((session_id, correlation_source, run_id))

        rows_to_fill = len(updates)
        rows_filled = 0
        if apply and updates:
            for session_id, source, run_id in updates:
                cur = conn.execute(
                    "UPDATE run_usage_facts SET "
                    "session_id=COALESCE(session_id, ?), "
                    "correlation_source=COALESCE(correlation_source, ?) "
                    "WHERE run_id=? AND session_id IS NULL",
                    (session_id, source, run_id),
                )
                rows_filled += max(0, int(cur.rowcount))
            conn.commit()
        after = _token_checksum(conn) if apply else before
        return {
            "applied": apply,
            "correlation_source": correlation_source,
            "handles": len(handle_to_session),
            "rows_to_fill": rows_to_fill,
            "rows_filled": rows_filled if apply else 0,
            "rows_already_set": rows_already_set,
            "handles_with_rows": handles_with_rows,
            "handles_without_rows": handles_without_rows,
            "before": before,
            "after": after,
        }
    finally:
        conn.close()


def correlate_from_run_metas(
    *,
    db_path: Path | str | None = None,
    runs_root: Path | str = DEFAULT_RUNS_ROOT,
    apply: bool = False,
    local_tz: Optional[Any] = None,
) -> dict[str, Any]:
    """Correlate usage rows via ``meta.claude_session_id`` (preview default).

    Metas **without** ``claude_session_id`` produce no assignment (no guessing).
    ``resume_handle=none`` is counted as unassignable, not silently dropped.
    """
    resolved = usage_facts_db_path(db_path)
    metas = iter_run_metas(runs_root, local_tz=local_tz)
    built = build_handle_to_claude_from_metas(metas)
    apply_stats = apply_handle_session_map(
        db_path=resolved,
        handle_to_session=built["handle_to_session"],
        correlation_source=CORRELATION_SOURCE_META,
        apply=apply,
    )
    return {
        "mode": "foreign_run_meta",
        "runs_root": str(Path(runs_root)),
        "metas_scanned": len(metas),
        "metas_with_claude": built["metas_with_claude"],
        "metas_without_claude": built["metas_without_claude"],
        "unassignable_no_handle": built["unassignable_no_handle"],
        "ambiguous_handles": built["ambiguous_handles"],
        "unique_handles": built["unique_handles"],
        **apply_stats,
    }


def parse_foreign_spawn_command(command: str) -> Optional[dict[str, str]]:
    """Extract lane + slug from a Bash command that invokes foreign.sh.

    Respects ``--slug`` when present (overrides brief basename), matching
    foreign.sh directory naming.
    """
    if not command or "foreign.sh" not in command:
        return None
    # Skip inspections of foreign.sh itself (cat/rg without a real spawn).
    if "--brief" not in command and "--slug" not in command:
        return None
    m = _FOREIGN_SH_RE.search(command)
    if not m:
        return None
    lane = m.group("lane").lower()
    rest = m.group("rest") or ""
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    brief: Optional[str] = None
    slug_arg: Optional[str] = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--brief" and i + 1 < len(tokens):
            brief = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--brief="):
            brief = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--slug" and i + 1 < len(tokens):
            slug_arg = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--slug="):
            slug_arg = tok.split("=", 1)[1]
            i += 1
            continue
        i += 1
    if slug_arg:
        slug = _slugify_foreign(slug_arg)
    elif brief:
        slug = _slugify_foreign(brief)
    else:
        return None
    if not slug:
        return None
    return {"lane": lane, "slug": slug, "brief": brief or ""}


def discover_foreign_spawns(
    projects_root: Path | str = DEFAULT_CLAUDE_PROJECTS,
) -> list[ForeignSpawnRecord]:
    """Scan Claude Code JSONL transcripts for foreign.sh Bash tool_use records."""
    root = Path(projects_root)
    if not root.is_dir():
        return []
    spawns: list[ForeignSpawnRecord] = []
    try:
        paths = sorted(root.rglob("*.jsonl"))
    except OSError:
        return []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "foreign.sh" not in line:
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            msg = obj.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            session_id = obj.get("sessionId") or obj.get("session_id")
            ts_raw = obj.get("timestamp")
            if not isinstance(session_id, str) or not session_id.strip():
                continue
            if not isinstance(ts_raw, str) or not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_use":
                    continue
                inp = part.get("input")
                if not isinstance(inp, dict):
                    continue
                command = inp.get("command")
                if not isinstance(command, str):
                    continue
                parsed = parse_foreign_spawn_command(command)
                if parsed is None:
                    continue
                spawns.append(
                    ForeignSpawnRecord(
                        claude_session_id=session_id.strip(),
                        timestamp=ts.astimezone(timezone.utc),
                        lane=parsed["lane"],
                        slug=parsed["slug"],
                        brief_path=parsed.get("brief") or None,
                        source_path=str(path),
                    )
                )
    return spawns


def match_spawns_to_run_metas(
    spawns: Sequence[ForeignSpawnRecord],
    metas: Sequence[RunMetaRecord],
    *,
    window_before_s: int = SPAWN_MATCH_WINDOW_BEFORE_S,
    window_after_s: int = SPAWN_MATCH_WINDOW_AFTER_S,
) -> dict[str, Any]:
    """Map spawns → run metas uniquely; ambiguities stay unassigned.

    Key: (lane, slug) + time window around spawn timestamp.
    If two run dirs match, **neither** is assigned.
    """
    by_lane: dict[str, dict[str, int]] = {}
    handle_sessions: dict[str, set[str]] = {}
    matched = 0
    ambiguous = 0
    no_candidate = 0
    no_handle = 0

    for sp in spawns:
        lane_stats = by_lane.setdefault(
            sp.lane,
            {
                "spawns": 0,
                "matched": 0,
                "ambiguous": 0,
                "no_candidate": 0,
                "no_handle": 0,
            },
        )
        lane_stats["spawns"] += 1
        cands: list[RunMetaRecord] = []
        for meta in metas:
            if meta.lane != sp.lane or meta.slug != sp.slug:
                continue
            if meta.started_at is None:
                continue
            delta = (meta.started_at - sp.timestamp).total_seconds()
            if -window_before_s <= delta <= window_after_s:
                cands.append(meta)
        if len(cands) == 0:
            no_candidate += 1
            lane_stats["no_candidate"] += 1
            continue
        if len(cands) > 1:
            ambiguous += 1
            lane_stats["ambiguous"] += 1
            continue
        meta = cands[0]
        if not _is_valid_handle(meta.resume_handle):
            no_handle += 1
            lane_stats["no_handle"] += 1
            continue
        handle = str(meta.resume_handle).strip()
        handle_sessions.setdefault(handle, set()).add(sp.claude_session_id)
        matched += 1
        lane_stats["matched"] += 1

    mapping: dict[str, str] = {}
    ambiguous_handles = 0
    for handle, sessions in handle_sessions.items():
        if len(sessions) == 1:
            mapping[handle] = next(iter(sessions))
        else:
            ambiguous_handles += 1

    return {
        "handle_to_session": mapping,
        "matched_spawns": matched,
        "ambiguous_spawns": ambiguous,
        "no_candidate_spawns": no_candidate,
        "no_handle_spawns": no_handle,
        "ambiguous_handles": ambiguous_handles,
        "by_lane": by_lane,
    }


def backfill_session_from_transcripts(
    *,
    db_path: Path | str | None = None,
    runs_root: Path | str = DEFAULT_RUNS_ROOT,
    projects_root: Path | str = DEFAULT_CLAUDE_PROJECTS,
    apply: bool = False,
    local_tz: Optional[Any] = None,
    window_before_s: int = SPAWN_MATCH_WINDOW_BEFORE_S,
    window_after_s: int = SPAWN_MATCH_WINDOW_AFTER_S,
) -> dict[str, Any]:
    """Preview/apply session_id backfill via transcript foreign.sh spawns.

    Preview (default) writes nothing. Apply fills only NULL ``session_id`` /
    ``correlation_source`` with source ``foreign_transcript_spawn``.
    """
    resolved = usage_facts_db_path(db_path)
    metas = iter_run_metas(runs_root, local_tz=local_tz)
    spawns = discover_foreign_spawns(projects_root)
    matched = match_spawns_to_run_metas(
        spawns,
        metas,
        window_before_s=window_before_s,
        window_after_s=window_after_s,
    )
    apply_stats = apply_handle_session_map(
        db_path=resolved,
        handle_to_session=matched["handle_to_session"],
        correlation_source=CORRELATION_SOURCE_TRANSCRIPT,
        apply=apply,
    )
    return {
        "mode": "foreign_transcript_spawn",
        "runs_root": str(Path(runs_root)),
        "projects_root": str(Path(projects_root)),
        "metas_scanned": len(metas),
        "spawns_scanned": len(spawns),
        "matched_spawns": matched["matched_spawns"],
        "ambiguous_spawns": matched["ambiguous_spawns"],
        "no_candidate_spawns": matched["no_candidate_spawns"],
        "no_handle_spawns": matched["no_handle_spawns"],
        "ambiguous_handles": matched["ambiguous_handles"],
        "unique_handles": len(matched["handle_to_session"]),
        "by_lane": matched["by_lane"],
        **apply_stats,
    }


def correlate_foreign_sessions(
    *,
    db_path: Path | str | None = None,
    runs_root: Path | str = DEFAULT_RUNS_ROOT,
    projects_root: Path | str = DEFAULT_CLAUDE_PROJECTS,
    apply: bool = False,
    include_transcript_backfill: bool = False,
    local_tz: Optional[Any] = None,
) -> dict[str, Any]:
    """Run meta correlation; optionally transcript backfill. Preview default."""
    meta_report = correlate_from_run_metas(
        db_path=db_path,
        runs_root=runs_root,
        apply=apply,
        local_tz=local_tz,
    )
    report: dict[str, Any] = {"meta": meta_report}
    if include_transcript_backfill:
        report["transcript"] = backfill_session_from_transcripts(
            db_path=db_path,
            runs_root=runs_root,
            projects_root=projects_root,
            apply=apply,
            local_tz=local_tz,
        )
    return report

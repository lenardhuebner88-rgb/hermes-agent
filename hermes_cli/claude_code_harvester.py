"""ETL harvester: Claude Code transcript JSONL → usage facts.

Streams files under ``~/.claude/projects`` (never loads a whole 1.9 GB tree into
memory).  Idempotency is global on ``(message.id, requestId)`` so ``--resume``
copies of the same assistant turn collapse to one fact row.

This module is fork-owned and does not alter the usage-facts schema.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence, TextIO
from urllib.parse import quote

from hermes_cli import usage_facts_db as usage_facts_db_mod
from hermes_cli.usage_facts_db import (
    initialize_usage_facts_db,
    usage_facts_db_path,
)

# Bump when the transcript shape we accept changes in a way that would make
# golden fixtures silently wrong.  Tests pin this value.
#
# v4 persists the transcript session identity and exact Kanban correlations.
# v5 fixes exact subagent attribution: a sidechain can carry its own sessionId,
# while the Kanban run records the parent session named by the transcript path.
# The format bump intentionally re-harvests captured files so those proven
# parent-session links replace previously unresolved rows.
# v6 stores every observed parent-binding field independently and restores
# ``model_source`` to the provenance of the model name itself.
# v7 stores Anthropic's observed cache-write split by 1h and 5m lifetime.
# v8 stores the observed tool name and output size per run/tool aggregate.
CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION = 8

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
ORIGIN = "claude_code"
PROVIDER = "anthropic"
NO_REQUEST_ID = "__no_request_id__"
RUN_ID_PREFIX = "claude_code"
_EXACT_RUN_CORRELATION_SOURCES = frozenset(
    {
        "claude_session_id_run",
        "claude_parent_session_id_run",
        "claude_worktree_path_run",
    }
)
_TASK_CORRELATION_SOURCES = frozenset(
    {
        "claude_session_id_task",
        "claude_parent_session_id_task",
    }
)

# Token fields we accept from message.usage (numbers only — not cache_creation).
_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

UNKNOWN_BILLING_MODE = "unknown"
_EXPLICIT_BILLING_MODE_KEYS = ("billing_mode", "billingMode")
_BILLING_MODE_SOURCE_TRANSCRIPT = "transcript"
_BILLING_MODE_SOURCE_ACCESS_CONFIGURATION = "access_configuration"


@dataclass
class HarvestStats:
    """Counters for one harvest run."""

    files_seen: int = 0
    files_skipped_hwm: int = 0
    files_processed: int = 0
    lines_seen: int = 0
    lines_skipped: int = 0
    calls_merged: int = 0
    calls_written: int = 0
    calls_unchanged: int = 0
    parse_errors: int = 0
    sessions_correlated: int = 0
    calls_correlated: int = 0
    calls_recorrelated: int = 0
    format_version: int = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION


@dataclass
class _CallDraft:
    """In-memory merge state for one (message_id, request_id) call."""

    message_id: str
    request_id: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    service_tier: Optional[str] = None
    billing_mode: str = UNKNOWN_BILLING_MODE
    billing_mode_source: Optional[str] = None
    effort: Optional[str] = None
    timestamp: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cache_write_1h_tokens: Optional[int] = None
    cache_write_5m_tokens: Optional[int] = None
    tool_use_ids: set[str] = field(default_factory=set)
    tool_names_by_use_id: dict[str, str] = field(default_factory=dict)
    tool_output_chars_by_use_id: dict[str, int] = field(default_factory=dict)
    tool_output_chars: int = 0
    call_kind: str = "main"
    profile: Optional[str] = None
    parent_session_id: Optional[str] = None
    parent_tool_use_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    spawn_depth: Optional[int] = None
    source_path: Optional[str] = None
    task_run_id: Optional[str] = None
    task_id: Optional[str] = None
    chain_id: Optional[str] = None
    board: Optional[str] = None
    correlation_source: Optional[str] = None
    fragment_count: int = 0

    @property
    def run_id(self) -> str:
        return make_run_id(self.message_id, self.request_id)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_use_ids)


def make_run_id(message_id: str, request_id: Optional[str]) -> str:
    """Stable global identity for one LLM call across sessions/files."""
    rid = (request_id or "").strip() or NO_REQUEST_ID
    mid = (message_id or "").strip()
    if not mid:
        raise ValueError("message_id must be non-empty")
    return f"{RUN_ID_PREFIX}:{mid}:{rid}"


def request_id_or_fallback(request_id: Any) -> str:
    """Map a missing/blank requestId to the defined fallback token."""
    if request_id is None:
        return NO_REQUEST_ID
    text = str(request_id).strip()
    return text if text else NO_REQUEST_ID


def _draft_correlation(
    draft: _CallDraft,
    correlations: Mapping[str, Any],
) -> tuple[Any, str] | None:
    """Return one exact, internally consistent correlation for ``draft``.

    Claude subagent transcripts are stored below the parent session directory,
    but newer sidechains can expose a distinct child ``sessionId``.  The path
    identity is therefore the exact worker lineage key.  If both identities
    resolve and disagree, refusing the join is safer than charging either run.
    """
    candidates: list[tuple[str, bool]] = []
    if draft.call_kind == "subagent" and draft.parent_session_id:
        candidates.append((draft.parent_session_id, True))
    if draft.session_id:
        candidates.append((draft.session_id, False))
    if draft.call_kind != "subagent" and draft.parent_session_id:
        candidates.append((draft.parent_session_id, True))

    matches: list[tuple[Any, bool]] = []
    seen: set[str] = set()
    for session_id, is_parent in candidates:
        if session_id in seen:
            continue
        seen.add(session_id)
        correlation = correlations.get(session_id)
        if correlation is not None:
            matches.append((correlation, is_parent))
    if not matches:
        return None

    task_boards = {(item.task_id, item.board) for item, _ in matches}
    exact_run_ids = {
        item.task_run_id for item, _ in matches if item.task_run_id is not None
    }
    chain_ids = {item.chain_id for item, _ in matches if item.chain_id is not None}
    if (
        len(task_boards) != 1
        or len(exact_run_ids) > 1
        or len(chain_ids) > 1
    ):
        return None

    exact_matches = [
        match for match in matches if match[0].task_run_id is not None
    ]
    correlation, is_parent = (exact_matches or matches)[0]
    source = correlation.source
    if is_parent:
        source = source.replace(
            "claude_session_id_",
            "claude_parent_session_id_",
            1,
        )
    return correlation, source


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _billing_mode_from_raw(
    record: Mapping[str, Any], message: Mapping[str, Any]
) -> tuple[str, bool]:
    """Return only explicit transcript billing metadata, never an inference.

    The currently measured transcript shape has ``userType`` and usage
    ``service_tier`` but neither identifies how a request was billed. Those
    fields therefore must not be mapped to a billing mode. If a transcript
    supplies one of the direct billing-mode fields, preserve its non-blank
    value; otherwise make the unknown observation explicit.
    """
    for source in (record, message):
        for key in _EXPLICIT_BILLING_MODE_KEYS:
            mode = _text(source.get(key))
            if mode is not None:
                return mode, True
    return UNKNOWN_BILLING_MODE, False


def _subscription_plan_label() -> Optional[str]:
    """Resolve the safe plan label without exposing OAuth credential fields."""
    try:
        from agent.account_usage import _resolve_anthropic_plan_label

        return _resolve_anthropic_plan_label()
    except Exception:
        return None


def _configuration_mentions_key(path: Path, keys: set[str]) -> bool:
    """Check only configuration key names; never parse or retain their values."""
    key_pattern = re.compile(
        r"(?<![A-Za-z0-9_])(?:"
        + "|".join(map(re.escape, keys))
        + r")(?![A-Za-z0-9_])\s*[:=]",
        re.IGNORECASE,
    )
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if key_pattern.search(line):
                    return True
    except OSError:
        return False
    return False


def _has_configured_metered_access(
    *,
    environ: Optional[Mapping[str, str]] = None,
    config_paths: Optional[Iterable[Path]] = None,
) -> bool:
    """Return whether a metered API-key route is configured, without reading it.

    An OAuth subscription is not enough to classify transcript rows when a
    metered route is also configured.  Presence checks deliberately use mapping
    membership and configuration key names, never credential values.
    """
    key_names = {f"{PROVIDER.upper()}_API_KEY"}
    environment = os.environ if environ is None else environ
    if any(key in environment for key in key_names):
        return True
    paths = config_paths
    if paths is None:
        home = Path.home()
        paths = (
            home / ".hermes" / "config.yaml",
            home / ".claude" / "settings.json",
        )
    return any(_configuration_mentions_key(Path(path), key_names) for path in paths)


def _billing_mode_from_access_configuration() -> tuple[str, Optional[str]]:
    """Infer billing only when subscription and exclusive access are proven."""
    if _subscription_plan_label() is None or _has_configured_metered_access():
        return UNKNOWN_BILLING_MODE, None
    return "subscription_included", _BILLING_MODE_SOURCE_ACCESS_CONFIGURATION


def _content_parts(message: Mapping[str, Any]) -> list[Any]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return []


def _tool_uses_from_content(
    content: Iterable[Any],
) -> tuple[set[str], dict[str, str]]:
    tool_ids: set[str] = set()
    tool_uses: dict[str, str] = {}
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") != "tool_use":
            continue
        tool_id = _text(part.get("id"))
        tool_name = _text(part.get("name"))
        if tool_id is None:
            continue
        tool_ids.add(tool_id)
        if tool_name is not None:
            tool_uses[tool_id] = tool_name
    return tool_ids, tool_uses


def _tool_result_chars(part: Mapping[str, Any]) -> int:
    content = part.get("content")
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(content))


def _apply_usage(draft: _CallDraft, usage: Mapping[str, Any]) -> None:
    """Last non-null wins per field — never sum streaming duplicates."""
    for src, dest in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cache_read_tokens"),
        ("cache_creation_input_tokens", "cache_write_tokens"),
    ):
        value = _int(usage.get(src))
        if value is not None:
            setattr(draft, dest, value)
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, Mapping):
        for src, dest in (
            ("ephemeral_1h_input_tokens", "cache_write_1h_tokens"),
            ("ephemeral_5m_input_tokens", "cache_write_5m_tokens"),
        ):
            value = _int(cache_creation.get(src))
            if value is not None:
                setattr(draft, dest, value)
    tier = _text(usage.get("service_tier"))
    if tier is not None:
        draft.service_tier = tier


def merge_assistant_fragment(
    draft: Optional[_CallDraft],
    record: Mapping[str, Any],
    *,
    source_path: Optional[str] = None,
    call_kind: str = "main",
    profile: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    parent_tool_use_id: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
    spawn_depth: Optional[int] = None,
) -> Optional[_CallDraft]:
    """Merge one assistant JSONL record into a call draft.

    Streaming turns appear as multiple lines with the same ``message.id`` (and
    usually the same ``requestId``).  Usage tokens are taken once (last non-null
    per field); tool_use ids are unioned.
    """
    if record.get("type") != "assistant":
        return draft
    message = record.get("message")
    if not isinstance(message, Mapping):
        return draft
    message_id = _text(message.get("id"))
    if message_id is None:
        return draft

    request_id = request_id_or_fallback(record.get("requestId"))
    if draft is None:
        draft = _CallDraft(message_id=message_id, request_id=request_id)
    elif draft.message_id != message_id or draft.request_id != request_id:
        raise ValueError(
            "fragment key mismatch: "
            f"{draft.message_id!r}/{draft.request_id!r} vs "
            f"{message_id!r}/{request_id!r}"
        )

    draft.fragment_count += 1
    draft.session_id = _text(record.get("sessionId")) or draft.session_id
    draft.timestamp = _text(record.get("timestamp")) or draft.timestamp
    draft.effort = _text(record.get("effort")) or draft.effort
    draft.model = _text(message.get("model")) or draft.model
    draft.stop_reason = _text(message.get("stop_reason")) or draft.stop_reason
    billing_mode, billing_mode_explicit = _billing_mode_from_raw(record, message)
    if billing_mode_explicit:
        # Streaming fragments may only disclose this field once; preserve the
        # last explicit source value instead of overwriting it with an absence.
        draft.billing_mode = billing_mode
        draft.billing_mode_source = _BILLING_MODE_SOURCE_TRANSCRIPT
    draft.source_path = source_path or draft.source_path
    draft.call_kind = call_kind or draft.call_kind
    draft.profile = profile if profile is not None else draft.profile
    draft.parent_session_id = parent_session_id or draft.parent_session_id
    draft.parent_tool_use_id = parent_tool_use_id or draft.parent_tool_use_id
    draft.parent_agent_id = parent_agent_id or draft.parent_agent_id
    if spawn_depth is not None:
        draft.spawn_depth = spawn_depth

    usage = message.get("usage")
    if isinstance(usage, Mapping):
        _apply_usage(draft, usage)

    observed_tool_ids, observed_tool_uses = _tool_uses_from_content(
        _content_parts(message)
    )
    draft.tool_use_ids.update(observed_tool_ids)
    draft.tool_names_by_use_id.update(observed_tool_uses)
    return draft


def load_agent_meta(meta_path: Path) -> dict[str, Any]:
    """Load a ``.meta.json`` file; missing/optional keys become absences.

    Only ``agentType`` is guaranteed across the measured corpus.  Every other
    key may be missing — callers must tolerate NULL.
    """
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def discover_jsonl_files(projects_root: Path) -> list[Path]:
    """Return all transcript ``.jsonl`` files under the projects root."""
    root = Path(projects_root)
    if not root.is_dir():
        return []
    # Sorted for deterministic HWM / test order; os.walk streams the tree.
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl"):
                found.append(Path(dirpath) / name)
    found.sort()
    return found


def _session_context_for_path(path: Path, projects_root: Path) -> dict[str, Any]:
    """Derive main-vs-subagent context from the path and optional meta file."""
    path = path.resolve()
    parts = path.parts
    ctx: dict[str, Any] = {
        "call_kind": "main",
        "profile": None,
        "parent_session_id": None,
        "parent_tool_use_id": None,
        "parent_agent_id": None,
        "spawn_depth": None,
    }
    # .../<sessionId>/subagents/agent-*.jsonl
    if len(parts) >= 2 and parts[-2] == "subagents":
        ctx["call_kind"] = "subagent"
        ctx["parent_session_id"] = parts[-3] if len(parts) >= 3 else None
        meta_path = path.with_suffix("").with_suffix(".meta.json")
        # agent-xxx.jsonl → agent-xxx.meta.json (not agent-xxx.jsonl.meta.json)
        if path.name.endswith(".jsonl"):
            meta_path = path.parent / (path.name[: -len(".jsonl")] + ".meta.json")
        meta = load_agent_meta(meta_path) if meta_path.is_file() else {}
        ctx["profile"] = _text(meta.get("agentType"))
        ctx["parent_tool_use_id"] = _text(meta.get("toolUseId"))
        ctx["parent_agent_id"] = _text(meta.get("parentAgentId"))
        ctx["spawn_depth"] = _int(meta.get("spawnDepth"))
        # description / model / stoppedByUser remain unrelated to binding.
    return ctx


def iter_jsonl_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(line_no, obj)`` streaming one file; bad lines are skipped."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield line_no, obj


def parse_transcript_file(
    path: Path,
    *,
    projects_root: Path,
) -> tuple[dict[str, _CallDraft], HarvestStats]:
    """Parse one JSONL transcript into merged call drafts keyed by run_id."""
    stats = HarvestStats()
    ctx = _session_context_for_path(path, projects_root)
    drafts: dict[str, _CallDraft] = {}
    # tool_use_id → run_id for tool_result char attachment
    tool_owners: dict[str, str] = {}

    for _line_no, record in iter_jsonl_records(path):
        stats.lines_seen += 1
        record_type = record.get("type")
        if record_type is None:
            stats.lines_skipped += 1
            continue
        if record_type == "assistant":
            message = record.get("message")
            if not isinstance(message, Mapping) or _text(message.get("id")) is None:
                stats.lines_skipped += 1
                continue
            message_id = _text(message.get("id")) or ""
            request_id = request_id_or_fallback(record.get("requestId"))
            run_id = make_run_id(message_id, request_id)
            existing = drafts.get(run_id)
            before_fragments = existing.fragment_count if existing else 0
            try:
                draft = merge_assistant_fragment(
                    existing,
                    record,
                    source_path=str(path),
                    call_kind=str(ctx["call_kind"]),
                    profile=ctx.get("profile"),
                    parent_session_id=ctx.get("parent_session_id"),
                    parent_tool_use_id=ctx.get("parent_tool_use_id"),
                    parent_agent_id=ctx.get("parent_agent_id"),
                    spawn_depth=ctx.get("spawn_depth"),
                )
            except ValueError:
                stats.parse_errors += 1
                continue
            if draft is None:
                stats.lines_skipped += 1
                continue
            if before_fragments > 0 and draft.fragment_count > before_fragments:
                stats.calls_merged += 1
            drafts[run_id] = draft
            for tool_id in draft.tool_use_ids:
                tool_owners[tool_id] = run_id
            continue

        if record_type == "user":
            message = record.get("message")
            content: list[Any] = []
            if isinstance(message, Mapping):
                content = _content_parts(message)
            elif isinstance(message, list):
                content = message
            for part in content:
                if not isinstance(part, Mapping) or part.get("type") != "tool_result":
                    continue
                tool_id = _text(part.get("tool_use_id"))
                if tool_id is None:
                    continue
                owner = tool_owners.get(tool_id)
                if owner is None:
                    continue
                draft = drafts.get(owner)
                if draft is None:
                    continue
                draft.tool_output_chars_by_use_id[tool_id] = _tool_result_chars(
                    part
                )
                draft.tool_output_chars = sum(
                    draft.tool_output_chars_by_use_id.values()
                )
            continue

        # attachment, queue-operation, last-prompt, system, …
        stats.lines_skipped += 1

    return drafts, stats


def draft_to_fields(draft: _CallDraft) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map a merged draft to ``record_llm_call`` field dicts.

    Unavailable timings and context-window observations stay absent (NULL) —
    they are never estimated.
    """
    total = None
    if (
        draft.input_tokens is not None
        and draft.output_tokens is not None
        and draft.cache_read_tokens is not None
        and draft.cache_write_tokens is not None
    ):
        total = (
            draft.input_tokens
            + draft.output_tokens
            + draft.cache_read_tokens
            + draft.cache_write_tokens
        )

    lane = draft.session_id
    if draft.call_kind == "subagent" and draft.parent_session_id:
        lane = draft.parent_session_id

    call_fields: dict[str, Any] = {
        "origin": ORIGIN,
        "provider": PROVIDER,
        "model": draft.model,
        "requested_model": draft.model,
        "model_source": "transcript",
        "parent_tool_use_id": draft.parent_tool_use_id,
        "parent_agent_id": draft.parent_agent_id,
        "parent_session_id": draft.parent_session_id,
        "spawn_depth": draft.spawn_depth,
        "serving_tier": draft.service_tier,
        "reasoning_effort": draft.effort,
        "response_id": draft.message_id,
        "input_tokens": draft.input_tokens,
        "output_tokens": draft.output_tokens,
        "cache_read_tokens": draft.cache_read_tokens,
        "cache_write_tokens": draft.cache_write_tokens,
        "cache_write_1h_tokens": draft.cache_write_1h_tokens,
        "cache_write_5m_tokens": draft.cache_write_5m_tokens,
        "total_tokens": total,
        "finish_reason": draft.stop_reason,
        "tool_call_count": draft.tool_call_count,
        "tool_output_chars": draft.tool_output_chars or None,
        # Source event time from the transcript; NULL when the record has none.
        "occurred_at": draft.timestamp,
        # Explicitly unavailable in Claude Code transcripts — do not invent:
        # first_token_ms, duration_ms, context_window_used
    }
    run_fields: dict[str, Any] = {
        "origin": ORIGIN,
        "session_id": draft.session_id,
        "task_run_id": draft.task_run_id,
        "task_id": draft.task_id,
        "chain_id": draft.chain_id,
        "board": draft.board,
        "correlation_source": draft.correlation_source,
        "provider": PROVIDER,
        "model": draft.model,
        "requested_model": draft.model,
        "model_source": "transcript",
        "parent_tool_use_id": draft.parent_tool_use_id,
        "parent_agent_id": draft.parent_agent_id,
        "parent_session_id": draft.parent_session_id,
        "spawn_depth": draft.spawn_depth,
        "lane": lane,
        "profile": draft.profile,
        "call_kind": draft.call_kind,
        "serving_tier": draft.service_tier,
        "reasoning_effort": draft.effort,
        "billing_mode": draft.billing_mode,
        "billing_mode_source": draft.billing_mode_source,
        "source": "measured",
        "captured_at": draft.timestamp,
    }
    return call_fields, run_fields


@dataclass
class HighWaterMark:
    """Path → (mtime_ns, size) snapshot for incremental harvests."""

    entries: dict[str, list[int]] = field(default_factory=dict)
    format_version: int = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION

    @classmethod
    def load(cls, path: Path) -> "HighWaterMark":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        entries_raw = data.get("files") or {}
        entries: dict[str, list[int]] = {}
        if isinstance(entries_raw, dict):
            for key, value in entries_raw.items():
                if (
                    isinstance(value, (list, tuple))
                    and len(value) == 2
                    and all(isinstance(x, (int, float)) for x in value)
                ):
                    entries[str(key)] = [int(value[0]), int(value[1])]
        version = data.get("format_version", CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION)
        try:
            version_i = int(version)
        except (TypeError, ValueError):
            version_i = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION
        return cls(entries=entries, format_version=version_i)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION,
            "files": self.entries,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def fingerprint(self, path: Path) -> Optional[list[int]]:
        try:
            st = path.stat()
        except OSError:
            return None
        return [int(st.st_mtime_ns), int(st.st_size)]

    def is_unchanged(self, path: Path) -> bool:
        key = str(path)
        current = self.fingerprint(path)
        if current is None:
            return False
        return self.entries.get(key) == current

    def mark(self, path: Path) -> None:
        fp = self.fingerprint(path)
        if fp is not None:
            self.entries[str(path)] = fp


def write_call(
    draft: _CallDraft,
    *,
    db_path: Path,
    dry_run: bool = False,
) -> bool:
    """Persist one call (opens its own connection). Prefer ``write_calls_batch``."""
    if dry_run:
        return True
    with usage_facts_db_mod._connection(db_path) as conn:
        _record_llm_call_on_conn(conn, draft)
    return True


def _tool_call_rows(draft: _CallDraft) -> list[tuple[str, int, int | None]]:
    """Return per-name counts and only the output sizes actually observed."""
    counts = Counter(draft.tool_names_by_use_id.values())
    output_totals: Counter[str] = Counter()
    names_with_output: set[str] = set()
    for tool_id, output_chars in draft.tool_output_chars_by_use_id.items():
        tool_name = draft.tool_names_by_use_id.get(tool_id)
        if tool_name is None:
            continue
        output_totals[tool_name] += output_chars
        names_with_output.add(tool_name)
    return [
        (
            tool_name,
            call_count,
            output_totals[tool_name] if tool_name in names_with_output else None,
        )
        for tool_name, call_count in sorted(counts.items())
    ]


def _record_llm_call_on_conn(
    conn: Any,
    draft: _CallDraft,
) -> None:
    """Write one call on an open connection (batch path)."""
    call_fields, run_fields = draft_to_fields(draft)
    run_id = draft.run_id
    call_index = 0
    values = usage_facts_db_mod._clean_fields(
        call_fields, usage_facts_db_mod.LLM_CALL_COLUMNS
    )
    if "origin" in values:
        values["origin"] = usage_facts_db_mod._origin(values["origin"])
    columns = ["run_id", "call_index", *values]
    params = [run_id, call_index, *values.values()]
    updates = [
        f"{column}=COALESCE(excluded.{column}, run_llm_calls.{column})"
        for column in values
    ]
    placeholders = ", ".join("?" for _ in columns)
    conflict = (
        f"DO UPDATE SET {', '.join(updates)}" if updates else "DO NOTHING"
    )
    usage_facts_db_mod._upsert_run_facts(conn, run_id, run_fields)
    conn.execute(
        f"""
        INSERT INTO run_llm_calls ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(run_id, call_index) {conflict}
        """,
        params,
    )
    for tool_name, call_count, output_chars in _tool_call_rows(draft):
        usage_facts_db_mod._record_tool_call_on_conn(
            conn,
            run_id,
            tool_name,
            call_count,
            output_chars,
        )
    usage_facts_db_mod._refresh_run_aggregates(conn, run_id)


def write_calls_batch(
    drafts: Iterable[_CallDraft],
    *,
    db_path: Path,
    dry_run: bool = False,
    update_existing_only: bool = False,
) -> int:
    """Persist many calls in one SQLite transaction. Returns write count.

    ``update_existing_only`` is an explicit backfill mode: it can update only
    Claude fact identities already present in the target DB snapshot. It never
    creates rows for newly discovered raw transcript records.
    """
    items = list(drafts)
    if not items:
        return 0
    if dry_run:
        return len(items)
    with usage_facts_db_mod._connection(db_path) as conn:
        written = 0
        for draft in items:
            if update_existing_only:
                existing = conn.execute(
                    "SELECT 1 FROM run_usage_facts "
                    "WHERE run_id=? AND origin=?",
                    (draft.run_id, ORIGIN),
                ).fetchone()
                if existing is None:
                    continue
            _record_llm_call_on_conn(conn, draft)
            written += 1
    return written


def recorrelate_existing_calls(
    correlations: Mapping[str, Any],
    *,
    db_path: Path,
    dry_run: bool,
    ambiguous_session_ids: Optional[Iterable[str]] = None,
) -> int:
    """Reconcile exact links for HWM-skipped transcripts.

    Only sessions proven ambiguous by readable board rows are cleared. Missing
    correlations are unresolved, not negative evidence. Resolved task-level
    links enrich NULL fields but never downgrade a previously exact run link.
    """
    ambiguous_sessions = {
        str(value).strip()
        for value in (ambiguous_session_ids or ())
        if str(value).strip()
    }
    if not correlations and not ambiguous_sessions:
        return 0
    if not db_path.is_file():
        return 0
    resolved = db_path.expanduser().resolve()
    if dry_run:
        try:
            connection = sqlite3.connect(
                f"file:{quote(str(resolved), safe='/')}?mode=ro",
                uri=True,
                timeout=2.0,
            )
        except sqlite3.Error:
            return 0
    else:
        connection = usage_facts_db_mod._connect(resolved)
    connection.row_factory = sqlite3.Row
    try:
        updated = 0
        sorted_ambiguous = sorted(ambiguous_sessions)
        for offset in range(0, len(sorted_ambiguous), 500):
            chunk = sorted_ambiguous[offset : offset + 500]
            placeholders = ", ".join("?" for _ in chunk)
            ambiguous_predicate = (
                "((session_id IN ("
                f"{placeholders}) "
                "AND (correlation_source LIKE 'claude_session_id_%' "
                "OR correlation_source='claude_worktree_path_run')) "
                "OR (call_kind='subagent' AND lane IN ("
                f"{placeholders}) "
                "AND (correlation_source LIKE "
                "'claude_parent_session_id_%' OR "
                "correlation_source='claude_worktree_path_run')))"
            )
            ambiguous_params = (ORIGIN, *chunk, *chunk)
            if dry_run:
                row = connection.execute(
                    "SELECT COUNT(*) FROM run_usage_facts "
                    f"WHERE origin=? AND {ambiguous_predicate}",
                    ambiguous_params,
                ).fetchone()
                updated += int(row[0]) if row is not None else 0
                continue
            cursor = connection.execute(
                "UPDATE run_usage_facts SET task_run_id=NULL, task_id=NULL, "
                "chain_id=NULL, board=NULL, correlation_source=NULL "
                f"WHERE origin=? AND {ambiguous_predicate}",
                ambiguous_params,
            )
            updated += max(0, int(cursor.rowcount))

        correlation_ids = sorted(str(value) for value in correlations)
        for offset in range(0, len(correlation_ids), 250):
            chunk = correlation_ids[offset : offset + 250]
            placeholders = ", ".join("?" for _ in chunk)
            try:
                candidates = connection.execute(
                    "SELECT run_id, session_id, lane, call_kind, "
                    "task_run_id, task_id, chain_id, board, profile, "
                    "correlation_source FROM run_usage_facts "
                    "WHERE origin=? AND (session_id IN ("
                    f"{placeholders}) OR (call_kind='subagent' AND lane IN ("
                    f"{placeholders}))) ORDER BY run_id",
                    (ORIGIN, *chunk, *chunk),
                ).fetchall()
            except sqlite3.Error:
                return 0
            for row in candidates:
                call_kind = _text(row["call_kind"]) or "main"
                draft = _CallDraft(
                    message_id=str(row["run_id"]),
                    request_id=NO_REQUEST_ID,
                    session_id=_text(row["session_id"]),
                    call_kind=call_kind,
                    parent_session_id=(
                        _text(row["lane"])
                        if call_kind == "subagent"
                        else None
                    ),
                )
                resolved_match = _draft_correlation(draft, correlations)
                if resolved_match is None:
                    # Parent and child correlations that disagree are not exact.
                    continue
                correlation, source = resolved_match
                fields = correlation.as_run_fields()
                existing = {
                    key: row[key]
                    for key in (
                        "task_run_id",
                        "task_id",
                        "chain_id",
                        "board",
                        "profile",
                        "correlation_source",
                    )
                }
                resolved = {
                    "task_run_id": existing["task_run_id"]
                    or fields.get("task_run_id"),
                    "task_id": existing["task_id"] or fields.get("task_id"),
                    "chain_id": existing["chain_id"]
                    or fields.get("chain_id"),
                    "board": existing["board"] or fields.get("board"),
                    "profile": existing["profile"] or fields.get("profile"),
                    "correlation_source": source,
                }
                if (
                    fields.get("task_run_id") is None
                    and existing["task_run_id"] is not None
                ):
                    # Task-level evidence may enrich NULLs but never downgrade
                    # an already exact run-level attribution.
                    resolved["correlation_source"] = existing[
                        "correlation_source"
                    ]
                if all(existing[key] == resolved[key] for key in existing):
                    continue
                updated += 1
                if dry_run:
                    continue
                connection.execute(
                    "UPDATE run_usage_facts SET task_run_id=?, task_id=?, "
                    "chain_id=?, board=?, profile=?, correlation_source=? "
                    "WHERE origin=? AND run_id=?",
                    (
                        resolved["task_run_id"],
                        resolved["task_id"],
                        resolved["chain_id"],
                        resolved["board"],
                        resolved["profile"],
                        resolved["correlation_source"],
                        ORIGIN,
                        row["run_id"],
                    ),
                )
        if not dry_run:
            connection.commit()
        return updated
    finally:
        connection.close()


def _existing_claude_correlation_state(db_path: Path) -> dict[str, str | None]:
    """Return one correlation state per stored Claude session, without contents."""
    resolved = db_path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    try:
        rows = connection.execute(
            "SELECT CASE "
            "WHEN (correlation_source LIKE 'claude_parent_session_id_%' "
            "OR (correlation_source='claude_worktree_path_run' "
            "AND call_kind='subagent')) "
            "AND lane IS NOT NULL AND TRIM(lane) != '' THEN lane "
            "ELSE session_id END AS correlation_session_id, "
            "correlation_source FROM run_usage_facts "
            "WHERE origin=? AND session_id IS NOT NULL AND TRIM(session_id) != ''",
            (ORIGIN,),
        )
        states: dict[str, str | None] = {}
        for session_id, source in rows:
            key = str(session_id).strip()
            value = str(source).strip() if source is not None else None
            previous = states.get(key)
            if value in _EXACT_RUN_CORRELATION_SOURCES or previous is None:
                states[key] = value
        return states
    finally:
        connection.close()


def _backfill_snapshot(
    states: Mapping[str, str | None],
    *,
    correlations: Mapping[str, Any],
    ambiguous_session_ids: Iterable[str],
    projected: bool,
) -> dict[str, int]:
    candidate_ids = set(states)
    ambiguous = candidate_ids & set(ambiguous_session_ids)
    if projected:
        projected_states = dict(states)
        for session_id in ambiguous:
            projected_states[session_id] = None
        for session_id, correlation in correlations.items():
            if session_id not in candidate_ids:
                continue
            if (
                projected_states.get(session_id)
                in _EXACT_RUN_CORRELATION_SOURCES
                and correlation.task_run_id is None
            ):
                continue
            projected_states[session_id] = correlation.source
        exact_run = {
            session_id
            for session_id, source in projected_states.items()
            if source in _EXACT_RUN_CORRELATION_SOURCES
        }
        task_only = {
            session_id
            for session_id, source in projected_states.items()
            if source in _TASK_CORRELATION_SOURCES
        }
    else:
        exact_run = {
            session_id
            for session_id, source in states.items()
            if source in _EXACT_RUN_CORRELATION_SOURCES
        }
        task_only = {
            session_id
            for session_id, source in states.items()
            if source in _TASK_CORRELATION_SOURCES
        }
    return {
        "candidate_sessions": len(candidate_ids),
        "exact_run_links": len(exact_run),
        "task_only_links": len(task_only),
        "ambiguous_sessions": len(ambiguous),
        "unresolved_sessions": len(candidate_ids - exact_run - task_only),
    }


def backfill_existing_correlations(
    *,
    db_path: Path | str | None = None,
    kanban_paths: Optional[Sequence[Path | str]] = None,
    projects_root: Path | str = DEFAULT_PROJECTS_ROOT,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply exact session/path joins to existing Claude facts.

    This explicit path reads transcript filenames, never their contents. Its
    report contains only aggregate counts and board status/error classes.
    """
    from hermes_cli.usage_fact_correlation import scan_claude_session_correlations

    resolved_db = usage_facts_db_path(db_path)
    states = _existing_claude_correlation_state(resolved_db)
    transcript_paths = discover_jsonl_files(Path(projects_root))
    scan = scan_claude_session_correlations(
        states,
        kanban_paths=kanban_paths,
        transcript_paths=transcript_paths,
    )
    before = _backfill_snapshot(
        states,
        correlations=scan.correlations,
        ambiguous_session_ids=scan.ambiguous_session_ids,
        projected=False,
    )
    updated = recorrelate_existing_calls(
        scan.correlations,
        db_path=resolved_db,
        dry_run=not apply,
        ambiguous_session_ids=scan.ambiguous_session_ids,
    )
    after = _backfill_snapshot(
        states,
        correlations=scan.correlations,
        ambiguous_session_ids=scan.ambiguous_session_ids,
        projected=True,
    )
    return {
        "applied": apply,
        "before": before,
        "after": after,
        "updated_facts": updated,
        "board_databases": [result.as_dict() for result in scan.database_results],
    }


def _transcript_occurred_at_by_run_id(
    projects_root: Path,
) -> dict[str, str]:
    """Map run_id → transcript event time for Claude assistant turns.

    Only non-empty source timestamps are returned. Absence stays absent —
    never filled from mtime or wall clock.
    """
    root = Path(projects_root)
    timestamps: dict[str, str] = {}
    for path in discover_jsonl_files(root):
        drafts, _stats = parse_transcript_file(path, projects_root=root)
        for run_id, draft in drafts.items():
            if draft.timestamp:
                timestamps[run_id] = draft.timestamp
    return timestamps


def backfill_occurred_at(
    *,
    db_path: Path | str | None = None,
    projects_root: Path | str = DEFAULT_PROJECTS_ROOT,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply source-event timestamps for existing Claude facts.

    Fills only NULL time columns (``run_llm_calls.occurred_at``,
    ``run_usage_facts.first_call_at`` / ``last_call_at``). Never touches
    ``captured_at``, tokens, or correlation fields. Preview is the default.
    """
    resolved_db = usage_facts_db_path(db_path)
    if not resolved_db.is_file():
        return {
            "applied": apply,
            "calls_to_fill": 0,
            "runs_to_fill": 0,
            "calls_filled": 0,
            "runs_filled": 0,
        }
    initialize_usage_facts_db(resolved_db)
    source_times = _transcript_occurred_at_by_run_id(Path(projects_root))

    if apply:
        connection = usage_facts_db_mod._connect(resolved_db)
    else:
        try:
            connection = sqlite3.connect(
                f"file:{quote(str(resolved_db.expanduser().resolve()), safe='/')}?mode=ro",
                uri=True,
                timeout=2.0,
            )
        except sqlite3.Error:
            return {
                "applied": apply,
                "calls_to_fill": 0,
                "runs_to_fill": 0,
                "calls_filled": 0,
                "runs_filled": 0,
            }
    connection.row_factory = sqlite3.Row
    try:
        null_calls = connection.execute(
            "SELECT run_id, call_index FROM run_llm_calls "
            "WHERE origin=? AND occurred_at IS NULL",
            (ORIGIN,),
        ).fetchall()
        call_updates: list[tuple[str, int, str]] = []
        for row in null_calls:
            run_id = str(row["run_id"])
            timestamp = source_times.get(run_id)
            if timestamp:
                call_updates.append((timestamp, run_id, int(row["call_index"])))

        calls_to_fill = len(call_updates)
        calls_filled = 0
        if apply and call_updates:
            for timestamp, run_id, call_index in call_updates:
                cursor = connection.execute(
                    "UPDATE run_llm_calls SET occurred_at=? "
                    "WHERE run_id=? AND call_index=? AND origin=? "
                    "AND occurred_at IS NULL",
                    (timestamp, run_id, call_index, ORIGIN),
                )
                calls_filled += max(0, int(cursor.rowcount))

        # Project call times that would exist after the fill for run aggregates.
        projected_times: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT run_id, occurred_at FROM run_llm_calls "
            "WHERE origin=? AND occurred_at IS NOT NULL",
            (ORIGIN,),
        ):
            projected_times.setdefault(str(row["run_id"]), []).append(
                str(row["occurred_at"])
            )
        if not apply:
            for timestamp, run_id, _call_index in call_updates:
                projected_times.setdefault(run_id, []).append(timestamp)

        null_runs = connection.execute(
            "SELECT run_id, first_call_at, last_call_at FROM run_usage_facts "
            "WHERE origin=? AND (first_call_at IS NULL OR last_call_at IS NULL)",
            (ORIGIN,),
        ).fetchall()
        run_updates: list[tuple[str | None, str | None, str]] = []
        for row in null_runs:
            run_id = str(row["run_id"])
            times = projected_times.get(run_id) or []
            if not times:
                continue
            computed_first = min(times)
            computed_last = max(times)
            new_first = (
                None if row["first_call_at"] is not None else computed_first
            )
            new_last = (
                None if row["last_call_at"] is not None else computed_last
            )
            if new_first is None and new_last is None:
                continue
            run_updates.append((new_first, new_last, run_id))

        runs_to_fill = len(run_updates)
        runs_filled = 0
        if apply and run_updates:
            for new_first, new_last, run_id in run_updates:
                cursor = connection.execute(
                    "UPDATE run_usage_facts SET "
                    "first_call_at=COALESCE(first_call_at, ?), "
                    "last_call_at=COALESCE(last_call_at, ?) "
                    "WHERE run_id=? AND origin=?",
                    (new_first, new_last, run_id, ORIGIN),
                )
                runs_filled += max(0, int(cursor.rowcount))
            connection.commit()
        elif apply:
            connection.commit()

        return {
            "applied": apply,
            "calls_to_fill": calls_to_fill,
            "runs_to_fill": runs_to_fill,
            "calls_filled": calls_filled if apply else 0,
            "runs_filled": runs_filled if apply else 0,
        }
    finally:
        connection.close()


def _transcript_tool_calls_by_run_id(
    projects_root: Path,
) -> dict[str, list[tuple[str, int, int | None]]]:
    """Aggregate observed tool names across duplicate transcript copies."""
    root = Path(projects_root)
    observed: dict[str, dict[str, tuple[str, int | None]]] = {}
    for path in discover_jsonl_files(root):
        drafts, _stats = parse_transcript_file(path, projects_root=root)
        for run_id, draft in drafts.items():
            run_tools = observed.setdefault(run_id, {})
            for tool_id, tool_name in draft.tool_names_by_use_id.items():
                previous = run_tools.get(tool_id)
                output_chars = draft.tool_output_chars_by_use_id.get(tool_id)
                if output_chars is None and previous is not None:
                    output_chars = previous[1]
                run_tools[tool_id] = (tool_name, output_chars)

    aggregates: dict[str, list[tuple[str, int, int | None]]] = {}
    for run_id, tool_uses in observed.items():
        counts = Counter(tool_name for tool_name, _output in tool_uses.values())
        output_totals: Counter[str] = Counter()
        names_with_output: set[str] = set()
        for tool_name, output_chars in tool_uses.values():
            if output_chars is None:
                continue
            output_totals[tool_name] += output_chars
            names_with_output.add(tool_name)
        aggregates[run_id] = [
            (
                tool_name,
                call_count,
                output_totals[tool_name]
                if tool_name in names_with_output
                else None,
            )
            for tool_name, call_count in sorted(counts.items())
        ]
    return aggregates


def backfill_tool_calls(
    *,
    db_path: Path | str | None = None,
    projects_root: Path | str = DEFAULT_PROJECTS_ROOT,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or insert missing named-tool rows for existing Claude runs.

    Existing rows are never updated. The origin filter is resolved from
    ``run_usage_facts`` and preview opens the target database read-only.
    """
    empty_report = {
        "applied": apply,
        "runs_to_fill": 0,
        "tool_rows_to_fill": 0,
        "tool_rows_filled": 0,
    }
    resolved_db = usage_facts_db_path(db_path)
    if not resolved_db.is_file():
        return empty_report

    transcript_rows = _transcript_tool_calls_by_run_id(Path(projects_root))
    if apply:
        initialize_usage_facts_db(resolved_db)
        connection = usage_facts_db_mod._connect(resolved_db)
    else:
        connection = sqlite3.connect(
            f"file:{quote(str(resolved_db.expanduser().resolve()), safe='/')}?mode=ro",
            uri=True,
            timeout=2.0,
        )
    connection.row_factory = sqlite3.Row
    try:
        claude_run_ids = {
            str(row["run_id"])
            for row in connection.execute(
                "SELECT run_id FROM run_usage_facts WHERE origin=?",
                (ORIGIN,),
            )
        }
        has_tool_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='run_tool_calls'"
        ).fetchone()
        existing: set[tuple[str, str]] = set()
        if has_tool_table is not None:
            existing = {
                (str(row["run_id"]), str(row["tool_name"]))
                for row in connection.execute(
                    "SELECT run_id, tool_name FROM run_tool_calls"
                )
            }

        candidates = [
            (run_id, tool_name, call_count, output_chars)
            for run_id, tool_rows in transcript_rows.items()
            if run_id in claude_run_ids
            for tool_name, call_count, output_chars in tool_rows
            if (run_id, tool_name) not in existing
        ]
        runs_to_fill = len({run_id for run_id, *_rest in candidates})
        rows_filled = 0
        if apply:
            for run_id, tool_name, call_count, output_chars in candidates:
                cursor = connection.execute(
                    "INSERT INTO run_tool_calls "
                    "(run_id, tool_name, call_count, output_chars) "
                    "SELECT ?, ?, ?, ? WHERE EXISTS ("
                    "SELECT 1 FROM run_usage_facts "
                    "WHERE run_id=? AND origin=?"
                    ") ON CONFLICT(run_id, tool_name) DO NOTHING",
                    (
                        run_id,
                        tool_name,
                        call_count,
                        output_chars,
                        run_id,
                        ORIGIN,
                    ),
                )
                rows_filled += max(0, int(cursor.rowcount))
            connection.commit()

        return {
            "applied": apply,
            "runs_to_fill": runs_to_fill,
            "tool_rows_to_fill": len(candidates),
            "tool_rows_filled": rows_filled if apply else 0,
        }
    except Exception:
        if apply:
            connection.rollback()
        raise
    finally:
        connection.close()


_CACHE_WRITE_SPLIT_COLUMNS = (
    "cache_write_1h_tokens",
    "cache_write_5m_tokens",
)


def _transcript_cache_write_splits_by_run_id(
    projects_root: Path,
) -> dict[str, dict[str, int]]:
    """Map run_id to cache-write lifetimes observed in transcript usage."""
    root = Path(projects_root)
    splits: dict[str, dict[str, int]] = {}
    for path in discover_jsonl_files(root):
        drafts, _stats = parse_transcript_file(path, projects_root=root)
        for run_id, draft in drafts.items():
            observed = {
                column: value
                for column, value in (
                    ("cache_write_1h_tokens", draft.cache_write_1h_tokens),
                    ("cache_write_5m_tokens", draft.cache_write_5m_tokens),
                )
                if value is not None
            }
            if observed:
                splits.setdefault(run_id, {}).update(observed)
    return splits


def _empty_cache_write_split_backfill_report(
    *, apply: bool
) -> dict[str, Any]:
    table_report = {
        "rows_to_fill": 0,
        "rows_filled": 0,
        "cache_write_1h_rows_to_fill": 0,
        "cache_write_1h_rows_filled": 0,
        "cache_write_1h_tokens_to_fill": 0,
        "cache_write_5m_rows_to_fill": 0,
        "cache_write_5m_rows_filled": 0,
        "cache_write_5m_tokens_to_fill": 0,
    }
    return {
        "applied": apply,
        "run_llm_calls": dict(table_report),
        "run_usage_facts": dict(table_report),
        "totals": dict(table_report),
    }


def _cache_write_split_candidates(
    connection: sqlite3.Connection,
    table: str,
    source_splits: Mapping[str, Mapping[str, int]],
) -> list[dict[str, Any]]:
    if table not in {"run_llm_calls", "run_usage_facts"}:
        raise ValueError(f"unsupported cache-write backfill table: {table}")
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    keys = ("run_id", "call_index") if table == "run_llm_calls" else ("run_id",)
    select_columns = [*keys]
    select_columns.extend(
        column if column in columns else f"NULL AS {column}"
        for column in _CACHE_WRITE_SPLIT_COLUMNS
    )
    call_filter = " AND call_index=0" if table == "run_llm_calls" else ""
    rows = connection.execute(
        f"SELECT {', '.join(select_columns)} FROM {table} "
        f"WHERE origin=?{call_filter}",
        (ORIGIN,),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        observed = source_splits.get(str(row["run_id"]))
        if observed is None:
            continue
        updates = {
            column: observed[column]
            for column in _CACHE_WRITE_SPLIT_COLUMNS
            if row[column] is None and observed.get(column) is not None
        }
        if updates:
            candidates.append(
                {
                    "key": tuple(row[key] for key in keys),
                    "updates": updates,
                }
            )
    return candidates


def _summarize_cache_write_split_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    summary = _empty_cache_write_split_backfill_report(apply=False)[
        "run_llm_calls"
    ]
    summary["rows_to_fill"] = len(candidates)
    for candidate in candidates:
        for column, value in candidate["updates"].items():
            prefix = column.removesuffix("_tokens")
            summary[f"{prefix}_rows_to_fill"] += 1
            summary[f"{column}_to_fill"] += int(value)
    return summary


def backfill_cache_write_splits(
    *,
    db_path: Path | str | None = None,
    projects_root: Path | str = DEFAULT_PROJECTS_ROOT,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply observed 1h/5m cache-write token lifetimes.

    Only Claude Code rows and NULL target cells are eligible. Preview opens the
    database read-only, including pre-migration databases without the columns.
    """
    resolved_db = usage_facts_db_path(db_path)
    report = _empty_cache_write_split_backfill_report(apply=apply)
    if not resolved_db.is_file():
        return report

    source_splits = _transcript_cache_write_splits_by_run_id(Path(projects_root))
    if apply:
        connection = usage_facts_db_mod._connect(resolved_db)
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection = sqlite3.connect(
            f"file:{quote(str(resolved_db.expanduser().resolve()), safe='/')}?mode=ro",
            uri=True,
            timeout=2.0,
        )
    connection.row_factory = sqlite3.Row

    try:
        table_candidates = {
            table: _cache_write_split_candidates(
                connection, table, source_splits
            )
            for table in ("run_llm_calls", "run_usage_facts")
        }
        for table, candidates in table_candidates.items():
            report[table] = _summarize_cache_write_split_candidates(candidates)

        if apply:
            for table, candidates in table_candidates.items():
                keys = (
                    ("run_id", "call_index")
                    if table == "run_llm_calls"
                    else ("run_id",)
                )
                key_where = " AND ".join(f"{key}=?" for key in keys)
                for candidate in candidates:
                    values = [
                        candidate["updates"].get(column)
                        for column in _CACHE_WRITE_SPLIT_COLUMNS
                    ]
                    cursor = connection.execute(
                        f"UPDATE {table} SET "
                        "cache_write_1h_tokens="
                        "COALESCE(cache_write_1h_tokens, ?), "
                        "cache_write_5m_tokens="
                        "COALESCE(cache_write_5m_tokens, ?) "
                        f"WHERE {key_where} AND origin=? AND "
                        "(cache_write_1h_tokens IS NULL OR "
                        "cache_write_5m_tokens IS NULL)",
                        (*values, *candidate["key"], ORIGIN),
                    )
                    if cursor.rowcount <= 0:
                        continue
                    report[table]["rows_filled"] += 1
                    for column in candidate["updates"]:
                        prefix = column.removesuffix("_tokens")
                        report[table][f"{prefix}_rows_filled"] += 1
            connection.commit()

        total_keys = tuple(report["totals"])
        report["totals"] = {
            key: sum(report[table][key] for table in table_candidates)
            for key in total_keys
        }
        return report
    except Exception:
        if apply:
            connection.rollback()
        raise
    finally:
        connection.close()


_PARENT_BINDING_COLUMNS = (
    "parent_tool_use_id",
    "parent_agent_id",
    "parent_session_id",
    "spawn_depth",
)


def _empty_parent_binding_backfill_report(*, apply: bool) -> dict[str, Any]:
    table_report = {
        **{f"{column}_to_fill": 0 for column in _PARENT_BINDING_COLUMNS},
        **{f"{column}_filled": 0 for column in _PARENT_BINDING_COLUMNS},
        "model_source_to_update": 0,
        "model_source_updated": 0,
    }
    return {
        "applied": apply,
        "run_llm_calls": dict(table_report),
        "run_usage_facts": dict(table_report),
        "totals": dict(table_report),
    }


def _transcript_parent_meta_by_run_id(
    projects_root: Path,
) -> dict[str, dict[str, Any]]:
    """Return unambiguous parent-agent/spawn observations per transcript call.

    Only values carried by the matching ``agent-*.meta.json`` are considered.
    The parent session path and legacy ``model_source`` values are deliberately
    excluded here: they are different observations. Conflicting meta values for
    a resumed call are not guessed and therefore remain absent.
    """
    observations: dict[str, dict[str, set[Any]]] = {}
    for path in discover_jsonl_files(projects_root):
        if path.parent.name != "subagents":
            continue
        drafts, _stats = parse_transcript_file(path, projects_root=projects_root)
        for run_id, draft in drafts.items():
            run_observations = observations.setdefault(
                run_id,
                {"parent_agent_id": set(), "spawn_depth": set()},
            )
            if draft.parent_agent_id is not None:
                run_observations["parent_agent_id"].add(draft.parent_agent_id)
            if draft.spawn_depth is not None:
                run_observations["spawn_depth"].add(draft.spawn_depth)

    resolved: dict[str, dict[str, Any]] = {}
    for run_id, fields in observations.items():
        exact = {
            column: next(iter(values))
            for column, values in fields.items()
            if len(values) == 1
        }
        if exact:
            resolved[run_id] = exact
    return resolved


def _legacy_parent_binding(model_source: Any) -> tuple[str, str] | None:
    if not isinstance(model_source, str):
        return None
    for prefix, column in (
        ("parent_tool_use:", "parent_tool_use_id"),
        ("parent_session:", "parent_session_id"),
        ("parent_agent:", "parent_agent_id"),
    ):
        if model_source.startswith(prefix):
            value = model_source[len(prefix) :].strip()
            return (column, value) if value else None
    return None


def _parent_binding_candidates(
    connection: sqlite3.Connection,
    table: str,
    parent_meta: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    keys = ("run_id", "call_index") if table == "run_llm_calls" else ("run_id",)
    select_columns = [*keys, "model", "model_source"]
    select_columns.extend(
        column if column in columns else f"NULL AS {column}"
        for column in _PARENT_BINDING_COLUMNS
    )
    rows = connection.execute(
        f"SELECT {', '.join(select_columns)} FROM {table} WHERE origin=?",
        (ORIGIN,),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        source = row["model_source"]
        observed: dict[str, Any] = {}
        legacy = _legacy_parent_binding(source)
        if legacy is not None and legacy[0] in {
            "parent_tool_use_id",
            "parent_session_id",
        }:
            observed[legacy[0]] = legacy[1]
        observed.update(parent_meta.get(run_id, {}))

        updates = {
            column: observed[column]
            for column in _PARENT_BINDING_COLUMNS
            if row[column] is None and observed.get(column) is not None
        }
        projected = {
            column: row[column] if row[column] is not None else updates.get(column)
            for column in _PARENT_BINDING_COLUMNS
        }

        normalize_source = False
        if row["model"] is not None:
            if source is None or source == "session":
                normalize_source = True
            elif legacy is not None:
                required_column, required_value = legacy
                normalize_source = projected.get(required_column) == required_value

        if updates or normalize_source:
            candidates.append(
                {
                    "key": tuple(row[key] for key in keys),
                    "updates": updates,
                    "model_source": source,
                    "normalize_source": normalize_source,
                }
            )
    return candidates


def _summarize_parent_binding_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    summary = {
        **{f"{column}_to_fill": 0 for column in _PARENT_BINDING_COLUMNS},
        **{f"{column}_filled": 0 for column in _PARENT_BINDING_COLUMNS},
        "model_source_to_update": 0,
        "model_source_updated": 0,
    }
    for candidate in candidates:
        for column in candidate["updates"]:
            summary[f"{column}_to_fill"] += 1
        if candidate["normalize_source"]:
            summary["model_source_to_update"] += 1
    return summary


def backfill_parent_bindings(
    *,
    db_path: Path | str | None = None,
    projects_root: Path | str = DEFAULT_PROJECTS_ROOT,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or atomically apply Claude parent-binding normalization.

    Legacy tool-use/session prefixes are transformed from each row's own
    ``model_source`` value. ``parent_agent_id`` and ``spawn_depth`` come only
    from the exact transcript's adjacent meta file. All writes are origin-
    scoped, NULL-only via ``COALESCE``, and model-source cleanup happens after
    the binding writes in the same transaction.
    """
    resolved_db = usage_facts_db_path(db_path)
    if not resolved_db.is_file():
        return _empty_parent_binding_backfill_report(apply=apply)

    parent_meta = _transcript_parent_meta_by_run_id(Path(projects_root))
    if apply:
        connection = usage_facts_db_mod._connect(resolved_db)
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection = sqlite3.connect(
            f"file:{quote(str(resolved_db.expanduser().resolve()), safe='/')}?mode=ro",
            uri=True,
            timeout=2.0,
        )
    connection.row_factory = sqlite3.Row

    report = _empty_parent_binding_backfill_report(apply=apply)
    try:
        table_candidates = {
            table: _parent_binding_candidates(connection, table, parent_meta)
            for table in ("run_llm_calls", "run_usage_facts")
        }
        for table, candidates in table_candidates.items():
            report[table] = _summarize_parent_binding_candidates(candidates)

        if apply:
            for table, candidates in table_candidates.items():
                keys = (
                    ("run_id", "call_index")
                    if table == "run_llm_calls"
                    else ("run_id",)
                )
                key_where = " AND ".join(f"{key}=?" for key in keys)
                for column in _PARENT_BINDING_COLUMNS:
                    params = [
                        (candidate["updates"][column], *candidate["key"], ORIGIN)
                        for candidate in candidates
                        if column in candidate["updates"]
                    ]
                    if not params:
                        continue
                    before = connection.total_changes
                    connection.executemany(
                        f"UPDATE {table} SET {column}=COALESCE({column}, ?) "
                        f"WHERE {key_where} AND origin=? AND {column} IS NULL",
                        params,
                    )
                    report[table][f"{column}_filled"] = (
                        connection.total_changes - before
                    )

            # Bindings are durable now. Only then replace the legacy vocabulary.
            for table, candidates in table_candidates.items():
                keys = (
                    ("run_id", "call_index")
                    if table == "run_llm_calls"
                    else ("run_id",)
                )
                key_where = " AND ".join(f"{key}=?" for key in keys)
                params = [
                    (*candidate["key"], ORIGIN, candidate["model_source"])
                    for candidate in candidates
                    if candidate["normalize_source"]
                ]
                if not params:
                    continue
                before = connection.total_changes
                connection.executemany(
                    f"UPDATE {table} SET model_source='transcript' "
                    f"WHERE {key_where} AND origin=? AND model_source IS ?",
                    params,
                )
                report[table]["model_source_updated"] = (
                    connection.total_changes - before
                )
            connection.commit()

        total_keys = tuple(report["totals"])
        report["totals"] = {
            key: sum(report[table][key] for table in table_candidates)
            for key in total_keys
        }
        return report
    except Exception:
        if apply:
            connection.rollback()
        raise
    finally:
        connection.close()


def harvest(
    *,
    projects_root: Path | str = DEFAULT_PROJECTS_ROOT,
    db_path: Path | str | None = None,
    state_path: Path | str | None = None,
    dry_run: bool = False,
    update_existing_only: bool = False,
    progress_every: int = 200,
    log: Optional[TextIO] = None,
    kanban_paths: Optional[Sequence[Path | str]] = None,
) -> HarvestStats:
    """Run an incremental, idempotent harvest into the usage-facts DB.

    Files are streamed one-by-one.  Each file's drafts are written in a single
    transaction (not held for the whole tree), so a 1.9 GB corpus does not
    accumulate in RAM. Cross-file ``--resume`` duplicates collapse via the
    global ``(message.id, requestId)`` run_id upsert. In explicit
    ``update_existing_only`` mode, raw calls with an absent Claude fact
    identity are skipped rather than inserted.
    """
    root = Path(projects_root)
    resolved_db = usage_facts_db_path(db_path)
    if not dry_run:
        initialize_usage_facts_db(resolved_db)

    if state_path is None:
        state_file = resolved_db.with_suffix(resolved_db.suffix + ".claude_code_hwm.json")
    else:
        state_file = Path(state_path)

    hwm = HighWaterMark.load(state_file)
    # Format bump invalidates HWM so we re-read everything under the new parser.
    if hwm.format_version != CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION:
        hwm = HighWaterMark()

    stats = HarvestStats()
    out = log or sys.stderr
    files = discover_jsonl_files(root)
    from hermes_cli.usage_fact_correlation import scan_claude_session_correlations

    candidate_session_ids: set[str] = set()
    for path in files:
        if path.parent.name == "subagents" and len(path.parts) >= 3:
            candidate_session_ids.add(path.parts[-3])
        else:
            candidate_session_ids.add(path.stem)
    correlation_scan = scan_claude_session_correlations(
        candidate_session_ids,
        kanban_paths=kanban_paths,
        transcript_paths=files,
    )
    correlations = correlation_scan.correlations
    stats.sessions_correlated = len(correlations)
    stats.calls_recorrelated = recorrelate_existing_calls(
        correlations,
        db_path=resolved_db,
        dry_run=dry_run,
        ambiguous_session_ids=correlation_scan.ambiguous_session_ids,
    )
    derived_billing_mode, derived_billing_mode_source = (
        _billing_mode_from_access_configuration()
    )

    for index, path in enumerate(files, 1):
        stats.files_seen += 1
        if hwm.is_unchanged(path):
            stats.files_skipped_hwm += 1
            continue
        try:
            drafts, file_stats = parse_transcript_file(path, projects_root=root)
        except OSError as exc:
            stats.parse_errors += 1
            print(f"skip unreadable {path}: {exc}", file=out)
            continue

        stats.files_processed += 1
        stats.lines_seen += file_stats.lines_seen
        stats.lines_skipped += file_stats.lines_skipped
        stats.calls_merged += file_stats.calls_merged
        stats.parse_errors += file_stats.parse_errors

        if derived_billing_mode_source is not None:
            for draft in drafts.values():
                if draft.billing_mode_source is None:
                    draft.billing_mode = derived_billing_mode
                    draft.billing_mode_source = derived_billing_mode_source

        for draft in drafts.values():
            resolved_correlation = _draft_correlation(draft, correlations)
            if resolved_correlation is None:
                continue
            correlation, correlation_source = resolved_correlation
            draft.task_run_id = correlation.task_run_id
            draft.task_id = correlation.task_id
            draft.chain_id = correlation.chain_id
            draft.board = correlation.board
            draft.profile = draft.profile or correlation.profile
            draft.correlation_source = correlation_source
            stats.calls_correlated += 1

        written = write_calls_batch(
            drafts.values(),
            db_path=resolved_db,
            dry_run=dry_run,
            update_existing_only=update_existing_only,
        )
        stats.calls_written += written

        if not dry_run:
            hwm.mark(path)
            # Persist HWM periodically so a kill mid-run still skips done files.
            if stats.files_processed % 50 == 0:
                hwm.format_version = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION
                hwm.save(state_file)

        if progress_every and index % progress_every == 0:
            print(
                f"progress files={index}/{len(files)} "
                f"processed={stats.files_processed} "
                f"skipped_hwm={stats.files_skipped_hwm} "
                f"written={stats.calls_written}",
                file=out,
                flush=True,
            )

    if not dry_run:
        hwm.format_version = CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION
        hwm.save(state_file)

    return stats


def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Harvest Claude Code transcript JSONL into the usage-facts DB "
            f"(format v{CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION}). "
            "Manual / on-demand only — not a cron."
        )
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=DEFAULT_PROJECTS_ROOT,
        help=f"Claude projects root (default: {DEFAULT_PROJECTS_ROOT})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="usage_facts.db path (default: HERMES_USAGE_FACTS_DB or /mnt/data/...)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="High-water-mark state JSON (default: <db>.claude_code_hwm.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and merge only; do not write the DB or update HWM",
    )
    parser.add_argument(
        "--backfill-correlations",
        action="store_true",
        help="Preview exact-session correlation backfill for existing Claude facts",
    )
    parser.add_argument(
        "--backfill-timestamps",
        action="store_true",
        help=(
            "Preview filling NULL occurred_at / first_call_at / last_call_at "
            "from transcript source times (never touches captured_at)"
        ),
    )
    parser.add_argument(
        "--backfill-parent-bindings",
        action="store_true",
        help=(
            "Preview filling NULL Claude parent-binding columns and replacing "
            "safe legacy model_source values with transcript"
        ),
    )
    parser.add_argument(
        "--backfill-cache-writes",
        action="store_true",
        help=(
            "Preview filling NULL 1h/5m cache-write token columns from "
            "transcript usage.cache_creation observations"
        ),
    )
    parser.add_argument(
        "--backfill-tool-calls",
        action="store_true",
        help=(
            "Preview inserting missing per-tool rows for existing Claude "
            "runs from transcript tool_use names"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply one explicit --backfill-* operation "
            "(preview is the default)"
        ),
    )
    parser.add_argument(
        "--update-existing-only",
        action="store_true",
        help=(
            "Backfill only existing Claude fact identities; skip newly "
            "discovered transcript records"
        ),
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=200,
        help="Log progress every N files (0 disables)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    backfill_modes = sum(
        bool(enabled)
        for enabled in (
            args.backfill_correlations,
            args.backfill_timestamps,
            args.backfill_parent_bindings,
            args.backfill_cache_writes,
            args.backfill_tool_calls,
        )
    )
    if backfill_modes > 1:
        parser.error("choose exactly one --backfill-* operation")
    if args.apply and backfill_modes == 0:
        parser.error("--apply requires one --backfill-* operation")
    if args.backfill_correlations:
        report = backfill_existing_correlations(
            db_path=args.db,
            projects_root=args.projects_root,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.backfill_timestamps:
        report = backfill_occurred_at(
            db_path=args.db,
            projects_root=args.projects_root,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.backfill_parent_bindings:
        report = backfill_parent_bindings(
            db_path=args.db,
            projects_root=args.projects_root,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.backfill_cache_writes:
        report = backfill_cache_write_splits(
            db_path=args.db,
            projects_root=args.projects_root,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.backfill_tool_calls:
        report = backfill_tool_calls(
            db_path=args.db,
            projects_root=args.projects_root,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    stats = harvest(
        projects_root=args.projects_root,
        db_path=args.db,
        state_path=args.state,
        dry_run=args.dry_run,
        update_existing_only=args.update_existing_only,
        progress_every=args.progress_every,
    )
    print(
        json.dumps(
            {
                "format_version": stats.format_version,
                "files_seen": stats.files_seen,
                "files_skipped_hwm": stats.files_skipped_hwm,
                "files_processed": stats.files_processed,
                "lines_seen": stats.lines_seen,
                "lines_skipped": stats.lines_skipped,
                "calls_merged": stats.calls_merged,
                "calls_written": stats.calls_written,
                "sessions_correlated": stats.sessions_correlated,
                "calls_correlated": stats.calls_correlated,
                "calls_recorrelated": stats.calls_recorrelated,
                "parse_errors": stats.parse_errors,
                "dry_run": bool(args.dry_run),
                "db": str(usage_facts_db_path(args.db)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

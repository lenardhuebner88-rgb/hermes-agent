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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence, TextIO
from urllib.parse import quote

from hermes_cli import usage_facts_db as usage_facts_db_mod
from hermes_cli.usage_facts_db import (
    initialize_usage_facts_db,
    record_llm_call,
    usage_facts_db_path,
)

# Bump when the transcript shape we accept changes in a way that would make
# golden fixtures silently wrong.  Tests pin this value.
#
# v4 persists the transcript session identity and exact Kanban correlations, so
# existing HWM snapshots re-harvest their captured files and backfill the link.
CLAUDE_CODE_TRANSCRIPT_FORMAT_VERSION = 4

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
ORIGIN = "claude_code"
PROVIDER = "anthropic"
NO_REQUEST_ID = "__no_request_id__"
RUN_ID_PREFIX = "claude_code"

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
    tool_use_ids: set[str] = field(default_factory=set)
    tool_output_chars: int = 0
    call_kind: str = "main"
    profile: Optional[str] = None
    parent_session_id: Optional[str] = None
    parent_tool_use_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
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


def _tool_use_ids_from_content(content: Iterable[Any]) -> set[str]:
    ids: set[str] = set()
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") != "tool_use":
            continue
        tool_id = _text(part.get("id"))
        if tool_id is not None:
            ids.add(tool_id)
    return ids


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

    usage = message.get("usage")
    if isinstance(usage, Mapping):
        _apply_usage(draft, usage)

    draft.tool_use_ids |= _tool_use_ids_from_content(_content_parts(message))
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
        # spawnDepth / description / model / stoppedByUser are intentionally
        # ignored when absent — never required.
    return ctx


def _model_source(ctx: Mapping[str, Any]) -> Optional[str]:
    tool_use = ctx.get("parent_tool_use_id")
    if tool_use:
        return f"parent_tool_use:{tool_use}"
    parent_agent = ctx.get("parent_agent_id")
    if parent_agent:
        return f"parent_agent:{parent_agent}"
    parent_session = ctx.get("parent_session_id")
    if parent_session:
        return f"parent_session:{parent_session}"
    return "session"


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
                draft.tool_output_chars += _tool_result_chars(part)
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
        "model_source": _model_source(
            {
                "parent_tool_use_id": draft.parent_tool_use_id,
                "parent_agent_id": draft.parent_agent_id,
                "parent_session_id": draft.parent_session_id,
            }
        ),
        "serving_tier": draft.service_tier,
        "reasoning_effort": draft.effort,
        "response_id": draft.message_id,
        "input_tokens": draft.input_tokens,
        "output_tokens": draft.output_tokens,
        "cache_read_tokens": draft.cache_read_tokens,
        "cache_write_tokens": draft.cache_write_tokens,
        "total_tokens": total,
        "finish_reason": draft.stop_reason,
        "tool_call_count": draft.tool_call_count,
        "tool_output_chars": draft.tool_output_chars or None,
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
    call_fields, run_fields = draft_to_fields(draft)
    record_llm_call(
        draft.run_id,
        0,
        call_fields,
        run_fields=run_fields,
        path=db_path,
    )
    return True


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
) -> int:
    """Fill exact links for HWM-skipped transcripts after task metadata lands."""
    if not correlations or not db_path.is_file():
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
    try:
        updated = 0
        for session_id, correlation in correlations.items():
            fields = correlation.as_run_fields()
            if dry_run:
                try:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM run_usage_facts "
                        "WHERE origin=? AND session_id=? AND task_id IS NULL",
                        (ORIGIN, session_id),
                    ).fetchone()
                except sqlite3.Error:
                    return 0
                updated += int(row[0]) if row is not None else 0
                continue
            cursor = connection.execute(
                "UPDATE run_usage_facts SET "
                "task_run_id=?, task_id=?, chain_id=?, board=?, "
                "profile=COALESCE(profile, ?), correlation_source=? "
                "WHERE origin=? AND session_id=? AND task_id IS NULL",
                (
                    fields.get("task_run_id"),
                    fields.get("task_id"),
                    fields.get("chain_id"),
                    fields.get("board"),
                    fields.get("profile"),
                    fields.get("correlation_source"),
                    ORIGIN,
                    session_id,
                ),
            )
            updated += max(0, int(cursor.rowcount))
        if not dry_run:
            connection.commit()
        return updated
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
    from hermes_cli.usage_fact_correlation import (
        load_claude_session_correlations,
    )

    candidate_session_ids: set[str] = set()
    for path in files:
        if path.parent.name == "subagents" and len(path.parts) >= 3:
            candidate_session_ids.add(path.parts[-3])
        else:
            candidate_session_ids.add(path.stem)
    correlations = load_claude_session_correlations(
        candidate_session_ids,
        kanban_paths=kanban_paths,
    )
    stats.sessions_correlated = len(correlations)
    stats.calls_recorrelated = recorrelate_existing_calls(
        correlations,
        db_path=resolved_db,
        dry_run=dry_run,
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
            correlation = correlations.get(
                draft.session_id or draft.parent_session_id or ""
            )
            if correlation is None:
                continue
            draft.task_run_id = correlation.task_run_id
            draft.task_id = correlation.task_id
            draft.chain_id = correlation.chain_id
            draft.board = correlation.board
            draft.profile = draft.profile or correlation.profile
            draft.correlation_source = correlation.source
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

"""Deterministic eliding of stale read-type tool results from the per-turn
API message copy.

Problem this solves
-------------------
On the token-heaviest worker lanes (Codex / ``coder``), the carried turn
history accumulates large, idempotent read outputs — old ``read_file`` and
``skill_view`` results — that the model rarely needs verbatim once the working
window has moved on. Because long Codex runs see provider-side prompt-cache
eviction (untunable), those huge bodies get re-sent and re-charged as full
input tokens on every cache-miss turn (observed: single Codex turns >900K /
>1.8M input tokens, in:out ratios >100:1).

What this pass does
-------------------
``elide_stale_tool_results`` is a *pure* transform that walks an API message
copy and replaces the body of **old, large, re-readable or low-context** tool
results with a deterministic one-line summary (path / offset / size), reusing
the same summary format the context compressor already uses. It returns a new
list and never mutates its input.

Correctness contract (the counter-metric guardrail)
---------------------------------------------------
* **Operates on the API copy only.** The wiring (``conversation_loop``) runs
  this on ``api_messages`` — the per-call copy rebuilt fresh from the stored
  ``messages`` every turn — never on the persisted transcript. So a worker
  never *loses* a tool result from its real history; only the bytes sent to
  the model on the current turn are trimmed. This is the master guarantee.
* **Bounded tool allowlist only.** Default set is
  ``{read_file, skill_view, search_files, terminal}``: idempotent reads plus
  stale command output, which dominates coder-lane bloat but is protected while
  recent. Mutating edit tools such as ``write_file`` / ``patch`` are never
  touched by default.
* **Youngest turns intact.** The last ``protect_last_n`` messages (the active
  working set + the current AC context) are never elided.
* **Pointer preserved.** The replacement is an informative summary
  (``[read_file] read foo.py from line 1 (12,345 chars)``), not a blank — the
  model keeps a precise pointer and can re-read on demand.
* **Byte-stable across turns.** Because ``api_messages`` is rebuilt from the
  pristine ``messages`` every turn, the elided form for any given message is
  deterministic and identical turn-to-turn; the small-summary length guard
  also means an already-summarised body (e.g. from a compressed session) is
  left alone rather than re-processed.

See the PlanSpec subtask ``WORKER-CONTEXT-DIET-ELIDE-S1`` and the worker-latency
findings handoff (``2026-06-19-worker-latency-findings-HANDOFF.md``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

# Reuse the compressor's already-tested tool-result summariser and tool-call
# accessors so the elided form matches what the compression path produces.
from agent.context_compressor import (
    _extract_tool_call_id,
    _extract_tool_call_name_and_args,
    _summarize_tool_result,
)
from utils import is_truthy_value

# Only these tools are elided by default. The set is deliberately bounded to
# high-volume sources observed in coder-lane cost attribution: idempotent reads
# plus stale terminal output (build/test logs). Mutating edit results are left
# verbatim unless an operator explicitly opts in via HERMES_TOOL_ELIDE_TOOLS.
DEFAULT_ELIDABLE_TOOLS: FrozenSet[str] = frozenset(
    {"read_file", "skill_view", "search_files", "terminal"}
)

# Tools an operator may opt into with HERMES_TOOL_ELIDE_TOOLS. Keep this allowlist
# narrow: it is a safety rail around the config knob, not a general arbitrary
# tool-name passthrough.
ALLOWED_ELIDABLE_TOOLS: FrozenSet[str] = frozenset(
    {
        "read_file",
        "skill_view",
        "search_files",
        "terminal",
        "session_search",
        "kanban_show",
    }
)

# Protect the youngest N messages (active working set + current AC) verbatim.
DEFAULT_PROTECT_LAST_N = 14

# Only bodies larger than this (chars) are worth eliding. Small read outputs
# are left alone — the summary would not save enough to justify the churn, and
# this length floor also skips any already-short summary/placeholder content.
DEFAULT_MIN_ELIDE_CHARS = 1500


@dataclass(frozen=True)
class ElideConfig:
    """Resolved runtime knobs for the eliding pass."""

    enabled: bool
    protect_last_n: int
    min_elide_chars: int
    elidable_tools: FrozenSet[str]


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse a positive int env var, falling back to ``default`` on anything bad."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def _tool_set_env(env: Mapping[str, str], key: str, default: FrozenSet[str]) -> FrozenSet[str]:
    """Parse a comma-separated tool allowlist, bounded to known safe tools."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    requested = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(name for name in requested if name in ALLOWED_ELIDABLE_TOOLS)


def tool_eliding_config(env: Optional[Mapping[str, str]] = None) -> ElideConfig:
    """Resolve the eliding configuration from the environment.

    Default is **enabled** with the conservative defaults above. A kill-switch
    (``HERMES_TOOL_ELIDE_DISABLED=1``) turns it off instantly without a code
    change — prudent given the pass sits on the runtime agent loop. The protect
    count, min-chars threshold, and bounded tool allowlist are tunable via env
    for live calibration.
    """
    env = os.environ if env is None else env
    return ElideConfig(
        enabled=not is_truthy_value(env.get("HERMES_TOOL_ELIDE_DISABLED")),
        protect_last_n=_int_env(env, "HERMES_TOOL_ELIDE_PROTECT_N", DEFAULT_PROTECT_LAST_N),
        min_elide_chars=_int_env(env, "HERMES_TOOL_ELIDE_MIN_CHARS", DEFAULT_MIN_ELIDE_CHARS),
        elidable_tools=_tool_set_env(env, "HERMES_TOOL_ELIDE_TOOLS", DEFAULT_ELIDABLE_TOOLS),
    )


def _build_call_id_index(messages: List[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """Map ``tool_call_id -> (tool_name, arguments_json)`` from assistant turns.

    The result dict carries the tool name directly, but the *arguments* (path,
    offset) live on the originating assistant ``tool_calls`` entry — needed for
    an informative summary.
    """
    index: Dict[str, Tuple[str, str]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            cid = _extract_tool_call_id(tc)
            if not cid:
                continue
            name, args = _extract_tool_call_name_and_args(tc)
            index[cid] = (name, args)
    return index


def elide_stale_tool_results(
    messages: List[Dict[str, Any]],
    *,
    protect_last_n: int = DEFAULT_PROTECT_LAST_N,
    min_elide_chars: int = DEFAULT_MIN_ELIDE_CHARS,
    elidable_tools: FrozenSet[str] = DEFAULT_ELIDABLE_TOOLS,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Return a copy of ``messages`` with stale read-type tool bodies elided.

    A tool-result message is elided iff **all** hold:

    * ``role == "tool"`` with plain-string content,
    * its tool name is in ``elidable_tools`` (default read_file / skill_view /
      search_files / terminal),
    * it sits **before** the protected tail (outside the last
      ``protect_last_n`` messages),
    * its body is longer than ``min_elide_chars``.

    The body is replaced with a deterministic one-line summary; ``role``,
    ``name``/``tool_name`` and ``tool_call_id`` are preserved so the
    assistant↔tool pairing and message alternation stay valid. The input list
    and its dicts are never mutated.

    Returns ``(new_messages, elided_count, saved_chars)``.
    """
    if not messages:
        return list(messages), 0, 0

    # Defensive clamp: a negative/garbage knob must never widen the blast
    # radius. Clamp to >= 0 (so boundary never exceeds len), and a protect
    # count >= len is treated as "protect all" by the boundary check below.
    protect_last_n = max(0, protect_last_n)
    n = len(messages)
    boundary = n - protect_last_n  # indices [0, boundary) are elidable
    if boundary <= 0:
        # Whole list protected — nothing to do. Still return a fresh list so
        # callers can treat the return as an independent copy uniformly.
        return [m.copy() if isinstance(m, dict) else m for m in messages], 0, 0

    call_index = _build_call_id_index(messages)

    result: List[Dict[str, Any]] = []
    elided = 0
    saved = 0
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            result.append(msg)
            continue
        if idx >= boundary or msg.get("role") != "tool":
            result.append(msg.copy())
            continue

        name = msg.get("tool_name") or msg.get("name") or ""
        content = msg.get("content")
        if name not in elidable_tools or not isinstance(content, str) or len(content) <= min_elide_chars:
            result.append(msg.copy())
            continue

        call_id = msg.get("tool_call_id", "")
        _idx_name, tool_args = call_index.get(call_id, (name, ""))
        summary = _summarize_tool_result(name, tool_args, content)
        new_msg = msg.copy()
        new_msg["content"] = summary
        result.append(new_msg)
        elided += 1
        saved += len(content) - len(summary)

    return result, elided, saved

"""Triage core for the Discord pre-filter bridge.

Classifies one inbound Discord message into a :class:`Bucket` using a cheap
model on the Max subscription via ``claude -p`` (no API call). The parsing is
deliberately **fail-open**: any output we cannot confidently read becomes
``ESCALATE`` so a real request is never silently dropped.

This module is pure-logic + a thin subprocess wrapper, kept free of discord.py
so it can be unit-tested in isolation (see tests/discord_prefilter/).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional, Pattern, Sequence

if TYPE_CHECKING:  # avoid an import cycle at runtime; config imports nothing here
    from bridges.discord_prefilter.config import PrefilterConfig

logger = logging.getLogger("discord_prefilter.triage")


class Bucket(str, Enum):
    """Where a message goes after triage."""

    TRIVIAL = "trivial"   # bot answers itself, free, on Max
    ESCALATE = "escalate"  # hand off to the full Hermes agent
    NOISE = "noise"       # ignore entirely


@dataclass(frozen=True)
class TriageDecision:
    bucket: Bucket
    reply: Optional[str]
    raw: str = ""          # raw model text, for logging/debugging
    source: str = "model"  # "model" | "heuristic" | "fallback"


# The contract handed to the cheap model. Used as a FULL system-prompt
# REPLACEMENT (not append) so the underlying Claude Code coding-assistant
# identity is overridden — otherwise the model engages with the task content
# instead of classifying it. Paired with `--tools ""` so the triage agent is
# completely inert (it cannot run tools, edit files, or start doing the work).
TRIAGE_SYSTEM_PROMPT = (
    "You are a strict, fast text classifier. You do NOT perform tasks, write "
    "code, run commands, or use tools — you only read ONE Discord message and "
    "classify it. Output ONLY a single-line JSON object, nothing else:\n"
    '{"bucket": "trivial|escalate|noise", "reply": <string or null>}\n\n'
    "Buckets:\n"
    "- trivial: you can fully answer the message right now (status question, "
    "ack, FAQ, small talk, short factual reply). Put your complete answer in "
    '"reply".\n'
    "- escalate: a real task — multi-step, needs tools/repo/files/actions, or "
    'you are unsure. Set "reply": null. A separate full agent will handle it.\n'
    "- noise: irrelevant, addressed to someone else, or bot chatter. "
    'Set "reply": null.\n\n'
    "When in doubt, choose escalate. Keep \"reply\" concise. Output JSON only."
)


# --- noise heuristic (runs before any model spawn) -------------------------


def build_noise_matchers(patterns: Sequence[str]) -> list[Pattern[str]]:
    """Compile regex noise patterns, skipping any that fail to compile.

    A bad pattern in operator config must never crash the bridge — it is
    logged and dropped.
    """
    compiled: list[Pattern[str]] = []
    for pat in patterns:
        if not pat:
            continue
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            logger.warning("ignoring invalid noise pattern %r: %s", pat, exc)
    return compiled


def heuristic_noise(text: str, matchers: Sequence[Pattern[str]]) -> bool:
    """True if ``text`` matches any configured noise pattern."""
    return any(m.search(text) for m in matchers)


# --- model output parsing (fail-open) --------------------------------------

_VALID = {b.value for b in Bucket}
# Greedy outer braces so a nested object (rare) is captured whole; we still
# fall back to escalate if json.loads can't read it.
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _fallback(raw: str) -> TriageDecision:
    return TriageDecision(bucket=Bucket.ESCALATE, reply=None, raw=raw, source="fallback")


def parse_triage_output(stdout: str) -> TriageDecision:
    """Parse the ``claude -p`` output into a decision. Never raises.

    Accepts either the ``--output-format json`` envelope (``{"result": ...}``)
    or the bare model text, then extracts the inner ``{"bucket","reply"}`` JSON.
    Anything ambiguous or malformed fails open to ESCALATE.
    """
    if not stdout or not stdout.strip():
        return _fallback(stdout)

    text = stdout.strip()

    # 1) Unwrap the claude --output-format json envelope if present.
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and ("result" in envelope or "is_error" in envelope):
            if envelope.get("is_error"):
                return _fallback(text)
            text = str(envelope.get("result", "")).strip()
    except (json.JSONDecodeError, ValueError):
        # Not an envelope — treat the whole thing as model text below.
        pass

    if not text:
        return _fallback(stdout)

    # 2) Extract the inner decision object (may be fenced or wrapped in prose).
    match = _JSON_OBJ.search(text)
    if not match:
        return _fallback(stdout)
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return _fallback(stdout)
    if not isinstance(obj, dict):
        return _fallback(stdout)

    bucket_raw = str(obj.get("bucket", "")).strip().lower()
    if bucket_raw not in _VALID:
        return _fallback(stdout)
    bucket = Bucket(bucket_raw)

    reply = obj.get("reply")
    reply = reply.strip() if isinstance(reply, str) else None

    # A "trivial" verdict with no usable answer can't be answered cheaply —
    # escalate rather than post an empty message.
    if bucket is Bucket.TRIVIAL and not reply:
        return TriageDecision(bucket=Bucket.ESCALATE, reply=None, raw=stdout, source="fallback")

    if bucket is not Bucket.TRIVIAL:
        reply = None  # only trivial carries a reply

    return TriageDecision(bucket=bucket, reply=reply, raw=stdout, source="model")


# --- subprocess: run the cheap model on Max --------------------------------


def resolve_claude_bin(configured: Optional[str] = None) -> str:
    """Resolve the ``claude`` executable: config → PATH → ~/.local/bin."""
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    import os

    fallback = os.path.expanduser("~/.local/bin/claude")
    return fallback


def run_triage(message: str, config: "PrefilterConfig") -> TriageDecision:
    """Triage one message. Heuristic noise first, else spawn ``claude -p``.

    Fail-open: a missing binary, timeout, non-zero exit, or unreadable output
    all resolve to ESCALATE.
    """
    if heuristic_noise(message, config.noise_matchers):
        return TriageDecision(bucket=Bucket.NOISE, reply=None, source="heuristic")

    cmd = [
        resolve_claude_bin(config.claude_bin),
        "-p",
        message,
        "--model",
        config.model,
        "--output-format",
        "json",
        # Full system-prompt REPLACEMENT → pure classifier identity.
        "--system-prompt",
        TRIAGE_SYSTEM_PROMPT,
        # Disable every tool → the triage agent is inert (no edits/commands).
        # With no tools there is nothing to permit, so no permission bypass
        # flag is needed (and none is used, by design).
        "--tools",
        "",
        # The classifier runs per Discord message — without this disable the
        # user-global memsearch plugin would inject shared memories into every
        # classification and spawn a haiku summarize per message (Planspec
        # 2026-06-12 memsearch-voll-rollout, T3).
        "--settings",
        '{"enabledPlugins": {"memsearch@memsearch-plugins": false}}',
    ]
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv built above
            cmd,
            capture_output=True,
            text=True,
            timeout=config.triage_timeout_s,
            env=config.claude_env(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.error("claude binary not found (%s); escalating", cmd[0])
        return _fallback("")
    except subprocess.TimeoutExpired:
        logger.warning("triage timed out after %ss; escalating", config.triage_timeout_s)
        return _fallback("")

    if proc.returncode != 0:
        logger.warning("claude -p exited %s; escalating. stderr=%s",
                       proc.returncode, (proc.stderr or "").strip()[:300])
        return _fallback(proc.stdout or "")

    return parse_triage_output(proc.stdout or "")

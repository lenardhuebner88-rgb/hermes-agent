"""Fork-owned: per-agent session facts (context, compactions, steering) for the deck.

Six ways a Buzz agent stem can prove what session it is in, all read live off
disk or the systemd journal — never Buzz's own source, config or keys:

* **claude family** (``claude``, ``fable``, ``sonnet``) — the Claude Code CLI's
  own transcript under ``~/.claude/projects/-mnt-data-services-buzz-agent-workspaces-<stem>/``.
* **codex family** (``codex``, ``terra``) — the Codex CLI's rollout log under
  ``~/.codex/sessions/YYYY/MM/DD/``, filtered by ``cwd`` because the newest
  rollout on this host is frequently a loop worker's, not a Buzz agent's.
* **qwen** — the Qwen Code CLI's transcript, which is the one format here that
  carries its own context-window size (``contextWindowSize``) instead of
  needing a models.dev lookup.
* **kimi** — the Kimi Code CLI's session store, indexed by workdir rather than
  directory name; only the ``agents/main`` sub-agent counts, the same doctrine
  as Claude's sidechain filter.
* **grok** — the Grok CLI's session store (URL-encoded workdir as the
  directory name) joined against its own unified log by session id.
* everything else (``hermes``, and any stem none of the above claims) — no
  local store exists; the payload says so (``source="none"``) with every
  number ``None``, never a guessed ``0``.

A stem's context percentage needs a context-window size, and three of the six
sources do not carry one in their own transcript (claude family, kimi, grok).
For those, :func:`hermes_cli.model_switch` already solves "given a model id,
what is its context window" via ``agent.models_dev.get_model_info`` — reused
here directly rather than reimplemented; an unrecognised model id yields
``context_window=None`` and no guessed default, never a fallback constant.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from hermes_cli.buzz_agent_tool_calls import decode_message

CLAUDE_STEMS = frozenset({"claude", "fable", "sonnet"})
CODEX_STEMS = frozenset({"codex", "terra"})
QWEN_STEMS = frozenset({"qwen"})
KIMI_STEMS = frozenset({"kimi"})
GROK_STEMS = frozenset({"grok"})

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STEERING_RE = re.compile(r"steering_supported=(true|false)")
# The startup banner of `buzz-acp`, which is the only place the mapping from a
# stem to its nostr identity is written down: neither the agent config
# (`~/.config/buzz-agents/<stem>.env`, which carries the *private* key) nor any
# code in this repo states the pubkey in the clear.
#
# Anchored to the banner prefix on purpose. Two measured false positives make
# an unanchored match dangerous: a `tool_call` log line quoting the words
# "buzz-acp starting" (an agent grepping its own logs), and another printing
# `BUZZ_ACP_RECOVERY_PATH=/…/acp/<64 hex>/…`. Either would hand back a
# confident, wrong identity — and a Zuruf sent to the wrong pubkey is silent,
# not loud.
_IDENTITY_RE = re.compile(r"buzz-acp starting:.*?\bpubkey=([0-9a-f]{64})\b")
_SUBSCRIBE_RE = re.compile(r"buzz-acp starting:.*?\bsubscribe=(\w+)")

_WORKSPACE_ROOT = "/mnt/data/services/buzz-agent-workspaces"

DEFAULT_TTL_SECONDS = 10.0


class CommandResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner(Protocol):
    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult: ...


class SubprocessRunner:
    """Run fixed argv vectors without invoking a shell."""

    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return CommandResult(-1, "", "timeout")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LastCompaction:
    """One compaction event. ``pre``/``post`` are ``None`` when the event was
    only proven by the ``isCompactSummary`` fallback signal, which carries no
    token counts of its own."""

    pre: int | None
    post: int | None
    at: float | None

    def as_payload(self) -> dict[str, Any]:
        return {"pre": self.pre, "post": self.post, "at": _iso_or_none(self.at)}


@dataclass(frozen=True)
class SessionFacts:
    """What is known about one Buzz agent's underlying CLI session."""

    stem: str
    source: str  # "claude" | "codex" | "qwen" | "kimi" | "grok" | "none"
    session_id: str | None = None
    started_at: float | None = None
    prompt_tokens: int | None = None
    context_window: int | None = None
    compacts: int = 0
    last_compaction: LastCompaction | None = None
    steerable: bool | None = None
    #: Nostr identity, read from the agent's startup banner. Without it the
    #: phone cannot address the agent at all.
    pubkey: str | None = None
    #: ``Mentions`` | ``Config`` | ... — which events the agent subscribes to.
    subscribe: str | None = None

    @property
    def addressable(self) -> bool:
        """Can a `p`-tagged message reach this agent at all?

        Deliberately only about *addressing*. Whether the agent then wakes is
        its own client-side rule set (`buzz-acp`'s `filter.rs` plus
        `<stem>-rules.toml`), not something this process can prove.
        """
        return self.pubkey is not None

    @property
    def percent(self) -> float | None:
        if self.prompt_tokens is None or not self.context_window:
            return None
        return round(100.0 * self.prompt_tokens / self.context_window, 1)

    def as_payload(self, *, now: float | None = None) -> dict[str, Any]:
        measured_at = time.time() if now is None else now
        return {
            "session_id": self.session_id,
            "started_at": _iso_or_none(self.started_at),
            "prompt_tokens": self.prompt_tokens,
            "context_window": self.context_window,
            "percent": self.percent,
            "compacts": self.compacts,
            "last_compaction": (
                None if self.last_compaction is None else self.last_compaction.as_payload()
            ),
            "measured_at": _iso_or_none(measured_at),
            "source": self.source,
            "steerable": self.steerable,
            "pubkey": self.pubkey,
            "subscribe": self.subscribe,
            "addressable": self.addressable,
        }

    @staticmethod
    def none(
        stem: str,
        *,
        steerable: bool | None = None,
        pubkey: str | None = None,
        subscribe: str | None = None,
    ) -> "SessionFacts":
        return SessionFacts(
            stem=stem,
            source="none",
            steerable=steerable,
            pubkey=pubkey,
            subscribe=subscribe,
        )


def _iso_or_none(epoch_seconds: float | None) -> str | None:
    if epoch_seconds is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch_seconds))


def _parse_iso_utc(ts: Any) -> float | None:
    """Claude/Codex/Qwen/Kimi/Grok all stamp UTC ISO 8601 (``…Z``). Naive local
    parsing here would silently be two hours off on this host."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Context-window resolution (shared by claude family, kimi, grok)
# ---------------------------------------------------------------------------

# models.dev provider id for each family's model-id shape. codex and qwen
# carry their own window in the transcript and never reach this table.
_CONTEXT_WINDOW_PROVIDER: dict[str, str] = {
    "claude": "anthropic",
    "fable": "anthropic",
    "sonnet": "anthropic",
    "kimi": "kimi-for-coding",
    "grok": "xai",
}

ContextWindowResolver = Callable[[str, str], "int | None"]


def _default_context_window_resolver(provider_id: str, model_id: str) -> int | None:
    """``model_info.context_window`` via models.dev — the same fallback
    ``hermes_cli.model_switch.resolve_display_context_length`` uses at
    ~line 894, called directly rather than through that function: the rest of
    its pipeline (provider-aware overrides, custom-provider config, the
    user's live ``/model`` pin) has no meaning for a Buzz agent's own CLI
    session and would only add fragile, irrelevant inputs.
    """
    from agent.models_dev import get_model_info

    info = get_model_info(provider_id, model_id)
    if info is None or not info.context_window:
        return None
    return int(info.context_window)


def _kimi_model_id(raw: str) -> str:
    """``"kimi-code/k3"`` -> ``"k3"``; the catalog id is the last path segment."""
    return raw.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Claude family
# ---------------------------------------------------------------------------


def claude_dir_name(stem: str) -> str:
    return f"-mnt-data-services-buzz-agent-workspaces-{stem}"


def _latest_jsonl(directory: Path) -> Path | None:
    try:
        files = [p for p in directory.iterdir() if p.suffix == ".jsonl" and p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def read_claude_session(path: Path) -> tuple[SessionFacts, str | None] | None:
    """One forward pass: compacts need the whole file regardless, so folding
    the "latest assistant turn" search into the same pass costs nothing extra
    — a second, reverse-only pass would only pay for itself if compacts were
    not also required on every call.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        return None

    session_id: str | None = None
    started_at: float | None = None
    compact_events: list[LastCompaction] = []
    boundary_uuids: set[str] = set()
    prompt_tokens: int | None = None
    model: str | None = None

    parsed: list[dict[str, Any]] = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        parsed.append(d)

    for d in parsed:
        if session_id is None:
            sid = d.get("sessionId")
            if isinstance(sid, str) and sid:
                session_id = sid
        if started_at is None:
            t = _parse_iso_utc(d.get("timestamp"))
            if t is not None:
                started_at = t

        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            meta = d.get("compactMetadata") or {}
            uuid_ = d.get("uuid")
            if isinstance(uuid_, str):
                boundary_uuids.add(uuid_)
            compact_events.append(
                LastCompaction(
                    pre=_int_or_none(meta.get("preTokens")),
                    post=_int_or_none(meta.get("postTokens")),
                    at=_parse_iso_utc(d.get("timestamp")),
                )
            )
        elif d.get("isCompactSummary") is True:
            # Second signal, deduplicated: a summary whose parent is a boundary
            # already counted above is the same compaction, not a new one.
            parent = d.get("parentUuid")
            if parent not in boundary_uuids:
                compact_events.append(
                    LastCompaction(pre=None, post=None, at=_parse_iso_utc(d.get("timestamp")))
                )

        if d.get("type") == "assistant" and d.get("isSidechain") is not True:
            msg = d.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(usage, dict):
                prompt_tokens = (
                    _int_or_none(usage.get("input_tokens")) or 0
                ) + (
                    _int_or_none(usage.get("cache_creation_input_tokens")) or 0
                ) + (
                    _int_or_none(usage.get("cache_read_input_tokens")) or 0
                )
                model = msg.get("model") if isinstance(msg.get("model"), str) else model

    last_compaction = compact_events[-1] if compact_events else None
    facts = SessionFacts(
        stem="",  # filled in by the caller
        source="claude",
        session_id=session_id,
        started_at=started_at,
        prompt_tokens=prompt_tokens,
        context_window=None,  # resolved by the caller, which knows the stem
        compacts=len(compact_events),
        last_compaction=last_compaction,
        steerable=None,
    )
    return facts, model


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


# ---------------------------------------------------------------------------
# Codex family
# ---------------------------------------------------------------------------


def codex_workspace_cwd(stem: str) -> str:
    return f"{_WORKSPACE_ROOT}/{stem}"


def _codex_session_meta_cwd(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return None
    try:
        d = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    payload = d.get("payload")
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) else None


def find_latest_codex_session(base: Path, stem: str, *, max_scan: int = 500) -> Path | None:
    """The newest rollout whose recorded ``cwd`` is this stem's Buzz
    workspace — NOT the newest rollout overall, which is frequently a loop
    worker's session on this host."""
    target_cwd = codex_workspace_cwd(stem)
    try:
        candidates = list(base.glob("*/*/*/rollout-*.jsonl"))
    except OSError:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:max_scan]:
        if _codex_session_meta_cwd(path) == target_cwd:
            return path
    return None


def read_codex_session(path: Path) -> SessionFacts | None:
    session_id: str | None = None
    started_at: float | None = None
    prompt_tokens: int | None = None
    context_window: int | None = None

    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                if d.get("type") == "session_meta":
                    payload = d.get("payload")
                    if isinstance(payload, dict):
                        sid = payload.get("session_id") or payload.get("id")
                        if isinstance(sid, str):
                            session_id = sid
                    if started_at is None:
                        started_at = _parse_iso_utc(d.get("timestamp"))
                    continue
                payload = d.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                # `last_token_usage`, never `total_token_usage` — the latter is
                # cumulative across the whole session (measured 30x too high).
                last_usage = info.get("last_token_usage")
                if isinstance(last_usage, dict):
                    tokens = last_usage.get("input_tokens")
                    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
                        prompt_tokens = int(tokens)
                window = info.get("model_context_window")
                if isinstance(window, (int, float)) and not isinstance(window, bool):
                    context_window = int(window)
    except OSError:
        return None

    return SessionFacts(
        stem="",
        source="codex",
        session_id=session_id,
        started_at=started_at,
        prompt_tokens=prompt_tokens,
        context_window=context_window,
        compacts=0,
        last_compaction=None,
        steerable=None,
    )


# ---------------------------------------------------------------------------
# Qwen
# ---------------------------------------------------------------------------


def qwen_chats_dir(base: Path, stem: str) -> Path:
    return base / f"-mnt-data-services-buzz-agent-workspaces-{stem}" / "chats"


def read_qwen_session(path: Path) -> SessionFacts | None:
    session_id: str | None = None
    started_at: float | None = None
    prompt_tokens: int | None = None
    context_window: int | None = None

    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                if session_id is None:
                    sid = d.get("sessionId")
                    if isinstance(sid, str):
                        session_id = sid
                if started_at is None:
                    t = _parse_iso_utc(d.get("timestamp"))
                    if t is not None:
                        started_at = t
                usage = d.get("usageMetadata")
                if isinstance(usage, dict) and isinstance(
                    usage.get("promptTokenCount"), (int, float)
                ):
                    prompt_tokens = int(usage["promptTokenCount"])
                    window = d.get("contextWindowSize")
                    if isinstance(window, (int, float)) and not isinstance(window, bool):
                        context_window = int(window)
    except OSError:
        return None

    return SessionFacts(
        stem="",
        source="qwen",
        session_id=session_id,
        started_at=started_at,
        prompt_tokens=prompt_tokens,
        context_window=context_window,
        compacts=0,
        last_compaction=None,
        steerable=None,
    )


# ---------------------------------------------------------------------------
# Kimi — indexed by workdir, not by directory name
# ---------------------------------------------------------------------------


def find_latest_kimi_session_dir(index_path: Path, stem: str) -> Path | None:
    target = f"{_WORKSPACE_ROOT}/{stem}"
    best_dir: Path | None = None
    best_mtime = -1.0
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("workDir") != target:
                    continue
                session_dir = entry.get("sessionDir")
                if not isinstance(session_dir, str):
                    continue
                wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
                try:
                    mtime = wire.stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_dir = Path(session_dir)
    except OSError:
        return None
    return best_dir


def read_kimi_session(
    session_dir: Path, *, session_id: str | None = None
) -> tuple[SessionFacts, str | None] | None:
    state_path = session_dir / "state.json"
    started_at: float | None = None
    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        started_at = _parse_iso_utc(state.get("createdAt"))
    except (OSError, json.JSONDecodeError):
        pass

    # Only `agents/main` — agent-0/agent-1 are sub-agents with their own
    # mini-context, the same doctrine as Claude's sidechain filter.
    wire_path = session_dir / "agents" / "main" / "wire.jsonl"
    prompt_tokens: int | None = None
    model: str | None = None
    try:
        with wire_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "usage.record":
                    continue
                usage = d.get("usage")
                if not isinstance(usage, dict):
                    continue
                prompt_tokens = (
                    _int_or_none(usage.get("inputOther")) or 0
                ) + (
                    _int_or_none(usage.get("inputCacheRead")) or 0
                ) + (
                    _int_or_none(usage.get("inputCacheCreation")) or 0
                )
                m = d.get("model")
                if isinstance(m, str):
                    model = m
    except OSError:
        return None

    facts = SessionFacts(
        stem="",
        source="kimi",
        session_id=session_id,
        started_at=started_at,
        prompt_tokens=prompt_tokens,
        context_window=None,
        compacts=0,
        last_compaction=None,
        steerable=None,
    )
    return facts, model


# ---------------------------------------------------------------------------
# Grok — URL-encoded workdir directory, joined against a shared log by sid
# ---------------------------------------------------------------------------

_SESSION_STATE_FILES = ("chat_history.jsonl", "events.jsonl", "prompt_context.json")


def grok_session_root(base: Path, stem: str) -> Path:
    encoded = urllib.parse.quote(f"{_WORKSPACE_ROOT}/{stem}", safe="")
    return base / "sessions" / encoded


def find_latest_grok_session_dir(root: Path) -> Path | None:
    try:
        subdirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return None
    if not subdirs:
        return None
    return max(subdirs, key=lambda p: p.stat().st_mtime)


def _grok_session_started_at(session_dir: Path) -> float | None:
    """The oldest mtime among the session's own state files — the moment the
    session was created, not the moment it was last touched."""
    mtimes = []
    for name in _SESSION_STATE_FILES:
        try:
            mtimes.append((session_dir / name).stat().st_mtime)
        except OSError:
            continue
    return min(mtimes) if mtimes else None


def _reversed_lines(path: Path) -> Iterable[str]:
    """Newest-line-first, so a caller that only wants the latest match can
    break out without paying to parse the whole (up to several MB) file."""
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    return reversed(lines)


def read_grok_session(
    session_dir: Path, unified_log_path: Path, *, session_id: str | None = None
) -> SessionFacts | None:
    sid = session_id or session_dir.name
    started_at = _grok_session_started_at(session_dir)

    prompt_tokens: int | None = None
    try:
        for raw in _reversed_lines(unified_log_path):
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("msg") != "shell.turn.inference_done" or d.get("sid") != sid:
                continue
            ctx = d.get("ctx")
            if isinstance(ctx, dict) and isinstance(ctx.get("prompt_tokens"), (int, float)):
                prompt_tokens = int(ctx["prompt_tokens"])
            break
    except OSError:
        pass

    return SessionFacts(
        stem="",
        source="grok",
        session_id=sid,
        started_at=started_at,
        prompt_tokens=prompt_tokens,
        context_window=None,
        compacts=0,
        last_compaction=None,
        steerable=None,
    )


# ---------------------------------------------------------------------------
# Steering (all stems, including source="none" ones — the journal exists
# independently of any CLI session store)
# ---------------------------------------------------------------------------


def steering_command(stem: str) -> list[str]:
    return [
        "journalctl",
        "--user",
        "-u",
        f"buzz-agent@{stem}.service",
        "--no-pager",
        "-o",
        "json",
        "--output-fields=_SYSTEMD_USER_UNIT,MESSAGE",
        "-g",
        "steering_supported",
        "--reverse",
        "-n",
        "1",
    ]


def identity_command(stem: str) -> list[str]:
    """The journal command that yields the agent's nostr identity.

    Same shape as :func:`steering_command`, and for the same reason: the
    journal is the only source.

    Twenty lines rather than one: `-g` matches the *text* of a log line, and
    agents that grep their own logs reproduce the banner's wording inside a
    tool call. Measured on `codex`, whose newest match was such an echo — with
    `-n 1` its real identity would simply have been missing. The strict regex
    in :data:`_IDENTITY_RE` then picks the first genuine banner out of the
    twenty.
    """
    return [
        "journalctl",
        "--user",
        "-u",
        f"buzz-agent@{stem}.service",
        "--no-pager",
        "-o",
        "json",
        "--output-fields=_SYSTEMD_USER_UNIT,MESSAGE",
        "-g",
        "buzz-acp starting",
        "--reverse",
        "-n",
        "20",
    ]


def parse_identity(stdout: str) -> tuple[str | None, str | None]:
    """``(pubkey, subscribe_mode)`` from the newest startup banner.

    Returns ``(None, None)`` when the banner is absent — an agent whose
    identity cannot be read must not get a button that promises delivery.
    """
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        message = decode_message(entry.get("MESSAGE"))
        if message is None:
            continue
        plain = _ANSI_RE.sub("", message)
        identity = _IDENTITY_RE.search(plain)
        if identity is None:
            continue
        subscribe = _SUBSCRIBE_RE.search(plain)
        return identity.group(1), (subscribe.group(1) if subscribe else None)
    return None, None


def parse_steering(stdout: str) -> bool | None:
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        message = decode_message(entry.get("MESSAGE"))
        if message is None:
            continue
        plain = _ANSI_RE.sub("", message)
        match = _STEERING_RE.search(plain)
        if match is not None:
            return match.group(1) == "true"
    return None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class SessionFactsService:
    """Assembles :class:`SessionFacts` for a Buzz agent stem, cached briefly
    because the phone polls every couple of seconds and every source here is
    a filesystem/journal read."""

    def __init__(
        self,
        *,
        claude_projects_dir: Path | None = None,
        codex_sessions_dir: Path | None = None,
        qwen_projects_dir: Path | None = None,
        kimi_session_index: Path | None = None,
        grok_home: Path | None = None,
        buzz_agents_config_dir: Path | None = None,
        runner: Runner | None = None,
        context_window_resolver: ContextWindowResolver = _default_context_window_resolver,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        home = Path.home()
        self._claude_projects_dir = claude_projects_dir or (home / ".claude" / "projects")
        self._codex_sessions_dir = codex_sessions_dir or (home / ".codex" / "sessions")
        self._qwen_projects_dir = qwen_projects_dir or (home / ".qwen" / "projects")
        self._kimi_session_index = kimi_session_index or (
            home / ".kimi-code" / "session_index.jsonl"
        )
        self._grok_home = grok_home or (home / ".grok")
        self._buzz_agents_config_dir = buzz_agents_config_dir or (
            home / ".config" / "buzz-agents"
        )
        self._runner: Runner = runner or SubprocessRunner()
        self._resolve_context_window = context_window_resolver
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, SessionFacts]] = {}

    # -- public --------------------------------------------------------

    def facts_for_stem(self, stem: str) -> SessionFacts:
        now = self._clock()
        cached = self._cache.get(stem)
        if cached is not None and now - cached[0] < self._ttl_seconds:
            return cached[1]
        facts = self._compute(stem)
        self._cache[stem] = (now, facts)
        return facts

    def facts_for_stems(self, stems: Iterable[str]) -> dict[str, SessionFacts]:
        return {stem: self.facts_for_stem(stem) for stem in stems}

    # -- per-source dispatch --------------------------------------------

    def _compute(self, stem: str) -> SessionFacts:
        steerable = self._read_steerable(stem)
        pubkey, subscribe = self._read_identity(stem)

        if stem in CLAUDE_STEMS:
            facts = self._compute_claude(stem)
        elif stem in CODEX_STEMS:
            facts = self._compute_codex(stem)
        elif stem in QWEN_STEMS:
            facts = self._compute_qwen(stem)
        elif stem in KIMI_STEMS:
            facts = self._compute_kimi(stem)
        elif stem in GROK_STEMS:
            facts = self._compute_grok(stem)
        else:
            facts = None

        if facts is None:
            return SessionFacts.none(
                stem, steerable=steerable, pubkey=pubkey, subscribe=subscribe
            )
        return SessionFacts(
            stem=stem,
            source=facts.source,
            session_id=facts.session_id,
            started_at=facts.started_at,
            prompt_tokens=facts.prompt_tokens,
            context_window=facts.context_window,
            compacts=facts.compacts,
            last_compaction=facts.last_compaction,
            steerable=steerable,
            pubkey=pubkey,
            subscribe=subscribe,
        )

    def _compute_claude(self, stem: str) -> SessionFacts | None:
        # Exact directory-name match — never a suffix/`endswith` match. A
        # scratch workspace directory
        # (`…workspaces-claude--scratch-a2-fable`) would otherwise book
        # Claude's scratch session onto `fable`.
        directory = self._claude_projects_dir / claude_dir_name(stem)
        latest = _latest_jsonl(directory)
        if latest is None:
            return None
        result = read_claude_session(latest)
        if result is None:
            return None
        facts, model = result
        context_window = None
        if model:
            context_window = self._resolve_context_window(_CONTEXT_WINDOW_PROVIDER[stem], model)
        return SessionFacts(
            stem=stem,
            source=facts.source,
            session_id=facts.session_id,
            started_at=facts.started_at,
            prompt_tokens=facts.prompt_tokens,
            context_window=context_window,
            compacts=facts.compacts,
            last_compaction=facts.last_compaction,
        )

    def _compute_codex(self, stem: str) -> SessionFacts | None:
        latest = find_latest_codex_session(self._codex_sessions_dir, stem)
        if latest is None:
            return None
        return read_codex_session(latest)

    def _compute_qwen(self, stem: str) -> SessionFacts | None:
        directory = qwen_chats_dir(self._qwen_projects_dir, stem)
        latest = _latest_jsonl(directory)
        if latest is None:
            return None
        return read_qwen_session(latest)

    def _compute_kimi(self, stem: str) -> SessionFacts | None:
        session_dir = find_latest_kimi_session_dir(self._kimi_session_index, stem)
        if session_dir is None:
            return None
        session_id = _kimi_session_id_for_dir(self._kimi_session_index, session_dir)
        result = read_kimi_session(session_dir, session_id=session_id)
        if result is None:
            return None
        facts, model = result
        context_window = None
        if model:
            context_window = self._resolve_context_window(
                _CONTEXT_WINDOW_PROVIDER["kimi"], _kimi_model_id(model)
            )
        return SessionFacts(
            stem=stem,
            source=facts.source,
            session_id=facts.session_id,
            started_at=facts.started_at,
            prompt_tokens=facts.prompt_tokens,
            context_window=context_window,
            compacts=facts.compacts,
            last_compaction=facts.last_compaction,
        )

    def _compute_grok(self, stem: str) -> SessionFacts | None:
        root = grok_session_root(self._grok_home, stem)
        session_dir = find_latest_grok_session_dir(root)
        if session_dir is None:
            return None
        unified_log = self._grok_home / "logs" / "unified.jsonl"
        facts = read_grok_session(session_dir, unified_log)
        if facts is None:
            return None
        model = _read_buzz_acp_model(self._buzz_agents_config_dir, stem)
        context_window = None
        if model:
            context_window = self._resolve_context_window(_CONTEXT_WINDOW_PROVIDER["grok"], model)
        return SessionFacts(
            stem=stem,
            source=facts.source,
            session_id=facts.session_id,
            started_at=facts.started_at,
            prompt_tokens=facts.prompt_tokens,
            context_window=context_window,
            compacts=facts.compacts,
            last_compaction=facts.last_compaction,
        )

    def _read_identity(self, stem: str) -> tuple[str | None, str | None]:
        result = self._runner.run(identity_command(stem), timeout=10.0)
        if result.returncode != 0:
            return None, None
        return parse_identity(result.stdout)

    def _read_steerable(self, stem: str) -> bool | None:
        result = self._runner.run(steering_command(stem), timeout=10.0)
        if result.returncode != 0:
            return None
        return parse_steering(result.stdout)


def _kimi_session_id_for_dir(index_path: Path, session_dir: Path) -> str | None:
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("sessionDir") == str(session_dir):
                    sid = entry.get("sessionId")
                    return sid if isinstance(sid, str) else None
    except OSError:
        return None
    return None


_ENV_MODEL_RE = re.compile(r'^BUZZ_ACP_MODEL=(.*)$')


def _read_buzz_acp_model(config_dir: Path, stem: str) -> str | None:
    path = config_dir / f"{stem}.env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = _ENV_MODEL_RE.match(line.strip())
        if match is None:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None

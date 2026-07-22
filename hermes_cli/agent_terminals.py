"""Allowlisted tmux backend for dashboard agent terminals.

This module intentionally keeps browser input away from command construction:
clients choose an agent kind/window, while argv/cwd/env are resolved here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hermes_cli.config import get_hermes_home
from hermes_cli.projects_overview import load_projects_registry
from hermes_constants import terminal_runs_root
from utils import atomic_json_write

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - tmux is Unix-only; import safety for Windows.
    _fcntl = None

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|AUTH)", re.IGNORECASE)
_TRAILING_NUMBER_RE = re.compile(r"-\d+$")
_MAX_NUMBERED_WINDOWS = 9
_EPHEMERAL_ATTACH_PREFIX = "__hermes_attach_"
_EPHEMERAL_ATTACH_MARKER = "@hermes_ephemeral_attach"
_EPHEMERAL_ATTACH_SOURCE = "@hermes_attach_source"
_EPHEMERAL_ATTACH_WINDOW = "@hermes_attach_window"
_EPHEMERAL_ATTACH_CREATED_AT = "@hermes_attach_created_at"
_EPHEMERAL_ATTACH_GRACE_SECONDS = 60
_TMUX_RUN_TIMEOUT_SECONDS = 10
_AUTO_CAPTURE_START = -25
_AUTO_CAPTURE_TTL_SECONDS = 2.0
_AUTO_CAPTURE_CACHE_MAX_ENTRIES = 128
_AUTO_CAPTURE_VARIANT = "question-v1"
_EVENT_LOG_MAX_BYTES = 256 * 1024
_EVENT_LOG_GENERATIONS = 3
_EVENT_LOG_LOCK = threading.Lock()
_TERMINAL_RUN_ID_OPTION = "@hermes_terminal_run_id"
_START_MODE_OPTION = "@hermes_start_mode"
_CONTEXT_PROFILE_OPTION = "@hermes_context_profile"
_BASE_SHA_OPTION = "@hermes_base_sha"
_NATIVE_SESSION_OPTION = "@hermes_native_session_id"
_WORKTREE_PATH_OPTION = "@hermes_worktree_path"
_WORKTREE_BRANCH_OPTION = "@hermes_worktree_branch"
_CWD_OPTION = "@hermes_cwd"
_START_MODE_FREE = "free"
_START_MODE_ISOLATED_WRITE = "isolated_write"
_CONTEXT_PROFILE_FULL = "full"
_CONTEXT_PROFILE_LEAN = "lean"
_ACTION_FRESH = "fresh"
_ACTION_RESUME = "resume"
_ACTION_FORK = "fork"
_ACTION_LEAN = "lean"
_TERMINAL_MANIFEST_SCHEMA_VERSION = 1
_TERMINAL_WORKTREE_DIRNAME = "terminal"
_TERMINAL_WORKTREE_GROUP = "terminal_worktree"
_TERMINAL_PRUNE_MIN_AGE_SECONDS = 7 * 24 * 60 * 60
_VALID_START_MODES = frozenset({_START_MODE_FREE, _START_MODE_ISOLATED_WRITE})
_VALID_CONTEXT_PROFILES = frozenset({_CONTEXT_PROFILE_FULL, _CONTEXT_PROFILE_LEAN})
_VALID_RESPAWN_ACTIONS = frozenset({_ACTION_FRESH, _ACTION_RESUME, _ACTION_FORK})
# Closed capability matrix: only proven installed CLI semantics are enabled.
# Lean is a start-time context profile, never a free-form flag approximation.
_AGENT_CONTEXT_ACTIONS: dict[str, dict[str, bool]] = {
    "hermes": {
        "fresh": True,
        "resume": False,
        "fork": False,
        "lean": False,
        "compact": False,
    },
    "claude": {
        "fresh": True,
        "resume": True,
        "fork": False,
        "lean": False,
        "compact": False,
    },
    "codex": {
        "fresh": True,
        "resume": True,
        "fork": False,
        "lean": True,
        "compact": False,
    },
    "kimi": {
        "fresh": True,
        "resume": False,
        "fork": False,
        "lean": False,
        "compact": False,
    },
    "grok": {
        "fresh": True,
        "resume": False,
        "fork": False,
        "lean": False,
        "compact": False,
    },
    "qwen": {
        "fresh": True,
        "resume": False,
        "fork": False,
        "lean": False,
        "compact": False,
    },
}

# Substrings in tmux stderr that mean "target/server is gone" (case-insensitive).
# Used by window_exists / show / idempotent kill to distinguish not-found from
# transient socket failures that must surface as AgentTerminalError (503).
# Keep markers specific: bare "no such" would false-match unrelated messages;
# "no such file" is only treated as gone when paired with "error connecting"
# (socket path not yet created — first ensure() before any server).
_TMUX_GONE_MARKERS: tuple[str, ...] = (
    "can't find",
    "no such window",
    "no such pane",
    "no such session",
    "no server running",
)

_AGENT_KINDS: tuple[str, ...] = ("hermes", "claude", "codex", "kimi", "grok", "qwen")

# ----- ANSI stripping --------------------------------------------------------
# Order matters: OSC sequences also start with ESC, so they must be stripped
# before the generic single-ESC catch-all would otherwise mangle them.
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_ESC_RE = re.compile(r"\x1b.")


def strip_ansi(text: str) -> str:
    """Strip CSI/OSC/simple ESC sequences from captured tmux pane text."""
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_ESC_RE.sub("", text)
    return text


# ----- agent pane classification ---------------------------------------------
# Precedence order matches the numbered rules this function implements below;
# do not reorder without re-checking the fixtures in test_agent_terminals.py.
_QUESTION_YN_RE = re.compile(r"y/n", re.IGNORECASE)
_QUESTION_ALLOW_RE = re.compile(r"\ballow\b", re.IGNORECASE)
_QUESTION_DO_YOU_WANT_RE = re.compile(r"do you want", re.IGNORECASE)
_QUESTION_PRESS_ENTER_RE = re.compile(r"press enter to", re.IGNORECASE)
_QUESTION_NUMBERED_RE = re.compile(r"[❯›]\s*\d+\.")


def _is_question_window(lines: Sequence[str], last_non_empty: str) -> bool:
    window = "\n".join(lines)
    if _QUESTION_YN_RE.search(window):
        return True
    if _QUESTION_DO_YOU_WANT_RE.search(window):
        return True
    if _QUESTION_ALLOW_RE.search(window):
        return True
    if _QUESTION_NUMBERED_RE.search(window):
        return True
    if _QUESTION_PRESS_ENTER_RE.search(window):
        return True
    return last_non_empty.rstrip().endswith("?")


def _is_running_window(lines: Sequence[str], activity_age_s: float | None) -> bool:
    window = "\n".join(lines)
    if "esc to interrupt" in window.lower():
        return True
    if "Working (" in window:
        return True
    return activity_age_s is not None and activity_age_s < 15


def _is_prompt_marker_line(line: str) -> bool:
    stripped = line.lstrip()
    if stripped.startswith("❯") or stripped.startswith("›"):
        return True
    if "│ >" in line:
        return True
    return "─ ready │" in line


def classify_agent_pane(tail: str, activity_age_s: float | None, dead: bool) -> str:
    """Heuristische Zustands-Klassifikation eines Agent-Panes.

    Rückgabe: "dead" | "frage" | "laeuft" | "wartet" | "idle".
    """
    if dead:
        return "dead"

    lines = (tail or "").splitlines()
    non_empty = [line for line in lines if line.strip()]
    last_non_empty = non_empty[-1] if non_empty else ""
    recent_window = lines[-8:]

    if _is_question_window(recent_window, last_non_empty):
        return "frage"

    if _is_running_window(recent_window, activity_age_s):
        return "laeuft"

    if any(_is_prompt_marker_line(line) for line in non_empty[-3:]):
        if activity_age_s is None or activity_age_s < 1800:
            return "wartet"
        return "idle"

    if activity_age_s is not None and activity_age_s < 60:
        return "laeuft"
    return "idle"


# Workdir allowlist: browser clients pick a key, the path is resolved here.
# (key, German label, path parts under $HOME, window-name suffix or None)
_WORKDIR_DEFS: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("home", "Zuhause (~)", (), None),
    ("hermes-agent", "Hermes-Agent", (".hermes", "hermes-agent"), "agent"),
    ("family-organizer", "Family Organizer", ("projects", "family-organizer"), "fo"),
    ("orchestration", "Orchestrierung", ("orchestration",), "orch"),
)
_WORKDIR_BY_KEY = {key: (label, parts, suffix) for key, label, parts, suffix in _WORKDIR_DEFS}
_WORKDIR_KEY_BY_SUFFIX = {suffix: key for key, _, _, suffix in _WORKDIR_DEFS if suffix}
_WORKDIR_CACHE_TTL_SECONDS = 5.0
_WORKTREE_LIMIT = 15
_WORKDIR_CACHE_LOCK = threading.Lock()
_workdir_cache: tuple[float, list[dict[str, object]]] | None = None


def _normalise_path(path: Path) -> Path:
    """Return a stable absolute path without requiring the target to exist."""
    return path.expanduser().resolve(strict=False)


def _parse_worktree_porcelain(output: str) -> list[tuple[Path, str | None]]:
    """Parse ``git worktree list --porcelain`` records."""
    records: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*output.splitlines(), ""]:
        if not line:
            if path is not None:
                records.append((path, branch))
            path = None
            branch = None
        elif line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
    return records


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _reset_workdir_options_cache() -> None:
    """Test hook: force the next workdir enumeration to rescan live state."""
    global _workdir_cache
    with _WORKDIR_CACHE_LOCK:
        _workdir_cache = None


class AgentTerminalError(RuntimeError):
    """Base exception for tmux agent terminal operations."""


class InvalidTarget(AgentTerminalError):
    """Raised when a user supplied tmux session/window/client name is unsafe."""


class CapabilityError(AgentTerminalError):
    """Raised when a requested backend capability is unavailable."""


@dataclass(frozen=True)
class CapabilityState:
    tmux_available: bool
    hermes_tui_available: bool
    hermes_binary: str | None
    reason: str | None = None
    agents: Mapping[str, Mapping[str, object]] | None = None
    workdirs: Sequence[Mapping[str, object]] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "tmux_available": self.tmux_available,
            "hermes_tui_available": self.hermes_tui_available,
            "hermes_binary": self.hermes_binary,
            "reason": self.reason,
            "agents": dict(self.agents) if self.agents is not None else {},
            "workdirs": list(self.workdirs) if self.workdirs is not None else [],
        }


@dataclass(frozen=True)
class AgentWindowDefinition:
    kind: str
    session: str
    window: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    workdir_key: str


@dataclass(frozen=True)
class IsolatedAttachTarget:
    source_session: str
    source_window: str
    session: str
    window: str


@dataclass(frozen=True)
class TerminalLaunchContext:
    """Server-controlled launch identity for one TMAX window.

    Browser payloads may select kind/workdir/start_mode/context_profile/action,
    but never options or argv. The service stamps these fields into tmux options
    and a profile-independent terminal-run manifest.
    """

    terminal_run_id: str
    agent_kind: str
    start_mode: str
    context_profile: str
    cwd: str
    base_sha: str | None = None
    native_session_id: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    action: str = _ACTION_FRESH
    capsule_correlation_id: str | None = None
    argv: tuple[str, ...] = ()

    def to_manifest(self, *, window: str, session: str, status: str = "running") -> dict[str, object]:
        return {
            "schema_version": _TERMINAL_MANIFEST_SCHEMA_VERSION,
            "terminal_run_id": self.terminal_run_id,
            "agent_kind": self.agent_kind,
            "start_mode": self.start_mode,
            "context_profile": self.context_profile,
            "cwd": self.cwd,
            "base_sha": self.base_sha,
            "native_session_id": self.native_session_id,
            # Free mode always records an explicit null worktree path.
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
            "action": self.action,
            "argv": list(self.argv),
            "window": window,
            "session": session,
            "status": status,
            "capsule_correlation_id": self.capsule_correlation_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


@dataclass(frozen=True)
class TmuxWindow:
    session: str
    window: str
    active: bool
    pane_id: str
    pid: int | None
    command: str
    cwd: str | None = None
    dead: bool = False
    activity: int | None = None
    # Whether terminate_live() will accept this window. Additive inventory field:
    # True = dashboard-managed (session==work + resolvable identity); False =
    # foreign (visible/attachable, but close would 503); None = unknown (callers
    # that did not compute it — safe default so older constructors stay valid).
    managed: bool | None = None
    task_id: str | None = None
    run_id: int | None = None
    correlation_id: str | None = None
    terminal_run_id: str | None = None
    start_mode: str | None = None
    context_profile: str | None = None
    base_sha: str | None = None
    native_session_id: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "window": self.window,
            "active": self.active,
            "pane_id": self.pane_id,
            "pid": self.pid,
            "command": self.command,
            "cwd": self.cwd,
            "dead": self.dead,
            "activity": self.activity,
            "managed": self.managed,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "terminal_run_id": self.terminal_run_id,
            "start_mode": self.start_mode,
            "context_profile": self.context_profile,
            "base_sha": self.base_sha,
            "native_session_id": self.native_session_id,
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
        }


@dataclass(frozen=True)
class TerminalSnapshot:
    """Canonical raw pane capture shared by automatic readers.

    ``raw`` is intentionally not normalized or truncated: overview rendering and
    question parsing derive their own views from the same exact 25-line capture.
    """

    pane_id: str
    window_activity: int
    captured_at: float
    raw: str
    variant: str


class PaneCaptureCache:
    """Thread-safe, bounded LRU with per-generation single-flight captures."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _AUTO_CAPTURE_TTL_SECONDS,
        max_entries: int = _AUTO_CAPTURE_CACHE_MAX_ENTRIES,
    ) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._entries: OrderedDict[
            tuple[int, str, str, int, str], TerminalSnapshot
        ] = OrderedDict()
        self._inflight: dict[
            tuple[int, str, str, int, str], threading.Event
        ] = {}
        # A send/interrupt can race a capture whose tmux activity timestamp is
        # still in the same whole second. The generation makes such an
        # in-flight result permanently non-reusable after invalidation.
        self._generation = 0

    def get_or_capture(
        self,
        *,
        server_id: str,
        pane_id: str,
        window_activity: int | None,
        variant: str,
        now: float,
        capture: Callable[[], str],
        clock: Callable[[], float] | None = None,
    ) -> TerminalSnapshot:
        """Return a reusable snapshot, or capture once for concurrent readers.

        tmux reports activity in whole seconds. An activity value from the current
        second may still change without changing the cache key, so it is never
        cacheable. Missing activity is likewise captured fresh.
        """
        activity = int(window_activity) if window_activity is not None else None
        cacheable = activity is not None and activity < int(now)
        if not cacheable:
            raw = capture()
            return TerminalSnapshot(
                pane_id=pane_id,
                window_activity=activity if activity is not None else int(now),
                captured_at=float(clock()) if clock is not None else now,
                raw=raw,
                variant=variant,
            )

        while True:
            leader = False
            with self._lock:
                generation = self._generation
                key = (generation, server_id, pane_id, activity, variant)
                existing = self._entries.get(key)
                if existing is not None:
                    age = now - existing.captured_at
                    if 0.0 <= age <= self.ttl_seconds:
                        self._entries.move_to_end(key)
                        return existing
                    del self._entries[key]
                pending = self._inflight.get(key)
                if pending is None:
                    pending = threading.Event()
                    self._inflight[key] = pending
                    leader = True
            if leader:
                break
            # A failed leader wakes waiters too; one waiter then becomes the next
            # leader and performs a fresh capture instead of caching an exception.
            pending.wait(_TMUX_RUN_TIMEOUT_SECONDS + 1)
            if clock is not None:
                now = float(clock())

        try:
            raw = capture()
            snapshot = TerminalSnapshot(
                pane_id=pane_id,
                window_activity=activity,
                captured_at=float(clock()) if clock is not None else now,
                raw=raw,
                variant=variant,
            )
            with self._lock:
                # If a send/interrupt invalidated the cache while tmux was
                # capturing, return this snapshot only to its original caller;
                # never publish it for a later automatic reader.
                if self._generation == generation:
                    self._entries[key] = snapshot
                    self._entries.move_to_end(key)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
            return snapshot
        finally:
            with self._lock:
                finished = self._inflight.pop(key, None)
                if finished is not None:
                    finished.set()

    def invalidate_pane(self, server_id: str, pane_id: str) -> None:
        with self._lock:
            # Deliberately global and conservative: invalidations are rare,
            # while a process-wide generation makes in-flight races impossible
            # without retaining an unbounded per-pane version map.
            self._generation += 1
            self._entries.clear()

    def invalidate_server(self, server_id: str) -> None:
        with self._lock:
            self._generation += 1
            self._entries.clear()

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._entries.clear()


_PANE_CAPTURE_CACHE = PaneCaptureCache()


def _reset_pane_capture_cache() -> None:
    """Test hook: discard all process-wide automatic terminal snapshots."""
    _PANE_CAPTURE_CACHE.clear()


class TmuxAgentSessionService:
    """Small tmux façade for allowlisted dashboard agent windows."""

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        tmux_binary: str | None = None,
        hermes_binary: str | Path | None = None,
        hermes_home: str | Path | None = None,
        now=time.time,
        capture_cache: PaneCaptureCache | None = None,
    ) -> None:
        self.tmux_binary = tmux_binary or shutil.which("tmux") or "tmux"
        self.socket_path = Path(socket_path) if socket_path else None
        self.hermes_binary_override = Path(hermes_binary) if hermes_binary else None
        self.hermes_home = Path(hermes_home) if hermes_home else get_hermes_home()
        self.log_dir = self.hermes_home / "agent-terminals"
        self._now = now
        self._capture_cache = capture_cache or _PANE_CAPTURE_CACHE

    @property
    def _capture_server_id(self) -> str:
        socket = (
            str(self.socket_path.expanduser().resolve(strict=False))
            if self.socket_path is not None
            else "<default>"
        )
        return f"{self.tmux_binary}\0{socket}"

    @property
    def execution_server_id(self) -> str:
        """Opaque local tmux-server identity safe to persist in run metadata."""
        return hashlib.sha256(self._capture_server_id.encode("utf-8")).hexdigest()

    # ----- validation / command helpers ---------------------------------
    @staticmethod
    def validate_name(value: str, *, field: str = "target") -> str:
        if not _SAFE_NAME_RE.fullmatch(value or "") or value.startswith("-"):
            raise InvalidTarget(f"invalid {field}: {value!r}")
        return value

    def _target(self, session: str, window: str | None = None) -> str:
        session = self.validate_name(session, field="session")
        if window is None:
            return session
        window = self.validate_name(window, field="window")
        return f"{session}:{window}"

    def _cmd_target(self, session: str, window: str) -> str:
        """Exact-match tmux target (`sess:=win`) for command invocations.

        Without `=`, tmux fuzzy-matches window names and some commands
        (display-message) even fall back to the session's current window, so
        keys could land in the wrong pane.
        """
        session = self.validate_name(session, field="session")
        window = self.validate_name(window, field="window")
        return f"{session}:={window}"

    def _tmux_cmd(self, *args: str) -> list[str]:
        cmd = [self.tmux_binary]
        if self.socket_path is not None:
            cmd.extend(["-S", str(self.socket_path)])
        cmd.extend(args)
        return cmd

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self._tmux_cmd(*args),
                check=check,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_TMUX_RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTerminalError(f"tmux command timed out: {exc}") from exc

    @staticmethod
    def _is_tmux_gone_message(stderr: str | None) -> bool:
        """True when tmux stderr indicates the target/server is already gone.

        Distinct from transient connect failures (permission, busy socket): those
        must raise so idempotent close paths cannot report success without a kill.
        A missing socket file ("error connecting" + "no such file") is the cold
        start case — no server yet — and counts as gone so ensure/kill-dead work
        before the first new-session.
        """
        text = (stderr or "").lower()
        if any(marker in text for marker in _TMUX_GONE_MARKERS):
            return True
        # Socket path not created yet (first use of a dedicated -S path).
        if "error connecting" in text and "no such file" in text:
            return True
        return False

    @staticmethod
    def _safe_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
        base: dict[str, str] = {}
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM"):
            if key in os.environ:
                base[key] = os.environ[key]
        if env:
            for key, value in env.items():
                if _SECRET_KEY_RE.search(key):
                    continue
                base[str(key)] = str(value)
        base.setdefault("TERM", "xterm-256color")
        return base

    def _log_event(self, event: str, **fields: object) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / "events.jsonl"
        record = {"ts": self._now(), "event": event, **fields}
        # Bounded metadata log: do not persist pane buffers or typed text.
        line = json.dumps(record, sort_keys=True) + "\n"
        encoded_size = len(line.encode("utf-8"))
        with _EVENT_LOG_LOCK:
            try:
                lock_path = self.log_dir / ".events.lock"
                with lock_path.open("a", encoding="utf-8") as lock_handle:
                    if _fcntl is not None:
                        _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
                    try:
                        current_size = path.stat().st_size if path.exists() else 0
                        if (
                            current_size
                            and current_size + encoded_size > _EVENT_LOG_MAX_BYTES
                        ):
                            for generation in range(
                                _EVENT_LOG_GENERATIONS - 1, 0, -1
                            ):
                                source = (
                                    path
                                    if generation == 1
                                    else path.with_name(
                                        f"{path.name}.{generation - 1}"
                                    )
                                )
                                target = path.with_name(
                                    f"{path.name}.{generation}"
                                )
                                if source.exists():
                                    os.replace(source, target)
                        with path.open("a", encoding="utf-8") as handle:
                            handle.write(line)
                    finally:
                        if _fcntl is not None:
                            _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_UN)
            except OSError:
                # Terminal control must remain available even if its metadata log
                # cannot be rotated or written.
                pass

    # ----- capabilities / definitions -----------------------------------
    def _discover_hermes_binary(self) -> Path | None:
        discovered = shutil.which("hermes")
        if discovered:
            return Path(discovered)
        candidates: list[Path] = []
        if self.hermes_home.name == ".hermes":
            candidates.append(self.hermes_home.parent / ".local" / "bin" / "hermes")
        candidates.extend([Path("/usr/local/bin/hermes"), Path("/usr/bin/hermes")])
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _validated_cli_binary(candidate: Path, *, name: str) -> Path:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CapabilityError(f"{name} binary is not resolvable: {candidate}") from exc
        if not os.access(resolved, os.X_OK):
            raise CapabilityError(f"{name} binary is not executable: {resolved}")
        # Reject task-local/transient worktree launchers; dashboard agent windows
        # should survive worktree cleanup and use a stable installed CLI.
        if "/.worktrees/" in str(resolved):
            raise CapabilityError(f"{name} binary resolves to transient worktree: {resolved}")
        return resolved

    def resolve_hermes_binary(self) -> Path:
        if self.hermes_binary_override is not None:
            candidate = self.hermes_binary_override
        else:
            discovered = self._discover_hermes_binary()
            if discovered is None:
                raise CapabilityError("hermes binary not found on PATH or standard install locations")
            candidate = discovered
        if not str(candidate):
            raise CapabilityError("hermes binary not found on PATH or standard install locations")
        return self._validated_cli_binary(candidate, name="hermes")

    @staticmethod
    def _agent_binary_candidates(kind: str) -> tuple[tuple[str, ...], tuple[Path, ...]]:
        """(which-names, fallback paths) per agent kind — argv stays server-side."""
        home = Path.home()
        if kind == "claude":
            return ("claude",), (home / ".local" / "bin" / "claude",)
        if kind == "codex":
            return ("codex",), (home / ".local" / "bin" / "codex", Path("/usr/local/bin/codex"))
        if kind == "kimi":
            return ("kimi-code", "kimi"), (
                home / ".kimi-code" / "bin" / "kimi",
                home / ".local" / "opt" / "kimi-code" / "bin" / "kimi",
            )
        if kind == "grok":
            return ("grok",), (
                home / ".npm-global" / "bin" / "grok",
                home / ".local" / "bin" / "grok",
            )
        if kind == "qwen":
            return ("qwen",), (
                home / ".npm-global" / "bin" / "qwen",
                home / ".local" / "bin" / "qwen",
            )
        raise InvalidTarget(f"unknown agent kind: {kind}")

    def resolve_agent_binary(self, kind: str) -> Path:
        kind = self.validate_name(kind, field="kind")
        if kind == "hermes":
            return self.resolve_hermes_binary()
        which_names, fallbacks = self._agent_binary_candidates(kind)
        for name in which_names:
            discovered = shutil.which(name)
            if discovered:
                return self._validated_cli_binary(Path(discovered), name=kind)
        for candidate in fallbacks:
            if candidate.exists():
                return self._validated_cli_binary(candidate, name=kind)
        raise CapabilityError(f"{kind} CLI not found on PATH or standard install locations")

    @staticmethod
    def _enumerate_workdir_options() -> list[dict[str, object]]:
        home = Path.home()
        options: list[dict[str, object]] = []
        known_paths: set[Path] = set()
        repos: list[tuple[Path, str]] = []
        for key, label, parts, _suffix in _WORKDIR_DEFS:
            path = home.joinpath(*parts)
            if path.is_dir():
                normalised = _normalise_path(path)
                known_paths.add(normalised)
                repos.append((normalised, label))
                options.append(
                    {"key": key, "label": label, "path": str(path), "group": "standard"}
                )

        registry = load_projects_registry(home=get_hermes_home())
        for project in registry.projects:
            path = _normalise_path(Path(project.repo_path))
            if not path.is_dir() or path in known_paths:
                continue
            known_paths.add(path)
            repos.append((path, project.name))
            options.append(
                {
                    "key": f"dir:{path}",
                    "label": project.name,
                    "path": str(path),
                    "group": "projekt",
                }
            )

        worktrees: dict[Path, tuple[str, str]] = {}
        for repo, project_name in repos:
            result = _run_git(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"]
            )
            if result is None or result.returncode != 0:
                continue
            for path, branch in _parse_worktree_porcelain(result.stdout):
                normalised = _normalise_path(path)
                if normalised == repo or normalised in known_paths or not normalised.is_dir():
                    continue
                branch_label = branch or normalised.name
                worktrees.setdefault(normalised, (project_name, branch_label))

        free_root = home / ".hermes" / "worktrees"
        if free_root.is_dir():
            for candidate in free_root.iterdir():
                normalised = _normalise_path(candidate)
                if not candidate.is_dir() or normalised in known_paths or normalised in worktrees:
                    continue
                result = _run_git(["git", "-C", str(candidate), "rev-parse", "--git-dir"])
                if result is None or result.returncode != 0:
                    continue
                branch_result = _run_git(
                    ["git", "-C", str(candidate), "branch", "--show-current"]
                )
                branch = (
                    branch_result.stdout.strip()
                    if branch_result is not None and branch_result.returncode == 0
                    else ""
                )
                worktrees[normalised] = (candidate.name, branch or candidate.name)

        def worktree_mtime(item: tuple[Path, tuple[str, str]]) -> float:
            try:
                return item[0].stat().st_mtime
            except OSError:
                return 0.0

        for path, (project_name, branch) in sorted(
            worktrees.items(), key=worktree_mtime, reverse=True
        ):
            # Terminal isolated-write worktrees use a separate picker group and
            # must not consume the regular fifteen worktree slots.
            if ".worktrees/terminal/" in f"{path.as_posix()}/" or path.parent.name == _TERMINAL_WORKTREE_DIRNAME and path.parent.parent.name == ".worktrees":
                continue
            options.append(
                {
                    "key": f"dir:{path}",
                    "label": f"{project_name} · {branch}",
                    "path": str(path),
                    "group": "worktree",
                }
            )
            if sum(1 for opt in options if opt.get("group") == "worktree") >= _WORKTREE_LIMIT:
                break
        return options

    @staticmethod
    def workdir_options() -> list[dict[str, object]]:
        global _workdir_cache
        with _WORKDIR_CACHE_LOCK:
            now = time.monotonic()
            if _workdir_cache is None or now - _workdir_cache[0] >= _WORKDIR_CACHE_TTL_SECONDS:
                options = TmuxAgentSessionService._enumerate_workdir_options()
                _workdir_cache = (time.monotonic(), options)
            return [dict(option) for option in _workdir_cache[1]]

    def workdir_options_with_terminal(self) -> list[dict[str, object]]:
        """Regular workdirs plus terminal-worktree group (separate from the 15-slot cap)."""
        options = self.workdir_options()
        options.extend(self._enumerate_terminal_worktree_options())
        return options

    @staticmethod
    def resolve_workdir(workdir: str | None) -> tuple[str, Path]:
        key = workdir or "home"
        entry = _WORKDIR_BY_KEY.get(key)
        if entry is not None:
            _label, parts, _suffix = entry
            path = Path.home().joinpath(*parts)
        elif key.startswith("dir:"):
            enumerated = {
                option["key"]: Path(str(option["path"]))
                for option in TmuxAgentSessionService.workdir_options()
                if isinstance(option.get("key"), str) and isinstance(option.get("path"), str)
            }
            path = enumerated.get(key)
            if path is None:
                # Terminal isolated-write worktrees are outside the regular
                # 15-slot cache; accept an existing .worktrees/terminal path.
                candidate = Path(key[4:]).expanduser()
                posix = candidate.as_posix()
                if candidate.is_dir() and "/.worktrees/terminal/" in f"{posix}/":
                    path = candidate
                else:
                    raise InvalidTarget(f"unknown workdir: {workdir!r}")
        else:
            raise InvalidTarget(f"unknown workdir: {workdir!r}")
        if not path.is_dir():
            raise CapabilityError(f"workdir not available: {path}")
        return key, path

    @staticmethod
    def window_name_for(kind: str, workdir_key: str) -> str:
        entry = _WORKDIR_BY_KEY.get(workdir_key)
        if entry is not None:
            suffix = entry[2]
            return kind if suffix is None else f"{kind}-{suffix}"
        if not workdir_key.startswith("dir:"):
            raise InvalidTarget(f"unknown workdir: {workdir_key!r}")
        basename = Path(workdir_key.removeprefix("dir:")).name.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", basename).strip("-") or "workdir"
        slug = slug[:16].rstrip("-") or "workdir"
        if slug[-1].isdigit():
            slug = f"{slug[:15]}x"
        return f"{kind}-dir-{slug}"

    @staticmethod
    def _identity_from_window(window: str) -> tuple[str, str]:
        """Map a dashboard-managed window name back to (kind, workdir key).

        `create_new` numbers collisions as `{base}-2`, `{base}-3`, … — strip a
        trailing `-<digits>` suffix before matching so those windows still
        resolve. Workdir suffixes (agent/fo/orch) never end in digits, so the
        strip is unambiguous.
        """
        base = _TRAILING_NUMBER_RE.sub("", window)
        for kind in _AGENT_KINDS:
            if base == kind:
                return kind, "home"
            if base.startswith(f"{kind}-"):
                suffix = base[len(kind) + 1 :]
                key = _WORKDIR_KEY_BY_SUFFIX.get(suffix)
                if key:
                    return kind, key
                if suffix.startswith("dir-"):
                    return kind, "home"
        raise CapabilityError(f"window {window!r} is not a dashboard-managed agent window")

    def identity_for(self, session: str, window: str) -> tuple[str, str]:
        """Resolve (kind, workdir key) for a window, preferring window options.

        `@hermes_kind`/`@hermes_workdir` are set at spawn time and survive a
        `rename()`, unlike `_identity_from_window`'s name-based parsing.
        Windows created before this option was introduced have neither set,
        so we fall back to name parsing for them.
        """
        target = self._cmd_target(session, window)
        kind_proc = self._run("show-options", "-w", "-v", "-t", target, "@hermes_kind", check=False)
        workdir_proc = self._run("show-options", "-w", "-v", "-t", target, "@hermes_workdir", check=False)
        if kind_proc.returncode == 0 and workdir_proc.returncode == 0:
            kind = kind_proc.stdout.strip()
            workdir_key = workdir_proc.stdout.strip()
            if kind in _AGENT_KINDS and (
                workdir_key in _WORKDIR_BY_KEY or workdir_key.startswith("dir:")
            ):
                return kind, workdir_key
        return self._identity_from_window(window)

    def _window_option(
        self, session: str, window: str, name: str
    ) -> str | None:
        target = self._cmd_target(session, window)
        proc = self._run(
            "show-options", "-w", "-v", "-t", target, name, check=False
        )
        if proc.returncode != 0:
            return None
        value = proc.stdout.strip()
        return value or None

    def execution_correlation_for(
        self, session: str, window: str
    ) -> dict[str, object | None]:
        """Read only exact-window correlation pointers; never pane/global scope."""
        target = self._cmd_target(session, window)
        proc = self._run("show-options", "-w", "-t", target, check=False)
        options: dict[str, str] = {}
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                name, separator, value = line.partition(" ")
                if separator and name.startswith("@hermes_"):
                    options[name] = value.strip()
        task_id = options.get("@hermes_task_id") or None
        run_raw = options.get("@hermes_run_id") or None
        correlation_id = options.get("@hermes_correlation_id") or None
        if task_id is not None and (
            len(task_id) > 128 or any(ch.isspace() for ch in task_id)
        ):
            task_id = None
        run_id = int(run_raw) if run_raw and run_raw.isdigit() else None
        if correlation_id is not None and not re.fullmatch(
            r"[a-f0-9]{24}", correlation_id
        ):
            correlation_id = None
        return {
            "task_id": task_id,
            "run_id": run_id,
            "correlation_id": correlation_id,
        }

    def _set_or_unset_window_option(
        self,
        session: str,
        window: str,
        name: str,
        value: object | None,
    ) -> None:
        target = self._cmd_target(session, window)
        if value is None:
            self._run("set-option", "-w", "-u", "-t", target, name)
        else:
            self._run("set-option", "-w", "-t", target, name, str(value))

    def restore_execution_correlation(
        self,
        session: str,
        window: str,
        previous: Mapping[str, object | None],
        *,
        expected_pane_id: str,
    ) -> None:
        current = self.show(session, window)
        if current.pane_id != expected_pane_id:
            raise CapabilityError(
                f"window {session}:{window} changed pane generation; refusing restore"
            )
        for field, option in (
            ("task_id", "@hermes_task_id"),
            ("run_id", "@hermes_run_id"),
            ("correlation_id", "@hermes_correlation_id"),
        ):
            self._set_or_unset_window_option(
                session, window, option, previous.get(field)
            )

    def stamp_execution_correlation(
        self,
        session: str,
        window: str,
        *,
        expected_pane_id: str,
        task_id: str,
        run_id: int,
        correlation_id: str,
    ) -> dict[str, object | None]:
        """CAS-stamp correlation pointers onto one concrete pane generation."""
        if not re.fullmatch(r"%\d+", expected_pane_id or ""):
            raise InvalidTarget(f"invalid pane_id: {expected_pane_id!r}")
        if not task_id or len(task_id) > 128 or any(ch.isspace() for ch in task_id):
            raise InvalidTarget("invalid execution-capsule task id")
        if int(run_id) <= 0:
            raise InvalidTarget("invalid execution-capsule run id")
        if not re.fullmatch(r"[a-f0-9]{24}", correlation_id or ""):
            raise InvalidTarget("invalid execution-capsule correlation id")
        current = self.show(session, window)
        if current.pane_id != expected_pane_id:
            raise CapabilityError(
                f"window {session}:{window} changed pane generation; refusing stamp"
            )
        previous = self.execution_correlation_for(session, window)
        try:
            self._set_or_unset_window_option(
                session, window, "@hermes_task_id", task_id
            )
            self._set_or_unset_window_option(
                session, window, "@hermes_run_id", int(run_id)
            )
            self._set_or_unset_window_option(
                session, window, "@hermes_correlation_id", correlation_id
            )
        except Exception:
            with contextlib.suppress(Exception):
                self.restore_execution_correlation(
                    session,
                    window,
                    previous,
                    expected_pane_id=expected_pane_id,
                )
            raise
        # Additive join with terminal-run manifests (one correlation truth only).
        stamped_run_id = self._read_window_option(session, window, _TERMINAL_RUN_ID_OPTION)
        if stamped_run_id:
            self.update_terminal_manifest(
                stamped_run_id,
                capsule_correlation_id=correlation_id,
                task_id=task_id,
                run_id=int(run_id),
            )
        self._log_event(
            "execution_correlation_stamped",
            session=session,
            window=window,
            pane_id=expected_pane_id,
            task_id=task_id,
            run_id=int(run_id),
            correlation_id=correlation_id,
            terminal_run_id=stamped_run_id,
        )
        return previous

    def is_managed_window(self, session: str, window: str) -> bool:
        """True iff terminate_live() would accept this window (not raise CapabilityError).

        Mirrors terminate_live guards: session must be ``work`` and identity_for
        must resolve without raising. Foreign names must not break callers —
        wrap identity lookup so inventory stays complete.
        """
        if session != "work":
            return False
        try:
            self.identity_for(session, window)
        except (CapabilityError, InvalidTarget):
            return False
        return True


    # ----- terminal-run identity / manifests ----------------------------
    def terminal_runs_root(self) -> Path:
        """Profile-independent root for this service instance."""
        return terminal_runs_root(explicit_home=self.hermes_home)

    def _lean_context_allowlist(self) -> set[str]:
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
        except Exception:
            return set()
        raw = cfg.get("agent_terminals") if isinstance(cfg, dict) else None
        if not isinstance(raw, dict):
            return set()
        items = raw.get("lean_context_profiles") or []
        if not isinstance(items, list):
            return set()
        return {str(item).strip() for item in items if str(item).strip()}

    def agent_context_actions(self, kind: str) -> dict[str, bool]:
        base = dict(_AGENT_CONTEXT_ACTIONS.get(kind, {
            "fresh": True,
            "resume": False,
            "fork": False,
            "lean": False,
            "compact": False,
        }))
        # Lean requires both a proven adapter and an operator allowlist entry.
        if base.get("lean") and kind not in self._lean_context_allowlist():
            base["lean"] = False
        return base

    def build_agent_argv(
        self,
        kind: str,
        *,
        binary: Path,
        action: str = _ACTION_FRESH,
        context_profile: str = _CONTEXT_PROFILE_FULL,
        native_session_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return closed argv for a proven action; never invent approximate flags."""
        actions = self.agent_context_actions(kind)
        effective_action = action
        if context_profile == _CONTEXT_PROFILE_LEAN:
            # Lean is a start option, not a mutation of a running context.
            if not actions.get("lean"):
                raise CapabilityError(
                    f"lean context is not available for agent {kind!r}; "
                    "no proven safe lean adapter is enabled"
                )
            effective_action = _ACTION_LEAN
        if effective_action == _ACTION_FRESH:
            if not actions.get("fresh", True):
                raise CapabilityError(f"fresh start is not available for agent {kind!r}")
            if kind == "hermes":
                return (str(binary), "--tui")
            if kind == "grok":
                return (str(binary), "--model", "grok-4.5")
            return (str(binary),)
        if effective_action == _ACTION_RESUME:
            if not actions.get("resume"):
                raise CapabilityError(f"resume is not available for agent {kind!r}")
            if kind == "claude":
                return (str(binary), "--continue")
            if kind == "codex":
                if not native_session_id:
                    raise CapabilityError("codex resume requires a stamped native_session_id")
                return (str(binary), "resume", native_session_id)
            raise CapabilityError(f"resume is not available for agent {kind!r}")
        if effective_action == _ACTION_FORK:
            if not actions.get("fork"):
                raise CapabilityError(f"fork is not available for agent {kind!r}")
            raise CapabilityError(f"fork is not available for agent {kind!r}")
        if effective_action == _ACTION_LEAN:
            if not actions.get("lean"):
                raise CapabilityError(f"lean is not available for agent {kind!r}")
            if kind == "codex":
                # Closed proven form: explicit profile flag, never free-form flags.
                return (str(binary), "--profile", "lean")
            raise CapabilityError(f"lean is not available for agent {kind!r}")
        raise CapabilityError(f"unknown context action: {effective_action!r}")

    def _manifest_path(self, terminal_run_id: str) -> Path:
        return self.terminal_runs_root() / terminal_run_id / "manifest.json"

    def read_terminal_manifest(self, terminal_run_id: str) -> dict[str, object] | None:
        path = self._manifest_path(terminal_run_id)
        if not path.is_file():
            return None
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_terminal_manifest(self, launch: TerminalLaunchContext, *, window: str, session: str, status: str = "running") -> Path:
        run_dir = self.terminal_runs_root() / launch.terminal_run_id
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass
        path = run_dir / "manifest.json"
        payload = launch.to_manifest(window=window, session=session, status=status)
        atomic_json_write(path, payload, mode=0o600)
        return path

    def update_terminal_manifest(self, terminal_run_id: str, **fields: object) -> dict[str, object] | None:
        current = self.read_terminal_manifest(terminal_run_id)
        if current is None:
            return None
        current.update(fields)
        path = self._manifest_path(terminal_run_id)
        atomic_json_write(path, current, mode=0o600)
        return current

    def mark_terminal_run_ended(self, terminal_run_id: str | None) -> None:
        if not terminal_run_id:
            return
        with contextlib.suppress(Exception):
            self.update_terminal_manifest(terminal_run_id, status="ended", ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def _git_output(self, *args: str, cwd: Path | None = None) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip()

    def _repo_root_for(self, path: Path) -> Path | None:
        out = self._git_output("rev-parse", "--show-toplevel", cwd=path)
        return Path(out) if out else None

    def _current_head_sha(self, path: Path) -> str | None:
        return self._git_output("rev-parse", "HEAD", cwd=path)

    def _create_isolated_write_worktree(self, *, base_cwd: Path, terminal_run_id: str, base_sha: str) -> tuple[Path, str]:
        repo = self._repo_root_for(base_cwd)
        if repo is None:
            raise CapabilityError(f"isolated write requires a git repository under {base_cwd}")
        branch = f"terminal/{terminal_run_id}"
        worktree_root = repo / ".worktrees" / _TERMINAL_WORKTREE_DIRNAME
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_root / terminal_run_id
        if worktree_path.exists():
            raise CapabilityError(f"terminal worktree already exists: {worktree_path}")
        proc = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree_path), base_sha],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            raise CapabilityError(f"failed to create isolated write worktree: {detail}")
        return worktree_path, branch

    def _build_launch_context(
        self,
        kind: str,
        workdir: str | None,
        *,
        start_mode: str = _START_MODE_FREE,
        context_profile: str = _CONTEXT_PROFILE_FULL,
        action: str = _ACTION_FRESH,
        native_session_id: str | None = None,
        terminal_run_id: str | None = None,
        capsule_correlation_id: str | None = None,
        reuse_worktree_path: str | None = None,
        reuse_worktree_branch: str | None = None,
        reuse_base_sha: str | None = None,
    ) -> tuple[AgentWindowDefinition, TerminalLaunchContext]:
        kind = self.validate_name(kind, field="kind")
        if kind not in _AGENT_KINDS:
            raise InvalidTarget(f"unknown agent kind: {kind}")
        start_mode = (start_mode or _START_MODE_FREE).strip()
        context_profile = (context_profile or _CONTEXT_PROFILE_FULL).strip()
        action = (action or _ACTION_FRESH).strip()
        if start_mode not in _VALID_START_MODES:
            raise InvalidTarget(f"unknown start_mode: {start_mode!r}")
        if context_profile not in _VALID_CONTEXT_PROFILES:
            raise InvalidTarget(f"unknown context_profile: {context_profile!r}")
        if action not in _VALID_RESPAWN_ACTIONS and action != _ACTION_LEAN:
            raise InvalidTarget(f"unknown action: {action!r}")
        workdir_key, cwd = self.resolve_workdir(workdir)
        run_id = terminal_run_id or uuid.uuid4().hex
        base_sha = reuse_base_sha or self._current_head_sha(cwd)
        worktree_path: str | None = None
        worktree_branch: str | None = None
        launch_cwd = cwd
        if start_mode == _START_MODE_ISOLATED_WRITE:
            if reuse_worktree_path:
                wt = Path(reuse_worktree_path)
                if not wt.is_dir():
                    raise CapabilityError(f"isolated write worktree missing: {wt}")
                worktree_path = str(wt)
                worktree_branch = reuse_worktree_branch
                launch_cwd = wt
            else:
                if not base_sha:
                    raise CapabilityError("isolated write requires a resolvable base SHA")
                wt, branch = self._create_isolated_write_worktree(
                    base_cwd=cwd,
                    terminal_run_id=run_id,
                    base_sha=base_sha,
                )
                worktree_path = str(wt)
                worktree_branch = branch
                launch_cwd = wt
        else:
            # Free/Exploration: existing directory only, never create a worktree.
            worktree_path = None
            worktree_branch = None
        binary = self.resolve_agent_binary(kind)
        argv = self.build_agent_argv(
            kind,
            binary=binary,
            action=action,
            context_profile=context_profile,
            native_session_id=native_session_id,
        )
        env = self._safe_env({"HERMES_TUI_INLINE": "1"} if kind == "hermes" else None)
        window = self.window_name_for(kind, workdir_key)
        definition = AgentWindowDefinition(
            kind=kind,
            session="work",
            window=window,
            argv=argv,
            cwd=launch_cwd,
            env=env,
            workdir_key=workdir_key,
        )
        launch = TerminalLaunchContext(
            terminal_run_id=run_id,
            agent_kind=kind,
            start_mode=start_mode,
            context_profile=context_profile,
            cwd=str(launch_cwd),
            base_sha=base_sha,
            native_session_id=native_session_id,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            action=_ACTION_LEAN if context_profile == _CONTEXT_PROFILE_LEAN else action,
            capsule_correlation_id=capsule_correlation_id,
            argv=argv,
        )
        return definition, launch

    def _read_window_option(self, session: str, window: str, option: str) -> str | None:
        target = self._cmd_target(session, window)
        proc = self._run("show-options", "-w", "-v", "-t", target, option, check=False)
        if proc.returncode != 0:
            return None
        value = (proc.stdout or "").strip()
        return value or None

    def terminal_launch_state_for(self, session: str, window: str) -> dict[str, str | None]:
        return {
            "terminal_run_id": self._read_window_option(session, window, _TERMINAL_RUN_ID_OPTION),
            "start_mode": self._read_window_option(session, window, _START_MODE_OPTION),
            "context_profile": self._read_window_option(session, window, _CONTEXT_PROFILE_OPTION),
            "base_sha": self._read_window_option(session, window, _BASE_SHA_OPTION),
            "native_session_id": self._read_window_option(session, window, _NATIVE_SESSION_OPTION),
            "worktree_path": self._read_window_option(session, window, _WORKTREE_PATH_OPTION),
            "worktree_branch": self._read_window_option(session, window, _WORKTREE_BRANCH_OPTION),
            "cwd": self._read_window_option(session, window, _CWD_OPTION),
        }

    def _stamp_launch_options(self, session: str, window: str, launch: TerminalLaunchContext) -> None:
        target = self._cmd_target(session, window)
        pairs = [
            (_TERMINAL_RUN_ID_OPTION, launch.terminal_run_id),
            (_START_MODE_OPTION, launch.start_mode),
            (_CONTEXT_PROFILE_OPTION, launch.context_profile),
            (_CWD_OPTION, launch.cwd),
        ]
        if launch.base_sha:
            pairs.append((_BASE_SHA_OPTION, launch.base_sha))
        if launch.native_session_id:
            pairs.append((_NATIVE_SESSION_OPTION, launch.native_session_id))
        if launch.worktree_path:
            pairs.append((_WORKTREE_PATH_OPTION, launch.worktree_path))
        if launch.worktree_branch:
            pairs.append((_WORKTREE_BRANCH_OPTION, launch.worktree_branch))
        for option, value in pairs:
            self._run("set-option", "-w", "-t", target, option, value)

    def _enumerate_terminal_worktree_options(self) -> list[dict[str, object]]:
        root = self.terminal_runs_root()
        if not root.is_dir():
            return []
        options: list[dict[str, object]] = []
        try:
            run_dirs = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        except OSError:
            return []
        for run_dir in run_dirs:
            if not run_dir.is_dir():
                continue
            manifest = self.read_terminal_manifest(run_dir.name)
            if not manifest:
                continue
            wt = manifest.get("worktree_path")
            if not isinstance(wt, str) or not wt:
                continue
            path = Path(wt)
            if not path.is_dir():
                continue
            branch = manifest.get("worktree_branch") or path.name
            label = f"terminal · {branch}"
            options.append(
                {
                    "key": f"dir:{path}",
                    "label": label,
                    "path": str(path),
                    "group": _TERMINAL_WORKTREE_GROUP,
                    "terminal_run_id": run_dir.name,
                }
            )
        return options

    def capabilities(self) -> CapabilityState:
        tmux_available = shutil.which(self.tmux_binary) is not None or Path(self.tmux_binary).exists()
        agents: dict[str, dict[str, object]] = {}
        for kind in _AGENT_KINDS:
            actions = self.agent_context_actions(kind)
            try:
                binary = self.resolve_agent_binary(kind)
                agents[kind] = {
                    "available": True,
                    "binary": str(binary),
                    "reason": None,
                    "actions": actions,
                    "prerequisites": self._agent_prerequisites(kind, actions),
                }
            except (CapabilityError, InvalidTarget) as exc:
                agents[kind] = {
                    "available": False,
                    "binary": None,
                    "reason": str(exc),
                    "actions": actions,
                    "prerequisites": self._agent_prerequisites(kind, actions),
                }
        hermes_state = agents["hermes"]
        return CapabilityState(
            tmux_available=tmux_available,
            hermes_tui_available=bool(hermes_state["available"]),
            hermes_binary=hermes_state["binary"] if isinstance(hermes_state["binary"], str) else None,
            reason=hermes_state["reason"] if isinstance(hermes_state["reason"], str) else None,
            agents=agents,
            workdirs=self.workdir_options_with_terminal(),
        )

    def _agent_prerequisites(self, kind: str, actions: Mapping[str, bool]) -> list[dict[str, object]]:
        """Operator-facing prerequisites; never auto-create host profiles."""
        notes: list[dict[str, object]] = []
        if kind == "codex" and actions.get("lean"):
            # Host-wide Codex profiles are operator-owned; surface a prerequisite only.
            codex_home = Path.home() / ".codex"
            profile_cfg = codex_home / "config.toml"
            if not profile_cfg.is_file():
                notes.append(
                    {
                        "code": "codex_profile_missing",
                        "message": (
                            "Lean/Fresh for Codex requires an operator-managed host Codex "
                            "profile (e.g. ~/.codex/config.toml with a lean profile). "
                            "Hermes will not create host-wide Codex profiles."
                        ),
                        "blocks": ["lean"],
                    }
                )
        return notes

    def definition_for(
        self,
        kind: str,
        workdir: str | None = None,
        *,
        start_mode: str = _START_MODE_FREE,
        context_profile: str = _CONTEXT_PROFILE_FULL,
        action: str = _ACTION_FRESH,
        native_session_id: str | None = None,
        terminal_run_id: str | None = None,
        capsule_correlation_id: str | None = None,
        reuse_worktree_path: str | None = None,
        reuse_worktree_branch: str | None = None,
        reuse_base_sha: str | None = None,
    ) -> AgentWindowDefinition:
        definition, _launch = self._build_launch_context(
            kind,
            workdir,
            start_mode=start_mode,
            context_profile=context_profile,
            action=action,
            native_session_id=native_session_id,
            terminal_run_id=terminal_run_id,
            capsule_correlation_id=capsule_correlation_id,
            reuse_worktree_path=reuse_worktree_path,
            reuse_worktree_branch=reuse_worktree_branch,
            reuse_base_sha=reuse_base_sha,
        )
        return definition

    def definition_and_launch(
        self,
        kind: str,
        workdir: str | None = None,
        *,
        start_mode: str = _START_MODE_FREE,
        context_profile: str = _CONTEXT_PROFILE_FULL,
        action: str = _ACTION_FRESH,
        native_session_id: str | None = None,
        terminal_run_id: str | None = None,
        capsule_correlation_id: str | None = None,
        reuse_worktree_path: str | None = None,
        reuse_worktree_branch: str | None = None,
        reuse_base_sha: str | None = None,
    ) -> tuple[AgentWindowDefinition, TerminalLaunchContext]:
        return self._build_launch_context(
            kind,
            workdir,
            start_mode=start_mode,
            context_profile=context_profile,
            action=action,
            native_session_id=native_session_id,
            terminal_run_id=terminal_run_id,
            capsule_correlation_id=capsule_correlation_id,
            reuse_worktree_path=reuse_worktree_path,
            reuse_worktree_branch=reuse_worktree_branch,
            reuse_base_sha=reuse_base_sha,
        )

    # ----- inventory ------------------------------------------------------
    def _isolated_attach_rows(self) -> list[tuple[str, str, str, str, int | None, int]]:
        proc = self._run(
            "list-sessions",
            "-F",
            "#{session_name}\t#{@hermes_ephemeral_attach}\t#{@hermes_attach_source}\t#{@hermes_attach_window}\t#{@hermes_attach_created_at}\t#{session_attached}",
            check=False,
        )
        if proc.returncode != 0:
            return []
        rows: list[tuple[str, str, str, str, int | None, int]] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            name, marker, source, window, created_raw, attached_raw = parts
            try:
                created = int(created_raw) if created_raw else None
            except ValueError:
                created = None
            try:
                attached = int(attached_raw or "0")
            except ValueError:
                attached = 0
            rows.append((name, marker, source, window, created, attached))
        return rows

    def list_sessions(self) -> list[str]:
        return sorted(
            name
            for name, marker, _source, _window, _created, _attached in self._isolated_attach_rows()
            if name and marker != "1"
        )

    def create_isolated_attach(
        self,
        session: str,
        window: str,
        *,
        attach_id: str | None = None,
        now: int | None = None,
    ) -> IsolatedAttachTarget:
        source_session = self.validate_name(session, field="session")
        source_window = self.validate_name(window, field="window")
        if source_session.startswith(_EPHEMERAL_ATTACH_PREFIX):
            raise InvalidTarget("ephemeral attach sessions cannot be used as sources")
        if not self.window_exists(source_session, source_window):
            raise KeyError(f"tmux target not found: {source_session}:{source_window}")
        token = self.validate_name(attach_id or secrets.token_hex(6), field="attach_id")
        group = self.validate_name(f"{_EPHEMERAL_ATTACH_PREFIX}{token}", field="session")
        if group in {name for name, *_rest in self._isolated_attach_rows()}:
            raise InvalidTarget(f"isolated attach already exists: {group}")
        created = int(self._now() if now is None else now)
        try:
            self._run("new-session", "-d", "-t", source_session, "-s", group)
            self._run("select-window", "-t", self._cmd_target(group, source_window))
            for option, value in (
                (_EPHEMERAL_ATTACH_MARKER, "1"),
                (_EPHEMERAL_ATTACH_SOURCE, source_session),
                (_EPHEMERAL_ATTACH_WINDOW, source_window),
                (_EPHEMERAL_ATTACH_CREATED_AT, str(created)),
            ):
                self._run("set-option", "-t", group, option, value)
        except Exception:
            self._run("kill-session", "-t", group, check=False)
            raise
        return IsolatedAttachTarget(source_session, source_window, group, source_window)

    def cleanup_isolated_attach(self, session: str) -> bool:
        target = self.validate_name(session, field="session")
        row = next((row for row in self._isolated_attach_rows() if row[0] == target), None)
        if row is None or row[1] != "1":
            return False
        return self._run("kill-session", "-t", target, check=False).returncode == 0

    def cleanup_related_isolated_attaches(self, source_session: str, source_window: str | None = None) -> list[str]:
        source = self.validate_name(source_session, field="session")
        window = self.validate_name(source_window, field="window") if source_window is not None else None
        cleaned: list[str] = []
        for name, marker, row_source, row_window, _created, _attached in self._isolated_attach_rows():
            if marker != "1" or row_source != source or (window is not None and row_window != window):
                continue
            if self.cleanup_isolated_attach(name):
                cleaned.append(name)
        return cleaned

    def cleanup_stale_isolated_attaches(
        self,
        *,
        now: int | None = None,
        grace_seconds: int = _EPHEMERAL_ATTACH_GRACE_SECONDS,
    ) -> list[str]:
        current = int(self._now() if now is None else now)
        cleaned: list[str] = []
        for name, marker, _source, _window, created, attached in self._isolated_attach_rows():
            if marker != "1" or attached or created is None or current - created <= grace_seconds:
                continue
            if self.cleanup_isolated_attach(name):
                cleaned.append(name)
        return cleaned

    def list_windows(self, session: str | None = None) -> list[TmuxWindow]:
        # pane_current_path stays LAST: it is the only field a pane process can
        # steer (cd into a crafted dir) — trailing position keeps injected tabs
        # from shifting pid/dead into the wrong columns. window_activity goes
        # right before it, so it's still bounded by the trailing-cwd rule.
        args = [
            "list-windows",
            "-a",
            "-F",
            "#{session_name}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}\t#{window_activity}\t#{@hermes_ephemeral_attach}\t#{pane_current_path}",
        ]
        if session:
            args.insert(1, "-t")
            args.insert(2, self.validate_name(session, field="session"))
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            return []
        windows: list[TmuxWindow] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 9)
            if len(parts) < 10:
                continue
            if parts[8] == "1":
                continue
            pid = int(parts[4]) if parts[4].isdigit() else None
            dead = parts[5] == "1"
            activity = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else None
            cwd = ("\t".join(parts[9:]) or None) if len(parts) > 9 else None
            session_name = parts[0]
            window_name = parts[1]
            # managed gates the UI terminate affordance only; kill_dead stays
            # available for dead foreign panes (intentional cleanup path).
            managed = self.is_managed_window(session_name, window_name)
            try:
                correlation = self.execution_correlation_for(
                    session_name, window_name
                )
            except (AgentTerminalError, OSError, subprocess.CalledProcessError):
                correlation = {
                    "task_id": None,
                    "run_id": None,
                    "correlation_id": None,
                }
            try:
                launch_state = self.terminal_launch_state_for(session_name, window_name)
            except (AgentTerminalError, OSError, subprocess.CalledProcessError):
                launch_state = {
                    "terminal_run_id": None,
                    "start_mode": None,
                    "context_profile": None,
                    "base_sha": None,
                    "native_session_id": None,
                    "worktree_path": None,
                    "worktree_branch": None,
                }
            windows.append(
                TmuxWindow(
                    session_name,
                    window_name,
                    parts[2] == "1",
                    parts[3],
                    pid,
                    parts[6],
                    cwd,
                    dead,
                    activity,
                    managed,
                    task_id=correlation["task_id"],
                    run_id=correlation["run_id"],
                    correlation_id=correlation["correlation_id"],
                    terminal_run_id=launch_state.get("terminal_run_id"),
                    start_mode=launch_state.get("start_mode"),
                    context_profile=launch_state.get("context_profile"),
                    base_sha=launch_state.get("base_sha"),
                    native_session_id=launch_state.get("native_session_id"),
                    worktree_path=launch_state.get("worktree_path"),
                    worktree_branch=launch_state.get("worktree_branch"),
                )
            )
        return windows

    def window_exists(self, session: str, window: str) -> bool:
        # list-panes errors on a missing window; display-message would silently
        # fall back to the current window and report every window as existing.
        # Transient tmux/socket failures must NOT map to "gone" — idempotent close
        # paths would otherwise report success without killing anything.
        target = self._cmd_target(session, window)
        proc = self._run("list-panes", "-t", target, "-F", "#{window_name}", check=False)
        if proc.returncode == 0:
            return True
        stderr = (proc.stderr or "").strip()
        if self._is_tmux_gone_message(stderr):
            return False
        detail = stderr or f"exit {proc.returncode}"
        raise AgentTerminalError(f"tmux list-panes failed for {session}:{window}: {detail}")

    # ----- lifecycle / IO -------------------------------------------------
    def ensure_session_options(self, session: str) -> None:
        """Best-effort, idempotent scrollback/mouse setup for one tmux session.

        Sets `mouse on` and `history-limit 10000` SCOPED to *session* (`-t
        <session>`, never `-g`) so wheel/touch scrolling can drive tmux's
        native SGR mouse tracking without touching the user's other tmux
        sessions. Re-applying the same value is a tmux no-op, so this is
        safe to call on every spawn and every WS attach. Must never fail the
        caller — any tmux error is logged and swallowed.
        """
        try:
            session = self.validate_name(session, field="session")
            for name, value in (("mouse", "on"), ("history-limit", "10000")):
                proc = self._run("set-option", "-t", session, name, value, check=False)
                if proc.returncode != 0:
                    self._log_event(
                        "ensure_session_options_failed",
                        session=session,
                        option=name,
                        stderr=proc.stderr.strip()[:200],
                    )
        except Exception:
            # The never-fail contract must hold even when the failure path
            # itself fails (validate_name, tmux exec, or _log_event on a
            # full/read-only disk) — a broken option setup may not take the
            # spawn/attach down with it.
            with contextlib.suppress(Exception):
                self._log_event("ensure_session_options_error", session=str(session))

    def _set_window_identity(
        self,
        session: str,
        window: str,
        *,
        kind: str,
        workdir_key: str,
        session_id: str | None = None,
        task_id: str | None = None,
        run_id: int | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Persist managed identity without touching the pane process."""
        target = self._cmd_target(session, window)
        self._run("set-option", "-w", "-t", target, "@hermes_kind", kind)
        self._run("set-option", "-w", "-t", target, "@hermes_workdir", workdir_key)
        if session_id:
            self._run("set-option", "-w", "-t", target, "@hermes_session_id", session_id)
        if task_id:
            self._run("set-option", "-w", "-t", target, "@hermes_task_id", task_id)
        if run_id is not None:
            self._run("set-option", "-w", "-t", target, "@hermes_run_id", str(int(run_id)))
        if correlation_id:
            self._run(
                "set-option",
                "-w",
                "-t",
                target,
                "@hermes_correlation_id",
                correlation_id,
            )

    def _spawn_window(
        self,
        definition: AgentWindowDefinition,
        launch: "TerminalLaunchContext | None" = None,
    ) -> TmuxWindow:
        """Create a tmux window from a server-side definition and optional launch context.

        Browser payloads never set tmux options or argv here. When ``launch`` is
        provided, the service stamps terminal_run identity into window options
        and writes an atomic 0600 manifest under ``terminal_runs_root``.
        """
        if not definition.argv:
            raise CapabilityError(f"baseline window {definition.session}:{definition.window} is missing")
        if definition.session not in self.list_sessions():
            self._run("new-session", "-d", "-s", definition.session, "-c", str(definition.cwd))
        self.ensure_session_options(definition.session)
        env_args: list[str] = []
        for key, value in definition.env.items():
            env_args.extend(["-e", f"{key}={value}"])
        self._run(
            "new-window",
            "-d",
            "-t",
            f"{definition.session}:",
            "-n",
            definition.window,
            "-c",
            str(definition.cwd),
            *env_args,
            shlex.join(definition.argv),
        )
        # Window options survive rename() — unlike the name-based parsing in
        # `_identity_from_window`, they let identity_for() recover kind/workdir
        # for a window whose name a user has since changed.
        self._set_window_identity(
            definition.session,
            definition.window,
            kind=definition.kind,
            workdir_key=definition.workdir_key,
        )
        if launch is not None:
            self._stamp_launch_options(definition.session, definition.window, launch)
            self.write_terminal_manifest(
                launch,
                window=definition.window,
                session=definition.session,
                status="running",
            )
        return self.show(definition.session, definition.window)

    def ensure(
        self,
        kind: str,
        workdir: str | None = None,
        *,
        start_mode: str = _START_MODE_FREE,
        context_profile: str = _CONTEXT_PROFILE_FULL,
    ) -> TmuxWindow:
        kind = self.validate_name(kind, field="kind")
        if kind not in _AGENT_KINDS:
            raise InvalidTarget(f"unknown agent kind: {kind}")
        workdir_key = workdir or "home"
        if workdir_key.startswith("dir:"):
            workdir_key, _cwd = self.resolve_workdir(workdir_key)
        elif workdir_key not in _WORKDIR_BY_KEY:
            raise InvalidTarget(f"unknown workdir: {workdir!r}")
        # Attach path first: an existing window stays reachable even if the
        # CLI binary or workdir is currently unresolvable.
        window = self.window_name_for(kind, workdir_key)
        if self.window_exists("work", window):
            self._set_window_identity("work", window, kind=kind, workdir_key=workdir_key)
            self._log_event("ensure_existing", kind=kind, session="work", window=window)
            return self.show("work", window)
        definition, launch = self.definition_and_launch(
            kind,
            workdir_key,
            start_mode=start_mode,
            context_profile=context_profile,
            action=_ACTION_FRESH,
        )
        result = self._spawn_window(definition, launch=launch)
        self._log_event(
            "ensure_created",
            kind=kind,
            session=definition.session,
            window=definition.window,
            workdir=workdir_key,
            terminal_run_id=launch.terminal_run_id,
            start_mode=launch.start_mode,
            context_profile=launch.context_profile,
        )
        return result

    def create_new(
        self,
        kind: str,
        workdir: str | None = None,
        *,
        start_mode: str = _START_MODE_FREE,
        context_profile: str = _CONTEXT_PROFILE_FULL,
        native_session_id: str | None = None,
        capsule_correlation_id: str | None = None,
    ) -> TmuxWindow:
        """Always create a fresh window, never reuse an existing one.

        Unlike `ensure` (get-or-create), a collision with the base window
        name is resolved by numbering: `{base}-2`, `{base}-3`, … up to
        `_MAX_NUMBERED_WINDOWS`.
        """
        kind = self.validate_name(kind, field="kind")
        if kind not in _AGENT_KINDS:
            raise InvalidTarget(f"unknown agent kind: {kind}")
        workdir_key, _cwd = self.resolve_workdir(workdir)
        base_name = self.window_name_for(kind, workdir_key)
        window_name = base_name
        if self.window_exists("work", window_name):
            window_name = None
            for suffix in range(2, _MAX_NUMBERED_WINDOWS + 1):
                candidate = f"{base_name}-{suffix}"
                if not self.window_exists("work", candidate):
                    window_name = candidate
                    break
            if window_name is None:
                raise CapabilityError(
                    f"too many open {base_name!r} windows (max {_MAX_NUMBERED_WINDOWS}); "
                    "close one before creating another"
                )
        definition, launch = self.definition_and_launch(
            kind,
            workdir_key,
            start_mode=start_mode,
            context_profile=context_profile,
            action=_ACTION_FRESH,
            native_session_id=native_session_id,
            capsule_correlation_id=capsule_correlation_id,
        )
        if window_name != definition.window:
            definition = replace(definition, window=window_name)
        result = self._spawn_window(definition, launch=launch)
        self._log_event(
            "create_new",
            kind=kind,
            session=definition.session,
            window=definition.window,
            workdir=workdir_key,
            terminal_run_id=launch.terminal_run_id,
            start_mode=launch.start_mode,
            context_profile=launch.context_profile,
        )
        return result

    def respawn_dead(
        self,
        session: str,
        window: str,
        *,
        action: str = _ACTION_FRESH,
    ) -> TmuxWindow:
        """Kill a dead agent pane and recreate its window — never live processes.

        Resume/Fork/Fresh use only server-stamped identity and the closed agent
        capability matrix. Browser payloads never supply options or argv.
        Lean is a start option only and is rejected as a respawn mutation.
        """
        info = self.show(session, window)
        if not info.dead:
            raise CapabilityError(f"window {session}:{window} is not marked dead; refusing respawn")
        # Same session guard as terminate_live: never kill a dead pane in a
        # foreign session and recreate it under work.
        if info.session != "work":
            raise CapabilityError(f"window {session}:{window} is not a dashboard-managed agent window")
        action = (action or _ACTION_FRESH).strip()
        if action not in _VALID_RESPAWN_ACTIONS:
            raise InvalidTarget(
                f"unsupported respawn action: {action!r}; "
                f"allowed: {sorted(_VALID_RESPAWN_ACTIONS)}"
            )
        kind, workdir_key = self.identity_for(session, info.window)
        stamped = self.terminal_launch_state_for(session, info.window)
        terminal_run_id = stamped.get("terminal_run_id")
        start_mode = stamped.get("start_mode") or _START_MODE_FREE
        native_session_id = stamped.get("native_session_id")
        base_sha = stamped.get("base_sha")
        worktree_path = stamped.get("worktree_path")
        worktree_branch = stamped.get("worktree_branch")
        actions = self.agent_context_actions(kind)
        if action != _ACTION_FRESH and not actions.get(action):
            raise CapabilityError(
                f"{action} is not available for agent {kind!r}; "
                "adapter does not prove installed CLI semantics"
            )
        # Validate binary + workdir BEFORE killing: a failing recreate must not
        # destroy the dead pane's scrollback for nothing.
        if terminal_run_id:
            definition, launch = self.definition_and_launch(
                kind,
                workdir_key,
                start_mode=start_mode,
                context_profile=_CONTEXT_PROFILE_FULL,
                action=action,
                native_session_id=native_session_id,
                terminal_run_id=terminal_run_id,
                reuse_worktree_path=worktree_path,
                reuse_worktree_branch=worktree_branch,
                reuse_base_sha=base_sha,
            )
        else:
            if action != _ACTION_FRESH:
                raise CapabilityError(
                    "native resume/fork requires a stamped terminal_run_id; "
                    "this legacy window only supports fresh respawn"
                )
            definition = self.definition_for(kind, workdir_key)
            launch = None
        # Recreate under the SAME name: a dead `claude-2` kommt als `claude-2`
        # zurück — ensure() würde stattdessen still das lebende Basis-Fenster
        # zurückgeben und das nummerierte Fenster verschwinden lassen.
        if definition.window != info.window:
            definition = replace(definition, window=info.window)
        self.cleanup_related_isolated_attaches(info.session, info.window)
        # Prefer pane id so a delayed close cannot race a respawn of the same name.
        kill_target = info.pane_id if info.pane_id else self._cmd_target(session, window)
        self._run("kill-window", "-t", kill_target)
        self._log_event(
            "respawn_dead",
            kind=kind,
            session=session,
            window=window,
            workdir=workdir_key,
            action=action,
            terminal_run_id=terminal_run_id,
        )
        return self._spawn_window(definition, launch=launch)

    def _kill_window_idempotent(
        self, session: str, window: str, *, pane_id: str | None = None
    ) -> bool:
        """Kill a window; treat already-gone as success (TOCTOU-safe).

        Prefer *pane_id* when present — window names are reusable after
        respawn, so a stale close must not kill the new generation. tmux
        accepts pane targets on kill-window and kills the containing window.

        Returns True if the target is gone after this call (killed now or
        already absent), False only when kill failed and the window still
        exists under the name — in which case the caller should raise.
        Transient tmux failures raise AgentTerminalError via window_exists.
        """
        kill_target = pane_id if pane_id else self._cmd_target(session, window)
        try:
            self._run("kill-window", "-t", kill_target)
        except subprocess.CalledProcessError as exc:
            stderr = getattr(exc, "stderr", None) or ""
            # Pane-id kill against a dead generation: gone markers mean the
            # old target is already absent (name may belong to a new window).
            if self._is_tmux_gone_message(stderr):
                return True
            if not self.window_exists(session, window):
                return True
            return False
        return True

    def _show_if_present(self, session: str, window: str) -> TmuxWindow | None:
        """Like show(), but return None when the window vanished (idempotent close)."""
        if not self.window_exists(session, window):
            return None
        try:
            return self.show(session, window)
        except CapabilityError as exc:
            if "not found" in str(exc) and not self.window_exists(session, window):
                return None
            raise

    def kill_dead(self, session: str, window: str) -> None:
        """Remove a dead pane's window — guarded so live sessions cannot be killed.

        Idempotent: a window that is already gone is a success (no raise), so
        double-click / stale UI state does not surface as a permanent 503.
        """
        info = self._show_if_present(session, window)
        if info is None:
            self._log_event("kill_dead", session=session, window=window, already_gone=True)
            return
        if not info.dead:
            raise CapabilityError(f"window {session}:{window} is not marked dead; refusing kill")
        self.cleanup_related_isolated_attaches(info.session, info.window)
        if not self._kill_window_idempotent(session, window, pane_id=info.pane_id or None):
            raise AgentTerminalError(f"failed to kill window {session}:{window}")
        self._log_event("kill_dead", session=session, window=window)

    def terminate_live(
        self, session: str, window: str, *, allow_external: bool = False
    ) -> None:
        """Terminate a live (or dead) agent window.

        Default path: dashboard-managed only (session ``work`` + resolvable
        identity). With ``allow_external=True`` both guards are skipped so any
        window on the socket may be closed (operator-confirmed foreign kill).

        Idempotent close: missing windows succeed (no raise). Dead panes are
        killed here too — the frontend may hold a stale ``dead`` flag and call
        terminate instead of kill-dead; that race must not 503.
        """
        info = self._show_if_present(session, window)
        if info is None:
            log_fields: dict[str, object] = {
                "session": session,
                "window": window,
                "already_gone": True,
            }
            if allow_external:
                log_fields["external"] = True
            self._log_event("terminate", **log_fields)
            return
        if not allow_external:
            if info.session != "work":
                raise CapabilityError(
                    f"window {session}:{window} is not a dashboard-managed agent window"
                )
            kind, _workdir = self.identity_for(info.session, info.window)
        else:
            try:
                kind, _workdir = self.identity_for(info.session, info.window)
            except (CapabilityError, InvalidTarget):
                kind = "external"
        # Isolated-attach cleanup is only meaningful for managed windows
        # (source markers are set by dashboard attach of work-session targets).
        if not allow_external or self.is_managed_window(info.session, info.window):
            self.cleanup_related_isolated_attaches(info.session, info.window)
        terminal_run_id = None
        with contextlib.suppress(Exception):
            terminal_run_id = self._read_window_option(info.session, info.window, _TERMINAL_RUN_ID_OPTION)
        if not self._kill_window_idempotent(session, window, pane_id=info.pane_id or None):
            raise AgentTerminalError(f"failed to kill window {session}:{window}")
        # Isolated-write worktrees are never auto-removed on terminate; only the
        # run manifest is marked ended so the pruner can decide later.
        self.mark_terminal_run_ended(terminal_run_id)
        log_fields = {"kind": kind, "session": session, "window": window}
        if terminal_run_id:
            log_fields["terminal_run_id"] = terminal_run_id
        if allow_external:
            log_fields["external"] = True
        self._log_event("terminate", **log_fields)

    def rename(self, session: str, window: str, new_name: str) -> TmuxWindow:
        """Rename a dashboard-managed window, preserving its respawn identity."""
        new_name = self.validate_name(new_name, field="window")
        # identity_for raises CapabilityError for windows this service doesn't
        # manage — renaming a foreign tmux window is refused, not allowlisted.
        kind, workdir_key = self.identity_for(session, window)
        if self.window_exists(session, new_name):
            raise CapabilityError(f"window {session}:{new_name} already exists")
        target = self._cmd_target(session, window)
        # Old windows resolved via the name-based fallback have no @hermes_*
        # options yet — set them now so the rename doesn't strand the window
        # without a respawn identity.
        self._run("set-option", "-w", "-t", target, "@hermes_kind", kind)
        self._run("set-option", "-w", "-t", target, "@hermes_workdir", workdir_key)
        self._run("rename-window", "-t", target, new_name)
        self._log_event("rename", session=session, window=window, new_window=new_name, kind=kind, workdir=workdir_key)
        return self.show(session, new_name)

    def show(self, session: str, window: str) -> TmuxWindow:
        target = self._cmd_target(session, window)
        if not self.window_exists(session, window):
            raise CapabilityError(f"window {session}:{window} not found")
        try:
            proc = self._run(
                "display-message",
                "-p",
                "-t",
                target,
                "#{session_name}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}\t#{window_activity}\t#{pane_current_path}",
            )
        except subprocess.CalledProcessError as exc:
            # Window can vanish between list-panes and display-message (TOCTOU);
            # only map genuine not-found/no-server messages to CapabilityError —
            # transient socket failures must stay honest AgentTerminalError.
            stderr = getattr(exc, "stderr", None) or ""
            if self._is_tmux_gone_message(stderr):
                raise CapabilityError(f"window {session}:{window} not found") from exc
            detail = (stderr or "").strip() or "nonzero exit"
            raise AgentTerminalError(
                f"tmux display-message failed for {session}:{window}: {detail}"
            ) from exc
        parts = proc.stdout.rstrip("\n").split("\t")
        pid = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        dead = len(parts) > 5 and parts[5] == "1"
        activity = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else None
        cwd = ("\t".join(parts[8:]) or None) if len(parts) > 8 else None
        session_name = parts[0]
        window_name = parts[1]
        # Cheap for a single window (two show-options + optional name parse) —
        # same rule as list_windows so show/ensure/respawn payloads stay consistent.
        managed = self.is_managed_window(session_name, window_name)
        try:
            correlation = self.execution_correlation_for(
                session_name, window_name
            )
        except (AgentTerminalError, OSError, subprocess.CalledProcessError):
            correlation = {
                "task_id": None,
                "run_id": None,
                "correlation_id": None,
            }
        try:
            launch_state = self.terminal_launch_state_for(session_name, window_name)
        except (AgentTerminalError, OSError, subprocess.CalledProcessError):
            launch_state = {
                "terminal_run_id": None,
                "start_mode": None,
                "context_profile": None,
                "base_sha": None,
                "native_session_id": None,
                "worktree_path": None,
                "worktree_branch": None,
            }
        return TmuxWindow(
            session_name,
            window_name,
            parts[2] == "1",
            parts[3],
            pid,
            parts[6] if len(parts) > 6 else "",
            cwd,
            dead,
            activity,
            managed,
            task_id=correlation["task_id"],
            run_id=correlation["run_id"],
            correlation_id=correlation["correlation_id"],
            terminal_run_id=launch_state.get("terminal_run_id"),
            start_mode=launch_state.get("start_mode"),
            context_profile=launch_state.get("context_profile"),
            base_sha=launch_state.get("base_sha"),
            native_session_id=launch_state.get("native_session_id"),
            worktree_path=launch_state.get("worktree_path"),
            worktree_branch=launch_state.get("worktree_branch"),
        )

    def capture(self, session: str, window: str, *, start: int = -200, log: bool = True) -> str:
        target = self._cmd_target(session, window)
        start = max(-5000, min(0, int(start)))
        proc = self._run("capture-pane", "-p", "-t", target, "-S", str(start))
        if log:
            self._log_event("capture", session=session, window=window, lines=abs(start))
        return proc.stdout

    def capture_pane(self, pane_id: str, *, start: int = -50) -> str:
        """Capture a pane by absolute tmux pane id (``%N``), not session:window.

        Pane ids survive window renames/respawns better for answer delivery. This
        explicit API is always fresh; safety rechecks must never use the automatic
        snapshot cache.
        """
        if not re.fullmatch(r"%\d+", pane_id or ""):
            raise InvalidTarget(f"invalid pane_id: {pane_id!r}")
        start = max(-5000, min(0, int(start)))
        proc = self._run("capture-pane", "-p", "-t", pane_id, "-S", str(start))
        self._log_event("capture_pane", pane_id=pane_id, lines=abs(start))
        return proc.stdout

    def capture_pane_snapshot(
        self,
        pane_id: str,
        *,
        window_activity: int | None,
        variant: str = _AUTO_CAPTURE_VARIANT,
        force_fresh: bool = False,
    ) -> TerminalSnapshot:
        """Capture the canonical automatic-reader view of a pane.

        Cache identity is server + pane + tmux activity generation + normalization
        variant. Consumer display depth is deliberately absent: all automatic
        readers derive from the same unmodified 25-line raw capture.
        """
        if not re.fullmatch(r"%\d+", pane_id or ""):
            raise InvalidTarget(f"invalid pane_id: {pane_id!r}")
        now = float(self._now())

        def capture_raw() -> str:
            proc = self._run(
                "capture-pane",
                "-p",
                "-t",
                pane_id,
                "-S",
                str(_AUTO_CAPTURE_START),
            )
            return proc.stdout

        if force_fresh:
            activity = (
                int(window_activity) if window_activity is not None else int(now)
            )
            return TerminalSnapshot(
                pane_id=pane_id,
                window_activity=activity,
                captured_at=now,
                raw=capture_raw(),
                variant=variant,
            )
        return self._capture_cache.get_or_capture(
            server_id=self._capture_server_id,
            pane_id=pane_id,
            window_activity=window_activity,
            variant=variant,
            now=now,
            capture=capture_raw,
            clock=self._now,
        )

    def overview(self, *, tail_lines: int = 10) -> dict[str, object]:
        """Fleet snapshot: every tmux window plus a best-effort live tail and
        an honest heuristic state — one call for the dashboard control room.

        ``tail_lines`` is clamped server-side to ``[1, 25]`` (the absolute value
        of ``_AUTO_CAPTURE_START``). Callers may request more, but the service
        never captures beyond that automatic-reader bound.

        Pane contents never reach `_log_event` (same rule as elsewhere in this
        module); only the window count is logged.
        """
        now = self._now()
        # API contract: clamp to the automatic capture bound (25 lines).
        tail_lines = max(1, min(abs(int(tail_lines)), abs(_AUTO_CAPTURE_START)))
        entries: list[dict[str, object]] = []
        for window in self.list_windows():
            tail: str | None
            try:
                raw = self.capture_pane_snapshot(
                    window.pane_id,
                    window_activity=window.activity,
                ).raw
            except (AgentTerminalError, OSError, subprocess.CalledProcessError):
                tail = None
            else:
                cleaned = strip_ansi(raw)
                lines = cleaned.splitlines()
                # A pane whose output hasn't filled its screen yet still
                # captures padded with blank rows down to the pane height
                # (tmux has no scrollback to clip against) — drop that
                # trailing padding before keeping the last tail_lines so we
                # tail real content, not empty screen rows.
                while lines and not lines[-1].strip():
                    lines.pop()
                tail = "\n".join(lines[-tail_lines:])[-600:]
            age = (now - window.activity) if window.activity is not None else None
            state = classify_agent_pane(tail or "", age, window.dead)
            entries.append({**window.to_dict(), "tail": tail, "state": state, "state_source": "heuristic"})
        self._log_event("overview", windows=len(entries))
        return {"now": int(now), "windows": entries}

    def send_keys(self, session: str, window: str, text: str) -> None:
        target = self._cmd_target(session, window)
        self._run("send-keys", "-t", target, "-l", "--", text)
        # This API addresses a window name rather than a stable pane id; clear
        # every cached pane for the server so no automatic reader sees pre-send
        # output under a still-unchanged whole-second activity value.
        self._capture_cache.invalidate_server(self._capture_server_id)
        self._log_event("send_keys", session=session, window=window, bytes=len(text.encode("utf-8")))

    def send_keys_to_pane(self, pane_id: str, text: str, *, enter: bool = False) -> None:
        """Send literal keys to an absolute pane id; optional separate Enter.

        Enter is never mixed into the literal payload — a second ``send-keys``
        fires only when ``enter=True``.
        """
        if not re.fullmatch(r"%\d+", pane_id or ""):
            raise InvalidTarget(f"invalid pane_id: {pane_id!r}")
        self._run("send-keys", "-t", pane_id, "-l", "--", text)
        if enter:
            self._run("send-keys", "-t", pane_id, "Enter")
        self._capture_cache.invalidate_pane(self._capture_server_id, pane_id)
        self._log_event(
            "send_keys_to_pane",
            pane_id=pane_id,
            bytes=len(text.encode("utf-8")),
            enter=bool(enter),
        )

    def interrupt(self, session: str, window: str) -> None:
        target = self._cmd_target(session, window)
        self._run("send-keys", "-t", target, "C-c")
        self._capture_cache.invalidate_server(self._capture_server_id)
        self._log_event("interrupt", session=session, window=window)

    def attach_argv(self, session: str, window: str) -> list[str]:
        # Exact-match target: with suffix windows (claude-fo, …) a fuzzy prefix
        # match could attach a different workdir terminal if the exact window
        # disappeared between listing and attach.
        return self._tmux_cmd("attach-session", "-t", self._cmd_target(session, window))

    def attach_metadata(self, session: str, window: str) -> dict[str, object]:
        info = self.show(session, window)
        target = self._target(info.session, info.window)
        argv = self.attach_argv(info.session, info.window)
        self._log_event("attach_metadata", session=info.session, window=info.window)
        return {
            "target": target,
            "session": info.session,
            "window": info.window,
            "active": info.active,
            "pane_id": info.pane_id,
            "pid": info.pid,
            "command": info.command,
            "cwd": info.cwd,
            "attach_argv": argv,
            "attach_command": shlex.join(argv),
        }

    def handoff_draft(self, session: str, window: str, *, start: int = -120) -> dict[str, object]:
        info = self.show(session, window)
        transcript = self.capture(info.session, info.window, start=start).rstrip()
        target = self._target(info.session, info.window)
        title = f"Terminal handoff for {target}"
        content = (
            f"# {title}\n\n"
            f"- tmux target: `{target}`\n"
            f"- pane: `{info.pane_id}`\n"
            f"- command: `{info.command}`\n"
            f"- cwd: `{info.cwd or 'unknown'}`\n\n"
            "## Recent pane capture\n\n"
            "```text\n"
            f"{transcript}\n"
            "```\n"
        )
        self._log_event("handoff_draft", session=info.session, window=info.window)
        return {
            "target": target,
            "session": info.session,
            "window": info.window,
            "title": title,
            "content": content,
        }

    def detach_client(self, client_id: str) -> None:
        client_id = self.validate_name(client_id, field="client")
        self._run("detach-client", "-t", client_id)
        self._log_event("detach_client", client=client_id)

"""Allowlisted tmux backend for dashboard agent terminals.

This module intentionally keeps browser input away from command construction:
clients choose an agent kind/window, while argv/cwd/env are resolved here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from hermes_cli.config import get_hermes_home

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

_AGENT_KINDS: tuple[str, ...] = ("hermes", "claude", "codex", "kimi", "grok")

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
        }


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
    ) -> None:
        self.tmux_binary = tmux_binary or shutil.which("tmux") or "tmux"
        self.socket_path = Path(socket_path) if socket_path else None
        self.hermes_binary_override = Path(hermes_binary) if hermes_binary else None
        self.hermes_home = Path(hermes_home) if hermes_home else get_hermes_home()
        self.log_dir = self.hermes_home / "agent-terminals"
        self._now = now

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
        return subprocess.run(
            self._tmux_cmd(*args),
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

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
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        max_bytes = 256 * 1024
        try:
            if path.stat().st_size > max_bytes:
                data = path.read_bytes()[-max_bytes:]
                path.write_bytes(data)
        except OSError:
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
    def workdir_options() -> list[dict[str, object]]:
        home = Path.home()
        options: list[dict[str, object]] = []
        for key, label, parts, _suffix in _WORKDIR_DEFS:
            path = home.joinpath(*parts)
            if path.is_dir():
                options.append({"key": key, "label": label, "path": str(path)})
        return options

    @staticmethod
    def resolve_workdir(workdir: str | None) -> tuple[str, Path]:
        key = workdir or "home"
        entry = _WORKDIR_BY_KEY.get(key)
        if entry is None:
            raise InvalidTarget(f"unknown workdir: {workdir!r}")
        _label, parts, _suffix = entry
        path = Path.home().joinpath(*parts)
        if not path.is_dir():
            raise CapabilityError(f"workdir not available: {path}")
        return key, path

    @staticmethod
    def window_name_for(kind: str, workdir_key: str) -> str:
        entry = _WORKDIR_BY_KEY.get(workdir_key)
        if entry is None:
            raise InvalidTarget(f"unknown workdir: {workdir_key!r}")
        suffix = entry[2]
        return kind if suffix is None else f"{kind}-{suffix}"

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
            if kind in _AGENT_KINDS and workdir_key in _WORKDIR_BY_KEY:
                return kind, workdir_key
        return self._identity_from_window(window)

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

    def capabilities(self) -> CapabilityState:
        tmux_available = shutil.which(self.tmux_binary) is not None or Path(self.tmux_binary).exists()
        agents: dict[str, dict[str, object]] = {}
        for kind in _AGENT_KINDS:
            try:
                binary = self.resolve_agent_binary(kind)
                agents[kind] = {"available": True, "binary": str(binary), "reason": None}
            except (CapabilityError, InvalidTarget) as exc:
                agents[kind] = {"available": False, "binary": None, "reason": str(exc)}
        hermes_state = agents["hermes"]
        return CapabilityState(
            tmux_available=tmux_available,
            hermes_tui_available=bool(hermes_state["available"]),
            hermes_binary=hermes_state["binary"] if isinstance(hermes_state["binary"], str) else None,
            reason=hermes_state["reason"] if isinstance(hermes_state["reason"], str) else None,
            agents=agents,
            workdirs=self.workdir_options(),
        )

    def definition_for(self, kind: str, workdir: str | None = None) -> AgentWindowDefinition:
        kind = self.validate_name(kind, field="kind")
        if kind not in _AGENT_KINDS:
            raise InvalidTarget(f"unknown agent kind: {kind}")
        workdir_key, cwd = self.resolve_workdir(workdir)
        window = self.window_name_for(kind, workdir_key)
        binary = self.resolve_agent_binary(kind)
        if kind == "hermes":
            argv: tuple[str, ...] = (str(binary), "--tui")
            env = self._safe_env({"HERMES_TUI_INLINE": "1"})
        elif kind == "grok":
            # CLI dropped the grok-build product slot (2026-07-16: "unknown
            # model id"); grok-4.5 is the CLI-native default id.
            argv = (str(binary), "--model", "grok-4.5")
            env = self._safe_env()
        else:
            argv = (str(binary),)
            env = self._safe_env()
        return AgentWindowDefinition(
            kind=kind, session="work", window=window, argv=argv, cwd=cwd, env=env, workdir_key=workdir_key
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
        self, session: str, window: str, *, kind: str, workdir_key: str
    ) -> None:
        """Persist managed identity without touching the pane process."""
        target = self._cmd_target(session, window)
        self._run("set-option", "-w", "-t", target, "@hermes_kind", kind)
        self._run("set-option", "-w", "-t", target, "@hermes_workdir", workdir_key)

    def _spawn_window(self, definition: AgentWindowDefinition) -> TmuxWindow:
        """Create a tmux window from a resolved definition and return it."""
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
        return self.show(definition.session, definition.window)

    def ensure(self, kind: str, workdir: str | None = None) -> TmuxWindow:
        kind = self.validate_name(kind, field="kind")
        if kind not in _AGENT_KINDS:
            raise InvalidTarget(f"unknown agent kind: {kind}")
        if workdir is not None and workdir not in _WORKDIR_BY_KEY:
            raise InvalidTarget(f"unknown workdir: {workdir!r}")
        workdir_key = workdir or "home"
        # Attach path first: an existing window stays reachable even if the
        # CLI binary or workdir is currently unresolvable.
        window = self.window_name_for(kind, workdir_key)
        if self.window_exists("work", window):
            self._set_window_identity("work", window, kind=kind, workdir_key=workdir_key)
            self._log_event("ensure_existing", kind=kind, session="work", window=window)
            return self.show("work", window)
        definition = self.definition_for(kind, workdir_key)
        result = self._spawn_window(definition)
        self._log_event("ensure_created", kind=kind, session=definition.session, window=definition.window, workdir=workdir_key)
        return result

    def create_new(self, kind: str, workdir: str | None = None) -> TmuxWindow:
        """Always create a fresh window, never reuse an existing one.

        Unlike `ensure` (get-or-create), a collision with the base window
        name is resolved by numbering: `{base}-2`, `{base}-3`, … up to
        `_MAX_NUMBERED_WINDOWS`.
        """
        kind = self.validate_name(kind, field="kind")
        if kind not in _AGENT_KINDS:
            raise InvalidTarget(f"unknown agent kind: {kind}")
        if workdir is not None and workdir not in _WORKDIR_BY_KEY:
            raise InvalidTarget(f"unknown workdir: {workdir!r}")
        workdir_key = workdir or "home"
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
        definition = self.definition_for(kind, workdir_key)
        if window_name != definition.window:
            definition = replace(definition, window=window_name)
        result = self._spawn_window(definition)
        self._log_event("create_new", kind=kind, session=definition.session, window=definition.window, workdir=workdir_key)
        return result

    def respawn_dead(self, session: str, window: str) -> TmuxWindow:
        """Kill a dead agent pane and recreate its window — never live processes."""
        info = self.show(session, window)
        if not info.dead:
            raise CapabilityError(f"window {session}:{window} is not marked dead; refusing respawn")
        # Same session guard as terminate_live: never kill a dead pane in a
        # foreign session and recreate it under work.
        if info.session != "work":
            raise CapabilityError(f"window {session}:{window} is not a dashboard-managed agent window")
        kind, workdir_key = self.identity_for(session, info.window)
        # Validate binary + workdir BEFORE killing: a failing recreate must not
        # destroy the dead pane's scrollback for nothing.
        definition = self.definition_for(kind, workdir_key)
        # Recreate under the SAME name: a dead `claude-2` kommt als `claude-2`
        # zurück — ensure() würde stattdessen still das lebende Basis-Fenster
        # zurückgeben und das nummerierte Fenster verschwinden lassen.
        if definition.window != info.window:
            definition = replace(definition, window=info.window)
        self.cleanup_related_isolated_attaches(info.session, info.window)
        # Prefer pane id so a delayed close cannot race a respawn of the same name.
        kill_target = info.pane_id if info.pane_id else self._cmd_target(session, window)
        self._run("kill-window", "-t", kill_target)
        self._log_event("respawn_dead", kind=kind, session=session, window=window, workdir=workdir_key)
        return self._spawn_window(definition)

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
        if not self._kill_window_idempotent(session, window, pane_id=info.pane_id or None):
            raise AgentTerminalError(f"failed to kill window {session}:{window}")
        log_fields = {"kind": kind, "session": session, "window": window}
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
        )

    def capture(self, session: str, window: str, *, start: int = -200, log: bool = True) -> str:
        target = self._cmd_target(session, window)
        start = max(-5000, min(0, int(start)))
        proc = self._run("capture-pane", "-p", "-t", target, "-S", str(start))
        if log:
            self._log_event("capture", session=session, window=window, lines=abs(start))
        return proc.stdout

    def overview(self, *, tail_lines: int = 10) -> dict[str, object]:
        """Fleet snapshot: every tmux window plus a best-effort live tail and
        an honest heuristic state — one call for the dashboard control room.

        Pane contents never reach `_log_event` (same rule as elsewhere in this
        module); only the window count is logged.
        """
        now = self._now()
        entries: list[dict[str, object]] = []
        for window in self.list_windows():
            tail: str | None
            try:
                raw = self.capture(window.session, window.window, start=-tail_lines, log=False)
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
        self._log_event("send_keys", session=session, window=window, bytes=len(text.encode("utf-8")))

    def interrupt(self, session: str, window: str) -> None:
        target = self._cmd_target(session, window)
        self._run("send-keys", "-t", target, "C-c")
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

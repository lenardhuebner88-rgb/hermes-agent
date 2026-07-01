"""Allowlisted tmux backend for dashboard agent terminals.

This module intentionally keeps browser input away from command construction:
clients choose an agent kind/window, while argv/cwd/env are resolved here.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from hermes_cli.config import get_hermes_home

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|AUTH)", re.IGNORECASE)

_AGENT_KINDS: tuple[str, ...] = ("hermes", "claude", "codex", "kimi")

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
        """Map a dashboard-managed window name back to (kind, workdir key)."""
        for kind in _AGENT_KINDS:
            if window == kind:
                return kind, "home"
            if window.startswith(f"{kind}-"):
                suffix = window[len(kind) + 1 :]
                key = _WORKDIR_KEY_BY_SUFFIX.get(suffix)
                if key:
                    return kind, key
        raise CapabilityError(f"window {window!r} is not a dashboard-managed agent window")

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
        else:
            argv = (str(binary),)
            env = self._safe_env()
        return AgentWindowDefinition(kind=kind, session="work", window=window, argv=argv, cwd=cwd, env=env)

    # ----- inventory ------------------------------------------------------
    def list_sessions(self) -> list[str]:
        proc = self._run("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return []
        return [line for line in proc.stdout.splitlines() if line]

    def list_windows(self, session: str | None = None) -> list[TmuxWindow]:
        # pane_current_path stays LAST: it is the only field a pane process can
        # steer (cd into a crafted dir) — trailing position keeps injected tabs
        # from shifting pid/dead into the wrong columns.
        args = [
            "list-windows",
            "-a",
            "-F",
            "#{session_name}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}",
        ]
        if session:
            args.insert(1, "-t")
            args.insert(2, self.validate_name(session, field="session"))
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            return []
        windows: list[TmuxWindow] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            pid = int(parts[4]) if parts[4].isdigit() else None
            dead = parts[5] == "1"
            cwd = ("\t".join(parts[7:]) or None) if len(parts) > 7 else None
            windows.append(TmuxWindow(parts[0], parts[1], parts[2] == "1", parts[3], pid, parts[6], cwd, dead))
        return windows

    def window_exists(self, session: str, window: str) -> bool:
        # list-panes errors on a missing window; display-message would silently
        # fall back to the current window and report every window as existing.
        target = self._cmd_target(session, window)
        return self._run("list-panes", "-t", target, "-F", "#{window_name}", check=False).returncode == 0

    # ----- lifecycle / IO -------------------------------------------------
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
            self._log_event("ensure_existing", kind=kind, session="work", window=window)
            return self.show("work", window)
        definition = self.definition_for(kind, workdir_key)
        if not definition.argv:
            raise CapabilityError(f"baseline window {definition.session}:{definition.window} is missing")
        if definition.session not in self.list_sessions():
            self._run("new-session", "-d", "-s", definition.session, "-c", str(definition.cwd))
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
        self._log_event("ensure_created", kind=kind, session=definition.session, window=definition.window, workdir=workdir_key)
        return self.show(definition.session, definition.window)

    def respawn_dead(self, session: str, window: str) -> TmuxWindow:
        """Kill a dead agent pane and recreate its window — never live processes."""
        info = self.show(session, window)
        if info.pid and not info.dead:
            raise CapabilityError(f"window {session}:{window} has a live process; refusing respawn")
        kind, workdir_key = self._identity_from_window(info.window)
        # Validate binary + workdir BEFORE killing: a failing recreate must not
        # destroy the dead pane's scrollback for nothing.
        self.definition_for(kind, workdir_key)
        self._run("kill-window", "-t", self._cmd_target(session, window))
        self._log_event("respawn_dead", kind=kind, session=session, window=window, workdir=workdir_key)
        return self.ensure(kind, workdir_key)

    def kill_dead(self, session: str, window: str) -> None:
        """Remove a dead pane's window — guarded so live sessions cannot be killed."""
        info = self.show(session, window)
        if info.pid and not info.dead:
            raise CapabilityError(f"window {session}:{window} has a live process; refusing kill")
        self._run("kill-window", "-t", self._cmd_target(session, window))
        self._log_event("kill_dead", session=session, window=window)

    def show(self, session: str, window: str) -> TmuxWindow:
        target = self._cmd_target(session, window)
        if not self.window_exists(session, window):
            raise CapabilityError(f"window {session}:{window} not found")
        proc = self._run(
            "display-message",
            "-p",
            "-t",
            target,
            "#{session_name}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}",
        )
        parts = proc.stdout.rstrip("\n").split("\t")
        pid = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        dead = len(parts) > 5 and parts[5] == "1"
        cwd = ("\t".join(parts[7:]) or None) if len(parts) > 7 else None
        return TmuxWindow(parts[0], parts[1], parts[2] == "1", parts[3], pid, parts[6] if len(parts) > 6 else "", cwd, dead)

    def capture(self, session: str, window: str, *, start: int = -200) -> str:
        target = self._cmd_target(session, window)
        start = max(-5000, min(0, int(start)))
        proc = self._run("capture-pane", "-p", "-t", target, "-S", str(start))
        self._log_event("capture", session=session, window=window, lines=abs(start))
        return proc.stdout

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

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

    def to_dict(self) -> dict[str, object]:
        return {
            "tmux_available": self.tmux_available,
            "hermes_tui_available": self.hermes_tui_available,
            "hermes_binary": self.hermes_binary,
            "reason": self.reason,
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

    def to_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "window": self.window,
            "active": self.active,
            "pane_id": self.pane_id,
            "pid": self.pid,
            "command": self.command,
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
    def resolve_hermes_binary(self) -> Path:
        if self.hermes_binary_override is not None:
            candidate = self.hermes_binary_override
        else:
            discovered = shutil.which("hermes")
            if discovered is None:
                raise CapabilityError("hermes binary not found on PATH")
            candidate = Path(discovered)
        if not str(candidate):
            raise CapabilityError("hermes binary not found on PATH")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CapabilityError(f"hermes binary is not resolvable: {candidate}") from exc
        if not os.access(resolved, os.X_OK):
            raise CapabilityError(f"hermes binary is not executable: {resolved}")
        # Reject task-local/transient worktree launchers; dashboard agent windows
        # should survive worktree cleanup and use a stable installed Hermes CLI.
        if "/.worktrees/" in str(resolved):
            raise CapabilityError(f"hermes binary resolves to transient worktree: {resolved}")
        return resolved

    def capabilities(self) -> CapabilityState:
        tmux_available = shutil.which(self.tmux_binary) is not None or Path(self.tmux_binary).exists()
        try:
            hermes = self.resolve_hermes_binary()
            hermes_ok = True
            reason = None
        except CapabilityError as exc:
            hermes = None
            hermes_ok = False
            reason = str(exc)
        return CapabilityState(
            tmux_available=tmux_available,
            hermes_tui_available=hermes_ok,
            hermes_binary=str(hermes) if hermes else None,
            reason=reason,
        )

    def definition_for(self, kind: str) -> AgentWindowDefinition:
        kind = self.validate_name(kind, field="kind")
        if kind == "hermes":
            binary = self.resolve_hermes_binary()
            return AgentWindowDefinition(
                kind="hermes",
                session="work",
                window="hermes",
                argv=(str(binary), "--tui"),
                cwd=Path.home(),
                env=self._safe_env({"HERMES_TUI_INLINE": "1"}),
            )
        if kind in {"claude", "codex", "kimi"}:
            # Existing baseline windows are discoverable/attachable; this backend
            # never recreates or overwrites them with guessed commands.
            return AgentWindowDefinition(
                kind=kind,
                session="work",
                window=kind,
                argv=(),
                cwd=Path.home(),
                env=self._safe_env(),
            )
        raise InvalidTarget(f"unknown agent kind: {kind}")

    # ----- inventory ------------------------------------------------------
    def list_sessions(self) -> list[str]:
        proc = self._run("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return []
        return [line for line in proc.stdout.splitlines() if line]

    def list_windows(self, session: str | None = None) -> list[TmuxWindow]:
        args = ["list-windows", "-a", "-F", "#{session_name}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}"]
        if session:
            args.insert(1, "-t")
            args.insert(2, self.validate_name(session, field="session"))
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            return []
        windows: list[TmuxWindow] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            pid = int(parts[4]) if parts[4].isdigit() else None
            windows.append(TmuxWindow(parts[0], parts[1], parts[2] == "1", parts[3], pid, parts[5]))
        return windows

    def window_exists(self, session: str, window: str) -> bool:
        target = self._target(session, window)
        return self._run("display-message", "-p", "-t", target, "#{window_name}", check=False).returncode == 0

    # ----- lifecycle / IO -------------------------------------------------
    def ensure(self, kind: str) -> TmuxWindow:
        definition = self.definition_for(kind)
        if self.window_exists(definition.session, definition.window):
            self._log_event("ensure_existing", kind=kind, session=definition.session, window=definition.window)
            return self.show(definition.session, definition.window)
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
        self._log_event("ensure_created", kind=kind, session=definition.session, window=definition.window)
        return self.show(definition.session, definition.window)

    def show(self, session: str, window: str) -> TmuxWindow:
        target = self._target(session, window)
        proc = self._run(
            "display-message",
            "-p",
            "-t",
            target,
            "#{session_name}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}",
        )
        parts = proc.stdout.rstrip("\n").split("\t")
        pid = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        return TmuxWindow(parts[0], parts[1], parts[2] == "1", parts[3], pid, parts[5] if len(parts) > 5 else "")

    def capture(self, session: str, window: str, *, start: int = -200) -> str:
        target = self._target(session, window)
        start = max(-5000, min(0, int(start)))
        proc = self._run("capture-pane", "-p", "-t", target, "-S", str(start))
        self._log_event("capture", session=session, window=window, lines=abs(start))
        return proc.stdout

    def send_keys(self, session: str, window: str, text: str) -> None:
        target = self._target(session, window)
        self._run("send-keys", "-t", target, "-l", "--", text)
        self._log_event("send_keys", session=session, window=window, bytes=len(text.encode("utf-8")))

    def interrupt(self, session: str, window: str) -> None:
        target = self._target(session, window)
        self._run("send-keys", "-t", target, "C-c")
        self._log_event("interrupt", session=session, window=window)

    def attach_argv(self, session: str, window: str) -> list[str]:
        return self._tmux_cmd("attach-session", "-t", self._target(session, window))

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
            f"- command: `{info.command}`\n\n"
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

"""Tools the live voice session may call: tmux terminals and Hermes delegation."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

_TMUX_TIMEOUT_SECONDS = 10
_DEFAULT_CAPTURE_LINES = 40
_MAX_CAPTURE_LINES = 200
_MAX_CAPTURE_CHARS = 8_000
_MAX_ERROR_CHARS = 1_000

FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "list_terminals",
        "description": (
            "Listet die laufenden tmux-Terminal-Sessions auf. Die Terminal-Zentrale "
            "heißt normalerweise work."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "read_terminal",
        "description": (
            "Liest die letzten Zeilen eines tmux-Ziels. Nutze work für das aktive "
            "Fenster oder optional work:window für ein bestimmtes Fenster."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "tmux-Ziel, zum Beispiel work oder work:window.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Anzahl letzter Zeilen (1 bis 200, Standard 40).",
                },
            },
            "required": ["session"],
        },
    },
    {
        "name": "send_to_terminal",
        "description": (
            "Sendet einen Befehl wörtlich an ein tmux-Ziel und drückt Enter. Nutze "
            "work oder optional work:window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "tmux-Ziel, zum Beispiel work oder work:window.",
                },
                "command": {"type": "string"},
            },
            "required": ["session", "command"],
        },
    },
    {
        "name": "delegate_to_hermes",
        "description": (
            "Übergibt eine größere Aufgabe an den Hermes-Agenten und gibt dessen "
            "Antwort zurück."
        ),
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
]

Delegate = Callable[[str], Awaitable[str]]


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build a stable, function-response-safe error payload."""

    return {"error": {"code": code, "message": message, **details}}


def _required_text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


class VoiceToolExecutor:
    """Execute the small, explicit tool surface exposed to Gemini Live."""

    def __init__(self, delegate: Delegate | None):
        self._delegate = delegate

    async def _run_tmux(
        self, command: list[str], *, action: str
    ) -> tuple[subprocess.CompletedProcess[str] | None, dict[str, Any] | None]:
        try:
            process = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=_TMUX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, _error(
                "timeout",
                f"tmux-Aktion '{action}' hat das Zeitlimit überschritten.",
                action=action,
                timeout_seconds=_TMUX_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            return None, _error(
                "tmux_unavailable",
                "tmux konnte nicht gestartet werden.",
                action=action,
                detail=str(exc)[:_MAX_ERROR_CHARS],
            )

        if process.returncode != 0:
            stderr = (process.stderr or "").strip()[-_MAX_ERROR_CHARS:]
            return None, _error(
                "tmux_failed",
                f"tmux-Aktion '{action}' ist fehlgeschlagen.",
                action=action,
                returncode=process.returncode,
                stderr=stderr,
            )
        return process, None

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            return _error("invalid_arguments", "Tool-Argumente müssen ein Objekt sein.")

        if name == "list_terminals":
            process, error = await self._run_tmux(
                [
                    "tmux",
                    "list-sessions",
                    "-F",
                    "#{session_name}|#{session_attached}",
                ],
                action="list_terminals",
            )
            if error is not None:
                return error

            terminals = []
            for line in (process.stdout or "").splitlines():
                if "|" not in line:
                    continue
                session, attached = line.rsplit("|", 1)
                try:
                    attached_count = int(attached.strip())
                except ValueError:
                    attached_count = 0
                terminals.append({"name": session, "attached": attached_count > 0})
            return {"terminals": terminals}

        if name == "read_terminal":
            session = _required_text(args, "session")
            if session is None:
                return _error(
                    "invalid_arguments", "Für read_terminal fehlt das tmux-Ziel."
                )
            try:
                requested_lines = int(args.get("lines", _DEFAULT_CAPTURE_LINES))
            except (TypeError, ValueError):
                return _error("invalid_arguments", "lines muss eine ganze Zahl sein.")
            lines = max(1, min(requested_lines, _MAX_CAPTURE_LINES))
            process, error = await self._run_tmux(
                [
                    "tmux",
                    "capture-pane",
                    "-p",
                    "-t",
                    session,
                    "-S",
                    f"-{lines}",
                ],
                action="read_terminal",
            )
            if error is not None:
                return error

            captured = process.stdout or ""
            output = "\n".join(captured.splitlines()[-lines:])[-_MAX_CAPTURE_CHARS:]
            return {"output": output}

        if name == "send_to_terminal":
            session = _required_text(args, "session")
            command = _required_text(args, "command")
            if session is None or command is None:
                return _error(
                    "invalid_arguments",
                    "Für send_to_terminal werden tmux-Ziel und Befehl benötigt.",
                )

            _, error = await self._run_tmux(
                ["tmux", "send-keys", "-t", session, "-l", "--", command],
                action="send_to_terminal",
            )
            if error is not None:
                return error
            _, error = await self._run_tmux(
                ["tmux", "send-keys", "-t", session, "Enter"],
                action="send_to_terminal_enter",
            )
            if error is not None:
                return error
            return {"ok": True}

        if name == "delegate_to_hermes":
            prompt = _required_text(args, "prompt")
            if prompt is None:
                return _error(
                    "invalid_arguments", "Für delegate_to_hermes fehlt der Prompt."
                )
            if self._delegate is None:
                return _error(
                    "delegation_unavailable", "Delegation ist nicht verfügbar."
                )
            try:
                result = await self._delegate(prompt)
            except Exception as exc:
                return _error(
                    "delegation_failed",
                    "Hermes konnte die delegierte Aufgabe nicht ausführen.",
                    detail=str(exc)[:_MAX_ERROR_CHARS],
                )
            return {"result": result}

        return _error("unknown_tool", f"Unbekanntes Tool: {name}")

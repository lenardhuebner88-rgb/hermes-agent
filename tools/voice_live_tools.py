"""Tools the live voice session may call: tmux terminals, Hermes delegation,
Discord messaging, Kanban task creation, system status, and reminders."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from hermes_constants import get_hermes_home
from pathlib import Path
from typing import Any

_TMUX_TIMEOUT_SECONDS = 10
_DEFAULT_CAPTURE_LINES = 40
_MAX_CAPTURE_LINES = 200
_MAX_CAPTURE_CHARS = 8_000
_MAX_ERROR_CHARS = 1_000
_DISCORD_TEXT_MAX_CHARS = 1_800
_REMINDER_MIN_MINUTES = 1
_REMINDER_MAX_MINUTES = 1_440
_REMINDER_SUBPROCESS_TIMEOUT_SECONDS = 15

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
            "Übergibt eine größere Aufgabe an den Hermes-Agenten. Läuft im "
            "Hintergrund weiter; das Ergebnis kommt später automatisch ins "
            "Gespräch."
        ),
        "behavior": "NON_BLOCKING",
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "send_discord_message",
        "description": (
            "Schickt Piet eine Textnachricht in den Discord-Home-Channel, z. B. "
            "Ergebnisse oder Links zum Nachlesen."
        ),
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "create_kanban_task",
        "description": "Legt eine neue Aufgabe auf dem Hermes-Kanban-Board an.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "hermes_status",
        "description": (
            "Liefert einen kompakten Systemstatus: Aufgaben nach Status, aktive "
            "Worker, Abschlüsse der letzten 24 Stunden."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "schedule_reminder",
        "description": (
            "Plant eine Erinnerung, die nach N Minuten als Discord-Nachricht "
            "ankommt (1 bis 1440 Minuten)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["minutes", "text"],
        },
    },
]

NON_BLOCKING_TOOLS: frozenset[str] = frozenset({"delegate_to_hermes"})

Delegate = Callable[[str], Awaitable[str]]


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build a stable, function-response-safe error payload."""

    return {"error": {"code": code, "message": message, **details}}


def _required_text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _ensure_hermes_env() -> None:
    """Seed ``os.environ`` from ``~/.hermes/.env`` without clobbering it.

    Neither the dashboard process nor a bare ``systemd-run`` unit carries the
    .env credentials (DISCORD_BOT_TOKEN, DISCORD_HOME_CHANNEL, ...) that the
    message adapters resolve from the environment — the same gap
    ``resolve_gemini_api_key`` closes for the Gemini key.
    """

    import os

    from hermes_cli.config import load_env

    for key, value in load_env().items():
        if value is not None:
            os.environ.setdefault(key, str(value))


def _send_discord_message_sync(text: str) -> str:
    """Post ``text`` to the Discord home channel; returns the raw JSON result."""

    _ensure_hermes_env()
    from tools.send_message_tool import send_message_tool

    return send_message_tool({"target": "discord", "message": text})


def _create_kanban_task_sync(title: str, description: str | None) -> str:
    """Create a kanban task in-process and return its id."""

    from hermes_cli import kanban_db

    conn = kanban_db.connect()
    try:
        return kanban_db.create_task(
            conn, title=title, body=description, created_by="voice"
        )
    finally:
        conn.close()


def _hermes_status_sync() -> dict[str, Any]:
    """Read a compact board snapshot: status counts, active workers, throughput."""

    from hermes_cli import kanban_db

    conn = kanban_db.connect()
    try:
        stats = kanban_db.board_stats(conn)
        # Mirrors plugins/kanban/dashboard/plugin_api.py::list_active_workers'
        # WHERE clause (a running task's run row with no end and a live pid),
        # reduced to a count — this tool only needs the number, not the rows.
        active_row = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs r "
            "JOIN tasks t ON t.id = r.task_id "
            "WHERE r.ended_at IS NULL AND r.worker_pid IS NOT NULL "
            "AND t.status = 'running'"
        ).fetchone()
    finally:
        conn.close()

    result: dict[str, Any] = {}
    by_status = stats.get("by_status") if isinstance(stats, dict) else None
    if by_status:
        result["aufgaben_nach_status"] = by_status
    result["aktive_worker"] = int(active_row["n"]) if active_row is not None else 0
    completed_24h = (
        stats.get("completed_last_24h") if isinstance(stats, dict) else None
    )
    result["abgeschlossen_24h"] = int(completed_24h) if completed_24h is not None else 0
    return result


def _write_reminder_payload(text: str) -> Path:
    """Persist a reminder payload; :mod:`scripts.voice_reminder_fire` reads it later."""

    reminders_dir = get_hermes_home() / "cache" / "voice-web" / "reminders"
    reminders_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = reminders_dir / f"{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class VoiceToolExecutor:
    """Execute the small, explicit tool surface exposed to Gemini Live."""

    def __init__(self, delegate: Delegate | None):
        self._delegate = delegate

    def is_non_blocking(self, name: str) -> bool:
        return name in NON_BLOCKING_TOOLS

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

        if name == "send_discord_message":
            text = _required_text(args, "text")
            if text is None:
                return _error(
                    "invalid_arguments", "Für send_discord_message fehlt der Text."
                )
            truncated = len(text) > _DISCORD_TEXT_MAX_CHARS
            if truncated:
                text = text[:_DISCORD_TEXT_MAX_CHARS]
            try:
                raw_result = await asyncio.to_thread(_send_discord_message_sync, text)
            except Exception as exc:
                return _error(
                    "discord_send_failed",
                    "Die Discord-Nachricht konnte nicht gesendet werden.",
                    detail=str(exc)[:_MAX_ERROR_CHARS],
                )
            try:
                parsed = json.loads(raw_result)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            if not isinstance(parsed, dict) or not parsed.get("success"):
                detail = parsed.get("error") if isinstance(parsed, dict) else None
                return _error(
                    "discord_send_failed",
                    str(
                        detail or "Die Discord-Nachricht konnte nicht gesendet werden."
                    )[:_MAX_ERROR_CHARS],
                )
            result: dict[str, Any] = {"ok": True}
            if truncated:
                result["truncated"] = True
            return result

        if name == "create_kanban_task":
            title = _required_text(args, "title")
            if title is None:
                return _error(
                    "invalid_arguments", "Für create_kanban_task fehlt der Titel."
                )
            description = args.get("description")
            if not isinstance(description, str) or not description.strip():
                description = None
            try:
                task_id = await asyncio.to_thread(
                    _create_kanban_task_sync, title, description
                )
            except Exception as exc:
                return _error(
                    "kanban_task_failed",
                    "Die Kanban-Aufgabe konnte nicht angelegt werden.",
                    detail=str(exc)[:_MAX_ERROR_CHARS],
                )
            return {"task_id": task_id, "title": title}

        if name == "hermes_status":
            try:
                return await asyncio.to_thread(_hermes_status_sync)
            except Exception as exc:
                return _error(
                    "status_unavailable",
                    "Der Systemstatus konnte nicht gelesen werden.",
                    detail=str(exc)[:_MAX_ERROR_CHARS],
                )

        if name == "schedule_reminder":
            text = _required_text(args, "text")
            try:
                minutes = int(args.get("minutes"))
            except (TypeError, ValueError):
                minutes = None
            if (
                text is None
                or minutes is None
                or not (_REMINDER_MIN_MINUTES <= minutes <= _REMINDER_MAX_MINUTES)
            ):
                return _error(
                    "invalid_arguments",
                    "schedule_reminder braucht text und minutes zwischen "
                    f"{_REMINDER_MIN_MINUTES} und {_REMINDER_MAX_MINUTES}.",
                )
            payload_path = await asyncio.to_thread(_write_reminder_payload, text)
            unit = f"hermes-voice-reminder-{payload_path.stem[:8]}"
            repo_root = Path(__file__).resolve().parents[1]
            fire_script = repo_root / "scripts" / "voice_reminder_fire.py"
            command = [
                "systemd-run",
                "--user",
                "--collect",
                f"--on-active={minutes}min",
                f"--unit={unit}",
                sys.executable,
                str(fire_script),
                str(payload_path),
            ]
            try:
                process = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    capture_output=True,
                    text=True,
                    timeout=_REMINDER_SUBPROCESS_TIMEOUT_SECONDS,
                    check=False,
                )
            except FileNotFoundError:
                return _error(
                    "systemd_unavailable",
                    "systemd-run ist auf diesem System nicht verfügbar.",
                )
            except subprocess.TimeoutExpired:
                return _error(
                    "timeout",
                    "Das Planen der Erinnerung hat das Zeitlimit überschritten.",
                )
            if process.returncode != 0:
                stderr = (process.stderr or "").strip()[-_MAX_ERROR_CHARS:]
                return _error(
                    "reminder_schedule_failed",
                    "Die Erinnerung konnte nicht geplant werden.",
                    stderr=stderr,
                )
            return {"ok": True, "minuten": minutes}

        return _error("unknown_tool", f"Unbekanntes Tool: {name}")

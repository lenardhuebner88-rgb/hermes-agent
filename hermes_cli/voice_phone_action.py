"""Ephemeral, correlated confirmation broker for Voice phone actions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

PHONE_ACTION_TIMEOUT_SECONDS = 30.0
COPY_TEXT_MAX_CHARS = 4_096
SHARE_TEXT_MAX_CHARS = 8_192
URL_MAX_CHARS = 2_048
PHONE_ACTION_STATUSES = frozenset(
    {"executed", "cancelled", "timeout", "unsupported", "failed"}
)

EmitEvent = Callable[[dict[str, Any]], Awaitable[bool]]


def validate_phone_action(args: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """Validate and normalize the model-supplied action without side effects."""
    if not isinstance(args, dict):
        return None, "Die Aktion muss ein Objekt sein."
    action = args.get("action")
    if action == "open_app":
        return None, "open_app ist noch nicht sicher unterstützt."
    if action not in {"copy_text", "share_text", "open_url"}:
        return None, "Unbekannte Handy-Aktion."
    expected = "url" if action == "open_url" else "text"
    if set(args) != {"action", expected}:
        return None, "Die Aktion enthält unerlaubte oder fehlende Felder."
    value = args.get(expected)
    if not isinstance(value, str) or not value.strip():
        return None, f"{expected} darf nicht leer sein."
    if "\x00" in value or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return None, f"{expected} enthält unzulässige Steuerzeichen."
    if action == "copy_text" and len(value) > COPY_TEXT_MAX_CHARS:
        return None, "Der Text überschreitet 4096 Zeichen."
    if action == "share_text" and len(value) > SHARE_TEXT_MAX_CHARS:
        return None, "Der Text überschreitet 8192 Zeichen."
    if action == "open_url":
        if len(value) > URL_MAX_CHARS:
            return None, "Die URL überschreitet 2048 Zeichen."
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return None, "Die URL ist ungültig."
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() for char in value)
            or port is not None and not 1 <= port <= 65535
        ):
            return None, "Nur gültige HTTPS-URLs ohne Zugangsdaten sind erlaubt."
    return {"action": action, expected: value}, None


def phone_action_preview(action: dict[str, str]) -> str:
    value = action.get("url") or action.get("text") or ""
    value = " ".join(value.split())
    return value if len(value) <= 180 else value[:179].rstrip() + "…"


@dataclass
class _Pending:
    request_id: str
    action: dict[str, str]
    future: asyncio.Future[dict[str, str]]
    phase: str = "confirmation"
    expires_at_ms: int = 0


class PhoneActionBroker:
    """At most one in-memory action per WebSocket session; never persists payloads."""

    def __init__(self, emit: EmitEvent, *, timeout: float = PHONE_ACTION_TIMEOUT_SECONDS):
        self._emit = emit
        self._timeout = timeout
        self._pending: _Pending | None = None
        self._lock = asyncio.Lock()

    @property
    def pending_request_id(self) -> str | None:
        pending = self._pending
        return pending.request_id if pending is not None and not pending.future.done() else None

    async def request(self, action: dict[str, str]) -> dict[str, str]:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._pending is not None and not self._pending.future.done():
                return {"status": "failed", "code": "phone_action_busy"}
            pending = _Pending(
                request_id=secrets.token_urlsafe(24),
                action=dict(action),
                future=loop.create_future(),
                expires_at_ms=int(time.time() * 1000 + self._timeout * 1000),
            )
            self._pending = pending
        emitted = await self._emit(
            {
                "type": "phone_action_confirmation",
                "request_id": pending.request_id,
                "action": pending.action["action"],
                "preview": phone_action_preview(pending.action),
            }
        )
        if not emitted:
            self.cancel("failed")
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), self._timeout)
        except TimeoutError:
            self._finish(pending, {"status": "timeout"})
            await self._emit(
                {"type": "phone_action_closed", "request_id": pending.request_id, "status": "timeout"}
            )
            return {"status": "timeout"}
        finally:
            async with self._lock:
                if self._pending is pending:
                    self._pending = None

    async def handle_control(self, control: dict[str, Any]) -> bool:
        control_type = control.get("type")
        if control_type not in {"phone_action_decision", "phone_action_result"}:
            return False
        pending = self._pending
        request_id = control.get("request_id")
        if (
            pending is None
            or pending.future.done()
            or not isinstance(request_id, str)
            or not secrets.compare_digest(request_id, pending.request_id)
        ):
            return True
        if control_type == "phone_action_decision":
            if pending.phase != "confirmation":
                return True
            decision = control.get("decision")
            if decision == "cancelled":
                self._finish(pending, {"status": "cancelled"})
            elif decision == "confirmed":
                pending.phase = "execution"
                emitted = await self._emit(
                    {
                        "type": "phone_action_execute",
                        "request_id": pending.request_id,
                        "expires_at_ms": pending.expires_at_ms,
                        **pending.action,
                    }
                )
                if not emitted:
                    self._finish(pending, {"status": "failed"})
            return True
        if pending.phase != "execution":
            return True
        status = control.get("status")
        if status in PHONE_ACTION_STATUSES:
            self._finish(pending, {"status": status})
        return True

    def cancel(self, status: str = "cancelled") -> None:
        pending = self._pending
        if pending is not None:
            self._finish(pending, {"status": status if status in PHONE_ACTION_STATUSES else "failed"})

    @staticmethod
    def _finish(pending: _Pending, result: dict[str, str]) -> None:
        if not pending.future.done():
            pending.future.set_result(result)

"""In-memory live-screen-share sessions for the Jarvis composer.

A live share is a *genuinely continuous* session and is deliberately distinct
from the one-shot image upload (``/api/pa/upload``):

* ``Bild anhängen`` picks one static file → persists as a normal asset.
* ``Bildschirm teilen`` opens a real ``getDisplayMedia`` stream in the browser.
  While it is open the client samples the shared screen and streams the LATEST
  frame to the registry (``put_frame``). The registry keeps exactly ONE frame
  per session ("latest wins") — never a growing pile of assets. When the user
  actually asks Jarvis something, the current frame is materialised into a
  single normal upload asset so the existing image-turn pipeline consumes it.

The registry is intentionally tiny and process-local: screen frames are
ephemeral, must not survive a restart, and must not be persisted by default.
Sessions are bounded three ways — a max session count, an idle TTL, and a
per-frame byte cap — and expired sessions are swept lazily on every access so
an abandoned browser tab cannot leak memory.

This module holds only the pure, framework-free logic so it can be unit tested
without a FastAPI app; ``hermes_cli.pa_chat`` registers the HTTP routes on top.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

# Frames are downscaled JPEG/WebP (≤1280px long edge) — a few hundred KiB in
# practice. The cap is a hard defensive ceiling, well above a real frame.
LIVE_FRAME_MAX_BYTES = 4 * 1024 * 1024
# A session with no new frame and no attach for this long is considered
# abandoned (closed tab, crashed browser) and swept.
LIVE_SHARE_TTL_SECONDS = 120.0
# Screen sharing is a single-operator dashboard feature; a handful of
# concurrent sessions is plenty and bounds worst-case memory.
LIVE_SHARE_MAX_SESSIONS = 8

_SESSION_ID_PREFIX = "live_"


class LiveShareError(Exception):
    """Base class for live-share registry errors."""


class LiveShareNotFound(LiveShareError):
    """Session id is unknown or has already expired/stopped."""


class LiveShareNoFrame(LiveShareError):
    """Session exists but no frame has been received yet."""


@dataclass
class _Session:
    session_id: str
    created_at: float
    updated_at: float
    frame: bytes | None = None
    frame_suffix: str = ".jpg"


@dataclass
class LiveShareRegistry:
    """Process-local registry of ephemeral live-share sessions."""

    ttl_seconds: float = LIVE_SHARE_TTL_SECONDS
    max_sessions: int = LIVE_SHARE_MAX_SESSIONS
    max_frame_bytes: int = LIVE_FRAME_MAX_BYTES
    clock: Callable[[], float] = time.time
    _sessions: dict[str, _Session] = field(default_factory=dict)

    # -- internal -----------------------------------------------------------
    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else float(now)

    def _sweep(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        expired = [
            sid for sid, session in self._sessions.items() if session.updated_at < cutoff
        ]
        for sid in expired:
            del self._sessions[sid]

    def _get_live(self, session_id: str, now: float) -> _Session:
        self._sweep(now)
        session = self._sessions.get(session_id)
        if session is None:
            raise LiveShareNotFound(session_id)
        return session

    # -- public API ---------------------------------------------------------
    def start(self, *, now: float | None = None) -> str:
        """Open a fresh session and return its id.

        If the registry is already at capacity the OLDEST session is evicted
        first (a stalled tab must never block a new share)."""
        moment = self._now(now)
        self._sweep(moment)
        if len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.updated_at)
            del self._sessions[oldest.session_id]
        session_id = f"{_SESSION_ID_PREFIX}{secrets.token_hex(12)}"
        self._sessions[session_id] = _Session(
            session_id=session_id, created_at=moment, updated_at=moment
        )
        return session_id

    def put_frame(
        self, session_id: str, data: bytes, suffix: str, *, now: float | None = None
    ) -> None:
        """Store the newest frame for a session (latest wins)."""
        moment = self._now(now)
        session = self._get_live(session_id, moment)
        if len(data) > self.max_frame_bytes:
            raise LiveShareError("frame exceeds byte cap")
        session.frame = data
        session.frame_suffix = suffix
        session.updated_at = moment

    def latest_frame(
        self, session_id: str, *, now: float | None = None
    ) -> tuple[bytes, str]:
        """Return the current frame ``(bytes, suffix)`` and mark activity."""
        moment = self._now(now)
        session = self._get_live(session_id, moment)
        if session.frame is None:
            raise LiveShareNoFrame(session_id)
        session.updated_at = moment
        return session.frame, session.frame_suffix

    def stop(self, session_id: str, *, now: float | None = None) -> bool:
        """Drop a session. Idempotent: returns True iff a session was removed."""
        moment = self._now(now)
        self._sweep(moment)
        return self._sessions.pop(session_id, None) is not None

    def active_count(self, *, now: float | None = None) -> int:
        moment = self._now(now)
        self._sweep(moment)
        return len(self._sessions)

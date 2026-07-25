"""Fork-owned cancellation seam for gateway hygiene compression leases.

Gateway hygiene runs synchronous compression in an executor. Its commit fence
prevents a worker that outlives the gateway timeout from mutating the
transcript, but the worker may already own the durable per-session compression
lease. Keeping that lease while the live turn continues makes normal SessionDB
appends fail with ``CompressionSessionBusyError``.

The guard is attached only to the temporary hygiene agent. The compression
lock-acquire seam delegates every normal agent unchanged; for a guarded agent,
it serializes acquisition against timeout cancellation. Cancellation therefore
either wins before acquisition or releases the just-recorded lease, without
replacing the SessionDB object bound to the agent and compressor.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_AGENT_GUARD_ATTR = "_gateway_hygiene_compression_lock_guard"


class HygieneCompressionLockGuard:
    """Track and cancel compression leases owned by one hygiene agent."""

    def __init__(self, session_db: Any | None) -> None:
        self._session_db = session_db
        self._gate = threading.Lock()
        self._cancelled = False
        self._owned_leases: set[tuple[str, str]] = set()

    def try_acquire(
        self,
        acquire: Callable[..., bool],
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Acquire only while the gateway is still waiting for this worker."""
        with self._gate:
            if self._cancelled:
                return False
            acquired = bool(acquire(session_id, holder, ttl_seconds=ttl_seconds))
            if acquired:
                self._owned_leases.add((session_id, holder))
            return acquired

    def cancel(self) -> int:
        """Release acquired leases and permanently reject later acquisitions.

        The same gate serializes cancellation with an in-flight acquire. Thus
        cancellation either marks the guard first (the acquire returns False)
        or waits for the acquire to be recorded and then releases it.
        """
        with self._gate:
            self._cancelled = True
            leases = tuple(self._owned_leases)
            self._owned_leases.clear()
            for session_id, holder in leases:
                try:
                    self._session_db.release_compression_lock(session_id, holder)
                except Exception:
                    logger.warning(
                        "Failed to release timed-out hygiene compression lease "
                        "for session %s",
                        session_id,
                        exc_info=True,
                    )
            return len(leases)


def install_hygiene_compression_lock_guard(
    agent: Any,
    session_db: Any | None,
) -> HygieneCompressionLockGuard:
    """Attach a timeout guard without replacing the agent's SessionDB."""
    guard = HygieneCompressionLockGuard(session_db)
    setattr(agent, _AGENT_GUARD_ATTR, guard)
    return guard


def try_acquire_compression_lock_with_guard(
    agent: Any,
    acquire: Callable[..., bool],
    session_id: str,
    holder: str,
    *,
    ttl_seconds: float,
) -> bool:
    """Run the core lock acquire through an optional hygiene timeout guard."""
    guard = getattr(agent, _AGENT_GUARD_ATTR, None)
    if isinstance(guard, HygieneCompressionLockGuard):
        return guard.try_acquire(
            acquire,
            session_id,
            holder,
            ttl_seconds=ttl_seconds,
        )
    return bool(acquire(session_id, holder, ttl_seconds=ttl_seconds))

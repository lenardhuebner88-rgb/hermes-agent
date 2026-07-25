"""Fork regressions for the gateway hygiene timeout lease guard."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent.hygiene_compression_lock_guard import (
    install_hygiene_compression_lock_guard,
    try_acquire_compression_lock_with_guard,
)
from hermes_state import CompressionSessionBusyError, SessionDB


def test_timeout_cancel_releases_session_for_live_turn(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "discord-timeout"
    holder = "pid=1:tid=2:agent=hygiene:nonce=test"
    db.create_session(session_id, source="discord")
    agent = SimpleNamespace()
    timeout_guard = install_hygiene_compression_lock_guard(agent, db)

    assert try_acquire_compression_lock_with_guard(
        agent,
        db.try_acquire_compression_lock,
        session_id,
        holder,
        ttl_seconds=300.0,
    )
    with pytest.raises(CompressionSessionBusyError):
        db.append_message(session_id, "user", "blocked while hygiene owns lease")

    assert timeout_guard.cancel() == 1

    db.append_message(session_id, "user", "live turn after hygiene timeout")
    assert db.get_compression_lock_holder(session_id) is None
    assert not try_acquire_compression_lock_with_guard(
        agent,
        db.try_acquire_compression_lock,
        session_id,
        holder,
        ttl_seconds=300.0,
    )


def test_cancel_serializes_with_inflight_acquire() -> None:
    acquire_entered = threading.Event()
    finish_acquire = threading.Event()

    class BlockingDB:
        def __init__(self) -> None:
            self.released: list[tuple[str, str]] = []

        def try_acquire_compression_lock(
            self,
            _session_id: str,
            _holder: str,
            ttl_seconds: float = 300.0,
        ) -> bool:
            del ttl_seconds
            acquire_entered.set()
            assert finish_acquire.wait(timeout=2)
            return True

        def release_compression_lock(self, session_id: str, holder: str) -> None:
            self.released.append((session_id, holder))

    db = BlockingDB()
    agent = SimpleNamespace()
    timeout_guard = install_hygiene_compression_lock_guard(agent, db)
    acquire_result: list[bool] = []
    cancel_result: list[int] = []

    acquire_thread = threading.Thread(
        target=lambda: acquire_result.append(
            try_acquire_compression_lock_with_guard(
                agent,
                db.try_acquire_compression_lock,
                "session",
                "holder",
                ttl_seconds=300.0,
            )
        )
    )
    cancel_thread = threading.Thread(
        target=lambda: cancel_result.append(timeout_guard.cancel())
    )
    acquire_thread.start()
    assert acquire_entered.wait(timeout=2)
    cancel_thread.start()
    finish_acquire.set()
    acquire_thread.join(timeout=2)
    cancel_thread.join(timeout=2)

    assert not acquire_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert acquire_result == [True]
    assert cancel_result == [1]
    assert db.released == [("session", "holder")]
    assert not try_acquire_compression_lock_with_guard(
        agent,
        db.try_acquire_compression_lock,
        "session",
        "late-holder",
        ttl_seconds=300.0,
    )


def test_none_session_db_preserves_existing_no_db_path() -> None:
    agent = SimpleNamespace()
    timeout_guard = install_hygiene_compression_lock_guard(agent, None)

    assert timeout_guard.cancel() == 0


def test_unguarded_agent_delegates_acquire_unchanged() -> None:
    calls: list[tuple[str, str, float]] = []

    def acquire(
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        calls.append((session_id, holder, ttl_seconds))
        return True

    assert try_acquire_compression_lock_with_guard(
        SimpleNamespace(),
        acquire,
        "session",
        "holder",
        ttl_seconds=12.0,
    )
    assert calls == [("session", "holder", 12.0)]

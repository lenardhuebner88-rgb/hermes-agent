"""Tests for stale-session reaper (SessionDB.close_stale_sessions + script).

Uses the real SessionDB schema (not a simplified CREATE TABLE). Fixture builds
three sessions: open+fresh, open+stale (old messages), already-ended.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB

# 10 days ago / 1 day ago relative to a fixed wall clock.
_NOW = 1_700_000_000.0
_DAY = 86400.0
_STALE_MSG_TS = _NOW - 10 * _DAY
_FRESH_MSG_TS = _NOW - 1 * _DAY
_OLDER_THAN = int(7 * _DAY)


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _patch_session_meta(
    db: SessionDB,
    session_id: str,
    *,
    started_at: float,
    title: str | None = None,
    display_name: str | None = None,
) -> None:
    """create_session always stamps started_at=now; tests need controlled ages."""

    def _do(conn):
        conn.execute(
            "UPDATE sessions SET started_at = ?, title = COALESCE(?, title), "
            "display_name = COALESCE(?, display_name) WHERE id = ?",
            (started_at, title, display_name, session_id),
        )

    db._execute_write(_do)


def _seed_three(db: SessionDB) -> None:
    """open+fresh, open+stale (old messages), already-ended."""
    db.create_session(session_id="sess-fresh", source="cli", model="test")
    _patch_session_meta(
        db, "sess-fresh", started_at=_NOW - 2 * _DAY, title="Fresh work"
    )
    db.append_message(
        "sess-fresh",
        role="user",
        content="recent activity",
        timestamp=_FRESH_MSG_TS,
    )

    db.create_session(session_id="sess-stale", source="cli", model="test")
    _patch_session_meta(
        db,
        "sess-stale",
        started_at=_NOW - 20 * _DAY,
        title="Stale abandoned",
        display_name="stale-label",
    )
    db.append_message(
        "sess-stale",
        role="user",
        content="old activity",
        timestamp=_STALE_MSG_TS,
    )

    db.create_session(session_id="sess-ended", source="cli", model="test")
    _patch_session_meta(
        db, "sess-ended", started_at=_NOW - 30 * _DAY, title="Already closed"
    )
    db.append_message(
        "sess-ended",
        role="user",
        content="historical",
        timestamp=_NOW - 25 * _DAY,
    )
    # Use direct write so ended_at is controlled (end_session uses time.time()).
    def _end(conn):
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (_NOW - 24 * _DAY, "agent_close", "sess-ended"),
        )

    db._execute_write(_end)


class TestCloseStaleSessions:
    def test_dry_run_lists_only_stale_and_writes_nothing(self, db):
        _seed_three(db)

        candidates = db.close_stale_sessions(
            older_than_seconds=_OLDER_THAN,
            now=_NOW,
            dry_run=True,
        )

        ids = {c["id"] for c in candidates}
        assert ids == {"sess-stale"}
        assert candidates[0]["title"] == "Stale abandoned"
        assert candidates[0]["display_name"] == "stale-label"
        assert candidates[0]["last_active"] == pytest.approx(_STALE_MSG_TS)
        assert candidates[0]["age_days"] == pytest.approx(10.0)

        # Nothing written: all three retain prior ended_at state.
        assert db.get_session("sess-stale")["ended_at"] is None
        assert db.get_session("sess-fresh")["ended_at"] is None
        assert db.get_session("sess-ended")["ended_at"] is not None
        assert db.get_session("sess-ended")["end_reason"] == "agent_close"

    def test_apply_closes_stale_with_honest_ended_at(self, db):
        _seed_three(db)

        candidates = db.close_stale_sessions(
            older_than_seconds=_OLDER_THAN,
            now=_NOW,
            dry_run=False,
        )

        assert {c["id"] for c in candidates} == {"sess-stale"}

        stale = db.get_session("sess-stale")
        assert stale["ended_at"] == pytest.approx(_STALE_MSG_TS)
        assert stale["end_reason"] == "stale_sweep"

        # Fresh and already-ended untouched.
        assert db.get_session("sess-fresh")["ended_at"] is None
        ended = db.get_session("sess-ended")
        assert ended["end_reason"] == "agent_close"
        assert ended["ended_at"] == pytest.approx(_NOW - 24 * _DAY)

    def test_apply_idempotent_second_run_zero(self, db):
        _seed_three(db)
        first = db.close_stale_sessions(
            older_than_seconds=_OLDER_THAN,
            now=_NOW,
            dry_run=False,
        )
        assert len(first) == 1

        second = db.close_stale_sessions(
            older_than_seconds=_OLDER_THAN,
            now=_NOW,
            dry_run=False,
        )
        assert second == []

        # Still closed with first-reason / honest end time.
        stale = db.get_session("sess-stale")
        assert stale["ended_at"] == pytest.approx(_STALE_MSG_TS)
        assert stale["end_reason"] == "stale_sweep"

    def test_stale_without_messages_uses_started_at(self, db):
        """No messages → last_active falls back to started_at."""
        db.create_session(
            session_id="sess-empty-stale",
            source="cli",
            model="test",
        )
        _patch_session_meta(
            db,
            "sess-empty-stale",
            started_at=_NOW - 15 * _DAY,
            title="Empty stale",
        )
        candidates = db.close_stale_sessions(
            older_than_seconds=_OLDER_THAN,
            now=_NOW,
            dry_run=False,
        )
        assert {c["id"] for c in candidates} == {"sess-empty-stale"}
        row = db.get_session("sess-empty-stale")
        assert row["ended_at"] == pytest.approx(_NOW - 15 * _DAY)
        assert row["end_reason"] == "stale_sweep"


class TestReapStaleSessionsScript:
    def test_script_dry_run_and_apply(self, tmp_path):
        state_db = tmp_path / "state.db"
        session_db = SessionDB(db_path=state_db)
        try:
            _seed_three(session_db)
        finally:
            session_db.close()

        script = Path(__file__).resolve().parent.parent / "scripts" / "reap_stale_sessions.py"
        env_python = sys.executable

        # Dry-run
        proc = subprocess.run(
            [
                env_python,
                str(script),
                "--state-db",
                str(state_db),
                "--days",
                "7",
                "--now",
                str(_NOW),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "reaped 1 sessions (dry-run)" in proc.stdout
        assert "Stale abandoned" in proc.stdout

        # Still open after dry-run
        check = SessionDB(db_path=state_db)
        try:
            assert check.get_session("sess-stale")["ended_at"] is None
        finally:
            check.close()

        # Apply (backup lands under state_db.parent/backups/)
        proc2 = subprocess.run(
            [
                env_python,
                str(script),
                "--state-db",
                str(state_db),
                "--days",
                "7",
                "--apply",
                "--now",
                str(_NOW),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc2.returncode == 0, proc2.stderr
        assert "reaped 1 sessions (applied)" in proc2.stdout
        assert "backup:" in proc2.stdout

        check2 = SessionDB(db_path=state_db)
        try:
            row = check2.get_session("sess-stale")
            assert row["end_reason"] == "stale_sweep"
            assert row["ended_at"] == pytest.approx(_STALE_MSG_TS)
            # Second apply → 0
            third = check2.close_stale_sessions(
                older_than_seconds=_OLDER_THAN,
                now=_NOW,
                dry_run=False,
            )
            assert third == []
        finally:
            check2.close()

        backups = list((tmp_path / "backups").glob("state.db.*.before-stale-sweep.db"))
        assert len(backups) == 1

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermes_cli import session_reaper
from hermes_state import SessionDB

_NOW = 1_700_000_000.0
_HOUR = 60 * 60.0


def _set_started_at(db: SessionDB, session_id: str, started_at: float) -> None:
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (started_at, session_id),
        )
    )


def _add_session(
    db: SessionDB,
    session_id: str,
    *,
    source: str,
    started_hours_ago: float,
    message_hours_ago: float | None = None,
) -> None:
    db.create_session(session_id=session_id, source=source, model="test")
    _set_started_at(db, session_id, _NOW - started_hours_ago * _HOUR)
    if message_hours_ago is not None:
        db.append_message(
            session_id,
            role="user",
            content="activity",
            timestamp=_NOW - message_hours_ago * _HOUR,
        )


@pytest.fixture()
def fixture_state_db(tmp_path: Path) -> Path:
    state_db = tmp_path / "state.db"
    db = SessionDB(db_path=state_db)
    try:
        _add_session(db, "expired-cron", source="cron", started_hours_ago=7)
        _add_session(db, "young-cron", source="cron", started_hours_ago=5)
        _add_session(
            db,
            "active-cron",
            source="cron",
            started_hours_ago=12,
            message_hours_ago=1,
        )
        _add_session(db, "expired-gateway", source="gateway", started_hours_ago=49)
        _add_session(db, "young-cli", source="cli", started_hours_ago=47)
        _add_session(
            db,
            "active-cli",
            source="cli",
            started_hours_ago=72,
            message_hours_ago=2,
        )
        _add_session(db, "already-ended", source="cron", started_hours_ago=100)
        db.end_session("already-ended", "agent_close")
    finally:
        db.close()
    return state_db


def _sessions(state_db: Path) -> dict[str, dict[str, Any]]:
    db = SessionDB(db_path=state_db)
    try:
        ids = (
            "expired-cron",
            "young-cron",
            "active-cron",
            "expired-gateway",
            "young-cli",
            "active-cli",
            "already-ended",
        )
        rows: dict[str, dict[str, Any]] = {}
        for session_id in ids:
            row = db.get_session(session_id)
            assert row is not None
            rows[session_id] = row
        return rows
    finally:
        db.close()


def test_default_dry_run_reports_exact_candidates_without_writes(
    fixture_state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = session_reaper.main([
        "--state-db",
        str(fixture_state_db),
        "--now",
        str(_NOW),
    ])

    assert result == 0
    output = capsys.readouterr().out
    assert "expired-cron" in output
    assert "expired-gateway" in output
    assert "young-cron" not in output
    assert "active-cron" not in output
    assert "young-cli" not in output
    assert "active-cli" not in output
    assert "expired 2 sessions (dry-run)" in output

    rows = _sessions(fixture_state_db)
    assert rows["expired-cron"]["ended_at"] is None
    assert rows["expired-gateway"]["ended_at"] is None
    assert rows["already-ended"]["end_reason"] == "agent_close"
    assert not (fixture_state_db.parent / "backups").exists()


def test_apply_expires_exact_candidates_and_is_idempotent(
    fixture_state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--state-db",
        str(fixture_state_db),
        "--now",
        str(_NOW),
        "--apply",
    ]

    assert session_reaper.main(args) == 0
    first_output = capsys.readouterr().out
    assert "expired 2 sessions (applied)" in first_output

    rows = _sessions(fixture_state_db)
    assert rows["expired-cron"]["ended_at"] == pytest.approx(_NOW - 7 * _HOUR)
    assert rows["expired-cron"]["end_reason"] == "expired"
    assert rows["expired-gateway"]["ended_at"] == pytest.approx(_NOW - 49 * _HOUR)
    assert rows["expired-gateway"]["end_reason"] == "expired"
    for session_id in ("young-cron", "active-cron", "young-cli", "active-cli"):
        assert rows[session_id]["ended_at"] is None
        assert rows[session_id]["end_reason"] is None
    assert rows["already-ended"]["end_reason"] == "agent_close"

    backups = list(
        (fixture_state_db.parent / "backups").glob(
            "state.db.*.before-session-reaper.db"
        )
    )
    assert len(backups) == 1

    assert session_reaper.main(args) == 0
    assert "expired 0 sessions (applied)" in capsys.readouterr().out


def test_apply_aborts_without_writes_when_backup_fails(
    fixture_state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(session_reaper, "_safe_copy_db", lambda _src, _dst: False)

    result = session_reaper.main([
        "--state-db",
        str(fixture_state_db),
        "--now",
        str(_NOW),
        "--apply",
    ])

    assert result == 1
    assert "Backup failed" in capsys.readouterr().err
    rows = _sessions(fixture_state_db)
    assert rows["expired-cron"]["ended_at"] is None
    assert rows["expired-gateway"]["ended_at"] is None

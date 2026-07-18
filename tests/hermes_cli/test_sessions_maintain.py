import sqlite3
import sys
import time
from pathlib import Path

import yaml

from hermes_state import SessionDB


def _write_config(home: Path, **sessions) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"sessions": sessions}), encoding="utf-8"
    )


def _make_ended_session(home: Path, session_id: str, days_old: int) -> None:
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=session_id, source="cli", model="test")
        db.append_message(
            session_id=session_id,
            role="user",
            content=f"message for {session_id}",
        )
        db.end_session(session_id, end_reason="done")
        old = time.time() - days_old * 86400
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (old, old + 1, session_id),
        )
        db._conn.commit()
    finally:
        db.close()


def _run_maintain(monkeypatch, capsys, hermes_home: Path, *args: str) -> str:
    import hermes_cli.main as main_mod

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: hermes_home.parent)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "sessions", "maintain", *args],
    )
    main_mod.main()
    return capsys.readouterr().out


def _session_exists(home: Path, session_id: str) -> bool:
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        return db.get_session(session_id) is not None
    finally:
        db.close()


def _last_auto_prune(home: Path) -> str | None:
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        return db.get_meta("last_auto_prune")
    finally:
        db.close()


def test_sessions_maintain_all_profiles_uses_each_home_retention(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    archived = root / "profiles" / "_archived" / "retired"
    _write_config(
        root,
        auto_prune=True,
        retention_days=90,
        min_interval_hours=24,
        vacuum_after_prune=False,
    )
    _write_config(
        profile,
        auto_prune=True,
        retention_days=30,
        min_interval_hours=24,
        vacuum_after_prune=False,
    )
    _write_config(
        archived,
        auto_prune=True,
        retention_days=1,
        min_interval_hours=24,
        vacuum_after_prune=False,
    )
    _make_ended_session(root, "root-60d", 60)
    _make_ended_session(root, "root-120d", 120)
    _make_ended_session(profile, "profile-60d", 60)
    _make_ended_session(archived, "archived-60d", 60)

    output = _run_maintain(
        monkeypatch, capsys, root, "--all-profiles", "--force"
    )

    assert _session_exists(root, "root-60d")
    assert not _session_exists(root, "root-120d")
    assert not _session_exists(profile, "profile-60d")
    assert _session_exists(archived, "archived-60d")
    assert str(root / "state.db") in output
    assert str(profile / "state.db") in output
    assert str(archived / "state.db") not in output
    assert _last_auto_prune(root) is not None
    assert _last_auto_prune(profile) is not None


def test_sessions_maintain_skips_auto_prune_false_home(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / ".hermes"
    disabled = root / "profiles" / "disabled"
    _write_config(
        root,
        auto_prune=True,
        retention_days=90,
        vacuum_after_prune=False,
    )
    _write_config(disabled, auto_prune=False, retention_days=1)
    _make_ended_session(root, "root-old", 120)
    _make_ended_session(disabled, "disabled-old", 120)

    output = _run_maintain(
        monkeypatch, capsys, root, "--all-profiles", "--force"
    )

    assert not _session_exists(root, "root-old")
    assert _session_exists(disabled, "disabled-old")
    assert "auto_prune disabled" in output
    assert _last_auto_prune(disabled) is None


def test_sessions_maintain_dry_run_is_read_only(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / ".hermes"
    _write_config(root, auto_prune=True, retention_days=30)
    _make_ended_session(root, "old", 60)
    db_path = root / "state.db"
    before_mtime = db_path.stat().st_mtime_ns

    output = _run_maintain(monkeypatch, capsys, root, "--dry-run")

    assert "candidates 1" in output
    assert "vacuum skipped (dry-run)" in output
    assert _session_exists(root, "old")
    assert _last_auto_prune(root) is None
    assert db_path.stat().st_mtime_ns == before_mtime


def test_sessions_maintain_force_bypasses_interval(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / ".hermes"
    _write_config(
        root,
        auto_prune=True,
        retention_days=30,
        min_interval_hours=24,
        vacuum_after_prune=False,
    )
    _make_ended_session(root, "old", 60)
    db = SessionDB(db_path=root / "state.db")
    try:
        db.set_meta("last_auto_prune", str(time.time()))
    finally:
        db.close()

    output = _run_maintain(monkeypatch, capsys, root)
    assert _session_exists(root, "old")
    assert "minimum interval not elapsed" in output

    output = _run_maintain(monkeypatch, capsys, root, "--force")
    assert not _session_exists(root, "old")
    assert "pruned 1" in output


def test_sessions_maintain_vacuum_lock_does_not_abort(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / ".hermes"
    _write_config(
        root,
        auto_prune=True,
        retention_days=30,
        vacuum_after_prune=True,
    )
    _make_ended_session(root, "old", 60)

    real_vacuum = SessionDB.vacuum

    def _vacuum_while_locked(db):
        lock = sqlite3.connect(str(db.db_path), isolation_level=None)
        try:
            lock.execute("BEGIN IMMEDIATE")
            return real_vacuum(db)
        finally:
            lock.rollback()
            lock.close()

    monkeypatch.setattr(SessionDB, "vacuum", _vacuum_while_locked)

    output = _run_maintain(monkeypatch, capsys, root, "--force")

    assert not _session_exists(root, "old")
    assert "pruned 1" in output
    assert "vacuum skipped" in output

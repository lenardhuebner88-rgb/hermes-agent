"""Tests for session display_name write-time labeling + one-shot backfill.

Covers B6: presentational labels via ``display_name`` (NOT unique ``title``).
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from hermes_state import SessionDB, derive_session_label

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_session_labels.py"
_spec = importlib.util.spec_from_file_location("backfill_session_labels", _SCRIPT)
assert _spec is not None and _spec.loader is not None
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


# ---------------------------------------------------------------------------
# derive_session_label unit cases
# ---------------------------------------------------------------------------


def test_derive_session_label_basic():
    assert derive_session_label("  hello   world  ") == "hello world"


def test_derive_session_label_first_line_only():
    assert derive_session_label("line one\nline two") == "line one"


def test_derive_session_label_truncates_to_80_with_ellipsis():
    long = "a" * 100
    label = derive_session_label(long)
    assert label is not None
    assert len(label) == 80
    assert label.endswith("…")
    assert label == ("a" * 79) + "…"


def test_derive_session_label_rejects_empty_and_symbols():
    assert derive_session_label(None) is None
    assert derive_session_label("") is None
    assert derive_session_label("   \n\t  ") is None
    assert derive_session_label("!!! ??? …") is None
    assert derive_session_label("---") is None


def test_derive_session_label_multimodal_text_parts():
    content = [
        {"type": "text", "text": "  photo caption  "},
        {"type": "image_url", "image_url": {"url": "http://x"}},
    ]
    assert derive_session_label(content) == "photo caption"


# ---------------------------------------------------------------------------
# Write-time (append_message)
# ---------------------------------------------------------------------------


def test_write_time_first_user_message_sets_display_name(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s_new", source="cli")
        long = "x" * 100
        db.append_message("s_new", "user", long)
        row = db.get_session("s_new")
        assert row is not None
        assert row["display_name"] == ("x" * 79) + "…"
        assert len(row["display_name"]) == 80
        assert row["title"] is None
    finally:
        db.close()


def test_write_time_preserves_existing_display_name(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s_named", source="cli")
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET display_name = ? WHERE id = ?",
                ("Keep Me", "s_named"),
            )
        )
        db.append_message("s_named", "user", "should not replace existing label")
        row = db.get_session("s_named")
        assert row["display_name"] == "Keep Me"
    finally:
        db.close()


def test_write_time_second_user_message_does_not_change_label(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s_second", source="cli")
        db.append_message("s_second", "user", "first question about cats")
        db.append_message("s_second", "assistant", "meow")
        db.append_message("s_second", "user", "totally different second message")
        row = db.get_session("s_second")
        assert row["display_name"] == "first question about cats"
    finally:
        db.close()


def test_write_time_skips_when_title_already_set(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s_titled", source="cli")
        db.set_session_title("s_titled", "Explicit Title")
        db.append_message("s_titled", "user", "user text should not become display_name")
        row = db.get_session("s_titled")
        assert row["title"] == "Explicit Title"
        assert row["display_name"] is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sweep script
# ---------------------------------------------------------------------------


def _seed_sweep_db(db_path: Path) -> SessionDB:
    """Build a fixture DB with candidates and non-candidates for the sweep."""
    db = SessionDB(db_path=db_path)

    # Candidate: neither display_name nor title, has user message
    db.create_session("cand_ok", source="cli")
    db.append_message("cand_ok", "assistant", "preamble")  # non-user first
    # Direct SQL insert so write-time path doesn't pre-label (tests sweep only).
    # Clear any write-time label after a user message, then re-seed content via SQL.
    # Simpler: create session, append user (write-time sets label), then NULL it out.
    db.append_message("cand_ok", "user", "backfill me please")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET display_name = NULL, title = NULL WHERE id = ?",
            ("cand_ok",),
        )
    )

    # Non-candidate: already has display_name
    db.create_session("has_dn", source="cli")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET display_name = ? WHERE id = ?",
            ("Already Named", "has_dn"),
        )
    )
    db.append_message("has_dn", "user", "ignore this content for label")

    # Non-candidate: has title
    db.create_session("has_title", source="cli")
    db.set_session_title("has_title", "Titled Session")
    # Write-time skips because title is set; still insert user msg.
    db.append_message("has_title", "user", "has a title already")

    # Non-candidate: no user message
    db.create_session("no_user", source="cli")
    db.append_message("no_user", "assistant", "only assistant")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET display_name = NULL, title = NULL WHERE id = ?",
            ("no_user",),
        )
    )

    # Second candidate for idempotency / multi-row
    db.create_session("cand_two", source="cli")
    db.append_message("cand_two", "user", "second candidate label")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET display_name = NULL, title = NULL WHERE id = ?",
            ("cand_two",),
        )
    )

    return db


def test_sweep_labels_candidates_only(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = _seed_sweep_db(db_path)
    db.close()

    monkeypatch.setattr(backfill, "get_hermes_home", lambda: tmp_path)

    rc = backfill.run(state_db=db_path, apply=True, now=None)
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        by_id = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, display_name, title FROM sessions"
            ).fetchall()
        }
        assert by_id["cand_ok"]["display_name"] == "backfill me please"
        assert by_id["cand_two"]["display_name"] == "second candidate label"
        assert by_id["has_dn"]["display_name"] == "Already Named"
        assert by_id["has_title"]["title"] == "Titled Session"
        assert by_id["has_title"]["display_name"] is None
        assert by_id["no_user"]["display_name"] is None
    finally:
        conn.close()

    # Backup must exist under hermes home backups/
    backups = list((tmp_path / "backups").glob("state.db.*.before-label-backfill.db"))
    assert len(backups) == 1


def test_sweep_dry_run_writes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = _seed_sweep_db(db_path)
    db.close()

    monkeypatch.setattr(backfill, "get_hermes_home", lambda: tmp_path)

    rc = backfill.run(state_db=db_path, apply=False)
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cand = conn.execute(
            "SELECT display_name FROM sessions WHERE id = 'cand_ok'"
        ).fetchone()
        assert cand["display_name"] is None
    finally:
        conn.close()

    # No backup on dry-run
    assert not (tmp_path / "backups").exists() or not list(
        (tmp_path / "backups").glob("*.before-label-backfill.db")
    )


def test_sweep_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = _seed_sweep_db(db_path)
    db.close()
    monkeypatch.setattr(backfill, "get_hermes_home", lambda: tmp_path)

    assert backfill.run(state_db=db_path, apply=True) == 0
    # Second apply: candidates now have display_name → 0 updates
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        before = {
            r["id"]: r["display_name"]
            for r in conn.execute("SELECT id, display_name FROM sessions")
        }

    assert backfill.run(state_db=db_path, apply=True) == 0

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        after = {
            r["id"]: r["display_name"]
            for r in conn.execute("SELECT id, display_name FROM sessions")
        }
    assert after == before


def test_cli_main_dry_run(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "state.db"
    db = _seed_sweep_db(db_path)
    db.close()
    monkeypatch.setattr(backfill, "get_hermes_home", lambda: tmp_path)

    rc = backfill.main(["--state-db", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "labeled" in out


# ---------------------------------------------------------------------------
# Edge pinning
# ---------------------------------------------------------------------------

def test_iter_candidates_limit_zero_returns_nothing(tmp_path):
    """limit=0 means 'no candidates at all' (LIMIT 0), not unlimited — an
    off-by-one here would label the whole board when the operator asked
    for zero rows."""
    db_path = tmp_path / "state.db"
    _seed_sweep_db(db_path)
    conn = backfill._open_ro(db_path)
    try:
        assert backfill.iter_candidates(conn, limit=0) == []
        assert len(backfill.iter_candidates(conn, limit=1)) == 1
    finally:
        conn.close()


def test_format_examples_caps_at_exactly_max_examples():
    """The example list stops AT the cap (default 20) — 25 labeled
    candidates print exactly 20 lines, and a smaller cap is inclusive."""
    cands = [(f"s{i}", "x", f"label {i}") for i in range(25)]
    assert len(backfill._format_examples(cands)) == 20
    assert len(backfill._format_examples(cands[:3], max_examples=2)) == 2


def test_run_missing_state_db_returns_exit_code_two(tmp_path):
    """A missing state db is a usage error with exit code 2 — scripts
    key off that code to distinguish 'nothing to do' from 'crashed'."""
    assert backfill.run(state_db=tmp_path / "missing.db") == 2


# ---------------------------------------------------------------------------
# Third pass: update counting, backups, defaults
# ---------------------------------------------------------------------------

def test_apply_labels_counts_only_rows_actually_updated():
    """The update counter starts at 0 and only counts rows the COALESCE
    guard actually wrote — a blocked row (display_name already set)
    contributes nothing."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sessions (id TEXT, display_name TEXT, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', NULL, NULL)")
    conn.execute("INSERT INTO sessions VALUES ('s2', 'existing', NULL)")

    n = backfill.apply_labels(
        conn,
        [
            ("s1", "c", "label one"),
            ("s2", "c", "label two"),
            ("s3", "c", "label three"),  # unknown session -> rowcount 0
        ],
    )
    assert n == 1


def test_backup_state_db_tolerates_existing_backups_dir(tmp_path, monkeypatch):
    """The second backfill of the night meets an existing backups dir —
    mkdir must not raise."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "backups").mkdir()
    src = tmp_path / "state.db"
    handle = sqlite3.connect(src)
    handle.execute("CREATE TABLE t (x)")
    handle.commit()
    handle.close()

    dst = backfill.backup_state_db(src, now=None)
    assert dst.exists()


def test_run_defaults_to_dry_run(tmp_path, monkeypatch):
    """run() without apply is a dry-run: no backup, no writes — the
    default must never silently flip to apply."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "state.db"
    SessionDB(db_path=db)

    assert backfill.run(state_db=db) == 0
    assert not (tmp_path / "backups").exists()


def test_parse_now_preserves_explicit_utc_offset():
    """An explicit +05:00 offset survives parsing unchanged — the tz
    normalisation only adds UTC to NAIVE stamps."""
    from datetime import timedelta

    parsed = backfill._parse_now("2026-01-01T05:00:00+05:00")
    assert parsed.utcoffset() == timedelta(hours=5)


def test_main_uses_explicit_state_db_not_default(tmp_path, monkeypatch):
    """--state-db wins over the HERMES_HOME default — the default path
    must not shadow an explicit argument."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "custom-state.db"
    SessionDB(db_path=db)

    assert backfill.main(["--state-db", str(db)]) == 0

"""Regression coverage for the read-only blocked-card renderer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Sequence

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "render_kanban_escalation_lines.py"
)
_spec = importlib.util.spec_from_file_location("render_kanban_escalation_lines", _SCRIPT)
assert _spec is not None and _spec.loader is not None
renderer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renderer)


def _write_board(
    home: Path, slug: str, rows: Sequence[tuple[str, str, str | None]]
) -> Path:
    board_dir = home / "kanban" / "boards" / slug
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "board.json").write_text(
        json.dumps({"slug": slug, "name": slug.replace("-", " ").title()}),
        encoding="utf-8",
    )
    db = home / "kanban.db" if slug == "default" else board_dir / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            block_kind TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO tasks (id, title, status, block_kind) VALUES (?, ?, 'blocked', ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO task_events (task_id, kind, payload) VALUES (?, 'blocked', ?)",
        [
            (task_id, json.dumps({"reason": f"reason for {task_id}"}))
            for task_id, _title, _kind in rows
        ],
    )
    conn.commit()
    conn.close()
    return db


def _append_event(db: Path, task_id: str, kind: str, payload: dict | None = None) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, kind, json.dumps(payload or {})),
    )
    conn.commit()
    conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_discovers_all_board_configs_and_maps_default_to_top_level_db(tmp_path):
    home = tmp_path / ".hermes"
    expected = {
        "default": _write_board(home, "default", []),
        "buzz-platform": _write_board(home, "buzz-platform", []),
        "health-track": _write_board(home, "health-track", []),
        "observability-sentinel": _write_board(home, "observability-sentinel", []),
    }

    databases = renderer.discover_board_databases(home)

    assert {board.slug: board.database for board in databases} == expected
    assert expected["default"] == home / "kanban.db"
    assert not (home / "kanban" / "boards" / "default" / "kanban.db").exists()


def test_renders_block_kind_and_latest_reason_verbatim_for_every_board(tmp_path):
    home = tmp_path / ".hermes"
    for slug in (
        "default",
        "buzz-platform",
        "health-track",
        "observability-sentinel",
    ):
        db = _write_board(home, slug, [(f"t_{slug}", f"Title {slug}", "needs_input")])
        _append_event(db, f"t_{slug}", "blocked", {"reason": f"literal | {slug}"})

    lines = renderer.render_all_boards(home)

    assert len(lines) == 4
    for slug in (
        "default",
        "buzz-platform",
        "health-track",
        "observability-sentinel",
    ):
        assert (
            f"[{slug}] t_{slug} — `Title {slug}` | Blocktyp: needs_input | "
            f"Grund: `literal | {slug}` | Operator-Halt: nein"
        ) in lines


def test_missing_block_kind_is_explicit_and_reason_uses_repo_cap_convention(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_born", "Born blocked", None)])
    reason = "0123456789abcdef"
    _append_event(db, "t_born", "blocked", {"reason": reason})

    lines = renderer.render_all_boards(home, reason_limit=10)

    assert lines == [
        "[default] t_born — `Born blocked` | Blocktyp: fehlt | "
        "Grund: `0123456789… [truncated, 6 chars omitted]` | Operator-Halt: nein"
    ]


def test_operator_halt_matches_latest_product_event_rule(tmp_path):
    home = tmp_path / ".hermes"
    rows = [
        ("t_active", "Active", "needs_input"),
        ("t_resolved", "Resolved", "needs_input"),
        ("t_nonspawnable", "Nonspawnable", "needs_input"),
        ("t_reblocked", "Reblocked", "needs_input"),
    ]
    db = _write_board(home, "default", rows)
    _append_event(db, "t_active", "operator_escalation")
    _append_event(db, "t_resolved", "operator_escalation")
    _append_event(db, "t_resolved", "promoted_manual")
    _append_event(
        db,
        "t_nonspawnable",
        "operator_escalation",
        {"evidence": {"trigger_outcome": "nonspawnable_assignee"}},
    )
    _append_event(db, "t_reblocked", "operator_escalation")
    _append_event(db, "t_reblocked", "unblocked")
    _append_event(db, "t_reblocked", "blocked", {"reason": "unrelated later block"})

    lines = renderer.render_all_boards(home)
    by_task = {line.split(" — ", 1)[0].split()[-1]: line for line in lines}

    assert by_task["t_active"].endswith("Operator-Halt: ja")
    assert by_task["t_resolved"].endswith("Operator-Halt: nein")
    assert by_task["t_nonspawnable"].endswith("Operator-Halt: nein")
    assert by_task["t_reblocked"].endswith("Operator-Halt: nein")
    assert "Grund: `unrelated later block`" in by_task["t_reblocked"]


def test_main_returns_zero_for_empty_boards_without_modifying_them(tmp_path, capsys):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [])
    before = _sha256(db)

    exit_code = renderer.main(["--hermes-home", str(home)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""
    assert _sha256(db) == before


def test_main_returns_nonzero_when_a_board_cannot_be_read(tmp_path, capsys):
    home = tmp_path / ".hermes"
    board_dir = home / "kanban" / "boards" / "default"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(
        json.dumps({"slug": "default", "name": "Hermes Agent"}), encoding="utf-8"
    )

    exit_code = renderer.main(["--hermes-home", str(home)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "default" in captured.err
    assert "kanban.db" in captured.err


# ---------------------------------------------------------------------------
# Security: process-safe + normalize + cap-before-wrap + inline code spans
# ---------------------------------------------------------------------------

_PIET_NPUB = (
    "npub1g8hwuesqexeqw3l5x0xh9h8z8l5kq5n6z8example000000000000000000"
)
# Valid-looking bech32 npub (32-byte payload encoded) used only as bait text.
_VALID_BAIT_NPUB = (
    "npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237lmu63q0ug09x"
)


def _hostile_bait(separator: str = "\n") -> str:
    return f"@Piet{separator}nostr:{_VALID_BAIT_NPUB}{separator}tick`here"


def test_security_hostile_title_is_single_line_closed_code_span(tmp_path):
    home = tmp_path / ".hermes"
    title = _hostile_bait("\n")
    db = _write_board(home, "default", [("t_hostile_title", title, "needs_input")])
    _append_event(db, "t_hostile_title", "blocked", {"reason": "plain"})

    lines = renderer.render_all_boards(home)

    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line
    # Title is one closed inline-code span; bait markers survive only as text.
    assert " — `" in line
    assert "` | Blocktyp:" in line
    title_span = line.split(" — ", 1)[1].split(" | Blocktyp:", 1)[0]
    assert title_span.startswith("`") and title_span.endswith("`")
    assert title_span.count("`") == 2  # opening + closing only
    assert "@Piet" in title_span
    assert "nostr:" in title_span
    assert "`" not in title_span[1:-1]  # backticks neutralized inside span


def test_security_hostile_reason_is_single_line_closed_code_span(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_hostile_reason", "Safe title", "needs_input")])
    _append_event(
        db, "t_hostile_reason", "blocked", {"reason": _hostile_bait("\n")}
    )

    lines = renderer.render_all_boards(home)
    line = lines[0]
    assert "\n" not in line
    reason_span = line.split("Grund: ", 1)[1].split(" | Operator-Halt:", 1)[0]
    assert reason_span.startswith("`") and reason_span.endswith("`")
    assert reason_span.count("`") == 2
    assert "@Piet" in reason_span
    assert "nostr:" in reason_span


def test_security_hostile_reason_crlf_is_single_line_closed_code_span(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_crlf", "Safe title", "needs_input")])
    _append_event(
        db, "t_crlf", "blocked", {"reason": _hostile_bait("\r\n")}
    )

    lines = renderer.render_all_boards(home)
    line = lines[0]
    assert "\n" not in line and "\r" not in line
    reason_span = line.split("Grund: ", 1)[1].split(" | Operator-Halt:", 1)[0]
    assert reason_span.startswith("`") and reason_span.endswith("`")
    assert reason_span.count("`") == 2


def test_security_overlimit_reason_keeps_closed_span_with_bait_before_cut(tmp_path):
    """Cap-before-wrap: bait before the cut must stay inside a closed span.

    Contract values are asserted literally so a drifted constant turns the
    test red instead of rewriting the fixture with itself.
    """
    # Vertrag: Done-when 4 — Grenzwerte woertlich, nicht aus dem Code gelesen.
    assert renderer.DEFAULT_REASON_LIMIT == 500
    assert renderer.DEFAULT_TITLE_LIMIT == 256

    home = tmp_path / ".hermes"
    bait = f"@Piet nostr:{_VALID_BAIT_NPUB} "
    # Fixed total length 700 → cap at 500 leaves exactly 200 chars omitted.
    assert len(bait) < 500
    reason = bait + ("X" * (700 - len(bait)))
    assert len(reason) == 700
    db = _write_board(home, "default", [("t_over_reason", "Safe", "needs_input")])
    _append_event(db, "t_over_reason", "blocked", {"reason": reason})

    lines = renderer.render_all_boards(home)
    line = lines[0]
    reason_span = line.split("Grund: ", 1)[1].split(" | Operator-Halt:", 1)[0]
    assert reason_span.startswith("`") and reason_span.endswith("`")
    assert reason_span.count("`") == 2
    assert "@Piet" in reason_span
    assert "nostr:" in reason_span
    # Woertliche Suffix-Zahl fuer den Grund-Pfad (nicht nachgerechnet).
    assert "… [truncated, 200 chars omitted]" in reason_span
    # Closed span: wrap after cap keeps the trailing backtick.
    assert reason_span[-1] == "`"
    # Schnittkante: first 500 chars of the raw reason are the capped body.
    assert reason_span == (
        f"`{reason[:500]}… [truncated, 200 chars omitted]`"
    )


def test_security_overlimit_title_keeps_closed_span_with_bait_before_cut(tmp_path):
    """Cap-before-wrap for title; contract 256 pinned with a literal suffix."""
    assert renderer.DEFAULT_REASON_LIMIT == 500
    assert renderer.DEFAULT_TITLE_LIMIT == 256

    home = tmp_path / ".hermes"
    bait = f"@Piet nostr:{_VALID_BAIT_NPUB} "
    # Fixed total length 356 → cap at 256 leaves exactly 100 chars omitted.
    assert len(bait) < 256
    title = bait + ("Y" * (356 - len(bait)))
    assert len(title) == 356
    db = _write_board(home, "default", [("t_over_title", title, "needs_input")])
    _append_event(db, "t_over_title", "blocked", {"reason": "ok"})

    lines = renderer.render_all_boards(home)
    line = lines[0]
    title_span = line.split(" — ", 1)[1].split(" | Blocktyp:", 1)[0]
    assert title_span.startswith("`") and title_span.endswith("`")
    assert title_span.count("`") == 2
    assert "@Piet" in title_span
    assert "nostr:" in title_span
    # Woertliche Suffix-Zahl fuer den Titel-Pfad (nicht nachgerechnet).
    assert "… [truncated, 100 chars omitted]" in title_span
    assert title_span[-1] == "`"
    assert title_span == (
        f"`{title[:256]}… [truncated, 100 chars omitted]`"
    )


def test_process_safe_replaces_nul_and_lone_surrogates():
    raw = "pre\x00mid\ud800end\udfff"
    safe = renderer._process_safe(raw)
    assert "\x00" not in safe
    assert safe.encode("utf-8")  # must not raise
    assert "\ufffd" in safe
    assert safe.count("\ufffd") == 3


def test_process_safe_nul_title_and_surrogate_reason_roundtrip_in_line(tmp_path):
    home = tmp_path / ".hermes"
    title = "nul\x00title"
    # Surrogate must be injected the way JSON round-trips it into Python str.
    reason = json.loads('"reason\\u0000and\\ud800surrogate"')
    db = _write_board(home, "default", [("t_proc", title, "needs_input")])
    _append_event(db, "t_proc", "blocked", {"reason": reason})

    lines = renderer.render_all_boards(home)
    line = lines[0]
    assert "\x00" not in line
    line.encode("utf-8")  # argv-survivable
    assert "\ufffd" in line


# ---------------------------------------------------------------------------
# Darstellung: exotic separators collapse to one display line (not security)
# ---------------------------------------------------------------------------

def test_display_carriage_return_alone_collapses_to_one_line(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_cr", "Safe", "needs_input")])
    _append_event(db, "t_cr", "blocked", {"reason": "a\rb"})

    lines = renderer.render_all_boards(home)
    assert len(lines) == 1
    assert "\r" not in lines[0]
    assert "Grund: `a b`" in lines[0]


def test_display_vertical_tab_collapses_to_one_line(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_vt", "Safe", "needs_input")])
    _append_event(db, "t_vt", "blocked", {"reason": "a\x0bb"})

    lines = renderer.render_all_boards(home)
    assert "\x0b" not in lines[0]
    assert "Grund: `a b`" in lines[0]


def test_display_form_feed_collapses_to_one_line(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_ff", "Safe", "needs_input")])
    _append_event(db, "t_ff", "blocked", {"reason": "a\x0cb"})

    lines = renderer.render_all_boards(home)
    assert "\x0c" not in lines[0]
    assert "Grund: `a b`" in lines[0]


def test_display_nel_collapses_to_one_line(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_nel", "Safe", "needs_input")])
    _append_event(db, "t_nel", "blocked", {"reason": "a\u0085b"})

    lines = renderer.render_all_boards(home)
    assert "\u0085" not in lines[0]
    assert "Grund: `a b`" in lines[0]


def test_display_line_separator_u2028_collapses_to_one_line(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_ls", "Safe", "needs_input")])
    _append_event(db, "t_ls", "blocked", {"reason": "a\u2028b"})

    lines = renderer.render_all_boards(home)
    assert "\u2028" not in lines[0]
    assert "Grund: `a b`" in lines[0]


def test_display_paragraph_separator_u2029_collapses_to_one_line(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_ps", "Safe", "needs_input")])
    _append_event(db, "t_ps", "blocked", {"reason": "a\u2029b"})

    lines = renderer.render_all_boards(home)
    assert "\u2029" not in lines[0]
    assert "Grund: `a b`" in lines[0]

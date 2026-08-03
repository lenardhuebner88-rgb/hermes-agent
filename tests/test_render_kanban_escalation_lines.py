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
            f"[{slug}] t_{slug} — Title {slug} | Blocktyp: needs_input | "
            f"Grund: literal | {slug} | Operator-Halt: nein"
        ) in lines


def test_missing_block_kind_is_explicit_and_reason_uses_repo_cap_convention(tmp_path):
    home = tmp_path / ".hermes"
    db = _write_board(home, "default", [("t_born", "Born blocked", None)])
    reason = "0123456789abcdef"
    _append_event(db, "t_born", "blocked", {"reason": reason})

    lines = renderer.render_all_boards(home, reason_limit=10)

    assert lines == [
        "[default] t_born — Born blocked | Blocktyp: fehlt | "
        "Grund: 0123456789… [truncated, 6 chars omitted] | Operator-Halt: nein"
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
    assert "Grund: unrelated later block" in by_task["t_reblocked"]


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

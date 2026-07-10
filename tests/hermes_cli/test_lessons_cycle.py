"""Tests for ``hermes lessons cycle`` — one-shot harvest then promote (L4).

AC: cycle runs harvest, and only when candidates meeting the threshold were
produced, runs promote against the fresh artefact. Promoted tasks stay HELD
(blocked) — no auto-unblock. When harvest yields zero candidates, promote is
skipped entirely (idle is correct, not an error).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import lessons


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an initialised kanban DB (real-shaped rows)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture()
def loops_root(tmp_path):
    root = tmp_path / "loops"
    root.mkdir()
    return root


@pytest.fixture()
def repo_dir(tmp_path):
    """Minimal repo dir with no existing pitfall docs (so nothing dedups away)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# AGENTS\n\n## Important Pitfalls\n\n", encoding="utf-8")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "agent-dev-guide.md").write_text("# Dev Guide\n", encoding="utf-8")
    return repo


def _create_task(conn, *, title, assignee="coder"):
    return kb.create_task(conn, title=title, assignee=assignee, created_by="test")


def _insert_disposition_item(conn, *, source_task_id, evidence, next_action="", status="open"):
    item_id = kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ="follow_up",
        disposition="defer",
        next_action=next_action,
        severity="none",
        evidence=evidence,
    )
    conn.execute("UPDATE disposition_items SET status=? WHERE id=?", (status, item_id))
    conn.commit()
    return item_id


def test_cycle_harvests_and_promotes_end_to_end(kanban_home, loops_root, repo_dir):
    """A synthetic disposition/ledger input -> cycle produces a held task."""
    now = int(time.time())
    with kb.connect() as conn:
        tid1 = _create_task(conn, title="Artifact issue")
        _insert_disposition_item(
            conn,
            source_task_id=tid1,
            evidence="ARTIFACT_POLICY_MISSING — no preserve prefix",
            next_action="add preserve prefix",
            status="open",
        )
        tid2 = _create_task(conn, title="Second artifact issue")
        _insert_disposition_item(
            conn,
            source_task_id=tid2,
            evidence="artifact policy missing for screenshot output",
            next_action="fix preserve prefix",
            status="accepted",
        )

    result = lessons.run_lessons_cycle(
        loops_root=loops_root,
        window_days=30,
        now_ts=now,
        repo_dir=repo_dir,
        cap=5,
    )

    harvest = result["harvest"]
    promote = result["promote"]
    assert harvest["candidate_count"] >= 1
    assert promote is not None
    assert promote["promoted"] >= 1

    with kb.connect_closing(board=None) as conn:
        rows = conn.execute(
            "SELECT title, status FROM tasks WHERE assignee='coder' AND created_by='lessons-promote'"
        ).fetchall()
    assert rows, "expected at least one lessons-promote task"
    # Promoted tasks stay HELD (blocked) — no auto-unblock.
    assert all(status == "blocked" for _title, status in rows)


def test_cycle_harvests_from_the_same_board_promote_targets(kanban_home, loops_root, repo_dir):
    """run_lessons_cycle(board=...) must resolve harvest's kanban DB from the
    same ``board`` promote uses — not silently fall back to the default board.
    Regression: harvest ignored ``board`` while promote honoured it, so a
    non-default-board cycle harvested nothing (default board's disposition
    items) while promote correctly targeted the other board."""
    now = int(time.time())
    other_db_path = kb.kanban_db_path(board="other-board")
    kb._INITIALIZED_PATHS.discard(str(other_db_path.resolve()))
    kb.init_db(board="other-board")

    with kb.connect_closing(board="other-board") as conn:
        tid1 = _create_task(conn, title="Artifact issue on other board")
        _insert_disposition_item(
            conn,
            source_task_id=tid1,
            evidence="ARTIFACT_POLICY_MISSING — no preserve prefix",
            next_action="add preserve prefix",
            status="open",
        )
        tid2 = _create_task(conn, title="Second artifact issue on other board")
        _insert_disposition_item(
            conn,
            source_task_id=tid2,
            evidence="artifact policy missing for screenshot output",
            next_action="fix preserve prefix",
            status="accepted",
        )

    result = lessons.run_lessons_cycle(
        loops_root=loops_root,
        window_days=30,
        now_ts=now,
        repo_dir=repo_dir,
        cap=5,
        board="other-board",
    )

    harvest = result["harvest"]
    promote = result["promote"]
    assert harvest["candidate_count"] >= 1
    assert promote is not None
    assert promote["promoted"] >= 1

    with kb.connect_closing(board="other-board") as conn:
        rows = conn.execute(
            "SELECT title FROM tasks WHERE assignee='coder' AND created_by='lessons-promote'"
        ).fetchall()
    assert rows, "expected the promoted task on the SAME board harvest read from"

    with kb.connect_closing(board=None) as conn:
        default_rows = conn.execute(
            "SELECT title FROM tasks WHERE assignee='coder' AND created_by='lessons-promote'"
        ).fetchall()
    assert not default_rows, "promote must not land on the default board when board='other-board'"


def test_cycle_skips_promote_when_no_candidates(kanban_home, loops_root, repo_dir):
    """An empty harvest (no disposition/blocked/ledger signal) skips promote."""
    result = lessons.run_lessons_cycle(
        loops_root=loops_root,
        window_days=30,
        repo_dir=repo_dir,
        cap=5,
    )
    assert result["harvest"]["candidate_count"] == 0
    assert result["promote"] is None

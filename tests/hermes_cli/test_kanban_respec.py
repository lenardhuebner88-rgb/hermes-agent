"""CLI contract tests for ``hermes kanban respec``.

These tests exercise the real Kanban SQLite schema and a live-board task body
fixture carrying the Hermes Coder Contract v1 boilerplate.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _live_coder_fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "review_efficiency_live_fixtures.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))["tier_substring_overfire"]
    assert "## Hermes Coder Contract v1" in fixture["body"]
    return fixture


def _task_links(conn) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
    ).fetchall()
    return {(row["parent_id"], row["child_id"]) for row in rows}


def test_respec_parser_accepts_body_file_and_title(parser):
    args = parser.parse_args(
        ["kanban", "respec", "t_old", "--body-file", "new.md", "--title", "New"]
    )

    assert args.kanban_action == "respec"
    assert args.task_id == "t_old"
    assert args.body_file == "new.md"
    assert args.title == "New"


def test_respec_body_file_archives_old_task_and_creates_linked_replacement(
    kanban_home, tmp_path, parser, capsys
):
    fixture = _live_coder_fixture()
    new_body = (
        "# Respecced implementation task\n\n"
        "Implement the corrected scope with concrete acceptance checks.\n\n"
        "## Acceptance\n"
        "- Gate proves the atomic respec workflow."
    )
    body_file = tmp_path / "new.md"
    body_file.write_text(new_body, encoding="utf-8")

    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="Upstream decision",
            body="Parent already accepted.",
        )
        assert kb.complete_task(conn, parent_id, result="parent done")
        old_id = kb.create_task(
            conn,
            title=fixture["title"],
            body=fixture["body"],
            assignee=fixture["assignee"],
            priority=7,
            parents=[parent_id],
            kind=fixture["kind"],
            epic_id="epic-live",
        )
        child_id = kb.create_task(
            conn,
            title="Existing child must stay on old task",
            body="Do not move this v1 child link.",
            parents=[old_id],
        )
        old_before = kb.get_task(conn, old_id)
        assert old_before is not None
        assert old_before.status == "ready"

    args = parser.parse_args(
        [
            "kanban",
            "respec",
            old_id,
            "--body-file",
            str(body_file),
            "--title",
            "Respecced live card",
        ]
    )

    assert kc.kanban_command(args) == 0
    out = capsys.readouterr().out
    match = re.search(r"t_[0-9a-f]+", out)
    assert match, out
    new_id = match.group(0)
    assert new_id != old_id

    with kb.connect() as conn:
        old_after = kb.get_task(conn, old_id)
        new_task = kb.get_task(conn, new_id)
        assert old_after is not None
        assert new_task is not None
        assert old_after.status == "archived"
        assert old_after.completed_at is not None
        assert old_after.body == fixture["body"]
        assert new_task.title == "Respecced live card"
        assert new_task.body == new_body
        assert new_task.assignee == fixture["assignee"]
        assert new_task.priority == 7
        assert new_task.kind == fixture["kind"]
        assert new_task.epic_id == "epic-live"
        assert (parent_id, new_id) in _task_links(conn)
        assert (old_id, new_id) in _task_links(conn)
        assert (old_id, child_id) in _task_links(conn)
        assert (new_id, child_id) not in _task_links(conn)
        comments = kb.list_comments(conn, old_id)
    assert any(f"respecced → {new_id}" in comment.body for comment in comments)


@pytest.mark.parametrize("status", ["running", "review", "done", "archived"])
def test_respec_rejects_non_editable_statuses(
    kanban_home, tmp_path, parser, capsys, status
):
    fixture = _live_coder_fixture()
    body_file = tmp_path / "replacement.md"
    body_file.write_text("new body that must not be written", encoding="utf-8")

    with kb.connect() as conn:
        old_id = kb.create_task(
            conn,
            title=fixture["title"],
            body=fixture["body"],
            assignee=fixture["assignee"],
            kind=fixture["kind"],
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, old_id))
        conn.commit()

    args = parser.parse_args(
        ["kanban", "respec", old_id, "--body-file", str(body_file)]
    )

    assert kc.kanban_command(args) == 1
    assert "cannot respec" in capsys.readouterr().err
    with kb.connect() as conn:
        task = kb.get_task(conn, old_id)
        assert task is not None
        assert task.status == status
        assert task.body == fixture["body"]
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1

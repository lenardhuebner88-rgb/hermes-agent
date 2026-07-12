"""Regression tests for read-only dispatch-hold reporting."""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return home


def _task(conn, *, title, status, workspace_kind="scratch", workspace_path=None):
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="coder",
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = ? WHERE id = ?",
            (status, "test:1" if status == "running" else None, task_id),
        )
    return task_id


def _parse_kanban(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli.build_parser(sub)
    return parser.parse_args(["kanban", *argv])


def test_cli_holds_uses_effective_config_and_real_parser(board, capsys):
    (board / "config.yaml").write_text(
        "kanban:\n  max_in_progress_per_profile: 1\n",
        encoding="utf-8",
    )
    with kb.connect_closing() as conn:
        _task(conn, title="running coder", status="running")
        waiter = _task(conn, title="ready coder", status="ready")

    args = _parse_kanban(["holds", "--json"])
    assert args.kanban_action == "holds"
    assert cli.kanban_command(args) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["count"] == 1
    assert report["count"] == 1
    hold = report["holds"][0]
    assert hold["task_id"] == waiter
    assert hold["title"] == "ready coder"
    assert hold["assignee"] == "coder"
    assert hold["bucket"] == "per_profile_capped"
    assert hold["current"] == 1
    assert hold["cap"] == 1


def test_repo_hold_names_waiter_holder_and_is_side_effect_free(board, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    with kb.connect_closing() as conn:
        holder = _task(
            conn,
            title="holder",
            status="running",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        waiter = _task(
            conn,
            title="waiter",
            status="ready",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        before = (
            conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            conn.execute("SELECT id, status FROM tasks ORDER BY id").fetchall(),
        )
        report = kb.list_dispatch_holds(conn)
        after = (
            conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            conn.execute("SELECT id, status FROM tasks ORDER BY id").fetchall(),
        )

    assert after == before
    hold = report["holds"][0]
    assert hold["task_id"] == waiter
    assert hold["title"] == "waiter"
    assert hold["assignee"] == "coder"
    assert hold["bucket"] == "repo_serialized"
    assert hold["repo_root"] == str(repo)
    assert hold["holder"]["task_id"] == holder
    assert hold["holder"]["status"] == "running"

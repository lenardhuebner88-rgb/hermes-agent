"""Tests for dispatch-holds reporting (`hermes kanban holds`).

The report is read-only: it must surface the per-task buckets the dispatcher
already computes (repo_serialized, per_profile_capped, respawn_guarded, …)
without changing task status, task_runs, or task_events.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest


def _is_purgeable_hermes_module(name: str) -> bool:
    return (
        name.startswith("hermes_cli")
        or name.startswith("hermes_state")
        or name == "hermes_constants"
    )


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with kanban DB + a 'coder' profile."""
    test_home = tempfile.mkdtemp(prefix="kanban_holds_test_")
    os.makedirs(os.path.join(test_home, "profiles", "coder"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    saved = {
        name: mod for name, mod in sys.modules.items()
        if _is_purgeable_hermes_module(name)
    }
    for name in saved:
        del sys.modules[name]
    from hermes_cli import kanban_db
    try:
        yield kanban_db
    finally:
        for name in [n for n in sys.modules if _is_purgeable_hermes_module(n)]:
            del sys.modules[name]
        sys.modules.update(saved)
        shutil.rmtree(test_home, ignore_errors=True)


def _git_repo(tmp_path, name: str = "repo") -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "holds@test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Holds Test"],
        check=True,
    )
    return str(repo)


def test_repo_serialized_hold_includes_waiter_and_holder(isolated_kanban_home, tmp_path):
    """A ready task blocked by repo serialization reports the waiter and the holder."""
    kb = isolated_kanban_home
    repo = _git_repo(tmp_path)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        holder = kb.create_task(
            conn,
            title="holder running",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=repo,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1' WHERE id = ?",
                (holder,),
            )
        waiter = kb.create_task(
            conn,
            title="waiter ready",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=repo,
            priority=5,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                (waiter,),
            )

    with kb.connect_closing() as conn:
        before_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        before_statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM tasks").fetchall()
        }
        report = kb.list_dispatch_holds(conn)
        after_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        after_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        after_statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM tasks").fetchall()
        }

    assert after_runs == before_runs
    assert after_events == before_events
    assert after_statuses == before_statuses

    assert report["count"] == 1
    assert len(report["holds"]) == 1
    hold = report["holds"][0]
    assert hold["task_id"] == waiter
    assert hold["title"] == "waiter ready"
    assert hold["assignee"] == "coder"
    assert hold["priority"] == 5
    assert hold["bucket"] == "repo_serialized"
    assert hold["repo_root"] == repo
    assert hold["holder"]["task_id"] == holder
    assert hold["holder"]["status"] == "running"
    assert "checked_at" in report


def test_per_profile_capped_hold_is_generic_bucket(isolated_kanban_home, tmp_path):
    """A task held by the per-profile cap is reported generically (not repo-only)."""
    kb = isolated_kanban_home
    repo = _git_repo(tmp_path, "repo2")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running = kb.create_task(
            conn,
            title="running coder",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=repo,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1' WHERE id = ?",
                (running,),
            )
        ready = kb.create_task(
            conn,
            title="ready coder",
            assignee="coder",
            workspace_kind="scratch",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                (ready,),
            )

    with kb.connect_closing() as conn:
        report = kb.list_dispatch_holds(conn, max_in_progress_per_profile=1)

    assert report["count"] == 1
    hold = report["holds"][0]
    assert hold["task_id"] == ready
    assert hold["bucket"] == "per_profile_capped"
    assert hold["assignee"] == "coder"
    assert hold["current"] >= 1
    assert hold["cap"] == 1


def test_cli_holds_json(isolated_kanban_home, tmp_path, capsys):
    """`hermes kanban holds --json` prints the structured report."""
    kb = isolated_kanban_home
    from hermes_cli import kanban as kb_cli

    repo = _git_repo(tmp_path, "repo3")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        holder = kb.create_task(
            conn,
            title="holder",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=repo,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1' WHERE id = ?",
                (holder,),
            )
        waiter = kb.create_task(
            conn,
            title="waiter",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=repo,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                (waiter,),
            )

    args = SimpleNamespace(board=None, json=True)
    assert kb_cli._cmd_holds(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert data["holds"][0]["bucket"] == "repo_serialized"
    assert data["holds"][0]["title"] == "waiter"

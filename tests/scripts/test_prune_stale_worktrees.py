from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "prune-stale-worktrees.sh"


def _run(*args: str | Path, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path, task_id: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-b", "main", cwd=repo)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    (repo / "tracked.txt").write_text("base\n")
    _run("git", "add", "tracked.txt", cwd=repo)
    _run("git", "commit", "-m", "base", cwd=repo)
    worktree = repo / ".worktrees" / "kanban" / task_id
    worktree.parent.mkdir(parents=True)
    _run("git", "worktree", "add", "-b", f"kanban/{task_id}", worktree, "main", cwd=repo)
    return repo, worktree


def _write_board(db_path: Path, *, task_id: str, status: str, workspace_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, workspace_path TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks(id, status, workspace_path) VALUES (?, ?, ?)",
            (task_id, status, str(workspace_path)),
        )


def _prune(repo: Path, db_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "PRUNE_REPOS": str(repo),
            "KANBAN_DB_PATH": str(db_path),
            "MIN_AGE_HOURS": "0",
        }
    )
    return _run("bash", SCRIPT, "--apply", cwd=REPO_ROOT, env=env)


@pytest.mark.parametrize(
    "status",
    ["scheduled", "todo", "ready", "running", "review", "blocked"],
)
def test_pruner_keeps_worktree_for_nonterminal_task(tmp_path: Path, status: str) -> None:
    task_id = f"t_{status}"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    _write_board(db_path, task_id=task_id, status=status, workspace_path=worktree)

    result = _prune(repo, db_path)

    assert worktree.exists()
    assert f"kept(nonterminal task): {worktree}" in result.stdout


@pytest.mark.parametrize("status", ["done", "archived", "failed", "cancelled"])
def test_pruner_removes_terminal_clean_merged_worktree(
    tmp_path: Path, status: str,
) -> None:
    task_id = f"t_{status}"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    _write_board(db_path, task_id=task_id, status=status, workspace_path=worktree)

    result = _prune(repo, db_path)

    assert not worktree.exists()
    assert f"removed: {worktree}" in result.stdout


def test_pruner_fails_closed_when_board_cannot_be_read(tmp_path: Path) -> None:
    task_id = "t_unreadable"
    repo, worktree = _make_repo(tmp_path, task_id)

    result = _prune(repo, tmp_path / "missing.db")

    assert worktree.exists()
    assert f"kept(board unavailable): {worktree}" in result.stdout


def test_pruner_still_keeps_terminal_dirty_worktree(tmp_path: Path) -> None:
    task_id = "t_dirty"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    _write_board(db_path, task_id=task_id, status="done", workspace_path=worktree)
    (worktree / "tracked.txt").write_text("dirty\n")

    _prune(repo, db_path)

    assert worktree.exists()


def test_pruner_still_keeps_terminal_unmerged_worktree(tmp_path: Path) -> None:
    task_id = "t_unmerged"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    _write_board(db_path, task_id=task_id, status="done", workspace_path=worktree)
    (worktree / "branch.txt").write_text("not in main\n")
    _run("git", "add", "branch.txt", cwd=worktree)
    _run("git", "commit", "-m", "unmerged", cwd=worktree)

    _prune(repo, db_path)

    assert worktree.exists()


def test_pruner_still_keeps_terminal_worktree_with_session_holder(tmp_path: Path) -> None:
    task_id = "t_session"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    _write_board(db_path, task_id=task_id, status="done", workspace_path=worktree)
    holder = subprocess.Popen(
        ["bash", "-c", "exec -a codex sleep 30"],
        cwd=worktree,
    )
    try:
        result = _prune(repo, db_path)
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert worktree.exists()
    assert f"kept(session): {worktree}" in result.stdout


def test_pruner_removes_clean_merged_worktree_without_associated_task(
    tmp_path: Path,
) -> None:
    task_id = "t_absent"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, workspace_path TEXT)"
        )

    result = _prune(repo, db_path)

    assert not worktree.exists()
    assert f"removed: {worktree}" in result.stdout


def test_pruner_keeps_worktree_when_any_associated_task_is_nonterminal(
    tmp_path: Path,
) -> None:
    task_id = "t_shared"
    repo, worktree = _make_repo(tmp_path, task_id)
    db_path = tmp_path / "kanban.db"
    _write_board(db_path, task_id=task_id, status="done", workspace_path=worktree)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks(id, status, workspace_path) VALUES (?, ?, ?)",
            ("t_shared_running", "running", str(worktree)),
        )

    result = _prune(repo, db_path)

    assert worktree.exists()
    assert f"kept(nonterminal task): {worktree}" in result.stdout

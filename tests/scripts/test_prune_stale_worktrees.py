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


def _prune(
    repo: Path,
    db_path: Path,
    *,
    hermes_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "PRUNE_REPOS": str(repo),
            "KANBAN_DB_PATH": str(db_path),
            "HERMES_HOME": str(hermes_home or db_path.parent),
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


def _write_empty_board(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, workspace_path TEXT)"
        )


@pytest.mark.parametrize(
    ("named_status", "default_status"),
    [("running", None), ("blocked", "done")],
)
def test_pruner_keeps_worktree_for_nonterminal_task_on_named_board(
    tmp_path: Path,
    named_status: str,
    default_status: str | None,
) -> None:
    task_id = f"t_multiboard_{named_status}"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    default_db = hermes_home / "kanban.db"
    named_db = hermes_home / "kanban" / "boards" / "ops" / "kanban.db"
    if default_status is None:
        _write_empty_board(default_db)
    else:
        default_db.parent.mkdir(parents=True, exist_ok=True)
        _write_board(
            default_db,
            task_id=task_id,
            status=default_status,
            workspace_path=worktree,
        )
    named_db.parent.mkdir(parents=True, exist_ok=True)
    _write_board(
        named_db,
        task_id=task_id,
        status=named_status,
        workspace_path=worktree,
    )

    result = _prune(repo, default_db, hermes_home=hermes_home)

    assert worktree.exists()
    assert f"kept(nonterminal task): {worktree}" in result.stdout


def test_pruner_keeps_worktree_when_custom_db_is_terminal_but_named_board_is_running(
    tmp_path: Path,
) -> None:
    task_id = "t_custom_multiboard"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    custom_db = tmp_path / "custom" / "board.db"
    custom_db.parent.mkdir(parents=True)
    _write_board(custom_db, task_id=task_id, status="done", workspace_path=worktree)
    named_db = hermes_home / "kanban" / "boards" / "ops" / "kanban.db"
    named_db.parent.mkdir(parents=True)
    _write_board(named_db, task_id=task_id, status="running", workspace_path=worktree)

    result = _prune(repo, custom_db, hermes_home=hermes_home)

    assert worktree.exists()
    assert f"kept(nonterminal task): {worktree}" in result.stdout


def test_pruner_removes_worktree_when_all_board_associations_are_terminal(
    tmp_path: Path,
) -> None:
    task_id = "t_multiboard_terminal"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    default_db = hermes_home / "kanban.db"
    default_db.parent.mkdir(parents=True)
    _write_board(default_db, task_id=task_id, status="done", workspace_path=worktree)
    named_db = hermes_home / "kanban" / "boards" / "ops" / "kanban.db"
    named_db.parent.mkdir(parents=True)
    _write_board(named_db, task_id=task_id, status="archived", workspace_path=worktree)

    result = _prune(repo, default_db, hermes_home=hermes_home)

    assert not worktree.exists()
    assert f"removed: {worktree}" in result.stdout


def test_pruner_scans_shared_root_when_hermes_home_is_a_profile(
    tmp_path: Path,
) -> None:
    task_id = "t_profile_multiboard"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_root = tmp_path / "hermes"
    profile_home = hermes_root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    default_db = hermes_root / "kanban.db"
    _write_empty_board(default_db)
    named_db = hermes_root / "kanban" / "boards" / "ops" / "kanban.db"
    named_db.parent.mkdir(parents=True)
    _write_board(named_db, task_id=task_id, status="review", workspace_path=worktree)

    result = _prune(repo, default_db, hermes_home=profile_home)

    assert worktree.exists()
    assert f"kept(nonterminal task): {worktree}" in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses EACCES permission checks")
def test_pruner_fails_closed_when_named_board_directory_is_unreadable(
    tmp_path: Path,
) -> None:
    task_id = "t_multiboard_eacces"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    selected_db = tmp_path / "selected" / "kanban.db"
    selected_db.parent.mkdir(parents=True)
    _write_board(selected_db, task_id=task_id, status="done", workspace_path=worktree)
    named_db = hermes_home / "kanban" / "boards" / "ops" / "kanban.db"
    named_db.parent.mkdir(parents=True)
    _write_board(named_db, task_id=task_id, status="running", workspace_path=worktree)
    named_db.parent.chmod(0)

    try:
        result = _prune(repo, selected_db, hermes_home=hermes_home)
    finally:
        named_db.parent.chmod(0o700)

    assert worktree.exists()
    assert f"kept(board unavailable): {worktree}" in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses EACCES permission checks")
def test_pruner_fails_closed_when_default_board_path_is_unreadable(
    tmp_path: Path,
) -> None:
    task_id = "t_default_eacces"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    default_db = hermes_home / "kanban.db"
    _write_board(default_db, task_id=task_id, status="running", workspace_path=worktree)
    selected_db = tmp_path / "selected" / "kanban.db"
    selected_db.parent.mkdir(parents=True)
    _write_board(selected_db, task_id=task_id, status="done", workspace_path=worktree)
    hermes_home.chmod(0)

    try:
        result = _prune(repo, selected_db, hermes_home=hermes_home)
    finally:
        hermes_home.chmod(0o700)

    assert worktree.exists()
    assert f"kept(board unavailable): {worktree}" in result.stdout


def test_pruner_skips_truly_missing_optional_named_board_db(tmp_path: Path) -> None:
    task_id = "t_multiboard_missing_optional"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    default_db = hermes_home / "kanban.db"
    default_db.parent.mkdir(parents=True)
    _write_board(default_db, task_id=task_id, status="done", workspace_path=worktree)
    (hermes_home / "kanban" / "boards" / "empty").mkdir(parents=True)

    result = _prune(repo, default_db, hermes_home=hermes_home)

    assert not worktree.exists()
    assert f"removed: {worktree}" in result.stdout


def test_pruner_fails_closed_when_named_board_schema_is_unavailable(
    tmp_path: Path,
) -> None:
    task_id = "t_multiboard_broken"
    repo, worktree = _make_repo(tmp_path, task_id)
    hermes_home = tmp_path / "hermes"
    default_db = hermes_home / "kanban.db"
    default_db.parent.mkdir(parents=True)
    _write_board(default_db, task_id=task_id, status="done", workspace_path=worktree)
    broken_db = hermes_home / "kanban" / "boards" / "broken" / "kanban.db"
    broken_db.parent.mkdir(parents=True)
    with sqlite3.connect(broken_db):
        pass

    result = _prune(repo, default_db, hermes_home=hermes_home)

    assert worktree.exists()
    assert f"kept(board unavailable): {worktree}" in result.stdout

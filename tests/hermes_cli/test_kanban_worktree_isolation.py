"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_dependency_links_use_identity_bound_external_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_parent = tmp_path / "identity-bound"
    repo_parent.mkdir()
    repo = _make_repo(repo_parent)
    worktree = tmp_path / "fresh-worktree"
    deps_root = tmp_path / "worktree-deps"
    (repo / ".gitignore").write_text("node_modules\n.venv\n", encoding="utf-8")
    (repo / "web").mkdir()
    (repo / "web" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", ".gitignore", "web/.keep")
    _git(repo, "commit", "-m", "ignore dependency trees")
    worktree = _add_worktree(repo, worktree, "identity-bound")
    (repo / "node_modules").mkdir()
    (repo / "web" / "node_modules").mkdir()
    (repo / ".venv").mkdir()
    monkeypatch.setenv("HERMES_WORKTREE_DEPS_ROOT", str(deps_root))

    kwt._link_shared_dependencies(repo, worktree)

    deps_tree = kwt._worktree_deps_tree(worktree)
    assert deps_tree.parent == deps_root.resolve()
    assert (worktree / "node_modules").is_symlink()
    assert (worktree / "node_modules").resolve() == deps_tree / "node_modules"
    assert (worktree / "web" / "node_modules").is_symlink()
    assert (
        worktree / "web" / "node_modules"
    ).resolve() == deps_tree / "web" / "node_modules"
    assert (worktree / "node_modules").resolve() != (repo / "node_modules").resolve()
    assert (worktree / "web" / "node_modules").resolve() != (
        repo / "web" / "node_modules"
    ).resolve()
    assert (worktree / ".venv").resolve() == (repo / ".venv").resolve()


def test_dependency_links_require_git_ignored_paths(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    deps_root = tmp_path / "worktree-deps"
    monkeypatch.setenv("HERMES_WORKTREE_DEPS_ROOT", str(deps_root))

    ignored_parent = tmp_path / "ignored"
    ignored_parent.mkdir()
    ignored_repo = _make_repo(ignored_parent)
    (ignored_repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (ignored_repo / "web").mkdir()
    (ignored_repo / "web" / ".keep").write_text("", encoding="utf-8")
    _git(ignored_repo, "add", ".gitignore", "web/.keep")
    _git(ignored_repo, "commit", "-m", "ignore node dependencies")
    ignored_worktree = _add_worktree(
        ignored_repo,
        ignored_parent / "worktree",
        "ignored-dependencies",
    )

    kwt._link_shared_dependencies(ignored_repo, ignored_worktree)

    assert (ignored_worktree / "node_modules").is_symlink()
    assert (ignored_worktree / "web" / "node_modules").is_symlink()

    visible_parent = tmp_path / "visible"
    visible_parent.mkdir()
    visible_repo = _make_repo(visible_parent)
    (visible_repo / "web").mkdir()
    (visible_repo / "web" / ".keep").write_text("", encoding="utf-8")
    _git(visible_repo, "add", "web/.keep")
    _git(visible_repo, "commit", "-m", "add web workspace")
    visible_worktree = _add_worktree(
        visible_repo,
        visible_parent / "worktree",
        "visible-dependencies",
    )
    (visible_repo / ".venv").mkdir()

    with caplog.at_level(logging.WARNING, logger=kwt.__name__):
        kwt._link_shared_dependencies(visible_repo, visible_worktree)

    assert not (visible_worktree / "node_modules").exists()
    assert not (visible_worktree / "node_modules").is_symlink()
    assert not (visible_worktree / "web" / "node_modules").exists()
    assert not (visible_worktree / "web" / "node_modules").is_symlink()
    assert not (visible_worktree / ".venv").exists()
    assert not (visible_worktree / ".venv").is_symlink()
    status = subprocess.run(
        ["git", "-C", str(visible_worktree), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
    assert any(
        "node_modules" in record.message
        and str(visible_repo) in record.message
        and ".gitignore" in record.message
        for record in caplog.records
    )


def test_remove_worktree_deletes_dedicated_tree_without_following_foreign_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (repo / "web").mkdir()
    (repo / "web" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", ".gitignore", "web/.keep")
    _git(repo, "commit", "-m", "ignore node dependencies")
    worktree = _add_worktree(
        repo,
        repo / ".worktrees" / "kanban" / "t_remove_deps",
        "kanban/t_remove_deps",
    )
    deps_root = tmp_path / "worktree-deps"
    monkeypatch.setenv("HERMES_WORKTREE_DEPS_ROOT", str(deps_root))
    (repo / "node_modules").mkdir()
    (repo / "web" / "node_modules").mkdir()
    kwt._link_shared_dependencies(repo, worktree)
    deps_tree = kwt._worktree_deps_tree(worktree)
    (deps_tree / "node_modules").mkdir(parents=True)
    deps_marker = deps_tree / "node_modules" / "REMOVE"
    deps_marker.write_text("dedicated\n")

    foreign_target = tmp_path / "foreign-checkout" / "node_modules"
    foreign_target.mkdir(parents=True)
    foreign_marker = foreign_target / "DO_NOT_TOUCH"
    foreign_marker.write_text("foreign\n")
    root_link = worktree / "node_modules"
    root_link.unlink()
    root_link.symlink_to(foreign_target, target_is_directory=True)

    kwt.remove_worktree(repo, worktree, "kanban/t_remove_deps")

    assert not deps_tree.exists()
    assert foreign_marker.read_text() == "foreign\n"


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None


def test_decompose_dir_children_still_inherit_path(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="ops sweep", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', "
            "workspace_path='/srv/ops' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "child", "assignee": "alice", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (child_ids[0],),
        ).fetchone()
        assert row["workspace_kind"] == "dir"
        assert row["workspace_path"] == "/srv/ops"


def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


def test_resolve_worktree_same_branch_still_reuses(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="returning task",
            workspace_kind="worktree",
        )
        own = _add_worktree(repo, repo / ".worktrees" / tid, f"wt/{tid}")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == own.resolve()
    assert branch == f"wt/{tid}"


def test_resolve_worktree_own_path_on_foreign_branch_keeps_legacy_reuse(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="foreign-branch checkout",
            workspace_kind="worktree",
        )
        own = _add_worktree(repo, repo / ".worktrees" / tid, "wt/foreign")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    # The fallback target would be the occupied path itself, so the
    # legacy reuse applies rather than failing dispatch.
    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == own.resolve()
    assert branch == "wt/foreign"

"""Conflict-fixer children inherit the declared scope of their parent slice."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt
from tests.hermes_cli._kanban_test_helpers import _commit_in, _events, _git


_PARK_REASON = "integration parked: merge conflict/failure (aborted): conflict.py"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    assert db_path.resolve() != Path("/home/piet/.hermes/kanban.db").resolve()
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixer-scope@test.invalid")
    _git(repo, "config", "user.name", "fixer scope test")
    for path in (
        "hermes_cli/allowed.py",
        "hermes_cli/parent_owned.py",
        "tests/unrelated/test_timeout.py",
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("BASE = True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _materialize_parent(conn, repo: Path, parent_id: str) -> tuple[object, Path]:
    claimed = kb.claim_task(conn, parent_id)
    assert claimed is not None
    materialized = kwt.materialize_dispatch_workspace(
        conn,
        claimed,
        mode=kwt.MANAGED_WORKTREE_PROVISION,
        board="default",
        resolve_existing=lambda task, *, board=None: (
            Path(task.workspace_path),
            task.branch_name,
        ),
        resolve_managed_base=lambda task, *, board=None: repo,
    )
    provisioned = kwt.split_provisioned_path(materialized.path)
    assert provisioned is not None
    return claimed, provisioned[2]


def _create_real_fixer(
    conn,
    repo: Path,
    *,
    allowed_paths: list[str] | None,
    parent_extra_path: str | None = None,
) -> tuple[str, str, Path, int]:
    create_kwargs = {}
    if allowed_paths is not None:
        create_kwargs["scope_contract"] = {"allowed_paths": allowed_paths}
    parent_id = kb.create_task(
        conn,
        title="parked scoped parent",
        assignee="coder",
        workspace_kind="worktree",
        workspace_path=str(repo),
        **create_kwargs,
    )
    _claimed_parent, worktree = _materialize_parent(conn, repo, parent_id)
    _commit_in(
        worktree,
        "hermes_cli/allowed.py",
        "BASE = True\nPARENT = True\n",
        msg=f"kanban({parent_id}): parent slice",
    )
    if parent_extra_path is not None:
        _commit_in(
            worktree,
            parent_extra_path,
            "BASE = True\nPARENT_OWNED = True\n",
            msg=f"kanban({parent_id}): parent-owned path",
        )
    assert kb.block_task(conn, parent_id, reason=_PARK_REASON, kind="integration")

    parent_row = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (parent_id,)
    ).fetchone()
    summary = {"parked": [], "conflict_fixer_dispatched": []}
    kb._maybe_route_conflict_park_fixer(
        conn,
        parent_row,
        reason=_PARK_REASON,
        retry_count=0,
        now=int(time.time()),
        summary=summary,
    )
    dispatched = summary["conflict_fixer_dispatched"]
    assert len(dispatched) == 1
    child_id = dispatched[0]["child_id"]
    assert _events(conn, child_id, "conflict_fixer_for")

    child = kb.claim_task(conn, child_id)
    assert child is not None
    run_id = child.current_run_id
    assert run_id is not None
    kwt.prepare_reused_task_worktree(conn, child, worktree)
    run = conn.execute(
        "SELECT workspace_materialized FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert run is not None and run["workspace_materialized"] == 1
    return parent_id, child_id, worktree, int(run_id)


def _complete_fixer(conn, child_id: str, run_id: int) -> bool:
    return kb.complete_task(
        conn,
        child_id,
        summary="conflict fixed",
        expected_run_id=run_id,
    )


def test_conflict_fixer_outside_parent_scope_is_parked(repo, kanban_home):
    with kb.connect() as conn:
        parent_id, child_id, worktree, run_id = _create_real_fixer(
            conn, repo, allowed_paths=["hermes_cli/allowed.py"]
        )
        _commit_in(
            worktree,
            "tests/unrelated/test_timeout.py",
            "BASE = True\nTIMEOUT = 45\n",
            msg=f"kanban({child_id}): out-of-scope workaround",
        )

        assert _complete_fixer(conn, child_id, run_id)
        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        assert child is not None
        assert child.status == "blocked", (
            "out-of-scope conflict fixer completed instead of being parked"
        )
        assert parent is not None and parent.status == "blocked"
        blocked = _events(conn, child_id, kwt.LANE_SCOPE_BLOCKED_EVENT)
        assert len(blocked) == 1
        assert blocked[0]["class"] == "fixer_scope"
        assert blocked[0]["parent_id"] == parent_id
        assert blocked[0]["allowed_paths"] == ["hermes_cli/allowed.py"]
        assert blocked[0]["violating_paths"] == [
            "tests/unrelated/test_timeout.py"
        ]

    assert _git(repo, "show", "main:tests/unrelated/test_timeout.py") == (
        "BASE = True"
    )


def test_conflict_fixer_inside_parent_scope_completes(repo, kanban_home):
    with kb.connect() as conn:
        parent_id, child_id, worktree, run_id = _create_real_fixer(
            conn, repo, allowed_paths=["hermes_cli/allowed.py"]
        )
        _commit_in(
            worktree,
            "hermes_cli/allowed.py",
            "BASE = True\nPARENT = True\nFIXER = True\n",
            msg=f"kanban({child_id}): in-scope fix",
        )

        assert _complete_fixer(conn, child_id, run_id)
        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        assert child is not None and child.status == "done"
        assert parent is not None and parent.status == "ready"
        assert _events(conn, child_id, kwt.LANE_SCOPE_BLOCKED_EVENT) == []


def test_conflict_fixer_allows_paths_already_touched_by_parent(repo, kanban_home):
    with kb.connect() as conn:
        parent_id, child_id, worktree, run_id = _create_real_fixer(
            conn,
            repo,
            allowed_paths=["hermes_cli/allowed.py"],
            parent_extra_path="hermes_cli/parent_owned.py",
        )
        _commit_in(
            worktree,
            "hermes_cli/parent_owned.py",
            "BASE = True\nPARENT_OWNED = True\nFIXER = True\n",
            msg=f"kanban({child_id}): repair parent-owned path",
        )

        assert _complete_fixer(conn, child_id, run_id)
        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        assert child is not None and child.status == "done"
        assert parent is not None and parent.status == "ready"
        assert _events(conn, child_id, kwt.LANE_SCOPE_BLOCKED_EVENT) == []


def test_conflict_fixer_allows_preservable_artifacts(repo, kanban_home):
    with kb.connect() as conn:
        parent_id, child_id, worktree, run_id = _create_real_fixer(
            conn, repo, allowed_paths=["hermes_cli/allowed.py"]
        )
        _commit_in(
            worktree,
            "artifacts/fixer.log",
            "conflict reproduction evidence\n",
            msg=f"kanban({child_id}): conflict evidence",
        )

        assert _complete_fixer(conn, child_id, run_id)
        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        assert child is not None and child.status == "done"
        assert parent is not None and parent.status == "ready"
        assert _events(conn, child_id, kwt.LANE_SCOPE_BLOCKED_EVENT) == []


def test_conflict_fixer_without_parent_scope_fails_open(repo, kanban_home):
    with kb.connect() as conn:
        parent_id, child_id, worktree, run_id = _create_real_fixer(
            conn, repo, allowed_paths=None
        )
        _commit_in(
            worktree,
            "tests/unrelated/test_timeout.py",
            "BASE = True\nTIMEOUT = 45\n",
            msg=f"kanban({child_id}): unscoped fix",
        )

        assert _complete_fixer(conn, child_id, run_id)
        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        assert child is not None and child.status == "done"
        assert parent is not None and parent.status == "ready"
        assert _events(conn, child_id, kwt.LANE_SCOPE_BLOCKED_EVENT) == []

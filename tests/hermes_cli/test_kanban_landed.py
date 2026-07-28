"""Truth tests for the fork-owned landed-state surface (``kanban_landed``).

Every test here drives a task through a real transition in a throwaway git
repo + throwaway board and asserts that the landed query tells the truth —
including when the truth is uncomfortable (work merged and then reverted,
work that never landed, work re-applied under a different patch).

The headline case, ``test_revert_flips_state_and_names_the_revert``, is the
red-without-fix proof: before ``hermes_cli/kanban_landed.py`` existed, this
file failed collection with ``ModuleNotFoundError`` (quoted in the design
doc); the board alone could not answer "is the merged work still on main?"
at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_landed as kl


# ---------------------------------------------------------------------------
# throwaway git repo helpers (never touch a real checkout)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True):
    proc = subprocess.run(
        ["git", *[str(a) for a in args]],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed rc={proc.returncode}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc


def _commit(repo: Path, message: str) -> str:
    _git(
        repo,
        "-c", "user.name=test",
        "-c", "user.email=test@example.com",
        "commit", "-q", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    init = _git(repo, "init", "-q", "-b", "main", check=False)
    if init.returncode != 0:  # pre-2.28 git fallback
        _git(repo, "init", "-q")
        _git(repo, "checkout", "-q", "-b", "main", check=False)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _commit(repo, "base commit")
    return repo


def _feature_branch(repo: Path, branch: str = "work", fname: str = "f.txt") -> str:
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / fname).write_text("work content\n", encoding="utf-8")
    _git(repo, "add", ".")
    sha = _commit(repo, "feature change")
    _git(repo, "checkout", "-q", "main")
    return sha


def _merge_no_ff(repo: Path, branch: str = "work") -> str:
    _git(repo, "merge", "--no-ff", "-m", f"merge {branch}", branch)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# board helpers (isolated board from the shared kanban_home fixture)
# ---------------------------------------------------------------------------

@pytest.fixture
def board(kanban_home):
    conn = kb.connect()
    yield conn
    conn.close()


def _done_task(conn, *, branch: str | None, title: str = "slice") -> str:
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="testrole",
        created_by="test",
        workspace_kind="worktree" if branch else "scratch",
        branch_name=branch,
    )
    conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = 1750000000 "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    return task_id


def _witness_merged(conn, task_id: str, merge_commit: str, target: str = "main"):
    kb._append_event(
        conn,
        task_id,
        "integration_merged",
        {"merge_commit": merge_commit, "target": target, "branch": "work"},
    )
    kb._append_event(
        conn,
        task_id,
        "INTEGRATOR_VERIFIED",
        {"merge_commit": merge_commit, "state": "green"},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# the truth cases
# ---------------------------------------------------------------------------

def test_merged_content_active_is_landed_active(tmp_path, board):
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    task_id = _done_task(board, branch="work")
    _witness_merged(board, task_id, merge)

    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.LANDED_ACTIVE
    assert state.merge_commit == merge
    assert state.detail.get("evidence") == "content_active"


def test_revert_flips_state_and_names_the_revert(tmp_path, board):
    """Headline case: merged, then reverted on main.

    The ancestor relation still holds after ``revert -m 1`` (the branch
    stays an ancestor of main), so a naive ``rev-list --count main..branch``
    watcher reports "landed".  The landed query must say "drifted" and name
    the revert.
    """
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    _git(
        repo,
        "-c", "user.name=test",
        "-c", "user.email=test@example.com",
        "revert", "-m", "1", "--no-edit", merge,
    )
    # sanity: the naive ancestor check would still claim "landed"
    assert _git(repo, "rev-list", "--count", f"main..work").stdout.strip() == "0"

    task_id = _done_task(board, branch="work")
    _witness_merged(board, task_id, merge)
    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.LANDED_DRIFTED
    assert state.detail["explicit_reverted"] is True
    assert state.detail["revert_commits"]
    assert state.detail["drifted_files"] == ["f.txt"]


def test_later_evolution_is_drifted_without_revert_evidence(tmp_path, board):
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    # a LATER commit edits the same file — evolution, not a revert
    (repo / "f.txt").write_text("built upon\n", encoding="utf-8")
    _git(repo, "add", ".")
    _commit(repo, "evolve the same file")

    task_id = _done_task(board, branch="work")
    _witness_merged(board, task_id, merge)
    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.LANDED_DRIFTED
    assert state.detail["explicit_reverted"] is False
    assert state.detail["drifted_files"] == ["f.txt"]


def test_unmerged_ahead_branch_is_not_landed(tmp_path, board):
    repo = _make_repo(tmp_path)
    feature = _feature_branch(repo)  # never merged

    task_id = _done_task(board, branch="work")
    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.NOT_LANDED
    assert feature in state.detail["unlanded_commits"]


def test_patch_equivalent_reapply_is_landed(tmp_path, board):
    """Manual re-apply ("pfadgenau nachziehen"): branch ahead=1, yet the
    identical patch is already on main under a different commit.
    ``git cherry`` marks it ``-`` → landed by patch evidence."""
    repo = _make_repo(tmp_path)
    feature = _feature_branch(repo)
    _git(repo, "cherry-pick", "-x", feature)  # same patch, new commit on main

    task_id = _done_task(board, branch="work")
    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.LANDED_ACTIVE
    assert state.detail.get("evidence") == "patch_equivalent"


def test_commitless_done_is_na(tmp_path, board):
    repo = _make_repo(tmp_path)
    task_id = _done_task(board, branch=None)
    state = kl.classify_task(board, repo, task_id)
    assert state.state == kl.NA_COMMITLESS


def test_branch_gone_without_witness_is_unknown(tmp_path, board):
    repo = _make_repo(tmp_path)
    task_id = _done_task(board, branch="kanban/t_never_existed")
    state = kl.classify_task(board, repo, task_id)
    assert state.state == kl.UNKNOWN


def test_witness_merge_commit_dropped_from_history(tmp_path, board):
    """The recorded merge sha no longer reaches the target (history
    rewrite) → drifted with an honest evidence marker, not a crash."""
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    _git(repo, "reset", "-q", "--hard", "main~1")  # drop the merge entirely

    task_id = _done_task(board, branch=None)
    _witness_merged(board, task_id, merge)
    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.LANDED_DRIFTED
    assert state.detail.get("evidence") == "merge_commit_not_ancestor"


def test_park_event_after_merge_marks_explicit_revert(tmp_path, board):
    """The park-cannot-demote-done trap: gate reverted the merge and wrote
    ``integration_parked`` after the merge witness, but the task reads done.
    The landed query must not call this active."""
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    _git(
        repo,
        "-c", "user.name=test",
        "-c", "user.email=test@example.com",
        "revert", "-m", "1", "--no-edit", merge,
    )

    task_id = _done_task(board, branch="work")
    _witness_merged(board, task_id, merge)
    kb._append_event(
        board,
        task_id,
        "integration_parked",
        {"merge_commit": merge, "reason": "post-merge gate red"},
    )
    board.commit()
    state = kl.classify_task(board, repo, task_id)

    assert state.state == kl.LANDED_DRIFTED
    assert state.detail["explicit_reverted"] is True


# ---------------------------------------------------------------------------
# migration / materialization contract
# ---------------------------------------------------------------------------

def test_ensure_schema_is_idempotent(board):
    kl.ensure_schema(board)
    kl.ensure_schema(board)  # second run must not fail
    tables = {
        row[0]
        for row in board.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "task_landed_state" in tables


def test_materialize_roundtrip_is_plain_sql(tmp_path, board):
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    good = _done_task(board, branch="work", title="landed slice")
    _witness_merged(board, good, merge)
    stranded = _done_task(board, branch="work", title="stranded slice")
    _git(repo, "checkout", "-q", "-b", "stranded")
    (repo / "g.txt").write_text("stranded\n", encoding="utf-8")
    _git(repo, "add", ".")
    _commit(repo, "stranded work")
    _git(repo, "checkout", "-q", "main")
    board.execute(
        "UPDATE tasks SET branch_name = 'stranded' WHERE id = ?", (stranded,)
    )
    board.commit()

    results = kl.materialize(board, repo, [good, stranded])
    assert {r.task_id: r.state for r in results} == {
        good: kl.LANDED_ACTIVE,
        stranded: kl.NOT_LANDED,
    }

    # the consumer query needs no JSON parsing
    rows = board.execute(
        "SELECT task_id, state FROM task_landed_state "
        "WHERE state IN (?, ?) ORDER BY task_id",
        (kl.LANDED_DRIFTED, kl.NOT_LANDED),
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(stranded, kl.NOT_LANDED)]

    detail = board.execute(
        "SELECT detail FROM task_landed_state WHERE task_id = ?", (stranded,)
    ).fetchone()[0]
    assert json.loads(detail)["evidence"] == "commits_not_patch_equivalent"


def test_sweep_reclassifies_after_revert(tmp_path, board):
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    task_id = _done_task(board, branch="work")
    _witness_merged(board, task_id, merge)

    first = kl.sweep(board, repo)
    assert {task_id: row["new"] for row in first if row["task_id"] == task_id} == {
        task_id: kl.LANDED_ACTIVE
    }

    _git(
        repo,
        "-c", "user.name=test",
        "-c", "user.email=test@example.com",
        "revert", "-m", "1", "--no-edit", merge,
    )
    second = kl.sweep(board, repo)
    transitions = {
        row["task_id"]: (row["old"], row["new"])
        for row in second
        if row["task_id"] == task_id
    }
    assert transitions[task_id] == (kl.LANDED_ACTIVE, kl.LANDED_DRIFTED)


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_is_ancestor_true_for_base_commit(tmp_path):
    """Kill boolean_flip L160: == 0 -> != 0 in _is_ancestor."""
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _feature_branch(repo)
    merge = _merge_no_ff(repo)
    # base is an ancestor of the merge commit
    assert kl._is_ancestor(repo, base, merge) is True
    # the merge commit is NOT an ancestor of base
    assert kl._is_ancestor(repo, merge, base) is False


def test_ahead_count_returns_int(tmp_path):
    """Kill boolean_flip L165: is not None -> is None in _ahead_count."""
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    _merge_no_ff(repo)
    count = kl._ahead_count(repo, "main", "work")
    assert isinstance(count, int)
    assert count >= 0


def test_witness_info_preserves_target_and_park_reason(board):
    """Kill bool_op_swap L285 and L297: or None -> and None."""
    task_id = _done_task(board, branch="work")
    kb._append_event(
        board,
        task_id,
        "integration_merged",
        {"merge_commit": "abc123", "target": "main"},
    )
    kb._append_event(
        board,
        task_id,
        "integration_parked",
        {"merge_commit": "def456", "reason": "gate red"},
    )
    board.commit()

    info = kl._witness_info(board, task_id)
    assert info["positive_target"] == "main"
    assert info["park_reason"] == "gate red"
    assert info["park_commit"] == "def456"

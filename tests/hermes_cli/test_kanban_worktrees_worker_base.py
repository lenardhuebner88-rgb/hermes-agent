"""Kanban worktrees tests: WIP adoption on resumable re-dispatch (S8).

Covers the guard extension in ``prepare_worker_base``/
``prepare_reused_task_worktree`` that adopts uncommitted worker edits as a
commit instead of parking the chain, when (and only when) the caller has
verified the dirt belongs to a resumable (``blocked``) prior run of the
SAME task.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt

from tests.hermes_cli._kanban_test_helpers import _git


@pytest.fixture
def repo(tmp_path):
    """Real git repo on branch ``main`` with one base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "a.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


# ---------------------------------------------------------------------------
# prepare_worker_base: adopt_wip_run_id gates the new adoption path.
# ---------------------------------------------------------------------------


def test_prepare_worker_base_adopts_wip_when_evidence_given(repo):
    """(a) dirty non-artifact worktree + adoption evidence -> the dirt is
    committed on the chain branch under a deterministic message, the
    worktree ends up clean, and the result reports the adopted files."""
    info = kwt.ensure_worktree(repo, "t_adopt")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "src.py").write_text("partial implementation\n")

    result = kwt.prepare_worker_base(
        worktree,
        recorded_head=recorded_head,
        merge_target="main",
        task_id="t_adopt",
        adopt_wip_run_id=42,
    )

    assert kwt.dirty_files(worktree) == []
    assert result["adopted_wip_files"] == ["src.py"]
    new_head = _git(worktree, "rev-parse", "HEAD")
    assert new_head != recorded_head
    assert result["head"] == new_head
    commit_msg = _git(worktree, "log", "-1", "--format=%s")
    assert commit_msg == "wip(t_adopt): adopt uncommitted WIP from blocked run 42"
    author = _git(worktree, "log", "-1", "--format=%an <%ae>")
    assert author == "Hermes Worker Base <worker-base@hermes.local>"
    # The adopted commit must land on the chain branch, not just the
    # worktree's detached index -- a plain `log` on the branch shows it.
    on_branch = _git(worktree, "log", "kanban/t_adopt", "-1", "--format=%H")
    assert on_branch == new_head


def test_prepare_worker_base_without_evidence_still_parks(repo):
    """(b) dirty non-artifact worktree + NO adoption evidence (fresh task /
    prior run was not blocked) -> raises exactly as before adoption existed."""
    info = kwt.ensure_worktree(repo, "t_no_evidence")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "src.py").write_text("partial implementation\n")

    with pytest.raises(kwt.WorktreeError, match="dirty before worker edits"):
        kwt.prepare_worker_base(
            worktree,
            recorded_head=recorded_head,
            merge_target="main",
            task_id="t_no_evidence",
            adopt_wip_run_id=None,
        )

    assert (worktree / "src.py").read_text() == "partial implementation\n"


def test_prepare_worker_base_adoption_evidence_does_not_bypass_head_guard(repo):
    """(c) adoption evidence present but the recorded HEAD does not match the
    actual HEAD -> the HEAD guard fires first, exactly as before; adoption
    evidence never widens that check."""
    info = kwt.ensure_worktree(repo, "t_head_mismatch")
    worktree = info["path"]
    (worktree / "src.py").write_text("partial implementation\n")

    with pytest.raises(kwt.WorktreeError, match="recorded pre-run HEAD"):
        kwt.prepare_worker_base(
            worktree,
            recorded_head="0" * 40,
            merge_target="main",
            task_id="t_head_mismatch",
            adopt_wip_run_id=7,
        )

    # Nothing was committed -- the worktree is untouched.
    assert (worktree / "src.py").read_text() == "partial implementation\n"
    assert kwt.dirty_files(worktree) == ["src.py"]


def test_prepare_worker_base_artifact_preservation_wins_over_adoption(repo, tmp_path):
    """(d) artifact-only dirt with adoption evidence present -> the existing
    artifact-preservation path still wins; no source commit is created
    because there is no non-artifact dirt left to adopt."""
    info = kwt.ensure_worktree(repo, "t_artifact_only")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "screenshots").mkdir()
    (worktree / "screenshots" / "shot1.png").write_text("fake-png-bytes")

    receipts_root = tmp_path / "receipts" / "artifacts"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", receipts_root)
    try:
        result = kwt.prepare_worker_base(
            worktree,
            recorded_head=recorded_head,
            merge_target="main",
            task_id="t_artifact_only",
            adopt_wip_run_id=99,
        )
    finally:
        monkeypatch.undo()

    assert result["action"] == "current"
    assert "adopted_wip_files" not in result
    assert not (worktree / "screenshots").exists()
    assert any(receipts_root.iterdir())
    # No adoption commit: HEAD did not move.
    assert _git(worktree, "rev-parse", "HEAD") == recorded_head


# ---------------------------------------------------------------------------
# prepare_reused_task_worktree (caller): computes adoption evidence from the
# task's own run history via real sqlite, end-to-end through dispatch_once.
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


def test_dispatch_adopts_wip_left_by_own_blocked_run(
    kanban_home, tmp_path, monkeypatch, all_assignees_spawnable,
):
    """End-to-end reproduction of the live incident (t_bfb52c79): a worker
    run ends ``blocked`` leaving real source edits uncommitted in the shared
    chain worktree. Re-dispatch must adopt that WIP and spawn -- not raise
    ``worker_base_rejected`` and give up."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="fleet drawer slice",
            assignee="sentinel",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        expected = repo / ".worktrees" / "kanban" / tid
        assert first.spawned == [(tid, "sentinel", str(expected))]

        run1_id = kb.get_task(conn, tid).current_run_id
        assert run1_id is not None

        # The worker leaves a real, uncommitted source edit and blocks
        # (needs_input) instead of completing.
        (expected / "NodeDetailDrawer.tsx").write_text("14/15 tests green\n")
        assert kb.block_task(conn, tid, reason="operator: which chip wins?")

        run1 = conn.execute(
            "SELECT outcome, status FROM task_runs WHERE id = ?", (run1_id,)
        ).fetchone()
        assert run1["outcome"] == "blocked"

        assert kb.unblock_task(conn, tid)

        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (tid,),
        ).fetchall()

    assert second.spawned == [(tid, "sentinel", str(expected))]
    assert not any(e["kind"] == "worker_base_rejected" for e in events)
    assert kwt.dirty_files(expected) == []
    adopted = [
        json.loads(e["payload"]) for e in events if e["kind"] == "wip_adopted"
    ]
    assert len(adopted) == 1
    assert adopted[0]["run_id"] == run1_id
    assert adopted[0]["files"] == ["NodeDetailDrawer.tsx"]
    commit_msg = _git(expected, "log", "-1", "--format=%s")
    assert commit_msg == f"wip({tid}): adopt uncommitted WIP from blocked run {run1_id}"


def test_dispatch_still_rejects_dirt_without_blocked_predecessor(
    kanban_home, tmp_path, monkeypatch, all_assignees_spawnable,
):
    """Gegenprobe: dirt left after a NON-blocked prior run (e.g. crashed) is
    still refused -- adoption never fires without resumable evidence."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="crash-then-retry",
            assignee="sentinel",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        expected = repo / ".worktrees" / "kanban" / tid
        assert first.spawned == [(tid, "sentinel", str(expected))]
        run1_id = kb.get_task(conn, tid).current_run_id

        # Simulate a crash: end the run as 'crashed' (not 'blocked'), then
        # put the task back into a dispatchable state directly, mirroring
        # what the crash-recovery path leaves behind for the dispatcher.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status='crashed', outcome='crashed', "
                "ended_at=strftime('%s','now') WHERE id=?",
                (run1_id,),
            )
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL, "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (tid,),
            )
        # A crashed predecessor's uncommitted leftovers land in the shared
        # worktree -- garbage from an unrelated run, not this run's own WIP.
        (expected / "leftover.py").write_text("half-written garbage\n")

        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (tid,),
        ).fetchall()

    assert second.spawned == []
    reject_events = [e for e in events if e["kind"] == "worker_base_rejected"]
    assert len(reject_events) == 1
    assert not any(e["kind"] == "wip_adopted" for e in events)
    assert (expected / "leftover.py").read_text() == "half-written garbage\n"

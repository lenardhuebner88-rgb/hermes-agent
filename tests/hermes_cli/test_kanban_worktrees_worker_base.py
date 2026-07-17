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
    """End-to-end reproduction of the ACTUAL live incident (t_bfb52c79),
    verified read-only against ``kanban.db``: run 7301 ``blocked`` (worker
    left real source edits uncommitted), run 7302 ``spawn_failed``, run 7303
    ``gave_up`` -- TWO re-dispatch attempts failed the dirty-worktree guard
    before the breaker tripped, neither of which ever touched the tree.
    The next re-dispatch must skip past the spawn_failed/gave_up rows,
    find the original ``blocked`` run as evidence, adopt that WIP, and
    spawn -- not raise ``worker_base_rejected`` and give up again."""
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

        # Reproduce the two dispatcher-side worktree-prep rejections that
        # produced runs 7302 (spawn_failed) and 7303 (gave_up) in the real
        # incident: no worker process is ever spawned, the tree is never
        # touched -- only the dispatcher's own failure/breaker bookkeeping
        # (real code, real task_runs/task_events writes) runs.
        real_prepare_worker_base = kwt.prepare_worker_base

        def _still_dirty(*_args, **_kwargs):
            raise kwt.WorktreeError(
                "worktree is dirty before worker edits; refusing automatic "
                "base update (simulated concurrent prep failure)"
            )

        monkeypatch.setattr(kwt, "prepare_worker_base", _still_dirty)
        spawn_failed_attempt = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        assert spawn_failed_attempt.spawned == []
        gave_up_attempt = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        assert gave_up_attempt.spawned == []
        monkeypatch.setattr(kwt, "prepare_worker_base", real_prepare_worker_base)

        run_rows = conn.execute(
            "SELECT id, outcome FROM task_runs WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        outcomes = [(r["id"], r["outcome"]) for r in run_rows]
        assert [o for _, o in outcomes] == ["blocked", "spawn_failed", "gave_up"]
        assert outcomes[0][0] == run1_id

        # The two simulated dispatcher-side rejections above each emit their
        # own worker_base_rejected event, exactly like the real incident's
        # two failed re-dispatch attempts. Watermark here so the assertions
        # below only judge the THIRD (real) dispatch, not the setup.
        watermark = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?",
            (tid,),
        ).fetchone()[0]

        # The breaker tripped the task to 'blocked' on the gave_up run;
        # unblock once more before the real re-dispatch that must adopt.
        assert kb.unblock_task(conn, tid)

        third = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND id > ? "
            "ORDER BY id",
            (tid, watermark),
        ).fetchall()

    assert third.spawned == [(tid, "sentinel", str(expected))]
    assert not any(e["kind"] == "worker_base_rejected" for e in events)
    assert kwt.dirty_files(expected) == []
    adopted = [
        json.loads(e["payload"]) for e in events if e["kind"] == "wip_adopted"
    ]
    assert len(adopted) == 1
    # Adoption is attributed to the ORIGINAL blocked run (7301-equivalent),
    # not either of the intervening spawn_failed/gave_up rows.
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


def test_dispatch_rejects_when_walk_lands_on_crashed_behind_failed_spawns(
    kanban_home, tmp_path, monkeypatch, all_assignees_spawnable,
):
    """Gegenprobe: the skip-list only skips spawn_failed/gave_up. A crashed
    run BEHIND those rows is still not resumable evidence -- the walk must
    land on it (not fall through to None by accident) and still refuse."""
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
            title="crash-then-two-failed-spawns",
            assignee="sentinel",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        expected = repo / ".worktrees" / "kanban" / tid
        assert first.spawned == [(tid, "sentinel", str(expected))]
        run1_id = kb.get_task(conn, tid).current_run_id

        # run1 crashes (not blocked) -- dispatchable again, no worker touched
        # the tree since.
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
        # worktree.
        (expected / "leftover.py").write_text("half-written garbage\n")

        real_prepare_worker_base = kwt.prepare_worker_base

        def _still_dirty(*_args, **_kwargs):
            raise kwt.WorktreeError(
                "worktree is dirty before worker edits; refusing automatic "
                "base update (simulated concurrent prep failure)"
            )

        monkeypatch.setattr(kwt, "prepare_worker_base", _still_dirty)
        assert kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default").spawned == []
        assert kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default").spawned == []
        monkeypatch.setattr(kwt, "prepare_worker_base", real_prepare_worker_base)

        run_rows = conn.execute(
            "SELECT id, outcome FROM task_runs WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        assert [r["outcome"] for r in run_rows] == ["crashed", "spawn_failed", "gave_up"]

        watermark = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?",
            (tid,),
        ).fetchone()[0]

        assert kb.unblock_task(conn, tid)

        third = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="default")
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND id > ? "
            "ORDER BY id",
            (tid, watermark),
        ).fetchall()

    assert third.spawned == []
    reject_events = [e for e in events if e["kind"] == "worker_base_rejected"]
    assert len(reject_events) == 1
    assert not any(e["kind"] == "wip_adopted" for e in events)
    assert (expected / "leftover.py").read_text() == "half-written garbage\n"

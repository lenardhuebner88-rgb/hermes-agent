"""Kanban worktrees tests: provision.

Split from test_kanban_worktrees.py (pure move; no test logic changes).
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt

from tests.hermes_cli._kanban_test_helpers import (
    _git,
    _commit_in,
    _events,
)

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    # Kanban workers inherit dispatcher pins for the live board. Tests must
    # explicitly clear them before resolving kanban_db_path(), otherwise a
    # worker-run pytest can write fixture tasks into /home/piet/.hermes/kanban.db.
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    live_db = Path("/home/piet/.hermes/kanban.db").resolve()
    assert db_path.resolve() != live_db
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path):
    """Real git repo on branch ``main`` with one base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "a.txt").write_text("base\n")
    (r / "web").mkdir()
    (r / "web" / "index.txt").write_text("web\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


@pytest.fixture
def fo_repo(tmp_path, monkeypatch):
    """A stand-in Family-Organizer checkout (git repo on ``main``) with
    ``kwt.FO_REPO_PATH`` pointed at it, so the tenant-pinning paths resolve
    here instead of the real /home/piet checkout."""
    r = tmp_path / "family-organizer"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "a.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    monkeypatch.setattr(kwt, "FO_REPO_PATH", r)
    return r


# ---------------------------------------------------------------------------
# Phase 1 — provisioning
# ---------------------------------------------------------------------------

def test_is_provisioned_path():
    assert kwt.is_provisioned_path("/x/repo/.worktrees/kanban/t_abc")
    # Subdirectory INSIDE a provisioned worktree counts too.
    assert kwt.is_provisioned_path("/x/repo/.worktrees/kanban/t_abc/web/src")
    assert not kwt.is_provisioned_path("/x/repo")
    assert not kwt.is_provisioned_path("/x/.worktrees/other/t_abc")
    assert not kwt.is_provisioned_path(None)
    assert not kwt.is_provisioned_path("")


def test_dispatch_workspace_facade_separates_existing_and_managed_modes(
    tmp_path, monkeypatch
):
    task = SimpleNamespace(id="t_modes")
    existing = tmp_path / "existing"
    managed_base = tmp_path / "repo"
    provisioned = tmp_path / "managed"
    calls: list[tuple[str, Path]] = []

    def resolve_existing(_task, *, board=None):
        calls.append((f"existing:{board}", existing))
        return existing, "wt/t_modes"

    def resolve_managed_base(_task, *, board=None):
        calls.append((f"base:{board}", managed_base))
        return managed_base

    monkeypatch.setattr(
        kwt,
        "provision_for_task",
        lambda _conn, _task, base, *, board=None: (
            calls.append((f"provision:{board}", Path(base))) or provisioned
        ),
    )

    resolved = kwt.materialize_dispatch_workspace(
        object(),
        task,
        mode=kwt.RESOLVE_EXISTING_WORKSPACE,
        board="alpha",
        resolve_existing=resolve_existing,
        resolve_managed_base=resolve_managed_base,
    )
    assert resolved.path == existing
    assert resolved.branch_name == "wt/t_modes"
    assert resolved.mode == kwt.RESOLVE_EXISTING_WORKSPACE
    assert calls == [("existing:alpha", existing)]

    calls.clear()
    managed = kwt.materialize_dispatch_workspace(
        object(),
        task,
        mode=kwt.MANAGED_WORKTREE_PROVISION,
        board="beta",
        resolve_existing=resolve_existing,
        resolve_managed_base=resolve_managed_base,
    )
    assert managed.path == provisioned
    assert managed.branch_name is None
    assert managed.mode == kwt.MANAGED_WORKTREE_PROVISION
    assert calls == [
        ("base:beta", managed_base),
        ("provision:beta", managed_base),
    ]


def test_dispatcher_routes_managed_provision_through_workspace_facade():
    source = inspect.getsource(kb._dispatch_once_locked)

    assert "_resolve_dispatch_workspace" in source
    assert "provision_for_task" not in source


def test_split_provisioned_path():
    repo_root, root_id, wt = kwt.split_provisioned_path(
        "/x/repo/.worktrees/kanban/t_abc/web/src"
    )
    assert repo_root == Path("/x/repo")
    assert root_id == "t_abc"
    assert wt == Path("/x/repo/.worktrees/kanban/t_abc")
    assert kwt.split_provisioned_path("/x/repo/src") is None


def test_ensure_worktree_creates_branch_from_current(repo):
    info = kwt.ensure_worktree(repo, "t_root1")
    wt = info["path"]
    assert info["created"] is True
    assert info["base_branch"] == "main"
    assert (wt / ".git").exists()
    assert _git(wt, "symbolic-ref", "--short", "HEAD") == "kanban/t_root1"
    # Same base commit as main.
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")


def test_ensure_worktree_is_idempotent(repo):
    first = kwt.ensure_worktree(repo, "t_root1")
    second = kwt.ensure_worktree(repo, "t_root1")
    assert second["created"] is False
    assert second["path"] == first["path"]


def test_prepare_worker_base_rebases_clean_stale_worktree(repo):
    info = kwt.ensure_worktree(repo, "t_fresh")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    _commit_in(repo, "main-only.txt", "new base\n", "advance main")

    result = kwt.prepare_worker_base(
        worktree,
        recorded_head=recorded_head,
        merge_target="main",
    )

    assert result["action"] == "rebased"
    assert result["previous_head"] == recorded_head
    assert result["head"] == _git(repo, "rev-parse", "main")
    assert kwt.dirty_files(worktree) == []


def test_prepare_worker_base_rejects_head_drift_before_rebase(repo):
    info = kwt.ensure_worktree(repo, "t_drift")
    worktree = info["path"]

    with pytest.raises(kwt.WorktreeError, match="recorded pre-run HEAD"):
        kwt.prepare_worker_base(
            worktree,
            recorded_head="0" * 40,
            merge_target="main",
        )


def test_prepare_worker_base_rejects_dirty_stale_worktree(repo):
    info = kwt.ensure_worktree(repo, "t_dirty_stale")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    _commit_in(repo, "main-only.txt", "new base\n", "advance main")
    (worktree / "a.txt").write_text("uncommitted worker edit\n")

    with pytest.raises(kwt.WorktreeError, match="dirty before worker edits"):
        kwt.prepare_worker_base(
            worktree,
            recorded_head=recorded_head,
            merge_target="main",
        )


def test_prepare_worker_base_rejects_dirty_current_worktree(repo):
    info = kwt.ensure_worktree(repo, "t_dirty_current")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "a.txt").write_text("uncommitted worker edit\n")

    with pytest.raises(kwt.WorktreeError, match="dirty before worker edits"):
        kwt.prepare_worker_base(
            worktree,
            recorded_head=recorded_head,
            merge_target="main",
        )


# ---------------------------------------------------------------------------
# Regression — artifact-only dirty reused worktree: preserve-and-remove before
# the dirty gate so artefakt-only-Schmutz die Kette nicht parkt.
# ---------------------------------------------------------------------------

def test_prepare_worker_base_preserves_artifact_only_dirt_and_proceeds(repo, tmp_path):
    """Artifact-only dirt (screenshots/) is preserved to receipts and removed
    from the worktree; the base update proceeds instead of parking."""
    info = kwt.ensure_worktree(repo, "t_reused_artifact_only")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    # Plant artifact-only dirt matching _PRESERVABLE_ARTIFACT_PREFIXES.
    (worktree / "screenshots").mkdir()
    (worktree / "screenshots" / "shot1.png").write_text("fake-png-bytes")

    # Redirect receipts root to a tmp dir so we don't write into the Vault.
    receipts_root = tmp_path / "receipts" / "artifacts"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", receipts_root)

    result = kwt.prepare_worker_base(
        worktree,
        recorded_head=recorded_head,
        merge_target="main",
        task_id="t_reused_artifact_only",
    )
    monkeypatch.undo()

    assert result["action"] == "current"
    # Artifact removed from worktree and preserved under receipts root.
    assert not (worktree / "screenshots").exists()
    assert any(receipts_root.iterdir()), "receipts root must hold the preserved artifact"


def test_prepare_worker_base_wraps_artifact_preserve_failure(repo, tmp_path, monkeypatch):
    info = kwt.ensure_worktree(repo, "t_reused_artifact_failure")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    artifact = worktree / "screenshots" / "shot1.png"
    artifact.parent.mkdir()
    artifact.write_text("fake-png-bytes")
    monkeypatch.setattr(
        kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts" / "artifacts"
    )
    monkeypatch.setattr(
        kwt.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(
        kwt.WorktreeError,
        match="artifact preserve failed before base update.*disk full",
    ) as exc_info:
        kwt.prepare_worker_base(
            worktree,
            recorded_head=recorded_head,
            merge_target="main",
            task_id="t_reused_artifact_failure",
        )

    assert artifact.read_text() == "fake-png-bytes"
    assert isinstance(exc_info.value.__cause__, OSError)


def test_prepare_worker_base_still_parks_on_real_source_edit(repo, tmp_path):
    """Real uncommitted source edits must still park and escalate — no silent
    discarding of load-bearing work."""
    info = kwt.ensure_worktree(repo, "t_reused_source_edit")
    worktree = info["path"]
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    # Plant artifact dirt AND a real source edit.
    (worktree / "screenshots").mkdir()
    (worktree / "screenshots" / "shot1.png").write_text("fake-png-bytes")
    (worktree / "a.txt").write_text("uncommitted worker edit\n")

    receipts_root = tmp_path / "receipts" / "artifacts"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", receipts_root)

    with pytest.raises(kwt.WorktreeError, match="dirty before worker edits"):
        kwt.prepare_worker_base(
            worktree,
            recorded_head=recorded_head,
            merge_target="main",
            task_id="t_reused_source_edit",
        )
    monkeypatch.undo()

    # Source edit must still be present (not silently discarded).
    assert (worktree / "a.txt").read_text() == "uncommitted worker edit\n"


def test_ensure_worktree_symlinks_node_modules(repo):
    (repo / "node_modules").mkdir()
    (repo / "web" / "node_modules").mkdir()
    info = kwt.ensure_worktree(repo, "t_nm")
    wt = info["path"]
    assert (wt / "node_modules").is_symlink()
    assert (wt / "web" / "node_modules").is_symlink()
    # The symlinks must never show up as dirty (they are filtered).
    assert kwt.dirty_files(wt) == []


# ---------------------------------------------------------------------------
# AC5 — .venv symlink: provision plants it, teardown removes only the link
# ---------------------------------------------------------------------------

def test_ensure_worktree_symlinks_venv(repo):
    """Provisioning creates a .venv symlink pointing at repo_root/.venv."""
    # Plant a fake .venv with a sentinel file — simulates a real venv safely.
    fake_venv = repo / ".venv"
    fake_venv.mkdir()
    sentinel = fake_venv / "pyvenv.cfg"
    sentinel.write_text("home = /usr/bin\n")

    info = kwt.ensure_worktree(repo, "t_venv")
    wt = info["path"]

    link = wt / ".venv"
    assert link.is_symlink(), ".venv inside worktree must be a symlink"
    # Symlink target must resolve to the repo's .venv, not a copy.
    assert link.resolve() == fake_venv.resolve()
    # Sentinel reachable through the symlink.
    assert (link / "pyvenv.cfg").read_text() == "home = /usr/bin\n"
    # Must NOT appear as a dirty path (already covered by _IGNORED_DIRTY_PATHS).
    assert kwt.dirty_files(wt) == []


def test_remove_worktree_unlinks_venv_symlink_leaves_real_venv(repo):
    """remove_worktree unlinks the .venv symlink but never deletes the real venv."""
    # Plant fake .venv with a sentinel — this is the "real" venv we must not lose.
    fake_venv = repo / ".venv"
    fake_venv.mkdir()
    sentinel = fake_venv / "pyvenv.cfg"
    sentinel.write_text("home = /usr/bin\n")

    info = kwt.ensure_worktree(repo, "t_venv_rm")
    wt = info["path"]
    branch = _git(wt, "symbolic-ref", "--short", "HEAD")

    # Pre-condition: symlink is there.
    assert (wt / ".venv").is_symlink()

    kwt.remove_worktree(repo, wt, branch)

    # Post-condition: worktree directory is gone (or link is gone).
    # Either the whole wt dir was removed, or at minimum the symlink is gone.
    if wt.exists():
        assert not (wt / ".venv").exists(), \
            ".venv symlink must be removed even if worktree dir survives"

    # CRITICAL: the real .venv must be entirely intact.
    assert fake_venv.is_dir(), "real .venv dir must survive remove_worktree"
    assert sentinel.exists(), "sentinel file inside real .venv must survive"
    assert sentinel.read_text() == "home = /usr/bin\n"


def test_remove_worktree_keeps_unmerged_branch_ref(repo):
    """Reaping a clean worktree must not force-delete unmerged work."""
    info = kwt.ensure_worktree(repo, "t_unmerged_reap")
    wt = info["path"]
    branch = info["branch"]
    _commit_in(wt, "feature.txt", "unmerged\n", msg="unmerged work")
    unmerged_head = _git(repo, "rev-parse", branch)
    assert unmerged_head != _git(repo, "rev-parse", "main")

    kwt.remove_worktree(repo, wt, branch)

    assert not wt.exists()
    assert _git(repo, "rev-parse", branch) == unmerged_head


def test_provision_for_task_repo_dir(kanban_home, repo):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="repo task", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.claim_task(conn, tid)
        ws = kwt.provision_for_task(conn, task, str(repo))
        assert kwt.is_provisioned_path(ws)
        assert ws.name == tid  # single task = its own chain root
        # Path persisted on the task row, branch stamped.
        row = conn.execute(
            "SELECT workspace_path, branch_name FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["workspace_path"] == str(ws)
        assert row["branch_name"] == f"kanban/{tid}"
        # Merge target frozen at claim (Entscheidung 3).
        events = _events(conn, tid, "worktree_provisioned")
        assert len(events) == 1
        assert events[0]["merge_target"] == "main"
        assert kwt.frozen_merge_target(conn, tid) == "main"


def test_provision_chain_child_lands_in_root_worktree(kanban_home, repo):
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="root", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        child = kb.create_task(
            conn, title="child", assignee="coder", parents=[root],
            workspace_kind="dir", workspace_path=str(repo),
        )
        rtask = kb.claim_task(conn, root)
        root_ws = kwt.provision_for_task(conn, rtask, str(repo))
        ctask = kb.get_task(conn, child)
        child_ws = kwt.provision_for_task(conn, ctask, str(repo))
        assert child_ws == root_ws
        # Only ONE provisioning event, on the chain root.
        assert len(_events(conn, root, "worktree_provisioned")) == 1
        assert _events(conn, child, "worktree_provisioned") == []


def test_provision_non_repo_dir_unchanged(kanban_home, tmp_path):
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="plain", assignee="coder",
            workspace_kind="dir", workspace_path=str(plain),
        )
        task = kb.claim_task(conn, tid)
        ws = kwt.provision_for_task(conn, task, str(plain))
    assert ws == plain  # untouched: not a git repo


def test_provision_scratch_unchanged(kanban_home, tmp_path):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="scratch", assignee="coder")
        task = kb.claim_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        ws = kwt.provision_for_task(conn, task, resolved)
    assert ws == resolved
    assert not kwt.is_provisioned_path(ws)


def test_provision_scratch_code_role_backstops_to_default_workdir(kanban_home, repo):
    kb.write_board_metadata(None, default_workdir=str(repo))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dashboard code task", assignee="coder")
        task = kb.claim_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        ws = kwt.provision_for_task(conn, task, resolved)
        assert kwt.is_provisioned_path(ws)
        assert task.workspace_kind == "dir"
        row = conn.execute(
            "SELECT workspace_kind, workspace_path, branch_name FROM tasks "
            "WHERE id = ?", (tid,)
        ).fetchone()
        assert row["workspace_kind"] == "dir"
        assert row["workspace_path"] == str(ws)
        assert row["branch_name"] == f"kanban/{tid}"
        assert len(_events(conn, tid, "worktree_provisioned")) == 1


def test_provision_scratch_non_code_role_stays_scratch(kanban_home, repo):
    kb.write_board_metadata(None, default_workdir=str(repo))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="research task", assignee="research")
        task = kb.claim_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        ws = kwt.provision_for_task(conn, task, resolved)
    assert ws == Path(resolved)
    assert not kwt.is_provisioned_path(ws)


def test_provision_scratch_non_repo_default_workdir_stays_scratch(
    kanban_home, tmp_path
):
    plain = tmp_path / "plain-workdir"
    plain.mkdir()
    kb.write_board_metadata(None, default_workdir=str(plain))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="code task", assignee="coder")
        task = kb.claim_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        ws = kwt.provision_for_task(conn, task, resolved)
    assert ws == Path(resolved)
    assert not kwt.is_provisioned_path(ws)


def test_provision_scratch_unassigned_stays_scratch(kanban_home, repo):
    kb.write_board_metadata(None, default_workdir=str(repo))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unassigned capture")
        task = kb.claim_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        ws = kwt.provision_for_task(conn, task, resolved)
    assert ws == Path(resolved)
    assert not kwt.is_provisioned_path(ws)


def test_fo_tenant_code_task_provisions_in_fo_repo(kanban_home, repo, fo_repo):
    """An FO-backlog commission (tenant=family-organizer, coder, no explicit
    workspace) is born pinned to the FO checkout and provisions its worktree
    there — never the board default_workdir (the Hermes repo). Regression guard
    for t_8fbe701d (2026-06-14)."""
    kb.write_board_metadata(None, default_workdir=str(repo))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="[FO] task", assignee="coder", tenant="family-organizer",
        )
        # Born-correct: create_task pinned the FO repo, not the board default.
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["workspace_kind"] == "dir"
        assert row["workspace_path"] == str(fo_repo)
        task = kb.claim_task(conn, tid)
        resolved = kb.resolve_workspace(task)
        ws = kwt.provision_for_task(conn, task, resolved)
        assert kwt.is_provisioned_path(ws)
        assert str(ws).startswith(str(fo_repo))
        assert str(repo) not in str(ws)


def test_scratch_code_redirect_fo_tenant_pins_fo_repo(kanban_home, repo, fo_repo):
    """Defense-in-depth: a scratch FO code task (e.g. created before the
    create_task pin, or by another caller) redirects to the FO repo, not the
    board default_workdir."""
    kb.write_board_metadata(None, default_workdir=str(repo))
    task = SimpleNamespace(
        id="t_fo", assignee="coder", tenant="family-organizer",
        workspace_kind="scratch",
    )
    assert kwt.scratch_code_redirect(task, None) == fo_repo


def test_scratch_code_redirect_non_fo_uses_default_workdir(kanban_home, repo, fo_repo):
    """A non-FO code task still backstops to the board default_workdir
    (unchanged behavior)."""
    kb.write_board_metadata(None, default_workdir=str(repo))
    task = SimpleNamespace(
        id="t_x", assignee="coder", tenant=None, workspace_kind="scratch",
    )
    assert kwt.scratch_code_redirect(task, None) == repo


def test_scratch_code_redirect_fo_missing_checkout_stays_scratch(
    kanban_home, repo, monkeypatch
):
    """FO checkout absent → stay scratch (return None); never fall back to the
    board default_workdir (the Hermes repo) — that fallback is the bug."""
    monkeypatch.setattr(kwt, "FO_REPO_PATH", Path("/nonexistent/family-organizer"))
    kb.write_board_metadata(None, default_workdir=str(repo))
    task = SimpleNamespace(
        id="t_fo2", assignee="coder", tenant="family-organizer",
        workspace_kind="scratch",
    )
    assert kwt.scratch_code_redirect(task, None) is None


def test_fo_integration_gate_self_heals_missing_node_modules(tmp_path, monkeypatch):
    """When the live FO checkout has no `next` bin, the gate runs `npm ci`
    FIRST, then builds — instead of failing on exit 127 and reverting approved
    work. Regression guard for t_8fbe701d (2026-06-14)."""
    repo = tmp_path / "fo"
    repo.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "ci":
            nb = repo / "node_modules" / ".bin"
            nb.mkdir(parents=True, exist_ok=True)
            (nb / "next").write_text("#!/bin/sh\n")  # simulate install
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    ok, detail = kwt.fo_integration_gate(repo, [])
    assert ok is True
    assert [c[1] for c in calls] == ["ci", "run"]  # npm ci THEN npm run build
    assert "self-healed" in detail


def test_fo_integration_gate_skips_npm_ci_when_next_present(tmp_path, monkeypatch):
    """When `next` is already installed, the gate builds directly — no npm ci
    overhead in the common case."""
    repo = tmp_path / "fo"
    (repo / "node_modules" / ".bin").mkdir(parents=True)
    (repo / "node_modules" / ".bin" / "next").write_text("#!/bin/sh\n")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    ok, detail = kwt.fo_integration_gate(repo, [])
    assert ok is True
    assert [c[1] for c in calls] == ["run"]  # only npm run build
    assert "self-healed" not in detail


def test_fo_integration_gate_npm_ci_failure_fails_clearly(tmp_path, monkeypatch):
    """If the self-heal npm ci itself fails, the gate fails with a clear
    message and does NOT fall through to a doomed exit-127 build."""
    repo = tmp_path / "fo"
    repo.mkdir()

    def fake_run(argv, **kwargs):
        if argv[1] == "ci":
            return SimpleNamespace(returncode=1, stdout="", stderr="lockfile mismatch")
        raise AssertionError("build must not run after npm ci failed")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    ok, detail = kwt.fo_integration_gate(repo, [])
    assert ok is False
    assert "npm ci (self-heal" in detail


def test_integration_gate_config_runs_commands_in_validation_worktree(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    validation_worktree = tmp_path / "validation"
    repo.mkdir()
    validation_worktree.mkdir()
    calls = []
    monkeypatch.setattr(
        kwt,
        "_integration_gate_config",
        lambda: {"repos": {str(repo.resolve()): ["npm test", "npm run lint"]}, "timeout": 321},
    )
    monkeypatch.setattr(
        kwt,
        "_is_fo_repo",
        lambda _repo: (_ for _ in ()).throw(AssertionError("heuristic must not run")),
    )

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    gate = kwt._integration_gate_for_repo(repo)
    ok, detail = gate(validation_worktree, ["changed.py"])

    assert ok is True
    assert detail == "npm test ok; npm run lint ok"
    assert [call[0] for call in calls] == [
        ["npm", "test"],
        ["npm", "run", "lint"],
    ]
    assert all(call[1]["cwd"] == str(validation_worktree) for call in calls)
    assert all(call[1]["timeout"] == 321 for call in calls)


def test_integration_gate_without_config_keeps_existing_fo_heuristic(
    tmp_path, monkeypatch
):
    fo_repo = tmp_path / "family-organizer"
    fo_repo.mkdir()
    (fo_repo / "package.json").write_text(
        '{"scripts": {"build": "next build --turbo"}}', encoding="utf-8"
    )
    hermes_repo = tmp_path / "hermes"
    hermes_repo.mkdir()
    monkeypatch.setattr(
        kwt, "_integration_gate_config", lambda: {"repos": {}, "timeout": 900}
    )

    assert kwt._integration_gate_for_repo(fo_repo) is kwt.fo_integration_gate
    assert kwt._integration_gate_for_repo(hermes_repo) is kwt.default_quick_gate


def test_integration_gate_config_reads_root_repo_map(kanban_home, repo):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  integration_gate:\n"
        "    timeout: 123\n"
        "    repos:\n"
        f"      {repo}:\n"
        "        - npm test\n",
        encoding="utf-8",
    )

    config = kwt._integration_gate_config()

    assert config == {
        "repos": {str(repo.resolve()): ["npm test"]},
        "timeout": 123,
    }


def test_provision_recreates_vanished_worktree(kanban_home, repo):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="repo task", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.claim_task(conn, tid)
        ws = kwt.provision_for_task(conn, task, str(repo))
        # Simulate a removed worktree (e.g. cleaned after an earlier merge).
        _git(repo, "worktree", "remove", "--force", str(ws))
        assert not ws.exists()
        task = kb.get_task(conn, tid)
        ws2 = kwt.provision_for_task(conn, task, str(ws))
        assert ws2 == ws
        assert (ws2 / ".git").exists()


def test_dispatch_once_provisions_when_flag_on(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_WORKER_ISOLATION", "worktree")
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="repo task", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
    assert tid in spawned
    assert kwt.is_provisioned_path(spawned[tid])
    # The live checkout itself stays clean (worktrees are filtered noise).
    assert kwt.dirty_files(repo) == []


def test_dispatch_once_updates_clean_stale_worktree_before_worker_spawn(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_WORKER_ISOLATION", "worktree")
    spawned_heads: list[str] = []

    def fake_spawn(_task, workspace):
        spawned_heads.append(_git(workspace, "rev-parse", "HEAD"))

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="repo task",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert kb.reclaim_task(conn, tid, reason="prepare retry")
        _commit_in(repo, "main-only.txt", "new base\n", "advance main")

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        prepared = _events(conn, tid, "worker_base_prepared")

    assert [task_id for task_id, _profile, _workspace in result.spawned] == [tid]
    assert spawned_heads[-1] == _git(repo, "rev-parse", "main")
    assert task.status == "running"
    assert prepared[-1]["action"] == "rebased"


def test_dispatch_once_blocks_dirty_worktree_before_worker_spawn(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_WORKER_ISOLATION", "worktree")
    spawned: list[str] = []

    def fake_spawn(_task, workspace):
        spawned.append(workspace)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="repo task",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert kb.reclaim_task(conn, tid, reason="prepare dirty retry")
        task = kb.get_task(conn, tid)
        (Path(task.workspace_path) / "a.txt").write_text("dirty retry\n")

        first_result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        first_task = kb.get_task(conn, tid)
        first_rejected = _events(conn, tid, "worker_base_rejected")

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        rejected = _events(conn, tid, "worker_base_rejected")

    assert len(spawned) == 1, "dirty retry must not reach the worker spawn"
    assert first_task.status == "ready"
    assert first_task.consecutive_failures == 1
    assert first_result.auto_blocked == []
    assert len(first_rejected) == 1
    assert task.status == "blocked"
    assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT
    assert result.auto_blocked == [tid]
    assert len(rejected) == kb.DEFAULT_FAILURE_LIMIT
    assert "dirty before worker edits" in rejected[-1]["reason"]


def test_dispatch_once_parks_artifact_preserve_failure(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_WORKER_ISOLATION", "worktree")
    monkeypatch.setattr(
        kwt,
        "_preserve_artifact_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    spawned: list[str] = []

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="repo task",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        kb.dispatch_once(conn, spawn_fn=lambda _task, workspace: spawned.append(workspace))
        assert kb.reclaim_task(conn, tid, reason="prepare artifact retry")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.workspace_path is not None
        artifact = Path(task.workspace_path) / "screenshots" / "shot1.png"
        artifact.parent.mkdir()
        artifact.write_text("fake-png-bytes")

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, workspace: spawned.append(workspace),
            failure_limit=1,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        rejected = _events(conn, tid, "worker_base_rejected")

    assert len(spawned) == 1, "preserve failure must not reach the worker spawn"
    assert result.spawned == []
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert result.auto_blocked == [tid]
    assert len(rejected) == 1
    assert "artifact preserve failed before base update" in rejected[0]["reason"]
    assert artifact.read_text() == "fake-png-bytes"


def test_dispatch_once_flag_off_is_unchanged(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="repo task", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
    assert spawned[tid] == str(repo)  # today's behavior, byte-identical
    assert not (repo / ".worktrees").exists()


def test_scheduled_tasks_do_not_hold_repo_serialization_lock(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    """Parked backlog cards must not block a newly-ready task in the same repo."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        parked = kb.create_task(
            conn, title="parked", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        assert kb.schedule_task(conn, parked, reason="park for later")
        ready = kb.create_task(
            conn, title="ready", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, serialize_by_repo=True)

    assert ready in spawned
    assert spawned[ready] == str(repo)
    assert res.skipped_repo_serialized == []
    assert parked not in spawned


def test_blocked_tasks_do_not_hold_repo_serialization_lock(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    with kb.connect() as conn:
        blocked = kb.create_task(
            conn, title="blocked", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        assert kb.block_task(conn, blocked, reason="waiting for operator")
        ready = kb.create_task(
            conn, title="ready", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, workspace: spawned.setdefault(task.id, workspace),
            serialize_by_repo=True,
        )

    assert ready in spawned
    assert blocked not in spawned
    assert res.skipped_repo_serialized == []


def test_running_and_review_tasks_still_hold_repo_serialization_lock(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)

    for inflight_status in ("running", "review"):
        spawned = {}
        with kb.connect() as conn:
            holder = kb.create_task(
                conn, title=f"{inflight_status} holder", assignee="coder",
                workspace_kind="dir", workspace_path=str(repo),
            )
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (inflight_status, holder))
            ready = kb.create_task(
                conn, title=f"ready behind {inflight_status}", assignee="coder",
                workspace_kind="dir", workspace_path=str(repo),
            )
            res = kb.dispatch_once(
                conn, spawn_fn=lambda task, workspace: spawned.setdefault(task.id, workspace),
                serialize_by_repo=True,
            )

        assert ready not in spawned
        assert ready in [task_id for task_id, _ in res.skipped_repo_serialized]


def test_promoted_blocked_task_can_be_retried(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="retry me", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        assert kb.block_task(conn, task_id, reason="bounded prerequisite")
        assert kb.promote_task(conn, task_id, actor="operator") == (True, None)
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, workspace: spawned.setdefault(task.id, workspace),
            serialize_by_repo=True,
        )

    assert task_id in spawned
    assert res.skipped_repo_serialized == []


def test_conflict_fixer_exempt_from_repo_serialization_lock(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    """A parked chain (blocked) holds its repo via serialize_by_repo. The
    conflict-park fixer created to REPAIR that chain works inside the same
    repo, so it must be exempt from the serialize guard — otherwise the parked
    chain deadlocks (2026-06-20 burn-dashboard: fixer stuck `ready` forever,
    needed manual rescue). A *normal* same-repo task stays serialized."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        # Parked parent (blocked) holds the repo serialize slot.
        parent = kb.create_task(
            conn, title="parked chain", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        assert kb.block_task(conn, parent, reason="integration parked")
        # The conflict-fixer for that parent, same repo, idempotency-marked
        # exactly as _create_conflict_park_fixer_subtask does.
        fixer = kb.create_task(
            conn, title="Konflikt-Fixer Kette", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key=f"conflict-fixer:{parent}:1",
        )
        # Control: a normal same-repo ready task must STAY serialized.
        normal = kb.create_task(
            conn, title="normal same-repo", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, serialize_by_repo=True)

    serialized_ids = [t[0] for t in res.skipped_repo_serialized]
    assert fixer in spawned, "conflict-fixer must break the serialize lock"
    assert fixer not in serialized_ids
    assert normal not in spawned
    assert normal in serialized_ids, "normal same-repo task must stay serialized"
    assert parent not in spawned


def test_repo_cap_default_one_byte_identical(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    """Default (kein max_concurrent_per_repo) == Cap 1 == heutiges serialize."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="coder",
                           workspace_kind="dir", workspace_path=str(repo))
        b = kb.create_task(conn, title="b", assignee="coder",
                           workspace_kind="dir", workspace_path=str(repo))
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, serialize_by_repo=True)

    assert len([t for t in (a, b) if t in spawned]) == 1
    assert len(res.skipped_repo_serialized) == 1


def test_repo_cap_two_allows_two_then_serializes(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    """Cap=2 lässt zwei Same-Repo-Tasks parallel zu, serialisiert den dritten."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        ids = [kb.create_task(conn, title=f"t{i}", assignee="coder",
                              workspace_kind="dir", workspace_path=str(repo))
               for i in range(3)]
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn,
            serialize_by_repo=True, max_concurrent_per_repo=2,
        )

    assert len([t for t in ids if t in spawned]) == 2
    assert len(res.skipped_repo_serialized) == 1


def test_conflict_fixer_exempt_under_full_cap(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    """Fixer bricht den Repo-Cap auch wenn er voll ausgeschöpft ist."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        holders = [
            kb.create_task(conn, title=f"holder{i}", assignee="coder",
                           workspace_kind="dir", workspace_path=str(repo))
            for i in range(2)
        ]
        conn.executemany(
            "UPDATE tasks SET status = 'running' WHERE id = ?",
            [(holder,) for holder in holders],
        )
        fixer = kb.create_task(
            conn, title="Konflikt-Fixer", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="conflict-fixer:t_parent:1",
        )
        normal = kb.create_task(conn, title="normal", assignee="coder",
                                workspace_kind="dir", workspace_path=str(repo))
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn,
            serialize_by_repo=True, max_concurrent_per_repo=2,
        )

    serialized = [t[0] for t in res.skipped_repo_serialized]
    assert fixer in spawned, "Fixer muss den vollen Cap brechen"
    assert normal not in spawned and normal in serialized


def test_serialize_off_ignores_cap(
    kanban_home, repo, all_assignees_spawnable, monkeypatch
):
    """serialize_by_repo=False bleibt No-Op, egal welcher Cap."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned = {}

    def fake_spawn(task, workspace):
        spawned[task.id] = workspace

    with kb.connect() as conn:
        ids = [kb.create_task(conn, title=f"t{i}", assignee="coder",
                              workspace_kind="dir", workspace_path=str(repo))
               for i in range(3)]
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn,
            serialize_by_repo=False, max_concurrent_per_repo=2,
        )

    assert len([t for t in ids if t in spawned]) == 3
    assert res.skipped_repo_serialized == []


def test_isolation_mode_reads_root_config(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    assert kwt.isolation_mode() == "off"
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  worker_isolation: worktree\n"
    )
    assert kwt.isolation_mode() == "worktree"
    monkeypatch.setenv("HERMES_KANBAN_WORKER_ISOLATION", "off")
    assert kwt.isolation_mode() == "off"  # env wins


"""Tests for worker isolation: dispatcher-provisioned worktrees + the
serialized chain integrator (hermes_cli.kanban_worktrees)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt


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


def _git(repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


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


def _commit_in(repo_or_wt, relpath, content, msg="change"):
    p = Path(repo_or_wt) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(repo_or_wt, "add", "-A")
    _git(repo_or_wt, "commit", "-m", msg)


def _ok_gate(_repo, _files):
    return True, "stub gate"


def _red_gate(_repo, _files):
    return False, "stub gate red"


def _events(conn, task_id, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


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


# ---------------------------------------------------------------------------
# Phase 2 — commit contract (prompt gating + metadata)
# ---------------------------------------------------------------------------

def test_commit_guidance_only_for_provisioned_workspace():
    from agent.prompt_builder import (
        KANBAN_COMMIT_GUIDANCE,
        KANBAN_GUIDANCE,
        kanban_commit_guidance_for,
    )

    assert kanban_commit_guidance_for("/r/.worktrees/kanban/t_x") == (
        KANBAN_COMMIT_GUIDANCE
    )
    assert kanban_commit_guidance_for("/home/x/.hermes/hermes-agent") == ""
    assert kanban_commit_guidance_for(None) == ""
    # The base guidance itself must NOT contain the commit contract —
    # live-checkout workers keep today's prompt (Entscheidung 1).
    assert "git add -A" not in KANBAN_GUIDANCE
    assert "git add -A" in KANBAN_COMMIT_GUIDANCE
    assert "NEVER push" in KANBAN_COMMIT_GUIDANCE


def test_claude_worker_prompt_gates_git_contract(kanban_home, monkeypatch, tmp_path):
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    def _spawn(workspace):
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee="coder")
            task = kb.get_task(conn, tid)
        kb._spawn_claude_worker(
            task, str(workspace), env={"PATH": "/usr/bin"}, board="default",
        )
        return captured["cmd"][captured["cmd"].index("-p") + 1]

    wt = tmp_path / "r" / ".worktrees" / "kanban" / "t_chain"
    wt.mkdir(parents=True)
    prompt_provisioned = _spawn(wt)
    assert "Git contract" in prompt_provisioned
    assert "git add -A && git commit" in prompt_provisioned
    assert "NEVER push" in prompt_provisioned

    prompt_plain = _spawn(tmp_path)
    assert "Git contract" not in prompt_plain
    assert "git add -A" not in prompt_plain


def test_claude_worker_prompt_includes_parent_results(kanban_home, monkeypatch, tmp_path):
    """A claude-CLI worker has no kanban_show, so parent task results must be
    baked into the prompt — using the same renderer as the Hermes worker path
    so both runtimes show identical parent-results context."""
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent job", assignee="researcher")
        child = kb.create_task(
            conn, title="child impl", assignee="coder", parents=[parent],
        )
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="authoritative parent result")
        task = kb.get_task(conn, child)

    wt = tmp_path / "wt"
    wt.mkdir()
    kb._spawn_claude_worker(
        task, str(wt), env={"PATH": "/usr/bin"}, board="default",
    )
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "## Parent task results" in prompt
    assert "authoritative parent result" in prompt
    assert f"### {parent}" in prompt


def test_claude_worker_prompt_labels_scout_parent_advisory(kanban_home, monkeypatch, tmp_path):
    """A scout parent's result must render under '## Advisory scout notes' with
    the source-of-truth warning in the claude-CLI prompt — same as the Hermes
    worker path — so a claude-CLI coder treats scout recon as hints, not a
    committed parent outcome it must follow."""
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    with kb.connect() as conn:
        scout = kb.create_task(conn, title="Scout: recon", assignee="scout")
        child = kb.create_task(
            conn, title="impl", assignee="coder", parents=[scout],
        )
        kb.claim_task(conn, scout)
        kb.complete_task(conn, scout, summary="advisory recon hint")
        task = kb.get_task(conn, child)

    wt = tmp_path / "wt"
    wt.mkdir()
    kb._spawn_claude_worker(
        task, str(wt), env={"PATH": "/usr/bin"}, board="default",
    )
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "## Advisory scout notes" in prompt
    assert "source of truth" in prompt.lower()
    assert "advisory recon hint" in prompt
    assert "## Parent task results" not in prompt
    assert f"### {scout} (scout)" in prompt


def test_claude_worker_prompt_includes_tenant_scoped_role_history(
    kanban_home, monkeypatch, tmp_path,
):
    """The claude-CLI prompt must include tenant-scoped recent-work history
    so the worker gets role continuity — but must NOT leak cross-tenant
    summaries, matching the Hermes worker path's tenant isolation."""
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    with kb.connect() as conn:
        a = kb.create_task(conn, title="A job", assignee="coder", tenant="tenant-a")
        kb.claim_task(conn, a)
        kb.complete_task(conn, a, summary="did the tenant-A thing")
        b = kb.create_task(conn, title="B job", assignee="coder", tenant="tenant-b")
        kb.claim_task(conn, b)
        kb.complete_task(conn, b, summary="did the tenant-B thing")
        new_a = kb.create_task(
            conn, title="A followup", assignee="coder", tenant="tenant-a",
        )
        task = kb.get_task(conn, new_a)

    wt = tmp_path / "wt"
    wt.mkdir()
    kb._spawn_claude_worker(
        task, str(wt), env={"PATH": "/usr/bin"}, board="default",
    )
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "## Recent work by @coder" in prompt
    assert "did the tenant-A thing" in prompt
    # cross-tenant contamination must not leak into the claude-CLI prompt
    assert "did the tenant-B thing" not in prompt
    assert "NOT this" in prompt


def test_claude_worker_prompt_no_parent_block_when_orphaned(kanban_home, monkeypatch, tmp_path):
    """A task with no done parents emits no parent-results block — the prompt
    stays lean and byte-identical to the pre-parity status quo for orphan
    tasks."""
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="orphan", assignee="coder")
        task = kb.get_task(conn, tid)

    wt = tmp_path / "wt"
    wt.mkdir()
    kb._spawn_claude_worker(
        task, str(wt), env={"PATH": "/usr/bin"}, board="default",
    )
    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "## Parent task results" not in prompt
    assert "## Advisory scout notes" not in prompt


def test_complete_promotes_commit_hash_to_event(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done",
            metadata={"commit": "abc123def456", "changed_files": ["x.py"]},
        )
        events = _events(conn, tid, "completed")
    assert events and events[0].get("commit") == "abc123def456"


def test_note_dirty_worktree_comments_only_when_dirty(kanban_home, repo):
    info = kwt.ensure_worktree(repo, "t_dirty")
    wt = info["path"]
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="coder",
            workspace_kind="dir", workspace_path=str(wt),
        )
        # Clean worktree → no comment.
        kwt.note_dirty_worktree(conn, tid, str(wt))
        n0 = conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()[0]
        assert n0 == 0
        # Uncommitted leftovers → one warning comment.
        (wt / "leftover.py").write_text("x = 1\n")
        kwt.note_dirty_worktree(conn, tid, str(wt))
        rows = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["author"] == "integrator"
    assert "DIRTY_WORKTREE" in rows[0]["body"]
    assert "leftover.py" in rows[0]["body"]
    assert "Recovery: commit intentional source changes" in rows[0]["body"]
    assert "worker contract" not in rows[0]["body"]


def test_note_dirty_worktree_classifies_artifact_leftovers(kanban_home, repo):
    info = kwt.ensure_worktree(repo, "t_dirty_artifact")
    wt = info["path"]
    (wt / "playwright-report").mkdir()
    (wt / "playwright-report" / "index.html").write_text("<html></html>\n")
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="coder",
            workspace_kind="dir", workspace_path=str(wt),
        )
        kwt.note_dirty_worktree(conn, tid, str(wt))
        body = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()["body"]
    assert "PRESERVABLE_ARTIFACTS" in body
    assert "playwright-report/index.html" in body
    assert "integrator can preserve" in body
    assert "worker contract" not in body


def test_artifact_policy_missing_message_names_recovery():
    dirty_class = kwt._classify_dirty_paths(["coverage/index.html"])
    assert dirty_class == kwt.ARTIFACT_POLICY_MISSING_CLASS
    recovery = kwt._dirty_recovery_instruction(dirty_class)
    assert "approved preserve prefixes" in recovery
    assert "extend the artifact policy" in recovery


@pytest.mark.parametrize(
    "path",
    [
        "screenshots/login.png",
        "screenshots/nested/dialog.png",
    ],
)
def test_common_visual_qa_dirs_are_preservable(path):
    # Regression: screenshots/ is emitted by common visual-QA tooling.
    # It must preserve to the Vault receipt, not park as DIRTY_WORKTREE
    # just because it sits outside the original 5 prefixes.
    assert kwt._is_preservable_artifact_path(path) is True
    assert kwt._classify_dirty_paths([path]) == kwt.PRESERVABLE_ARTIFACTS_CLASS


@pytest.mark.parametrize(
    "path",
    [
        "playwright/.auth/state.json",
        "playwright/.auth/user.json",
        "playwright/.auth/subdir/token.json",
    ],
)
def test_playwright_auth_dir_is_neither_preserved_nor_parked(path):
    # Secret-safety regression: playwright/.auth/ holds storageState
    # (session tokens). It must NOT be preservable (would copy secrets to
    # the Vault via shutil.copy2) and must NOT park as DIRTY_WORKTREE.
    # Instead it is ignored entirely by dirty_files().
    assert kwt._is_preservable_artifact_path(path) is False
    assert kwt._is_ignorable_dirty_path(path) is True


def test_screenshots_classify_as_preservable():
    paths = ["screenshots/home.png", "screenshots/error.png"]
    assert kwt._classify_dirty_paths(paths) == kwt.PRESERVABLE_ARTIFACTS_CLASS


def test_playwright_auth_does_not_park_dirty_worktree():
    # When playwright/.auth/ is the only dirty path, dirty_files() filters
    # it out entirely → empty list → no DIRTY_WORKTREE classification.
    assert kwt.dirty_files.__name__  # sanity: function exists
    assert kwt._is_ignorable_dirty_path("playwright/.auth/state.json")
    assert kwt._is_ignorable_dirty_path("playwright/.auth/subdir/token.json")


def test_preservable_screenshots_with_source_change_still_parks():
    # Counter-metric guardrail (AC-2): broadening the preserve allowlist must
    # NOT cause genuine uncommitted source changes to be cleaned away — a mix of
    # a preservable screenshot and a real .py edit still parks as DIRTY_WORKTREE.
    paths = ["screenshots/home.png", "hermes_cli/kanban_db.py"]
    assert kwt._classify_dirty_paths(paths) == kwt.DIRTY_WORKTREE_CLASS


# ---------------------------------------------------------------------------
# Package 1A — default quick gate web quality checks
# ---------------------------------------------------------------------------

def _install_fake_web_bins(repo):
    bin_dir = repo / "web" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("tsc", "vitest"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)


def _install_fake_root_bins(repo):
    # Hoisted npm-workspace layout: a single-version dep (e.g. typescript) is
    # deduped into the ROOT node_modules/.bin, leaving web/node_modules/.bin empty.
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("tsc", "vitest"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)


def test_default_quick_gate_web_diff_runs_control_frontend_gates(repo, monkeypatch):
    _install_fake_web_bins(repo)
    calls = []

    def fake_which(name):
        return {
            "npm": "/usr/bin/npm",
            "npx": "/usr/bin/npx",
        }.get(name)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", fake_which)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["web/src/control/App.tsx"])

    assert ok is True
    assert "lint:control ok" in detail
    assert "tsc -b ok" in detail
    assert "vitest[control] ok" in detail
    assert ["/usr/bin/npm", "run", "lint:control"] in calls
    assert ["/usr/bin/npx", "vitest", "run", "src/control"] in calls
    assert any(cmd[-2:] == ["-b", "--noEmit"] and "tsc" in cmd[0] for cmd in calls)
    assert ["/usr/bin/npm", "run", "build"] not in calls


def test_default_quick_gate_web_diff_resolves_hoisted_root_bins(repo, monkeypatch):
    # Regression (2026-06-20 burn-dashboard incident): npm-workspace hoisting puts
    # tsc/vitest in ROOT node_modules/.bin, NOT web/node_modules/.bin. The gate must
    # resolve them from root and pass instead of reverting the merge as "tsc missing".
    _install_fake_root_bins(repo)  # ROOT bins only; web/node_modules/.bin absent
    calls = []

    def fake_which(name):
        return {"npm": "/usr/bin/npm", "npx": "/usr/bin/npx"}.get(name)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", fake_which)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["web/src/control/App.tsx"])

    assert ok is True, detail
    assert "tsc -b ok" in detail
    assert "vitest[control] ok" in detail
    # tsc must have been invoked from the ROOT node_modules/.bin, not web/.
    root_tsc = str(repo / "node_modules" / ".bin" / "tsc")
    assert any(cmd[-2:] == ["-b", "--noEmit"] and cmd[0] == root_tsc for cmd in calls)


def test_default_quick_gate_web_diff_fails_when_bins_missing_everywhere(repo, monkeypatch):
    # Neither web/ nor root has the bins → fail closed (cannot type-check the diff).
    calls = []

    def fake_which(name):
        return {"npm": "/usr/bin/npm", "npx": "/usr/bin/npx"}.get(name)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", fake_which)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["web/src/control/App.tsx"])

    assert ok is False
    assert "tsc" in detail and "not found" in detail


def test_default_quick_gate_non_web_diff_skips_frontend_gates(repo, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["hermes_cli/kanban_db.py"])

    assert ok is True
    assert "pytest skipped" in detail
    flattened = [" ".join(cmd) for cmd in calls]
    assert not any("npm run lint:control" in cmd for cmd in flattened)
    assert not any("vitest run src/control" in cmd for cmd in flattened)
    assert not any("tsc -b --noEmit" in cmd for cmd in flattened)
    assert not any("tsc --noEmit" in cmd for cmd in flattened)


def test_default_quick_gate_frontend_failure_fails_closed(repo, monkeypatch):
    _install_fake_web_bins(repo)

    def fake_which(name):
        return {
            "npm": "/usr/bin/npm",
            "npx": "/usr/bin/npx",
        }.get(name)

    def fake_run(argv, **kwargs):
        if argv[:3] == ["/usr/bin/npm", "run", "lint:control"]:
            return SimpleNamespace(returncode=1, stdout="lint failed", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", fake_which)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["web/src/control/App.tsx"])

    assert ok is False
    assert "lint:control: exit 1" in detail
    assert "lint failed" in detail


# ---------------------------------------------------------------------------
# Phase 3 — serialized integrator
# ---------------------------------------------------------------------------

def _provisioned_chain(repo, root_id, relpath="feature.py",
                       content="VALUE = 1\n"):
    """Worktree with one committed change, ready to integrate."""
    info = kwt.ensure_worktree(repo, root_id)
    _commit_in(info["path"], relpath, content, msg=f"kanban({root_id}): work")
    return info


@pytest.mark.parametrize(
    "reason",
    [
        "live checkout has an operation in progress (MERGE_HEAD)",
        "checked-out branch 'other-branch' != frozen merge target 'main'",
        "worktree has uncommitted changes but no commits to merge",
        "chain worktree has uncommitted changes: uncommitted.py",
        "dirty files in live checkout overlap the branch diff: a.txt",
        "chain worktree missing before rebase",
    ],
)
def test_integration_park_class_marks_transient_reasons(reason):
    assert kwt._integration_park_class(reason) == "transient"


@pytest.mark.parametrize(
    "reason",
    [
        "merge conflict/failure (aborted): conflict details",
        "post-merge gate failed: ruff failed",
    ],
)
def test_integration_park_class_marks_orchestrator_reasons(reason):
    assert kwt._integration_park_class(reason) == "needs_orchestrator"


@pytest.mark.parametrize(
    "reason",
    [
        "cannot inspect live checkout: rev-parse failed",
        "some unexpected integrator failure",
        "",
    ],
)
def test_integration_park_class_marks_operator_reasons(reason):
    assert kwt._integration_park_class(reason) == "needs_operator"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # As stored by _park_integration (kanban_db.py): the raw integrator
        # reason gets an "integration parked: " prefix. The retry lane reads
        # this stored form, so the classifier must strip it before matching.
        (
            "integration parked: dirty files in live checkout overlap the "
            "branch diff: a.txt",
            "transient",
        ),
        (
            "integration parked: merge conflict/failure (aborted): boom",
            "needs_orchestrator",
        ),
        (
            "integration parked: cannot inspect live checkout: rev-parse failed",
            "needs_operator",
        ),
    ],
)
def test_integration_park_class_strips_stored_prefix(reason, expected):
    assert kwt._integration_park_class(reason) == expected


def test_integrate_merges_no_ff_and_cleans_up(repo):
    info = _provisioned_chain(repo, "t_m1")
    validated_heads = []

    def gate(validation_root, _files):
        validated_heads.append(_git(validation_root, "rev-parse", "HEAD"))
        return True, "stub gate"

    out = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=gate,
    )
    assert out["action"] == "merged"
    assert validated_heads == [out["merge_commit"]]
    # --no-ff: HEAD is a real merge commit with two parents.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert (repo / "feature.py").read_text() == "VALUE = 1\n"
    # Worktree and branch are gone.
    assert not info["path"].exists()
    assert "kanban/t_m1" not in _git(repo, "branch", "--list", "kanban/*")


def test_two_chains_two_separate_merge_commits(repo):
    a = _provisioned_chain(repo, "t_a", relpath="a_mod.py")
    b = _provisioned_chain(repo, "t_b", relpath="b_mod.py")
    out_a = kwt.integrate_chain(repo, a["path"], a["branch"], "main",
                                gate_runner=_ok_gate)
    out_b = kwt.integrate_chain(repo, b["path"], b["branch"], "main",
                                gate_runner=_ok_gate)
    assert out_a["action"] == "merged"
    assert out_b["action"] == "merged"
    assert out_a["merge_commit"] != out_b["merge_commit"]
    merges = _git(repo, "log", "--merges", "--oneline").splitlines()
    assert len(merges) == 2


def test_dirty_files_reports_full_path_of_unstaged_first_entry(repo):
    """Regression: a single unstaged modification must report its FULL path.

    ``git status --porcelain -z`` renders an unstaged change as ``" M a.txt\0"``
    — a leading space in the status column. ``dirty_files`` must not let that
    leading space be stripped away (it would shift the parse and drop the first
    character of the path, e.g. ``a.txt`` -> ``.txt``), or the overlap pre-check
    silently misses real dirty-overlaps and a transient park misclassifies as a
    merge conflict.
    """
    (repo / "a.txt").write_text("foreign edit\n")
    assert kwt.dirty_files(repo) == ["a.txt"]
    # A second dirty file must still parse correctly regardless of ordering.
    (repo / "z_new.txt").write_text("new\n")
    assert set(kwt.dirty_files(repo)) == {"a.txt", "z_new.txt"}


def test_overlap_with_dirty_live_checkout_parks(repo):
    info = _provisioned_chain(repo, "t_ovl", relpath="a.txt",
                              content="branch change\n")
    # Foreign uncommitted edit of the SAME file in the live checkout.
    (repo / "a.txt").write_text("manual session edit\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    # Be specific: the park must be the OVERLAP pre-check, not an incidental
    # "overlap" substring leaking in from the tmp repo path inside a merge-error
    # reason. That coincidence masked a real dirty_files parse bug before.
    assert out["reason"].startswith(
        "dirty files in live checkout overlap the branch diff:"
    )
    assert "a.txt" in out["reason"]
    # Nothing merged; the manual edit is untouched.
    assert (repo / "a.txt").read_text() == "manual session edit\n"
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_nonoverlapping_dirty_file_does_not_park(repo):
    """Entscheidung 2: overlap check only — foreign dirty files OUTSIDE the
    branch diff don't block the merge."""
    info = _provisioned_chain(repo, "t_novl", relpath="feature.py")
    (repo / "unrelated.txt").write_text("manual session\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert (repo / "unrelated.txt").read_text() == "manual session\n"


def test_rebase_conflict_aborts_and_returns_to_coder(repo):
    # B1: the pre-merge rebase catches the conflict FIRST (before the merge), so
    # a branch that conflicts with the advanced main is routed back to the coder
    # via a ``rebase_conflict`` outcome instead of a silent ``parked``.
    info = _provisioned_chain(repo, "t_cfl", relpath="a.txt",
                              content="branch version\n")
    _commit_in(repo, "a.txt", "main version\n", msg="conflicting main commit")
    head_before = _git(repo, "rev-parse", "HEAD")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "rebase_conflict"
    assert "conflict" in out["reason"]
    assert out["target"] == "main"
    # main HEAD unchanged (the rebase ran in the chain worktree; the merge
    # never ran), no MERGE_HEAD left behind.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    assert not (git_dir / "MERGE_HEAD").exists()
    # Rebase aborted cleanly: no rebase state in the chain worktree, tree clean.
    wt_git_dir = Path(_git(info["path"], "rev-parse", "--absolute-git-dir"))
    assert not (wt_git_dir / "rebase-merge").exists()
    assert not (wt_git_dir / "rebase-apply").exists()
    assert _git(info["path"], "status", "--porcelain") == ""


def test_rebase_onto_advanced_main_then_merges(repo):
    # B1: main advances with an unrelated, non-overlapping commit AFTER the chain
    # branched. The pre-merge rebase replays the chain onto the new main, so the
    # merge lands cleanly and history contains BOTH commits (no conflict, no park).
    info = _provisioned_chain(repo, "t_ff", relpath="feature.py",
                              content="VALUE = 1\n")
    _commit_in(repo, "unrelated.txt", "advanced\n", msg="unrelated main commit")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert out.get("merge_commit")
    log = _git(repo, "log", "--oneline")
    assert "unrelated main commit" in log
    assert (repo / "feature.py").exists()
    assert (repo / "unrelated.txt").exists()


def test_target_mismatch_parks(repo):
    info = _provisioned_chain(repo, "t_tgt")
    _git(repo, "checkout", "-b", "other-branch")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert "frozen merge target" in out["reason"]
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_operation_in_progress_parks(repo):
    info = _provisioned_chain(repo, "t_oip")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    (git_dir / "MERGE_HEAD").write_text(_git(repo, "rev-parse", "HEAD") + "\n")
    try:
        out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                                  gate_runner=_ok_gate)
    finally:
        (git_dir / "MERGE_HEAD").unlink()
    assert out["action"] == "parked"
    assert "operation in progress" in out["reason"]


def test_red_gate_reverts_merge_and_parks(repo):
    info = _provisioned_chain(repo, "t_red", relpath="breaks.py")
    validation_roots = []

    def red_gate(validation_root, _files):
        validation_roots.append(Path(validation_root))
        return False, "stub gate red"

    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=red_gate)
    assert out["action"] == "parked"
    assert "post-merge gate failed" in out["reason"]
    assert out["reverted"] is True
    # The merge commit exists in history but its content is reverted.
    merges = _git(repo, "log", "--merges", "--oneline").splitlines()
    assert len(merges) == 1
    assert not (repo / "breaks.py").exists()
    # Live branch stays provably green: HEAD is the revert commit.
    head_subject = _git(repo, "log", "-1", "--format=%s")
    assert head_subject.startswith("Revert")
    assert validation_roots and all(not root.exists() for root in validation_roots)


def _red_gate_web(_repo, _files):
    """Stub mimicking a real ``tsc -b`` failure label — the incident shape
    (t_2fa852c6): AutoReleaseTile.test.tsx is an untracked foreign file."""
    return False, "tsc -b: exit 2\nerror TS2345 in AutoReleaseTile.test.tsx"


def _red_gate_python(_repo, _files):
    """Stub mimicking a real ``pytest[N]`` failure label."""
    return False, "pytest[1]: exit 1\nFAILED tests/hermes_cli/test_wip_broken.py"


def test_foreign_dirty_web_file_is_absent_from_clean_validation_worktree(repo):
    """A foreign live-checkout web file cannot create a false-red gate."""
    info = _provisioned_chain(repo, "t_fdc_web", relpath="feature.py")
    foreign = repo / "web" / "src" / "control" / "AutoReleaseTile.test.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// half-finished foreign test\n")
    validation_roots = []

    def gate(validation_root, _files):
        validation_roots.append(Path(validation_root))
        contaminated = (
            Path(validation_root)
            / "web/src/control/AutoReleaseTile.test.tsx"
        ).exists()
        return (not contaminated, "clean" if not contaminated else "contaminated")

    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=gate)
    assert out["action"] == "merged"
    assert validation_roots and all(root != repo for root in validation_roots)
    assert all(not root.exists() for root in validation_roots)
    assert foreign.read_text() == "// half-finished foreign test\n"


def test_foreign_dirty_python_file_is_absent_from_clean_validation_worktree(repo):
    """A foreign live-checkout Python test cannot create a false-red gate."""
    info = _provisioned_chain(repo, "t_fdc_py", relpath="feature.py")
    foreign = repo / "tests" / "hermes_cli" / "test_wip_broken.py"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("def test_x():\n    assert False\n")
    validation_roots = []

    def gate(validation_root, _files):
        validation_roots.append(Path(validation_root))
        contaminated = (
            Path(validation_root) / "tests/hermes_cli/test_wip_broken.py"
        ).exists()
        return (not contaminated, "clean" if not contaminated else "contaminated")

    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=gate)
    assert out["action"] == "merged"
    assert validation_roots and all(not root.exists() for root in validation_roots)
    assert foreign.exists()


def test_red_gate_without_foreign_dirty_keeps_generic_classification(repo):
    """DONE-WHEN (b) regression: a red gate with NO foreign dirty files in
    the failing stage's scope keeps today's generic 'post-merge gate failed'
    park + revert, byte-identical to test_red_gate_reverts_merge_and_parks."""
    info = _provisioned_chain(repo, "t_red_clean", relpath="breaks.py")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate_web)
    assert out["action"] == "parked"
    assert out["reason"].startswith("post-merge gate failed:")
    assert "park_class" not in out
    assert out["reverted"] is True


def test_foreign_dirty_web_file_cannot_contaminate_green_gate(repo):
    """A green result now proves a clean commit checkout, not annotated dirt."""
    info = _provisioned_chain(
        repo, "t_fdc_green", relpath="web/src/control/Foo.tsx",
        content="export const x = 1;\n",
    )
    foreign = repo / "web" / "src" / "control" / "AutoReleaseTile.test.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// half-finished foreign test\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert "gate_environment" not in out
    assert "foreign_dirty_files" not in out
    assert foreign.exists()


def test_clean_checkout_green_has_no_gate_environment_flag(repo):
    """DONE-WHEN (4) regression: a genuinely clean checkout must not gain the
    additive gate_environment metadata — identical behavior to today."""
    info = _provisioned_chain(repo, "t_clean_green", relpath="feature.py")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert "gate_environment" not in out
    assert "foreign_dirty_files" not in out


def test_overlapping_dirty_file_parks_by_overlap_not_foreign_dirty_checkout(repo):
    """DONE-WHEN (d) regression: a dirty file that OVERLAPS the branch diff
    still parks via the pre-existing overlap pre-check (a), unaffected by the
    new foreign-dirty-checkout classification introduced above."""
    info = _provisioned_chain(repo, "t_ovl_regression", relpath="a.txt",
                              content="branch change\n")
    (repo / "a.txt").write_text("manual session edit\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate_web)
    assert out["action"] == "parked"
    assert out["reason"].startswith(
        "dirty files in live checkout overlap the branch diff:"
    )
    assert "park_class" not in out


def test_reverted_merge_is_reintegrated_not_clean(repo):
    info = _provisioned_chain(repo, "t_reverted", relpath="restored.py")
    gate_results = iter([(False, "first gate failed"), (True, "gate ok")])

    out1 = kwt.integrate_chain(
        repo,
        info["path"],
        info["branch"],
        "main",
        gate_runner=lambda _repo, _files: next(gate_results),
    )

    assert out1["action"] == "parked"
    assert out1["gate_output"] == "first gate failed"
    assert kwt._branch_is_ancestor(repo, info["branch"], "main") is True
    assert not (repo / "restored.py").exists()

    out2 = kwt.integrate_chain(
        repo,
        info["path"],
        info["branch"],
        "main",
        gate_runner=lambda _repo, _files: next(gate_results),
    )

    assert out2["action"] == "merged"
    assert out2["reintegrated_after_revert"] is True
    assert out2["original_merge_commit"] == out1["merge_commit"]
    assert "revert_commit" in out2
    assert (repo / "restored.py").read_text() == "VALUE = 1\n"
    assert not info["path"].exists()


def test_reverted_ancestor_is_replayed_with_later_branch_commit(repo):
    """A later B commit must not make a reverted, reviewed A look integrated."""
    info = _provisioned_chain(
        repo, "t_reverted_ancestor", relpath="acceptance.py",
        content="ACCEPTED = True\n",
    )
    accepted_commit = _git(info["path"], "rev-parse", "HEAD")
    first = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=_ok_gate,
    )
    assert first["action"] == "merged"

    _git(repo, "revert", "-m", "1", "--no-edit", first["merge_commit"])
    _git(repo, "branch", info["branch"], accepted_commit)
    _git(repo, "worktree", "add", str(info["path"]), info["branch"])
    _commit_in(info["path"], "hardening.py", "HARDENED = True\n", "B")

    gated_files = []

    def recording_gate(_repo, files):
        gated_files.extend(files)
        return True, "recorded"

    out = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=recording_gate,
    )

    assert out["action"] == "merged"
    assert (repo / "acceptance.py").read_text() == "ACCEPTED = True\n"
    assert (repo / "hardening.py").read_text() == "HARDENED = True\n"
    assert set(out["changed_files"]) == {"acceptance.py", "hardening.py"}
    assert set(gated_files) == {"acceptance.py", "hardening.py"}


def test_branch_created_after_revert_does_not_restore_unrelated_merge(repo):
    info = _provisioned_chain(repo, "t_old", relpath="old.py", content="OLD = True\n")
    first = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=_ok_gate,
    )
    _git(repo, "revert", "-m", "1", "--no-edit", first["merge_commit"])

    later = _provisioned_chain(
        repo, "t_later", relpath="later.py", content="LATER = True\n",
    )
    out = kwt.integrate_chain(
        repo, later["path"], later["branch"], "main", gate_runner=_ok_gate,
    )

    assert out["action"] == "merged"
    assert not (repo / "old.py").exists()
    assert out["changed_files"] == ["later.py"]


def test_reintegration_gate_uses_clean_validation_worktree(repo):
    """The revert-of-revert gate is isolated from later foreign live WIP."""
    info = _provisioned_chain(repo, "t_reint_fdc", relpath="restored.py")
    validation_roots = []
    calls = 0

    def gate(validation_root, _files):
        nonlocal calls
        calls += 1
        validation_roots.append(Path(validation_root))
        if calls == 1:
            return False, "first gate failed"
        contaminated = (
            Path(validation_root)
            / "web/src/control/AutoReleaseTile.test.tsx"
        ).exists()
        return (not contaminated, "clean" if not contaminated else "contaminated")

    out1 = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main",
        gate_runner=gate,
    )
    assert out1["action"] == "parked"
    assert kwt._branch_is_ancestor(repo, info["branch"], "main") is True

    # A foreign session leaves an untracked WIP file behind between the
    # first (generic) park and the second (reintegration) attempt.
    foreign = repo / "web" / "src" / "control" / "AutoReleaseTile.test.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// half-finished foreign test\n")

    out2 = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main",
        gate_runner=gate,
    )

    assert out2["action"] == "merged"
    assert out2["reintegrated_after_revert"] is True
    assert out2["merge_commit"] == _git(repo, "rev-parse", "HEAD")
    assert all(not root.exists() for root in validation_roots)
    assert foreign.read_text() == "// half-finished foreign test\n"


def test_integration_parked_writes_full_gate_output_comment(kanban_home):
    full_output = "line-000\n" + "x" * 5000 + "\nline-end"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gate fail", assignee="coder")
        assert kb._park_integration(
            conn,
            tid,
            {"reason": "post-merge gate failed", "gate_output": full_output},
        )
        body = conn.execute(
            "SELECT body FROM task_comments "
            "WHERE task_id = ? AND author = 'integrator' "
            "ORDER BY created_at DESC LIMIT 1",
            (tid,),
        ).fetchone()["body"]

    assert "Post-merge gate failed; full gate output follows." in body
    assert full_output in body


def test_dirty_chain_worktree_parks(repo):
    info = _provisioned_chain(repo, "t_dwt")
    (info["path"] / "uncommitted.py").write_text("oops = 1\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert out["park_class"] == "DIRTY_WORKTREE"
    assert "DIRTY_WORKTREE" in out["reason"]
    assert "uncommitted" in out["reason"]


def test_artifact_policy_missing_chain_worktree_parks_with_recovery(repo):
    info = _provisioned_chain(repo, "t_artifact_policy")
    wt = info["path"]
    (wt / "coverage").mkdir()
    (wt / "coverage" / "index.html").write_text("<html></html>\n")
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert out["park_class"] == "ARTIFACT_POLICY_MISSING"
    assert "ARTIFACT_POLICY_MISSING" in out["reason"]
    assert "extend the artifact policy" in out["reason"]


def test_deliverable_md_alone_does_not_block_clean_close(repo):
    info = kwt.ensure_worktree(repo, "t_deliverable")
    (info["path"] / ".deliverable.md").write_text("# handoff\n")

    assert kwt.dirty_files(info["path"]) == []
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "clean"
    assert not info["path"].exists()


def test_cache_byproducts_do_not_count_as_dirty(repo):
    """Gate runs write __pycache__/.pytest_cache into the worktree; in repos
    without a .gitignore those must NOT park the chain (live E2E finding
    2026-06-11: verifier's ruff run created util.cpython-311.pyc → park)."""
    info = _provisioned_chain(repo, "t_cache")
    wt = info["path"]
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "feature.cpython-311.pyc").write_bytes(b"\x00")
    (wt / ".pytest_cache").mkdir()
    (wt / ".pytest_cache" / "CACHEDIR.TAG").write_text("tag")
    (wt / "stray.pyc").write_bytes(b"\x00")
    assert kwt.dirty_files(wt) == []
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    # A REAL uncommitted file still parks (filter is noise-only).
    info2 = _provisioned_chain(repo, "t_cache2", relpath="other.py")
    (info2["path"] / "__pycache__").mkdir()
    (info2["path"] / "real_leftover.py").write_text("x = 1\n")
    out2 = kwt.integrate_chain(repo, info2["path"], info2["branch"], "main",
                               gate_runner=_ok_gate)
    assert out2["action"] == "parked"
    assert "real_leftover.py" in out2["reason"]


def test_visual_artifacts_are_preserved_then_chain_merges(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    monkeypatch.setattr(kwt, "_artifact_receipt_timestamp", lambda: "20260621T010203Z")
    info = _provisioned_chain(repo, "t_artifact")
    wt = info["path"]
    (wt / ".playwright-mcp").mkdir()
    (wt / ".playwright-mcp" / "console.log").write_text("[]")
    (wt / ".playwright-mcp" / "page.yml").write_text("a: 1")

    assert sorted(kwt.dirty_files(wt)) == [
        ".playwright-mcp/console.log",
        ".playwright-mcp/page.yml",
    ]
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "merged"
    receipt = out["artifact_receipt"]
    assert receipt["destination"] == str(tmp_path / "receipts" / "t_artifact-20260621T010203Z")
    assert receipt["file_count"] == 2
    assert sorted(receipt["paths"]) == [
        ".playwright-mcp/console.log",
        ".playwright-mcp/page.yml",
    ]
    assert (Path(receipt["destination"]) / ".playwright-mcp" / "console.log").read_text() == "[]"
    assert (repo / "feature.py").read_text() == "VALUE = 1\n"
    assert not wt.exists()


def test_mixed_artifacts_and_source_change_park_without_cleanup(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    info = _provisioned_chain(repo, "t_mixed")
    wt = info["path"]
    (wt / ".playwright-mcp").mkdir()
    (wt / ".playwright-mcp" / "console.log").write_text("[]")
    (wt / "uncommitted.py").write_text("oops = 1\n")

    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "parked"
    assert "uncommitted.py" in out["reason"]
    assert "artifact_receipt" not in out
    assert (wt / ".playwright-mcp" / "console.log").exists()
    assert (wt / "uncommitted.py").exists()
    assert not (tmp_path / "receipts").exists()


def test_artifact_copy_failure_parks_without_deleting(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    info = _provisioned_chain(repo, "t_copyfail")
    wt = info["path"]
    (wt / ".playwright-mcp").mkdir()
    artifact = wt / ".playwright-mcp" / "console.log"
    artifact.write_text("[]")

    def fail_copy(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(kwt.shutil, "copy2", fail_copy)
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "parked"
    assert out["park_class"] == "ARTIFACT_PRESERVE_FAILED"
    assert "ARTIFACT_PRESERVE_FAILED" in out["reason"]
    assert artifact.read_text() == "[]"


def test_no_commits_is_clean_and_removes_worktree(repo):
    info = kwt.ensure_worktree(repo, "t_empty")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "clean"
    assert not info["path"].exists()
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_affected_pytest_module_mapping(repo):
    (repo / "tests" / "hermes_cli").mkdir(parents=True)
    (repo / "tests" / "hermes_cli" / "test_kanban_db.py").write_text("")
    (repo / "tests" / "stress").mkdir(parents=True)
    (repo / "tests" / "stress" / "test_atypical_scenarios.py").write_text("")
    mods = kwt._affected_pytest_modules(
        repo,
        ["hermes_cli/kanban_db.py", "hermes_cli/no_tests.py",
         "web/src/x.ts", "tests/hermes_cli/test_kanban_db.py",
         "tests/stress/test_atypical_scenarios.py"],
    )
    # hermes_cli/kanban_db.py -> 1:1 match
    # hermes_cli/no_tests.py -> no 1:1 test -> fallback to tests/hermes_cli/
    # tests/hermes_cli/test_kanban_db.py -> runs itself
    # tests/stress/ skipped
    assert mods == ["tests/hermes_cli/", "tests/hermes_cli/test_kanban_db.py"]


def test_affected_pytest_module_matches_submodule_from_import_sibling(repo):
    (repo / "hermes_cli").mkdir(parents=True)
    (repo / "tests" / "hermes_cli").mkdir(parents=True)
    (repo / "tests" / "hermes_cli" / "test_commands.py").write_text("")
    (repo / "tests" / "hermes_cli" / "test_goals.py").write_text(
        "from hermes_cli.commands import resolve_command\n"
    )

    mods = kwt._affected_pytest_modules(repo, ["hermes_cli/commands.py"])

    assert mods == [
        "tests/hermes_cli/test_commands.py",
        "tests/hermes_cli/test_goals.py",
    ]


def test_affected_pytest_module_fallback_for_monolith(repo):
    """A monolith source file with no 1:1 test selects the package test dir."""
    (repo / "gateway").mkdir(parents=True)
    (repo / "tests" / "gateway").mkdir(parents=True)
    (repo / "tests" / "gateway" / "test_shutdown_cache_cleanup.py").write_text("")
    mods = kwt._affected_pytest_modules(repo, ["gateway/run.py"])
    assert mods == ["tests/gateway/"]


def test_affected_pytest_module_oversize_dir_downgrades(repo):
    """When the package test dir exceeds _FALLBACK_MAX_TEST_FILES, the
    fallback downgrades to no selection — nightly full suite remains the
    backstop (AC-2 counter-metric: no gate-tempo-for-coverage trade)."""
    (repo / "gateway").mkdir(parents=True)
    pkg = repo / "tests" / "gateway"
    pkg.mkdir(parents=True)
    cap = kwt._FALLBACK_MAX_TEST_FILES
    for i in range(cap + 1):
        (pkg / f"test_{i:04d}.py").write_text("")
    mods = kwt._affected_pytest_modules(repo, ["gateway/run.py"])
    assert mods == []


def test_affected_pytest_module_no_fallback_for_root_source(repo):
    """Root-level source without a package dir must not select tests/ root."""
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_something.py").write_text("")
    mods = kwt._affected_pytest_modules(repo, ["run_agent.py"])
    assert mods == []


# ---------------------------------------------------------------------------
# Phase 3 — complete_task wiring (park vs done)
# ---------------------------------------------------------------------------

def _provisioned_task(conn, repo, *, title="iso task"):
    tid = kb.create_task(
        conn, title=title, assignee="coder",
        workspace_kind="dir", workspace_path=str(repo),
    )
    task = kb.claim_task(conn, tid)
    ws = kwt.provision_for_task(conn, task, str(repo))
    return tid, ws


def test_complete_task_integrates_then_done(kanban_home, repo, monkeypatch):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "feature.py", "VALUE = 2\n", msg=f"kanban({tid}): work")
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        merged_events = _events(conn, tid, "integration_merged")
        integrator_verified = _events(conn, tid, "INTEGRATOR_VERIFIED")
        release_events = _events(conn, tid, "release_gate_created")
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()
    assert task.status == "done"
    assert len(merged_events) == 1
    assert merged_events[0]["target"] == "main"
    assert len(integrator_verified) == 1
    assert release_events == []
    assert (repo / "feature.py").read_text() == "VALUE = 2\n"
    assert not ws.exists()
    receipt = [c for c in comments if c["author"] == "integrator"]
    assert receipt and merged_events[0]["merge_commit"][:12] in receipt[0]["body"]


def test_complete_task_records_artifact_preserve_receipt(
    kanban_home, repo, tmp_path, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    monkeypatch.setattr(kwt, "_artifact_receipt_timestamp", lambda: "20260621T010203Z")
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "feature.py", "VALUE = 7\n", msg=f"kanban({tid}): work")
        (ws / ".playwright-mcp").mkdir()
        (ws / ".playwright-mcp" / "console.log").write_text("[]")
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        preserved_events = _events(conn, tid, "artifact_preserved")
        merged_events = _events(conn, tid, "integration_merged")
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()

    expected_dest = tmp_path / "receipts" / f"{tid}-20260621T010203Z"
    assert task is not None
    assert task.status == "done"
    assert len(merged_events) == 1
    assert len(preserved_events) == 1
    assert preserved_events[0]["destination"] == str(expected_dest)
    assert preserved_events[0]["file_count"] == 1
    assert preserved_events[0]["paths"] == [".playwright-mcp/console.log"]
    assert (expected_dest / ".playwright-mcp" / "console.log").read_text() == "[]"
    assert any(
        c["author"] == "integrator"
        and str(expected_dest) in c["body"]
        and "1 file" in c["body"]
        and ".playwright-mcp/console.log" in c["body"]
        for c in comments
    )
    assert not ws.exists()


def test_web_integration_creates_parked_release_gate_child(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(
            ws,
            "web/src/control/App.tsx",
            "export const value = 1\n",
            msg=f"kanban({tid}): web work",
        )
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        merged_events = _events(conn, tid, "integration_merged")
        release_events = _events(conn, tid, "release_gate_created")
        child_id = release_events[0]["child_id"]
        child = kb.get_task(conn, child_id)
        parked_events = _events(conn, child_id, "release_gate_parked")
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child_id,),
        ).fetchone()[0]

    assert task.status == "done"
    assert merged_events[0]["state"] == kwt.MERGED_GREEN
    assert len(release_events) == 1
    assert child is not None
    assert child.status == "blocked"
    assert child.title == (
        "[Release-Gate] Dashboard build + runtime activation check for "
        f"{tid}"
    )
    assert parked_events[0]["state"] == kwt.GREEN_CODE_NOT_RUNTIME_ACTIVATED
    assert run_count == 0
    for command in (
        "cd /home/piet/.hermes/hermes-agent/web",
        "npm run build",
        "test -f /home/piet/.hermes/hermes-agent/hermes_cli/web_dist/index.html",
    ):
        assert command in (child.body or "")
    assert "127.0.0.1:9119" not in (child.body or "")


def test_completion_retry_reconciles_merge_after_closeout_enqueue_rollback(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    real_enqueue = kb._enqueue_closeout_in_txn

    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(
            ws,
            "web/src/control/retry.ts",
            "export const retry = true\n",
            msg=f"kanban({tid}): retryable web work",
        )

        monkeypatch.setattr(
            kb,
            "_enqueue_closeout_in_txn",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("forced closeout enqueue failure")
            ),
        )
        with pytest.raises(RuntimeError, match="forced closeout enqueue failure"):
            kb.complete_task(conn, tid, result="done")

        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running"
        assert not kwt._branch_exists(repo, f"kanban/{tid}")
        assert len(_events(conn, tid, "integration_merged")) == 1
        assert len(_events(conn, tid, "INTEGRATOR_VERIFIED")) == 1

        monkeypatch.setattr(kb, "_enqueue_closeout_in_txn", real_enqueue)
        assert kb.complete_task(conn, tid, result="done")
        assert kb.get_task(conn, tid).status == "done"
        assert len(_events(conn, tid, "integration_merged")) == 1
        assert _events(conn, tid, "integration_parked") == []
        pending = _events(conn, tid, "closeout_pending")
        assert len(pending) == 1
        assert pending[0]["release_context"]["release_gate_child_id"]


def test_completion_retry_parks_when_green_merge_was_reverted_after_rollback(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    real_enqueue = kb._enqueue_closeout_in_txn

    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(
            ws,
            "web/src/control/reverted.ts",
            "export const active = true\n",
            msg=f"kanban({tid}): web work later reverted",
        )
        monkeypatch.setattr(
            kb,
            "_enqueue_closeout_in_txn",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("forced closeout enqueue failure")
            ),
        )
        with pytest.raises(RuntimeError, match="forced closeout enqueue failure"):
            kb.complete_task(conn, tid, result="done")

        merge_commit = _events(conn, tid, "integration_merged")[0]["merge_commit"]
        _git(repo, "revert", "-m", "1", "--no-edit", merge_commit)
        monkeypatch.setattr(kb, "_enqueue_closeout_in_txn", real_enqueue)

        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "blocked"
        assert _events(conn, tid, "closeout_pending") == []
        run = kb.latest_run(conn, tid)
        assert run is not None
        assert run.metadata["content_drift_after_merge"] is True
        assert run.metadata["revert_commits"]


def test_required_web_release_gate_creation_failure_parks_completion(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    monkeypatch.setattr(
        kwt,
        "_create_parked_release_gate_child",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("release gate DB unavailable")
        ),
    )

    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(
            ws,
            "web/src/control/gated.ts",
            "export const gated = true\n",
            msg=f"kanban({tid}): gated web work",
        )
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "blocked"
        assert _events(conn, tid, "closeout_pending") == []
        merged = _events(conn, tid, "integration_merged")
        assert merged[0]["release_gate_required"] is True
        run = kb.latest_run(conn, tid)
        assert run is not None
        assert run.metadata["release_gate_creation_failed"] is True


def test_complete_task_closes_already_integrated_branch_visibly(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "feature.py", "VALUE = 22\n", msg=f"kanban({tid}): work")

    _git(repo, "merge", "--no-ff", "--no-edit", "-m", "manual merge", f"kanban/{tid}")

    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        clean_events = _events(conn, tid, "integration_clean")
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()

    assert task.status == "done"
    assert len(clean_events) == 1
    assert clean_events[0]["already_integrated"] is True
    assert "already reachable" in clean_events[0]["reason"]
    assert any(
        c["author"] == "integrator" and "already reachable" in c["body"]
        for c in comments
    )
    assert not ws.exists()


def test_complete_task_rebase_conflict_returns_to_coder(kanban_home, repo, monkeypatch):
    # B1: a conflicting integration is caught by the pre-merge rebase and routed
    # back to the coder as a REQUEST_CHANGES fix-run (NOT a dead park). The chain
    # re-enters the review loop instead of sitting blocked with an integration park.
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "a.txt", "branch version\n", msg=f"kanban({tid}): work")
    # Conflicting commit on main AFTER the claim → rebase conflict → coder re-run.
    _commit_in(repo, "a.txt", "main version\n", msg="conflicting")
    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        rebase_conflict_events = _events(conn, tid, "integration_rebase_conflict")
        returned_events = _events(conn, tid, "rebase_conflict_returned")
        parked_events = _events(conn, tid, "integration_parked")
        verdict_row = conn.execute(
            "SELECT verdict FROM task_runs WHERE task_id = ? "
            "ORDER BY ended_at DESC, id DESC LIMIT 1",
            (tid,),
        ).fetchone()
    assert task.status == "blocked"  # decision queue, not done
    # Routed to the coder, not parked: the integrator recorded a rebase-conflict
    # event and the router recorded the coder-return; no integration_parked.
    assert len(rebase_conflict_events) == 1
    assert len(returned_events) == 1
    assert "conflict" in returned_events[0]["reason"]
    assert parked_events == []
    # REQUEST_CHANGES verdict → respawn guard re-runs the coder (not suppressed
    # as 'recent_success').
    assert verdict_row is not None and verdict_row["verdict"] == "REQUEST_CHANGES"
    # Worktree + branch preserved for the coder fix.
    assert ws.exists()
    assert _git(repo, "branch", "--list", f"kanban/{tid}").strip() != ""


def test_complete_task_defers_until_chain_finished(kanban_home, repo, monkeypatch):
    """Real dispatcher flow: the unclaimed child still points at the REPO
    (it gets the worktree only at claim time) — the deferral must see it
    via chain membership (task_links), not via workspace_path equality."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
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
        ws = kwt.provision_for_task(conn, rtask, str(repo))
        _commit_in(ws, "feature.py", "VALUE = 3\n", msg="kanban(root): work")
        # Root completes while the child is still open (todo, NOT yet
        # provisioned — its workspace_path is still the repo) → defer.
        assert kb.complete_task(conn, root, result="done")
        assert _events(conn, root, "integration_merged") == []
        assert _git(repo, "log", "--merges", "--oneline") == ""
        assert ws.exists()
        # Child claims (gets the chain worktree via provisioning) and is
        # then the LAST open chain task → its completion integrates.
        kb.recompute_ready(conn)
        ctask = kb.claim_task(conn, child)
        child_ws = kwt.provision_for_task(conn, ctask, str(repo))
        assert child_ws == ws
        assert kb.complete_task(conn, child, result="done")
        assert len(_events(conn, child, "integration_merged")) == 1
    assert (repo / "feature.py").read_text() == "VALUE = 3\n"
    assert not ws.exists()


def test_child_completion_surfaces_pending_root_finalizer(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="root finalizer", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        child = kb.create_task(
            conn, title="approved child", assignee="coder", parents=[root],
            workspace_kind="dir", workspace_path=str(repo),
        )
        rtask = kb.claim_task(conn, root)
        ws = kwt.provision_for_task(conn, rtask, str(repo))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', workspace_path = ? "
                "WHERE id = ?",
                (str(ws), child),
            )
        _commit_in(ws, "feature.py", "VALUE = 30\n", msg="kanban(child): work")

        assert kb.complete_task(conn, child, result="child done")
        pending = _events(conn, root, "children_approved_pending_root_integration")
        child_task = kb.get_task(conn, child)
        root_task = kb.get_task(conn, root)

    assert child_task.status == "done"
    assert root_task.status == "running"
    assert len(pending) == 1
    assert pending[0]["completed_task_id"] == child
    assert pending[0]["branch"] == f"kanban/{root}"
    assert ws.exists()
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_missing_branch_evidence_parks_root_with_exact_reason(
    kanban_home, repo, monkeypatch,
):
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)

    _git(repo, "worktree", "remove", "--force", str(ws))
    _git(repo, "branch", "-D", f"kanban/{tid}")

    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        parked = _events(conn, tid, "integration_parked")
        blocked = _events(conn, tid, "blocked")

    assert task.status == "blocked"
    assert parked
    assert parked[-1]["reason"] == (
        f"missing branch evidence for root finalizer: kanban/{tid}"
    )
    assert "missing branch evidence" in blocked[-1]["reason"]


def test_stale_complete_does_not_integrate(kanban_home, repo, monkeypatch):
    """A stale worker (wrong expected_run_id) must stay a guaranteed no-op:
    no merge, no worktree removal — the integrator guard mirrors the
    done-UPDATE guards."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "feature.py", "VALUE = 9\n", msg=f"kanban({tid}): work")
        assert not kb.complete_task(
            conn, tid, result="stale", expected_run_id=999_999,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "running"  # untouched
        assert _events(conn, tid, "integration_merged") == []
        assert _events(conn, tid, "integration_parked") == []
    assert ws.exists()
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_review_status_complete_does_not_integrate(kanban_home, repo, monkeypatch):
    """A task parked in 'review' (verifier pending) cannot be completed —
    and the integrator must not merge the unreviewed chain either."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "feature.py", "VALUE = 8\n", msg=f"kanban({tid}): work")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL, "
                "claim_expires = NULL WHERE id = ?",
                (tid,),
            )
        assert not kb.complete_task(conn, tid, result="premature")
        assert _events(conn, tid, "integration_merged") == []
    assert ws.exists()
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_provision_preserves_subdirectory_workspace(kanban_home, repo):
    """A workspace pointing at <repo>/web keeps the /web part inside the
    worktree instead of being silently rebased to the worktree root."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="subdir task", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo / "web"),
        )
        task = kb.claim_task(conn, tid)
        ws = kwt.provision_for_task(conn, task, str(repo / "web"))
        assert kwt.is_provisioned_path(ws)
        assert ws.name == "web"
        assert ws.parent.name == tid
        row = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["workspace_path"] == str(ws)
        # Idempotent on the subdir path too.
        task = kb.get_task(conn, tid)
        assert kwt.provision_for_task(conn, task, str(ws)) == ws


def test_complete_task_non_provisioned_untouched(kanban_home, tmp_path):
    """Completion semantics for ordinary tasks must not change."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, result="done")
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert _events(conn, tid, "integration_merged") == []
        assert _events(conn, tid, "integration_parked") == []


# ---------------------------------------------------------------------------
# Spawn-resilience: WorktreeTimeout classification + env-tunable timeout + reap
# (plan 2026-06-15-001, Tasks 1-3)
# ---------------------------------------------------------------------------

def test_git_raises_worktree_timeout_on_subprocess_timeout(monkeypatch, repo):
    """Task 1: a git subprocess timeout surfaces as WorktreeTimeout (a
    WorktreeError subclass), not a raw subprocess.TimeoutExpired."""
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 120))
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    with pytest.raises(kwt.WorktreeTimeout):
        kwt._git(repo, "status")
    # Subclass contract: existing ``except WorktreeError`` handlers still catch it.
    assert issubclass(kwt.WorktreeTimeout, kwt.WorktreeError)


def test_git_timeout_follows_env_override(monkeypatch, repo):
    """Task 2: the timeout handed to subprocess.run follows
    HERMES_WORKTREE_GIT_TIMEOUT read at call time (not import time)."""
    seen = {}
    def fake_run(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setenv("HERMES_WORKTREE_GIT_TIMEOUT", "37")
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    with pytest.raises(kwt.WorktreeTimeout):
        kwt._git(repo, "status")
    assert seen["timeout"] == 37


def test_ensure_worktree_reaps_partial_on_failure(monkeypatch, repo):
    """Task 3: a failed ``worktree add`` (timeout) force-removes + prunes the
    partial/locked worktree before propagating, so no ``initializing`` leak.

    ``ensure_worktree(repo_root, root_id)`` derives wt/branch/base_branch
    internally (verified against the real :func:`kwt.ensure_worktree`
    signature), so the test fakes every ``_git`` call: ``symbolic-ref``
    (current branch), ``rev-parse`` (branch-exists), the failing
    ``worktree add``, and the reap's ``worktree remove`` / ``worktree
    prune``."""
    calls = []
    def fake_git(r, *args, check=True, timeout=None):
        calls.append(tuple(args))
        if args[:2] == ("worktree", "add"):
            raise kwt.WorktreeTimeout("simulated add timeout")
        return ""  # symbolic-ref / rev-parse / remove / prune
    monkeypatch.setattr(kwt, "_git", fake_git)
    with pytest.raises(kwt.WorktreeTimeout):
        kwt.ensure_worktree(repo, "t_reap")
    assert any(c[:4] == ("worktree", "remove", "--force", "--force") for c in calls)
    assert any(c[:2] == ("worktree", "prune") for c in calls)


# ---------------------------------------------------------------------------
# R2 — Release-gate executor: run gate, bounded coder-claude fixer (worktree-
# only) on red, escalate on persistent red. (P2-release-executor / AC2.)
# ---------------------------------------------------------------------------

def _make_release_gate_child(conn, *, root_id=None, merge_commit="abc123def456"):
    """Create a done source integration + its parked release-gate child,
    mirroring the production ``_create_parked_release_gate_child`` path.
    Returns ``(source_id, child_id, root_id)``."""
    source_id = kb.create_task(
        conn, title="web slice", assignee="coder", created_by="integrator",
    )
    # Source merged & done so the gate child can later be unblocked->done.
    assert kb.complete_task(conn, source_id, result="merged")
    root = root_id or source_id
    child_id = kwt._create_parked_release_gate_child(
        conn, source_id, root, {"merge_commit": merge_commit},
    )
    return source_id, child_id, root


def _fake_activation(*, ok=True, pre=1111, post=2222):
    """Injectable ``activation_runner`` seam for tests: mimics the real
    deploy_dashboard.sh runner's ``(ok, output, meta)`` contract with a changed
    dashboard PID (pre != post) as the 'restart happened' evidence. No real
    build/restart runs."""
    calls = []

    def _run():
        calls.append(True)
        output = "deploy_dashboard.sh: OK" if ok else "activation: deploy_dashboard.sh exit 1"
        return ok, output, {"pre_pid": pre, "post_pid": post, "deploy_exit": 0 if ok else 1}

    _run.calls = calls  # type: ignore[attr-defined]
    return _run



# ---------------------------------------------------------------------------
# S6 — end-to-end capstone: park -> clear -> auto-merge against a REAL repo
# ---------------------------------------------------------------------------

def _age_integration_retry_events(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?",
            (kb.INTEGRATION_RETRY_BACKOFF_SECONDS + 5, task_id),
        )


def test_e2e_dirty_overlap_park_clears_and_auto_merges(kanban_home, repo, monkeypatch):
    """Real incident shape: dirty overlap parks, remains blocked while dirty,
    then self-heals through the integration retry lane once the operator clears
    the checkout — no worker respawn and no operator escalation."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    spawned: list[str] = []

    def recording_spawn(task, workspace, *a, **kw):
        spawned.append(task.id)
        return None

    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "a.txt", "branch change\n", msg=f"kanban({tid}): work")
        (repo / "a.txt").write_text("manual session edit\n")

        assert kb.complete_task(conn, tid, result="done")
        parked = kb.get_task(conn, tid)
        park_events = _events(conn, tid, "integration_parked")
        assert parked.status == "blocked"
        assert park_events and "overlap" in park_events[0]["reason"]
        assert _git(repo, "log", "--merges", "--oneline") == ""
        assert (repo / "a.txt").read_text() == "manual session edit\n"
        assert ws.exists()

        # Fresh park: default backoff keeps the sweep silent and does not escalate.
        fresh = kb.no_silent_stall_sweep(conn)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kb.OPERATOR_ESCALATION_EVENT not in kinds
        assert kb.NO_SILENT_STALL_EVENT not in kinds
        assert not any(p["task_id"] == tid for p in fresh["parked"])

        # Still dirty after the backoff: retry attempts the real integrator, sees
        # the same transient park, and leaves the task blocked (never ready).
        _age_integration_retry_events(conn, tid)
        dirty_retry = kb.no_silent_stall_sweep(conn)
        still = kb.get_task(conn, tid)
        assert any(r["task_id"] == tid for r in dirty_retry["integration_retried"])
        assert still.status == "blocked"
        assert _git(repo, "log", "--merges", "--oneline") == ""

        # Operator clears the overlap. The next due sweep auto-merges through the
        # real integration path. dispatch_once is called too to prove the done
        # chain is not re-spawned as worker work.
        _git(repo, "checkout", "--", "a.txt")
        assert kwt.dirty_files(repo) == []
        _age_integration_retry_events(conn, tid)
        healed = kb.no_silent_stall_sweep(conn)
        res = kb.dispatch_once(conn, spawn_fn=recording_spawn)
        done = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        merges = _git(repo, "log", "--merges", "--oneline").splitlines()

    assert any(h["task_id"] == tid for h in healed["self_healed"])
    assert done.status == "done"
    assert len(merges) == 1
    assert (repo / "a.txt").read_text() == "branch change\n"
    assert not ws.exists()
    assert "integration_merged" in kinds
    assert "INTEGRATOR_VERIFIED" in kinds
    assert "integration_retry_succeeded" in kinds
    assert tid not in spawned
    assert res.spawned == []
    assert kb.OPERATOR_ESCALATION_EVENT not in kinds
    assert "auto_retried" not in kinds


def test_e2e_auto_merge_tick_is_idempotent(kanban_home, repo, monkeypatch):
    """After a transient integration park self-heals, subsequent sweeps/ticks are
    no-ops: no second merge, no respawn, and the task stays done."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        tid, ws = _provisioned_task(conn, repo)
        _commit_in(ws, "a.txt", "branch change\n", msg=f"kanban({tid}): work")
        (repo / "a.txt").write_text("manual session edit\n")
        assert kb.complete_task(conn, tid, result="done")
        assert kb.get_task(conn, tid).status == "blocked"

        _git(repo, "checkout", "--", "a.txt")
        _age_integration_retry_events(conn, tid)
        first = kb.no_silent_stall_sweep(conn)
        second = kb.no_silent_stall_sweep(conn)
        dispatch_second = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        done = kb.get_task(conn, tid)
        merges = _git(repo, "log", "--merges", "--oneline").splitlines()

    assert any(h["task_id"] == tid for h in first["self_healed"])
    assert not any(h["task_id"] == tid for h in second["self_healed"])
    assert not any(r["task_id"] == tid for r in second["integration_retried"])
    assert dispatch_second.spawned == []
    assert done.status == "done"
    assert len(merges) == 1


# ---------------------------------------------------------------------------
# Befund 6 (2026-07-02): dispatch-time decompose-root finalizer.
# A decomposed chain root must NEVER be spawned as a worker (a spawn re-runs the
# whole chain). The completion-side integrator only fires when the LAST child
# completes from the provisioned worktree; a chain whose last completion came
# from a scratch workspace leaves the root stranded in 'ready'. The dispatcher
# finalizes it instead.
# ---------------------------------------------------------------------------

def test_scratch_last_child_finalizes_decompose_root_at_dispatch(
    kanban_home, repo, monkeypatch,
):
    """AC-4: the live t_350e7481 pattern. A decompose chain with MIXED child
    workspaces — one child in the provisioned chain worktree
    (<repo>/.worktrees/kanban/<root>), one in a scratch workspace
    (kanban/workspaces/<id>) — where the LAST child to complete is the scratch
    child. The completion-side integrator never fires (non-provisioned path,
    the deferred/None branch), so the root strands in 'ready'. The dispatcher
    must FINALIZE it (run the existing integrator on the provisioned child's
    branch, complete the root) and NEVER spawn it or auto-assign it a lane.
    """
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    spawned = []

    def recording_spawn(task, workspace, *a, **kw):
        spawned.append(task.id)
        return 4321

    with kb.connect() as conn:
        # Real decompose root (workspace_kind=dir at the repo) fanned out via
        # decompose_triage_task, which emits the 'decomposed' event + INVERTED
        # links (parent=child, child=root) so _is_decompose_root is True. Root
        # left UNASSIGNED so that, WITHOUT the guard, the dispatcher would
        # auto-assign default_assignee and spawn it — exactly the incident.
        root = kb.create_task(
            conn, title="ship decomposed feature", triage=True,
            workspace_kind="dir", workspace_path=str(repo),
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee=None,
            children=[
                {"title": "worktree child", "assignee": "coder", "parents": []},
                {"title": "scratch child", "assignee": "coder",
                 "workspace_kind": "scratch", "parents": []},
            ],
            author="decomposer",
        )
        assert child_ids is not None
        wt_child, scratch_child = child_ids

        # Child 1 claims the provisioned chain worktree, commits, completes FIRST
        # -> integrator DEFERS (scratch sibling still open) so the branch survives
        # un-merged and the worktree stays.
        task1 = kb.claim_task(conn, wt_child)
        ws = kwt.provision_for_task(conn, task1, str(repo))
        assert kwt.split_provisioned_path(str(ws))[1] == root
        _commit_in(ws, "feature.py", "VALUE = 7\n", msg=f"kanban({wt_child}): work")
        assert kb.complete_task(conn, wt_child, result="worktree child done")
        assert _events(conn, wt_child, "integration_merged") == []
        assert ws.exists()

        # Child 2 runs in a SCRATCH workspace (the real kanban/workspaces/<id>
        # form) and completes LAST. The integrator hook sees a non-provisioned
        # path and returns None: no integration, root stranded in 'ready'.
        scratch_ws = str(kanban_home / "kanban" / "workspaces" / scratch_child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', workspace_path = ? "
                "WHERE id = ?",
                (scratch_ws, scratch_child),
            )
        assert kb.complete_task(conn, scratch_child, result="scratch child done")
        assert _events(conn, scratch_child, "integration_merged") == []
        # Bug precondition: root not yet done, worktree not yet integrated.
        assert kb.get_task(conn, root).status != "done"
        assert ws.exists()

        # Dispatcher tick: promotes root todo->ready (all children done), then the
        # guard finalizes it. default_assignee set so, without the guard, the root
        # would be auto-assigned + spawned.
        res = kb.dispatch_once(
            conn, spawn_fn=recording_spawn, default_assignee="coder",
        )

        root_task = kb.get_task(conn, root)
        root_kinds = [e.kind for e in kb.list_events(conn, root)]
        merged = _events(conn, wt_child, "integration_merged")
        merges = _git(repo, "log", "--merges", "--oneline").splitlines()

    # AC-1: never spawned, never auto-assigned.
    assert root not in spawned
    assert res.spawned == []
    assert res.auto_assigned_default == []
    assert "spawned" not in root_kinds
    # AC-2a / AC-4: the EXISTING integrator ran; the root is finalized.
    assert (root, "integrated") in res.decompose_root_finalized
    assert root_task.status == "done"
    assert len(merged) == 1
    assert len(merges) == 1
    assert (repo / "feature.py").read_text() == "VALUE = 7\n"
    assert not ws.exists()  # integrate_chain removed the worktree
    assert "decompose_root_auto_completed" in root_kinds


def test_commitless_decompose_root_direct_completes_at_dispatch(
    kanban_home, repo, monkeypatch,
):
    """AC-2b: a decompose chain where NO child was ever provisioned (all scratch,
    no chain branch). The dispatcher completes the root DIRECTLY with a
    decompose_root_auto_completed event + an evidence comment listing the
    children and their terminal status — never spawns it."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    spawned = []

    def recording_spawn(task, workspace, *a, **kw):
        spawned.append(task.id)
        return 999

    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="commitless decompose root", triage=True,
            workspace_kind="dir", workspace_path=str(repo),
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee=None,
            children=[
                {"title": "scratch A", "assignee": "coder",
                 "workspace_kind": "scratch", "parents": []},
                {"title": "scratch B", "assignee": "coder",
                 "workspace_kind": "scratch", "parents": []},
            ],
            author="decomposer",
        )
        assert child_ids is not None
        # Both children run + complete from scratch workspaces (no chain branch).
        for cid in child_ids:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'running', workspace_path = ? "
                    "WHERE id = ?",
                    (str(kanban_home / "kanban" / "workspaces" / cid), cid),
                )
            assert kb.complete_task(conn, cid, result=f"{cid} done")

        res = kb.dispatch_once(
            conn, spawn_fn=recording_spawn, default_assignee="coder",
        )

        root_task = kb.get_task(conn, root)
        root_kinds = [e.kind for e in kb.list_events(conn, root)]
        auto = _events(conn, root, "decompose_root_auto_completed")
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (root,)
        ).fetchall()
        merges = _git(repo, "log", "--merges", "--oneline").splitlines()

    # AC-1: never spawned / auto-assigned.
    assert res.spawned == []
    assert res.auto_assigned_default == []
    assert "spawned" not in root_kinds
    # AC-2b: direct completion with evidence, no integration/merge.
    assert (root, "auto_completed_commitless") in res.decompose_root_finalized
    assert root_task.status == "done"
    assert len(auto) == 1
    assert auto[0]["integration_action"] == "commitless"
    assert set(auto[0]["children"]) == set(child_ids)
    assert merges == []
    receipt = [c for c in comments if c["author"] == "integrator"]
    assert receipt and all(cid in receipt[-1]["body"] for cid in child_ids)


def test_decompose_root_with_open_child_not_finalized(
    kanban_home, repo, monkeypatch,
):
    """AC-3: a decompose root with a non-done child is neither completed nor
    spawned — the chain stays visible. Exercises the finalizer's defensive
    children_pending branch directly (recompute_ready would not promote such a
    root, so a real dispatch never reaches it)."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="incomplete decompose root", triage=True,
            workspace_kind="dir", workspace_path=str(repo),
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee=None,
            children=[
                {"title": "child A", "assignee": "coder", "parents": []},
                {"title": "child B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        child_a, child_b = child_ids
        # Only child A completes (in the provisioned worktree); child B stays open.
        task_a = kb.claim_task(conn, child_a)
        ws = kwt.provision_for_task(conn, task_a, str(repo))
        _commit_in(ws, "feature.py", "VALUE = 1\n", msg=f"kanban({child_a}): work")
        assert kb.complete_task(conn, child_a, result="child A done")
        assert kb.get_task(conn, child_b).status != "done"

        # Force the root to 'ready' (defensive: recompute_ready would not do this
        # with an open child) and finalize.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (root,)
            )
        action = kwt.finalize_decompose_root_at_dispatch(conn, root, dry_run=False)

        root_task = kb.get_task(conn, root)
        root_kinds = [e.kind for e in kb.list_events(conn, root)]

    assert action == "children_pending"
    assert root_task.status == "ready"  # untouched — stays visible on the board
    assert "decompose_root_auto_completed" not in root_kinds
    assert "spawned" not in root_kinds
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_release_gate_executor_green_path(kanban_home):
    """Gate green on first run -> real activation -> success event, no fixer,
    child done."""
    calls = []

    def fake_fixer(**kw):
        calls.append(kw)

    activation = _fake_activation()
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (True, "build ok"),
            fixer_runner=fake_fixer,
            activation_runner=activation,
        )
        executed = _events(conn, child_id, "release_gate_executed")
        fix_attempts = _events(conn, child_id, "release_gate_fix_attempt")
        activated = _events(conn, child_id, "release_gate_activated")
        child = kb.get_task(conn, child_id)

    assert result["status"] == "green"
    assert result["fixer_attempts"] == 0
    assert calls == []  # fixer never spawned
    assert activation.calls == [True]  # activation ran exactly once
    assert len(executed) == 1
    assert executed[0]["ok"] is True
    assert executed[0]["attempt"] == 0
    assert fix_attempts == []
    # green now REQUIRES a real activation: the child is done only after the
    # backend restart landed (pre != post PID) and health passed.
    assert len(activated) == 1
    assert activated[0]["pre_pid"] == 1111
    assert activated[0]["post_pid"] == 2222
    assert child.status == "done"


def test_release_gate_validates_exact_commit_in_clean_ephemeral_worktree(
    kanban_home, repo,
):
    """Foreign live WIP is absent and the detached validation ref is exact."""
    merge_commit = _git(repo, "rev-parse", "HEAD")
    foreign = repo / "foreign-wip.txt"
    foreign.write_text("not part of the release commit\n")
    validation_roots = []
    validation_heads = []

    def gate(validation_root):
        root = Path(validation_root)
        validation_roots.append(root)
        validation_heads.append(_git(root, "rev-parse", "HEAD"))
        return (not (root / "foreign-wip.txt").exists(), "clean validation")

    with kb.connect() as conn:
        _, child_id, _ = _make_release_gate_child(
            conn, merge_commit=merge_commit,
        )
        result = kwt.execute_release_gate(
            conn,
            child_id,
            gate_runner=gate,
            activation_runner=_fake_activation(),
            max_retries=0,
            repo_root=repo,
        )

    assert result["status"] == "green"
    assert validation_heads == [merge_commit]
    assert validation_roots and all(root != repo for root in validation_roots)
    assert all(not root.exists() for root in validation_roots)
    assert foreign.read_text() == "not part of the release commit\n"


def test_default_release_runner_rebinds_absolute_commands_to_validation_root(
    repo, monkeypatch,
):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=0, stdout="build ok", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: False)

    ok, _ = kwt._default_release_gate_runner(repo_root=repo)

    assert ok is True
    assert captured["cwd"] == str(repo)
    assert str(repo) in captured["argv"][2]
    assert str(kwt.LIVE_CHECKOUT_ROOT) not in captured["argv"][2]


def test_release_runner_self_heals_missing_tsc_toolchain(repo, monkeypatch):
    # The validation worktree symlinks a live node_modules that is missing the
    # lock-pinned `tsc` (typescript bumped but live never re-installed). The
    # runner must `npm ci` a fresh tree BEFORE the build instead of failing on
    # "tsc: not found" and wrongly parking the release.
    (repo / "web" / "package.json").write_text('{"name":"web"}\n')
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: False)

    ok, _ = kwt._default_release_gate_runner(repo_root=repo)

    assert ok is True
    # First subprocess call is the self-heal `npm ci`, then the build script.
    assert calls[0][-1] == "ci" and calls[0][0].endswith("npm")
    assert calls[-1][0] == "bash" and "npm run build" in calls[-1][2]


def test_release_runner_skips_self_heal_when_tsc_present(repo, monkeypatch):
    # A complete symlinked tree already resolves tsc — no npm ci, no cost.
    (repo / "web" / "package.json").write_text('{"name":"web"}\n')
    tsc = repo / "node_modules" / ".bin" / "tsc"
    tsc.parent.mkdir(parents=True)
    tsc.write_text("#!/bin/sh\n")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: False)

    ok, _ = kwt._default_release_gate_runner(repo_root=repo)

    assert ok is True
    assert not any("ci" in c for c in calls)
    assert len(calls) == 1 and calls[0][0] == "bash"


def test_release_runner_reports_self_heal_npm_ci_failure(repo, monkeypatch):
    # A failed `npm ci` must fail the gate closed (never a silent build attempt).
    (repo / "web" / "package.json").write_text('{"name":"web"}\n')
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=1, stdout="", stderr="ENOTFOUND registry")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: False)

    ok, detail = kwt._default_release_gate_runner(repo_root=repo)

    assert ok is False
    assert "self-heal" in detail and "npm ci" in detail
    # Failed closed: no build attempt after the heal failed.
    assert len(calls) == 1


def test_release_gate_refuses_activation_when_live_head_advanced(
    kanban_home, repo,
):
    validated_commit = _git(repo, "rev-parse", "HEAD")
    _commit_in(repo, "later.txt", "later\n", msg="concurrent integration")
    activation = _fake_activation()

    with kb.connect() as conn:
        _, child_id, _ = _make_release_gate_child(
            conn, merge_commit=validated_commit,
        )
        result = kwt.execute_release_gate(
            conn,
            child_id,
            gate_runner=lambda _validation_root: (True, "old commit green"),
            activation_runner=activation,
            max_retries=0,
            repo_root=repo,
        )
        child = kb.get_task(conn, child_id)
        escalations = _events(conn, child_id, kb.OPERATOR_ESCALATION_EVENT)

    assert result["status"] == "escalated"
    assert child.status == "blocked"
    assert activation.calls == []
    assert "live HEAD advanced" in escalations[0]["evidence"]["last_error"]


def test_release_activation_holds_integrator_process_and_file_locks(
    kanban_home, repo, monkeypatch,
):
    """HEAD comparison and activation share the integrator's lock boundary."""
    merge_commit = _git(repo, "rev-parse", "HEAD")
    events = []
    state = {"process": False, "file": False}
    handle = object()
    expected_lock_path = (
        Path(_git(repo, "rev-parse", "--absolute-git-dir"))
        / "hermes-kanban-integrator.lock"
    )

    class RecordingProcessLock:
        def __enter__(self):
            assert not state["process"]
            state["process"] = True
            events.append("process_enter")

        def __exit__(self, exc_type, exc, tb):
            assert state["process"] and not state["file"]
            events.append("process_exit")
            state["process"] = False

    def acquire_file_lock(path, timeout=kwt.LOCK_TIMEOUT_SECONDS):
        assert state["process"] and not state["file"]
        assert Path(path) == expected_lock_path
        state["file"] = True
        events.append("file_acquire")
        return handle

    def release_file_lock(actual_handle):
        assert actual_handle is handle
        assert state["process"] and state["file"]
        events.append("file_release")
        state["file"] = False

    def activation():
        assert state == {"process": True, "file": True}
        events.append("activation")
        return True, "activated", {"pre_pid": 31, "post_pid": 32}

    monkeypatch.setattr(kwt, "_PROCESS_LOCK", RecordingProcessLock())
    monkeypatch.setattr(kwt, "_acquire_file_lock", acquire_file_lock)
    monkeypatch.setattr(kwt, "_release_file_lock", release_file_lock)

    with kb.connect() as conn:
        _, child_id, _ = _make_release_gate_child(
            conn, merge_commit=merge_commit,
        )
        result = kwt.execute_release_gate(
            conn,
            child_id,
            gate_runner=lambda _validation_root: (True, "green"),
            activation_runner=activation,
            max_retries=0,
            repo_root=repo,
        )

    assert result["status"] == "green"
    assert events == [
        "process_enter",
        "file_acquire",
        "activation",
        "file_release",
        "process_exit",
    ]


def test_release_gate_validation_worktree_cleans_up_when_runner_crashes(
    kanban_home, repo,
):
    merge_commit = _git(repo, "rev-parse", "HEAD")
    validation_roots = []

    def crashing_gate(validation_root):
        validation_roots.append(Path(validation_root))
        raise RuntimeError("gate exploded")

    with kb.connect() as conn:
        _, child_id, _ = _make_release_gate_child(
            conn, merge_commit=merge_commit,
        )
        result = kwt.execute_release_gate(
            conn,
            child_id,
            gate_runner=crashing_gate,
            max_retries=0,
            repo_root=repo,
        )

    assert result["status"] == "escalated"
    assert validation_roots and all(not root.exists() for root in validation_roots)
    assert "kanban-validation" not in _git(repo, "worktree", "list", "--porcelain")


def test_release_fixer_commit_is_integrated_and_revalidated_before_activation(
    kanban_home, repo,
):
    merge_commit = _git(repo, "rev-parse", "HEAD")
    validation_roots = []
    activation_heads = []

    def gate(validation_root):
        root = Path(validation_root)
        validation_roots.append(root)
        ok = (root / "release-fix.txt").is_file()
        return ok, "fixed" if ok else "missing release fix"

    def fixer(**kwargs):
        info = kwt.ensure_worktree(repo, kwargs["root_id"])
        assert info["path"] == kwargs["worktree"]
        _commit_in(
            info["path"],
            "release-fix.txt",
            "fixed\n",
            msg="release fixer commit",
        )

    def activation():
        assert (repo / "release-fix.txt").read_text() == "fixed\n"
        activation_heads.append(_git(repo, "rev-parse", "HEAD"))
        return True, "activated", {"pre_pid": 11, "post_pid": 22}

    with kb.connect() as conn:
        _, child_id, _ = _make_release_gate_child(
            conn, merge_commit=merge_commit,
        )
        result = kwt.execute_release_gate(
            conn,
            child_id,
            gate_runner=gate,
            fixer_runner=fixer,
            activation_runner=activation,
            max_retries=1,
            repo_root=repo,
        )
        executed = _events(conn, child_id, "release_gate_executed")

    assert result["status"] == "green"
    assert result["fixer_attempts"] == 1
    assert activation_heads == [_git(repo, "rev-parse", "HEAD")]
    assert [event.get("phase") for event in executed] == [
        "merged_commit",
        "fixer_branch",
        "integrated_fixer_commit",
    ]
    assert executed[-1]["validation_commit"] == activation_heads[0]
    assert all(not root.exists() for root in validation_roots)
    assert "kanban/" not in _git(repo, "branch", "--list", "kanban/*")


def test_release_fixer_failed_integration_stays_blocked_without_activation(
    kanban_home, repo,
):
    merge_commit = _git(repo, "rev-parse", "HEAD")
    activation = _fake_activation()
    validation_roots = []

    def gate(validation_root):
        root = Path(validation_root)
        validation_roots.append(root)
        ok = (root / "a.txt").read_text() == "fixed on branch\n"
        return ok, "fixed" if ok else "still broken"

    def fixer(**kwargs):
        info = kwt.ensure_worktree(repo, kwargs["root_id"])
        _commit_in(
            info["path"], "a.txt", "fixed on branch\n", msg="release fix",
        )
        # Simulate concurrent operator WIP on the same live path. The existing
        # overlap guard must park before merge; activation must remain impossible.
        (repo / "a.txt").write_text("operator WIP\n")

    with kb.connect() as conn:
        _, child_id, _ = _make_release_gate_child(
            conn, merge_commit=merge_commit,
        )
        result = kwt.execute_release_gate(
            conn,
            child_id,
            gate_runner=gate,
            fixer_runner=fixer,
            activation_runner=activation,
            max_retries=1,
            repo_root=repo,
        )
        child = kb.get_task(conn, child_id)
        escalations = _events(conn, child_id, kb.OPERATOR_ESCALATION_EVENT)

    assert result["status"] == "escalated"
    assert child.status == "blocked"
    assert activation.calls == []
    assert _git(repo, "rev-parse", "HEAD") == merge_commit
    assert (repo / "a.txt").read_text() == "operator WIP\n"
    assert "integration failed" in escalations[0]["evidence"]["last_error"]
    assert all(not root.exists() for root in validation_roots)


def test_release_gate_executor_red_then_fixer_then_green(kanban_home):
    """Gate red -> one bounded fixer in the chain worktree -> re-run green."""
    gate_results = iter([(False, "TS2322 build error"), (True, "build ok")])
    fixer_calls = []

    def fake_gate():
        return next(gate_results)

    def fake_fixer(**kw):
        fixer_calls.append(kw)

    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=fake_gate,
            fixer_runner=fake_fixer,
            activation_runner=_fake_activation(),
            max_retries=2,
        )
        executed = _events(conn, child_id, "release_gate_executed")
        fix_attempts = _events(conn, child_id, "release_gate_fix_attempt")
        activated = _events(conn, child_id, "release_gate_activated")
        child = kb.get_task(conn, child_id)

    assert result["status"] == "green"
    assert result["fixer_attempts"] == 1
    assert len(activated) == 1  # activation runs once, after the code went green
    # exactly one fixer spawn, on the chain worktree/branch (NOT live-main)
    assert len(fixer_calls) == 1
    fc = fixer_calls[0]
    assert fc["root_id"] == root
    assert fc["branch"] == kwt.chain_branch(root)
    assert kwt.split_provisioned_path(fc["worktree"]) is not None
    assert "TS2322" in fc["gate_error"]  # fixer reads the gate error
    # event trail: attempt-0 red + attempt-1 green, plus one fix attempt
    assert [e["ok"] for e in executed] == [False, True]
    assert len(fix_attempts) == 1
    assert fix_attempts[0]["attempt"] == 1
    assert child.status == "done"


def test_release_gate_executor_persistent_red_escalates(kanban_home):
    """Gate stays red after the retry budget -> operator_escalation, child
    stays blocked, fixer ran exactly max_retries times."""
    fixer_calls = []

    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (False, "still broken"),
            fixer_runner=lambda **kw: fixer_calls.append(kw),
            max_retries=2,
        )
        executed = _events(conn, child_id, "release_gate_executed")
        fix_attempts = _events(conn, child_id, "release_gate_fix_attempt")
        escalations = _events(conn, child_id, kb.OPERATOR_ESCALATION_EVENT)
        child = kb.get_task(conn, child_id)

    assert result["status"] == "escalated"
    assert result["fixer_attempts"] == 2
    assert len(fixer_calls) == 2
    # attempt-0 + 2 re-runs = 3 executed events, all red; 2 fix attempts
    assert [e["ok"] for e in executed] == [False, False, False]
    assert len(fix_attempts) == 2
    assert len(escalations) == 1
    assert escalations[0]["attempts_already_made"] == 2
    assert "still broken" in escalations[0]["evidence"]["last_error"]
    assert child.status == "blocked"


def test_release_gate_escalation_writes_inline_heiler_classification(kanban_home):
    """ESCALATION-INLINE-CLASSIFY-S1 (defense-in-depth): persistent-red release
    gate classifies atomically AT the escalation site. Exactly one
    heiler_classification, referencing the escalation event, tagged with the
    inline release-gate source, with a belegter (signal-source) evidence
    reference (AC-2). No classify_escalations_sweep poll required, and the sweep
    then adds nothing because the escalation is already paired."""
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (False, "tests failed: AssertionError"),
            fixer_runner=lambda **kw: None,
            max_retries=0,
        )
        events = kb.list_events(conn, child_id)
        escalations = [
            e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heilers = [
            e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]
        # pre-existing safety net must be a no-op now that we classify inline
        summary = kb.classify_escalations_sweep(conn)

    assert result["status"] == "escalated"
    assert len(escalations) == 1
    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == escalations[0].id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_RELEASE_GATE
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert heilers[0].payload["blocked"] is True
    assert heilers[0].payload["evidence"].get("signal_source")
    assert summary["classified"] == []


def test_release_gate_trigger_outcome_helper():
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1 (pure): the structural failure-mode
    label reads our own runner sentinels as infra (transient) and treats every
    other red gate — incl. empty output — as a candidate defect (real-bug)."""
    assert kwt._release_gate_trigger_outcome("") == "release_gate_red"
    assert kwt._release_gate_trigger_outcome("still broken") == "release_gate_red"
    assert (
        kwt._release_gate_trigger_outcome("visual-gate: scrollWidth exceeds")
        == "release_gate_red"
    )
    assert (
        kwt._release_gate_trigger_outcome("release-gate timed out after 1800s")
        == "release_gate_infra"
    )
    assert (
        kwt._release_gate_trigger_outcome("release-gate command error: boom")
        == "release_gate_infra"
    )


def test_release_gate_blind_escalation_classifies_real_bug(kanban_home):
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1 (AC-1): a persistent-red gate whose
    output carries NO free-text signal (the live blind case: opaque / visual-gate /
    empty ``last_error``) now escalates with a structural ``trigger_outcome`` and
    its paired heiler_classification lands in the real cause class (real-bug),
    OUT of the unclassified bucket that drove up unclassified_share."""
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (False, "visual-gate: scrollWidth exceeds viewport"),
            fixer_runner=lambda **kw: None,
            max_retries=0,
        )
        events = kb.list_events(conn, child_id)
        escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
        heilers = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]

    assert result["status"] == "escalated"
    assert len(escalations) == 1
    assert escalations[0].payload["evidence"]["trigger_outcome"] == "release_gate_red"
    assert len(heilers) == 1
    assert heilers[0].payload["class"] == kb.HEILER_CLASS_REAL_BUG
    assert heilers[0].payload["class"] != kb.HEILER_CLASS_UNCLASSIFIED


def test_release_gate_infra_escalation_classifies_transient(kanban_home):
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: a gate the runner could not
    complete (timeout sentinel) escalates as infra → transient, not real-bug —
    the failure mode, not the wording, drives the class."""
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (False, "release-gate timed out after 1800s"),
            fixer_runner=lambda **kw: None,
            max_retries=0,
        )
        events = kb.list_events(conn, child_id)
        escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
        heilers = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]

    assert result["status"] == "escalated"
    assert escalations[0].payload["evidence"]["trigger_outcome"] == "release_gate_infra"
    assert heilers[0].payload["class"] == kb.HEILER_CLASS_TRANSIENT


def test_release_gate_enrichment_leaves_escalation_behavior_unchanged(kanban_home):
    """AC-2 guardrail: pure context enrichment — the secondary action is
    unchanged. Exactly one escalation (count does not rise), the child stays
    blocked, and the fixer ran the full retry budget, same as before the
    trigger_outcome was added."""
    fixer_calls = []
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (False, "still broken"),
            fixer_runner=lambda **kw: fixer_calls.append(kw),
            max_retries=2,
        )
        escalations = _events(conn, child_id, kb.OPERATOR_ESCALATION_EVENT)
        child = kb.get_task(conn, child_id)

    assert result["status"] == "escalated"
    assert result["fixer_attempts"] == 2
    assert len(fixer_calls) == 2
    assert len(escalations) == 1  # count did not rise
    assert child.status == "blocked"  # block decision unchanged


def test_release_gate_executor_max_retries_zero_immediate_escalation(kanban_home):
    """max_retries=0 -> red gate escalates immediately, no fixer."""
    fixer_calls = []

    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (False, "broken"),
            fixer_runner=lambda **kw: fixer_calls.append(kw),
            max_retries=0,
        )
        escalations = _events(conn, child_id, kb.OPERATOR_ESCALATION_EVENT)

    assert result["status"] == "escalated"
    assert fixer_calls == []
    assert len(escalations) == 1


def test_release_gate_backend_change_activates_and_greens(kanban_home):
    """S1 capstone (AC-2/AC-3/AC-5): a green code gate does NOT finish on 'builds'
    — it drives a REAL runtime activation (backend restart) and only then greens
    the child. The reproducible 'backend change -> restart happened -> child green'
    path: the activation seam records that it ran, a release_gate_activated event
    carries the changed :9119 PID (restart evidence), and the child is done."""
    activation = _fake_activation(pre=4242, post=9191)
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (True, "npm build ok"),
            fixer_runner=lambda **kw: None,
            activation_runner=activation,
        )
        activated = _events(conn, child_id, "release_gate_activated")
        child = kb.get_task(conn, child_id)

    assert result["status"] == "green"
    assert result["activation"]["pre_pid"] == 4242
    assert result["activation"]["post_pid"] == 9191
    # activation ran exactly once, AFTER the code gate proved green
    assert activation.calls == [True]
    assert len(activated) == 1
    assert activated[0]["ok"] is True
    assert activated[0]["root_id"] == root
    assert activated[0]["pre_pid"] != activated[0]["post_pid"]  # restart took
    # the child is deterministically done — the result survives the (simulated)
    # restart because the writer is the activation process, not a dying request
    assert child.status == "done"


def test_release_gate_activation_failure_escalates_and_keeps_child_blocked(kanban_home):
    """AC-4: green CODE but a failed runtime activation (backend restart / health)
    escalates to the operator and keeps the child BLOCKED — a green build is never
    silently reported as activated. The escalation carries the activation error and
    an activation_failed event, distinct from a persistent-red gate escalation."""
    def failing_activation():
        return (
            False,
            "activation: dashboard not running after restart (no MainPID)",
            {"pre_pid": 100, "post_pid": None, "deploy_exit": 0},
        )

    fixer_calls = []
    with kb.connect() as conn:
        _, child_id, root = _make_release_gate_child(conn)
        result = kwt.execute_release_gate(
            conn, child_id,
            gate_runner=lambda: (True, "build ok"),
            fixer_runner=lambda **kw: fixer_calls.append(kw),
            activation_runner=failing_activation,
        )
        activated = _events(conn, child_id, "release_gate_activated")
        activation_failed = _events(conn, child_id, "release_gate_activation_failed")
        escalations = _events(conn, child_id, kb.OPERATOR_ESCALATION_EVENT)
        child = kb.get_task(conn, child_id)

    assert result["status"] == "escalated"
    # code was green on attempt 0, so the bounded fixer budget was never spent
    assert fixer_calls == []
    assert result["fixer_attempts"] == 0
    assert activated == []  # no success event
    assert len(activation_failed) == 1
    assert activation_failed[0]["ok"] is False
    assert len(escalations) == 1
    assert "activation" in escalations[0]["evidence"]["last_error"].lower()
    assert child.status == "blocked"


def test_default_release_gate_activation_runs_deploy_and_verifies_new_pid(
    kanban_home, monkeypatch, tmp_path,
):
    """AC-1: the default activation runner invokes deploy_dashboard.sh and treats
    a deploy exit 0 WITH a changed dashboard PID (a genuinely new :9119 process)
    as success, surfacing the pre/post PIDs as evidence."""
    deploy = tmp_path / "deploy_dashboard.sh"
    deploy.write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.setattr(kwt, "DEPLOY_SCRIPT", deploy)

    ran = {}

    def fake_run(argv, **kwargs):
        ran["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="[deploy] OK", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    pids = iter([54321, 67890])  # pre, then post — a real restart forks a new PID
    monkeypatch.setattr(kwt, "_dashboard_service_pid", lambda: next(pids))

    ok, output, meta = kwt._default_release_gate_activation()

    assert ok is True
    assert ran["argv"][0] == "bash"
    assert ran["argv"][1] == str(deploy)  # canonical deploy script, not a bare build
    assert meta == {"pre_pid": 54321, "post_pid": 67890, "deploy_exit": 0}


def test_default_release_gate_activation_pid_unchanged_is_failure(
    kanban_home, monkeypatch, tmp_path,
):
    """A deploy that exits 0 but leaves the dashboard PID unchanged means the
    restart did not take (stale backend) → activation FAILS, so the child never
    greens on a build-only run that skipped the real restart (AC-2 safeguard)."""
    deploy = tmp_path / "deploy_dashboard.sh"
    deploy.write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.setattr(kwt, "DEPLOY_SCRIPT", deploy)
    monkeypatch.setattr(
        kwt.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(kwt, "_dashboard_service_pid", lambda: 4444)  # same pre==post

    ok, output, meta = kwt._default_release_gate_activation()

    assert ok is False
    assert "unchanged" in output.lower()
    assert meta["pre_pid"] == meta["post_pid"] == 4444


def test_spawn_release_gate_activation_launches_detached_transient_unit():
    """AC-3/AC-5: the endpoint/auto path launches the activation as a systemd
    --user transient unit (its own cgroup → survives the restart it triggers)
    running the SAME `hermes kanban release-gate` core the CLI runs, threading the
    board through. The systemd-run launcher is injected so nothing really starts."""
    captured = {}

    def fake_runner(argv, **kwargs):
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = kwt.spawn_release_gate_activation(
        "t_gate/9", board="ops", runner=fake_runner, hermes_bin="/opt/hermes",
    )

    argv = captured["argv"]
    assert result["ok"] is True
    assert argv[0].endswith("systemd-run")
    assert "--user" in argv and "--collect" in argv
    # sanitized, stable unit name (dedup guard) — no '/' in a unit name
    assert "--unit=hermes-release-gate-t_gate_9" in argv
    # the detached command IS the shared CLI activation core, board-scoped. The
    # global --board MUST sit before the subcommand or argparse rejects it.
    # --inline: the detached unit runs the gate core directly (no nested spawn).
    tail = argv[argv.index("/opt/hermes"):]
    assert tail == ["/opt/hermes", "kanban", "--board", "ops",
                    "release-gate", "t_gate/9", "--inline", "--json"]
    # Regression guard: the emitted CLI portion must actually parse (a --board
    # after the subcommand would raise "unrecognized arguments").
    import argparse
    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="cmd"))
    ns = parser.parse_args(tail[1:])  # drop the hermes binary path
    assert ns.task_id == "t_gate/9" and ns.board == "ops"
    assert ns.inline is True  # the detached unit must run inline (no recursion)
    # PATH is passed through so npm/node/git/systemctl resolve in the clean unit env
    assert any(a.startswith("--setenv=PATH=") for a in argv)


def test_spawn_release_gate_activation_reports_launch_failure():
    """A launcher that fails (e.g. a unit of the same name already running — the
    dedup guard) is reported as ok=False, not silently swallowed."""
    def fake_runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Unit already exists.")

    result = kwt.spawn_release_gate_activation("t_gate", runner=fake_runner)

    assert result["ok"] is False
    assert "already exists" in result["detail"].lower()


def test_auto_mode_release_gate_child_launches_board_scoped_activation(
    kanban_home, monkeypatch,
):
    """AC-5 regression: ``release_gate.mode:auto`` on a NON-default board must
    launch the SAME detached activation the CLI/endpoint use, scoped to the child's
    board. A detached ``systemd-run --user`` unit does NOT inherit the caller's
    ``HERMES_KANBAN_BOARD``/``HERMES_KANBAN_DB`` (spawn only forwards PATH/HERMES_HOME/
    bus vars), so a board=None launch would resolve ``<root>/kanban/current`` and
    green the WRONG board's child. The auto path must therefore derive the board
    from the integration connection and emit
    ``hermes kanban --board <slug> release-gate <child> --json`` (global --board
    BEFORE the subcommand)."""
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "auto")
    monkeypatch.setenv("HERMES_BIN", "/opt/hermes")
    captured = {}

    def fake_run(argv, **kwargs):  # the injected systemd-run launcher
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    # The child lives on the non-default board "ops"; the conn's DB path is what
    # the auto path must reverse-map to a --board slug.
    with kb.connect(board="ops") as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, {"merge_commit": "deadbeefcafe"},
        )
        assert _events(conn, child_id, "release_gate_auto_execute_started") == []
        assert kwt.start_parked_release_gate(conn, child_id) == "started"
        started = _events(conn, child_id, "release_gate_auto_execute_started")
        failed = _events(conn, child_id, "release_gate_auto_execute_failed")

    assert child_id is not None
    # auto-execute fired and the detached launcher reported success (no fail event)
    assert len(started) == 1
    assert failed == []
    argv = captured["argv"]
    assert argv[0].endswith("systemd-run")
    assert "--user" in argv
    # board-scoped, same shape the CLI/endpoint path is asserted to build:
    # `hermes kanban --board ops release-gate <child> --inline --json`
    # (--inline: the detached unit must NOT spawn another unit — see spawn.)
    i = argv.index("release-gate")
    assert argv[i - 3:i + 4] == [
        "kanban", "--board", "ops", "release-gate", child_id, "--inline", "--json",
    ]
    # never launches against the default board when the child is on "ops"
    assert argv[i - 1] != "default"


def test_autonomous_switch_auto_executes_parked_release_gate(
    kanban_home, monkeypatch,
):
    """AD-S2: with the global ``release.autonomous`` switch ON and every guard
    green (freigabe:complete root, standard tier, no redesign), the just-parked
    gate is auto-executed via the SAME detached activation — no new deploy path.
    The started event is labelled ``mode: autonomous`` and the launcher argv is
    the same ``systemd-run … release-gate <child> --inline --json`` shape."""
    from hermes_cli import auto_release

    # global switch ON; release_gate.mode stays manual so ONLY the AD-S2 hook
    # (not the legacy mode:auto path) can fire.
    monkeypatch.setattr(
        auto_release, "_release_config",
        lambda: {"autonomous": True, "max_tier_autonomous": "review"},
    )
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "manual")
    monkeypatch.setenv("HERMES_BIN", "/opt/hermes")
    captured = {}

    def fake_run(argv, **kwargs):  # the injected systemd-run launcher
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
            freigabe="complete",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, {"merge_commit": "deadbeefcafe"},
        )
        assert captured == {}
        assert _events(conn, child_id, "release_gate_auto_execute_started") == []
        assert kwt.start_parked_release_gate(conn, child_id) == "started"
        started = _events(conn, child_id, "release_gate_auto_execute_started")
        failed = _events(conn, child_id, "release_gate_auto_execute_failed")
        held = _events(conn, child_id, "release_gate_auto_execute_held")

    assert child_id is not None
    assert len(started) == 1
    assert started[0]["mode"] == "autonomous"
    assert failed == []
    assert held == []
    argv = captured["argv"]
    assert argv[0].endswith("systemd-run")
    i = argv.index("release-gate")
    assert argv[i:i + 3] == ["release-gate", child_id, "--inline"]
    assert "--json" in argv[i:]


def test_parked_gate_creation_never_starts_release_before_closeout(
    kanban_home, monkeypatch,
):
    """Gate creation is DB-only; closeout owns every external release start."""
    from hermes_cli import auto_release

    monkeypatch.setattr(
        auto_release, "_release_config",
        lambda: {"autonomous": True, "max_tier_autonomous": "review"},
    )
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "manual")
    monkeypatch.setenv("HERMES_BIN", "/opt/hermes")
    calls = []
    monkeypatch.setattr(kwt.subprocess, "run", lambda *a, **kw: calls.append(a))

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
            freigabe="complete",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        outcome = {"merge_commit": "deadbeefcafe"}
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, outcome,
        )

    assert child_id is not None
    assert outcome.get("release_gate_child_id") == child_id
    assert "release_gate_auto_executed" not in outcome
    assert calls == []
    with kb.connect() as conn:
        assert _events(conn, child_id, "release_gate_auto_execute_started") == []


def test_mode_auto_starts_only_when_closeout_invokes_parked_gate(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "auto")
    monkeypatch.setenv("HERMES_BIN", "/opt/hermes")
    monkeypatch.setattr(
        kwt.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        outcome = {"merge_commit": "deadbeefcafe"}
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, outcome,
        )
        assert _events(conn, child_id, "release_gate_auto_execute_started") == []
        assert kwt.start_parked_release_gate(conn, child_id) == "started"

    assert child_id is not None
    assert outcome.get("release_gate_child_id") == child_id
    assert "release_gate_auto_executed" not in outcome


def test_held_gate_leaves_mutual_exclusion_flag_unset(kanban_home, monkeypatch):
    """Kill-switch OFF: the gate stays parked and the ``outcome`` is NOT flagged,
    so ``complete_task`` still reaches ``maybe_auto_release`` unchanged — the fix
    only suppresses the SECOND deploy, it never creates a deploy gap."""
    from hermes_cli import auto_release

    monkeypatch.setattr(
        auto_release, "_release_config",
        lambda: {"autonomous": False, "max_tier_autonomous": "review"},
    )
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "manual")
    monkeypatch.setattr(
        kwt.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
            freigabe="complete",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        outcome = {"merge_commit": "deadbeefcafe"}
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, outcome,
        )

    assert child_id is not None
    assert "release_gate_auto_executed" not in outcome


def test_failed_gate_spawn_leaves_mutual_exclusion_flag_unset(
    kanban_home, monkeypatch,
):
    """A failed acknowledgement is ambiguous and never authorizes fallback."""
    from hermes_cli import auto_release

    monkeypatch.setattr(
        auto_release, "_release_config",
        lambda: {"autonomous": True, "max_tier_autonomous": "review"},
    )
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "manual")
    # The guards pass (autonomous + freigabe:complete) so the hook TRIES to
    # spawn, but the launch itself fails.
    monkeypatch.setattr(
        kwt, "spawn_release_gate_activation",
        lambda *a, **k: {"ok": False, "detail": "systemd-run failed"},
    )

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
            freigabe="complete",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        outcome = {"merge_commit": "deadbeefcafe"}
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, outcome,
        )
        assert _events(conn, child_id, "release_gate_auto_execute_failed") == []
        state = kwt.start_parked_release_gate(conn, child_id)
        failed = _events(conn, child_id, "release_gate_auto_execute_failed")

    assert child_id is not None
    assert state == "ambiguous"
    assert "release_gate_auto_executed" not in outcome
    assert len(failed) == 1


def test_autonomous_switch_off_parks_release_gate_byte_exact(
    kanban_home, monkeypatch,
):
    """AD-S2 AC-3: ``release.autonomous`` false parks EXACTLY as today — the
    child is blocked with a ``release_gate_parked`` event, no activation is
    spawned, and NO new auto-execute/held event is written (silent kill-switch)."""
    from hermes_cli import auto_release

    monkeypatch.setattr(
        auto_release, "_release_config",
        lambda: {"autonomous": False, "max_tier_autonomous": "review"},
    )
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "manual")
    ran = {"called": False}

    def fake_run(argv, **kwargs):  # must never fire when the switch is off
        ran["called"] = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
            freigabe="complete",
        )
        assert kb.complete_task(conn, source_id, result="merged")
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, {"merge_commit": "deadbeefcafe"},
        )
        child = kb.get_task(conn, child_id)
        parked = _events(conn, child_id, "release_gate_parked")
        started = _events(conn, child_id, "release_gate_auto_execute_started")
        held = _events(conn, child_id, "release_gate_auto_execute_held")
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child_id,),
        ).fetchone()[0]

    assert child is not None
    assert child.status == "blocked"
    assert len(parked) == 1
    assert parked[0]["state"] == kwt.GREEN_CODE_NOT_RUNTIME_ACTIVATED
    assert started == []          # no auto-execution
    assert held == []             # kill-switch-off is silent (byte-exact)
    assert run_count == 0
    assert ran["called"] is False  # no detached activation spawned


def test_autonomous_switch_holds_and_logs_when_guard_red(
    kanban_home, monkeypatch,
):
    """AD-S2 AC-2: switch ON but a guard held (freigabe:operator) parks the child
    AND records an additive ``release_gate_auto_execute_held`` audit event — the
    activation is never spawned."""
    from hermes_cli import auto_release

    monkeypatch.setattr(
        auto_release, "_release_config",
        lambda: {"autonomous": True, "max_tier_autonomous": "review"},
    )
    monkeypatch.setattr(kwt, "release_gate_mode", lambda: "manual")
    ran = {"called": False}

    def fake_run(argv, **kwargs):
        ran["called"] = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn, title="web slice", assignee="coder", created_by="integrator",
            freigabe="operator",  # operator-gated chain never auto-releases
        )
        assert kb.complete_task(conn, source_id, result="merged")
        child_id = kwt._create_parked_release_gate_child(
            conn, source_id, source_id, {"merge_commit": "deadbeefcafe"},
        )
        assert kwt.start_parked_release_gate(conn, child_id) == "held"
        started = _events(conn, child_id, "release_gate_auto_execute_started")
        held = _events(conn, child_id, "release_gate_auto_execute_held")

    assert started == []
    assert len(held) == 1
    assert held[0]["outcome"] == "held_no_freigabe"
    assert ran["called"] is False


def test_release_gate_executor_rejects_non_gate_task(kanban_home):
    """A task without a release_gate_parked event is not a gate child."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="not a gate", assignee="coder")
        with pytest.raises(kwt.ReleaseGateError):
            kwt.execute_release_gate(
                conn, tid, gate_runner=lambda: (True, ""),
            )


def test_resolve_fixer_worktree_is_isolated(tmp_path):
    """The fixer worktree is always under <repo>/.worktrees/kanban/<root>,
    never the live checkout root itself."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wt, branch = kwt._resolve_fixer_worktree("t_root", repo_root=repo)
    assert wt == repo / kwt.WORKTREES_DIRNAME / kwt.WORKTREES_NAMESPACE / "t_root"
    assert branch == kwt.chain_branch("t_root")
    assert wt.resolve() != repo.resolve()
    assert kwt.split_provisioned_path(wt) is not None


def test_release_gate_fixer_spawn_isolates_mcp(monkeypatch, tmp_path):
    """disposition-di_109b5a17-S1: the release-gate fixer claude-cli spawn pins
    --strict-mcp-config so no external MCP servers (vault qmd, @playwright/mcp
    headless chromium) load. Those server child processes keep the Node event
    loop alive, so ``claude -p`` cannot exit after its turn — the post-commit
    ep_poll idle hang (0-byte json log, slot + token stream pinned)."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    kwt._spawn_release_gate_fixer_process(
        worktree=wt, branch="wt/t_root", prompt="fix it",
        task_id="t_child", root_id="t_root",
    )
    assert "--strict-mcp-config" in captured["cmd"], captured["cmd"]
    # Empty-server-set form: no companion --mcp-config smuggling servers back in.
    assert "--mcp-config" not in captured["cmd"], captured["cmd"]


def test_default_fixer_provisions_isolated_worktree_not_live(
    kanban_home, repo, monkeypatch,
):
    """The default fixer recreates the chain worktree and spawns the
    coder-claude process with cwd = the isolated worktree, NEVER the live
    checkout. The live repo's main working tree is left untouched."""
    spawned = {}

    def fake_spawn(*, worktree, branch, prompt, task_id, root_id):
        spawned["worktree"] = Path(worktree)
        spawned["branch"] = branch
        spawned["cwd_is_repo_root"] = Path(worktree).resolve() == repo.resolve()

    monkeypatch.setattr(kwt, "_spawn_release_gate_fixer_process", fake_spawn)

    before = _git(repo, "rev-parse", "HEAD")
    kwt._default_release_gate_fixer(
        worktree=repo / kwt.WORKTREES_DIRNAME / kwt.WORKTREES_NAMESPACE / "t_root",
        branch=kwt.chain_branch("t_root"),
        gate_error="boom",
        attempt=1,
        task_id="t_child",
        root_id="t_root",
        repo_root=repo,
    )
    after = _git(repo, "rev-parse", "HEAD")

    # worktree was actually created, isolated, on the chain branch
    wt = repo / kwt.WORKTREES_DIRNAME / kwt.WORKTREES_NAMESPACE / "t_root"
    assert (wt / ".git").exists()
    assert spawned["cwd_is_repo_root"] is False
    assert spawned["worktree"].resolve() == wt.resolve()
    assert spawned["branch"] == kwt.chain_branch("t_root")
    # live checkout main branch untouched (no commits, branch unchanged)
    assert before == after
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_release_gate_fixer_max_retries_config(kanban_home, monkeypatch):
    """Env override > config.yaml key > default 2."""
    import yaml
    from hermes_constants import get_default_hermes_root

    # default when nothing set
    monkeypatch.delenv("HERMES_RELEASE_GATE_FIXER_MAX_RETRIES", raising=False)
    assert kwt.release_gate_fixer_max_retries() == 2

    # config.yaml value
    cfg_path = get_default_hermes_root() / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"kanban": {"release_gate_fixer_max_retries": 4}}))
    assert kwt.release_gate_fixer_max_retries() == 4

    # env wins over config
    monkeypatch.setenv("HERMES_RELEASE_GATE_FIXER_MAX_RETRIES", "1")
    assert kwt.release_gate_fixer_max_retries() == 1


def _red_gate_ruff(_repo, _files):
    """Stub mimicking a real ruff failure. ruff lints ONLY the chain's own
    diff .py files (default_quick_gate's _changed_py), so a red ruff stage is
    chain fault by construction."""
    return False, "ruff: exit 1\nfeature.py:1:1: F821 undefined name"


def _red_gate_vitest_missing(_repo, _files):
    """Stub of the fail-closed missing-binary message whose label collides
    with the vitest[control] stage prefix (real string from
    default_quick_gate)."""
    return False, ("vitest[control]: web/ in diff but "
                   "vitest not found in web/ or root node_modules/.bin")


def test_ruff_failure_never_foreign_attributed(repo):
    """Codex review finding 1 (2026-07-06): a foreign dirty .py nearby must
    NOT exculpate a red ruff stage — ruff never reads non-diff files."""
    info = _provisioned_chain(repo, "t_ruff_own_fault", relpath="feature.py")
    foreign = repo / "foreign_wip.py"
    foreign.write_text("this is not python\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate_ruff)
    assert out["action"] == "parked"
    assert out.get("park_class") != kwt.FOREIGN_DIRTY_CHECKOUT_CLASS
    assert out["reason"].startswith("post-merge gate failed")


def test_missing_binary_failure_never_foreign_attributed(repo):
    """Codex finding 2 follow-up: only a real run failure ('exit N') is
    evidence the stage exercised the contaminated tree — the vitest-missing
    fail-closed message is a tooling problem, not foreign dirt."""
    info = _provisioned_chain(repo, "t_vitest_missing", relpath="feature.py")
    foreign = repo / "web" / "src" / "control" / "Wip.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// foreign wip\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate_vitest_missing)
    assert out["action"] == "parked"
    assert out.get("park_class") != kwt.FOREIGN_DIRTY_CHECKOUT_CLASS
    assert out["reason"].startswith("post-merge gate failed")

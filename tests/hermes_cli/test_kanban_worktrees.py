"""Tests for worker isolation: dispatcher-provisioned worktrees + the
serialized chain integrator (hermes_cli.kanban_worktrees)."""

from __future__ import annotations

import json
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
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
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
    assert "leftover.py" in rows[0]["body"]
    assert "REQUEST_CHANGES" in rows[0]["body"]


# ---------------------------------------------------------------------------
# Phase 3 — serialized integrator
# ---------------------------------------------------------------------------

def _provisioned_chain(repo, root_id, relpath="feature.py",
                       content="VALUE = 1\n"):
    """Worktree with one committed change, ready to integrate."""
    info = kwt.ensure_worktree(repo, root_id)
    _commit_in(info["path"], relpath, content, msg=f"kanban({root_id}): work")
    return info


def test_integrate_merges_no_ff_and_cleans_up(repo):
    info = _provisioned_chain(repo, "t_m1")
    out = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=_ok_gate,
    )
    assert out["action"] == "merged"
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


def test_overlap_with_dirty_live_checkout_parks(repo):
    info = _provisioned_chain(repo, "t_ovl", relpath="a.txt",
                              content="branch change\n")
    # Foreign uncommitted edit of the SAME file in the live checkout.
    (repo / "a.txt").write_text("manual session edit\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert "overlap" in out["reason"]
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
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate)
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


def test_dirty_chain_worktree_parks(repo):
    info = _provisioned_chain(repo, "t_dwt")
    (info["path"] / "uncommitted.py").write_text("oops = 1\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert "uncommitted" in out["reason"]


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
    assert mods == ["tests/hermes_cli/test_kanban_db.py"]


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
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()
    assert task.status == "done"
    assert len(merged_events) == 1
    assert merged_events[0]["target"] == "main"
    assert (repo / "feature.py").read_text() == "VALUE = 2\n"
    assert not ws.exists()
    receipt = [c for c in comments if c["author"] == "integrator"]
    assert receipt and merged_events[0]["merge_commit"][:12] in receipt[0]["body"]


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

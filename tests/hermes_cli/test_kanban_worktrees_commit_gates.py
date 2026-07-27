"""Kanban worktrees tests: commit gates.

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
    _commit_in,
    _events,
    _git,
    _ok_gate,
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


def test_default_quick_gate_web_diff_bootstraps_missing_bins_with_frontend_gate(
    repo, monkeypatch,
):
    scripts = repo / "scripts"
    scripts.mkdir()
    frontend_gate = scripts / "gate-frontend.sh"
    frontend_gate.write_text("#!/usr/bin/env bash\n")
    frontend_gate.chmod(0o755)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["web/src/control/App.tsx"])

    assert ok is True, detail
    assert "frontend bootstrap ok" in detail
    assert [str(frontend_gate), "--skip-build"] in calls


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


def test_default_quick_gate_ruff_uses_main_repo_venv_before_runtime_python(
    repo, monkeypatch,
):
    """Nacht M5.2: the gate follows the worker-gate/run-tests repo venv."""
    repo_ruff = repo / "venv" / "bin" / "ruff"
    repo_ruff.parent.mkdir(parents=True)
    repo_ruff.write_text("#!/bin/sh\nexit 0\n")
    repo_ruff.chmod(0o755)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    notes = []
    error = kwt._default_quick_gate_ruff(repo, ["feature.py"], notes)

    assert error is None
    assert notes == ["ruff ok"]
    assert calls == [[
        str(repo_ruff),
        "check",
        "feature.py",
        "--extend-exclude",
        kwt.WORKTREES_DIRNAME,
    ]]


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



def test_review_snapshot_is_detached_read_only_full_diff_and_stable(repo, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_SNAPSHOT_MIN_FREE_BYTES", "0")
    base = _git(repo, "rev-parse", "HEAD").strip()
    payload = "start\n" + ("x" * (kb._DIFF_SNAPSHOT_PER_FILE_BYTE_CAP + 4096)) + "\nEND-OF-LARGE-DIFF\n"
    (repo / "a.txt").write_text(payload)
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD").strip()

    snapshot = kwt.provision_review_snapshot(
        source_workspace=repo,
        task_id="t_review",
        run_id=42,
        candidate_commit=candidate,
        base_commit=base,
    )
    path = Path(snapshot["workspace_path"])
    try:
        assert _git(path, "rev-parse", "HEAD").strip() == candidate
        detached = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=path,
            text=True,
            capture_output=True,
            check=False,
        )
        assert detached.returncode == 1
        full_diff = _git(path, "diff", "--no-ext-diff", f"{base}..{candidate}")
        assert "END-OF-LARGE-DIFF" in full_diff
        with pytest.raises(PermissionError):
            (path / "a.txt").write_text("review mutation\n")

        (repo / "a.txt").write_text("parallel writer\n")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-m", "parallel writer")
        assert _git(path, "rev-parse", "HEAD").strip() == candidate
        assert (path / "a.txt").read_text() == payload
    finally:
        kwt.cleanup_review_snapshot(
            repo_root=Path(snapshot["repo_root"]),
            workspace_path=path,
        )

    assert not path.exists()
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(path) not in listed

    orphan = kwt.provision_review_snapshot(
        source_workspace=repo, task_id="t_orphan", run_id=43,
        candidate_commit=candidate, base_commit=base,
    )
    orphan_path = Path(orphan["workspace_path"])
    swept = kwt.hygiene_sweep_review_snapshots(repo, max_age_seconds=0)
    assert str(orphan_path) in [item["path"] for item in swept["removed"]]
    assert not orphan_path.exists()


# ---------------------------------------------------------------------------
# Lane-scope enforcement (coder <-> coder-frontend) at the worker-commit
# boundary — mechanical backing for the decomposer PATH RULE prompt.
# Real SQLite kanban DB + real git repos; no mocks for the file set.
# ---------------------------------------------------------------------------

def _lane_scope_task(conn, repo, root_id, *, assignee, commits):
    """Provision a chain worktree, commit ``commits`` [(relpath, content)],
    and register the completing task against that worktree."""
    info = kwt.ensure_worktree(repo, root_id)
    for relpath, content in commits:
        _commit_in(info["path"], relpath, content, msg=f"kanban({root_id}): work")
    task_id = kb.create_task(
        conn,
        title=f"lane-scope {assignee}",
        assignee=assignee,
        workspace_kind="dir",
        workspace_path=str(info["path"]),
    )
    return task_id, info


def _complete(conn, task_id):
    return kwt.maybe_integrate_on_complete(conn, task_id, gate_runner=_ok_gate)


def _record_review_snapshot_event(
    conn, task_id, *, base_commit, candidate_commit,
):
    """Record the same commit-pair event that review snapshot provisioning emits."""
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            "review_snapshot_provisioned",
            {
                "base_commit": base_commit,
                "candidate_commit": candidate_commit,
            },
        )


def test_lane_scope_coder_frontend_allows_control_only(repo, kanban_home):
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn, repo, "t_ls_fe_ok",
            assignee="coder-frontend",
            commits=[("web/src/control/Foo.tsx", "export const Foo = 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []
    assert (repo / "web" / "src" / "control" / "Foo.tsx").exists()


def test_lane_scope_coder_frontend_blocks_backend_path(repo, kanban_home):
    with kb.connect() as conn:
        task_id, info = _lane_scope_task(
            conn, repo, "t_ls_fe_bad",
            assignee="coder-frontend",
            commits=[
                ("web/src/control/Foo.tsx", "export const Foo = 1\n"),
                ("hermes_cli/kanban.py", "X = 1\n"),
            ],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "parked"
        assert "lane-scope violation" in out["reason"]
        assert "'coder'" in out["reason"]
        blocked = _events(conn, task_id, "worker_gate_blocked")
        assert len(blocked) == 1
        payload = blocked[0]
        assert payload["violating_paths"] == ["hermes_cli/kanban.py"]
        assert payload["expected_lane"] == "coder"
        assert payload["assignee"] == "coder-frontend"
        fixer_event = _events(conn, task_id, "lane_scope_fixer_dispatched")
        assert len(fixer_event) == 1
        fixer = kb.get_task(conn, fixer_event[0]["child_id"])
        assert fixer.assignee == "coder"
        assert fixer.workspace_kind == "dir"
        assert fixer.workspace_path == str(info["path"])
        assert "hermes_cli/kanban.py" in fixer.body
    # Nothing merged; the branch survives for the operator.
    assert not (repo / "hermes_cli" / "kanban.py").exists()
    assert _git(repo, "rev-parse", info["branch"])


def test_lane_scope_coder_allows_backend_path(repo, kanban_home):
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn, repo, "t_ls_be_ok",
            assignee="coder",
            commits=[("hermes_cli/kanban.py", "X = 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []
    assert (repo / "hermes_cli" / "kanban.py").exists()


def test_lane_scope_coder_blocks_frontend_path(repo, kanban_home):
    with kb.connect() as conn:
        task_id, info = _lane_scope_task(
            conn, repo, "t_ls_be_bad",
            assignee="coder",
            commits=[("web/src/control/Foo.tsx", "export const Foo = 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "parked"
        assert "lane-scope violation" in out["reason"]
        assert "'coder-frontend'" in out["reason"]
        blocked = _events(conn, task_id, "worker_gate_blocked")
        assert len(blocked) == 1
        payload = blocked[0]
        assert payload["violating_paths"] == ["web/src/control/Foo.tsx"]
        assert payload["expected_lane"] == "coder-frontend"
        assert payload["assignee"] == "coder"
        fixer_event = _events(conn, task_id, "lane_scope_fixer_dispatched")
        assert len(fixer_event) == 1
        fixer = kb.get_task(conn, fixer_event[0]["child_id"])
        assert fixer.assignee == "coder-frontend"
        assert fixer.workspace_kind == "dir"
        assert fixer.workspace_path == str(info["path"])
        assert "web/src/control/Foo.tsx" in fixer.body
    assert not (repo / "web" / "src" / "control" / "Foo.tsx").exists()
    assert _git(repo, "rev-parse", info["branch"])


def test_lane_scope_fixer_creation_failure_preserves_park(
    repo, kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn,
            repo,
            "t_ls_fixer_failure",
            assignee="coder",
            commits=[("web/src/control/Foo.tsx", "export const Foo = 1\n")],
        )

        def raise_create(*args, **kwargs):
            raise RuntimeError("fixer creation failed")

        monkeypatch.setattr(kb, "create_task", raise_create)
        out = _complete(conn, task_id)

        assert out is not None
        assert out["action"] == "parked"
        assert len(_events(conn, task_id, "worker_gate_blocked")) == 1
        assert _events(conn, task_id, "lane_scope_fixer_dispatched") == []
        fixer_count = conn.execute(
            "SELECT COUNT(*) AS count FROM tasks WHERE idempotency_key LIKE ?",
            ("lane-scope-fixer:%",),
        ).fetchone()["count"]
        assert fixer_count == 0


def test_lane_scope_coder_allows_upstream_web_entry(repo, kanban_home):
    # web/src/App.tsx is upstream-owned and belongs to `coder` (named
    # exception, mirrors the decomposer prompt).
    assert "web/src/App.tsx" in kwt.LANE_SCOPE_UPSTREAM_WEB_PATHS
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn, repo, "t_ls_upstream",
            assignee="coder",
            commits=[("web/src/App.tsx", "export default 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []


def test_lane_scope_premium_lane_not_enforced(repo, kanban_home):
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn, repo, "t_ls_premium",
            assignee="premium",
            commits=[
                ("web/src/control/Foo.tsx", "export const Foo = 1\n"),
                ("hermes_cli/kanban.py", "X = 1\n"),
            ],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []


def test_lane_scope_coder_frontend_allows_preservable_artifact(repo, kanban_home):
    # Preservable visual-QA artifacts (existing PRESERVABLE_ARTIFACTS_CLASS
    # classification) are not source code and never decide a lane.
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn, repo, "t_ls_fe_art",
            assignee="coder-frontend",
            commits=[
                ("web/src/control/Foo.tsx", "export const Foo = 1\n"),
                ("visual-qa/lane-scope.png", "PNG\n"),
            ],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []


def test_lane_scope_coder_allows_generated_web_dist(repo, kanban_home):
    # hermes_cli/web_dist/** is generated build output and never counts as a
    # frontend path (named exception, mirrors the decomposer prompt).
    with kb.connect() as conn:
        task_id, _info = _lane_scope_task(
            conn, repo, "t_ls_webdist",
            assignee="coder",
            commits=[("hermes_cli/web_dist/app.js", "// built\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []


# ---------------------------------------------------------------------------
# Per-task attribution: siblings share ONE chain branch (worktree lease is
# `chain:<root_task_id>`), so the lane check must diff against the task's own
# pre_run_commit_sha, not cumulatively against the merge target.
# ---------------------------------------------------------------------------

def _claim_and_materialize(conn, task_id):
    """Run the production claim/materialization/base-preparation sequence."""
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None

    def resolve_existing(task, *, board=None):
        return Path(task.workspace_path), task.branch_name

    def resolve_managed_base(task, *, board=None):
        return Path(task.workspace_path)

    materialized = kwt.materialize_dispatch_workspace(
        conn,
        claimed,
        mode=kwt.MANAGED_WORKTREE_PROVISION,
        board="default",
        resolve_existing=resolve_existing,
        resolve_managed_base=resolve_managed_base,
    )
    prepared = kwt.prepare_reused_task_worktree(
        conn, claimed, materialized.path,
    )
    # `_apply_materialized_dispatch_workspace` refreshes this object in the
    # dispatcher after preparation. Provisioning has already persisted SQLite.
    claimed.workspace_path = str(materialized.path)
    claimed.branch_name = materialized.branch_name
    return claimed, materialized, prepared


def _lane_scope_sibling_chain(conn, repo, label, b_commits):
    """Coder A's commit precedes B on one chain branch; B uses the real path.

    The sibling commit is present before B is claimed, exactly like the
    accepted reproduction. B then goes through ``claim_task`` plus the public
    managed-materialization facade instead of receiving a hand-inserted run.
    """
    task_b = kb.create_task(
        conn, title="chain frontend slice", assignee="coder-frontend",
        workspace_kind="dir", workspace_path=str(repo),
    )
    info = kwt.ensure_worktree(repo, task_b)
    _commit_in(
        info["path"], "hermes_cli/foo.py", "FOO = 1\n",
        msg=f"kanban({label}): backend sibling slice",
    )
    sibling_head = _git(info["path"], "rev-parse", "HEAD")

    claimed, materialized, prepared = _claim_and_materialize(conn, task_b)
    assert materialized.path == info["path"]
    assert prepared is None, "first-time chain materialization is not a retry"
    run = conn.execute(
        "SELECT pre_run_commit_sha FROM task_runs WHERE id = ?",
        (claimed.current_run_id,),
    ).fetchone()
    assert run["pre_run_commit_sha"] == sibling_head

    for relpath, content in b_commits:
        _commit_in(
            info["path"], relpath, content,
            msg=f"kanban({label}): frontend",
        )
    return task_b, info


def test_lane_scope_sibling_files_not_attributed_to_frontend_task(repo, kanban_home):
    # The decomposer-prompt pattern: a coder child plus a coder-frontend
    # child on the SAME chain branch. B's own diff contains only
    # web/src/control/Bar.tsx — A's hermes_cli/foo.py must NOT be attributed
    # to B. With the old cumulative diff this test is RED (B blocked on
    # hermes_cli/foo.py); that control probe is cited in report-s2.md.
    with kb.connect() as conn:
        task_b, _info = _lane_scope_sibling_chain(
            conn, repo, "t_ls_sib_ok",
            b_commits=[("web/src/control/Bar.tsx", "export const Bar = 1\n")],
        )
        out = _complete(conn, task_b)
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_b, "worker_gate_blocked") == []
    # The full chain — A's backend file AND B's frontend file — landed.
    assert (repo / "hermes_cli" / "foo.py").exists()
    assert (repo / "web" / "src" / "control" / "Bar.tsx").exists()


def test_lane_scope_sibling_block_names_only_own_violating_paths(repo, kanban_home):
    # B genuinely crosses the lane (hermes_cli/baz.py) and stays blocked —
    # but the payload names ONLY B's own violation, never A's sibling file.
    with kb.connect() as conn:
        task_b, info = _lane_scope_sibling_chain(
            conn, repo, "t_ls_sib_bad",
            b_commits=[
                ("web/src/control/Bar.tsx", "export const Bar = 1\n"),
                ("hermes_cli/baz.py", "BAZ = 1\n"),
            ],
        )
        out = _complete(conn, task_b)
        assert out is not None and out["action"] == "parked"
        assert "lane-scope violation" in out["reason"]
        blocked = _events(conn, task_b, "worker_gate_blocked")
        assert len(blocked) == 1
        payload = blocked[0]
        assert payload["violating_paths"] == ["hermes_cli/baz.py"]
        assert payload["expected_lane"] == "coder"
        assert "hermes_cli/foo.py" not in payload["violating_paths"]
        assert "hermes_cli/foo.py" not in payload["changed_files"]
    # Nothing merged; the branch (both slices) survives for the operator.
    assert not (repo / "hermes_cli" / "baz.py").exists()
    assert _git(repo, "rev-parse", info["branch"])


def test_lane_scope_retry_keeps_first_run_basis(repo, kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="frontend retry",
            assignee="coder-frontend",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        first, materialized, first_prepared = _claim_and_materialize(conn, task_id)
        assert first_prepared is None
        first_base = conn.execute(
            "SELECT pre_run_commit_sha FROM task_runs WHERE id = ?",
            (first.current_run_id,),
        ).fetchone()["pre_run_commit_sha"]

        # Run 1 violates the frontend lane, then ends resumably.
        _commit_in(
            materialized.path,
            "hermes_cli/run_one_violation.py",
            "VIOLATION = 1\n",
            msg=f"kanban({task_id}): run 1 violation",
        )
        assert kb.block_task(conn, task_id, reason="retry cleanly")
        assert kb.unblock_task(conn, task_id)

        second, materialized_retry, second_prepared = _claim_and_materialize(
            conn, task_id,
        )
        assert second_prepared is not None
        assert second_prepared["action"] == "current"
        second_base = conn.execute(
            "SELECT pre_run_commit_sha FROM task_runs WHERE id = ?",
            (second.current_run_id,),
        ).fetchone()["pre_run_commit_sha"]
        assert second_base != first_base
        _commit_in(
            materialized_retry.path,
            "web/src/control/RetryClean.tsx",
            "export const RetryClean = 1\n",
            msg=f"kanban({task_id}): run 2 clean",
        )

        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "parked"
        blocked = _events(conn, task_id, "worker_gate_blocked")
        assert len(blocked) == 1
        assert blocked[0]["violating_paths"] == [
            "hermes_cli/run_one_violation.py",
        ]
        assert "web/src/control/RetryClean.tsx" in blocked[0]["changed_files"]


def test_lane_scope_non_ancestor_basis_excludes_sibling_after_rebase(
    repo, kanban_home, caplog,
):
    # B's ONLY recorded stamp is orphaned by a rebase of the shared chain
    # branch (main advanced, then the branch was rebased onto it) — its own
    # object no longer resolves as an ancestor of `branch`. The old plain
    # cumulative-diff fallback re-attributed sibling A's pre-existing
    # hermes_cli/foo.py to B and parked it; the fix subtracts what already
    # existed at the orphaned stamp (via its merge-base with `branch`) from
    # the cumulative diff, so only B's own frontend file remains.
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_rebased",
            b_commits=[
                ("web/src/control/Rebased.tsx", "export const Rebased = 1\n"),
            ],
        )
        old_basis = conn.execute(
            "SELECT pre_run_commit_sha FROM task_runs "
            "WHERE task_id = ? ORDER BY id ASC LIMIT 1",
            (task_id,),
        ).fetchone()["pre_run_commit_sha"]

        main_only = repo / "main-only.txt"
        main_only.write_text("target advanced\n")
        _git(repo, "add", "main-only.txt")
        _git(repo, "commit", "-m", "advance target before chain rebase")
        _git(info["path"], "rebase", "main")
        assert subprocess.run(
            [
                "git", "-C", str(repo), "merge-base", "--is-ancestor",
                old_basis, info["branch"],
            ],
            check=False,
            capture_output=True,
            text=True,
        ).returncode == 1

        with caplog.at_level("WARNING", logger=kwt.__name__):
            out = _complete(conn, task_id)

        assert out is not None and out["action"] == "merged"
        assert "is not an ancestor" in caplog.text
        assert "merge-base diff" in caplog.text
        assert _events(conn, task_id, "worker_gate_blocked") == []
    # The full chain — A's backend file AND B's frontend file — landed.
    assert (repo / "hermes_cli" / "foo.py").exists()
    assert (repo / "web" / "src" / "control" / "Rebased.tsx").exists()


def test_lane_scope_rebase_exclusion_still_catches_own_violation(
    repo, kanban_home,
):
    # Counter-probe for the merge-base exclusion above: B's OWN post-rebase
    # commit crosses the lane. Excluding what pre-existed at the orphaned
    # stamp must not hide a violation B commits itself AFTER that point.
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_rebased_own_violation",
            b_commits=[
                ("web/src/control/Rebased.tsx", "export const Rebased = 1\n"),
                ("hermes_cli/own_violation.py", "OWN = 1\n"),
            ],
        )
        main_only = repo / "main-only.txt"
        main_only.write_text("target advanced\n")
        _git(repo, "add", "main-only.txt")
        _git(repo, "commit", "-m", "advance target before chain rebase")
        _git(info["path"], "rebase", "main")

        out = _complete(conn, task_id)

        assert out is not None and out["action"] == "parked"
        blocked = _events(conn, task_id, "worker_gate_blocked")
        assert len(blocked) == 1
        assert blocked[0]["violating_paths"] == ["hermes_cli/own_violation.py"]
        assert "hermes_cli/foo.py" not in blocked[0]["violating_paths"]
    assert not (repo / "hermes_cli" / "own_violation.py").exists()


def test_lane_scope_spawn_retry_stamp_excluded_own_violation_still_parks(
    repo, kanban_home,
):
    # Counter-probe for the spawn_retry exclusion (B1): a task that survives
    # a transient spawn-retry gap and then genuinely commits outside its own
    # lane must still park — the fix removes a poisoned STAMP, not the check
    # itself.
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="chain root", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        info = kwt.ensure_worktree(repo, root)
        _commit_in(
            info["path"], "hermes_cli/foo.py", "FOO = 1\n",
            msg=f"kanban({root}): backend sibling slice",
        )
        task_b = kb.create_task(
            conn, title="frontend slice", assignee="coder-frontend",
            workspace_kind="dir", workspace_path=str(repo),
        )
        kb.link_tasks(conn, root, task_b)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (task_b,),
        )
        conn.commit()

        # RUN 1: claim, then a transient provisioning failure before the
        # materialization stamp — the exact spawn_retry gap from B1.
        claimed1 = kb.claim_task(conn, task_b)
        assert claimed1 is not None
        kb._record_spawn_retry(conn, task_b, "worktree provisioning (transient): git lock")

        # RUN 2: the real materialization pipeline.
        claimed2 = kb.claim_task(conn, task_b)
        assert claimed2 is not None
        workspace = kwt.provision_for_task(conn, claimed2, Path(repo))
        kwt.prepare_reused_task_worktree(conn, claimed2, workspace)
        kb.set_workspace_path(conn, task_b, str(workspace))

        # Worker B commits its own out-of-lane file.
        _commit_in(
            workspace, "hermes_cli/own_violation.py", "OWN = 1\n",
            msg=f"kanban({task_b}): own violation",
        )

        out = _complete(conn, task_b)

        assert out is not None and out["action"] == "parked"
        blocked = _events(conn, task_b, "worker_gate_blocked")
        assert len(blocked) == 1
        assert blocked[0]["violating_paths"] == ["hermes_cli/own_violation.py"]
        assert "hermes_cli/foo.py" not in blocked[0]["violating_paths"]
    assert not (repo / "hermes_cli" / "own_violation.py").exists()


def test_lane_scope_spawn_failed_stamp_excluded_sibling_not_attributed(
    repo, kanban_home,
):
    # S2 residual gap: _record_spawn_failure (outcome='spawn_failed') closes
    # a never-materialized run through the exact same _record_task_failure
    # machinery as _record_spawn_retry (outcome='spawn_retry'), leaving the
    # same claim-time poisoned stamp — but an outcome-name blacklist that
    # only excludes 'spawn_retry' lets it straight through: 'spawn_retry'
    # has 0 runs on the live board while 'spawn_failed' has 148 (49 with a
    # stamp). The positive workspace_materialized signal must exclude this
    # run's stamp regardless of which outcome name closed it.
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="chain root", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        info = kwt.ensure_worktree(repo, root)
        _commit_in(
            info["path"], "hermes_cli/foo.py", "FOO = 1\n",
            msg=f"kanban({root}): backend sibling slice",
        )
        task_b = kb.create_task(
            conn, title="frontend slice", assignee="coder-frontend",
            workspace_kind="dir", workspace_path=str(repo),
        )
        kb.link_tasks(conn, root, task_b)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (task_b,),
        )
        conn.commit()

        # RUN 1: claim, then a provisioning failure before the
        # materialization stamp, closed via the spawn_failed path (below
        # the failure limit, so the task returns to 'ready' for a retry).
        claimed1 = kb.claim_task(conn, task_b)
        assert claimed1 is not None
        kb._record_spawn_failure(conn, task_b, "worktree provisioning failed: git lock")

        # RUN 2: the real materialization pipeline.
        claimed2 = kb.claim_task(conn, task_b)
        assert claimed2 is not None
        workspace = kwt.provision_for_task(conn, claimed2, Path(repo))
        kwt.prepare_reused_task_worktree(conn, claimed2, workspace)
        kb.set_workspace_path(conn, task_b, str(workspace))

        # Worker B commits ONLY its own frontend file.
        _commit_in(
            workspace, "web/src/control/Bar.tsx", "export const Bar = 1\n",
            msg=f"kanban({task_b}): frontend",
        )

        out = _complete(conn, task_b)

        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_b, "worker_gate_blocked") == []
    assert (repo / "hermes_cli" / "foo.py").exists()
    assert (repo / "web" / "src" / "control" / "Bar.tsx").exists()


def test_lane_scope_gave_up_stamp_excluded_sibling_not_attributed(
    repo, kanban_home,
):
    # Same never-materialized-poisoned-stamp shape, but the breaker trips
    # inside the spawn-retry gap itself (a real board population: 175
    # gave_up runs, 53 carrying a stamp). The trip flips the task to
    # 'blocked'; the dispatcher's own recovery path unblocks it for the
    # next attempt — mirrored here via kb.unblock_task.
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="chain root", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        info = kwt.ensure_worktree(repo, root)
        _commit_in(
            info["path"], "hermes_cli/foo.py", "FOO = 1\n",
            msg=f"kanban({root}): backend sibling slice",
        )
        task_b = kb.create_task(
            conn, title="frontend slice", assignee="coder-frontend",
            workspace_kind="dir", workspace_path=str(repo),
        )
        kb.link_tasks(conn, root, task_b)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (task_b,),
        )
        conn.commit()

        # RUN 1: claim, then a provisioning failure that trips the breaker
        # (failure_limit=1) before the materialization stamp — closes with
        # outcome='gave_up' and blocks the task.
        claimed1 = kb.claim_task(conn, task_b)
        assert claimed1 is not None
        tripped = kb._record_spawn_failure(
            conn, task_b, "worktree provisioning failed: git lock",
            failure_limit=1,
        )
        assert tripped is True
        run1 = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? "
            "ORDER BY id ASC LIMIT 1",
            (task_b,),
        ).fetchone()
        assert run1["outcome"] == "gave_up"
        assert kb.unblock_task(conn, task_b)

        # RUN 2: the real materialization pipeline.
        claimed2 = kb.claim_task(conn, task_b)
        assert claimed2 is not None
        workspace = kwt.provision_for_task(conn, claimed2, Path(repo))
        kwt.prepare_reused_task_worktree(conn, claimed2, workspace)
        kb.set_workspace_path(conn, task_b, str(workspace))

        # Worker B commits ONLY its own frontend file.
        _commit_in(
            workspace, "web/src/control/Bar.tsx", "export const Bar = 1\n",
            msg=f"kanban({task_b}): frontend",
        )

        out = _complete(conn, task_b)

        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_b, "worker_gate_blocked") == []
    assert (repo / "hermes_cli" / "foo.py").exists()
    assert (repo / "web" / "src" / "control" / "Bar.tsx").exists()


def test_lane_scope_spawn_failed_own_violation_still_parks(repo, kanban_home):
    # Positive counter-probe for the spawn_failed path (mirrors the existing
    # spawn_retry counter-probe above): a task that survives a spawn_failed
    # gap and then genuinely commits outside its own lane must still park —
    # the materialization signal removes a poisoned STAMP, not the check.
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="chain root", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
        )
        info = kwt.ensure_worktree(repo, root)
        _commit_in(
            info["path"], "hermes_cli/foo.py", "FOO = 1\n",
            msg=f"kanban({root}): backend sibling slice",
        )
        task_b = kb.create_task(
            conn, title="frontend slice", assignee="coder-frontend",
            workspace_kind="dir", workspace_path=str(repo),
        )
        kb.link_tasks(conn, root, task_b)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root,))
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (task_b,),
        )
        conn.commit()

        claimed1 = kb.claim_task(conn, task_b)
        assert claimed1 is not None
        kb._record_spawn_failure(conn, task_b, "worktree provisioning failed: git lock")

        claimed2 = kb.claim_task(conn, task_b)
        assert claimed2 is not None
        workspace = kwt.provision_for_task(conn, claimed2, Path(repo))
        kwt.prepare_reused_task_worktree(conn, claimed2, workspace)
        kb.set_workspace_path(conn, task_b, str(workspace))

        # Worker B commits its own out-of-lane file (genuine violation) plus
        # a legit frontend file.
        _commit_in(
            workspace, "web/src/control/Bar.tsx", "export const Bar = 1\n",
            msg=f"kanban({task_b}): frontend",
        )
        _commit_in(
            workspace, "hermes_cli/own_violation.py", "OWN = 1\n",
            msg=f"kanban({task_b}): own violation",
        )

        out = _complete(conn, task_b)

        assert out is not None and out["action"] == "parked"
        blocked = _events(conn, task_b, "worker_gate_blocked")
        assert len(blocked) == 1
        assert blocked[0]["violating_paths"] == ["hermes_cli/own_violation.py"]
        assert "hermes_cli/foo.py" not in blocked[0]["violating_paths"]
    assert not (repo / "hermes_cli" / "own_violation.py").exists()


def test_lane_scope_no_materialized_run_skips_check_visibly(
    repo, kanban_home, caplog,
):
    # Bestandsdaten: a task whose ONLY run predates the workspace_materialized
    # migration (pre_run_commit_sha set by plain claim_task, flag never set
    # because neither stamping call site — _stamp_first_materialized_run_head
    # nor prepare_reused_task_worktree — ran) is an unverifiable basis: we
    # cannot tell a poisoned claim-time stamp apart from a legitimately
    # materialized pre-migration one. Fail-closed (park) would cost more
    # than it protects, so this must fail OPEN — but visibly (a WARNING),
    # not as a silent "clean" no-op indistinguishable from an actually clean
    # completion.
    with kb.connect() as conn:
        task_id, info = _lane_scope_task(
            conn, repo, "t_ls_legacy", assignee="coder-frontend", commits=[],
        )
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (task_id,),
        )
        conn.commit()
        # Plain claim_task stamps pre_run_commit_sha at claim time; neither
        # materialization call site runs here, so workspace_materialized
        # stays at its column default (0) — exactly the pre-migration shape.
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run = conn.execute(
            "SELECT pre_run_commit_sha, workspace_materialized "
            "FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        assert run["pre_run_commit_sha"]
        assert run["workspace_materialized"] == 0

        # Worker commits a backend path from a coder-frontend task — a
        # genuine violation the check would normally catch.
        _commit_in(
            info["path"], "hermes_cli/backend_from_frontend.py", "X = 1\n",
            msg=f"kanban({task_id}): backend path from a coder-frontend task",
        )

        with caplog.at_level("WARNING", logger=kwt.__name__):
            out = _complete(conn, task_id)

        # The lane-scope check itself is skipped (fail-open on an
        # unverifiable basis), so completion proceeds exactly as if the
        # task were clean — but the skip is logged, not silent.
        assert out is not None and out["action"] == "merged"
        assert "workspace_materialized" in caplog.text
        assert "skipping lane-scope enforcement" in caplog.text
        assert _events(conn, task_id, "worker_gate_blocked") == []
    assert (repo / "hermes_cli" / "backend_from_frontend.py").exists()


# ---------------------------------------------------------------------------
# Review-snapshot attribution: use the exact reviewed base..candidate pair,
# rather than the shared chain branch's moving tip.
# ---------------------------------------------------------------------------


def _lane_scope_pre_run_basis(conn, task_id):
    row = conn.execute(
        "SELECT pre_run_commit_sha FROM task_runs "
        "WHERE task_id = ? AND workspace_materialized = 1 "
        "ORDER BY id ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None
    return row["pre_run_commit_sha"]


def test_lane_scope_review_snapshot_excludes_foreign_later_chain_commit(
    repo, kanban_home,
):
    # Regression: B's review was clean, but a different task appended a
    # backend commit to the same shared branch after B's candidate. The
    # pre_run..branch range incorrectly attributes that foreign path to B.
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_snapshot_foreign",
            b_commits=[("web/src/control/Reviewed.tsx", "export const Reviewed = 1\n")],
        )
        base = _lane_scope_pre_run_basis(conn, task_id)
        candidate = _git(info["path"], "rev-parse", "HEAD")
        _commit_in(
            info["path"],
            "hermes_cli/foreign_after_review.py",
            "FOREIGN = 1\n",
            msg="kanban(foreign): commit after reviewed candidate",
        )
        _record_review_snapshot_event(
            conn,
            task_id,
            base_commit=base,
            candidate_commit=candidate,
        )

        out = _complete(conn, task_id)

        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []


def test_lane_scope_review_snapshot_still_blocks_own_violation(repo, kanban_home):
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_snapshot_own_violation",
            b_commits=[
                ("web/src/control/Reviewed.tsx", "export const Reviewed = 1\n"),
                ("hermes_cli/own_reviewed_violation.py", "OWN = 1\n"),
            ],
        )
        _record_review_snapshot_event(
            conn,
            task_id,
            base_commit=_lane_scope_pre_run_basis(conn, task_id),
            candidate_commit=_git(info["path"], "rev-parse", "HEAD"),
        )

        out = _complete(conn, task_id)

        assert out is not None and out["action"] == "parked"
        assert _events(conn, task_id, "worker_gate_blocked")[0]["violating_paths"] == [
            "hermes_cli/own_reviewed_violation.py",
        ]


def test_lane_scope_without_review_snapshot_keeps_pre_run_branch_fallback(
    repo, kanban_home,
):
    # No snapshot remains the current pre_run..branch behavior: a later
    # foreign backend path is visible and blocks the frontend task.
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_snapshot_absent",
            b_commits=[("web/src/control/Reviewed.tsx", "export const Reviewed = 1\n")],
        )
        _commit_in(
            info["path"],
            "hermes_cli/foreign_without_snapshot.py",
            "FOREIGN = 1\n",
            msg="kanban(foreign): commit without snapshot",
        )

        out = _complete(conn, task_id)

        assert out is not None and out["action"] == "parked"
        assert _events(conn, task_id, "worker_gate_blocked")[0]["violating_paths"] == [
            "hermes_cli/foreign_without_snapshot.py",
        ]


def test_lane_scope_review_snapshot_missing_base_uses_fallback(repo, kanban_home):
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_snapshot_missing_base",
            b_commits=[("web/src/control/Reviewed.tsx", "export const Reviewed = 1\n")],
        )
        _commit_in(
            info["path"],
            "hermes_cli/foreign_missing_base.py",
            "FOREIGN = 1\n",
            msg="kanban(foreign): missing snapshot base",
        )
        _record_review_snapshot_event(
            conn,
            task_id,
            base_commit=None,
            candidate_commit=_git(info["path"], "rev-parse", "HEAD"),
        )

        out = _complete(conn, task_id)

        assert out is not None and out["action"] == "parked"
        assert _events(conn, task_id, "worker_gate_blocked")[0]["violating_paths"] == [
            "hermes_cli/foreign_missing_base.py",
        ]


def test_lane_scope_unresolvable_review_snapshot_uses_fallback_and_logs(
    repo, kanban_home, caplog,
):
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_snapshot_unresolvable",
            b_commits=[("web/src/control/Reviewed.tsx", "export const Reviewed = 1\n")],
        )
        _commit_in(
            info["path"],
            "hermes_cli/foreign_unresolvable.py",
            "FOREIGN = 1\n",
            msg="kanban(foreign): unresolved snapshot candidate",
        )
        _record_review_snapshot_event(
            conn,
            task_id,
            base_commit=_lane_scope_pre_run_basis(conn, task_id),
            candidate_commit="f" * 40,
        )

        with caplog.at_level("WARNING", logger=kwt.__name__):
            out = _complete(conn, task_id)

        assert out is not None and out["action"] == "parked"
        assert "review snapshot" in caplog.text
        assert "does not resolve" in caplog.text


def test_lane_scope_most_recent_review_snapshot_wins(repo, kanban_home):
    with kb.connect() as conn:
        task_id, info = _lane_scope_sibling_chain(
            conn,
            repo,
            "t_ls_snapshot_latest",
            b_commits=[("web/src/control/Reviewed.tsx", "export const Reviewed = 1\n")],
        )
        base = _lane_scope_pre_run_basis(conn, task_id)
        clean_candidate = _git(info["path"], "rev-parse", "HEAD")
        _commit_in(
            info["path"],
            "hermes_cli/newer_reviewed_violation.py",
            "NEWER = 1\n",
            msg="kanban(task): newer reviewed violation",
        )
        violating_candidate = _git(info["path"], "rev-parse", "HEAD")
        _record_review_snapshot_event(
            conn,
            task_id,
            base_commit=base,
            candidate_commit=violating_candidate,
        )
        _record_review_snapshot_event(
            conn,
            task_id,
            base_commit=base,
            candidate_commit=clean_candidate,
        )

        out = _complete(conn, task_id)

        # The newer clean event must beat both the older violating snapshot
        # and the shared branch tip, which still contains the later commit.
        assert out is not None and out["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked") == []


# ---------------------------------------------------------------------------
# V3 — auto-close never-ran ordinary chain roots so auto-land can fire
# ---------------------------------------------------------------------------


def test_never_ran_ordinary_root_auto_closes_when_children_terminal(
    repo, kanban_home, monkeypatch,
):
    """Bookkeeping root (never assigned/ran) must auto-close after last child.

    Without the never-ran finalizer, maybe_integrate_on_complete returns
    deferred (open sibling = root) and the chain never lands.
    """
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="bookkeeping chain root",
            assignee=None,
            created_by="tester",
            kind="code",
        )
        info = kwt.ensure_worktree(repo, root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', workspace_kind = 'dir', "
                "workspace_path = ?, started_at = NULL WHERE id = ?",
                (str(info["path"]), root_id),
            )
        child_id = kb.create_task(
            conn,
            title="real work child",
            assignee="coder",
            created_by="tester",
            workspace_kind="dir",
            workspace_path=str(info["path"]),
            kind="code",
        )
        kb.link_tasks(conn, root_id, child_id)
        # Mirror complete_task mid-flight status (hook runs before done write).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (child_id,),
            )
        _commit_in(
            info["path"],
            "feature.py",
            "VALUE = 1\n",
            msg=f"kanban({child_id}): work",
        )

        # Precondition: root never ran.
        runs = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? LIMIT 1", (root_id,),
        ).fetchone()
        assert runs is None
        assert kb.get_task(conn, root_id).started_at is None
        assert kb.get_task(conn, root_id).status == "ready"

        out = kwt.maybe_integrate_on_complete(
            conn, child_id, gate_runner=_ok_gate,
        )

        root = kb.get_task(conn, root_id)
        auto = _events(conn, root_id, "never_ran_root_auto_completed")

    assert out is not None
    assert out["action"] == "merged"
    assert root.status == "done"
    assert len(auto) == 1
    assert auto[0]["completed_by"] == child_id
    assert (repo / "feature.py").read_text() == "VALUE = 1\n"


def test_never_ran_root_not_auto_closed_when_it_has_task_runs(
    repo, kanban_home, monkeypatch,
):
    """Guard 2: a root that has a task_runs row stays open (deferred)."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn, title="ran root", assignee="coder", kind="code",
        )
        info = kwt.ensure_worktree(repo, root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', workspace_kind = 'dir', "
                "workspace_path = ? WHERE id = ?",
                (str(info["path"]), root_id),
            )
        # Simulate a prior run without going through claim (started_at/run).
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, started_at, status, profile) "
                "VALUES (?, ?, 'running', 'coder')",
                (root_id, int(__import__("time").time())),
            )
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (int(__import__("time").time()), root_id),
            )
        child_id = kb.create_task(
            conn,
            title="child",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(info["path"]),
            kind="code",
        )
        kb.link_tasks(conn, root_id, child_id)
        _commit_in(
            info["path"], "feature.py", "VALUE = 2\n",
            msg=f"kanban({child_id}): work",
        )

        out = kwt.maybe_integrate_on_complete(
            conn, child_id, gate_runner=_ok_gate,
        )
        root = kb.get_task(conn, root_id)
        auto = _events(conn, root_id, "never_ran_root_auto_completed")

    assert out is not None
    assert out.get("action") == "deferred"
    assert root.status == "ready"
    assert auto == []


def test_lane_scope_allowlist_prevents_repark_after_fixer(
    repo, kanban_home, monkeypatch,
):
    """V7 loop trap: after fixer is done, parent re-check must not re-park
    solely for the fixer's allowlisted paths."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    from hermes_cli import kanban_lane_fixer as lane_fixer

    with kb.connect() as conn:
        task_id, info = _lane_scope_task(
            conn,
            repo,
            "t_ls_repark",
            assignee="coder",
            commits=[("web/src/control/Foo.tsx", "export const Foo = 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "parked"
        fixer_events = _events(conn, task_id, "lane_scope_fixer_dispatched")
        assert len(fixer_events) == 1
        child_id = fixer_events[0]["child_id"]

        # Mark fixer done (successful corrective handoff).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        allow = lane_fixer.allowlisted_paths_for_parent(
            conn,
            task_id,
            violating_paths=["web/src/control/Foo.tsx"],
            expected_lane="coder-frontend",
        )
        assert "web/src/control/Foo.tsx" in allow

        # Re-run lane enforcement: same attributed paths, but allowlisted.
        out2 = _complete(conn, task_id)
        assert out2 is not None
        assert out2["action"] == "merged"
        assert _events(conn, task_id, "worker_gate_blocked")  # original park only
        # No second park payload beyond the first.
        assert len(_events(conn, task_id, "worker_gate_blocked")) == 1


def test_lane_scope_allowlist_does_not_cover_new_commits_after_fixer(
    repo, kanban_home, monkeypatch,
):
    """B5: after a successful fixer resume, NEW parent commits on a formerly
    allowlisted path must re-arm the lane gate (not permanently disable it)."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    from hermes_cli import kanban_lane_fixer as lane_fixer

    with kb.connect() as conn:
        task_id, info = _lane_scope_task(
            conn,
            repo,
            "t_ls_retouch",
            assignee="coder",
            commits=[("web/src/control/Foo.tsx", "export const Foo = 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "parked"
        fixer_events = _events(conn, task_id, "lane_scope_fixer_dispatched")
        child_id = fixer_events[0]["child_id"]
        tip_before = _git(info["path"], "rev-parse", "HEAD").strip()

        # Force a blocked park + structured lane-scope event (no claim_task —
        # that would insert non-materialized pre_run stamps and skip the gate).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'integration' "
                "WHERE id = ?",
                (task_id,),
            )
            kb._append_event(
                conn,
                task_id,
                "blocked",
                {
                    "reason": (
                        "integration parked: lane-scope violation: assignee "
                        "'coder' changed paths outside its lane: "
                        "web/src/control/Foo.tsx — this task should have been "
                        "assigned to lane 'coder-frontend'."
                    ),
                    "kind": "integration",
                },
            )

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_id) is True
        tip_events = _events(conn, task_id, lane_fixer.LANE_FIXER_PARENT_RESUMED_EVENT)
        assert tip_events
        assert tip_events[0].get("resume_branch_tip")

        # Parent makes NEW work on the formerly allowlisted path after resume.
        _commit_in(
            info["path"],
            "web/src/control/Foo.tsx",
            "export const Foo = 2\n",
            msg=f"kanban({task_id}): new work after fixer",
        )
        assert _git(info["path"], "rev-parse", "HEAD").strip() != tip_before

        # Ensure no poisoned task_runs stamps skip the gate (legacy path uses
        # cumulative branch diff, which still sees Foo.tsx).
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))

        out2 = _complete(conn, task_id)
        assert out2 is not None
        assert out2["action"] == "parked", out2
        assert "lane-scope" in (out2.get("reason") or "").lower()
        # Second park recorded (original + re-arm).
        assert len(_events(conn, task_id, "worker_gate_blocked")) >= 2


def test_never_ran_root_not_auto_closed_when_it_has_assignee(
    repo, kanban_home, monkeypatch,
):
    """B1: a ready root WITH assignee is real work, not bookkeeping — even if
    it never ran and all children are terminal."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="assigned chain root still waiting to run",
            assignee="coder",
            created_by="tester",
            kind="code",
        )
        info = kwt.ensure_worktree(repo, root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', workspace_kind = 'dir', "
                "workspace_path = ?, started_at = NULL WHERE id = ?",
                (str(info["path"]), root_id),
            )
        child_id = kb.create_task(
            conn,
            title="real work child",
            assignee="coder",
            created_by="tester",
            workspace_kind="dir",
            workspace_path=str(info["path"]),
            kind="code",
        )
        kb.link_tasks(conn, root_id, child_id)
        _commit_in(
            info["path"],
            "feature.py",
            "VALUE = 1\n",
            msg=f"kanban({child_id}): work",
        )

        out = kwt.maybe_integrate_on_complete(
            conn, child_id, gate_runner=_ok_gate,
        )
        root = kb.get_task(conn, root_id)
        auto = _events(conn, root_id, "never_ran_root_auto_completed")

    assert out is not None
    assert out.get("action") == "deferred"
    assert root.status == "ready"
    assert auto == []


def test_never_ran_root_parks_when_children_only_failed(
    repo, kanban_home, monkeypatch,
):
    """M2: failed/cancelled children must not green-close the root or stamp
    strategist shipped — park like the decompose twin."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="bookkeeping root over failed children",
            assignee=None,
            created_by="tester",
            kind="code",
        )
        info = kwt.ensure_worktree(repo, root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', workspace_kind = 'dir', "
                "workspace_path = ?, started_at = NULL WHERE id = ?",
                (str(info["path"]), root_id),
            )
        child_id = kb.create_task(
            conn,
            title="failed child",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(info["path"]),
            kind="code",
        )
        kb.link_tasks(conn, root_id, child_id)
        # Put a real commit on the chain so integrate can merge; the completing
        # "driver" below will be treated as done evidence separately.
        _commit_in(
            info["path"],
            "feature.py",
            "VALUE = 9\n",
            msg=f"kanban({child_id}): work before fail",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'failed' WHERE id = ?", (child_id,),
            )

        # Drive auto-complete path as if a terminal sibling finished the chain.
        # Eligibility treats failed as terminal; auto-complete must then refuse
        # because completed_task_id is the failed child (not a real done).
        assert kwt._never_ran_root_eligible(
            conn, root_id, repo_root=repo, completing_task_id=child_id,
        )
        kwt._auto_complete_never_ran_chain_root(
            conn,
            root_id=root_id,
            completed_task_id=child_id,
            outcome={
                "action": "merged",
                "branch": kwt.chain_branch(root_id),
                "merge_commit": "a" * 40,
            },
        )
        root = kb.get_task(conn, root_id)
        auto = _events(conn, root_id, "never_ran_root_auto_completed")
        blocked = _events(conn, root_id, "blocked")

    assert root.status == "blocked"
    assert auto == []
    assert blocked
    assert "kein Kind erfolgreich" in (blocked[-1].get("reason") or "")


def test_archive_retrigger_runs_integration_when_chain_becomes_terminal(
    repo, kanban_home, monkeypatch,
):
    """F3 / M1 incident replay: last code child completes while siblings open
    → deferred; later archives make the chain terminal → the archive_task wire
    re-fires integration and can auto-complete the decompose root.

    Covers the operator wire: ``kb.archive_task(..., retrigger_integration=True)``
    calls the fork-owned ``maybe_retrigger_integration_after_archive`` after
    ``recompute_ready``. Without that flag/wire the root stays ``ready`` here.
    """
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="decompose root stranded by late archive",
            triage=True,
        )
        # Mark as decompose root (inverted links + durable decomposed event).
        child_a, child_b, child_c = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "code A", "assignee": "coder", "parents": []},
                {"title": "code B open then archive", "assignee": "coder", "parents": []},
                {"title": "code C open then archive", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        # Provision via ensure_worktree (root may not claim cleanly after decompose).
        info = kwt.ensure_worktree(repo, root)
        shared_wt = info["path"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL, "
                "workspace_path = ?, workspace_kind = 'dir', started_at = NULL "
                "WHERE id = ?",
                (str(shared_wt), root),
            )
            for cid in (child_a, child_b, child_c):
                conn.execute(
                    "UPDATE tasks SET status = 'running', workspace_path = ?, "
                    "workspace_kind = 'dir' WHERE id = ?",
                    (str(shared_wt), cid),
                )

        _commit_in(
            shared_wt,
            "feature_a.py",
            "A = 1\n",
            msg=f"kanban({child_a}): work",
        )
        # Child A completes → siblings B/C still open → deferred.
        out1 = kwt.maybe_integrate_on_complete(
            conn, child_a, gate_runner=_ok_gate,
        )
        assert out1 is not None
        assert out1.get("action") == "deferred"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(__import__("time").time()), child_a),
            )

        # B and C archived ~later (incident: ~3 minutes). Operator archive
        # wire must re-fire integration once the LAST sibling goes terminal.
        assert kb.archive_task(conn, child_b, retrigger_integration=True)
        assert kb.get_task(conn, root).status == "ready", (
            "chain not terminal yet (child_c open) — wire must not fire"
        )
        assert kb.archive_task(conn, child_c, retrigger_integration=True)
        assert kb.get_task(conn, child_b).status == "archived"
        assert kb.get_task(conn, child_c).status == "archived"
        root_after = kb.get_task(conn, root)

    # Wire-driven integration landed the chain and auto-completed the root.
    assert root_after.status == "done", (
        "archive_task wire did not re-trigger integration (root still "
        f"{root_after.status!r})"
    )
    assert (repo / "feature_a.py").read_text() == "A = 1\n"


def test_archive_without_retrigger_flag_does_not_integrate(
    repo, kanban_home, monkeypatch,
):
    """R2-3: default archive_task must NOT run the integration retrigger wire
    (SUPERSEDED / funnel / planspec paths leave the flag False)."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="decompose root no operator flag",
            triage=True,
        )
        child_a, child_b = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "code A", "assignee": "coder", "parents": []},
                {"title": "code B archive default", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        info = kwt.ensure_worktree(repo, root)
        shared_wt = info["path"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL, "
                "workspace_path = ?, workspace_kind = 'dir', started_at = NULL "
                "WHERE id = ?",
                (str(shared_wt), root),
            )
            for cid in (child_a, child_b):
                conn.execute(
                    "UPDATE tasks SET status = 'running', workspace_path = ?, "
                    "workspace_kind = 'dir' WHERE id = ?",
                    (str(shared_wt), cid),
                )
        _commit_in(
            shared_wt,
            "feature_a.py",
            "A = 1\n",
            msg=f"kanban({child_a}): work",
        )
        out1 = kwt.maybe_integrate_on_complete(
            conn, child_a, gate_runner=_ok_gate,
        )
        assert out1 is not None and out1.get("action") == "deferred"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(__import__("time").time()), child_a),
            )

        # Default flag False — archive succeeds but does not integrate.
        assert kb.archive_task(conn, child_b)
        root_after = kb.get_task(conn, root)

    assert root_after.status == "ready", (
        f"default archive must not retrigger; root became {root_after.status!r}"
    )
    assert not (repo / "feature_a.py").exists()


def test_retrigger_driver_skips_lane_scope_on_done_child(
    repo, kanban_home, monkeypatch,
):
    """R2-1: archive retrigger must not re-apply lane-scope to a done driver
    when a later sibling committed foreign-lane paths before archiving."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(conn, title="decompose root", triage=True)
        child_a, child_b = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "A", "assignee": "coder", "parents": []},
                {"title": "B frontend", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        info = kwt.ensure_worktree(repo, root)
        wt = info["path"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', workspace_path=?, "
                "workspace_kind='dir', started_at=NULL WHERE id=?",
                (str(wt), root),
            )
            for cid in (child_a, child_b):
                conn.execute(
                    "UPDATE tasks SET status='running', workspace_path=?, "
                    "workspace_kind='dir' WHERE id=?",
                    (str(wt), cid),
                )
        _commit_in(wt, "feature_a.py", "A = 1\n", msg=f"kanban({child_a}): work")
        out1 = kwt.maybe_integrate_on_complete(
            conn, child_a, gate_runner=_ok_gate,
        )
        assert out1.get("action") == "deferred"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                (int(__import__("time").time()), child_a),
            )
        # Sibling B commits a FRONTEND path, then is archived without completing.
        _commit_in(
            wt,
            "web/src/control/Panel.tsx",
            "export const P = 1\n",
            msg=f"kanban({child_b}): frontend work",
        )
        assert kb.archive_task(conn, child_b, retrigger_integration=True)
        root_after = kb.get_task(conn, root)
        fixers = _events(conn, child_a, "lane_scope_fixer_dispatched")
        parks = _events(conn, child_a, "worker_gate_blocked")

    assert root_after.status == "done", (
        f"retrigger did NOT integrate (root={root_after.status!r})"
    )
    assert not fixers, "spurious lane fixer created for an already-done task"
    assert not parks, "lane-scope park written on already-done driver"
    assert (repo / "web/src/control/Panel.tsx").exists()
    assert (repo / "feature_a.py").read_text() == "A = 1\n"


def test_lane_scope_allowlist_without_resume_still_rearms_on_new_commits(
    repo, kanban_home, monkeypatch,
):
    """R2-2 residual B5: operator unblock (no resume event) must not leave a
    permanent allowlist — brand-new foreign-lane work after the park tip
    re-arms the gate via park_branch_tip."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        task_id, info = _lane_scope_task(
            conn,
            repo,
            "t_ls_resid",
            assignee="coder",
            commits=[("web/src/control/Foo.tsx", "export const Foo = 1\n")],
        )
        out = _complete(conn, task_id)
        assert out is not None and out["action"] == "parked"
        park_payloads = _events(conn, task_id, "worker_gate_blocked")
        assert park_payloads
        assert park_payloads[0].get("park_branch_tip"), (
            "park payload must carry park_branch_tip for residual B5 retouch"
        )
        child_id = _events(conn, task_id, "lane_scope_fixer_dispatched")[0][
            "child_id"
        ]

        # Fixer finishes. Operator used `unblock` instead of the resume hook →
        # NO lane_scope_fixer_parent_resumed event exists.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (child_id,),
            )
        assert _events(conn, task_id, "lane_scope_fixer_parent_resumed") == []

        # Parent commits BRAND NEW work on the same foreign-lane path.
        _commit_in(
            info["path"],
            "web/src/control/Foo.tsx",
            "export const Foo = 999\n",
            msg=f"kanban({task_id}): brand new frontend work",
        )
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))

        out2 = _complete(conn, task_id)
        assert out2 is not None
        assert out2["action"] == "parked", (
            f"GATE SUPPRESSED: new foreign-lane work merged, action={out2.get('action')}"
        )
        assert "lane-scope" in (out2.get("reason") or "").lower()
        assert len(_events(conn, task_id, "worker_gate_blocked")) >= 2


def test_retrigger_skips_done_children_without_workspace(repo, kanban_home, monkeypatch):
    """Opus R3-1 regression: the latest-done driver may be a reviewer child
    with workspace_kind='scratch' and workspace_path NULL. The driver query
    must skip workspace-less done children, otherwise the whole retrigger
    silently aborts and the root stays stranded (root='ready').
    """
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(conn, title="decompose root", triage=True)
        a, r, c = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "A code", "assignee": "coder", "parents": []},
                {"title": "R review", "assignee": "reviewer", "parents": []},
                {"title": "C code", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        info = kwt.ensure_worktree(repo, root)
        wt = info["path"]
        now = int(__import__("time").time())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', workspace_path=?, "
                "workspace_kind='dir', started_at=NULL WHERE id=?",
                (str(wt), root),
            )
            for cid in (a, c):
                conn.execute(
                    "UPDATE tasks SET status='running', workspace_path=?, "
                    "workspace_kind='dir' WHERE id=?",
                    (str(wt), cid),
                )
        _commit_in(wt, "feature_a.py", "A = 1\n", msg=f"kanban({a}): work")
        out = kwt.maybe_integrate_on_complete(conn, a, gate_runner=_ok_gate)
        assert out is not None and out.get("action") == "deferred"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                (now - 300, a),
            )
            # Reviewer finished LAST with no workspace of its own (live shape).
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=?, "
                "workspace_path=NULL, workspace_kind='scratch' WHERE id=?",
                (now - 5, r),
            )
        assert kb.archive_task(conn, c, retrigger_integration=True)
        root_after = kb.get_task(conn, root)

    assert root_after.status == "done", (
        "retrigger aborted on workspace-less latest-done child "
        f"(root still {root_after.status!r})"
    )
    assert (repo / "feature_a.py").read_text() == "A = 1\n"

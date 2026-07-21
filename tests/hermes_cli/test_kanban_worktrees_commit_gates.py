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
    _events,
    _git,
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

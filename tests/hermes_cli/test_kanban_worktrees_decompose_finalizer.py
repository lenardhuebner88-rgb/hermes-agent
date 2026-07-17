"""Kanban worktrees tests: decompose finalizer.

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
    _ok_gate,
    _events,
    _provisioned_chain,
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


def _fake_activation(*, ok=True, pre=1111, post=2222, sha="a" * 40):
    """Injectable ``activation_runner`` seam for tests: mimics the real
    deploy_dashboard.sh runner's ``(ok, output, meta)`` contract with a changed
    dashboard PID (pre != post) as the 'restart happened' evidence. No real
    build/restart runs."""
    calls = []

    def _run():
        calls.append(True)
        output = "deploy_dashboard.sh: OK" if ok else "activation: deploy_dashboard.sh exit 1"
        return ok, output, {
            "pre_pid": pre,
            "post_pid": post,
            "deploy_exit": 0 if ok else 1,
            "deployed_sha": sha,
            "running_sha": sha,
        }

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


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


def test_auto_complete_decompose_root_refuses_when_all_children_archived(
    kanban_home,
):
    """S3 regression (t_ecd5cf42, 2026-07-17): a decompose root must not be
    marked ``done`` when EVERY chain child was only superseded-archived (via
    ``block_task`` + a SUPERSEDED reason, which auto-archives and DELETES the
    child's task_links row to the root) — zero real work landed. Instead of
    completing, the root is parked ``blocked`` with an operator-facing reason."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="all-archived decompose root", triage=True,
        )
        child_a, child_b = kb.decompose_triage_task(
            conn, root, root_assignee=None,
            children=[
                {"title": "child A", "assignee": "coder", "parents": []},
                {"title": "child B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        for child in (child_a, child_b):
            kb.claim_task(conn, child)
            assert kb.block_task(
                conn, child, reason="SUPERSEDED: recovery-loop stop",
            )
            assert kb.get_task(conn, child).status == "archived"

        kwt._auto_complete_decompose_root(
            conn, root_id=root, completed_task_id=child_b, outcome={},
        )

        root_task = kb.get_task(conn, root)
        blocked_events = _events(conn, root, "blocked")

    assert root_task.status == "blocked"
    assert blocked_events
    assert "kein Kind erfolgreich" in blocked_events[-1]["reason"]
    assert "Operator pruefen" in blocked_events[-1]["reason"]


def test_auto_complete_decompose_root_proceeds_when_one_child_really_done(
    kanban_home,
):
    """Gegentest: as long as at least one chain child reached a REAL
    ``done`` (its task_links row to the root survives), the root may still
    auto-complete even if a sibling was superseded-archived."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="partial-done decompose root", triage=True,
        )
        child_a, child_b = kb.decompose_triage_task(
            conn, root, root_assignee=None,
            children=[
                {"title": "child A", "assignee": "coder", "parents": []},
                {"title": "child B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        kb.claim_task(conn, child_a)
        assert kb.complete_task(conn, child_a, result="child A done")
        kb.claim_task(conn, child_b)
        assert kb.block_task(
            conn, child_b, reason="SUPERSEDED: recovery-loop stop",
        )
        assert kb.get_task(conn, child_b).status == "archived"

        kwt._auto_complete_decompose_root(
            conn, root_id=root, completed_task_id=child_a, outcome={},
        )

        root_task = kb.get_task(conn, root)
        auto_done = _events(conn, root, "decompose_root_auto_completed")

    assert root_task.status == "done"
    assert auto_done and auto_done[-1]["completed_by"] == child_a


def test_auto_complete_decompose_root_refuses_when_completed_task_row_missing(
    kanban_home,
):
    """Cross-family review finding 2 (2026-07-17): the no-real-completion
    guard must fail CLOSED, not open, when ``completed_task_id`` doesn't
    resolve to a row at all (deleted / never existed / wrong id) — the
    previous blacklist-based check treated a missing row (status=None) as
    "not archived" and silently accepted it as real completion evidence."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="missing-completer root", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (root,),
            )

        kwt._auto_complete_decompose_root(
            conn, root_id=root, completed_task_id="t_doesnotexist", outcome={},
        )

        root_task = kb.get_task(conn, root)
        blocked_events = _events(conn, root, "blocked")

    assert root_task.status != "done"
    assert blocked_events
    assert "kein Kind erfolgreich" in blocked_events[-1]["reason"]


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
        deployed = _events(conn, root, "deployment_verified")
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
    assert deployed[0]["deployed_sha"] == deployed[0]["running_sha"] == "a" * 40
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
        deployed = _events(conn, root, "deployment_verified")
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
    assert deployed == [
        {
            "deployed_sha": "a" * 40,
            "running_sha": "a" * 40,
            "release_gate_task_id": child_id,
            "source": "release_gate_activation",
        }
    ]
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
    monkeypatch.setattr(kwt, "_git", lambda *args, **kwargs: "a" * 40)
    pids = iter([54321, 67890])  # pre, then post — a real restart forks a new PID
    monkeypatch.setattr(kwt, "_dashboard_service_pid", lambda: next(pids))

    ok, output, meta = kwt._default_release_gate_activation()

    assert ok is True
    assert ran["argv"][0] == "bash"
    assert ran["argv"][1] == str(deploy)  # canonical deploy script, not a bare build
    assert meta == {
        "pre_pid": 54321,
        "post_pid": 67890,
        "deploy_exit": 0,
        "deployed_sha": "a" * 40,
        "running_sha": "a" * 40,
    }


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
    monkeypatch.setattr(kwt, "_git", lambda *args, **kwargs: "a" * 40)
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


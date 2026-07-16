"""Kanban worktrees tests: complete park.

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


def _provisioned_task(conn, repo, *, title="iso task"):
    tid = kb.create_task(
        conn, title=title, assignee="coder",
        workspace_kind="dir", workspace_path=str(repo),
    )
    task = kb.claim_task(conn, tid)
    ws = kwt.provision_for_task(conn, task, str(repo))
    return tid, ws


def _age_integration_retry_events(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?",
            (kb.INTEGRATION_RETRY_BACKOFF_SECONDS + 5, task_id),
        )


def test_complete_task_blocks_when_isolated_integration_hook_crashes(
    kanban_home, tmp_path, monkeypatch
):
    def crash_hook(conn, task_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(kwt, "maybe_integrate_on_complete", crash_hook)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="isolated hook crash",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(tmp_path / "isolated"),
        )
        assert kb.claim_task(conn, task_id) is not None

        assert kb.complete_task(conn, task_id, result="done")

        task = kb.get_task(conn, task_id)
        blocked_events = _events(conn, task_id, "blocked")
    assert task is not None and task.status == "blocked"
    assert blocked_events[-1]["reason"] == (
        "integration parked: integration_hook_failed"
    )


def test_complete_task_nonisolated_ignores_integration_hook(
    kanban_home, monkeypatch
):
    def crash_hook(conn, task_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(kwt, "maybe_integrate_on_complete", crash_hook)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="non-isolated completion",
            assignee="coder",
        )
        assert kb.claim_task(conn, task_id) is not None

        assert kb.complete_task(conn, task_id, result="done")

        task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "done"


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


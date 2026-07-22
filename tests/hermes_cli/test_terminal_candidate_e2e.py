from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as worktrees
from hermes_cli import terminal_candidates as candidates


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True,
    )
    return proc.stdout.strip()


def _candidate(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    repo = tmp_path / "repo-b"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "e2e@example.test")
    _git(repo, "config", "user.name", "E2E")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    source = repo / ".worktrees" / "terminal" / "tr_e2e"
    source.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", "terminal/tr_e2e", str(source), base)
    (source / "candidate.txt").write_text("candidate\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "candidate")
    candidate = _git(source, "rev-parse", "HEAD")
    runs = tmp_path / "home" / "terminal-runs"
    run_dir = runs / "tr_e2e"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema_version": 2, "run_id": "tr_e2e", "correlation_id": "corr_e2e",
        "start_mode": "isolated_write", "repo_root": str(repo),
        "worktree_path": str(source), "branch": "terminal/tr_e2e", "base_sha": base,
    }))
    return repo, source, runs, base, candidate


def test_submit_imports_into_one_held_chain_without_touching_live_or_source(tmp_path, monkeypatch):
    repo, source, runs, base, candidate = _candidate(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(kb, "validate_spawnable_assignee", lambda value: value)
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        result = candidates.submit_terminal_candidate(
            conn, terminal_run_id="tr_e2e", correlation_id="corr_e2e",
            candidate_sha=candidate, terminal_runs_dir=runs,
            repo_allowlist=[str(repo)], enabled=True, intake_assignee="premium",
        )
        assert _git(repo, "rev-parse", "main") == base
        assert _git(source, "rev-parse", "HEAD") == candidate
        assert (Path(result.workspace_path) / "candidate.txt").read_text() == "candidate\n"
        root = kb.get_task(conn, result.root_task_id)
        intake = kb.get_task(conn, result.intake_task_id)
        assert root.status == "scheduled"
        assert intake.status == "scheduled"
        assert root.freigabe == "operator"
        assert root.live_test_depth == "contract"
        assert root.workspace_path == result.workspace_path
        assert intake.workspace_path == result.workspace_path
        assert conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id=? AND kind='decomposed'",
            (result.root_task_id,),
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id=? AND kind='worktree_provisioned'",
            (result.root_task_id,),
        ).fetchone()["n"] == 1
        attachment = conn.execute(
            "SELECT immutable FROM task_attachments WHERE task_id=? AND artifact_kind='terminal_candidate_capsule'",
            (result.root_task_id,),
        ).fetchone()
        assert attachment["immutable"] == 1
        again = candidates.submit_terminal_candidate(
            conn, terminal_run_id="tr_e2e", correlation_id="corr_e2e",
            candidate_sha=candidate, terminal_runs_dir=runs,
            repo_allowlist=[str(repo)], enabled=True, intake_assignee="premium",
        )
        assert again.idempotent is True
        assert again.root_task_id == result.root_task_id
        assert conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 2
    finally:
        conn.close()


def test_busy_repo_lock_fails_before_board_or_git_mutation(tmp_path, monkeypatch):
    repo, source, runs, base, candidate = _candidate(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = kb.connect(tmp_path / "kanban.db")
    held = worktrees._acquire_file_lock(worktrees._integrator_lock_path(repo))
    try:
        with pytest.raises(candidates.CandidateBusyError, match="retry"):
            candidates.submit_terminal_candidate(
                conn, terminal_run_id="tr_e2e", correlation_id="corr_e2e",
                candidate_sha=candidate, terminal_runs_dir=runs,
                repo_allowlist=[str(repo)], enabled=True, intake_assignee="premium",
            )
        assert conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 0
        assert _git(repo, "rev-parse", "main") == base
        assert _git(source, "rev-parse", "HEAD") == candidate
    finally:
        worktrees._release_file_lock(held)
        conn.close()


def test_conflict_aborts_cleanly_and_retry_reuses_the_same_held_chain(tmp_path, monkeypatch):
    repo, _source, runs, _base, candidate = _candidate(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(kb, "validate_spawnable_assignee", lambda value: value)
    (repo / "candidate.txt").write_text("conflicting live change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "conflicting target change")
    live_head = _git(repo, "rev-parse", "HEAD")
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(candidates.CandidateImportError) as first:
            candidates.submit_terminal_candidate(
                conn, terminal_run_id="tr_e2e", correlation_id="corr_e2e",
                candidate_sha=candidate, terminal_runs_dir=runs,
                repo_allowlist=[str(repo)], enabled=True, intake_assignee="premium",
            )
        assert first.value.recovery_required is False
        tasks = conn.execute(
            "SELECT id, workspace_path FROM tasks ORDER BY created_at, id",
        ).fetchall()
        assert len(tasks) == 2
        root = conn.execute(
            "SELECT task_id FROM task_events WHERE kind='terminal_candidate_pending'",
        ).fetchone()["task_id"]
        workspace = Path(kb.get_task(conn, root).workspace_path)
        assert _git(repo, "rev-parse", "HEAD") == live_head
        assert _git(workspace, "rev-parse", "HEAD") == live_head
        assert _git(workspace, "status", "--porcelain=v1", "--untracked-files=all") == ""
        marker = subprocess.run(
            ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"],
            cwd=workspace, text=True, capture_output=True,
        )
        assert marker.returncode != 0
        assert conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id=? "
            "AND kind='terminal_candidate_import_failed'",
            (root,),
        ).fetchone()["n"] == 1

        with pytest.raises(candidates.CandidateImportError):
            candidates.submit_terminal_candidate(
                conn, terminal_run_id="tr_e2e", correlation_id="corr_e2e",
                candidate_sha=candidate, terminal_runs_dir=runs,
                repo_allowlist=[str(repo)], enabled=True, intake_assignee="premium",
            )
        assert conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 2
    finally:
        conn.close()

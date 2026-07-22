from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import terminal_candidates as candidates


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=check,
    )
    return proc.stdout.strip()


@pytest.fixture()
def isolated_candidate(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "candidate@example.test")
    _git(repo, "config", "user.name", "Candidate")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    terminal_root = repo / ".worktrees" / "terminal"
    terminal_root.mkdir(parents=True)
    source = terminal_root / "tr_candidate"
    _git(repo, "worktree", "add", "-b", "terminal/tr_candidate", str(source), base_sha)
    (source / "candidate.txt").write_text("candidate\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "candidate")
    candidate_sha = _git(source, "rev-parse", "HEAD")
    runs_root = tmp_path / "terminal-runs"
    run_dir = runs_root / "tr_candidate"
    run_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 2, "run_id": "tr_candidate",
        "correlation_id": "corr_candidate", "start_mode": "isolated_write",
        "repo_root": str(repo), "worktree_path": str(source),
        "branch": "terminal/tr_candidate", "base_sha": base_sha,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return repo, source, runs_root, base_sha, candidate_sha


def test_preflight_accepts_clean_linear_isolated_write_candidate(isolated_candidate):
    repo, source, runs_root, base_sha, candidate_sha = isolated_candidate
    result = candidates.preflight_terminal_candidate(
        terminal_run_id="tr_candidate", correlation_id="corr_candidate",
        candidate_sha=candidate_sha, terminal_runs_dir=runs_root,
        repo_allowlist=[str(repo)],
    )
    assert result.repo_root == repo.resolve()
    assert result.source_worktree == source.resolve()
    assert result.base_sha == base_sha
    assert result.candidate_sha == candidate_sha
    assert result.commits == (candidate_sha,)


@pytest.mark.parametrize("mutation", ["dirty", "untracked", "merge", "detached"])
def test_preflight_rejects_invalid_candidate_without_mutation(isolated_candidate, mutation):
    repo, source, runs_root, _base_sha, candidate_sha = isolated_candidate
    if mutation == "dirty":
        (source / "candidate.txt").write_text("dirty\n")
    elif mutation == "untracked":
        (source / "untracked.txt").write_text("nope\n")
    elif mutation == "detached":
        _git(source, "checkout", "--detach", candidate_sha)
    else:
        _git(source, "checkout", "-b", "side", "HEAD~1")
        (source / "side.txt").write_text("side\n")
        _git(source, "add", "-A")
        _git(source, "commit", "-m", "side")
        _git(source, "checkout", "terminal/tr_candidate")
        _git(source, "merge", "--no-ff", "side", "-m", "merge")
        candidate_sha = _git(source, "rev-parse", "HEAD")
    with pytest.raises(candidates.CandidatePreflightError):
        candidates.preflight_terminal_candidate(
            terminal_run_id="tr_candidate", correlation_id="corr_candidate",
            candidate_sha=candidate_sha, terminal_runs_dir=runs_root,
            repo_allowlist=[str(repo)],
        )


def test_preflight_rejects_repo_outside_allowlist(isolated_candidate, tmp_path):
    _repo, _source, runs_root, _base_sha, candidate_sha = isolated_candidate
    with pytest.raises(candidates.CandidatePreflightError, match="allowlist"):
        candidates.preflight_terminal_candidate(
            terminal_run_id="tr_candidate", correlation_id="corr_candidate",
            candidate_sha=candidate_sha, terminal_runs_dir=runs_root,
            repo_allowlist=[str(tmp_path / "other")],
        )


def test_candidate_root_and_pending_marker_commit_atomically(tmp_path, monkeypatch):
    db_path = tmp_path / "board.db"
    conn = kb.connect(db_path)
    observer = kb.connect(db_path)
    original_append = kb._append_event

    def fail_pending(*args, **kwargs):
        if args[2] == "terminal_candidate_pending":
            assert observer.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 0
            raise RuntimeError("injected before commit")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_pending)
    with pytest.raises(RuntimeError, match="injected"):
        kb.create_held_decompose_root(
            conn,
            title="candidate",
            assignee=None,
            created_by="test",
            hold_reason="Candidate: held before release",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "repo-b"),
            idempotency_key="terminal-candidate:corr:sha",
            root_kind="terminal_candidate",
            correlation_id="corr",
            freigabe="operator",
            live_test_depth="contract",
            initial_event_kind="terminal_candidate_pending",
            initial_event_payload={"repo_root": str(tmp_path / "repo-b")},
        )
    assert observer.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 0

    monkeypatch.setattr(kb, "_append_event", original_append)
    root_id = kb.create_held_decompose_root(
        conn,
        title="candidate", assignee=None, created_by="test",
        hold_reason="Candidate: held before release",
        workspace_kind="worktree", workspace_path=str(tmp_path / "repo-b"),
        idempotency_key="terminal-candidate:corr:sha", root_kind="terminal_candidate",
        correlation_id="corr", freigabe="operator", live_test_depth="contract",
        initial_event_kind="terminal_candidate_pending",
        initial_event_payload={"repo_root": str(tmp_path / "repo-b")},
    )
    visible = observer.execute(
        "SELECT status, workspace_path FROM tasks WHERE id=?", (root_id,),
    ).fetchone()
    assert dict(visible) == {"status": "scheduled", "workspace_path": str(tmp_path / "repo-b")}
    assert observer.execute(
        "SELECT count(*) AS n FROM task_events WHERE task_id=? AND kind='terminal_candidate_pending'",
        (root_id,),
    ).fetchone()["n"] == 1
    observer.close()
    conn.close()

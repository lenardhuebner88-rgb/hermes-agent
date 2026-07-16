"""Write-time carry of diff_text on re-submit after block→promote→claim.

Production path: coder submits with a real B1 snapshot (incl. diff_text),
reviewer blocks, operator promotes, coder reclaims (new pre_run_commit_sha).
When the reclaimed run has no new net-diff against the new baseline, the
fresh capture is empty and the new submitted_for_review event must still
persist the predecessor's diff_text at write time — not only changed_files
/diff_stat.

Mirrors the stage-advance carry (2b6dedee2) for the re-submit path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not installed")


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo_with_baseline(repo: Path) -> Path:
    """Real git workspace: one committed baseline file (production shape)."""
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "t@t")
    _run_git(repo, "config", "user.name", "t")
    target = repo / "tracked.py"
    target.write_text("original = 1\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.py")
    _run_git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")
    return target


def _submitted_payloads(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'submitted_for_review' "
        "ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else None
        if isinstance(payload, dict):
            out.append(payload)
    return out


@requires_git
def test_resubmit_after_block_lifecycle_carries_diff_text_write_time(
    kanban_home, tmp_path
):
    """claim → submit (real snapshot) → review-block → promote → reclaim →
    resubmit with empty fresh capture must write-time-carry diff_text."""
    repo = tmp_path / "ws"
    target = _init_repo_with_baseline(repo)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="carry diff_text on resubmit",
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run1 = claimed.current_run_id
        assert run1 is not None

        # Coder finishes with a committed change (production: commit-then-complete).
        # Capture baselines against pre_run_commit_sha from claim_task.
        target.write_text("original = 2\n", encoding="utf-8")
        _run_git(repo, "add", "tracked.py")
        _run_git(
            repo,
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "coder fix",
        )

        assert kb._submit_for_review(
            conn,
            tid,
            result="done",
            summary="first submit with real snapshot",
            metadata=None,
            verified_cards=[],
            expected_run_id=run1,
        )

        first_payloads = _submitted_payloads(conn, tid)
        assert len(first_payloads) == 1
        first = first_payloads[0]
        assert "tracked.py" in (first.get("changed_files") or [])
        assert first.get("diff_stat")
        first_diff_text = first.get("diff_text")
        assert isinstance(first_diff_text, str) and first_diff_text.strip()
        assert "original = 2" in first_diff_text or "+original = 2" in first_diff_text

        # Reviewer claims and BLOCKS (REQUEST_CHANGES path).
        assert kb.claim_review_task(conn, tid) is not None
        review_run = kb.get_task(conn, tid).current_run_id
        assert kb.block_task(
            conn,
            tid,
            reason="needs regression for the changed symbol",
            expected_run_id=review_run,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        ok, msg = kb.promote_task(conn, tid, actor="test", reason="retry after block")
        assert ok, msg
        assert kb.get_task(conn, tid).status == "ready"

        # Reclaim: new run, new pre_run_commit_sha (= HEAD after the coder commit).
        claimed2 = kb.claim_task(conn, tid)
        assert claimed2 is not None
        run2 = claimed2.current_run_id
        assert run2 is not None and run2 != run1

        # Clean workspace vs new baseline → empty net-diff (the production case).
        fresh = kb._capture_review_diff_snapshot(conn, tid, expected_run_id=run2)
        assert not fresh.get("changed_files")
        assert not fresh.get("diff_stat")
        assert not fresh.get("diff_text")

        assert kb._submit_for_review(
            conn,
            tid,
            result="done",
            summary="resubmit after promote — no new net-diff",
            metadata=None,
            verified_cards=[],
            expected_run_id=run2,
        )

        payloads = _submitted_payloads(conn, tid)
        assert len(payloads) == 2
        second = payloads[-1]

        # Write-time carry: new event itself holds the predecessor's diff_text.
        assert second.get("diff_text") == first_diff_text
        assert second.get("changed_files") == first.get("changed_files")
        assert second.get("diff_stat") == first.get("diff_stat")

        # Walk-back still surfaces non-empty diff_text (bestandsverhalten).
        _changed, _stat, walk_text = kb._latest_review_diff_snapshot(conn, tid)
        assert isinstance(walk_text, str) and walk_text.strip()
        assert walk_text == first_diff_text or first_diff_text in walk_text


@requires_git
def test_resubmit_fresh_diff_wins_over_stale_carried_diff_text(kanban_home, tmp_path):
    """When the reclaimed run produces a real net-diff, do NOT overwrite it
    with the predecessor's stale diff_text."""
    repo = tmp_path / "ws"
    target = _init_repo_with_baseline(repo)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="fresh diff wins",
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run1 = claimed.current_run_id

        target.write_text("original = 2\n", encoding="utf-8")
        _run_git(repo, "add", "tracked.py")
        _run_git(
            repo,
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "first change",
        )
        assert kb._submit_for_review(
            conn,
            tid,
            result="done",
            summary="first",
            metadata=None,
            verified_cards=[],
            expected_run_id=run1,
        )
        first_diff = _submitted_payloads(conn, tid)[-1].get("diff_text")
        assert isinstance(first_diff, str) and first_diff.strip()

        assert kb.claim_review_task(conn, tid) is not None
        review_run = kb.get_task(conn, tid).current_run_id
        assert kb.block_task(
            conn, tid, reason="still broken", expected_run_id=review_run
        )
        ok, msg = kb.promote_task(conn, tid, actor="test")
        assert ok, msg

        claimed2 = kb.claim_task(conn, tid)
        assert claimed2 is not None
        run2 = claimed2.current_run_id

        # New net-diff on the reclaimed run (fresh capture must win).
        target.write_text("original = 3\n", encoding="utf-8")
        _run_git(repo, "add", "tracked.py")
        _run_git(
            repo,
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "second change",
        )

        assert kb._submit_for_review(
            conn,
            tid,
            result="done",
            summary="resubmit with real new diff",
            metadata=None,
            verified_cards=[],
            expected_run_id=run2,
        )
        second = _submitted_payloads(conn, tid)[-1]
        second_diff = second.get("diff_text")
        assert isinstance(second_diff, str) and second_diff.strip()
        # Fresh capture wins — not the stale predecessor text.
        assert second_diff != first_diff
        assert "original = 3" in second_diff or "+original = 3" in second_diff
        assert "original = 2" not in second_diff or "+original = 3" in second_diff

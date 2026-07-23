"""Kanban DB tests: review.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

from tests.hermes_cli._kanban_test_helpers import (
    _set_task_status,
    _latest_run_verdict,
)

import shutil as _shutil  # noqa: E402

_GIT = _shutil.which("git")


requires_git = pytest.mark.skipif(_GIT is None, reason="git not installed")


def _init_git_repo_with_changes(path: Path) -> None:
    """Init a git repo at *path* with one committed file modified + one
    untracked file, so ``status --porcelain`` and ``diff --stat`` both report."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    run("init")
    (path / "tracked.py").write_text("original = 1\n", encoding="utf-8")
    run("add", "tracked.py")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")
    # Modify the tracked file (→ diff --stat) and add an untracked one (→ porcelain).
    (path / "tracked.py").write_text("original = 2\n", encoding="utf-8")
    (path / "untracked.py").write_text("brand_new = True\n", encoding="utf-8")


def _frontmatter_dict(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    for line in lines[1:end]:
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def _write_fo_backlog_item(path: Path, *, status: str = "next") -> None:
    path.write_text(
        "---\n"
        "id: 0141\n"
        "title: Shopping-Favoriten Chips\n"
        f"status: {status}\n"
        "owner: hermes\n"
        "risk: medium\n"
        "area: shopping\n"
        "updated: 2026-06-01\n"
        "---\n\n"
        "## Kontext\n\n"
        "Analog zu FO Beispiel 141.\n",
        encoding="utf-8",
    )


def _claimed_review_section(conn, *, kind=None, acceptance=None):
    """Create a task (optionally kind-marked), drive it into the review lane,
    claim it as the verifier, and return its rendered review-section as text."""
    t = kb.create_task(conn, title="probe", assignee="coder-claude", kind=kind)
    if acceptance is not None:
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(acceptance), t),
        )
    _set_task_status(conn, t, "review")
    assert kb.claim_review_task(conn, t) is not None
    return t, "\n".join(kb._render_review_verifier_section(conn, t))


@requires_git
def test_b1_capture_diff_snapshot_git_workspace(kanban_home, tmp_path):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", workspace_kind="dir", workspace_path=str(repo)
        )
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert set(snap.get("changed_files", [])) == {"tracked.py", "untracked.py"}
    assert "tracked.py" in snap.get("diff_stat", "")


def test_b1_capture_diff_snapshot_non_git_scratch(kanban_home, tmp_path):
    """A plain (non-git) workspace yields an empty snapshot, never a crash."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "file.txt").write_text("hi", encoding="utf-8")
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", workspace_kind="dir", workspace_path=str(scratch)
        )
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert snap == {}


def test_b1_capture_diff_snapshot_missing_workspace(kanban_home, tmp_path):
    """workspace_path pointing at a vanished directory → empty, no crash."""
    gone = tmp_path / "gone"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", workspace_kind="dir", workspace_path=str(gone)
        )
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert snap == {}


def test_b1_capture_diff_snapshot_no_workspace(kanban_home):
    """A scratch task with no workspace_path → empty snapshot."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="b1")
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert snap == {}


@requires_git
def test_b1_submit_for_review_event_and_metadata_carry_snapshot(
    kanban_home, tmp_path
):
    import json
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        ok = kb._submit_for_review(
            conn, tid, result="done", summary="all done",
            metadata={"artifacts": ["tracked.py"]}, verified_cards=[],
            expected_run_id=None,
        )
        assert ok is True
        ev = [
            e for e in kb.list_events(conn, tid)
            if e.kind == "submitted_for_review"
        ]
        assert len(ev) == 1
        payload = ev[0].payload
        # Additive snapshot keys present...
        assert set(payload["changed_files"]) == {"tracked.py", "untracked.py"}
        assert "tracked.py" in payload["diff_stat"]
        # ...and the pre-existing keys are untouched (byte-identical contract).
        assert payload["result_len"] == len("done")
        assert payload["summary"] == "all done"
        assert payload["artifacts"] == ["tracked.py"]
        # Snapshot also rides the run metadata.
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        md = json.loads(row["metadata"])
        assert set(md["changed_files"]) == {"tracked.py", "untracked.py"}


def test_b1_submit_for_review_non_git_payload_has_no_snapshot_keys(
    kanban_home, tmp_path
):
    """Regression guard: with no git workspace, the event payload carries NONE
    of the new keys — the pre-B1 shape is preserved exactly."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", assignee="coder",
            workspace_kind="dir", workspace_path=str(scratch),
            initial_status="running",
        )
        kb._submit_for_review(
            conn, tid, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        ev = [
            e for e in kb.list_events(conn, tid)
            if e.kind == "submitted_for_review"
        ]
        assert len(ev) == 1
        assert "changed_files" not in ev[0].payload
        assert "diff_stat" not in ev[0].payload


def test_review_diff_snapshot_walks_back_and_resubmit_carries_snapshot(
    kanban_home, tmp_path
):
    """A vanished workspace must not erase prior diff evidence for review."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot = {
        "changed_files": ["hermes_cli/kanban_db.py"],
        "diff_stat": " hermes_cli/kanban_db.py | 12 ++++++++++++\n",
        "diff_base_commit": "deadbeef",
        "diff_baseline": "pre_run_commit",
    }
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="carry prior diff",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(scratch),
            initial_status="running",
        )
        kb.add_event(conn, tid, "submitted_for_review", snapshot)
        assert kb._submit_for_review(
            conn,
            tid,
            result="done",
            summary="resubmit after workspace vanished",
            metadata=None,
            verified_cards=[],
            expected_run_id=None,
        )

        events = [
            event
            for event in kb.list_events(conn, tid)
            if event.kind == "submitted_for_review"
        ]
        payload = events[-1].payload
        assert payload is not None
        assert payload["changed_files"] == snapshot["changed_files"]
        assert payload["diff_stat"] == snapshot["diff_stat"]
        assert payload["diff_base_commit"] == snapshot["diff_base_commit"]
        assert payload["diff_baseline"] == snapshot["diff_baseline"]

        kb.add_event(
            conn,
            tid,
            "submitted_for_review",
            {"review_stage": 1, "target_profile": "critical"},
        )
        _set_task_status(conn, tid, "review")
        assert kb.claim_review_task(conn, tid) is not None
        section = "\n".join(kb._render_review_verifier_section(conn, tid))

    assert "`hermes_cli/kanban_db.py`" in section
    assert snapshot["diff_stat"].strip() in section
    assert "No machine diff snapshot was captured" not in section


def test_parent_context_uses_skipped_review_diff_snapshot(kanban_home):
    """A PlanSpec reviewer child can inspect a deterministic-skip diff snapshot."""
    snapshot = {
        "changed_files": ["hermes_cli/kanban_db.py"],
        "diff_stat": " hermes_cli/kanban_db.py | 12 ++++++++++++\n",
        "diff_base_commit": "deadbeef",
        "diff_baseline": "pre_run_commit",
        "commit_sha": "cafebabe",
        "branch": "kanban/code-slice",
    }
    with kb.connect_closing() as conn:
        code_task = kb.create_task(
            conn, title="code slice", assignee="coder", initial_status="running"
        )
        reviewer_child = kb.create_task(
            conn, title="PlanSpec reviewer", assignee="reviewer", initial_status="running"
        )
        kb.link_tasks(conn, code_task, reviewer_child)
        # Build the persisted event stream that an economy-mode completion leaves:
        # no submitted_for_review event, only its deterministic skip + snapshot.
        kb.add_event(conn, code_task, "review_diff_snapshot", snapshot)
        kb.add_event(
            conn,
            code_task,
            "review_skipped_deterministic",
            {"worker_gate": {"status": "green"}, "tier": "standard"},
        )
        _set_task_status(conn, code_task, "done")

        context = kb.build_worker_context(conn, reviewer_child)
        submitted = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? "
            "AND kind = 'submitted_for_review'",
            (code_task,),
        ).fetchone()

    assert submitted is None
    assert "hermes_cli/kanban_db.py" in context
    assert snapshot["diff_stat"].strip() in context


@requires_git
def test_deterministic_skip_persists_captured_diff_snapshot(
    kanban_home, tmp_path, monkeypatch
):
    """Completion captures diff evidence even when it bypasses verifier submit."""
    repo = tmp_path / "skip-workspace"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "standard_uses_llm_verifier": False,
        },
    )
    monkeypatch.setattr(
        kb,
        "_worker_gate_config",
        lambda: {
            "enabled": True,
            "repos": {str(repo.resolve()): ["true"]},
            "default": [],
            "timeout": 60,
            "code_roles": frozenset({"coder"}),
        },
    )
    with kb.connect_closing() as conn:
        code_task = kb.create_task(
            conn,
            title="code slice",
            assignee="coder",
            workspace_path=str(repo),
            initial_status="running",
        )
        assert kb.claim_task(conn, code_task) is not None
        assert kb.complete_task(conn, code_task, summary="done", review_gate=True)
        snapshot_row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'review_diff_snapshot'",
            (code_task,),
        ).fetchone()

    assert snapshot_row is not None
    snapshot = json.loads(snapshot_row["payload"])
    assert {"changed_files", "diff_stat", "diff_base_commit", "diff_baseline"} <= snapshot.keys()
    assert {"tracked.py", "untracked.py"} <= set(snapshot["changed_files"])

@requires_git
def test_review_context_includes_bounded_unified_submit_diff(kanban_home, tmp_path):
    """A real submit event exposes its unified diff, not only the stat."""
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="inline diff",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
            initial_status="running",
        )
        assert kb._submit_for_review(
            conn,
            task_id,
            result="done",
            summary="done",
            metadata=None,
            verified_cards=[],
            expected_run_id=None,
        )
        event = next(
            e
            for e in kb.list_events(conn, task_id)
            if e.kind == "submitted_for_review"
        )
        assert "diff_text" in event.payload
        assert "-original = 1" in event.payload["diff_text"]
        assert "+original = 2" in event.payload["diff_text"]
        assert kb.claim_review_task(conn, task_id) is not None
        context = kb.build_worker_context(conn, task_id)

    assert "## Unified diff at submit (bounded)" in context
    assert "-original = 1" in context
    assert "MANDATORY: for any CHANGED existing symbol" in context


def test_review_context_without_diff_uses_capability_aware_clause(kanban_home):
    """No event and no workspace tells reviewers to request evidence, not block."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="no source", assignee="coder")
        _set_task_status(conn, task_id, "review")
        assert kb.claim_review_task(conn, task_id) is not None
        context = kb.build_worker_context(conn, task_id)

    assert "Kein Diff-Zugriff in diesem Run" in context
    assert "NEEDS_MORE_CONTEXT statt als Blocker" in context
    assert "MANDATORY: for any CHANGED existing symbol" not in context


@requires_git
def test_reviewer_child_receives_parent_unified_diff(kanban_home, tmp_path):
    """A reviewer child gets the coder parent's event-backed unified diff."""
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(
            conn,
            title="code slice",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
            initial_status="running",
        )
        child_id = kb.create_task(
            conn, title="review child", assignee="reviewer", initial_status="running"
        )
        kb.link_tasks(conn, parent_id, child_id)
        assert kb._submit_for_review(
            conn,
            parent_id,
            result="done",
            summary="done",
            metadata=None,
            verified_cards=[],
            expected_run_id=None,
        )
        _set_task_status(conn, parent_id, "done")
        context = kb.build_worker_context(conn, child_id)

    assert "_review unified diff (bounded)_:" in context
    assert "-original = 1" in context
    assert "+original = 2" in context


def test_bounded_unified_diff_marks_per_file_and_global_truncation():
    """Both hard caps leave explicit evidence rather than silently slicing."""
    text = "".join(
        f"diff --git a/file{idx}.py b/file{idx}.py\n" + ("+x\n" * 120)
        for idx in range(8)
    )
    bounded = kb._bounded_review_diff_text(text)
    assert len(bounded.encode("utf-8")) <= kb._DIFF_SNAPSHOT_TEXT_BYTE_CAP
    assert len(bounded.splitlines()) <= kb._DIFF_SNAPSHOT_TEXT_LINE_CAP
    assert "per-file cap" in bounded
    assert "global cap" in bounded


def test_b2_verdict_column_present_and_migrate_idempotently(kanban_home):
    """task_runs gains a ``verdict`` column; re-running the additive migration
    is a no-op (idempotent, no duplicate-column crash)."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "verdict" in cols
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")]
        assert cols2.count("verdict") == 1


def test_b2_approved_verdict_on_review_complete(kanban_home):
    """A verifier completing a task it reviewed → verdict APPROVED."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.complete_task(
            conn,
            t,
            result="lgtm",
            summary="lgtm",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )
        assert ok is True
        assert _latest_run_verdict(conn, t) == "APPROVED"


def test_b2_request_changes_verdict_on_review_block(kanban_home):
    """A verifier blocking a task it reviewed → verdict REQUEST_CHANGES."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.block_task(conn, t, reason="missing tests")
        assert ok is True
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"


def test_b2_review_complete_rejects_free_text_verdict(kanban_home):
    """A verdict in prose cannot authorize a review transition."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        with pytest.raises(kb.ReviewVerdictRequiredError):
            kb.complete_task(
                conn,
                t,
                result="reviewed",
                summary="Verdict: NEEDS_REVISION",
                review_gate=True,
            )
        assert kb.get_task(conn, t).status == "running"
        assert _latest_run_verdict(conn, t) is None


def test_b2_review_complete_extracts_metadata_verdict_synonym(kanban_home):
    """Structured reviewer metadata is normalized before column write."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.complete_task(
            conn,
            t,
            result="reviewed",
            summary="done",
            metadata={"review_verdict": "changes-requested"},
            review_gate=True,
        )
        assert ok is True
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"
        assert kb.get_task(conn, t).status == "blocked"


def test_b2_review_block_extracts_metadata_verdict_synonym(kanban_home):
    """Block path uses the same reviewer verdict normalization."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.block_task(
            conn,
            t,
            reason="blocking after review",
            reviewer_metadata={"review": {"verdict": "needs revision"}},
        )
        assert ok is True
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"


def test_b2_set_run_verdict_requires_existing_run_row(kanban_home):
    """The verdict update is atomic: exactly one task_runs row must change."""
    with kb.connect_closing() as conn:
        assert kb._set_run_verdict(conn, 999_999_999, "APPROVED") is False


def test_b2_explicit_approved_not_overwritten_by_later_verdict(kanban_home, monkeypatch):
    """The first structured run verdict remains immutable."""
    monkeypatch.setattr(
        kb,
        "_review_stages_for_tier",
        lambda tier, cfg: ["verifier", "critic"],
    )
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder", review_tier="critical")
        assert kb._submit_for_review(
            conn,
            t,
            verified_cards=[],
            target_profile="verifier",
            stage=0,
            effective_tier="critical",
            result=None,
            summary=None,
            metadata=None,
            expected_run_id=None,
        )
        assert kb.claim_review_task(conn, t, reviewer_profile="verifier") is not None
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()["current_run_id"]

        assert kb.complete_task(
            conn,
            t,
            summary="verifier approved",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        ) is True
        assert _latest_run_verdict(conn, t) == "APPROVED"

        assert kb._set_run_verdict(conn, run_id, "REQUEST_CHANGES") is False
        assert _latest_run_verdict(conn, t) == "APPROVED"


@requires_git
def test_stage_advance_carries_diff_snapshot_to_next_reviewer(kanban_home, tmp_path):
    """Regression: the B1 diff snapshot captured at the coder→verifier handoff
    must survive `_maybe_advance_review_chain`'s stage-advance event, so the
    reviewer stage (stage 1) still sees the changed-files evidence instead of
    the 'No machine diff snapshot' fallback. Before the fix, the stage-advance
    event dropped changed_files/diff_stat and the reviewer's context regressed
    to the no-snapshot fallback (infinite bounce loop bug).

    Uses the ``critical`` tier because it is the two-stage tier (verifier →
    reviewer) under the review-economy topology; ``review`` is single-stage
    (verifier only) and never advances to a reviewer stage."""
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="widget", assignee="coder", review_tier="critical",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        # Coder submits → real B1 snapshot rides the FIRST submitted_for_review
        # event (stage 0, verifier).
        assert kb._submit_for_review(
            conn, t, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )

        # Verifier (stage 0) claims and APPROVES → chain advances to stage 1
        # (reviewer) via _maybe_advance_review_chain, appending a SECOND,
        # newer submitted_for_review event.
        assert kb.claim_review_task(conn, t) is not None
        assert kb.complete_task(
            conn, t, result="lgtm", summary="lgtm",
            metadata={"review_verdict": "APPROVED"}, review_gate=True,
        ) is True
        stage_one_payload = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()["payload"]

        # Reviewer (stage 1) claims — its context must read the CARRIED
        # snapshot from the newest submitted_for_review event.
        assert kb.claim_review_task(conn, t, reviewer_profile="reviewer") is not None
        ctx = kb.build_worker_context(conn, t)
    assert "Changed files at submit" in ctx
    assert "tracked.py" in ctx
    assert "diff_text" in stage_one_payload
    assert "```diff" in ctx
    assert "+original = 2" in ctx
    assert "MANDATORY: for any CHANGED existing symbol" in ctx
    assert "No machine diff snapshot" not in ctx


@requires_git
def test_stage_resubmit_after_history_rewrite_recaptures_full_main_diff(
    kanban_home, tmp_path
):
    repo = tmp_path / "rewritten-workspace"
    repo.mkdir()

    def run_git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc.stdout.strip()

    run_git("init", "-b", "main")
    run_git("config", "user.email", "t@t")
    run_git("config", "user.name", "t")
    (repo / "base.py").write_text("base = True\n", encoding="utf-8")
    run_git("add", "base.py")
    run_git("commit", "-m", "main base")
    main_commit = run_git("rev-parse", "HEAD")
    run_git("switch", "-c", "work")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="history rewrite review",
            assignee="coder",
            kind="code",
            review_tier="critical",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        first_claim = kb.claim_task(conn, task_id)
        assert first_claim is not None
        first_run = first_claim.current_run_id
        assert first_run is not None

        (repo / "old_wave.py").write_text("old = True\n", encoding="utf-8")
        run_git("add", "old_wave.py")
        run_git("commit", "-m", "old candidate")
        old_candidate = run_git("rev-parse", "HEAD")
        assert kb._submit_for_review(
            conn,
            task_id,
            result="old done",
            summary="old candidate",
            metadata=None,
            verified_cards=[],
            expected_run_id=first_run,
        )

        assert kb.claim_review_task(conn, task_id) is not None
        review_run = kb.get_task(conn, task_id).current_run_id
        assert kb.block_task(
            conn,
            task_id,
            reason="revision required",
            expected_run_id=review_run,
        )
        promoted, message = kb.promote_task(conn, task_id, actor="test")
        assert promoted, message
        second_claim = kb.claim_task(conn, task_id)
        assert second_claim is not None
        second_run = second_claim.current_run_id
        assert second_run is not None

        run_git("reset", "--hard", "main")
        (repo / "new_alpha.py").write_text("alpha = True\n", encoding="utf-8")
        (repo / "new_beta.py").write_text("beta = True\n", encoding="utf-8")
        run_git("add", "new_alpha.py", "new_beta.py")
        run_git("commit", "-m", "rebased candidate")
        new_candidate = run_git("rev-parse", "HEAD")
        assert new_candidate != old_candidate
        assert run_git("merge-base", "--is-ancestor", "main", "HEAD") == ""

        assert kb._submit_for_review(
            conn,
            task_id,
            result="new done",
            summary="rewritten candidate",
            metadata=None,
            verified_cards=[],
            expected_run_id=second_run,
            stage=1,
            effective_tier="critical",
        )
        payload = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ][-1]

    assert payload is not None
    assert payload["diff_candidate_commit"] == new_candidate
    assert payload["diff_base_commit"] == main_commit
    assert payload["diff_baseline"] == "main_ancestor_commit"
    assert set(payload["changed_files"]) == {"new_alpha.py", "new_beta.py"}
    assert "old_wave.py" not in payload["diff_stat"]
    assert "new_alpha.py" in payload["diff_text"]
    assert "new_beta.py" in payload["diff_text"]


@requires_git
def test_stage_resubmit_unchanged_candidate_carries_exact_snapshot(
    kanban_home, tmp_path
):
    repo = tmp_path / "unchanged-workspace"
    repo.mkdir()
    _init_git_repo_with_changes(repo)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="unchanged review stage",
            assignee="coder",
            kind="code",
            review_tier="critical",
            workspace_kind="dir",
            workspace_path=str(repo),
            initial_status="running",
        )
        assert kb._submit_for_review(
            conn,
            task_id,
            result="done",
            summary="first stage",
            metadata=None,
            verified_cards=[],
            expected_run_id=None,
        )
        first = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ][-1]
        assert first is not None

        assert kb.claim_review_task(conn, task_id) is not None
        review_run = kb.get_task(conn, task_id).current_run_id
        assert kb._submit_for_review(
            conn,
            task_id,
            result="approved",
            summary="next stage",
            metadata=None,
            verified_cards=[],
            expected_run_id=review_run,
            stage=1,
            effective_tier="critical",
        )
        second = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ][-1]

    assert second is not None
    snapshot_keys = {
        "diff_candidate_commit",
        "diff_base_commit",
        "diff_baseline",
        "changed_files",
        "diff_stat",
        "diff_text",
        "diff_truncated",
    }
    assert {key: second.get(key) for key in snapshot_keys} == {
        key: first.get(key) for key in snapshot_keys
    }


@requires_git
def test_same_tree_squash_candidate_uses_candidate_parent_snapshot(
    kanban_home, tmp_path
):
    """A rewritten squash must not make a stale same-tree run base render empty."""
    repo = tmp_path / "same-tree-squash-workspace"
    repo.mkdir()

    def run_git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return proc.stdout.strip()

    run_git("init", "-b", "main")
    run_git("config", "user.email", "t@t")
    run_git("config", "user.name", "t")
    (repo / "base.py").write_text("base = True\n", encoding="utf-8")
    run_git("add", "base.py")
    run_git("commit", "-m", "main base")
    main_commit = run_git("rev-parse", "HEAD")
    run_git("switch", "-c", "work")
    (repo / "candidate.py").write_text("candidate = True\n", encoding="utf-8")
    run_git("add", "candidate.py")
    run_git("commit", "-m", "pre-squash candidate")
    pre_squash_tree = run_git("rev-parse", "HEAD^{tree}")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="same-tree squash review",
            assignee="coder",
            kind="code",
            review_tier="critical",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        claim = kb.claim_task(conn, task_id)
        assert claim is not None
        assert claim.current_run_id is not None

        run_git("reset", "--hard", "main")
        (repo / "candidate.py").write_text("candidate = True\n", encoding="utf-8")
        run_git("add", "candidate.py")
        run_git("commit", "-m", "squashed candidate")
        candidate = run_git("rev-parse", "HEAD")
        assert run_git("rev-parse", "HEAD^{tree}") == pre_squash_tree

        snapshot = kb._capture_review_diff_snapshot(
            conn, task_id, expected_run_id=claim.current_run_id
        )

    assert snapshot["diff_candidate_commit"] == candidate
    assert snapshot["diff_base_commit"] == main_commit
    assert snapshot["diff_baseline"] == "candidate_parent_same_tree_fallback"
    assert snapshot["changed_files"] == ["candidate.py"]
    assert "candidate.py" in snapshot["diff_text"]


@requires_git
def test_stage_resubmit_without_main_ancestor_keeps_fresh_snapshot(
    kanban_home, tmp_path
):
    repo = tmp_path / "no-main-workspace"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-M", "topic"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="fresh fallback",
            assignee="coder",
            kind="code",
            review_tier="critical",
            workspace_kind="dir",
            workspace_path=str(repo),
            initial_status="running",
        )
        assert kb._submit_for_review(
            conn,
            task_id,
            result="done",
            summary="old stage",
            metadata=None,
            verified_cards=[],
            expected_run_id=None,
        )
        first = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ][-1]
        old_candidate = first["diff_candidate_commit"]

        assert kb.claim_review_task(conn, task_id) is not None
        review_run = kb.get_task(conn, task_id).current_run_id
        assert kb.block_task(
            conn,
            task_id,
            reason="revision required",
            expected_run_id=review_run,
        )
        promoted, message = kb.promote_task(conn, task_id, actor="test")
        assert promoted, message
        second_claim = kb.claim_task(conn, task_id)
        assert second_claim is not None
        second_run = second_claim.current_run_id
        assert second_run is not None
        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "new candidate"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        new_candidate = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()

        assert kb._submit_for_review(
            conn,
            task_id,
            result="done again",
            summary="new stage",
            metadata=None,
            verified_cards=[],
            expected_run_id=second_run,
            stage=1,
            effective_tier="critical",
        )
        second = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ][-1]

    assert second is not None
    assert second["diff_candidate_commit"] == new_candidate
    assert second["diff_candidate_commit"] != old_candidate
    assert second["diff_base_commit"] == old_candidate
    assert second["diff_baseline"] == "pre_run_commit_sha"
    assert set(second["changed_files"]) == {"tracked.py", "untracked.py"}


@requires_git
def test_review_context_recaptures_missing_submit_snapshot(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    real_capture = kb._capture_review_diff_snapshot
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="widget", assignee="coder", review_tier="review",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        monkeypatch.setattr(kb, "_capture_review_diff_snapshot", lambda *_a, **_kw: {})
        assert kb._submit_for_review(
            conn, t, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        monkeypatch.setattr(kb, "_capture_review_diff_snapshot", real_capture)

        assert kb.claim_review_task(conn, t) is not None
        ctx = kb.build_worker_context(conn, t)

    assert "Changed files at submit" in ctx
    assert "tracked.py" in ctx
    assert "No machine diff snapshot" not in ctx


@requires_git
def test_review_context_missing_snapshot_and_workspace_stays_fail_soft(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    real_capture = kb._capture_review_diff_snapshot
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="widget", assignee="coder", review_tier="review",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        monkeypatch.setattr(kb, "_capture_review_diff_snapshot", lambda *_a, **_kw: {})
        assert kb._submit_for_review(
            conn, t, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        monkeypatch.setattr(kb, "_capture_review_diff_snapshot", real_capture)
        repo.rename(tmp_path / "gone")

        assert kb.claim_review_task(conn, t) is not None
        ctx = kb.build_worker_context(conn, t)

    assert "Kein Diff-Zugriff in diesem Run" in ctx
    assert "MANDATORY: for any CHANGED existing symbol" not in ctx


@requires_git
def test_review_context_does_not_recapture_existing_submit_snapshot(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="widget", assignee="coder", review_tier="review",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        assert kb._submit_for_review(
            conn, t, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        event_payload_before = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()["payload"]
        (repo / "late.py").write_text("late = True\n", encoding="utf-8")
        capture_calls = 0

        def counted_capture(*_args, **_kwargs):
            nonlocal capture_calls
            capture_calls += 1
            return {"changed_files": ["late.py"]}

        monkeypatch.setattr(kb, "_capture_review_diff_snapshot", counted_capture)
        assert kb.claim_review_task(conn, t) is not None
        ctx = kb.build_worker_context(conn, t)
        event_payload_after = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()["payload"]

    assert capture_calls == 0
    assert event_payload_after == event_payload_before
    assert "tracked.py" in ctx
    assert "late.py" not in ctx


def test_b2_non_review_complete_leaves_verdict_null(kanban_home):
    """An ordinary coder completion leaves task_runs.verdict NULL."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="done", summary="done")
        assert _latest_run_verdict(conn, t) is None


def test_b2_non_review_block_leaves_verdict_null(kanban_home):
    """An ordinary block (coder hit a wall) leaves task_runs.verdict NULL."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="stuck")
        assert _latest_run_verdict(conn, t) is None


def test_b2_metadata_verdict_field_is_untouched(kanban_home):
    """Back-compat: an existing metadata['verdict'] free-form value is NOT
    promoted into the new column, and stays intact on the run metadata."""
    import json
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        kb.complete_task(
            conn, t, result="done", summary="done",
            metadata={"verdict": "free-form-note"},
        )
        # Column stays NULL (non-review run)...
        assert _latest_run_verdict(conn, t) is None
        # ...and the metadata key is preserved verbatim.
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert json.loads(row["metadata"])["verdict"] == "free-form-note"


def test_fo_backlog_item_closes_only_on_terminal_flow_done(
    kanban_home, tmp_path, monkeypatch
):
    """Regression: FO tasks copied into Fleet close their source backlog item
    only once the flow reaches terminal done, not at coder->review handoff."""
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    monkeypatch.setattr(kb.time, "time", lambda: 1781049600)  # 2026-06-10 UTC
    item = tmp_path / "0141-shopping-favoriten-chips-aus-historie.md"
    _write_fo_backlog_item(item)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="[FO] Favoriten-Chips",
            assignee="coder",
            tenant="family-organizer",
            idempotency_key="fo-backlog:0141",
        )
        kb.claim_task(conn, task_id)
        assert kb._submit_for_review(
            conn,
            task_id,
            result=None,
            summary="Implemented favorite chips from history",
            metadata={"changed_files": ["web/src/shopping.tsx"]},
            verified_cards=[],
            expected_run_id=None,
        )
        assert _frontmatter_dict(item)["status"] == "next"

        assert kb.claim_review_task(conn, task_id) is not None
        assert kb.complete_task(
            conn,
            task_id,
            result="APPROVED",
            summary="APPROVED",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )

        fm = _frontmatter_dict(item)
        assert fm["status"] == "done"
        assert fm["updated"] == "2026-06-10"
        assert fm["result"] == "Implemented favorite chips from history"
        events = [
            e for e in kb.list_events(conn, task_id)
            if e.kind == "family_organizer_backlog_closed"
        ]
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["item_id"] == "0141"


def test_fo_backlog_close_ignores_unlinked_family_organizer_tasks(
    kanban_home, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    item = tmp_path / "0141-shopping-favoriten-chips-aus-historie.md"
    _write_fo_backlog_item(item)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="[FO] unrelated",
            assignee="coder",
            tenant="family-organizer",
        )
        kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="unrelated done")

    assert _frontmatter_dict(item)["status"] == "next"


def test_fo_completion_hook_uses_connection_board(
    kanban_home, tmp_path, monkeypatch
):
    """A non-current board completion must not reopen the ambient board."""
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    item = tmp_path / "0141-shopping-favoriten-chips-aus-historie.md"
    _write_fo_backlog_item(item)
    kb.create_board("alpha")
    assert kb.get_current_board() == kb.DEFAULT_BOARD

    with kb.connect_closing(board="alpha") as conn:
        task_id = kb.create_task(
            conn,
            title="[FO] explicit board",
            assignee="coder",
            tenant="family-organizer",
            idempotency_key="fo-backlog:0141",
        )
        kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="done on alpha")
        assert any(
            event.kind == "family_organizer_backlog_closed"
            for event in kb.list_events(conn, task_id)
        )

    assert _frontmatter_dict(item)["status"] == "done"


# ---------------------------------------------------------------------------
# A1 (N-A1): acceptance-criteria column + body parser
# ---------------------------------------------------------------------------

def test_a1_acceptance_criteria_column_present_and_migrate_idempotently(
    kanban_home,
):
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "acceptance_criteria" in cols
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert cols2.count("acceptance_criteria") == 1


def test_a1_parse_extracts_ac_bullets():
    import json
    body = (
        "Goal: ship it.\n"
        "- AC-1: endpoint returns 200 — verification: curl\n"
        "* AC-2: row persisted — done_signal: row present\n"
        "- a non-AC bullet that should be ignored\n"
    )
    raw = kb._parse_acceptance_criteria(body)
    parsed = json.loads(raw)
    assert len(parsed) == 2
    assert "AC-1" in parsed[0]
    assert "AC-2" in parsed[1]


def test_a1_parse_none_for_empty_or_missing():
    assert kb._parse_acceptance_criteria(None) is None
    assert kb._parse_acceptance_criteria("") is None
    assert kb._parse_acceptance_criteria("   \n  ") is None


def test_a1_parse_none_when_no_ac_ids():
    body = (
        "Just prose.\n"
        "- implement the feature\n"
        "- tests run\n"
        "- documentation updated\n"
    )
    assert kb._parse_acceptance_criteria(body) is None


def test_a1_parse_numbered_bullets():
    import json
    body = "1. AC-1: works — verification: test\n2) AC-2: persists\n"
    parsed = json.loads(kb._parse_acceptance_criteria(body))
    assert len(parsed) == 2


# ---------------------------------------------------------------------------
# A2 (N-A2): verifier binding — review context + acceptance_roles config
# ---------------------------------------------------------------------------

@requires_git
def test_a2_review_context_has_checklist_and_changed_files(kanban_home, tmp_path):
    import json
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="widget", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        # A1 column is normally filled at decompose; set it directly here.
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(["AC-1: endpoint returns 200",
                         "AC-2: widget row persisted"]), t),
        )
        # Coder submits → B1 snapshot rides the submitted_for_review event.
        kb._submit_for_review(
            conn, t, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        # Verifier claims the review lane → its run is the current run.
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ctx = kb.build_worker_context(conn, t)
    assert "Acceptance checklist" in ctx
    assert "AC-1: endpoint returns 200" in ctx
    assert "AC-2: widget row persisted" in ctx
    assert "Changed files at submit" in ctx
    assert "tracked.py" in ctx
    assert "caller" in ctx.lower()


def test_a2_review_context_fallbacks_when_no_acs_no_snapshot(kanban_home):
    """Review run with NULL acceptance_criteria and no diff snapshot → both
    fallback notes render, no crash."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ctx = kb.build_worker_context(conn, t)
    assert "No structured acceptance criteria" in ctx
    assert "Kein Diff-Zugriff in diesem Run" in ctx


def test_a2_non_review_context_has_no_review_section(kanban_home):
    """Regression: an ordinary worker's context carries NONE of the A2 section,
    preserving the pre-A2 output."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        ctx = kb.build_worker_context(conn, t)
    assert "Acceptance checklist" not in ctx
    assert "Changed files at submit" not in ctx


def test_verifier_section_analysis_kind_emits_class_header(kanban_home):
    """kind='analysis' surfaces the read-only task-class header in the verifier
    Acceptance checklist block; AC items still render. End-to-end through
    build_worker_context so the header actually reaches the verifier."""
    with kb.connect_closing() as conn:
        t, section = _claimed_review_section(
            conn, kind="analysis",
            acceptance=["AC-1: report the bound type + lever"],
        )
        ctx = kb.build_worker_context(conn, t)
    assert "Task-Klasse: analysis" in section
    assert "BEOBACHTUNGEN, KEINE Blocker" in section
    # header lives inside the acceptance-checklist block, AC items still render
    assert "Acceptance checklist" in section
    assert "AC-1: report the bound type + lever" in section
    # and it survives into the full worker context the verifier actually sees
    assert "Task-Klasse: analysis" in ctx


def test_verifier_section_code_kind_has_no_class_header(kanban_home):
    """kind='code' (a build task) must NOT emit the analysis header —
    default-strict is preserved for everything that is not explicit analysis."""
    with kb.connect_closing() as conn:
        _t, section = _claimed_review_section(
            conn, kind="code",
            acceptance=["AC-1: endpoint returns 200"],
        )
    assert "Task-Klasse: analysis" not in section
    assert "Acceptance checklist" in section
    assert "AC-1: endpoint returns 200" in section


def test_verifier_section_unmarked_identical_to_code_default_strict(kanban_home):
    """Default-strict invariant: an UNMARKED task renders byte-identically to a
    kind='code' task. The marker only ever ADDS the analysis header; it never
    changes the strict default rendering."""
    acceptance = ["AC-1: endpoint returns 200", "AC-2: row persisted"]
    with kb.connect_closing() as conn:
        _tu, section_unmarked = _claimed_review_section(
            conn, kind=None, acceptance=acceptance,
        )
        _tc, section_code = _claimed_review_section(
            conn, kind="code", acceptance=acceptance,
        )
    assert "Task-Klasse: analysis" not in section_unmarked
    assert section_unmarked == section_code


def test_a2_acceptance_roles_default_empty_is_noop(kanban_home):
    cfg = kb._review_gate_config()
    assert cfg["acceptance_roles"] == frozenset()
    # Default code_roles unchanged (union with ∅).
    assert cfg["code_roles"] == frozenset(kb._DEFAULT_REVIEW_CODE_ROLES)
    assert "coder-claude" in cfg["code_roles"]


def test_review_gate_config_string_false_flags_are_disabled(kanban_home):
    import yaml
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({
            "kanban": {"review_gate": {
                "enabled": "false",
                "auto_tier": "false",
                "auto_scout_on_critical": "false",
            }}
        }),
        encoding="utf-8",
    )
    cfg = kb._review_gate_config()
    assert cfg["enabled"] is False
    assert cfg["auto_tier"] is False
    assert cfg["auto_scout_on_critical"] is False


def test_worker_gate_config_string_false_flag_is_disabled(kanban_home):
    import yaml
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({
            "kanban": {"worker_gate": {"enabled": "false"}}
        }),
        encoding="utf-8",
    )
    cfg = kb._worker_gate_config()
    assert cfg["enabled"] is False


def test_a2_acceptance_roles_union_into_code_roles(kanban_home):
    import yaml
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({
            "kanban": {"review_gate": {
                "enabled": True, "acceptance_roles": ["docs", "qa"],
            }}
        }),
        encoding="utf-8",
    )
    cfg = kb._review_gate_config()
    assert cfg["acceptance_roles"] == frozenset({"docs", "qa"})
    assert {"docs", "qa"} <= cfg["code_roles"]
    # Defaults preserved alongside the additions.
    assert frozenset(kb._DEFAULT_REVIEW_CODE_ROLES) <= cfg["code_roles"]

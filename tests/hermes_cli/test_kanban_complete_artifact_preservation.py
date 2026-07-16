"""Tests for scratch-workspace artifact auto-preservation on
``kanban_complete``.

Workers opt in by writing absolute paths into the per-run
``metadata.artifacts`` array BEFORE calling ``kanban_complete``. The terminal
completion transaction copies anything underneath scratch to attachments and
the legacy ``reports/by-task`` readmodel; cleanup only removes scratch after
that persistence owner succeeds.

Motivating incident (2026-05-27): the Combined-Template artifact
(405 lines) was lost when `t_b24a11fd` was completed by main-session-
claude; the worker hadn't copied it out of the scratch workspace. The
opt-in `runs.metadata.artifacts` channel already existed in the schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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


def _scratch_dir_for(home: Path, task_id: str) -> Path:
    """Build a managed scratch directory under the workspaces root."""
    root = kb.workspaces_root()
    workspace = root / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _bind_scratch_workspace(conn, task_id: str, workspace: Path) -> None:
    conn.execute(
        "UPDATE tasks SET workspace_kind='scratch', workspace_path=? "
        "WHERE id=?",
        (str(workspace), task_id),
    )


def _complete_with_artifacts(
    conn,
    task_id: str,
    artifacts: list[str],
    *,
    summary: str = "done",
) -> bool:
    """Realistic worker flow: complete the task with
    ``metadata={"artifacts": [...]}`` so ``complete_task`` lands the
    artifacts on its own closing run — which is the row the cleanup
    path inspects.
    """
    return kb.complete_task(
        conn,
        task_id,
        summary=summary,
        result="ok",
        metadata={"artifacts": list(artifacts)},
    )


def test_artifact_under_workspace_is_preserved_before_cleanup(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="preserve me")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)

    # The worker's artifact lives inside the scratch dir.
    artifact = wp / "output.md"
    artifact.write_text("# important content\nline2\n", encoding="utf-8")

    with kb.connect() as conn:
        _complete_with_artifacts(conn, tid, [str(artifact)])

    # Workspace must be gone (existing ephemeral contract).
    assert not wp.exists()

    # Persistent copy must exist under ~/.hermes/reports/by-task/<tid>/.
    dest = kanban_home / "reports" / "by-task" / tid / "output.md"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "# important content\nline2\n"


def test_empty_artifacts_keeps_legacy_behaviour(kanban_home):
    """Backwards-compat: a task with `artifacts=[]` (or no key at all)
    must keep the legacy "scratch deleted, nothing preserved" behaviour.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    (wp / "ignored.md").write_text("not declared as artifact", encoding="utf-8")
    with kb.connect() as conn:
        _complete_with_artifacts(conn, tid, [])
    assert not wp.exists()
    # Nothing copied because artifacts was empty.
    dest = kanban_home / "reports" / "by-task" / tid
    assert not dest.exists()


def test_artifact_outside_workspace_is_skipped(kanban_home, tmp_path):
    """Containment: an absolute path the worker declared that lives
    OUTSIDE the scratch workspace is silently skipped. We don't try
    to copy random files the worker happened to name — only artifacts
    that would otherwise be lost when the scratch dir is rmtree'd.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="contain")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    # An out-of-workspace path that exists (e.g. ~/.hermes/reports already).
    outside = tmp_path / "outside-report.md"
    outside.write_text("already persisted", encoding="utf-8")

    with kb.connect() as conn:
        _complete_with_artifacts(conn, tid, [str(outside)])

    # workspace removed, no copy attempted to by-task/.
    assert not wp.exists()
    dest = kanban_home / "reports" / "by-task" / tid
    assert not dest.exists()
    # Original outside file remains untouched.
    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "already persisted"


def test_relative_artifact_path_is_skipped(kanban_home):
    """Relative paths in `artifacts[]` are informational labels, not
    filesystem references. Don't attempt to copy them.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rel-paths")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    (wp / "output.md").write_text("x", encoding="utf-8")
    with kb.connect() as conn:
        _complete_with_artifacts(conn, tid, ["output.md", "tests passed"])
    assert not wp.exists()
    dest = kanban_home / "reports" / "by-task" / tid
    assert not dest.exists()


def test_missing_artifact_rejects_false_completion(kanban_home):
    """A declared deliverable must exist before the task can become done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="missing")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    declared = wp / "never-written.md"
    with kb.connect() as conn:
        with pytest.raises(kb.ArtifactPreservationError, match="unavailable"):
            _complete_with_artifacts(conn, tid, [str(declared)])
        assert kb.get_task(conn, tid).status != "done"
    assert wp.exists()


def test_multiple_artifacts_preserved(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="multi")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    files = []
    for name in ("a.md", "b.txt", "c.json"):
        f = wp / name
        f.write_text(f"content of {name}", encoding="utf-8")
        files.append(str(f))
    with kb.connect() as conn:
        _complete_with_artifacts(conn, tid, files)

    dest = kanban_home / "reports" / "by-task" / tid
    assert dest.exists()
    for name in ("a.md", "b.txt", "c.json"):
        assert (dest / name).exists()
        assert (dest / name).read_text(encoding="utf-8") == f"content of {name}"


def test_artifact_subdir_preserved_recursively(kanban_home):
    """A worker that pointed at a subdirectory inside scratch must get
    the whole tree copied (typical for sprint-output bundles).
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bundle")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    bundle = wp / "out-bundle"
    bundle.mkdir()
    (bundle / "spec.md").write_text("spec body", encoding="utf-8")
    (bundle / "fixture.json").write_text("{}", encoding="utf-8")
    with kb.connect() as conn:
        _complete_with_artifacts(conn, tid, [str(bundle)])

    dest = kanban_home / "reports" / "by-task" / tid / "out-bundle"
    assert (dest / "spec.md").read_text(encoding="utf-8") == "spec body"
    assert (dest / "fixture.json").read_text(encoding="utf-8") == "{}"


def test_non_scratch_workspace_is_untouched(kanban_home, tmp_path):
    """Worktree / dir workspaces are intentionally preserved by the
    existing contract; the new preservation hook must NOT run for them
    (would be wasted work and could clobber non-scratch state).
    """
    extdir = tmp_path / "shared-workspace"
    extdir.mkdir()
    (extdir / "artifact.md").write_text("shared", encoding="utf-8")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dir-kind")
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=? "
            "WHERE id=?",
            (str(extdir), tid),
        )
        _complete_with_artifacts(conn, tid, [str(extdir / "artifact.md")])
    # Directory preserved per legacy contract.
    assert extdir.exists()
    # No by-task/ copy because kind != scratch.
    dest = kanban_home / "reports" / "by-task" / tid
    assert not dest.exists()


def test_cleanup_workspace_has_no_second_artifact_copy_owner(
    kanban_home, monkeypatch
):
    """Cleanup removes managed scratch without rescanning completed runs."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="single artifact owner")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    (wp / "ephemeral.md").write_text("already persisted", encoding="utf-8")

    with kb.connect() as conn:
        kb._cleanup_workspace(conn, tid)

    assert not wp.exists()


def test_artifacts_survive_the_review_gate(kanban_home, monkeypatch):
    """Phase 2 regression guard: with the review gate, the terminal 'done' is
    the verifier's run (no artifacts), while the coder's artifacts=[...] rode
    the earlier submit_for_review run. Preservation must replay the latest
    implementer handoff so the deliverable lands in reports/by-task/."""
    import hermes_cli.profiles as profiles_mod
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gate preserve", assignee="coder")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    artifact = wp / "RESULT.md"
    artifact.write_text("# coder deliverable\n", encoding="utf-8")

    with kb.connect() as conn:
        kb.claim_task(conn, tid)
        # Coder submits WITH artifacts -> parks in review (workspace preserved).
        assert kb.complete_task(
            conn, tid, summary="impl done",
            metadata={"artifacts": [str(artifact)]}, review_gate=True,
        )
        assert kb.get_task(conn, tid).status == "review"
        assert wp.exists()  # not cleaned on the review hop
        # Verifier claims + approves WITHOUT artifacts -> terminal done.
        kb.claim_review_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="APPROVED — tests pass",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )
        assert kb.get_task(conn, tid).status == "done"

    # Workspace cleaned, but the latest coder handoff survived review.
    assert not wp.exists()
    dest = kanban_home / "reports" / "by-task" / tid / "RESULT.md"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "# coder deliverable\n"
    with kb.connect() as conn:
        preserved = [
            event
            for event in kb.list_events(conn, tid)
            if event.kind == "deliverables_preserved"
        ]
    assert len(preserved) == 1
    assert preserved[0].payload["files"] == ["RESULT.md"]


def test_latest_review_submission_replaces_superseded_artifacts(
    kanban_home, monkeypatch
):
    """A REQUEST_CHANGES round may replace or remove an earlier deliverable.

    Terminal approval must persist the latest implementer handoff only; a
    deleted path from the rejected submission is no longer part of the
    completion contract.
    """
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="replace artifact", assignee="coder")
        workspace = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, workspace)
        rejected = workspace / "REJECTED.md"
        replacement = workspace / "FINAL.md"
        rejected.write_text("obsolete\n", encoding="utf-8")

        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="first implementation",
            metadata={"artifacts": [str(rejected)]},
            review_gate=True,
        )
        assert kb.claim_review_task(conn, tid) is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="replace the deliverable",
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["replace the deliverable"],
            },
            review_gate=True,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        rejected.unlink()
        replacement.write_text("accepted\n", encoding="utf-8")
        promoted, reason = kb.promote_task(
            conn, tid, actor="test", reason="address review"
        )
        assert promoted, reason
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="reworked implementation",
            metadata={"artifacts": [str(replacement)]},
            review_gate=True,
        )
        assert kb.claim_review_task(conn, tid) is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="APPROVED",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )

    report_dir = kanban_home / "reports" / "by-task" / tid
    assert (report_dir / "FINAL.md").read_text(encoding="utf-8") == "accepted\n"
    assert not (report_dir / "REJECTED.md").exists()


def test_artifacts_survive_the_full_critical_review_chain(
    kanban_home, monkeypatch
):
    """Intermediate verifier/reviewer submissions must not shadow the coder.

    Every approved intermediate stage emits its own ``submitted_for_review``
    event. Those review-originated runs carry verdict metadata rather than the
    implementer's artifact contract, so terminal critic approval must replay
    the latest non-review submission.
    """
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="critical artifact",
            assignee="coder",
            review_tier="critical",
        )
        workspace = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, workspace)
        artifact = workspace / "RESULT.md"
        artifact.write_text("critical deliverable\n", encoding="utf-8")

        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="implementation",
            metadata={"artifacts": [str(artifact)]},
            review_gate=True,
        )
        for profile in ("verifier", "reviewer", "critic"):
            assert kb.claim_review_task(
                conn, tid, reviewer_profile=profile
            ) is not None
            assert kb.complete_task(
                conn,
                tid,
                summary=f"{profile} approved",
                metadata={"review_verdict": "APPROVED"},
                review_gate=True,
            )

        assert kb.get_task(conn, tid).status == "done"

    report = kanban_home / "reports" / "by-task" / tid / "RESULT.md"
    assert report.read_text(encoding="utf-8") == "critical deliverable\n"


def test_empty_latest_implementer_artifacts_supersede_rejected_handoff(
    kanban_home, monkeypatch
):
    """An explicit empty list removes the prior round's artifact contract."""
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="remove artifact", assignee="coder")
        workspace = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, workspace)
        rejected = workspace / "REJECTED.md"
        rejected.write_text("obsolete\n", encoding="utf-8")

        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="first implementation",
            metadata={"artifacts": [str(rejected)]},
            review_gate=True,
        )
        assert kb.claim_review_task(conn, tid) is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="remove the deliverable",
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["remove the deliverable"],
            },
            review_gate=True,
        )
        rejected.unlink()

        promoted, reason = kb.promote_task(
            conn, tid, actor="test", reason="address review"
        )
        assert promoted, reason
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="artifact intentionally removed",
            metadata={"artifacts": []},
            review_gate=True,
        )
        assert kb.claim_review_task(conn, tid) is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="APPROVED",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )

    assert not (kanban_home / "reports" / "by-task" / tid).exists()


def test_missing_artifacts_key_does_not_supersede_earlier_handoff(
    kanban_home, monkeypatch
):
    """A second coder submission whose metadata never mentions ``artifacts``
    makes no assertion about deliverables — unlike an explicit empty list, it
    must not shadow the earlier round's artifact contract (#artifacts-key)."""
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="keep artifact", assignee="coder")
        workspace = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, workspace)
        artifact = workspace / "RESULT.md"
        artifact.write_text("# coder deliverable\n", encoding="utf-8")

        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="first implementation",
            metadata={"artifacts": [str(artifact)]},
            review_gate=True,
        )
        assert kb.claim_review_task(conn, tid) is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="needs a tweak",
            metadata={
                "review_verdict": "REQUEST_CHANGES",
                "blocking_findings": ["needs a tweak"],
            },
            review_gate=True,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        promoted, reason = kb.promote_task(
            conn, tid, actor="test", reason="address review"
        )
        assert promoted, reason
        kb.claim_task(conn, tid)
        # Second coder completion has NO ``artifacts`` key at all — not an
        # explicit empty list — so it must not erase the first round's file.
        assert kb.complete_task(
            conn,
            tid,
            summary="minor fix, same deliverable",
            metadata={},
            review_gate=True,
        )
        assert kb.claim_review_task(conn, tid) is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="APPROVED",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )
        assert kb.get_task(conn, tid).status == "done"

    dest = kanban_home / "reports" / "by-task" / tid / "RESULT.md"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "# coder deliverable\n"

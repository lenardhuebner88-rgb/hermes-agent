"""Tests for scratch-workspace artifact auto-preservation on
``kanban_complete``.

Workers opt in by writing absolute paths into the per-run
``metadata.artifacts`` array BEFORE calling ``kanban_complete``; the
cleanup path auto-copies anything that lives underneath the scratch
workspace to ``~/.hermes/reports/by-task/<task_id>/<basename>`` so the
artifact survives the ``shutil.rmtree(scratch)`` that runs immediately
after.

Motivating incident (2026-05-27): the Combined-Template artifact
(405 lines) was lost when `t_b24a11fd` was completed by main-session-
claude; the worker hadn't copied it out of the scratch workspace. The
opt-in `runs.metadata.artifacts` channel already existed in the
schema — this commit just plumbs it into the cleanup path.
"""

from __future__ import annotations

import json
import time
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


def test_missing_artifact_is_skipped(kanban_home):
    """A worker that names a file but never wrote it (LLM hallucination,
    rename race, etc.) must not crash the cleanup. The missing artifact
    is silently dropped.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="missing")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    declared = wp / "never-written.md"
    with kb.connect() as conn:
        # Must complete cleanly even though the named file doesn't exist.
        assert _complete_with_artifacts(conn, tid, [str(declared)]) is True
    assert not wp.exists()


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


def test_preservation_function_returns_basenames(kanban_home):
    """Direct unit test of the helper for use in operator scripts /
    future dashboard surfacing.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="direct")
        wp = _scratch_dir_for(kanban_home, tid)
        _bind_scratch_workspace(conn, tid, wp)
    (wp / "a.md").write_text("a", encoding="utf-8")
    (wp / "b.md").write_text("b", encoding="utf-8")
    # For this direct-helper test we INSERT a closed run with the
    # metadata manually (we want to test the helper in isolation,
    # without going through complete_task).
    now = int(time.time())
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "ended_at, outcome, summary, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, "coder", "ended", now - 30, now - 1, "completed", "done",
                json.dumps({"artifacts": [str(wp / "a.md"), str(wp / "b.md")]}),
            ),
        )
        conn.commit()
        preserved = kb._preserve_scratch_artifacts(conn, tid, wp)
    assert sorted(preserved) == ["a.md", "b.md"]

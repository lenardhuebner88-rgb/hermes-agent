"""Kanban DB tests: spawn workdir.

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

def _make_task(**overrides) -> "kb.Task":
    """Minimal Task with all required fields filled in. Override anything."""
    defaults = dict(
        id="t_age",
        title="x",
        body=None,
        assignee=None,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    defaults.update(overrides)
    return kb.Task(**defaults)


# ---------------------------------------------------------------------------
# latest_summary / latest_summaries — surface task_runs.summary handoffs
# ---------------------------------------------------------------------------

def test_latest_summary_returns_none_when_no_runs(kanban_home):
    """A freshly-created task has no runs and therefore no summary."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="fresh", assignee="alice")
        assert kb.latest_summary(conn, t) is None


def test_latest_summary_returns_summary_after_complete(kanban_home):
    """``complete_task(summary=...)`` is the canonical kanban-worker
    handoff; ``latest_summary`` must surface it so dashboards/CLI can
    render what the worker actually did."""
    handoff = "shipped 3 files, ran tests, opened PR #42"
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="work", assignee="alice")
        kb.complete_task(conn, t, summary=handoff)
        assert kb.latest_summary(conn, t) == handoff


def test_latest_summary_picks_newest_when_multiple_runs(kanban_home):
    """When a task has been re-run (block → unblock → complete), the
    newest run's summary wins. We unblock to take the task back to
    ``ready``, then complete a second time and verify the second
    summary surfaces."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="retry", assignee="alice")
        kb.complete_task(conn, t, summary="first attempt")
        # Move back to ready by direct SQL — block_task / unblock_task
        # paths require an active claim, but we just want a second run
        # row to exist with a later ended_at.
        conn.execute(
            "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?",
            (t,),
        )
        # Sleep 1s so the second run's ended_at is provably later than
        # the first (complete_task uses int(time.time())).
        time.sleep(1.05)
        kb.complete_task(conn, t, summary="second attempt — final")
        assert kb.latest_summary(conn, t) == "second attempt — final"


def test_latest_summary_skips_empty_string(kanban_home):
    """A run with an empty-string summary should not mask an earlier
    populated one — empty strings carry no information."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="t", assignee="alice")
        kb.complete_task(conn, t, summary="real handoff")
        # Inject a later run with empty summary directly. Workers
        # writing "" instead of None is a real shape we want to ignore.
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, "
            "outcome, summary) VALUES (?, 'done', ?, ?, 'completed', ?)",
            (t, int(time.time()) + 1, int(time.time()) + 2, ""),
        )
        conn.commit()
        assert kb.latest_summary(conn, t) == "real handoff"


def test_latest_summaries_batch_omits_tasks_without_summary(kanban_home):
    """``latest_summaries`` is the dashboard's N+1 escape hatch — it
    must return only entries for tasks that actually have a summary,
    keep the per-task latest, and accept an empty input gracefully."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        t3 = kb.create_task(conn, title="c", assignee="carol")
        kb.complete_task(conn, t1, summary="alpha")
        kb.complete_task(conn, t3, summary="charlie")
        out = kb.latest_summaries(conn, [t1, t2, t3])
        assert out == {t1: "alpha", t3: "charlie"}
        # Empty input → empty dict, no SQL syntax error from "IN ()".
        assert kb.latest_summaries(conn, []) == {}


# ---------------------------------------------------------------------------
# NFS / network-filesystem fallback (see hermes_state.apply_wal_with_fallback)
# ---------------------------------------------------------------------------

def test_connect_falls_back_to_delete_on_locking_protocol(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must handle ``locking protocol`` on NFS/SMB.

    Without this fallback, the gateway's kanban dispatcher crashes every
    60s and the kanban migration (``consecutive_failures`` ADD COLUMN) is
    retried forever — which is what the real-world user report shows
    (see hermes-agent issue #22032).

    NOTE: We do NOT use the ``kanban_home`` fixture here because that
    fixture pre-initializes the DB via ``kb.init_db()`` — putting the
    file in WAL on disk. The Bug D safety guard now refuses to downgrade
    to DELETE when the on-disk header is already WAL, so testing the
    NFS-fallback path requires a truly-fresh DB file (NFS scenario in
    production: first connection of the first process ever to touch the
    file, where downgrading is safe because nobody else has WAL state
    yet).
    """
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()

    real_connect = _sqlite3.connect

    class _WalBlockingConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                raise _sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    def wal_blocking_connect(*args, **kwargs):
        return real_connect(
            *args, factory=_WalBlockingConnection, **kwargs
        )

    with _patch("hermes_cli.kanban_db.sqlite3.connect", side_effect=wal_blocking_connect):
        with caplog.at_level("WARNING", logger="hermes_state"):
            conn = kb.connect()

    # One fallback warning, naming kanban.db
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "kanban.db" in r.getMessage()
    ]
    assert len(warnings) >= 1, (
        f"Expected a kanban.db WARNING, got: {[r.getMessage() for r in caplog.records]}"
    )

    # DB still usable end-to-end — create + list a task
    t = kb.create_task(conn, title="post-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()


def test_unlink_tasks_triggers_recompute_ready(kanban_home):
    """Regression test for issue #22459.

    Removing a dependency via unlink_tasks must immediately promote the child
    to ready when all remaining parents are done — same contract as
    complete_task and unblock_task.

    Before the fix, child stayed 'todo' indefinitely after unlink; only the
    next dispatcher tick or a manual 'hermes kanban recompute' would promote it.
    """
    with kb.connect_closing() as conn:
        # A is done.
        a = kb.create_task(conn, title="parent-done")
        kb.complete_task(conn, a)

        # C is running (not done) — blocks child B.
        c = kb.create_task(conn, title="parent-running")
        kb.claim_task(conn, c, claimer="worker:1")

        # B depends on both A (done) and C (running) → stays todo.
        b = kb.create_task(conn, title="child", parents=[a, c])
        assert kb.get_task(conn, b).status == "todo"

        # Remove the blocking dependency C → B.
        removed = kb.unlink_tasks(conn, c, b)
        assert removed is True

        # B's only remaining parent is A (done) → must be ready immediately.
        assert kb.get_task(conn, b).status == "ready", (
            "child should promote to ready immediately after unlink_tasks "
            "removes its last blocking dependency"
        )


def test_archive_task_releases_dependent_children(kanban_home):
    """Archiving a parent removes its obsolete link and promotes its child."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="obsolete parent")
        child = kb.create_task(conn, title="child", parents=[parent])

        assert kb.get_task(conn, child).status == "todo"
        assert kb.archive_task(conn, parent) is True

        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "ready"


# ---------------------------------------------------------------------------
# _add_column_if_missing / _migrate_add_optional_columns idempotency (#21708)
# ---------------------------------------------------------------------------

def test_add_column_if_missing_is_idempotent_on_race(kanban_home):
    """``_add_column_if_missing`` must swallow 'duplicate column name' errors.

    Regression for #21708: the kanban dispatcher opens the DB twice per tick
    (once via _tick_once_for_board, once via init_db's discard-and-reconnect
    path).  A second concurrent connection runs _migrate_add_optional_columns
    before the first one commits, so ALTER TABLE raises OperationalError with
    'duplicate column name: consecutive_failures'.  Without the idempotency
    guard that crashes the dispatcher on the first tick after every restart.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = kb._add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = kb._add_column_if_missing(
        conn, "tasks", "extra_col", "extra_col TEXT"
    )
    assert added_again is False

    conn.close()


def test_migrate_add_optional_columns_tolerates_concurrent_migration(kanban_home):
    """Full _migrate_add_optional_columns must not raise when columns already
    exist (issue #21708 race window — two connections migrate concurrently)."""
    import sqlite3

    # Schema already in fully-migrated state (all optional columns present).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            branch_name TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_failure_error TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            current_run_id INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            skills TEXT,
            max_retries INTEGER,
            session_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL DEFAULT '',
            run_id     INTEGER,
            kind       TEXT NOT NULL DEFAULT '',
            payload    TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Running migration on an already-migrated schema must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


def test_resolve_hermes_argv_prefers_path_shim(monkeypatch):
    """When `hermes` is on PATH, use the shim — preserves familiar ps output."""
    import shutil
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/hermes")
    argv = kb._resolve_hermes_argv()
    assert argv == ["/usr/local/bin/hermes"]


def test_resolve_hermes_argv_absolutizes_relative_exe_shim(monkeypatch, tmp_path):
    """A relative executable override must not remain workspace-cwd-dependent."""
    import hermes_cli.kanban_db as kb

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_BIN", ".\\hermes.exe")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [os.path.abspath(".\\hermes.exe")]


def test_resolve_hermes_argv_avoids_implicit_windows_batch_shim(monkeypatch, tmp_path):
    """Implicit .cmd/.bat shims use the module fallback, not batch argv[0]."""
    import sys
    import hermes_cli.kanban_db as kb

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes.CMD").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_honors_hermes_bin_path_override(monkeypatch, tmp_path):
    """An explicit path-like HERMES_BIN lets service managers pin the executable."""
    import shutil
    import hermes_cli.kanban_db as kb

    shim = tmp_path / "bin" / "hermes"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_BIN", str(shim))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert kb._resolve_hermes_argv() == [str(shim)]


def test_resolve_hermes_argv_hermes_bin_bare_name_uses_path(monkeypatch, tmp_path):
    """Bare HERMES_BIN values keep PATH semantics instead of cwd shadowing."""
    import stat
    import hermes_cli.kanban_db as kb

    cwd_hermes = tmp_path / "hermes"
    cwd_hermes.write_text("wrong\n", encoding="utf-8")
    cwd_hermes.chmod(cwd_hermes.stat().st_mode | stat.S_IXUSR)
    path_hermes = tmp_path / "bin" / "hermes"
    path_hermes.parent.mkdir()
    path_hermes.write_text("right\n", encoding="utf-8")
    path_hermes.chmod(path_hermes.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(path_hermes.parent))
    monkeypatch.setenv("HERMES_BIN", "hermes")

    assert kb._resolve_hermes_argv() == [str(path_hermes)]


def test_resolve_hermes_argv_hermes_bin_bare_name_ignores_cwd(monkeypatch, tmp_path):
    """Bare HERMES_BIN does not accept current-directory shadow executables."""
    import sys
    import hermes_cli.kanban_db as kb

    (tmp_path / "hermes.exe").write_text("wrong\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_BIN", "hermes")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_hermes_bin_bare_cmd_uses_module_fallback(monkeypatch, tmp_path):
    """A PATH-resolved HERMES_BIN batch shim is not used as worker argv[0]."""
    import sys
    import hermes_cli.kanban_db as kb

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes.CMD").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("HERMES_BIN", "hermes")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_hermes_bin_unresolved_bare_name_falls_back(monkeypatch):
    """Unresolved HERMES_BIN command names do not delegate cwd search to Popen."""
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_BIN", "hermes")

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim(monkeypatch):
    """When the shim is not on PATH, fall back to `python -m hermes_cli.main`.

    Pins the correct module name (NOT `hermes` — there is no top-level
    `hermes` package). Regression for #23198: the original PR shipped
    `python -m hermes` which fails with `No module named hermes` on every
    invocation.
    """
    import shutil
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kb._resolve_hermes_argv()
    assert argv == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.
    """
    import subprocess
    import hermes_cli.kanban_db as kb
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kb._resolve_hermes_argv()
    r = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
    assert "Hermes Agent" in r.stdout, f"unexpected output: {r.stdout[:200]!r}"


def test_safe_int_accepts_int_and_int_string():
    """Sanity: well-typed values pass through."""
    # PR d8ad431de renamed _safe_int → _to_epoch (now also handles ISO-8601).
    assert kb._to_epoch(0) == 0
    assert kb._to_epoch(1700000000) == 1700000000
    assert kb._to_epoch("1700000000") == 1700000000


def test_safe_int_returns_none_on_corrupt_inputs():
    """All the failure modes that used to crash task_age."""
    # None — common when the column was never written
    assert kb._to_epoch(None) is None
    # Unsubstituted format string — the literal case the PR title cites
    assert kb._to_epoch("%s") is None
    # Arbitrary non-numeric strings
    assert kb._to_epoch("abc") is None
    assert kb._to_epoch("") is None
    # Float-ish strings: int("1.5") raises ValueError too — caller wants None.
    assert kb._to_epoch("1.5") is None
    # Random object — covered by TypeError branch
    assert kb._to_epoch(object()) is None


def test_task_age_handles_corrupt_created_at():
    """Pre-fix this raised ValueError and 500'd /api/plugins/kanban/board."""
    t = _make_task(created_at="%s")
    age = kb.task_age(t)
    assert age["created_age_seconds"] is None
    assert age["started_age_seconds"] is None
    assert age["time_to_complete_seconds"] is None


def test_task_age_handles_corrupt_started_and_completed():
    """All three timestamp fields share the same _safe_int treatment."""
    t = _make_task(
        created_at=1700000000,
        started_at="garbage",
        completed_at=None,
    )
    age = kb.task_age(t)
    assert isinstance(age["created_age_seconds"], int)
    assert age["started_age_seconds"] is None
    assert age["time_to_complete_seconds"] is None


def test_task_age_well_formed_task():
    """Regression: the safe-int path must not change behavior for normal data."""
    import time
    now = int(time.time())
    t = _make_task(
        created_at=now - 60,
        started_at=now - 30,
        completed_at=now,
    )
    age = kb.task_age(t)
    assert 55 <= age["created_age_seconds"] <= 65
    assert 25 <= age["started_age_seconds"] <= 35
    assert 25 <= age["time_to_complete_seconds"] <= 35


def test_task_dict_survives_corrupt_created_at(tmp_path, monkeypatch):
    """Defense in depth: even if task_age ever raised, plugin_api must not 500.

    The PR also added a try/except around the task_age call in
    `plugins/kanban/dashboard/plugin_api.py::_task_dict`. Verify a single
    corrupt row doesn't turn the whole board response into an error.
    """
    # Set up an isolated kanban home so we can write a corrupt created_at.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    # Insert a row with a non-int created_at (simulates the historical
    # bug that produced corrupt rows).
    conn = kb.connect()
    try:
        good_id = kb.create_task(conn, title="good")
        # Now write a row with corrupt created_at directly.
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("%s", good_id),
        )
    finally:
        conn.close()

    # Re-read and pass through task_age — must not raise.
    conn = kb.connect()
    try:
        task = kb.get_task(conn, good_id)
    finally:
        conn.close()
    age = kb.task_age(task)
    assert age["created_age_seconds"] is None


def test_create_task_scratch_without_workspace_ignores_board_default_workdir(kanban_home, monkeypatch):
    """Scratch tasks must NOT inherit board.default_workdir — would point auto-cleanup
    at the user's source tree on completion (#28818)."""
    default_wd = "/home/user/project"
    kb.create_board("work-proj", default_workdir=default_wd)

    with kb.connect(board="work-proj") as conn:
        tid = kb.create_task(conn, title="scratch-task", board="work-proj")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_create_task_dir_without_workspace_inherits_board_default_workdir(kanban_home, monkeypatch):
    """Board default_workdir is for persistent dir/worktree workspaces, not scratch."""
    default_wd = "/home/user/project"
    kb.create_board("work-proj-dir", default_workdir=default_wd)

    with kb.connect(board="work-proj-dir") as conn:
        tid = kb.create_task(
            conn,
            title="inherited",
            workspace_kind="dir",
            board="work-proj-dir",
        )
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path == default_wd


def test_create_task_without_workspace_no_default_stays_none(kanban_home):
    """Board without default_workdir → create_task without workspace_path → stays None."""
    kb.create_board("empty-board")

    with kb.connect(board="empty-board") as conn:
        tid = kb.create_task(conn, title="none", board="empty-board")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path is None


def test_create_task_with_explicit_workspace_ignores_board_default(kanban_home):
    """create_task with explicit workspace_path → ignores board default."""
    kb.create_board("custom-ws-board", default_workdir="/board/default")

    explicit = "/my/explicit/path"
    with kb.connect(board="custom-ws-board") as conn:
        tid = kb.create_task(conn, title="explicit", workspace_path=explicit, board="custom-ws-board")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path == explicit
    assert t.workspace_path != "/board/default"


def test_create_task_code_role_gets_coder_contract(
    kanban_home, monkeypatch, tmp_path
):
    """Code-role cards get compact scope/deps/test/handoff rails."""
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )
    repo = tmp_path / "family-organizer"
    repo.mkdir()
    from hermes_cli import kanban_worktrees

    monkeypatch.setattr(kanban_worktrees, "FO_REPO_PATH", repo)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="[FO] ship chips",
            body="Implement favorite chips.",
            assignee="coder",
            tenant="family-organizer",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(repo)
    assert task.body is not None
    assert task.body.startswith("Implement favorite chips.")
    assert "## Hermes Coder Contract v1" in task.body
    assert f"Workspace: dir:{repo}" in task.body
    assert "Dependency gate:" in task.body
    assert "Kanban CLI self-tests:" in task.body
    assert "HERMES_SANDBOX_MODE=1" in task.body
    assert "Completion metadata:" in task.body


def test_code_task_contract_body_has_no_duplicate_workspace_or_assignee_lines(
    kanban_home, tmp_path
):
    """Contract body must have exactly one Workspace: and one Assignee: line (no Repo/workspace, no Assignee/lane)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="fix the bug",
            body="Do the work.",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.body is not None
    body = task.body
    workspace_lines = [ln for ln in body.splitlines() if ln.startswith("- Workspace:")]
    assignee_lines = [ln for ln in body.splitlines() if ln.startswith("- Assignee:")]
    assert len(workspace_lines) == 1, f"Expected exactly 1 Workspace: line, got: {workspace_lines}"
    assert len(assignee_lines) == 1, f"Expected exactly 1 Assignee: line, got: {assignee_lines}"
    assert not any("Repo/workspace:" in ln for ln in body.splitlines()), (
        "Duplicate 'Repo/workspace:' line found in contract body"
    )
    assert not any("Assignee/lane:" in ln for ln in body.splitlines()), (
        "Duplicate 'Assignee/lane:' line found in contract body"
    )


def test_code_task_contract_body_risk_is_low_for_scratch_workspace(kanban_home):
    """A scratch-workspace task's contract body Risk line must say 'low', not 'medium'."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="analyse scratch results",
            body="Run some analysis.",
            assignee="coder",
            workspace_kind="scratch",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.body is not None
    body = task.body
    risk_lines = [ln for ln in body.splitlines() if ln.startswith("- Risk:")]
    assert risk_lines, "No Risk: line found in contract body"
    assert "low" in risk_lines[0], (
        f"Expected 'low' risk for scratch workspace, got: {risk_lines[0]}"
    )
    assert "medium" not in risk_lines[0], (
        f"scratch-workspace body should say 'low' not 'medium': {risk_lines[0]}"
    )


def test_create_task_non_code_role_body_unchanged(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="read docs",
            body="Summarize the release notes.",
            assignee="research",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.body == "Summarize the release notes."


def test_code_task_missing_contract_blocks_before_claim(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="ambiguous repo",
            assignee="coder",
            workspace_kind="dir",
        )
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "blocked"
    kinds = [e.kind for e in events]
    assert "needs_contract" in kinds
    assert "needs_contract_blocked" in kinds
    blocked = [e for e in events if e.kind == "blocked"][-1]
    assert "repo_workspace" in (blocked.payload or {}).get("reason", "")


def test_code_task_safe_contract_is_auto_enriched_before_pickup(
    kanban_home, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="explicit repo",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
                (tid, "code_task_contract_inferred"),
            )
        task = kb.claim_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    contract_events = [e for e in events if e.kind == "code_task_contract_inferred"]
    assert contract_events
    payload = contract_events[-1].payload
    assert payload["repo_workspace"] == f"dir:{repo}"
    assert payload["allowed_paths"] == [str(repo)]


def test_absolute_paths_from_text_rejects_single_segment_prose_token():
    """B2.1: the allowed-paths parser must not scoop a single-segment slash token
    out of prose. The observed defect: a body mentioning the dispatcher action
    `action=="merged"/integration_merged` produced allowed_paths
    ['/integration_merged'] (the slash after the closing quote passed the
    negative lookbehind). A 1-segment absolute token is never a real
    repo/allowed path, so it is dropped."""
    body = (
        'The integrator returns action=="merged"/integration_merged when the '
        "rebase applies cleanly; otherwise \"rebase_conflict\"/integration_parked."
    )
    paths = kb._absolute_paths_from_text(body)
    assert "/integration_merged" not in paths
    assert "/integration_parked" not in paths
    assert paths == []


def test_absolute_paths_from_text_keeps_real_multi_segment_paths():
    """B2.1: genuine multi-segment absolute paths still survive (no regression for
    real allowed-path extraction)."""
    body = (
        "Edit /home/piet/.hermes/hermes-agent/hermes_cli/kanban_db.py and the "
        "test at /home/piet/.hermes/hermes-agent/tests/stress/conftest.py only."
    )
    paths = kb._absolute_paths_from_text(body)
    assert "/home/piet/.hermes/hermes-agent/hermes_cli/kanban_db.py" in paths
    assert "/home/piet/.hermes/hermes-agent/tests/stress/conftest.py" in paths


def test_reason_for_lane_coder_makes_no_false_model_claim():
    """B2.2: the `coder` lane reason must not assert a fixed model/provider — the
    lane resolves to whatever the lane config routes to (e.g. glm/neuralwatt),
    so the old hardcoded '(OpenAI-Codex/GPT)' was actively misleading."""
    reason = kb._reason_for_lane("coder")
    assert "OpenAI-Codex/GPT" not in reason
    assert "OpenAI" not in reason and "GPT" not in reason
    # the lane PURPOSE is still described
    assert "code" in reason.lower()
    # the canonical Claude lane reason is untouched (regression guard)
    assert "chain-critical" in kb._reason_for_lane("premium")


def test_code_task_contract_allowed_paths_excludes_prose_token():
    """B2.1 end-to-end at the payload builder: a scratch code task whose body
    mentions `"merged"/integration_merged` must NOT infer that prose token as an
    allowed path."""
    payload, _missing = kb._code_task_contract_payload(
        assignee="coder",
        workspace_kind="scratch",
        workspace_path=None,
        tenant="default",
        body='returns action=="merged"/integration_merged on a clean rebase',
        created_by="tester",
        protected_funnel_root=False,
        source="test",
    )
    assert "/integration_merged" not in payload["allowed_paths"]
    assert "OpenAI" not in payload["reason_for_lane"]


@pytest.mark.parametrize("assignee", ["reviewer", "critic", "research"])
def test_3a_code_task_rejects_verdict_only_roles_at_create(
    kanban_home, assignee
):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="role_misuse"):
            kb.create_task(
                conn,
                title="implement widget",
                assignee=assignee,
                kind="code",
            )


def test_3a_existing_code_task_with_verdict_role_blocks_before_claim(
    kanban_home, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"code_roles": ["coder", "coder-claude", "premium"]},
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="legacy wrong-role code task",
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?",
                ("reviewer", tid),
            )

        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    assert task.status == "blocked"
    needs = [e for e in events if e.kind == "needs_contract"][-1]
    assert (needs.payload or {})["issue"] == "role_misuse"
    blocked = [e for e in events if e.kind == "blocked"][-1]
    assert "role_misuse" in (blocked.payload or {})["reason"]


@pytest.mark.parametrize(
    ("assignee", "kind"),
    [("reviewer", "review"), ("critic", "review"), ("research", "research")],
)
def test_3a_verdict_and_research_tasks_still_claim_when_not_code(
    kanban_home, assignee, kind
):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title=f"{assignee} lane task",
            assignee=assignee,
            kind=kind,
        )
        claimed = kb.claim_task(conn, tid)

    assert claimed is not None
    assert claimed.assignee == assignee


def test_3a_coder_claude_contract_uses_canonical_lane_reason(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="reason through chain-critical change",
            body="Implement the careful fix.",
            assignee="coder-claude",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    assert task.body is not None
    # Phase A: coder-claude folds into the canonical Claude coder lane `premium`.
    assert task.assignee == "premium"
    assert "Reason for lane: reasoning-heavy" in task.body
    assert "claude-cli/Opus" not in task.body
    contract = [e for e in events if e.kind == "code_task_contract_inferred"][-1]
    payload = contract.payload or {}
    assert payload["assignee_lane"] == "premium"
    assert "chain-critical" in payload["reason_for_lane"]
    assert "cross-family review" in payload["reason_for_lane"]


def test_complete_task_records_self_verification_event(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="verify self", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            result="done",
            summary="ran focused gate",
            metadata={"self_verification": kb.SELF_VERIFIED},
        )
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert kb.SELF_VERIFIED in kinds


def test_deliverable_posted_not_completed_is_recoverable_and_repairable(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="render quarterly report",
            assignee="default",
            kind="text",
        )
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn,
            tid,
            "default",
            (
                "# Deliverable: render quarterly report\n\n"
                "The quarterly report is complete and mapped to the requested "
                "objective. Evidence includes the final section list, validation "
                "notes, and remaining risk. " + "x" * 120
            ),
        )
        pid = 424242
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)

        crashed = kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

        assert tid not in crashed
        assert task.status == "blocked"
        kinds = [e.kind for e in events]
        assert kb.DELIVERABLE_POSTED_NOT_COMPLETED in kinds
        assert "gave_up" not in kinds

        assert kb.repair_deliverable_posted_not_completed(
            conn, tid, actor="integrator",
        )
        repaired = kb.get_task(conn, tid)
        repair_events = [
            e for e in kb.list_events(conn, tid)
            if e.kind == "deliverable_protocol_repaired"
        ]
        verdicts = conn.execute(
            "SELECT verdict FROM task_runs WHERE task_id = ?", (tid,),
        ).fetchall()

    assert repaired.status == "done"
    assert repair_events
    assert repair_events[-1].payload["actor"] == "integrator"
    assert all(row["verdict"] is None for row in verdicts)


def test_code_deliverable_protocol_repair_routes_through_review(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        kb,
        "_run_worker_gate",
        lambda *_args, **_kwargs: {"configured": False},
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="implement lifecycle guard",
            assignee="coder",
            kind="code",
        )
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn,
            tid,
            "coder",
            "# RESULT: implement lifecycle guard\n\n"
            "Implementation and focused tests complete. " + "x" * 160,
        )
        pid = 525252
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)

        kb.detect_crashed_workers(conn)
        blocked = kb.get_task(conn, tid)
        assert blocked.status == "blocked"

        assert kb.repair_deliverable_posted_not_completed(
            conn, tid, actor="integrator",
        )
        repaired = kb.get_task(conn, tid)
        kinds = [event.kind for event in kb.list_events(conn, tid)]

    assert repaired.status == "review"
    assert "submitted_for_review" in kinds
    assert "completed" not in kinds
    assert "deliverable_protocol_repaired" in kinds


def test_worktree_deliverable_protocol_repair_routes_through_review(
    kanban_home, monkeypatch, tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        kb,
        "_run_worker_gate",
        lambda *_args, **_kwargs: {"configured": False},
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="prepare worktree artifact",
            assignee="default",
            kind="text",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "artifact-worktree"),
        )
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn,
            tid,
            "default",
            "# RESULT: prepare worktree artifact\n\n"
            "The worktree artifact is complete and validated. " + "x" * 160,
        )
        pid = 626262
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)

        kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.repair_deliverable_posted_not_completed(
            conn, tid, actor="integrator",
        )
        repaired = kb.get_task(conn, tid)

    assert repaired.status == "review"


def test_stale_deliverable_event_does_not_repair_later_failure_cycle(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="render quarterly report",
            assignee="coder",
            max_retries=1,
        )
        assert kb.claim_task(conn, tid) is not None
        kb.add_comment(
            conn,
            tid,
            "coder",
            (
                "# Deliverable: render quarterly report\n\n"
                "The quarterly report is complete and mapped to the requested "
                "objective. Evidence includes the final section list, validation "
                "notes, and remaining risk. " + "x" * 120
            ),
        )
        first_pid = 424244
        kb._set_worker_pid(conn, tid, first_pid)
        kb._record_worker_exit(first_pid, 0)

        assert tid not in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid) is not None

        second_pid = 424245
        kb._set_worker_pid(conn, tid, second_pid)
        kb._record_worker_exit(second_pid, 1)
        assert tid in kb.detect_crashed_workers(conn)

        events = kb.list_events(conn, tid)
        assert len([
            event for event in events
            if event.kind == kb.DELIVERABLE_POSTED_NOT_COMPLETED
        ]) == 1
        assert "unblocked" in [event.kind for event in events]
        assert "gave_up" in [event.kind for event in events]
        assert kb.get_task(conn, tid).status == "blocked"

        assert not kb.repair_deliverable_posted_not_completed(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"


def test_protocol_miss_without_deliverable_uses_bounded_retry(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="silent protocol miss", assignee="worker")
        kb.claim_task(conn, tid)
        pid = 424243
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)

        crashed = kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert tid in crashed
    assert task.status == "ready"
    assert "protocol_violation" in kinds
    assert "gave_up" not in kinds
    assert kb.DELIVERABLE_POSTED_NOT_COMPLETED not in kinds


def test_3b_operator_escalation_emitted_once_when_failure_ladder_exhausts(
    kanban_home,
):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs human decision", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        assert not kb._record_task_failure(
            conn,
            tid,
            "first spawn failure",
            outcome="spawn_failed",
            failure_limit=2,
            release_claim=True,
            end_run=True,
        )
        assert [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ] == []

        assert kb.claim_task(conn, tid) is not None
        assert kb._record_task_failure(
            conn,
            tid,
            "second spawn failure",
            outcome="spawn_failed",
            failure_limit=2,
            release_claim=True,
            end_run=True,
            event_payload_extra={"pid": 1234},
        )
        events = kb.list_events(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "blocked"
    assert len([e for e in events if e.kind == "gave_up"]) == 1
    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    assert len(escalations) == 1
    payload = escalations[0].payload or {}
    assert set(payload) == {
        "task",
        "why_now",
        "attempts_already_made",
        "evidence",
        "recommended_human_action",
        "blocked_action_boundary",
    }
    assert payload["attempts_already_made"] == 2
    assert payload["task"]["id"] == tid
    assert payload["evidence"]["trigger_outcome"] == "spawn_failed"
    assert payload["evidence"]["context"] == {"pid": 1234}
    assert payload["blocked_action_boundary"] == list(kb.OPERATOR_ONLY_ACTIONS)
    boundary = " ".join(payload["blocked_action_boundary"]).lower()
    assert "push" not in boundary
    assert "deploy" not in boundary
    assert "restart" not in boundary


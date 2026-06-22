"""Progress-gated bounded one-time iteration-budget extension.

Lever HEILER-BUDGET-BOUNDED-EXTEND-S1: a task that exhausts its continuation
budget but is STILL making measurable progress (workspace diff growing) gets
EXACTLY ONE bounded extra continuation before it is blocked/escalated as
capacity. A looping task (flat diff) is never extended. Default OFF.
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


def _enable_extension(home: Path, *, min_progress_delta: int = 1) -> None:
    """Write a root config.yaml opting the board into the extension lever."""
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  budget_progress_extension:\n"
        "    enabled: true\n"
        f"    min_progress_delta: {min_progress_delta}\n",
        encoding="utf-8",
    )


def _scripted_progress(monkeypatch, values):
    """Make ``_workspace_progress_size`` return *values* in order, then repeat last."""
    seq = list(values)
    state = {"i": 0}

    def _fake(conn, task_id):
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return int(seq[i])

    monkeypatch.setattr(kb, "_workspace_progress_size", _fake)


def _ready_task(conn, *, max_continuations=None):
    return kb.create_task(
        conn,
        title="budget-heavy build",
        assignee="coder",
        max_iterations=4,
        max_continuations=max_continuations,
    )


def _claim(conn, tid):
    claimed = kb.claim_task(conn, tid, claimer="test-host:worker")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return claimed


def _exhaust(conn, tid):
    claimed = _claim(conn, tid)
    return kb.record_iteration_budget_exhausted(
        conn, tid, summary="slice", expected_run_id=claimed.current_run_id,
    )


# --------------------------------------------------------------------------- #
# Default-off: byte-identical to the existing continuation contract.
# --------------------------------------------------------------------------- #


def test_extension_disabled_by_default_blocks_at_limit(kanban_home, monkeypatch):
    # Even if a progress signal would qualify, with no config the lever is OFF.
    _scripted_progress(monkeypatch, [10, 99])
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=1)
        assert _exhaust(conn, tid)          # continuation 1/1
        assert _exhaust(conn, tid)          # at limit -> block (no extension)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.continuation_count == 1
        assert int(task.budget_extension_count or 0) == 0
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "budget_extension_granted" not in kinds
        assert kinds[-1] == "auto_continuation_exhausted"


def test_extension_string_false_blocks_at_limit(kanban_home, monkeypatch):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  budget_progress_extension:\n"
        "    enabled: \"false\"\n",
        encoding="utf-8",
    )
    _scripted_progress(monkeypatch, [10, 99])
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=1)
        assert _exhaust(conn, tid)
        assert _exhaust(conn, tid)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert int(task.budget_extension_count or 0) == 0
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "budget_extension_granted" not in kinds


# --------------------------------------------------------------------------- #
# Enabled + progress: exactly one bounded extension, then block.
# --------------------------------------------------------------------------- #


def test_progress_gated_extension_grants_exactly_one_more_run(kanban_home, monkeypatch):
    _enable_extension(kanban_home)
    # P1=5 (seed marker), P2=20 (grew -> extend), P3=40 (grew but cap used -> block)
    _scripted_progress(monkeypatch, [5, 20, 40])
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=1)

        assert _exhaust(conn, tid)          # continuation 1/1, marker=5
        t1 = kb.get_task(conn, tid)
        assert t1.status == "ready"
        assert t1.continuation_count == 1
        assert int(t1.budget_progress_marker) == 5

        assert _exhaust(conn, tid)          # at limit + progress -> EXTENSION
        t2 = kb.get_task(conn, tid)
        assert t2.status == "ready"
        assert int(t2.budget_extension_count) == 1
        assert t2.continuation_count == 1   # extension does NOT spend a continuation
        assert int(t2.budget_progress_marker) == 20
        ev = kb.list_events(conn, tid)[-1]
        assert ev.kind == "budget_extension_granted"
        assert ev.payload["progress"] == 20
        assert ev.payload["prior_marker"] == 5
        assert ev.payload["extension"] == 1
        assert ev.payload["extension_limit"] == 1

        assert _exhaust(conn, tid)          # extension already used -> BLOCK
        t3 = kb.get_task(conn, tid)
        assert t3.status == "blocked"
        assert int(t3.budget_extension_count) == 1
        last = kb.list_events(conn, tid)[-1]
        assert last.kind == "auto_continuation_exhausted"
        assert last.payload.get("extension_skipped") == "already_used"

        # Guardrail: at most ONE extension event over the whole task lifetime.
        granted = [e for e in kb.list_events(conn, tid)
                   if e.kind == "budget_extension_granted"]
        assert len(granted) == 1


# --------------------------------------------------------------------------- #
# Enabled + looping (no progress): never extended, escalated immediately.
# --------------------------------------------------------------------------- #


def test_looping_task_not_extended_and_escalates(kanban_home, monkeypatch):
    _enable_extension(kanban_home)
    # P1=10 (seed marker), P2=10 (flat -> looping -> no extension)
    _scripted_progress(monkeypatch, [10, 10])
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=1)

        assert _exhaust(conn, tid)          # continuation 1/1, marker=10
        assert _exhaust(conn, tid)          # at limit, flat diff -> BLOCK

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert int(task.budget_extension_count or 0) == 0
        last = kb.list_events(conn, tid)[-1]
        assert last.kind == "auto_continuation_exhausted"
        assert last.payload.get("extension_skipped") == "no_progress"
        assert "budget_extension_granted" not in [
            e.kind for e in kb.list_events(conn, tid)
        ]


def test_progress_below_min_delta_is_not_extended(kanban_home, monkeypatch):
    _enable_extension(kanban_home, min_progress_delta=5)
    # P1=10, P2=12 -> grew by 2 < 5 -> below threshold -> looping, no extension
    _scripted_progress(monkeypatch, [10, 12])
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=1)
        assert _exhaust(conn, tid)
        assert _exhaust(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert int(task.budget_extension_count or 0) == 0
        assert kb.list_events(conn, tid)[-1].payload.get(
            "extension_skipped"
        ) == "no_progress"


# --------------------------------------------------------------------------- #
# Honor an explicit per-task continuation disable.
# --------------------------------------------------------------------------- #


def test_extension_does_not_override_disabled_continuations(kanban_home, monkeypatch):
    _enable_extension(kanban_home)
    _scripted_progress(monkeypatch, [50, 99])
    with kb.connect() as conn:
        tid = _ready_task(conn, max_continuations=0)
        assert _exhaust(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert int(task.budget_extension_count or 0) == 0
        assert kb.list_events(conn, tid)[-1].kind == "auto_continuation_disabled"


# --------------------------------------------------------------------------- #
# The progress helper actually reads git from the workspace.
# --------------------------------------------------------------------------- #


def test_workspace_progress_size_reads_real_git_diff(kanban_home, tmp_path):
    import subprocess

    ws = tmp_path / "ws"
    ws.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(ws), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    git("init")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (ws / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-m", "base")

    with kb.connect() as conn:
        tid = _ready_task(conn)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid)
        )
        conn.commit()

        # Clean tree -> zero progress.
        assert kb._workspace_progress_size(conn, tid) == 0

        # Tracked edit grows the diff.
        (ws / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
        small = kb._workspace_progress_size(conn, tid)
        assert small > 0

        # A new untracked file is also counted as progress (porcelain).
        (ws / "b.py").write_text("print('more work')\n")
        bigger = kb._workspace_progress_size(conn, tid)
        assert bigger > small

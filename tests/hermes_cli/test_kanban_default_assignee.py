"""Regression tests for #27145 — kanban.default_assignee for unassigned ready tasks.

When the dispatcher hits an unassigned ready task and ``kanban.default_assignee``
is set, the dispatcher applies the assignment and spawns. Without the config,
the task is skipped (existing behavior preserved).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest


def _is_purgeable_hermes_module(name: str) -> bool:
    return (
        name.startswith("hermes_cli")
        or name.startswith("hermes_state")
        or name == "hermes_constants"
    )


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB.

    Purges the hermes_cli/hermes_state/hermes_constants modules so they
    re-import against this fixture's fresh HERMES_HOME, then **restores the
    original module objects on teardown**. Without that restore, every later
    test file that bound these modules at import time keeps a reference to the
    orphaned pre-purge module while lazy imports inside it resolve to the
    freshly re-imported copy — a module-identity split that silently points
    later tests (e.g. test_kanban_db.py) at the wrong kanban DB.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_default_assignee_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Force-reimport so the fresh HERMES_HOME is picked up.
    saved = {
        name: mod for name, mod in sys.modules.items()
        if _is_purgeable_hermes_module(name)
    }
    for name in saved:
        del sys.modules[name]
    from hermes_cli import kanban_db
    try:
        yield kanban_db, test_home
    finally:
        for name in [n for n in sys.modules if _is_purgeable_hermes_module(n)]:
            del sys.modules[name]
        sys.modules.update(saved)
        shutil.rmtree(test_home, ignore_errors=True)


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345


def test_unassigned_task_skipped_without_default_assignee(isolated_kanban_home):
    """Baseline: with no default_assignee, an unassigned ready task is
    skipped via the existing `skipped_unassigned` bucket and the DB row
    is untouched."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.skipped_unassigned == [task_id]
    assert not res.auto_assigned_default
    assert not res.spawned
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["assignee"] is None


def test_unassigned_task_auto_assigned_with_default_assignee(isolated_kanban_home):
    """Core #27145 contract: with default_assignee set, an unassigned ready
    task gets the assignment applied and dispatched on the same tick. The
    DB row is mutated (assignee column + an 'assigned' event)."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
        )
    assert res.auto_assigned_default == [task_id]
    assert not res.skipped_unassigned
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id
    assert res.spawned[0][1] == "default"

    with kb.connect_closing() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["assignee"] == "default"

    # 'assigned' event emitted for the audit trail
    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ))
    assert len(evs) == 1
    payload = json.loads(evs[0][1])
    assert payload["assignee"] == "default"
    assert payload["source"] == "kanban.default_assignee"


def test_dry_run_with_default_assignee_reports_without_mutating(isolated_kanban_home):
    """Dry-run mode: reports what WOULD happen (task in auto_assigned_default,
    spawn entry) but does NOT mutate the DB. Operators using
    `hermes kanban dispatch --dry-run` see the routing decision before
    committing."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            default_assignee="default",
        )
    assert res.auto_assigned_default == [task_id]
    assert len(res.spawned) == 1
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    # DB unchanged — dry_run did not commit the assignment.
    assert row["assignee"] is None


def test_whitespace_default_assignee_treated_as_none(isolated_kanban_home):
    """Empty / whitespace-only default_assignee values must be treated as
    'no fallback set' so a misconfigured kanban.default_assignee=' '
    doesn't surprise operators by silently routing unassigned tasks."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="   ",
        )
    assert task_id in res.skipped_unassigned
    assert not res.auto_assigned_default


def test_explicitly_assigned_task_untouched_by_default_assignee(isolated_kanban_home):
    """A task with an explicit assignee must NOT be touched by the
    default_assignee logic — that fallback only applies to genuinely
    unassigned rows."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="someother",
        )
    assert task_id not in res.auto_assigned_default
    assert any(s[0] == task_id and s[1] == "default" for s in res.spawned)


def test_dispatch_result_has_auto_assigned_default_field():
    """Schema-level invariant: DispatchResult exposes the
    auto_assigned_default field so CLI / dashboard / gateway can surface
    the new routing decisions."""
    from hermes_cli.kanban_db import DispatchResult
    r = DispatchResult()
    assert hasattr(r, "auto_assigned_default")
    assert r.auto_assigned_default == []


def test_canonical_assignee_aliases_coder_claude_to_premium():
    """Phase A: coder-claude folds into the canonical Claude coder lane `premium`.

    Every DB write routes assignee `coder-claude` -> `premium` (back-compat alias),
    case-insensitively; `coder` (Codex/GPT) and `premium` are unchanged; None stays None.
    """
    from hermes_cli import kanban_db as kb
    assert kb._canonical_assignee("coder-claude") == "premium"
    assert kb._canonical_assignee("Coder-Claude") == "premium"
    assert kb._canonical_assignee("premium") == "premium"
    assert kb._canonical_assignee("coder") == "coder"
    assert kb._canonical_assignee(None) is None
    # No invented opus-coder lane.
    assert "opus-coder" not in kb._LANE_ALIASES
    assert kb._LANE_ALIASES.get("coder-claude") == "premium"


def test_phase_a_premium_is_canonical_claude_lane_no_opus_coder():
    """Phase A invariant guard: premium stays the canonical Claude coder lane,
    is wired everywhere it needs to be, and no `opus-coder` lane is introduced."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import planspecs
    from hermes_cli import kanban_decompose as kd
    assert kb._LANE_SEED_API_STANDARD["premium"]["worker_runtime"] == "claude-cli"
    assert kb._LANE_SEED_MAX_ABO["premium"]["worker_runtime"] == "claude-cli"
    assert "premium" in kb._DEFAULT_REVIEW_CODE_ROLES
    assert "premium" in planspecs.VALID_PLANSPEC_LANES
    assert "premium" in kd._WORKER_SCOPE_LANES
    assert kb.AUTO_RETRY_ESCALATION_PROFILE == "premium"
    # coder-claude stays an accepted name for back-compat (its routing target is premium).
    assert "coder-claude" in planspecs.VALID_PLANSPEC_LANES
    assert "coder-claude" in kb._DEFAULT_REVIEW_CODE_ROLES
    # Negative invariant: opus-coder must not exist anywhere.
    assert "opus-coder" not in kb._LANE_SEED_API_STANDARD
    assert "opus-coder" not in planspecs.VALID_PLANSPEC_LANES
    assert "opus-coder" not in kd._WORKER_SCOPE_LANES
    assert "opus-coder" not in kb._LANE_ALIASES.values()

"""Tests for kanban lifecycle plugin hooks.

Verifies that claim/complete/block transitions fire the
kanban_task_claimed / kanban_task_completed / kanban_task_blocked plugin
hooks AFTER the board DB change is committed, with the documented kwargs,
and that a misbehaving hook callback never breaks the transition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_hooks(monkeypatch):
    """Register capturing callbacks for the three kanban lifecycle hooks.

    Patches the plugin manager's _hooks dict directly (the same registry
    invoke_hook reads) and restores it afterward.
    """
    mgr = get_plugin_manager()
    events: list[tuple[str, dict]] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    for hook in ("kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"):
        mgr._hooks.setdefault(hook, []).append(
            lambda _h=hook, **kw: events.append((_h, kw))
        )
    try:
        yield events
    finally:
        mgr._hooks = saved


def test_hooks_are_registered_as_valid():
    """The three lifecycle hook names are part of VALID_HOOKS."""
    assert "kanban_task_claimed" in VALID_HOOKS
    assert "kanban_task_completed" in VALID_HOOKS
    assert "kanban_task_blocked" in VALID_HOOKS


def test_claim_fires_hook(kanban_home, captured_hooks):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_claimed"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert "profile_name" in kw
    assert kw["run_id"] is not None


def test_complete_fires_hook_with_summary(kanban_home, captured_hooks):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="all done")
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_completed"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["summary"] == "all done"
    assert kw["assignee"] == "worker"


def test_block_fires_hook_with_reason(kanban_home, captured_hooks):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="needs human")
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_blocked"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["reason"] == "needs human"


def test_no_hook_on_failed_transition(kanban_home, captured_hooks):
    """complete_task on an unclaimed/nonexistent task fires no hook."""
    conn = kb.connect()
    try:
        # Completing a task that doesn't exist returns False without firing.
        assert kb.complete_task(conn, "t_doesnotexist", summary="x") is False
    finally:
        conn.close()
    assert [e for e in captured_hooks if e[0] == "kanban_task_completed"] == []


def test_complete_hook_fires_after_dependents_promoted(kanban_home):
    """complete_task runs recompute_ready BEFORE firing kanban_task_completed,
    so a plugin observing the completed event already sees dependents promoted.

    Locks the documented hook ordering: were the hook to fire before the
    dependent promotion, a plugin reading the board would see a stale 'todo'
    child — an invisible regression today (no test asserts the order).
    """
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    observed_child_status: list[str] = []
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="worker", parents=[parent]
        )
        assert kb.get_task(conn, child).status == "todo"  # gated by open parent

        def _observe(**kw):
            # The completed hook fires after the write txn committed, so the
            # child's promotion must be visible on a fresh connection.
            with kb.connect_closing() as c2:
                observed_child_status.append(kb.get_task(c2, child).status)

        mgr._hooks.setdefault("kanban_task_completed", []).append(_observe)
        kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, summary="done")
    finally:
        conn.close()
        mgr._hooks = saved
    assert observed_child_status == ["ready"]


def test_unblock_dependency_to_todo_fires_no_lifecycle_hook(kanban_home, captured_hooks):
    """unblock_task is intentionally NOT in the claim/complete/block hook set
    (see _fire_kanban_lifecycle_hook docstring). A dependency block whose
    unblock re-gates the child back to 'todo' must fire no lifecycle hook —
    no spurious completed/blocked/claimed on that transition.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="worker", parents=[parent]
        )
        # Force the gated child to 'blocked' (SQL, not the hook-firing API),
        # then unblock it. Parent is still open so it re-gates to 'todo'.
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
        conn.commit()
        before = len(captured_hooks)
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "todo"  # re-gated on open parent
    finally:
        conn.close()
    assert len(captured_hooks) == before  # unblock fired no lifecycle hook


def test_misbehaving_hook_does_not_break_transition(kanban_home, monkeypatch):
    """A hook callback that raises must not break the board transition."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("plugin exploded")

    mgr._hooks.setdefault("kanban_task_completed", []).append(_boom)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            # Despite the raising hook, completion succeeds and persists.
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"
        finally:
            conn.close()
    finally:
        mgr._hooks = saved

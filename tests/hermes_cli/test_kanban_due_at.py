"""K9 / D7 L2 — ``due_at`` time-based scheduling gate in recompute_ready.

A task with a FUTURE ``due_at`` is held in ``todo``/``blocked`` (not promoted
to ``ready``) until the wall clock reaches it. A task with a NULL ``due_at``
(the common case) promotes exactly as before the column existed — identical
count, no extra ``promoted`` event. A past/equal ``due_at`` promotes normally.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time

import pytest


def _is_purgeable_hermes_module(name: str) -> bool:
    return (
        name.startswith("hermes_cli")
        or name.startswith("hermes_state")
        or name == "hermes_constants"
    )


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_due_at_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Purge so kanban_db re-imports against the fresh HERMES_HOME, then restore
    # the original module objects on teardown (an unrestored purge causes a
    # module-identity split that contaminates later test files).
    saved = {
        name: mod for name, mod in sys.modules.items()
        if _is_purgeable_hermes_module(name)
    }
    for name in saved:
        del sys.modules[name]
    from hermes_cli import kanban_db

    try:
        yield kanban_db
    finally:
        for name in [n for n in sys.modules if _is_purgeable_hermes_module(n)]:
            del sys.modules[name]
        sys.modules.update(saved)
        shutil.rmtree(test_home, ignore_errors=True)


def _make_todo(kb, conn, *, due_at=None, status="todo", title="t"):
    task_id = kb.create_task(conn, title=title, assignee="coder")
    conn.execute(
        "UPDATE tasks SET status = ?, due_at = ? WHERE id = ?",
        (status, due_at, task_id),
    )
    conn.commit()
    return task_id


def _promoted_events(conn, task_id):
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'promoted'",
            (task_id,),
        )
    ]


def _status(conn, task_id):
    return conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["status"]


def test_future_due_task_held(isolated_kanban_home):
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_todo(kb, conn, due_at=int(time.time()) + 3600)
    with kb.connect_closing() as conn:
        promoted = kb.recompute_ready(conn)
        assert _status(conn, task_id) == "todo"
        assert _promoted_events(conn, task_id) == []
    # The future-due task contributed nothing to the promote count.
    assert promoted == 0


def test_null_due_task_promoted_as_before(isolated_kanban_home):
    """Regression baseline: NULL due_at promotes exactly as today."""
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_todo(kb, conn, due_at=None)
    with kb.connect_closing() as conn:
        promoted = kb.recompute_ready(conn)
        assert _status(conn, task_id) == "ready"
        # Exactly one 'promoted' event — no extra, no missing.
        assert _promoted_events(conn, task_id) == ["promoted"]
    assert promoted == 1


def test_past_due_task_promoted(isolated_kanban_home):
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_todo(kb, conn, due_at=int(time.time()) - 10)
    with kb.connect_closing() as conn:
        promoted = kb.recompute_ready(conn)
        assert _status(conn, task_id) == "ready"
        assert _promoted_events(conn, task_id) == ["promoted"]
    assert promoted == 1


def test_future_then_due_becomes_eligible(isolated_kanban_home):
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_todo(kb, conn, due_at=int(time.time()) + 3600)
    # First pass: held.
    with kb.connect_closing() as conn:
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, task_id) == "todo"
    # The due time passes (simulate by moving due_at into the past).
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET due_at = ? WHERE id = ?",
            (int(time.time()) - 1, task_id),
        )
        conn.commit()
    with kb.connect_closing() as conn:
        assert kb.recompute_ready(conn) == 1
        assert _status(conn, task_id) == "ready"
        # Still exactly one 'promoted' — the held pass emitted none.
        assert _promoted_events(conn, task_id) == ["promoted"]


def test_future_due_does_not_affect_other_tasks(isolated_kanban_home):
    """A held future-due task must not change the promote count of its peers."""
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        held = _make_todo(kb, conn, due_at=int(time.time()) + 3600, title="held")
        ready_now = _make_todo(kb, conn, due_at=None, title="now")
    with kb.connect_closing() as conn:
        promoted = kb.recompute_ready(conn)
        assert _status(conn, held) == "todo"
        assert _status(conn, ready_now) == "ready"
    assert promoted == 1


def test_blocked_future_due_held(isolated_kanban_home):
    """A non-sticky blocked task with a future due_at stays blocked."""
    kb = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_todo(
            kb, conn, due_at=int(time.time()) + 3600, status="blocked"
        )
    with kb.connect_closing() as conn:
        assert kb.recompute_ready(conn) == 0
        assert _status(conn, task_id) == "blocked"

"""Regression tests: a task born ``blocked`` (``create_task(...,
initial_status="blocked")``) must stay blocked across the promotion
sweep exactly like a worker-initiated ``kanban_block`` — not silently
auto-promoted.

Live incident (kanban.db, task_events t_50a1983f, 2026-07-03):
``create_task`` with ``initial_status="blocked"`` only set
``status='blocked'`` on the row; it emitted no ``"blocked"`` event.
``_has_sticky_block`` returns ``False`` when there is no blocked/
unblocked event at all (the pre-#28712 circuit-breaker semantics), so
``recompute_ready`` treated the deliberately-parked task as a transient
breaker block and promoted it — a dispatch-worthy card meant to wait
for the operator got dispatched, re-blocked itself, and burned two
worker runs before anyone noticed.

Fix: ``create_task`` now emits a ``"blocked"`` event in the same
write_txn when ``initial_status == "blocked"``, so the existing sticky
guard covers the born-blocked path with no new predicate.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_born_blocked_task_is_not_auto_promoted(kanban_home: Path) -> None:
    """The exact t_50a1983f shape: a standalone task created directly with
    ``initial_status="blocked"`` (no parents, so the parent-completion
    check is vacuously true) must stay blocked across repeated
    ``recompute_ready`` ticks and must not emit a ``promoted`` event."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="operator design decision — do not dispatch",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "born-blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"

        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        kinds = [r["kind"] for r in events]
        assert "blocked" in kinds, "create_task must emit a blocked event"
        assert "promoted" not in kinds


def test_born_blocked_child_with_done_parents_stays_blocked(kanban_home: Path) -> None:
    """The parent-completion path is what actually promoted t_50a1983f-shaped
    tasks in production once any dependency finished — pin the child case too."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(
            conn,
            title="child — born blocked",
            parents=[parent],
            initial_status="blocked",
        )
        kb.complete_task(conn, parent, result="parent ok")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


def test_circuit_breaker_block_without_event_still_auto_recovers(
    kanban_home: Path,
) -> None:
    """Regression (a): a task blocked purely by the circuit breaker — status
    flipped with a ``gave_up`` event, no ``blocked`` event, failures below
    the limit — must keep auto-recovering once its parents are done. The
    born-blocked fix must not make ``_has_sticky_block`` fire for this path."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (child,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (child, int(time.time())),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1, "circuit-breaker block must still auto-recover"
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_makes_born_blocked_task_promotable(kanban_home: Path) -> None:
    """Regression (b): explicit ``unblock_task`` is the legitimate exit — after
    it, the born-blocked task must become promotable again."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(
            conn,
            title="child — born blocked",
            parents=[parent],
            initial_status="blocked",
        )

        assert kb.unblock_task(conn, child)
        # unblock_task on a parent-gated task returns it to 'todo', not
        # 'ready' — it is no longer stuck, just waiting for the parent.
        assert kb.get_task(conn, child).status == "todo"

        # complete_task promotes eligible children itself
        # (recompute_ready runs inside it); the child must now be
        # promotable instead of trapped as sticky-blocked.
        kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, child).status == "ready"


def test_release_gate_double_blocked_event_stays_harmless(kanban_home: Path) -> None:
    """The Release-Gate caller (kanban_worktrees.py) already appends its own
    ``blocked`` event after ``create_task(..., initial_status="blocked")``.
    With the fix, that produces two consecutive ``blocked`` events for the
    same task — ``_has_sticky_block`` only looks at the latest one, so the
    duplicate must stay harmless (still sticky, still not auto-promoted)."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(
            conn,
            title="release-gate child",
            parents=[parent],
            initial_status="blocked",
        )
        # Mirrors kanban_worktrees._create_release_gate_child: a second
        # explicit blocked event on top of create_task's own.
        kb.add_event(conn, child, "blocked", {"reason": "awaiting release-gate GO"})
        kb.complete_task(conn, parent, result="ok")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"

"""Regression coverage for automatic repair of broken operator-held chains."""

from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_chain_repair import repair_broken_operator_holds


def _held_chain(conn, *, title: str) -> tuple[str, str]:
    """Build the production-shaped root <- child dependency topology."""
    root_id = kb.create_task(conn, title=f"{title} root", assignee="coder", triage=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET freigabe = 'operator' WHERE id = ?", (root_id,))
    child_ids = kb.decompose_triage_task(
        conn,
        root_id,
        root_assignee="coder",
        children=[{"title": f"{title} worker"}],
        initial_child_status="scheduled",
    )
    assert child_ids is not None
    return root_id, child_ids[0]


def _status(conn, task_id: str) -> str:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return str(row["status"])


def _freigabe(conn, task_id: str) -> str | None:
    row = conn.execute("SELECT freigabe FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return row["freigabe"]


def test_repair_releases_only_broken_operator_held_chain_and_reports_once(kanban_home):
    with kb.connect_closing() as conn:
        broken_root, completed_member = _held_chain(conn, title="broken chain")
        intact_root, intact_member = _held_chain(conn, title="intact chain")
        assert _status(conn, broken_root) == "scheduled"
        assert _status(conn, completed_member) == "scheduled"
        assert _status(conn, intact_root) == "scheduled"
        assert _status(conn, intact_member) == "scheduled"

        # Reproduce the production inconsistency: one linked chain member already
        # completed while its freigabe:operator root and successor stay held.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (completed_member,))
            kb._append_event(conn, completed_member, "completed", {"summary": "already ran"})

        repairs = repair_broken_operator_holds(conn)
        repeat = repair_broken_operator_holds(conn)

        assert [repair.root_id for repair in repairs] == [broken_root]
        assert repairs[0].completed_member_id == completed_member
        assert repeat == []
        assert _status(conn, broken_root) == "ready"
        assert _status(conn, intact_root) == "scheduled"
        assert _freigabe(conn, intact_root) == "operator"
        assert _status(conn, intact_member) == "scheduled"

        reports = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id",
            (broken_root, kb.OPERATOR_ESCALATION_EVENT),
        ).fetchall()

    assert len(reports) == 1
    payload = json.loads(reports[0]["payload"])
    assert payload["task"]["id"] == broken_root
    assert payload["evidence"]["completed_member"]["id"] == completed_member
    assert "broken chain worker" in payload["why_now"]

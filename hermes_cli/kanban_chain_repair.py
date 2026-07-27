"""Repair interrupted ``freigabe:operator`` Kanban chains.

The normal dispatcher intentionally leaves an operator-held chain untouched.  If
one of its linked members nevertheless completed, that hold can no longer be a
coherent operator decision: the root stays scheduled and ordinary readiness
recomputation does not consider scheduled tasks.  This fork-owned sweep repairs
that narrow state by using the existing chain-wide release path and emits an
``operator_escalation`` report consumed by the established alert channel.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

from hermes_cli import kanban_db as kb


@dataclass(frozen=True)
class BrokenOperatorHoldRepair:
    """One chain-wide operator-hold repair performed by the sweep."""

    root_id: str
    completed_member_id: str


def _task_rows(conn: sqlite3.Connection, task_ids: list[str]) -> list[sqlite3.Row]:
    if not task_ids:
        return []
    placeholders = ", ".join("?" for _ in task_ids)
    return conn.execute(
        f"SELECT id, title, status FROM tasks WHERE id IN ({placeholders}) ORDER BY id",
        task_ids,
    ).fetchall()


def _report_repair(
    conn: sqlite3.Connection,
    *,
    root: sqlite3.Row,
    member_rows: list[sqlite3.Row],
    completed_member: sqlite3.Row,
) -> None:
    """Emit the existing operator report event after a successful release."""
    root_id = str(root["id"])
    completed_id = str(completed_member["id"])
    completed_title = str(completed_member["title"] or completed_id)
    payload: dict[str, Any] = {
        "task": {"id": root_id, "title": str(root["title"] or root_id)},
        "why_now": (
            "Automatische Reparatur hat die angebrochene Operator-Freigabe "
            f"der Kette {root_id} aufgehoben: bereits gelaufenes Glied "
            f"{completed_title} ({completed_id})."
        ),
        "attempts_already_made": 1,
        "evidence": {
            "chain_root": root_id,
            "chain_members": [str(member["id"]) for member in member_rows],
            "completed_member": {"id": completed_id, "title": completed_title},
        },
        "recommended_human_action": (
            "Keine Aktion erforderlich; die automatische Kettenfreigabe bei "
            "unerwartetem Ablauf pruefen."
        ),
        "blocked_action_boundary": [],
    }
    with kb.write_txn(conn):
        kb._append_event(conn, root_id, kb.OPERATOR_ESCALATION_EVENT, payload)


def repair_broken_operator_holds(conn: sqlite3.Connection) -> list[BrokenOperatorHoldRepair]:
    """Release and report every actually interrupted operator-held chain.

    Candidates must still be ``scheduled`` with ``freigabe:operator``.  A chain
    is repaired only when it has linked members and at least one is already
    ``done``; intact operator holds are deliberately ignored.  Releasing through
    :func:`kanban_db.release_freigabe_hold` preserves the single chain-wide
    release contract.  On the next sweep the root is no longer an eligible
    scheduled candidate, which makes both release and report idempotent.
    """
    candidates = conn.execute(
        "SELECT id, title FROM tasks "
        "WHERE status = 'scheduled' AND freigabe = 'operator' ORDER BY id"
    ).fetchall()
    repairs: list[BrokenOperatorHoldRepair] = []

    for root in candidates:
        root_id = str(root["id"])
        member_rows = _task_rows(conn, kb._chain_member_ids_from_sink(conn, root_id))
        if len(member_rows) < 2:
            continue
        completed_members = [member for member in member_rows if member["status"] == "done"]
        if not completed_members:
            continue
        completed_member = completed_members[0]
        if not kb.release_freigabe_hold(conn, root_id, author="chain-repair"):
            continue
        _report_repair(
            conn,
            root=root,
            member_rows=member_rows,
            completed_member=completed_member,
        )
        repairs.append(
            BrokenOperatorHoldRepair(
                root_id=root_id,
                completed_member_id=str(completed_member["id"]),
            )
        )

    return repairs

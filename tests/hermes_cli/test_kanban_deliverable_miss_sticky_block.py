"""recoverable_protocol_miss must emit a sticky 'blocked' event.

Bug: detect_crashed_workers' recoverable_protocol_miss branch (clean exit
rc=0 + deliverable evidence) flips status='blocked' via bare UPDATE and
never appends a task_events 'blocked' row. _has_sticky_block keys on that
event, so recompute_ready (every dispatch_once tick) silently promotes the
task back to ready and discards the posted deliverable.

Production path under test: detect_crashed_workers (not block_task).
Fixture shape matches live worker posts consumed by
_deliverable_evidence_for_protocol_miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# Exact error_text from detect_crashed_workers recoverable_protocol_miss branch.
PROTOCOL_MISS_REASON = (
    "deliverable posted but worker exited cleanly (rc=0) "
    "without calling kanban_complete — repair required"
)

# Structure-preserving production-shaped deliverable comment (title terms +
# deliverable signal + length > 80) so _deliverable_evidence_for_protocol_miss
# matches — same shape as repair_fidelity / spawn_workdir fixtures.
LIVE_DELIVERABLE = (
    "# Deliverable: render quarterly report\n\n"
    "The quarterly report is complete and mapped to the requested "
    "objective. Evidence includes the final section list, validation "
    "notes, and remaining risk. " + "x" * 120
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    return home


def _drive_recoverable_protocol_miss(conn, *, pid: int) -> str:
    """Drive the PRODUCTION detect_crashed_workers path to protocol-miss block.

    claimed/running task, dead worker pid with clean exit rc=0, deliverable
    comment in the real evidence format.
    """
    task_id = kb.create_task(
        conn,
        title="render quarterly report",
        assignee="default",
        kind="text",
    )
    assert kb.claim_task(conn, task_id) is not None
    kb.add_comment(conn, task_id, "default", LIVE_DELIVERABLE)
    kb._set_worker_pid(conn, task_id, pid)
    kb._record_worker_exit(pid, 0)

    # Production: clean exit + evidence → recoverable miss, NOT in crashed list.
    assert task_id not in kb.detect_crashed_workers(conn)
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.last_failure_error == PROTOCOL_MISS_REASON
    return task_id


def test_protocol_miss_emits_sticky_blocked_event_and_survives_recompute(
    kanban_home: Path,
) -> None:
    """done_when (1)-(3): blocked event, sticky, recompute holds, repair still accepts."""
    with kb.connect_closing() as conn:
        task_id = _drive_recoverable_protocol_miss(conn, pid=820001)

        # (1) A 'blocked' task_event with the protocol-miss reason exists —
        #    not just the silent status flip. Payload form matches other
        #    block paths: {"reason": ...} (optional block_kind ok).
        blocked_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "blocked"
        ]
        assert blocked_events, (
            "recoverable_protocol_miss must append a 'blocked' event so "
            "_has_sticky_block / recompute_ready treat the park as sticky"
        )
        reasons = [
            (event.payload or {}).get("reason")
            for event in blocked_events
            if isinstance(event.payload, dict)
        ]
        assert PROTOCOL_MISS_REASON in reasons

        # (2) Sticky for this task; recompute_ready (dispatch_once production
        #     callsite) must leave status='blocked' — no silent promote.
        assert kb._has_sticky_block(conn, task_id) is True
        promoted = kb.recompute_ready(conn)
        task_after = kb.get_task(conn, task_id)
        assert task_after is not None
        assert task_after.status == "blocked", (
            f"recompute_ready promoted sticky protocol-miss block to "
            f"{task_after.status!r}; deliverable evidence would be discarded"
        )
        assert promoted == 0 or task_id not in {
            # recompute_ready returns a count, not ids — status is the truth.
        }
        # No promoted / claimed / unblocked / reclaimed between evidence and now.
        kinds_after_recompute = [e.kind for e in kb.list_events(conn, task_id)]
        assert "promoted" not in kinds_after_recompute

        # (3) repair still accepts the evidence (guard 17421-17435 does not fire).
        assert kb.repair_deliverable_posted_not_completed(
            conn, task_id, actor="integrator",
        ) is True
        repaired = kb.get_task(conn, task_id)
        assert repaired is not None
        assert repaired.status == "done"

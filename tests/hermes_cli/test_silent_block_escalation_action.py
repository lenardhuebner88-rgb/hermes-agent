"""Silent-block escalation action text for release-gate parks."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt


RELEASE_GATE_REASON = "awaiting release-gate GO"
NON_RELEASE_REASON = (
    "per-task input-token cap exceeded: 2292964 > 2000000 "
    "(cumulative input across 6 run(s))"
)
GENERIC_SILENT_BLOCK_ACTION = (
    "inspect the task, answer any operator question, and decide whether "
    "to unblock/reassign/close — the worker loop cannot proceed alone"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _operator_escalation_payload(conn, task_id: str) -> dict:
    return next(
        event.payload
        for event in kb.list_events(conn, task_id)
        if event.kind == kb.OPERATOR_ESCALATION_EVENT
    )


def test_silent_block_release_gate_payload_names_release_gate_command(
    kanban_home, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        source = kb.create_task(
            conn,
            title="merged source integration",
            assignee="coder",
        )
        child = kwt._create_parked_release_gate_child(
            conn,
            source,
            source,
            {"merge_commit": "abc1234"},
        )
        assert child is not None

        blocked = [
            event.payload
            for event in kb.list_events(conn, child)
            if event.kind == "blocked"
        ]
        assert blocked[-1]["reason"] == RELEASE_GATE_REASON
        assert kb.silent_block_task_ids(conn, now=base) == [child]

        kb.escalate_silent_blocks_sweep(conn, now=base)
        payload = _operator_escalation_payload(conn, child)

    assert (
        payload["recommended_human_action"]
        == f"run `hermes kanban release-gate {child}` to process the parked "
        "release gate"
    )
    assert payload["evidence"]["release_gate_candidate"] is True
    assert payload["evidence"]["last_error"] == RELEASE_GATE_REASON


def test_silent_block_non_release_reason_keeps_generic_action(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="token cap block", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, task_id),
        )
        assert kb.claim_task(conn, task_id) is not None
        kb.block_task(conn, task_id, reason=NON_RELEASE_REASON)
        assert kb.silent_block_task_ids(conn, now=base) == [task_id]

        kb.escalate_silent_blocks_sweep(conn, now=base)
        payload = _operator_escalation_payload(conn, task_id)

    assert payload["recommended_human_action"] == GENERIC_SILENT_BLOCK_ACTION
    assert payload["evidence"]["last_error"] == NON_RELEASE_REASON
    assert "release_gate_candidate" not in payload["evidence"]

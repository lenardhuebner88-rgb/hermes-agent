"""Episode-scoped deduplication for the silent-block safety net."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _operator_escalations(conn, task_id: str):
    return [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == kb.OPERATOR_ESCALATION_EVENT
    ]


def test_silent_block_sweep_rearms_after_operator_unblock(
    kanban_home, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="re-blocked task", assignee="alice")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Which credential should I use?",
            kind="needs_input",
        )

        assert kb.silent_block_task_ids(conn) == [task_id]
        first = kb.escalate_silent_blocks_sweep(conn)
        assert [row["task_id"] for row in first["escalated"]] == [task_id]
        assert len(_operator_escalations(conn, task_id)) == 1

        assert kb.unblock_task(conn, task_id)
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Required database capability is unavailable",
            kind="capability",
        )

        assert kb.silent_block_task_ids(conn) == [task_id]
        second = kb.escalate_silent_blocks_sweep(conn)
        assert [row["task_id"] for row in second["escalated"]] == [task_id]
        assert len(_operator_escalations(conn, task_id)) == 2


def test_silent_block_sweep_rearms_after_manual_promotion(
    kanban_home, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="promoted re-block", assignee="alice")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Which credential should I use?",
            kind="needs_input",
        )

        first = kb.escalate_silent_blocks_sweep(conn)
        assert [row["task_id"] for row in first["escalated"]] == [task_id]

        assert kb.promote_task(conn, task_id, actor="operator") == (True, None)
        assert kb._operator_escalation_is_active(conn, task_id) is False
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Required database capability is unavailable",
            kind="capability",
        )

        assert kb.silent_block_task_ids(conn) == [task_id]


def test_silent_block_sweep_stays_idempotent_within_one_block_episode(
    kanban_home, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="still blocked", assignee="alice")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(conn, task_id, reason="Which credential should I use?")

        first = kb.escalate_silent_blocks_sweep(conn)
        second = kb.escalate_silent_blocks_sweep(conn)

        assert [row["task_id"] for row in first["escalated"]] == [task_id]
        assert second["escalated"] == []
        assert kb.silent_block_task_ids(conn) == []
        assert len(_operator_escalations(conn, task_id)) == 1


def test_nonspawnable_escalation_does_not_dedup_later_silent_block(
    kanban_home, monkeypatch
):
    from hermes_cli import profiles

    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="misassigned then blocked",
            assignee="ui-verifier",
        )
        kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        first = _operator_escalations(conn, task_id)
        assert len(first) == 1
        assert first[0].payload["evidence"]["trigger_outcome"] == (
            "nonspawnable_assignee"
        )

        monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(conn, task_id, reason="Which credential should I use?")

        assert kb.silent_block_task_ids(conn) == [task_id]
        sweep = kb.escalate_silent_blocks_sweep(conn)
        assert [row["task_id"] for row in sweep["escalated"]] == [task_id]
        assert len(_operator_escalations(conn, task_id)) == 2

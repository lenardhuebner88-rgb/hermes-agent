"""Budget-runaway operator_escalation / heiler_classification dedup.

Live board 2026-07-16: task t_852cf8ee received three identical
operator_escalation pages (16:34 → 18:14) with byte-identical evidence
``{"input_token_sum": 4148125, "per_task_input_token_cap": 4000000, "runs": 17}``
because ``_park_budget_runaway`` re-paged on every park after operator
unblock, even when the cumulative token sum had not changed.

Production call path: ``gateway/kanban_watchers.py`` → ``dispatch_once``
with ``per_task_input_token_cap`` set (same kwarg the dispatcher tick uses).
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb


# Live evidence shape from kanban.db (read-only 7d window, 2026-07-16).
_LIVE_INPUT_TOKEN_SUM = 4_148_125
_LIVE_CAP = 4_000_000
_LIVE_RUNS = 17


def _seed_input_token_run(conn, task_id, *, input_tokens, profile="alice"):
    """Insert a completed task_run outside the respawn-guard success window.

    Mirrors tests/hermes_cli/test_kanban_db_tokens_workspace.py — the G1
    per-task sum is age-independent, so a stale run still counts toward the
    cap while leaving the task otherwise spawnable.
    """
    end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 300
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, "
        "started_at, ended_at, input_tokens) "
        "VALUES (?, ?, 'done', 'completed', ?, ?, ?)",
        (task_id, profile, end - 300, end, input_tokens),
    )


def _seed_live_runaway_runs(conn, task_id: str) -> None:
    """Seed 17 completed runs summing to the live 4_148_125 input tokens."""
    base = _LIVE_INPUT_TOKEN_SUM // _LIVE_RUNS
    remainder = _LIVE_INPUT_TOKEN_SUM - base * _LIVE_RUNS
    for i in range(_LIVE_RUNS):
        tokens = base + (remainder if i == 0 else 0)
        _seed_input_token_run(conn, task_id, input_tokens=tokens)


def _count_kinds(events, kind: str) -> int:
    return sum(1 for e in events if e.kind == kind)


def test_budget_runaway_dedup_same_evidence_after_operator_unblock(
    kanban_home, all_assignees_spawnable
):
    """Park → operator unblock → same-cap re-trip: still one page only.

    Production path: dispatch_once(..., per_task_input_token_cap=...).
    Park/block may re-occur; operator_escalation + heiler_classification
    must not grow when input_token_sum is unchanged.
    """
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="runaway-dedup", assignee="alice")
        _seed_live_runaway_runs(conn, task_id)

        first = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=_LIVE_CAP,
        )
        assert (task_id, _LIVE_INPUT_TOKEN_SUM) in first.budget_runaway_parked
        assert kb.get_task(conn, task_id).status == "blocked"

        events_after_first = kb.list_events(conn, task_id)
        assert _count_kinds(events_after_first, kb.OPERATOR_ESCALATION_EVENT) == 1
        assert _count_kinds(events_after_first, kb.HEILER_CLASSIFICATION_EVENT) == 1
        esc = next(
            e for e in events_after_first if e.kind == kb.OPERATOR_ESCALATION_EVENT
        )
        # Live evidence format (field names + values).
        assert esc.payload["evidence"] == {
            "input_token_sum": _LIVE_INPUT_TOKEN_SUM,
            "per_task_input_token_cap": _LIVE_CAP,
            "runs": _LIVE_RUNS,
        }

        # Operator unblocks → ready (clears _operator_escalation_is_active).
        assert kb.unblock_task(conn, task_id) is True
        assert kb.get_task(conn, task_id).status == "ready"

        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=_LIVE_CAP,
        )
        # Park itself still happens on the re-trip.
        assert (task_id, _LIVE_INPUT_TOKEN_SUM) in second.budget_runaway_parked
        assert kb.get_task(conn, task_id).status == "blocked"

        events_after_second = kb.list_events(conn, task_id)
        assert _count_kinds(events_after_second, kb.OPERATOR_ESCALATION_EVENT) == 1
        assert _count_kinds(events_after_second, kb.HEILER_CLASSIFICATION_EVENT) == 1
        # Park protocol event may accumulate; pages must not.
        assert _count_kinds(events_after_second, kb.BUDGET_RUNAWAY_PARKED_EVENT) >= 1


def test_budget_runaway_re_escalates_when_input_token_sum_grows(
    kanban_home, all_assignees_spawnable
):
    """New evidence (higher cumulative sum) pages again after unblock.

    Guard is per-evidence/episode, not a lifetime muzzle.
    """
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="runaway-new-ev", assignee="alice")
        _seed_live_runaway_runs(conn, task_id)

        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=_LIVE_CAP,
        )
        assert kb.unblock_task(conn, task_id) is True

        # New run burns more input tokens → sum changes → re-escalate.
        extra = 50_000
        _seed_input_token_run(conn, task_id, input_tokens=extra)
        new_sum = _LIVE_INPUT_TOKEN_SUM + extra
        new_runs = _LIVE_RUNS + 1

        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: pytest.fail("must not spawn"),
            per_task_input_token_cap=_LIVE_CAP,
        )
        assert (task_id, new_sum) in second.budget_runaway_parked
        assert kb.get_task(conn, task_id).status == "blocked"

        events = kb.list_events(conn, task_id)
        escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
        heilers = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
        assert len(escalations) == 2
        assert len(heilers) == 2
        assert escalations[-1].payload["evidence"] == {
            "input_token_sum": new_sum,
            "per_task_input_token_cap": _LIVE_CAP,
            "runs": new_runs,
        }

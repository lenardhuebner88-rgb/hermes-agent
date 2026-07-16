"""Regression tests for timed ``kanban schedule`` holds."""

from __future__ import annotations

import argparse
import time

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    top = parser.add_subparsers(dest="_top")
    kc.build_parser(top)
    return parser


def _create_task() -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="timer", assignee="default")


def _schedule(task_id: str, *extra: str) -> int:
    args = _parser().parse_args(["kanban", "schedule", task_id, *extra])
    return kc._cmd_schedule(args)


def test_schedule_due_relative_persists_and_wakes_through_sweep(kanban_home):
    task_id = _create_task()

    before = int(time.time())
    assert _schedule(task_id, "--due", "+2h") == 0
    after = int(time.time())

    with kb.connect_closing() as conn:
        scheduled = kb.get_task(conn, task_id)
        assert scheduled is not None
        assert scheduled.status == "scheduled"
        assert before + 7200 - 5 <= scheduled.due_at <= after + 7200 + 5

        early = kb.no_silent_stall_sweep(conn, now=scheduled.due_at - 1)
        assert early["self_healed"] == []
        assert kb.get_task(conn, task_id).status == "scheduled"

        due = kb.no_silent_stall_sweep(conn, now=scheduled.due_at)
        assert {"task_id": task_id, "class": "scheduled_due", "action": "unblocked"} in due["self_healed"]
        assert kb.get_task(conn, task_id).status == "ready"

    assert kc._task_to_dict(scheduled)["due_at"] == scheduled.due_at


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-16T23:05:00+02:00", 1784235900),
        ("1784235900", 1784235900),
    ],
)
def test_schedule_due_accepts_iso_offset_and_epoch(raw, expected):
    args = _parser().parse_args(["kanban", "schedule", "t_abc", "--due", raw])
    assert args.due == expected


def test_schedule_due_invalid_value_rejected_without_db_write(kanban_home):
    task_id = _create_task()

    with pytest.raises(SystemExit):
        _parser().parse_args(["kanban", "schedule", task_id, "--due", "tomorrow"])

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"
    assert task.due_at is None


def test_schedule_without_due_keeps_legacy_overdue_nudge(kanban_home):
    task_id = _create_task()
    assert _schedule(task_id) == 0

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.due_at is None
        summary = kb.no_silent_stall_sweep(conn, now=task.created_at + 7200, min_age_seconds=3600)
        assert {"task_id": task_id, "class": "scheduled_overdue", "action": "unblocked"} in summary["self_healed"]


def test_schedule_bulk_assigns_same_due_at(kanban_home):
    first = _create_task()
    second = _create_task()

    args = _parser().parse_args(["kanban", "schedule", first, "--ids", second, "--due", "+90s"])
    assert kc._cmd_schedule(args) == 0

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, first).due_at == kb.get_task(conn, second).due_at

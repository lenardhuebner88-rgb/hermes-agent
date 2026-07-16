"""Released archived dependencies must not keep scheduled descendants parked."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _scheduled_child(conn, *, root_status):
    root_id = kb.create_task(conn, title=f"{root_status} root")
    child_id = kb.create_task(conn, title="scheduled child")
    kb.link_tasks(conn, root_id, child_id)
    if root_status == "archived":
        assert kb.archive_task(conn, root_id)
    else:
        assert root_status == "done"
        assert kb.complete_task(conn, root_id, result="complete")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
            (child_id,),
        )
    return root_id, child_id


def test_sweep_nudges_scheduled_child_of_archived_root_after_release(kanban_home):
    with kb.connect() as conn:
        _, child_id = _scheduled_child(conn, root_status="archived")

        summary = kb.no_silent_stall_sweep(
            conn,
            now=int(time.time()) + 1,
            min_age_seconds=0,
        )

        assert kb.get_task(conn, child_id).status == "ready"
        assert child_id not in summary["skipped_archived_chain"]
        assert any(item["task_id"] == child_id for item in summary["self_healed"])


def test_sweep_still_nudges_scheduled_child_of_done_root(kanban_home):
    with kb.connect() as conn:
        _, child_id = _scheduled_child(conn, root_status="done")

        summary = kb.no_silent_stall_sweep(
            conn,
            now=int(time.time()) + 1,
            min_age_seconds=0,
        )

        assert kb.get_task(conn, child_id).status == "ready"
        assert child_id not in summary["skipped_archived_chain"]
        assert any(
            item["task_id"] == child_id for item in summary["self_healed"]
        )

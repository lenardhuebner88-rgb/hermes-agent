"""Read-only kanban adapter for the Design Board — never writes task state."""
from __future__ import annotations

from hermes_cli import kanban_db

TERMINAL = {"done", "archived"}

_get_task = kanban_db.get_task


def _open_ro():
    # connect_closing() resolves the active board's kanban.db itself and is the
    # documented convention for long-lived readers (kanban_db.py:2766) — avoids
    # FD exhaustion in the dashboard process.
    return kanban_db.connect_closing()


def task_facets(task_ids: list[str]) -> list[dict]:
    if not task_ids:
        return []
    out: list[dict] = []
    with _open_ro() as conn:
        for tid in task_ids:
            task = _get_task(conn, tid)
            if task is None:
                continue
            out.append({
                "id": task.id, "status": task.status,
                "assignee": task.assignee, "terminal": task.status in TERMINAL,
            })
    return out

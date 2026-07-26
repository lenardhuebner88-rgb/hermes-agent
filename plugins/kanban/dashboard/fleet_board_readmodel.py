"""Compact, limit-independent read model for Fleet → Board.

The dashboard's normal board payload intentionally pages ``done`` and strips
long card bodies.  Fleet still needs truthful overview counts and saved-view
counts over the selected board.  This module derives those values from the
full in-memory task set before either payload optimization is applied.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Iterable, Mapping


_TERMINAL_STATUSES = frozenset({"done", "archived"})
_KNOWN_STATUSES = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
    "done",
    "archived",
)


def build_board_summary(
    tasks: Iterable[Any],
    *,
    link_counts: Mapping[str, Mapping[str, int]],
    observed_at: int | None = None,
) -> dict[str, Any]:
    """Return a stable Fleet overview over *all* selected-board tasks.

    ``with_result`` deliberately spans every status.  The frontend labels that
    global scope instead of implying that it is an open-work count.
    """

    task_list = list(tasks)
    counts = Counter(str(getattr(task, "status", "") or "todo") for task in task_list)
    status_counts = {status: int(counts.get(status, 0)) for status in _KNOWN_STATUSES}
    open_tasks = [
        task
        for task in task_list
        if str(getattr(task, "status", "") or "") not in _TERMINAL_STATUSES
    ]

    def _has_result(task: Any) -> bool:
        return bool(str(getattr(task, "result", "") or "").strip())

    def _is_standalone(task: Any) -> bool:
        links = link_counts.get(
            str(getattr(task, "id", "") or ""),
            {"parents": 0, "children": 0},
        )
        return int(links.get("parents", 0)) == 0 and int(links.get("children", 0)) == 0

    return {
        "total_count": len(task_list),
        "open_count": len(open_tasks),
        "status_counts": status_counts,
        "quick_counts": {
            "unassigned_open": sum(
                not str(getattr(task, "assignee", "") or "").strip()
                for task in open_tasks
            ),
            "review": status_counts["review"],
            "standalone_open": sum(_is_standalone(task) for task in open_tasks),
            "with_result": sum(_has_result(task) for task in task_list),
        },
        "observed_at": int(time.time() if observed_at is None else observed_at),
    }

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli.fleet_metrics_readmodel import _queue_projection


class _CountingConnection(sqlite3.Connection):
    timeline_input_rows: list[int]
    timeline_select: str | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.timeline_input_rows = []
        self.timeline_select = None

    def execute(  # type: ignore[override]
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Cursor:
        normalized = " ".join(sql.split())
        if (
            "FROM worker_run_timeline_events" in normalized
            and "GROUP BY task_run_id" in normalized
        ):
            self.timeline_select = normalized
            group_position = normalized.index("GROUP BY task_run_id")
            where_clause = "WHERE observed_at_ms >= ?"
            if where_clause in normalized[:group_position]:
                count_row = sqlite3.Connection.execute(
                    self,
                    """
                    SELECT COUNT(*)
                      FROM worker_run_timeline_events
                     WHERE observed_at_ms >= ?
                    """,
                    parameters[:1],
                ).fetchone()
            else:
                count_row = sqlite3.Connection.execute(
                    self,
                    "SELECT COUNT(*) FROM worker_run_timeline_events",
                ).fetchone()
            self.timeline_input_rows.append(int(count_row[0]))
        return super().execute(sql, parameters)


def test_queue_projection_filters_timeline_before_grouping(
    tmp_path: Path,
) -> None:
    board_path = kb.init_db(db_path=tmp_path / "kanban.db")
    with sqlite3.connect(board_path) as fixture_connection:
        fixture_connection.executemany(
            """
            INSERT INTO worker_run_timeline_events (
                task_run_id, event_kind, observed_at_ms, source,
                task_id, board, chain_root_id, profile
            ) VALUES (?, ?, ?, 'worker_runtime', ?, 'default', NULL, 'coder')
            """,
            (
                (1, "queued", 1_000, "old-task"),
                (1, "claimed", 1_300, "old-task"),
                (2, "queued", 11_000, "recent-task"),
                (2, "claimed", 15_000, "recent-task"),
                (3, "queued", 12_000, "open-task"),
                (3, "ended", 13_000, "open-task"),
            ),
        )

    connection = sqlite3.connect(
        board_path,
        factory=_CountingConnection,
    )
    connection.row_factory = sqlite3.Row
    try:
        all_window = _queue_projection(
            connection,
            now_seconds=20,
            cutoff_ms=0,
        )
        recent_window = _queue_projection(
            connection,
            now_seconds=20,
            cutoff_ms=10_000,
        )
    finally:
        connection.close()

    assert connection.timeline_input_rows == [6, 4]
    assert connection.timeline_select is not None
    assert connection.timeline_select.index("WHERE observed_at_ms >= ?") < (
        connection.timeline_select.index("GROUP BY task_run_id")
    )
    assert all_window == {
        "available": True,
        "eligible_backlog": 0,
        "queue_wait_ms": {
            "p50": 300,
            "p95": 4_000,
            "observed_runs": 2,
            "queued_runs": 3,
            "coverage": {
                "observed_rows": 2,
                "denominator_rows": 3,
                "ratio": 2 / 3,
                "status": "partial",
            },
        },
        "source": "tasks plus worker_run_timeline_events",
    }
    assert recent_window == {
        "available": True,
        "eligible_backlog": 0,
        "queue_wait_ms": {
            "p50": 4_000,
            "p95": 4_000,
            "observed_runs": 1,
            "queued_runs": 2,
            "coverage": {
                "observed_rows": 1,
                "denominator_rows": 2,
                "ratio": 0.5,
                "status": "partial",
            },
        },
        "source": "tasks plus worker_run_timeline_events",
    }

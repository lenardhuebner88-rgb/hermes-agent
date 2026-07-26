"""Read-only HTTP delivery of the locally computed Kanban score digest."""
from __future__ import annotations

from hermes_cli.kanban_scores_digest import scores_digest


@scorecard_routes.get("/scores/digest")
def get_scores_digest(
    weeks: int = Query(4, ge=1, description="Number of ISO weeks to include"),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return the existing local score digest without external requests."""
    conn = _conn(board)
    try:
        return scores_digest(conn, weeks=weeks)
    finally:
        conn.close()


__all__ = ["get_scores_digest"]

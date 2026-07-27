"""Unified usage-facts read route for the observability namespace."""

from __future__ import annotations

from hermes_constants import get_hermes_home as _usage_get_hermes_home
from hermes_cli.usage_facts_db import (
    usage_facts_db_path as _usage_facts_db_path,
)
from hermes_cli.usage_facts_readmodel import (
    build_usage_facts_payload as _build_usage_facts_payload,
)

# extension_runtime.load_api_extension injects the parent API context.


@observability_routes.get("/stats/usage-facts")
def get_usage_facts_stats(
    board: Optional[str] = Query(
        None,
        description="Kanban board slug (omit for current)",
    ),
    origin: Optional[list[str]] = Query(
        None,
        description="Repeat to restrict the selected usage origins",
    ),
    captured_from: Optional[str] = Query(
        None,
        description="Inclusive ISO-8601 captured_at lower bound",
    ),
    captured_to: Optional[str] = Query(
        None,
        description="Exclusive ISO-8601 captured_at upper bound",
    ),
):
    """Aggregate unified facts without mutating either SQLite source."""

    board = _resolve_board(board)
    hermes_home = _usage_get_hermes_home()
    return _build_usage_facts_payload(
        _usage_facts_db_path(),
        kanban_path=kanban_db.kanban_db_path(board=board),
        profiles_root=hermes_home / "profiles",
        origins=origin,
        captured_from=captured_from,
        captured_to=captured_to,
    )


__all__ = ()

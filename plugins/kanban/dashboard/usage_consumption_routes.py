"""Versioned consumption-metrics route for the observability namespace.

Contract ``usage-consumption.v1`` (Canon kosten-ssot-im-lesepfad): all
costs are read-path derivations from raw facts × the current price
table; every rate carries numerator and denominator; missing basis is
``"not applicable"``, never 0; every value carries its confidence and
coverage.  The payload builder is TTL-cached in-process (the facts
layer changes only via the 15-minute harvest), so the warm latency
budget of <500 ms for 30 days holds; a daemon warmup for the default
payload runs at import so the first hit is warm.
"""

from __future__ import annotations

import threading

from hermes_cli.usage_consumption_readmodel import (
    build_consumption_payload as _build_consumption_payload,
)
from hermes_cli.usage_facts_db import (
    usage_facts_db_path as _usage_facts_db_path,
)

# extension_runtime.load_api_extension injects the parent API context.

_ALLOWED_DAYS = {7, 30, 90}
_ALLOWED_BREAKDOWNS = {
    "origin", "model", "provider", "lane", "buzz_unit",
    "billing_mode", "day", "chain_id", "task_id",
}


@observability_routes.get("/stats/usage-consumption")
def get_usage_consumption(
    board: Optional[str] = Query(
        None,
        description="Kanban board slug (omit for current)",
    ),
    days: int = Query(30, description="Window size in days: 7, 30 or 90"),
    breakdown: str = Query(
        "origin",
        description="origin, model, provider, lane, buzz_unit, "
        "billing_mode, day, chain_id or task_id",
    ),
):
    """Serve the derived consumption metrics without touching writes."""
    if days not in _ALLOWED_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"days must be one of {sorted(_ALLOWED_DAYS)}",
        )
    if breakdown not in _ALLOWED_BREAKDOWNS:
        raise HTTPException(
            status_code=400,
            detail=f"breakdown must be one of {sorted(_ALLOWED_BREAKDOWNS)}",
        )
    board = _resolve_board(board)
    return _build_consumption_payload(
        _usage_facts_db_path(),
        days=days,
        breakdown=breakdown,
        kanban_path=kanban_db.kanban_db_path(board=board),
    )


def _warm_in_background() -> None:
    """Warm exactly the default request so its first hit meets the budget."""
    try:
        _build_consumption_payload(
            _usage_facts_db_path(),
            days=30,
            breakdown="origin",
            kanban_path=kanban_db.kanban_db_path(board=_resolve_board(None)),
        )
    except Exception:
        pass


threading.Thread(target=_warm_in_background, daemon=True).start()


__all__ = ()

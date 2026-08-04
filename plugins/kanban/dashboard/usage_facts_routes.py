"""Unified usage-facts read route for the observability namespace."""

from __future__ import annotations

import threading

from hermes_constants import get_hermes_home as _usage_get_hermes_home
from hermes_cli.usage_consumption_readmodel import (
    build_consumption_payload as _build_consumption_payload,
)
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


_ALLOWED_CONSUMPTION_DAYS = {7, 30, 90}
_ALLOWED_CONSUMPTION_BREAKDOWNS = {
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
    """Serve the derived consumption metrics (usage-consumption.v1).

    Read-path derivation only (Canon kosten-ssot-im-lesepfad): every rate
    carries numerator and denominator; missing basis is "not applicable".
    The payload builder is TTL-cached in-process; a daemon warmup for the
    default payload runs at import so the first hit is within budget.
    """
    if days not in _ALLOWED_CONSUMPTION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"days must be one of {sorted(_ALLOWED_CONSUMPTION_DAYS)}",
        )
    if breakdown not in _ALLOWED_CONSUMPTION_BREAKDOWNS:
        raise HTTPException(
            status_code=400,
            detail=f"breakdown must be one of {sorted(_ALLOWED_CONSUMPTION_BREAKDOWNS)}",
        )
    board = _resolve_board(board)
    _ensure_consumption_warmup()
    return _build_consumption_payload(
        _usage_facts_db_path(),
        days=days,
        breakdown=breakdown,
        kanban_path=kanban_db.kanban_db_path(board=board),
    )


def _warm_consumption_in_background() -> None:
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


_WARMUP_LOCK = threading.Lock()
_WARMUP_STARTED = False


def _ensure_consumption_warmup() -> None:
    """Start the warmup lazily on the first request — never at import.

    An import-time thread would open the production databases during
    pytest collection and every incidental package import (Review 1,
    2026-08-04).  The daemon warms the default variant after the first
    hit; the cold-build cost is documented in usage-facts.md.
    """
    global _WARMUP_STARTED
    if _WARMUP_STARTED:
        return
    with _WARMUP_LOCK:
        if _WARMUP_STARTED:
            return
        _WARMUP_STARTED = True
    threading.Thread(target=_warm_consumption_in_background, daemon=True).start()


__all__ = ()

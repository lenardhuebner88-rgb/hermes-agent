"""Runtime, dispatch-hold, activity, and aggregate readmodel routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

@observability_routes.get("/runs/live-events")
def get_live_events(
    board: Optional[str] = Query(None),
    limit: int = Query(_LIVE_EVENTS_DEFAULT_LIMIT, ge=1),
    since_id: Optional[int] = Query(None, ge=1),
):
    """Latest cross-worker events suitable for the worker-tab ticker.

    Returns a newest-first list of task events from a curated allowlist of
    kinds (heartbeat, claimed, completed, blocked, …).  The caller can poll
    incrementally with ``since_id``; only events with ids greater than the
    supplied value are returned, preserving ``limit``.
    """
    limit = min(limit, _LIVE_EVENTS_MAX_LIMIT)
    board = _resolve_board(board)
    conn = _conn(board=board)
    kinds = _LIVE_EVENT_KINDS
    kind_placeholders = ",".join("?" for _ in kinds)

    # ``board`` is applied by opening the selected board database above.
    # The tasks table is per-board and intentionally has no ``board`` column.
    if since_id is not None:
        try:
            rows = conn.execute(
                f"""
                SELECT e.id, e.run_id, e.task_id, t.title AS task_title,
                       COALESCE(r.profile, t.assignee) AS profile,
                       e.kind, e.payload, e.created_at
                FROM task_events e
                JOIN tasks t ON t.id = e.task_id
                LEFT JOIN task_runs r ON r.id = e.run_id
                WHERE e.kind IN ({kind_placeholders})
                  AND e.id > ?
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (*kinds, since_id, limit),
            ).fetchall()
        finally:
            conn.close()
    else:
        try:
            rows = conn.execute(
                f"""
                SELECT e.id, e.run_id, e.task_id, t.title AS task_title,
                       COALESCE(r.profile, t.assignee) AS profile,
                       e.kind, e.payload, e.created_at
                FROM task_events e
                JOIN tasks t ON t.id = e.task_id
                LEFT JOIN task_runs r ON r.id = e.run_id
                WHERE e.kind IN ({kind_placeholders})
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (*kinds, limit),
            ).fetchall()
        finally:
            conn.close()

    events = []
    latest_id: Optional[int] = None
    for row in rows:
        row_id = int(row["id"])
        if latest_id is None or row_id > latest_id:
            latest_id = row_id
        try:
            payload = json.loads(row["payload"]) if row["payload"] else None
        except Exception:
            payload = None
        events.append(
            {
                "id": row_id,
                "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
                "task_id": row["task_id"],
                "task_title": row["task_title"],
                "profile": row["profile"],
                "kind": row["kind"],
                "note": payload.get("note") if isinstance(payload, dict) else None,
                "at": int(row["created_at"] or 0),
            }
        )

    return {
        "events": events,
        "count": len(events),
        "latest_id": latest_id,
        "checked_at": int(time.time()),
    }


try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]




@observability_routes.get("/dispatch/holds")
def get_dispatch_holds(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return the same read-only dispatch-hold report as the Kanban CLI."""
    board = _resolve_board(board)
    dispatch_kwargs = kanban_db.dispatch_kwargs_from_config(
        _read_root_kanban_cfg()
    )
    conn = _conn(board=board)
    try:
        return kanban_db.list_dispatch_holds(
            conn,
            board=board,
            **dispatch_kwargs,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------


_ACTIVITY_DEFAULT_LIMIT = 12
_ACTIVITY_MAX_LIMIT = 50


@evidence_routes.get("/tasks/{task_id}/activity")
def get_task_activity(
    task_id: str,
    limit: int = Query(_ACTIVITY_DEFAULT_LIMIT, ge=1, le=_ACTIVITY_MAX_LIMIT),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Recent task events for the activity timeline in the cockpit view (F1).

    Returns the most recent *limit* events (newest-first) from ``task_events``.
    ``note`` is extracted from ``payload.note`` if present, otherwise null.
    Default limit 12; hard-capped at 50.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        # Hard-cap regardless of the query-param validator (defence-in-depth).
        effective_limit = min(max(1, int(limit)), _ACTIVITY_MAX_LIMIT)
        rows = conn.execute(
            """
            SELECT id, run_id, kind, payload, created_at
              FROM task_events
             WHERE task_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (task_id, effective_limit),
        ).fetchall()
        events = []
        for row in rows:
            note: Optional[str] = None
            if row["payload"]:
                try:
                    note = json.loads(row["payload"]).get("note")
                except Exception:
                    note = None
            events.append({
                "id": row["id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "note": note,
                "at": row["created_at"],
            })
        return {"task_id": task_id, "events": events}
    finally:
        conn.close()




@observability_routes.get("/runs/{run_id}/timeline")
def run_timeline_endpoint(
    run_id: int,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F3: flat per-run timeline — run frame + time-sorted events with
    ``offset_seconds``/``delta_seconds``. Read-only; 404 when absent."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        tl = kanban_db.run_timeline(conn, run_id)
        if tl is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return tl
    finally:
        conn.close()




@observability_routes.get("/stats/autonomy")
def get_stats_autonomy(board: Optional[str] = Query(None)):
    """Operator-free acceptance rate from task events."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.autonomy_stats(conn)
    finally:
        conn.close()


@observability_routes.get("/stats/chain-completion")
def get_stats_chain_completion(board: Optional[str] = Query(None)):
    """Done roots whose dependency leaves are all done."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.chain_completion_stats(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worker log (read-only; file written by _default_spawn)
# ---------------------------------------------------------------------------


__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)

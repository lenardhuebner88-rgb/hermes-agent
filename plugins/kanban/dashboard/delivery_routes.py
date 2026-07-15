"""Web-push delivery contract routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

@delivery_routes.get("/push/vapid-public-key")
def get_push_vapid_public_key():
    vapid = _vapid_config()
    return {
        "enabled": vapid is not None,
        "public_key": vapid["public_key"] if vapid else None,
    }


@delivery_routes.post("/push/subscribe")
def subscribe_push(
    payload: PushSubscriptionBody,
    board: Optional[str] = Query(None),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        kanban_db.add_push_subscription(
            conn,
            endpoint=payload.endpoint,
            keys_p256dh=payload.keys.p256dh,
            keys_auth=payload.keys.auth,
        )
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@delivery_routes.post("/push/unsubscribe")
def unsubscribe_push(
    payload: PushUnsubscribeBody,
    board: Optional[str] = Query(None),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        removed = kanban_db.remove_push_subscription(conn, endpoint=payload.endpoint)
        return {"ok": True, "removed": removed}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Home-channel subscriptions (per-task, per-platform toggles)
# ---------------------------------------------------------------------------
#
# Home channels are a first-class gateway concept — each configured platform
# can have exactly one (chat_id, thread_id, name) it considers "home". The
# dashboard surfaces these as per-task toggles so a user can opt a specific
# task into receiving terminal notifications (completed / blocked / gave_up)
# at their telegram/discord/slack home, without touching the CLI.
#
# The wire format mirrors kanban_db.add_notify_sub — (task_id, platform,
# chat_id, thread_id) — so toggle-on creates exactly the same row the
# `/kanban create` slash command would, and the existing gateway notifier
# watcher delivers events without any additional plumbing.



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)

"""Jarvis PA push channel (S3.2).

Sends Jarvis-typed web pushes to every registered browser subscription.
Reuses the existing kanban push infrastructure (``push_subscriptions`` table,
VAPID env config, pywebpush) but owns the PA payload: deep link into the PA
thread instead of ``/control/flow``.  The service worker
(``web/public/hermes-push-sw.js``) already opens arbitrary ``data.url`` values.

Every send is best-effort: a missing VAPID config, an unavailable pywebpush,
or a failing push endpoint degrades to a result dict — never an exception
that could break the audited action it piggybacks on.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

_log = logging.getLogger(__name__)

PA_THREAD_URL = "/control/projekte?inbox=open"
PUSH_TEXT_LIMIT = 220
_log_disabled_reason: Optional[str] = None


def _vapid_config() -> Optional[dict[str, Any]]:
    private_key = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    public_key = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
    claims_sub = (os.environ.get("VAPID_CLAIMS_SUB") or "").strip()
    if not (private_key and public_key and claims_sub):
        return None
    return {
        "private_key": private_key,
        "public_key": public_key,
        "claims": {"sub": claims_sub},
    }


def _truncate(value: str, limit: int = PUSH_TEXT_LIMIT) -> str:
    clean = " ".join((value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def build_pa_payload(*, title: str, body: str, tag: str, url: str = PA_THREAD_URL) -> dict[str, Any]:
    return {
        "schema": "hermes-control-push-v1",
        "type": "pa",
        "title": _truncate(title, 80),
        "body": _truncate(body),
        "tag": tag,
        "url": url,
    }


def send_pa_push(
    *,
    title: str,
    body: str,
    tag: str,
    url: str = PA_THREAD_URL,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Send a Jarvis push to all subscriptions; never raises.

    Returns ``{enabled, sent, removed, failed}`` — ``enabled=False`` means the
    channel is not configured (missing VAPID env or pywebpush), which is a
    normal state on hosts without push credentials.
    """
    global _log_disabled_reason
    result: dict[str, Any] = {"enabled": False, "sent": 0, "removed": 0, "failed": 0}
    vapid = _vapid_config()
    if vapid is None:
        if _log_disabled_reason != "vapid":
            _log_disabled_reason = "vapid"
            _log.info("PA push disabled: VAPID env incomplete")
        return result
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        if _log_disabled_reason != "pywebpush":
            _log_disabled_reason = "pywebpush"
            _log.info("PA push disabled: pywebpush unavailable")
        return result

    from hermes_cli import kanban_db

    payload = json.dumps(
        build_pa_payload(title=title, body=body, tag=tag, url=url),
        ensure_ascii=False,
    )
    result["enabled"] = True
    with kanban_db.connect_closing(board=board) as conn:
        for sub in kanban_db.list_push_subscriptions(conn):
            endpoint = str(sub.get("endpoint") or "")
            try:
                webpush(
                    subscription_info={
                        "endpoint": endpoint,
                        "keys": {
                            "p256dh": str(sub.get("keys_p256dh") or ""),
                            "auth": str(sub.get("keys_auth") or ""),
                        },
                    },
                    data=payload,
                    vapid_private_key=vapid["private_key"],
                    vapid_claims=vapid["claims"],
                    ttl=300,
                    timeout=10,
                )
                kanban_db.record_push_success(conn, endpoint=endpoint)
                result["sent"] += 1
            except Exception as exc:
                status_code = None
                if isinstance(exc, WebPushException):
                    response = getattr(exc, "response", None)
                    status_code = getattr(response, "status_code", None)
                if status_code in {404, 410}:
                    kanban_db.remove_push_subscription(conn, endpoint=endpoint)
                    result["removed"] += 1
                else:
                    kanban_db.record_push_failure(conn, endpoint=endpoint)
                    result["failed"] += 1
                    _log.debug("PA push failed for %s: %s", endpoint[:32], exc)
    return result


def notify_pa_action_enqueued(event_id: int, question_text: str) -> None:
    """Best-effort push for a freshly enqueued gated action (S3.2 hook)."""
    try:
        send_pa_push(
            title="Jarvis: Entscheidung nötig",
            body=question_text,
            tag=f"hermes-pa-action-{int(event_id)}",
        )
    except Exception:
        _log.debug("PA action push failed for event %s", event_id, exc_info=True)

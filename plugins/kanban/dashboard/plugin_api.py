"""Kanban dashboard plugin — backend API routes.

Mounted at /api/plugins/kanban/ by the dashboard plugin system.

This layer is intentionally thin: every handler is a small wrapper around
``hermes_cli.kanban_db`` or a direct SQL query. Writes use the same code
paths the CLI and gateway ``/kanban`` command use, so the three surfaces
cannot drift.

Live updates arrive via the ``/events`` WebSocket, which tails the
append-only ``task_events`` table on a short poll interval (WAL mode lets
reads run alongside the dispatcher's IMMEDIATE write transactions).

Security note
-------------
Plugin HTTP routes go through the dashboard's session-token auth middleware
(``web_server.auth_middleware``) just like core API routes — every
``/api/plugins/...`` request must present the session bearer token (or the
session cookie set when you load the dashboard HTML). The token is the
random per-process ``_SESSION_TOKEN`` printed at startup; the dashboard's
own pages inject it via ``window.__HERMES_SESSION_TOKEN__`` so logged-in
browsers don't have to handle it manually.

For the ``/events`` WebSocket we still require the session token as a
``?token=`` query parameter (browsers cannot set the ``Authorization``
header on an upgrade request), matching the established pattern used by
the in-browser PTY bridge in ``hermes_cli/web_server.py``.

This means ``hermes dashboard --host 0.0.0.0`` is safe to run on a LAN:
plugin routes are no longer an unauthenticated exception. The auth still
isn't multi-user — anyone who can read the printed URL+token gets full
dashboard access — but they can't ride along just because they can reach
the port.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import tempfile
import time
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

from fastapi import File, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, status as http_status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hermes_cli import funnel as kanban_funnel
from hermes_cli import kanban_db
from hermes_cli import projects_db
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import strategist_surface
from plugins.kanban.dashboard.route_contracts import DashboardRouteContract

log = logging.getLogger(__name__)

route_contract = DashboardRouteContract()
router = route_contract.router
core_routes = route_contract.namespace("core")
evidence_routes = route_contract.namespace("evidence")
control_routes = route_contract.namespace("control")
lane_routes = route_contract.namespace("lanes")
observability_routes = route_contract.namespace("observability")
delivery_routes = route_contract.namespace("delivery")
planspec_routes = route_contract.namespace("planspec")
flow_release_routes = route_contract.namespace("flow_release")

_SHORT_TEXT_MAX_LENGTH = 512
_FREE_TEXT_MAX_LENGTH = 20_000
_LIST_MAX_LENGTH = 1_000
_PUSH_HOOKS_REGISTERED = False
_PUSH_DISABLED_REASONS_LOGGED: set[str] = set()
_PUSH_OPERATOR_EVENT_IDS: OrderedDict[int, None] = OrderedDict()
_PUSH_OPERATOR_EVENT_IDS_LOCK = threading.Lock()
_PUSH_OPERATOR_EVENT_IDS_MAX = 2_000

ShortText = Annotated[str, Field(max_length=_SHORT_TEXT_MAX_LENGTH)]
FreeText = Annotated[str, Field(max_length=_FREE_TEXT_MAX_LENGTH)]


# ---------------------------------------------------------------------------
# Auth helper — WebSocket only (HTTP routes live behind the dashboard's
# existing plugin-bypass; this is documented above).
# ---------------------------------------------------------------------------

def _ws_upgrade_authorized(ws: "WebSocket") -> bool:
    """Authorize a WebSocket upgrade by delegating to the dashboard's canonical
    WS auth gate (``hermes_cli.web_server._ws_auth_ok``).

    Delegating (rather than re-implementing a ``_SESSION_TOKEN``-only check)
    means this endpoint transparently accepts whatever the core gate accepts
    in each mode:

      * loopback / ``--insecure``: legacy ``?token=<_SESSION_TOKEN>``
      * gated OAuth: single-use ``?ticket=`` (the browser SDK's
        ``buildWsUrl`` mints one per connect)
      * server-internal: the process-lifetime ``?internal=`` credential

    The previous bespoke check only understood ``_SESSION_TOKEN``, so the
    kanban live-events WS was rejected on every OAuth-gated deployment even
    though the rest of the dashboard worked. Routing through the shared gate
    also means this can never drift from core auth again.

    Imported lazily so the plugin still loads in test contexts where the
    dashboard ``web_server`` module isn't importable (e.g. the bare-FastAPI
    test harness); there we accept so the tail loop stays testable, matching
    the prior behaviour.
    """
    try:
        from hermes_cli import web_server as _ws
    except Exception:
        # No dashboard context (tests). Accept so the tail loop is still
        # testable; in production the dashboard module always imports
        # cleanly because it's the caller.
        return True
    return bool(_ws._ws_auth_ok(ws))


def _ws_host_origin_is_allowed(ws: WebSocket) -> bool:
    """Apply the dashboard WebSocket DNS-rebinding guard."""
    try:
        from hermes_cli import web_server as _ws

        return bool(_ws._ws_host_origin_is_allowed(ws))
    except Exception:
        return False


def _ws_is_loopback_connection(ws: WebSocket) -> bool:
    """Return True for the dashboard's tokenless loopback WebSocket mode."""
    try:
        from hermes_cli import web_server as _ws
    except Exception:
        return False

    app_state = getattr(getattr(_ws, "app", None), "state", None)
    if bool(getattr(app_state, "auth_required", False)):
        return False

    loopback_hosts = getattr(_ws, "_LOOPBACK_HOSTS", frozenset())
    bound_host = (getattr(app_state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in loopback_hosts:
        return False

    client = getattr(ws, "client", None)
    client_host = client.host if client else ""
    return client_host in loopback_hosts


def _resolve_board(board: Optional[str]) -> Optional[str]:
    """Validate and normalise a board slug from a query param.

    Raises :class:`HTTPException` 400 on malformed slugs so the browser
    sees a clean error instead of a 500. Returns the normalised slug,
    or ``None`` when the caller omitted the param (which then falls
    through to the active board inside ``kb.connect()``).
    """
    if board is None or board == "":
        return None
    try:
        normed = kanban_db._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if normed and normed != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(normed):
        raise HTTPException(
            status_code=404,
            detail=f"board {normed!r} does not exist",
        )
    return normed


def _resolve_dashboard_busy_timeout_ms() -> int:
    """Lock-wait budget for dashboard DB connections (ms), env-overridable."""
    raw = os.environ.get("HERMES_KANBAN_DASHBOARD_BUSY_TIMEOUT_MS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return 5_000


_DASHBOARD_BUSY_TIMEOUT_MS = _resolve_dashboard_busy_timeout_ms()
_DASHBOARD_CORRUPT_OPEN_RETRY_DELAY_S = 0.2


def _discard_initialized_path(path: Path) -> None:
    """Best-effort: force a retried dashboard open through kanban's slow path."""
    try:
        resolved = str(path.resolve())
    except OSError:
        return
    with contextlib.suppress(Exception):
        kanban_db._INITIALIZED_PATHS.discard(resolved)


def _conn(
    board: Optional[str] = None,
    source_errors: Optional[list[dict[str, Any]]] = None,
):
    """Open a kanban_db connection, creating the schema on first use.

    Every handler that mutates the DB goes through this so the plugin
    self-heals on a fresh install (no user-visible "no such table"
    error if somebody hits POST /tasks before GET /board).

    Self-heal is delegated to :func:`kanban_db.connect`, whose first
    connection to a given path runs ``SCHEMA_SQL`` + the additive
    migration pass and then caches the path in ``_INITIALIZED_PATHS``
    so subsequent connects are cheap. We deliberately do **not** call
    :func:`kanban_db.init_db` here: it discards that cache to force a
    re-migration on every call, which on the read-heavy dashboard poll
    path (board 8s, workers 5s, decision-queue 15s, runs 20s) meant the
    full schema + ~36 ``PRAGMA table_info`` migration pass re-ran on
    every request. ``init_db`` stays the explicit force-migration entry
    point for the CLI / DB upgrades, not the hot read path.

    ``board`` is the query-param slug (already normalised by
    :func:`_resolve_board`). When ``None`` the active board is used
    via the resolution chain (env var → ``current`` file → ``default``).

    Dashboard connections wait at most ``_DASHBOARD_BUSY_TIMEOUT_MS`` (5s
    default) on a locked DB instead of kanban's 120s worker default: the
    SPA has its own GET timeout + retry/backoff, so a fast 5xx beats a
    request that pins a server thread for two minutes.

    The kanban DB layer still fails closed on persistent corruption.  The
    dashboard adds one short retry for the observed WAL/checkpoint edge where
    the initial open reported ``KanbanDbCorruptError`` but an immediate fresh
    integrity probe was healthy; without that retry the whole Flow board showed
    a browser-level "failed fetch" and hid the just-captured triage card.
    """
    try:
        return kanban_db.connect(board=board, busy_timeout_ms=_DASHBOARD_BUSY_TIMEOUT_MS)
    except kanban_db.KanbanDbCorruptError as exc:
        if source_errors is not None:
            source_errors.append(
                {
                    "artifact": "kanban_board_fetch",
                    "source": "kanban_db",
                    "stage": "db_open",
                    "severity": "warning",
                    "message": exc.reason,
                    "db_path": str(exc.db_path),
                    "backup_path": str(exc.backup_path) if exc.backup_path else None,
                    "retry_count": 1,
                }
            )
        _discard_initialized_path(exc.db_path)
        log.warning(
            "kanban dashboard DB open reported corruption for %s; retrying once",
            exc.db_path,
        )
        time.sleep(_DASHBOARD_CORRUPT_OPEN_RETRY_DELAY_S)
        return kanban_db.connect(board=board, busy_timeout_ms=_DASHBOARD_BUSY_TIMEOUT_MS)


# ---------------------------------------------------------------------------
# Browser Web Push
# ---------------------------------------------------------------------------

class PushSubscriptionKeysBody(BaseModel):
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class PushSubscriptionBody(BaseModel):
    endpoint: str = Field(min_length=1)
    keys: PushSubscriptionKeysBody


class PushUnsubscribeBody(BaseModel):
    endpoint: str = Field(min_length=1)


def _log_push_disabled_once(reason: str) -> None:
    if reason in _PUSH_DISABLED_REASONS_LOGGED:
        return
    _PUSH_DISABLED_REASONS_LOGGED.add(reason)
    log.info("kanban web push disabled: %s", reason)


def _vapid_config() -> Optional[dict[str, Any]]:
    private_key = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    public_key = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
    claims_sub = (os.environ.get("VAPID_CLAIMS_SUB") or "").strip()
    missing = [
        name
        for name, value in (
            ("VAPID_PRIVATE_KEY", private_key),
            ("VAPID_PUBLIC_KEY", public_key),
            ("VAPID_CLAIMS_SUB", claims_sub),
        )
        if not value
    ]
    if missing:
        _log_push_disabled_once("missing " + ", ".join(missing))
        return None
    return {
        "private_key": private_key,
        "public_key": public_key,
        "claims": {"sub": claims_sub},
    }


def _load_pywebpush():
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:
        _log_push_disabled_once(f"pywebpush unavailable: {type(exc).__name__}")
        return None, None
    return webpush, WebPushException


def _push_url(task_id: str) -> str:
    return f"/control/flow?task={quote(task_id)}"


def _truncate_push_text(value: str, limit: int = 220) -> str:
    clean = " ".join((value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _push_payload(
    *,
    title: str,
    body: str,
    task_id: str,
    tag: str,
) -> dict[str, Any]:
    return {
        "schema": "hermes-control-push-v1",
        "title": title,
        "body": _truncate_push_text(body),
        "tag": tag,
        "task_id": task_id,
        "url": _push_url(task_id),
    }


def _webpush_status_code(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(response, "status", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _send_web_push_payload(
    *,
    board: Optional[str],
    payload: dict[str, Any],
) -> dict[str, int | bool]:
    vapid = _vapid_config()
    if vapid is None:
        return {"enabled": False, "sent": 0, "removed": 0, "failed": 0}
    webpush_fn, webpush_exc = _load_pywebpush()
    if webpush_fn is None:
        return {"enabled": False, "sent": 0, "removed": 0, "failed": 0}

    conn = _conn(board=board)
    sent = 0
    removed = 0
    failed = 0
    try:
        subscriptions = kanban_db.list_push_subscriptions(conn)
        for sub in subscriptions:
            endpoint = str(sub.get("endpoint") or "")
            try:
                webpush_fn(
                    subscription_info={
                        "endpoint": endpoint,
                        "keys": {
                            "p256dh": str(sub.get("keys_p256dh") or ""),
                            "auth": str(sub.get("keys_auth") or ""),
                        },
                    },
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=vapid["private_key"],
                    vapid_claims=vapid["claims"],
                    ttl=300,
                )
                kanban_db.record_push_success(conn, endpoint=endpoint)
                sent += 1
            except Exception as exc:
                status_code = (
                    _webpush_status_code(exc)
                    if webpush_exc is not None and isinstance(exc, webpush_exc)
                    else None
                )
                if status_code in {404, 410}:
                    kanban_db.remove_push_subscription(conn, endpoint=endpoint)
                    removed += 1
                else:
                    kanban_db.record_push_failure(conn, endpoint=endpoint)
                    failed += 1
                    log.debug(
                        "kanban web push send failed for endpoint %s: %s",
                        endpoint[:32],
                        exc,
                    )
    finally:
        conn.close()
    return {"enabled": True, "sent": sent, "removed": removed, "failed": failed}


def _task_title(conn: sqlite3.Connection, task_id: str) -> str:
    task = kanban_db.get_task(conn, task_id)
    return task.title if task is not None and task.title else task_id


def _reason_needs_operator(reason: Optional[str]) -> bool:
    normalized = (reason or "").casefold()
    return "operator" in normalized or "freigabe" in normalized


def _handle_blocked_push(
    *,
    task_id: str,
    board: Optional[str] = None,
    reason: Optional[str] = None,
    **_: Any,
) -> None:
    if not _reason_needs_operator(reason):
        return
    conn = _conn(board=board)
    try:
        title = _task_title(conn, task_id)
    finally:
        conn.close()
    body = f"{title}: {reason}" if reason else title
    _send_web_push_payload(
        board=board,
        payload=_push_payload(
            title="Entscheidung nötig",
            body=body,
            task_id=task_id,
            tag=f"hermes-decision-{task_id}",
        ),
    )


def _completed_task_is_chain_root(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
) -> bool:
    if _resolve_chain_root(conn, task_id) != task_id:
        return False
    if not kanban_db.parent_ids(conn, task_id):
        return False
    if run_id is None:
        return True
    row = conn.execute(
        "SELECT outcome FROM task_runs WHERE id = ?",
        (int(run_id),),
    ).fetchone()
    return row is None or row["outcome"] == "completed"


def _handle_completed_push(
    *,
    task_id: str,
    board: Optional[str] = None,
    run_id: Optional[int] = None,
    summary: Optional[str] = None,
    **_: Any,
) -> None:
    conn = _conn(board=board)
    try:
        if not _completed_task_is_chain_root(conn, task_id, run_id):
            return
        title = _task_title(conn, task_id)
    finally:
        conn.close()
    body = summary or title
    _send_web_push_payload(
        board=board,
        payload=_push_payload(
            title="Kette fertig",
            body=body,
            task_id=task_id,
            tag=f"hermes-chain-complete-{task_id}",
        ),
    )


def register_push_lifecycle_hooks() -> None:
    """Register the dashboard Web Push sender as a kanban hook consumer."""
    global _PUSH_HOOKS_REGISTERED
    from hermes_cli.plugins import register_hook_once

    for hook_name, callback in (
        ("kanban_task_blocked", _handle_blocked_push),
        ("kanban_task_completed", _handle_completed_push),
    ):
        register_hook_once(hook_name, callback)
    _PUSH_HOOKS_REGISTERED = True


def _claim_operator_escalation_push_event(event_id: int) -> bool:
    if event_id <= 0:
        return True
    with _PUSH_OPERATOR_EVENT_IDS_LOCK:
        if event_id in _PUSH_OPERATOR_EVENT_IDS:
            return False
        _PUSH_OPERATOR_EVENT_IDS[event_id] = None
        while len(_PUSH_OPERATOR_EVENT_IDS) > _PUSH_OPERATOR_EVENT_IDS_MAX:
            _PUSH_OPERATOR_EVENT_IDS.popitem(last=False)
        return True


def _handle_operator_escalation_event_for_push(
    *,
    event_id: int,
    task_id: str,
    board: Optional[str],
    payload: Optional[dict[str, Any]],
) -> None:
    if not _claim_operator_escalation_push_event(int(event_id)):
        return
    conn = _conn(board=board)
    try:
        task_title = _task_title(conn, task_id)
    finally:
        conn.close()
    data = payload or {}
    detail = (
        str(data.get("recommended_human_action") or "").strip()
        or str(data.get("reason") or "").strip()
        or task_title
    )
    _send_web_push_payload(
        board=board,
        payload=_push_payload(
            title="Entscheidung nötig",
            body=f"{task_title}: {detail}" if detail != task_title else task_title,
            task_id=task_id,
            tag=f"hermes-operator-escalation-{task_id}",
        ),
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

# Columns shown by the dashboard, in left-to-right order. "archived" is
# available via a filter toggle rather than a visible column.
#
# Keep this in sync with kanban_db.VALID_STATUSES.  In particular,
# ``scheduled`` is a first-class waiting column used for time-based follow-ups;
# if it is omitted here, the board-level fallback below mis-buckets scheduled
# tasks into ``todo`` and makes the dashboard look like the Scheduled column
# disappeared.
BOARD_COLUMNS: list[str] = [
    "triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done",
]


_CARD_SUMMARY_PREVIEW_CHARS = 200


def _task_dict(
    task: kanban_db.Task,
    *,
    latest_summary: Optional[str] = None,
) -> dict[str, Any]:
    d = asdict(task)
    # Add derived age metrics so the UI can colour stale cards without
    # computing deltas client-side.
    try:
        d["age"] = kanban_db.task_age(task)
    except Exception:
        d["age"] = {"created_age_seconds": None, "started_age_seconds": None, "time_to_complete_seconds": None}
    # Surface the latest non-null run summary so dashboards don't show
    # blank cards/drawers for tasks where the worker handed off via
    # ``task_runs.summary`` (the kanban-worker pattern) instead of
    # ``tasks.result``. ``None`` when no run has produced a summary yet.
    d["latest_summary"] = latest_summary
    # Keep body short on list endpoints; full body comes from /tasks/:id.
    return d


def _event_dict(event: kanban_db.Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": event.created_at,
        "run_id": event.run_id,
    }


def _comment_dict(c: kanban_db.Comment) -> dict[str, Any]:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "author": c.author,
        "body": c.body,
        "created_at": c.created_at,
    }


def _attachment_dict(a: kanban_db.Attachment) -> dict[str, Any]:
    """Serialise an Attachment for the drawer. ``stored_path`` is the
    absolute on-disk path workers read; the UI uses ``id`` for download."""
    return {
        "id": a.id,
        "task_id": a.task_id,
        "filename": a.filename,
        "content_type": a.content_type,
        "size": a.size,
        "uploaded_by": a.uploaded_by,
        "stored_path": a.stored_path,
        "created_at": a.created_at,
    }


def _run_value(run: Any, key: str) -> Any:
    if isinstance(run, sqlite3.Row):
        return run[key] if key in run.keys() else None
    if isinstance(run, dict):
        return run.get(key)
    return getattr(run, key, None)


def _run_metadata_dict(run: Any) -> dict[str, Any]:
    raw_metadata = _run_value(run, "metadata")
    if isinstance(raw_metadata, dict):
        return raw_metadata
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


class _LegacyModelRouteResolver:
    """Request-scoped cache/batch for explicitly legacy-only inference."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        runs: Optional[list[Any]] = None,
        *,
        board: Optional[str] = None,
    ) -> None:
        self.conn = conn
        self.board = board or kanban_db.board_slug_for_conn(conn)
        self._identity_by_profile: dict[str, dict[str, Any]] = {}
        requests: list[tuple[str, Optional[str]]] = []
        for run in runs or []:
            metadata = _run_metadata_dict(run)
            session_id = metadata.get("worker_session_id")
            if session_id:
                requests.append((str(session_id), _run_value(run, "profile")))
        self._usage_by_session = kanban_db._backfill_usage_batch_from_state_db(requests)

    def usage(self, session_id: str, profile: Optional[str]) -> dict[str, Any]:
        key = (str(session_id), str(profile or "").strip() or None)
        if key not in self._usage_by_session:
            self._usage_by_session[key] = kanban_db._backfill_usage_from_state_db(
                key[0], profile=key[1]
            )
        return self._usage_by_session[key]

    def identity(self, profile: Optional[str]) -> dict[str, Any]:
        key = str(profile or "").strip()
        if not key:
            return {}
        if key not in self._identity_by_profile:
            lane_entry = kanban_db._active_lane_entry_for_profile_from_conn(
                self.conn, key
            )
            self._identity_by_profile[key] = kanban_db._spawn_identity_metadata(
                key,
                board=self.board,
                # Empty dict is intentional: it tells the resolver there is no
                # active lane without making it open another board connection.
                lane_entry=lane_entry or {},
            ) or {}
        return self._identity_by_profile[key]


def _run_model_route_fields(
    conn: sqlite3.Connection,
    run: Any,
    *,
    board: Optional[str] = None,
    legacy_resolver: Optional[_LegacyModelRouteResolver] = None,
) -> dict[str, Any]:
    """Return honest run-bound model fields with explicit legacy inference.

    Persisted columns always win. Old runs may be inferred from their immutable
    metadata/session record, and only then from current profile configuration;
    inferred values remain ``unknown``/``legacy_inferred`` and are never
    presented as provider-confirmed telemetry.
    """
    metadata = _run_metadata_dict(run)

    requested_provider = _run_value(run, "requested_provider") or None
    requested_model = _run_value(run, "requested_model") or None
    active_provider = _run_value(run, "active_provider") or requested_provider
    active_model = _run_value(run, "active_model") or requested_model
    model_state = _run_value(run, "model_state") or None
    model_source = _run_value(run, "model_source") or None
    observed_at = _run_value(run, "model_observed_at") or None

    inferred_provider = metadata.get("route_provider") or metadata.get("provider")
    inferred_model = metadata.get("model")
    if metadata.get("worker_runtime") == "claude-cli":
        inferred_provider = "claude-cli"

    session_id = metadata.get("worker_session_id")
    if session_id and (not inferred_provider or not inferred_model):
        resolver = legacy_resolver or _LegacyModelRouteResolver(conn, board=board)
        usage = resolver.usage(str(session_id), _run_value(run, "profile"))
        inferred_provider = inferred_provider or usage.get("billing_provider")
        inferred_model = inferred_model or usage.get("model")

    if not inferred_provider or not inferred_model:
        try:
            resolver = legacy_resolver or _LegacyModelRouteResolver(conn, board=board)
            identity = resolver.identity(_run_value(run, "profile"))
        except Exception:
            identity = {}
        inferred_provider = inferred_provider or identity.get("route_provider")
        inferred_model = inferred_model or identity.get("model")

    inferred = False
    if not active_provider and inferred_provider:
        active_provider = inferred_provider
        inferred = True
    if not active_model and inferred_model:
        active_model = inferred_model
        inferred = True
    if inferred:
        model_state = "unknown"
        model_source = "legacy_inferred"
        observed_at = observed_at or _run_value(run, "started_at")
    if not active_provider or not active_model:
        model_state = "unknown"
    elif not model_state:
        model_state = "unknown"
    if not model_source:
        model_source = "legacy_inferred" if (active_provider or active_model) else None

    return {
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "active_provider": active_provider,
        "active_model": active_model,
        "model_state": model_state,
        "model_source": model_source,
        "model_observed_at": observed_at,
        # Compatibility alias, now derived exclusively from this run.
        "effective_model": active_model or requested_model,
    }


def _run_dict(
    conn: sqlite3.Connection,
    r: kanban_db.Run,
    *,
    legacy_resolver: Optional[_LegacyModelRouteResolver] = None,
) -> dict[str, Any]:
    """Serialise a Run for the drawer's Run history section."""
    d = {
        "id": r.id,
        "task_id": r.task_id,
        "profile": r.profile,
        "step_key": r.step_key,
        "status": r.status,
        "claim_lock": r.claim_lock,
        "claim_expires": r.claim_expires,
        "worker_pid": r.worker_pid,
        "max_runtime_seconds": r.max_runtime_seconds,
        "last_heartbeat_at": r.last_heartbeat_at,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "outcome": r.outcome,
        "summary": r.summary,
        "metadata": r.metadata,
        "error": r.error,
        # K5a: per-run token/cost accounting (NULL until a run records usage).
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cost_usd": r.cost_usd,
    }
    d.update(_run_model_route_fields(conn, r, legacy_resolver=legacy_resolver))
    d.update(_run_lineage_fields(conn, r.task_id, r.id))
    return d


_RESULT_SUMMARY_LIMIT = 8 * 1024
_RESULT_METADATA_LIMIT = 16 * 1024
_RESULT_PREVIEW_LIMIT = 160
_DELIVERABLES_MAX_FILES = 50
# Bound dashboard work for pathological worker outputs: a task can preserve
# thousands of artifacts, and /runs/recent-results calls this helper for many
# rows.  Keep RESULT.md deterministic, then stop walking after a generous cap
# instead of letting one task monopolize the control API.
_DELIVERABLES_MAX_SCANNED = 5_000
_DELIVERABLE_EXCERPT_LIMIT = 600
_VAULT_MEMORY_CARD_SOURCE_CHARS = 4_000


def _vault_memory_file_url(path: str) -> str:
    return f"/api/plugins/kanban/vault-memory-links/file?path={quote(path, safe='')}"


def _with_vault_memory_file_urls(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for link in links:
        item = dict(link)
        path = item.get("path")
        resolved = (
            kanban_db.resolve_vault_memory_link_path(str(path))
            if path and item.get("exists") is True
            else None
        )
        if resolved is not None and resolved.is_file():
            item["url"] = _vault_memory_file_url(str(path))
        enriched.append(item)
    return enriched


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    if value == "":
        return []
    return [str(value)]


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _load_result_metadata(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    text = str(raw)[:_RESULT_METADATA_LIMIT]
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {"raw_metadata": text}
    return data if isinstance(data, dict) else {"metadata": data}


def _summary_preview(summary: str) -> str:
    first = next((line.strip() for line in summary.splitlines() if line.strip()), "")
    source = first or summary.strip()
    return source[:_RESULT_PREVIEW_LIMIT]


_VERDICT_TOKENS = {
    "APPROVED": "APPROVED",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "REQUEST-CHANGES": "REQUEST_CHANGES",
    "REQUEST CHANGES": "REQUEST_CHANGES",
    "NEEDS_REVISION": "REQUEST_CHANGES",
    "NEEDS-REVISION": "REQUEST_CHANGES",
    "NEEDS REVISION": "REQUEST_CHANGES",
}

_VERIFIER_EVIDENCE_KEYS = (
    "gate_output_excerpt",
    "command_output_excerpt",
    "verification_evidence",
    "evidence_audited",
    "evidence_used",
    "commands_evidence",
    "tests_run",
    "tests_passed",
)


def _normalize_verifier_verdict(summary: str, metadata: dict[str, Any]) -> Optional[str]:
    raw = metadata.get("verdict")
    if not isinstance(raw, str) or not raw.strip():
        first = next((line.strip() for line in summary.splitlines() if line.strip()), "")
        raw = first.split("—", 1)[0].split(":", 1)[0].strip() if first else ""
    token = str(raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    return _VERDICT_TOKENS.get(token)


def _verification_state(verdict: Optional[str], *, default: str) -> str:
    if verdict == "APPROVED":
        return "approved"
    if verdict == "REQUEST_CHANGES":
        return "request_changes"
    return default


def _result_quality_badge(verification_state: str, *, profile: Optional[str]) -> dict[str, str]:
    """Return a compact done-result gate-quality taxonomy for /control cards."""
    if verification_state == "approved":
        return {
            "state": "verifier_approved",
            "label": "Verifier-approved",
            "tone": "emerald",
            "description": "Independent verifier gate passed.",
        }
    if verification_state == "request_changes":
        return {
            "state": "rejected_needs_work",
            "label": "Rejected / needs work",
            "tone": "red",
            "description": "Verifier gate requested changes before this should count as done.",
        }
    if not profile:
        return {
            "state": "unknown_legacy",
            "label": "Unknown legacy",
            "tone": "zinc",
            "description": "Legacy run has no verifier metadata or profile lineage.",
        }
    return {
        "state": "ungated",
        "label": "Ungated",
        "tone": "amber",
        "description": "Completed without an independent verifier gate.",
    }


def _claimed_event_payload(conn: sqlite3.Connection, task_id: str, run_id: Any) -> Optional[dict[str, Any]]:
    try:
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
            "ORDER BY id DESC LIMIT 1",
            (task_id, int(run_id)),
        ).fetchone()
    except (TypeError, ValueError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _run_lineage_fields(conn: sqlite3.Connection, task_id: str, run_id: Any) -> dict[str, str]:
    """Return explicit human-facing lineage labels for a task_runs row.

    The durable discriminator for review/verifier attempts is the claimed
    event written by claim_review_task with source_status='review'.
    Old/synthetic rows may lack any claimed event; surface them as legacy
    unknown instead of inferring coder from task.assignee/profile fallbacks.
    """
    payload = _claimed_event_payload(conn, task_id, run_id)
    if payload is None:
        return {
            "run_role": "legacy_unknown",
            "run_role_label": "Unknown / legacy run",
            "run_role_source": "missing_claim_event",
        }
    if str(payload.get("source_status") or "").strip().lower() == "review":
        return {
            "run_role": "verification",
            "run_role_label": "Verifier / review run",
            "run_role_source": "claimed_event",
        }
    return {
        "run_role": "implementation",
        "run_role_label": "Implementation / coder run",
        "run_role_source": "claimed_event",
    }


def _verifier_evidence(metadata: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for key in _VERIFIER_EVIDENCE_KEYS:
        _append_unique(evidence, _coerce_str_list(metadata.get(key)))
    return [item[:500] for item in evidence[:6]]


def _safe_deliverables_root(task_id: str) -> tuple[Path, Path]:
    """Return the task deliverables dir and resolved dir, or 404 on escape.

    Deliverables are only served from ``<kanban_home>/reports/by-task/<task_id>``.
    We intentionally do not trust path segments from the URL: malformed task IDs
    such as ``../x`` resolve outside ``by-task`` and are rejected before any file
    enumeration or download attempt.
    """
    reports_root = kanban_db.kanban_home() / "reports" / "by-task"
    root = reports_root / task_id
    reports_resolved = reports_root.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        inside = root_resolved.is_relative_to(reports_resolved)
    except ValueError:
        inside = False
    if not inside:
        raise HTTPException(status_code=404, detail="deliverable not found")
    return root, root_resolved


def _deliverable_content_type(path: Path) -> str:
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _deliverable_url(task_id: str, relative_path: str) -> str:
    task_part = quote(task_id, safe="")
    rel_part = quote(relative_path, safe="/-._~")
    return f"/api/plugins/kanban/tasks/{task_part}/deliverables/{rel_part}"


def _deliverable_dict(path: Path, root: Path, root_resolved: Path, task_id: str) -> Optional[dict[str, Any]]:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root_resolved) or not path.is_file():
            return None
        rel = path.relative_to(root).as_posix()
        st = path.stat()
    except (OSError, ValueError):
        return None
    return {
        "filename": path.name,
        "relative_path": rel,
        "size": int(st.st_size),
        "mtime": int(st.st_mtime),
        "content_type": _deliverable_content_type(path),
        "url": _deliverable_url(task_id, rel),
    }


def _list_task_deliverables(task_id: str) -> list[dict[str, Any]]:
    root, root_resolved = _safe_deliverables_root(task_id)
    if not root.is_dir():
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(candidate: Path) -> bool:
        item = _deliverable_dict(candidate, root, root_resolved, task_id)
        if item is None:
            return False
        rel = str(item["relative_path"])
        if rel in seen:
            return False
        seen.add(rel)
        items.append(item)
        return True

    # Preserve the conventional primary handoff even when a worker dumps a very
    # large artifact tree before/around it.
    add_candidate(root / "RESULT.md")

    scanned = 0
    try:
        candidates = root.rglob("*")
    except OSError:
        candidates = iter(())
    for candidate in candidates:
        scanned += 1
        add_candidate(candidate)
        if scanned >= _DELIVERABLES_MAX_SCANNED or len(items) >= _DELIVERABLES_MAX_FILES:
            break

    items.sort(key=lambda item: (0 if item["relative_path"] == "RESULT.md" else 1, item["relative_path"].lower()))
    return items[:_DELIVERABLES_MAX_FILES]


def _artifact_link_from_deliverable(
    deliverable: dict[str, Any],
    *,
    path: str,
    source: str,
) -> dict[str, Any]:
    item = dict(deliverable)
    item["path"] = path
    item["source"] = source
    return item


def _artifact_links_from_metadata(
    task_id: str,
    artifact_paths: list[str],
    deliverables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map declared run artifact paths onto safe deliverable URLs when possible.

    Workers record absolute paths in ``task_runs.metadata.artifacts``. For
    scratch tasks those paths often point at the now-deleted workspace, while the
    preserved file is served from ``reports/by-task/<task_id>/<basename>``. Keep
    the original path for provenance, but only emit a link when it resolves to
    an already-enumerated deliverable under the safe reports root.
    """
    by_rel = {str(item.get("relative_path") or ""): item for item in deliverables}
    by_name: dict[str, dict[str, Any]] = {}
    for item in deliverables:
        name = str(item.get("filename") or "")
        if name and name not in by_name:
            by_name[name] = item

    try:
        _root, root_resolved = _safe_deliverables_root(task_id)
    except HTTPException:
        root_resolved = None

    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in artifact_paths:
        raw_path = str(raw or "").strip()
        if not raw_path:
            continue
        deliverable = None
        p = Path(raw_path).expanduser()
        if p.is_absolute() and root_resolved is not None:
            try:
                rel = p.resolve(strict=False).relative_to(root_resolved).as_posix()
                deliverable = by_rel.get(rel)
            except (OSError, ValueError):
                deliverable = None
        if deliverable is None:
            deliverable = by_name.get(p.name)
        if deliverable is None:
            continue
        rel = str(deliverable.get("relative_path") or "")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        links.append(_artifact_link_from_deliverable(deliverable, path=raw_path, source="metadata.artifacts"))
    return links


def _artifact_links_from_preserved_events(
    conn: sqlite3.Connection,
    task_id: str,
    deliverables: list[dict[str, Any]],
    *,
    seen: set[str],
) -> list[dict[str, Any]]:
    by_rel = {str(item.get("relative_path") or ""): item for item in deliverables}
    links: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'deliverables_preserved' "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        base_dir = str(payload.get("dir") or "").strip()
        for filename in _coerce_str_list(payload.get("files")):
            rel = PurePosixPath(filename).as_posix()
            if rel in seen:
                continue
            deliverable = by_rel.get(rel)
            if deliverable is None:
                continue
            seen.add(rel)
            path = str(Path(base_dir) / rel) if base_dir else rel
            links.append(_artifact_link_from_deliverable(deliverable, path=path, source="deliverables_preserved"))
    return links


def _artifact_links_for_result(
    conn: sqlite3.Connection,
    task_id: str,
    artifact_paths: list[str],
    deliverables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    links = _artifact_links_from_metadata(task_id, artifact_paths, deliverables)
    seen = {str(item.get("relative_path") or "") for item in links}
    links.extend(_artifact_links_from_preserved_events(conn, task_id, deliverables, seen=seen))
    return links


def _deliverable_excerpt(task_id: str, deliverable: Optional[dict[str, Any]]) -> Optional[str]:
    if not deliverable:
        return None
    content_type = str(deliverable.get("content_type") or "")
    relative_path = str(deliverable.get("relative_path") or "")
    textish = (
        content_type.startswith("text/")
        or relative_path.endswith((".md", ".markdown", ".txt", ".json", ".yaml", ".yml"))
    )
    if not textish:
        return None
    try:
        path = _resolve_deliverable_file(task_id, relative_path)
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, HTTPException):
        return None
    excerpt = " ".join(raw.split())
    if not excerpt:
        return None
    if len(excerpt) > _DELIVERABLE_EXCERPT_LIMIT:
        return excerpt[: _DELIVERABLE_EXCERPT_LIMIT - 1].rstrip() + "…"
    return excerpt


def _resolve_deliverable_file(task_id: str, relative_path: str) -> Path:
    requested = PurePosixPath(relative_path)
    if requested.is_absolute() or not requested.parts or any(part in {"", ".", ".."} for part in requested.parts):
        raise HTTPException(status_code=404, detail="deliverable not found")
    root, root_resolved = _safe_deliverables_root(task_id)
    candidate = root.joinpath(*requested.parts)
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root_resolved) or not candidate.is_file():
            raise HTTPException(status_code=404, detail="deliverable not found")
    except OSError:
        raise HTTPException(status_code=404, detail="deliverable not found")
    return candidate


@evidence_routes.get("/vault-memory-links/file")
def open_vault_memory_link_file(path: str = Query(..., min_length=1)):
    """Serve a normalized Vault/Memory link through the dashboard auth boundary."""
    resolved = kanban_db.resolve_vault_memory_link_path(path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="link target is outside allowed vault/memory roots")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="link target not found")
    media_type, _encoding = mimetypes.guess_type(str(resolved))
    return FileResponse(
        resolved,
        media_type=media_type or "text/plain",
        filename=resolved.name,
        content_disposition_type="inline",
    )


def _recent_result_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    summary = (row["summary"] or "")[:_RESULT_SUMMARY_LIMIT]
    metadata = _load_result_metadata(row["metadata"])
    followups: list[str] = []
    artifacts: list[str] = []
    verification: list[str] = []
    for key in ("required_verification", "next_actions", "suggested_fixes", "residual_risk"):
        _append_unique(followups, _coerce_str_list(metadata.get(key)))
    for key in ("artifacts", "artifact", "receipt_path"):
        _append_unique(artifacts, _coerce_str_list(metadata.get(key)))
    for key in ("verification_evidence", "tests_run", "tests_passed", "changed_files"):
        _append_unique(verification, _coerce_str_list(metadata.get(key)))
    verdict = _normalize_verifier_verdict(summary, metadata)
    verification_state = _verification_state(verdict, default="ungated")
    ended_at = int(row["ended_at"] or 0)
    started_at = int(row["started_at"] or 0)
    deliverables = _list_task_deliverables(row["task_id"])
    d = {
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "task_title": row["task_title"],
        "task_status": row["task_status"],
        "task_assignee": row["task_assignee"],
        "profile": row["profile"],
        "status": row["run_status"],
        "outcome": row["run_outcome"],
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": max(0, ended_at - started_at) if ended_at and started_at else 0,
        "summary": summary,
        "summary_preview": _summary_preview(summary),
        "followups": followups,
        "artifacts": artifacts,
        "artifact_links": _artifact_links_for_result(conn, row["task_id"], artifacts, deliverables),
        "verification": verification,
        "verification_state": verification_state,
        "verifier_verdict": verdict,
        "verifier_evidence": _verifier_evidence(metadata) if verdict else [],
        "result_quality": _result_quality_badge(verification_state, profile=row["profile"]),
        "deliverables": deliverables,
        "residual_risk": metadata.get("residual_risk") if isinstance(metadata.get("residual_risk"), str) else None,
    }
    d.update(_run_lineage_fields(conn, row["task_id"], row["run_id"]))
    return d


def _local_day_start(ts: Optional[int] = None) -> int:
    now = int(time.time()) if ts is None else int(ts)
    local = time.localtime(now)
    return int(time.mktime(local[:3] + (0, 0, 0) + local[6:]))


def _verdict_label(verification_state: str, verdict: Optional[str]) -> str:
    if verification_state == "approved" and verdict:
        return f"Verified: {verdict}"
    if verification_state == "request_changes" and verdict:
        return f"Verifier requested changes: {verdict}"
    if verification_state == "pending":
        return "Verification pending"
    return "Not independently verified"


def _today_digest_item(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = _recent_result_dict(conn, row)
    deliverables = result.get("deliverables") if isinstance(result.get("deliverables"), list) else []
    primary_deliverable = deliverables[0] if deliverables else None
    verification_state = str(result.get("verification_state") or "ungated")
    verifier_verdict = result.get("verifier_verdict") if isinstance(result.get("verifier_verdict"), str) else None
    return {
        "run_id": result["run_id"],
        "task_id": result["task_id"],
        "task_title": result["task_title"],
        "task_summary": result.get("summary_preview") or result.get("summary") or "",
        "ended_at": result["ended_at"],
        "profile": result["profile"],
        "run_role": result["run_role"],
        "run_role_label": result["run_role_label"],
        "verification_state": verification_state,
        "verifier_verdict": verifier_verdict,
        "verdict_label": _verdict_label(verification_state, verifier_verdict),
        "result_quality": result.get("result_quality"),
        "gate_evidence": result.get("verifier_evidence") or result.get("verification") or [],
        "deliverable": primary_deliverable,
        "deliverable_excerpt": _deliverable_excerpt(result["task_id"], primary_deliverable),
        "residual_risk": result.get("residual_risk"),
    }


def _review_signal_run(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """Return the active verifier run, else the latest verifier signal run."""
    active = conn.execute(
        """
        SELECT
            r.id AS run_id,
            r.profile,
            r.status AS run_status,
            r.outcome AS run_outcome,
            r.started_at,
            r.ended_at,
            r.summary,
            r.metadata,
            'claimed_event' AS review_run_source
        FROM task_runs r
        JOIN task_events e ON e.run_id = r.id
        WHERE r.task_id = ?
          AND r.ended_at IS NULL
          AND e.kind = 'claimed'
          AND json_extract(e.payload, '$.source_status') = 'review'
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if active is not None:
        return active
    return conn.execute(
        """
        SELECT
            r.id AS run_id,
            r.profile,
            r.status AS run_status,
            r.outcome AS run_outcome,
            r.started_at,
            r.ended_at,
            r.summary,
            r.metadata,
            CASE
              WHEN EXISTS (
                SELECT 1 FROM task_events e
                WHERE e.run_id = r.id
                  AND e.kind = 'claimed'
                  AND json_extract(e.payload, '$.source_status') = 'review'
              ) THEN 'claimed_event'
              ELSE 'latest_ended_run'
            END AS review_run_source
        FROM task_runs r
        WHERE r.task_id = ?
          AND r.ended_at IS NOT NULL
        ORDER BY r.ended_at DESC, r.id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()


def _review_run_state(run_row: Optional[sqlite3.Row], verdict: Optional[str]) -> str:
    if run_row is None:
        return "pending"
    if run_row["ended_at"] is None:
        return "active"
    if verdict == "APPROVED":
        return "approved"
    if verdict == "REQUEST_CHANGES":
        return "request_changes"
    return "pending"


def _review_verdict_dict(task_row: sqlite3.Row, run_row: Optional[sqlite3.Row]) -> dict[str, Any]:
    summary = (run_row["summary"] or "")[:_RESULT_SUMMARY_LIMIT] if run_row else ""
    metadata = _load_result_metadata(run_row["metadata"] if run_row else None)
    verdict = _normalize_verifier_verdict(summary, metadata)
    active_verifier = bool(run_row is not None and run_row["ended_at"] is None)
    submitted_at = None
    if run_row is not None:
        submitted_at = int((run_row["started_at"] if active_verifier else run_row["ended_at"]) or 0)
    return {
        "task_id": task_row["id"],
        "task_title": task_row["title"],
        "task_status": task_row["status"],
        "task_assignee": task_row["assignee"],
        "created_at": int(task_row["created_at"] or 0),
        "submitted_at": submitted_at,
        "run_id": run_row["run_id"] if run_row else None,
        "reviewer_profile": (run_row["profile"] if run_row else None),
        "summary_preview": _summary_preview(summary) if summary else "",
        "verification_state": _verification_state(verdict, default="pending"),
        "verifier_verdict": verdict,
        "verifier_evidence": _verifier_evidence(metadata) if verdict else [],
        "active_verifier": active_verifier,
        "active_run_id": run_row["run_id"] if active_verifier else None,
        "review_run_state": _review_run_state(run_row, verdict),
        "review_run_source": (run_row["review_run_source"] if run_row else None),
    }


def _blocked_completion_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Serialise a hallucination-warning ``task_events`` row (joined with
    its task) for the dashboard's blocked-completions panel.

    The event payload is parsed defensively: a malformed/absent JSON blob
    must not 500 the endpoint. ``phantom`` unifies the two event shapes —
    ``phantom_cards`` (blocked completions) and ``phantom_refs`` (the
    advisory prose scan) — into a single chip list for the UI.
    """
    raw = row["payload"]
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    phantom = payload.get("phantom_cards") or payload.get("phantom_refs") or []
    summary_preview = payload.get("summary_preview")
    return {
        "event_id": row["event_id"],
        "task_id": row["task_id"],
        "task_title": row["task_title"],
        "task_status": row["task_status"],
        "assignee": row["assignee"],
        "kind": row["kind"],
        "created_at": int(row["created_at"] or 0),
        "summary_preview": summary_preview if isinstance(summary_preview, str) else None,
        "phantom": _coerce_str_list(phantom),
    }


# Hallucination-warning event kinds — see complete_task() in kanban_db.py.
# completion_blocked_hallucination: kernel rejected created_cards with
#   phantom ids; task stays in prior state.
# suspected_hallucinated_references: prose scan found t_<hex> in summary
#   that doesn't resolve; completion succeeded, advisory only.
_WARNING_EVENT_KINDS = (
    "completion_blocked_hallucination",
    "suspected_hallucinated_references",
)

# Kinds surfaced by GET /runs/live-events.  Intentionally excludes noise such as
# archived/created/promoted/task_ping_sent/spawned/mother_receipt_sent so the
# cross-worker ticker stays actionable.
_LIVE_EVENT_KINDS = (
    "heartbeat",
    "claimed",
    "submitted_for_review",
    "review_released",
    "completed",
    "blocked",
    "unblocked",
    "integration_merged",
    "timed_out",
    "crashed",
    "gave_up",
    "auto_retried",
)

_LIVE_EVENTS_DEFAULT_LIMIT = 40
_LIVE_EVENTS_MAX_LIMIT = 200

_VERIFIER_REJECTION_KIND = "verifier_request_changes"
_FIX_SUMMARY_KEYS = (
    "fix_summary",
    "actionable_fix_summary",
    "what_to_fix",
    "required_fix",
    "next_fix",
)
_FIX_LIST_KEYS = (
    "blocking_findings",
    "suggested_fixes",
    "required_verification",
)


def _fix_summary(metadata: dict[str, Any], summary: str) -> Optional[str]:
    """Return a short operator-facing fix target for rejected verifier runs."""
    for key in _FIX_SUMMARY_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_RESULT_PREVIEW_LIMIT]
    for key in _FIX_LIST_KEYS:
        items = _coerce_str_list(metadata.get(key))
        if items:
            return "; ".join(items)[:_RESULT_PREVIEW_LIMIT]
    text = " ".join(line.strip() for line in str(summary or "").splitlines() if line.strip())
    if not text:
        return None
    # Common verifier prose: "... Fix X" / "... fix it to ...".
    match = re.search(r"\b(fix(?:e[sn]?|ing)?\b.*)$", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()[:_RESULT_PREVIEW_LIMIT]
    return None


def _is_verifier_rejection_run(conn: sqlite3.Connection, row: sqlite3.Row, verdict: Optional[str]) -> bool:
    if verdict != "REQUEST_CHANGES":
        return False
    if str(row["profile"] or "").strip() == "verifier":
        return True
    lineage = _run_lineage_fields(conn, row["task_id"], row["run_id"])
    return lineage.get("run_role") == "verification"


def _verifier_rejection_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    summary = (row["summary"] or "")[:_RESULT_SUMMARY_LIMIT]
    metadata = _load_result_metadata(row["metadata"])
    evidence = _verifier_evidence(metadata)
    if not evidence and summary:
        evidence = [_summary_preview(summary)]
    run_id = int(row["run_id"] or 0)
    return {
        "event_id": -run_id,
        "run_id": run_id,
        "task_id": row["task_id"],
        "task_title": row["task_title"],
        "task_status": row["task_status"],
        "assignee": row["assignee"],
        "kind": _VERIFIER_REJECTION_KIND,
        "created_at": int(row["ended_at"] or row["started_at"] or 0),
        "summary_preview": _summary_preview(summary) if summary else None,
        "phantom": [],
        "reviewer_profile": row["profile"],
        "verifier_verdict": "REQUEST_CHANGES",
        "failure_output": evidence,
        "fix_summary": _fix_summary(metadata, summary),
    }


def _compute_task_diagnostics(
    conn: sqlite3.Connection,
    task_ids: Optional[list[str]] = None,
) -> dict[str, list[dict]]:
    """Run the diagnostic rule engine against every task (or a subset)
    and return ``{task_id: [diagnostic_dict, ...]}``.

    Tasks with no active diagnostics are omitted from the result.
    Uses ``hermes_cli.kanban_diagnostics`` — see that module for the
    rule definitions.
    """
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    # Build the candidate task list. We need each task's row + its
    # events + its runs. Doing N separate queries works but scales
    # poorly; do three aggregate queries instead.
    if task_ids is not None:
        if not task_ids:
            return {}
        placeholders = ",".join(["?"] * len(task_ids))
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders})",
            tuple(task_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived'",
        ).fetchall()

    if not rows:
        return {}

    # Index events + runs by task id. For very large boards this will
    # slurp a lot — acceptable on the dashboard's typical working set
    # (hundreds of tasks), but we can add pagination / filtering later
    # if profiling shows it's a hotspot.
    row_ids = [r["id"] for r in rows]
    placeholders = ",".join(["?"] * len(row_ids))
    events_by_task: dict[str, list] = {tid: [] for tid in row_ids}
    for ev_row in conn.execute(
        f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
        tuple(row_ids),
    ).fetchall():
        events_by_task.setdefault(ev_row["task_id"], []).append(ev_row)
    runs_by_task: dict[str, list] = {tid: [] for tid in row_ids}
    for run_row in conn.execute(
        f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
        tuple(row_ids),
    ).fetchall():
        runs_by_task.setdefault(run_row["task_id"], []).append(run_row)

    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r,
            events_by_task.get(tid, []),
            runs_by_task.get(tid, []),
            config=diag_config,
        )
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def _warnings_summary_from_diagnostics(
    diagnostics: list[dict],
) -> Optional[dict]:
    """Compact summary for cards: {count, highest_severity, kinds,
    latest_at}. Replaces the old hallucination-only ``warnings`` object
    — same shape additions plus ``highest_severity`` so the UI can color
    badges per diagnostic severity.

    Returns None when ``diagnostics`` is empty.
    """
    if not diagnostics:
        return None
    from hermes_cli.kanban_diagnostics import SEVERITY_ORDER

    kinds: dict[str, int] = {}
    latest = 0
    highest_idx = -1
    highest_sev: Optional[str] = None
    count = 0
    for d in diagnostics:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + d.get("count", 1)
        count += d.get("count", 1)
        la = d.get("last_seen_at") or 0
        if la > latest:
            latest = la
        sev = d.get("severity")
        if sev in SEVERITY_ORDER:
            idx = SEVERITY_ORDER.index(sev)
            if idx > highest_idx:
                highest_idx = idx
                highest_sev = sev
    return {
        "count": count,
        "kinds": kinds,
        "latest_at": latest,
        "highest_severity": highest_sev,
    }


def _links_for(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Return link ids plus current endpoint state for honest UI guards."""
    parent_rows = conn.execute(
        "SELECT l.parent_id AS id, t.title, t.status FROM task_links l "
        "LEFT JOIN tasks t ON t.id = l.parent_id "
        "WHERE l.child_id = ? ORDER BY l.parent_id",
        (task_id,),
    ).fetchall()
    child_rows = conn.execute(
        "SELECT l.child_id AS id, t.title, t.status FROM task_links l "
        "LEFT JOIN tasks t ON t.id = l.child_id "
        "WHERE l.parent_id = ? ORDER BY l.child_id",
        (task_id,),
    ).fetchall()

    def states(rows: list[sqlite3.Row]) -> list[dict[str, str]]:
        return [
            {"id": row["id"], "title": row["title"], "status": row["status"]}
            for row in rows
            if row["title"] is not None and row["status"] is not None
        ]

    return {
        "parents": [row["id"] for row in parent_rows],
        "children": [row["id"] for row in child_rows],
        "parent_states": states(parent_rows),
        "child_states": states(child_rows),
    }


# ---------------------------------------------------------------------------
# GET /board — server-side payload cache
# ---------------------------------------------------------------------------
# /board is the hottest dashboard endpoint (every SPA client polls it on an
# 8 s interval) and each request used to rebuild the full payload — diagnostics
# for ALL tasks, serialisation, sha256 ETag — even when nothing changed. The
# browser-side 304 only saves transfer, not that CPU. So the handler keeps the
# last rendered payload+ETag per parameter combination and revalidates it with
# a cheap DB change stamp instead of recomputing.
#
# The stamp is a tuple of aggregates over every table the payload reads.
# Most board-visible mutations append a ``task_events`` row, but not all of
# them do (in-place run/heartbeat updates, direct SQL), and some diagnostics
# rules are TIME-driven (stuck_in_blocked & friends flip without any DB
# write), so a short max-TTL backstops the stamp: a stale entry is never
# served longer than ``_BOARD_CACHE_TTL_S`` even when the stamp still matches.

_BOARD_CACHE_MAX_ENTRIES = 8


def _resolve_board_cache_ttl_s() -> float:
    """Max age for a cached board payload (s), env-overridable; 0 disables."""
    raw = os.environ.get("HERMES_KANBAN_BOARD_CACHE_TTL_S", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 30.0


_BOARD_CACHE_TTL_S = _resolve_board_cache_ttl_s()
_board_cache: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
_board_cache_lock = threading.Lock()


def _board_db_version(conn: sqlite3.Connection) -> tuple:
    """Cheap change stamp over every table the /board payload reads.

    MAX over autoincrement PKs is an O(1) index lookup; the COUNTs run over
    tables with at most a few hundred rows. Together well under 1 ms — vs
    ~100 ms for a full payload rebuild.
    """
    row = conn.execute(
        "SELECT"
        " (SELECT COALESCE(MAX(id), 0) FROM task_events),"
        " (SELECT COUNT(*) FROM tasks),"
        " (SELECT COALESCE(MAX(id), 0) FROM task_runs),"
        " (SELECT COUNT(*) FROM task_links),"
        " (SELECT COALESCE(MAX(id), 0) FROM task_comments)"
    ).fetchone()
    return tuple(row)


def _board_json_response(body_prefix: bytes, etag: str) -> Response:
    """Assemble the final /board response from a pre-serialised payload.

    ``body_prefix`` is the payload JSON minus its closing brace; the volatile
    ``now`` field is appended per-request so cache hits skip re-serialising
    the ~400 KB document but clients still get a fresh server clock.
    """
    body = body_prefix + (',"now":%d}' % int(time.time())).encode()
    return Response(
        content=body,
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


def _board_not_modified(etag: str) -> Response:
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


def _archive_cursor(archived_at: int, task_id: str) -> str:
    return f"{int(archived_at)}:{task_id}"


def _parse_archive_cursor(cursor: Optional[str]) -> Optional[tuple[int, str]]:
    if not cursor:
        return None
    raw_time, separator, task_id = cursor.partition(":")
    if not separator or not raw_time.isdigit() or not re.fullmatch(r"t_[A-Za-z0-9]+", task_id):
        raise HTTPException(status_code=400, detail="invalid archive cursor")
    return int(raw_time), task_id


def _literal_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# GET /board/archive — on-demand archive truth, separate from the hot poll
# ---------------------------------------------------------------------------

@evidence_routes.get("/board/archive")
def get_board_archive(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
    q: Optional[str] = Query(None, max_length=200, description="Literal title/id/assignee search"),
    assignee: Optional[str] = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, max_length=200),
):
    """Return one deterministic archive page without bloating ``GET /board``.

    Keyset cursor: latest durable ``archived`` event, then task id as a stable
    tie-breaker. Rows already archived when the walk starts are neither skipped
    nor duplicated. A task archived *during* the walk lands above the cursor and
    is therefore not seen by that walk — the standard keyset trade-off; it shows
    up on the next refresh. ``total_count`` is read per page, so a mid-walk
    archive can briefly make the loaded count trail the total.
    """
    board = _resolve_board(board)
    parsed_cursor = _parse_archive_cursor(cursor)
    query = (q or "").strip()
    exact_assignee = (assignee or "").strip()
    conn = _conn(board=board)
    try:
        filters = ["t.status = 'archived'"]
        filter_params: list[Any] = []
        if query:
            pattern = f"%{_literal_like(query.lower())}%"
            filters.append(
                "(LOWER(t.title) LIKE ? ESCAPE '\\' "
                "OR LOWER(t.id) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(t.assignee, '')) LIKE ? ESCAPE '\\')"
            )
            filter_params.extend([pattern, pattern, pattern])
        if exact_assignee:
            filters.append("t.assignee = ?")
            filter_params.append(exact_assignee)
        where = " AND ".join(filters)

        total_count = int(
            conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'archived'").fetchone()[0]
        )
        filtered_count = int(
            conn.execute(f"SELECT COUNT(*) FROM tasks t WHERE {where}", filter_params).fetchone()[0]
        )

        page_where = where
        page_params = list(filter_params)
        if parsed_cursor is not None:
            cursor_time, cursor_id = parsed_cursor
            page_where += " AND (archived_at < ? OR (archived_at = ? AND id < ?))"
            page_params.extend([cursor_time, cursor_time, cursor_id])
        page_params.append(limit + 1)
        rows = conn.execute(
            f"""
            WITH archive_rows AS (
                SELECT t.*,
                       COALESCE(
                           (SELECT MAX(e.created_at) FROM task_events e
                            WHERE e.task_id = t.id AND e.kind = 'archived'),
                           t.completed_at,
                           -- Not every archive path stamps an 'archived' event: a
                           -- freigabe root vetoed/completed via its hold, and a task
                           -- merged away on the Flow tab, go straight to archived
                           -- (emitting only freigabe_vetoed/-completed, or nothing at
                           -- all on the merged-away row) and never get completed_at.
                           -- Their newest event is the closest honest proxy for when
                           -- they left the board; created_at would date them by their
                           -- birth, which is a visible lie in the Archive view.
                           (SELECT MAX(e.created_at) FROM task_events e
                            WHERE e.task_id = t.id),
                           t.created_at,
                           0
                       ) AS archived_at
                FROM tasks t
            )
            SELECT * FROM archive_rows t
            WHERE {page_where}
            ORDER BY archived_at DESC, id DESC
            LIMIT ?
            """,
            page_params,
        ).fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        tasks = [kanban_db.Task.from_row(row) for row in page_rows]
        task_ids = [task.id for task in tasks]

        link_counts: dict[str, dict[str, int]] = {}
        dependents: dict[str, list[str]] = {}
        all_links = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
        task_id_set = set(task_ids)
        for link in all_links:
            parent_id = link["parent_id"]
            child_id = link["child_id"]
            dependents.setdefault(parent_id, []).append(child_id)
            if parent_id in task_id_set:
                link_counts.setdefault(parent_id, {"parents": 0, "children": 0})["children"] += 1
            if child_id in task_id_set:
                link_counts.setdefault(child_id, {"parents": 0, "children": 0})["parents"] += 1

        root_cache: dict[str, str] = {}

        def resolve_root(task_id: str) -> str:
            visited: list[str] = []
            current = task_id
            while current not in root_cache:
                if current in visited:
                    break
                visited.append(current)
                following = dependents.get(current)
                if not following:
                    break
                current = min(following)
            root = root_cache.get(current, current)
            for visited_id in visited:
                root_cache[visited_id] = root
            return root

        comment_counts: dict[str, int] = {}
        progress: dict[str, dict[str, int]] = {}
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            comment_counts = {
                row["task_id"]: int(row["n"])
                for row in conn.execute(
                    f"SELECT task_id, COUNT(*) AS n FROM task_comments "
                    f"WHERE task_id IN ({placeholders}) GROUP BY task_id",
                    task_ids,
                )
            }
            for row in conn.execute(
                f"SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l "
                f"JOIN tasks t ON t.id = l.child_id WHERE l.parent_id IN ({placeholders})",
                task_ids,
            ).fetchall():
                item = progress.setdefault(row["pid"], {"done": 0, "total": 0})
                item["total"] += 1
                if row["cstatus"] in {"done", "archived"}:
                    item["done"] += 1

        summary_map = kanban_db.latest_summaries(conn, task_ids)
        cost_map = kanban_db.batch_task_costs(conn, task_ids)
        archived_at_by_id = {row["id"]: int(row["archived_at"] or 0) for row in page_rows}
        cards: list[dict[str, Any]] = []
        for task in tasks:
            summary = summary_map.get(task.id)
            card = _task_dict(
                task,
                latest_summary=summary[:_CARD_SUMMARY_PREVIEW_CHARS] if summary else None,
            )
            card.pop("body", None)
            card.pop("result", None)
            card["archived_at"] = archived_at_by_id[task.id]
            card["block_reason"] = None
            card["link_counts"] = link_counts.get(task.id, {"parents": 0, "children": 0})
            card["comment_count"] = comment_counts.get(task.id, 0)
            card["progress"] = progress.get(task.id)
            card["root_id"] = resolve_root(task.id)
            cost = cost_map.get(task.id)
            if cost is not None:
                for field in (
                    "cost_usd",
                    "input_tokens",
                    "output_tokens",
                    "cost_usd_equivalent",
                    "cost_effective_usd",
                ):
                    card[field] = cost[field]
            cards.append(card)

        next_cursor = None
        if has_more and cards:
            last = cards[-1]
            next_cursor = _archive_cursor(last["archived_at"], last["id"])
        archived_assignees = [
            row["assignee"]
            for row in conn.execute(
                "SELECT DISTINCT assignee FROM tasks "
                "WHERE status = 'archived' AND assignee IS NOT NULL ORDER BY assignee"
            )
        ]
        latest_event_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        )
        return {
            "tasks": cards,
            "total_count": total_count,
            "filtered_count": filtered_count,
            "loaded_count": len(cards),
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "query": query,
            "assignee": exact_assignee or None,
            "assignees": archived_assignees,
            "latest_event_id": latest_event_id,
            "now": int(time.time()),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /board
# ---------------------------------------------------------------------------

@core_routes.get("/board")
def get_board(
    request: Request,
    response: Response,
    tenant: Optional[str] = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
    workflow_template_id: Optional[str] = Query(
        None, description="Restrict to tasks using this workflow template id",
    ),
    current_step_key: Optional[str] = Query(
        None, description="Restrict to tasks at this workflow step key",
    ),
    card_diagnostics: str = Query(
        "full",
        description=(
            "Per-card diagnostics payload: 'full' (default) embeds the whole "
            "structured diagnostics list on each card; 'summary' omits it and "
            "keeps only the compact 'warnings' badge (detail still available via "
            "/tasks/:id). The /control board polls 'summary' since it renders "
            "only the badge — trims the largest part of the 8 s poll payload."
        ),
    ),
    card_body: str = Query(
        "full",
        description=(
            "Per-card long-text payload: 'full' (default) keeps the task's "
            "body and result on each card; 'none' drops both (the /control "
            "board never renders them — its card schema strips the fields — "
            "and detail views fetch /tasks/:id). On a ~300-card board body "
            "alone is well over half the poll payload."
        ),
    ),
):
    """Return the full board grouped by status column.

    ``_conn()`` auto-initializes ``kanban.db`` on first call so a fresh
    install doesn't surface a "failed to load" error on the plugin tab.

    ``board`` selects which board to read from. Omitting it falls
    through to the active board (``HERMES_KANBAN_BOARD`` env → on-disk
    ``current`` pointer → ``default``).
    """
    board = _resolve_board(board)
    source_errors: list[dict[str, Any]] = []
    conn = _conn(board=board, source_errors=source_errors)
    try:
        # Cache lookup. Keyed by the resolved DB file (NOT the query param:
        # omitting ``board`` follows the on-disk ``current`` pointer, which
        # can move without any DB write) plus every payload-shaping param.
        # A degraded open (source_errors) bypasses the cache entirely — its
        # payload carries the error annotation and must not be reused.
        version: Optional[tuple] = None
        cache_key: Optional[tuple] = None
        if not source_errors and _BOARD_CACHE_TTL_S > 0:
            try:
                db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
            except Exception:
                db_file = f"board:{board}"
            version = _board_db_version(conn)
            cache_key = (
                db_file,
                tenant,
                include_archived,
                workflow_template_id,
                current_step_key,
                card_diagnostics,
                card_body,
            )
            with _board_cache_lock:
                entry = _board_cache.get(cache_key)
                if (
                    entry
                    and entry["version"] == version
                    and time.monotonic() < entry["expires"]
                ):
                    _board_cache.move_to_end(cache_key)
                    cached_etag = entry["etag"]
                    cached_prefix = entry["body_prefix"]
                else:
                    entry = None
            if entry is not None:
                if request.headers.get("if-none-match") == cached_etag:
                    return _board_not_modified(cached_etag)
                return _board_json_response(cached_prefix, cached_etag)

        tasks = kanban_db.list_tasks(
            conn,
            tenant=tenant,
            include_archived=include_archived,
            workflow_template_id=workflow_template_id,
            current_step_key=current_step_key,
        )
        # Pre-fetch link counts per task (cheap: one query). The same pass
        # collects the dependents adjacency (parent → children) used to
        # resolve each card's chain root below.
        link_counts: dict[str, dict[str, int]] = {}
        dependents: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links"
        ).fetchall():
            link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})[
                "children"
            ] += 1
            link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})[
                "parents"
            ] += 1
            dependents.setdefault(row["parent_id"], []).append(row["child_id"])

        # Chain root per card: the tree SINK — the task nobody depends on
        # (link convention: a child waits for its parent; decompose links the
        # root as child of every subtask, see kanban_db.decompose_triage_task).
        # Same root definition as kanban_db.runs_summary. Memoised + cycle-
        # safe; a diamond (multiple dependents) resolves deterministically via
        # the smallest child id. Standalone tasks are their own root.
        root_cache: dict[str, str] = {}

        def _resolve_root(tid: str) -> str:
            visited: list[str] = []
            cur = tid
            while cur not in root_cache:
                if cur in visited:
                    break  # cycle guard: current node becomes the sink
                visited.append(cur)
                nxt = dependents.get(cur)
                if not nxt:
                    break
                cur = min(nxt)
            sink = root_cache.get(cur, cur)
            for v in visited:
                root_cache[v] = sink
            return sink

        # Comment + event counts (both cheap aggregates).
        comment_counts: dict[str, int] = {
            r["task_id"]: r["n"]
            for r in conn.execute(
                "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
            )
        }

        # Progress rollup: for each parent, how many children are done / total.
        # One pass over task_links joined with child status — cheaper than
        # N per-task queries and the plugin uses it to render "N/M".
        progress: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.status AS cstatus "
            "FROM task_links l JOIN tasks t ON t.id = l.child_id"
        ).fetchall():
            p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
            p["total"] += 1
            if row["cstatus"] == "done":
                p["done"] += 1

        # Diagnostics rollup for this board — see kanban_diagnostics.
        # We get the full structured list per task AND a compact
        # summary for the card badge (so cards don't carry the detail
        # text; the drawer fetches that via /tasks/:id or /diagnostics).
        diagnostics_per_task = _compute_task_diagnostics(conn, task_ids=None)

        latest_event_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()["m"]

        columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
        if include_archived:
            columns["archived"] = []

        # Batch-fetch the latest non-null run summary per task in one
        # window-function query (avoids N+1 ``latest_summary`` calls
        # for boards with hundreds of tasks). Truncated to a card-size
        # preview here — the full text is available via /tasks/:id.
        summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        # Per-task cost/token rollup for the Flow-board card footer — one batch
        # query (mirrors the chain-graph per-node aggregate). Tasks with no runs
        # are omitted, so their cards render no cost footer.
        cost_map = kanban_db.batch_task_costs(conn, [t.id for t in tasks])
        # Slice b: the live review stage (verifier→reviewer→critic) currently
        # targeted, for the chain card's stage pill. One batch query; only surfaced
        # for tasks actually in ``review`` (below), so a done task that was once
        # reviewed never shows a stale stage.
        active_stage_map = kanban_db.batch_active_review_stages(conn, [t.id for t in tasks])

        # Block-reason for blocked tasks: the latest task_runs.summary for each
        # blocked task distinguishes operator holds ("operator hold") from other
        # blocked causes (circuit-breaker, dependency stall). One batch query.
        blocked_ids = [t.id for t in tasks if t.status == "blocked"]
        block_reason_map: dict[str, Optional[str]] = {}
        operator_question_map: dict[str, bool] = {}
        if blocked_ids:
            block_reason_map = kanban_db.latest_summaries(conn, blocked_ids)
            operator_question_map = kanban_db.blocked_task_operator_questions(conn, tasks)
        planspec_source_map: dict[str, str] = {}
        if tasks:
            placeholders = ",".join("?" for _ in tasks)
            planspec_source_map = {
                row["id"]: row["planspec_source"]
                for row in conn.execute(
                    f"SELECT id, planspec_source FROM tasks WHERE id IN ({placeholders}) AND planspec_source IS NOT NULL",
                    [t.id for t in tasks],
                ).fetchall()
            }

        for t in tasks:
            full = summary_map.get(t.id)
            preview = (
                full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None
            )
            d = _task_dict(t, latest_summary=preview)
            if card_body == "none":
                # The /control poller renders neither field on a card —
                # body alone dominates the payload on real boards.
                d.pop("body", None)
                d.pop("result", None)
            # Surface block_reason for blocked tasks so the UI can distinguish
            # operator holds (block_reason contains "operator hold") from other
            # blocked states (circuit-breaker, review-required, etc.).
            # Non-blocked tasks carry null — additive, old clients skip the key.
            d["block_reason"] = block_reason_map.get(t.id) if t.status == "blocked" else None
            d["operator_question"] = operator_question_map.get(t.id, False)
            d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
            d["comment_count"] = comment_counts.get(t.id, 0)
            d["progress"] = progress.get(t.id)  # None when the task has no children
            # Chain key for the /control Flow board: equals the task's own id
            # for standalone tasks and chain roots, the sink's id for members.
            d["root_id"] = _resolve_root(t.id)
            d["vault_memory_links"] = _with_vault_memory_file_urls(
                kanban_db.vault_memory_links_for_task(
                    t,
                    latest_summary=full,
                    planspec_source=planspec_source_map.get(t.id),
                    source_char_limit=_VAULT_MEMORY_CARD_SOURCE_CHARS,
                    limit=4,
                )
            )
            cost = cost_map.get(t.id)
            if cost is not None:
                # Per-run cost read-out for the card footer — only attached when
                # the task actually ran (no runs → no keys → no footer). Same
                # five fields the chain-graph nodes carry, so one renderer fits both.
                d["cost_usd"] = cost["cost_usd"]
                d["input_tokens"] = cost["input_tokens"]
                d["output_tokens"] = cost["output_tokens"]
                d["cost_usd_equivalent"] = cost["cost_usd_equivalent"]
                d["cost_effective_usd"] = cost["cost_effective_usd"]
            # Slice b: live review-stage pill — only while the task is in review, so
            # a done/blocked task never shows a stale stage. Absent key → schema null.
            if t.status == "review":
                stage = active_stage_map.get(t.id)
                if stage is not None:
                    d["active_review_stage"] = stage
            diags = diagnostics_per_task.get(t.id)
            if diags:
                # The full list lets a drawer render without a second round-trip
                # (the kanban plugin dashboard uses it). Callers that only render
                # the badge pass ``card_diagnostics=summary`` to drop it — it is
                # the bulk of the board payload — and fetch detail via /tasks/:id.
                if card_diagnostics != "summary":
                    d["diagnostics"] = diags
                d["warnings"] = _warnings_summary_from_diagnostics(diags)
            col = t.status if t.status in columns else "todo"
            columns[col].append(d)

        # Stable per-column ordering already applied by list_tasks
        # (priority DESC, created_at ASC), keep as-is.

        # List of known tenants for the UI filter dropdown.
        tenants = [
            r["tenant"]
            for r in conn.execute(
                "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant"
            )
        ]
        # List of distinct assignees for the lane-by-profile sub-grouping.
        assignees = [
            r["assignee"]
            for r in conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL "
                "AND status != 'archived' ORDER BY assignee"
            )
        ]

        payload = {
            "columns": [
                {"name": name, "tasks": columns[name]} for name in columns.keys()
            ],
            "tenants": tenants,
            "assignees": assignees,
            "latest_event_id": int(latest_event_id),
        }
        if source_errors:
            payload["source_errors"] = source_errors
        # Conditional GET: a weak ETag over the content WITHOUT the volatile
        # wall-clock fields — "now" and each card's derived "age" change every
        # second and would defeat caching. Both derive from timestamps that
        # ARE hashed (created/started/completed_at), so excluding them never
        # masks a real change. Diagnostics stay IN the hash on purpose: some
        # rules are time-driven and must invalidate when they flip. The SPA
        # polls every 8 s; on an idle board the browser's If-None-Match
        # revalidation turns a ~1 MB transfer into a 304. no-cache (NOT
        # no-store) = the browser keeps the body but revalidates every time.
        etag_basis = {
            **payload,
            "columns": [
                {
                    "name": col["name"],
                    "tasks": [
                        {k: v for k, v in t.items() if k != "age"}
                        for t in col["tasks"]
                    ],
                }
                for col in payload["columns"]
            ],
        }
        etag = 'W/"' + hashlib.sha256(
            json.dumps(etag_basis, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()[:32] + '"'
        # Serialise once (matching JSONResponse's compact encoding) and keep
        # the result — minus the closing brace — for the per-request ``now``
        # append and for cache reuse by later identical requests.
        body_prefix = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str,
        ).encode()[:-1]
        if cache_key is not None:
            with _board_cache_lock:
                _board_cache[cache_key] = {
                    "version": version,
                    "expires": time.monotonic() + _BOARD_CACHE_TTL_S,
                    "etag": etag,
                    "body_prefix": body_prefix,
                }
                _board_cache.move_to_end(cache_key)
                while len(_board_cache) > _BOARD_CACHE_MAX_ENTRIES:
                    _board_cache.popitem(last=False)
        if request.headers.get("if-none-match") == etag:
            return _board_not_modified(etag)
        return _board_json_response(body_prefix, etag)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /tasks/review-verdicts
# ---------------------------------------------------------------------------

@evidence_routes.get("/tasks/review-verdicts")
def list_review_verdicts(
    limit: int = Query(12, ge=1, description="Maximum review tasks to return (capped at 50)"),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return review-gate tasks plus their latest verifier signal.

    Includes both tasks parked in ``review`` and tasks actively claimed by the
    verifier (``running`` with a claimed event whose ``source_status`` was
    ``review``). Done-task markers are carried by /runs/recent-results.
    """
    board = _resolve_board(board)
    capped_limit = max(1, min(int(limit), 50))
    conn = _conn(board=board)
    try:
        tasks = conn.execute(
            """
            SELECT id, title, status, assignee, created_at, current_run_id
            FROM tasks t
            WHERE status = 'review'
               OR EXISTS (
                    SELECT 1 FROM task_events e
                    WHERE e.task_id = t.id
                      AND e.run_id = t.current_run_id
                      AND e.kind = 'claimed'
                      AND json_extract(e.payload, '$.source_status') = 'review'
               )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (capped_limit,),
        ).fetchall()
        reviews: list[dict[str, Any]] = []
        for task in tasks:
            run = _review_signal_run(conn, task["id"])
            reviews.append(_review_verdict_dict(task, run))
        return {
            "reviews": reviews,
            "count": len(reviews),
            "checked_at": int(time.time()),
            "limit": capped_limit,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /tasks/:id
# ---------------------------------------------------------------------------

@core_routes.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    board: Optional[str] = Query(None),
    run_state_type: Optional[str] = Query(
        None, description="With run_state_name: filter runs by column 'status' or 'outcome'",
    ),
    run_state_name: Optional[str] = Query(
        None, description="With run_state_type: exact value for that run column",
    ),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if (run_state_type is None) ^ (run_state_name is None):
            raise HTTPException(
                status_code=400,
                detail="run_state_type and run_state_name must be passed together or omitted",
            )
        if run_state_type is not None and run_state_type not in ("status", "outcome"):
            raise HTTPException(
                status_code=400,
                detail="run_state_type must be 'status' or 'outcome'",
            )
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        # Drawer/detail view returns the FULL summary (no truncation) so
        # operators can read the complete worker handoff without making
        # a second round-trip. Cards on /board carry a 200-char preview.
        full_summary = kanban_db.latest_summary(conn, task_id)
        task_d = _task_dict(task, latest_summary=full_summary)
        links = _links_for(conn, task_id)
        child_ids = links["children"]
        child_summaries = kanban_db.latest_summaries(conn, child_ids)
        child_results = []
        for child_id in child_ids:
            child = kanban_db.get_task(conn, child_id)
            if child is not None:
                child_results.append(
                    {
                        "id": child.id,
                        "title": child.title,
                        "status": child.status,
                        "latest_summary": child_summaries.get(child.id),
                        "result": child.result,
                    }
                )
        task_d["operator_question"] = kanban_db.blocked_task_operator_questions(
            conn, [task]
        ).get(task.id, False)
        # K6: per-task cost = sum of cost_usd across this task's runs.
        # None until K5a populates the column / a run records a cost.
        task_d["cost_usd"] = kanban_db.task_runs_cost_usd_sum(conn, task_id=task_id)
        # Attach diagnostics so the drawer's Diagnostics section can
        # render recovery actions without a second round-trip.
        diags = _compute_task_diagnostics(conn, task_ids=[task_id])
        diag_list = diags.get(task_id) or []
        if diag_list:
            task_d["diagnostics"] = diag_list
            task_d["warnings"] = _warnings_summary_from_diagnostics(diag_list)
        # Card->PlanSpec 1-hop: resolve the originating PlanSpec straight off the
        # task's own row (no parent->root walk) so the drawer can deep-link a
        # card to its spec. None for non-PlanSpec tasks.
        planspec_source = kanban_db.planspec_source_for_task(conn, task_id)
        task_d["planspec_source"] = planspec_source
        comments = kanban_db.list_comments(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        task_d["vault_memory_links"] = _with_vault_memory_file_urls(
            kanban_db.vault_memory_links_for_task(
                task,
                latest_summary=full_summary,
                planspec_source=planspec_source,
                comments=comments,
                events=events,
            )
        )
        runs = kanban_db.list_runs(
            conn,
            task_id,
            state_type=run_state_type,
            state_name=run_state_name,
        )
        legacy_resolver = _LegacyModelRouteResolver(conn, list(runs), board=board)
        return {
            "task": task_d,
            "comments": [_comment_dict(c) for c in comments],
            "events": [_event_dict(e) for e in events],
            "attachments": [_attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)],
            "deliverables": _list_task_deliverables(task_id),
            "links": links,
            "child_results": child_results,
            "runs": [
                _run_dict(conn, r, legacy_resolver=legacy_resolver)
                for r in runs
            ],
        }
    finally:
        conn.close()


@evidence_routes.get("/tasks/{task_id}/deliverables")
def list_task_deliverables(task_id: str):
    """List preserved worker deliverables for a task.

    Files are enumerated from ``<kanban_home>/reports/by-task/<task_id>`` only.
    ``RESULT.md`` sorts first because it is the conventional human-readable
    handoff; all other nearby artifacts follow alphabetically by relative path.
    """
    deliverables = _list_task_deliverables(task_id)
    return {
        "task_id": task_id,
        "deliverables": deliverables,
        "count": len(deliverables),
    }


@evidence_routes.get("/tasks/{task_id}/deliverables/{relative_path:path}")
def download_task_deliverable(task_id: str, relative_path: str):
    """Serve one preserved deliverable through the dashboard auth boundary."""
    path = _resolve_deliverable_file(task_id, relative_path)
    return FileResponse(
        path,
        media_type=_deliverable_content_type(path),
        filename=path.name,
        content_disposition_type="inline",
    )


# ---------------------------------------------------------------------------
# POST /tasks
# ---------------------------------------------------------------------------

class CreateTaskBody(BaseModel):
    title: ShortText
    body: Optional[FreeText] = None
    assignee: Optional[ShortText] = None
    tenant: Optional[ShortText] = None
    priority: int = 0
    workspace_kind: ShortText = "scratch"
    workspace_path: Optional[ShortText] = None
    parents: list[ShortText] = Field(default_factory=list, max_length=_LIST_MAX_LENGTH)
    triage: bool = False
    # Park the freshly-created task in ``scheduled`` (atomically, in this same
    # handler) so the autonomous orchestrator/dispatcher do NOT auto-launch it.
    # Used by the dashboard's "copy to Fleet" action: the operator explicitly
    # clicks Dispatch in the Fleet to start it, instead of a one-click transfer
    # silently kicking off a worker (or an epic decompose).
    park: bool = False
    idempotency_key: Optional[ShortText] = None
    max_runtime_seconds: Optional[int] = None
    skills: Optional[list[ShortText]] = Field(default=None, max_length=_LIST_MAX_LENGTH)
    # Subscribe the new task to every configured home channel so its terminal
    # state (and, via H1 inheritance, its decompose children's) reaches the
    # team's home channel without a manual notify-subscribe. Opt-out for
    # bulk/scripted creation that doesn't want the notification.
    notify_home: bool = True
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    # Phase B (Programm 3): per-task model escalation — highest precedence in
    # the spawn resolution (task.model_override > active lane > profile).
    model_override: Optional[ShortText] = None
    # Phase C lever carried from the Flow capture sheet: a PARKED task carries the
    # chosen review tier so the staged-review resolver governs it at dispatch.
    # Only the park capture sends it (the levers are hidden for lean+auto). Optional
    # → omitting it is byte-identical to today.
    review_tier: Optional[Literal["standard", "review", "critical"]] = None


@core_routes.post("/tasks")
def create_task(payload: CreateTaskBody, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            assignee = kanban_db.validate_spawnable_assignee(payload.assignee) if payload.assignee else None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        task_id = kanban_db.create_task(
            conn,
            title=payload.title,
            body=payload.body,
            assignee=assignee,
            created_by="dashboard",
            workspace_kind=payload.workspace_kind,
            workspace_path=payload.workspace_path,
            tenant=payload.tenant,
            priority=payload.priority,
            parents=payload.parents,
            triage=payload.triage,
            idempotency_key=payload.idempotency_key,
            max_runtime_seconds=payload.max_runtime_seconds,
            skills=payload.skills,
            goal_mode=payload.goal_mode,
            goal_max_turns=payload.goal_max_turns,
            model_override=payload.model_override,
            # P1-S3: standalone operator create couples a scout to a resolved-critical
            # task (flag/tier-gated, idempotent). Skip when this create will be parked
            # below — a held task defers its scout to release (no held-scout deadlock).
            auto_scout=not bool(payload.park),
        )
        if payload.park:
            _park_task_for_operator(
                conn,
                task_id,
                reason="Aus dem Backlog in die Fleet kopiert — wartet auf Dispatch.",
                allow_existing_active=False,
            )
        # Phase C: stamp the chosen review tier on the freshly-created (parked)
        # task. After park so the column write survives; the setter validates the
        # value and is a no-op for None.
        if payload.review_tier:
            kanban_db.set_task_review_tier(conn, task_id, payload.review_tier)
        if payload.notify_home:
            _subscribe_task_to_home_channels(conn, task_id)
        task = kanban_db.get_task(conn, task_id)
        body: dict[str, Any] = {"task": _task_dict(task) if task else None}
        # Surface a dispatcher-presence warning so the UI can show a
        # banner when a `ready` task would otherwise sit idle because no
        # gateway is running (or dispatch_in_gateway=false). Only emit
        # for ready+assigned tasks; triage/todo are expected to wait,
        # and unassigned tasks can't be dispatched regardless.
        if task and task.status == "ready" and task.assignee:
            try:
                from hermes_cli.kanban import _check_dispatcher_presence
                running, message = _check_dispatcher_presence()
                if not running and message:
                    body["warning"] = message
            except Exception:
                # Probe failure must never block the create itself.
                pass
        return body
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Attachments — upload / list / download / delete (#35338)
# ---------------------------------------------------------------------------

# Cap a single upload so a runaway request can't fill the disk. 25 MB
# comfortably covers PDFs, images, and source docs — the kanban use case.
_MAX_ATTACHMENT_BYTES = kanban_db.KANBAN_ATTACHMENT_MAX_BYTES


def _safe_attachment_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Strips any directory components (``os.path.basename`` on both
    separators) so a malicious ``../../etc/passwd`` or ``C:\\x`` collapses
    to its leaf. Rejects empty / dotfile-only names. The result is only
    ever joined under the per-task attachments dir, never used verbatim
    as a path from the client.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    # Drop control chars and leading dots so we never write a dotfile or
    # a name with embedded NULs/newlines.
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '\x00').strip()
    name = name.lstrip(".").strip()
    if not name:
        raise HTTPException(status_code=400, detail="invalid attachment filename")
    return name[:200]


@core_routes.get("/tasks/{task_id}/attachments")
def list_task_attachments(task_id: str, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {
            "attachments": [
                _attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)
            ]
        }
    finally:
        conn.close()


@core_routes.post("/tasks/{task_id}/attachments")
async def upload_task_attachment(
    task_id: str,
    file: UploadFile = File(...),
    board: Optional[str] = Query(None),
    uploaded_by: Optional[str] = Form(None),
):
    """Store an uploaded file for a task and record its metadata.

    The blob lands under ``attachments_root(board)/<task_id>/`` with a
    sanitised, collision-resolved name. The worker reads it via the
    absolute path surfaced in ``build_worker_context``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")

        safe_name = _safe_attachment_name(file.filename or "")

        # Stream to disk with a hard size cap so a huge upload can't fill
        # the disk. Read in chunks; abort + clean up if the cap is hit.
        dest_dir = kanban_db.task_attachments_dir(task_id, board=board)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Resolve name collisions: foo.pdf → foo (1).pdf, foo (2).pdf, …
        stem, dot, ext = safe_name.partition(".")
        candidate = safe_name
        n = 1
        while (dest_dir / candidate).exists():
            candidate = f"{stem} ({n}){dot}{ext}"
            n += 1
        dest_path = dest_dir / candidate

        total = 0
        try:
            with open(dest_path, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ATTACHMENT_BYTES:
                        out.close()
                        dest_path.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"attachment exceeds {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
                            ),
                        )
                    out.write(chunk)
        except HTTPException:
            raise
        except OSError:
            log.exception("failed to store attachment")
            raise HTTPException(status_code=500, detail="failed to store attachment")

        att_id = kanban_db.add_attachment(
            conn,
            task_id,
            filename=candidate,
            stored_path=str(dest_path.resolve()),
            content_type=file.content_type,
            size=total,
            uploaded_by=(uploaded_by or "dashboard"),
        )
        att = kanban_db.get_attachment(conn, att_id)
        return {"attachment": _attachment_dict(att) if att else None}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@core_routes.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        att = kanban_db.get_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        # Confirm the blob still lives under the board's attachments root
        # before serving — defense in depth against a tampered DB row.
        root = kanban_db.attachments_root(board=board).resolve()
        try:
            stored = Path(att.stored_path).resolve()
            stored.relative_to(root)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="attachment file unavailable")
        if not stored.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing on disk")
        return FileResponse(
            path=str(stored),
            filename=att.filename,
            media_type=att.content_type or "application/octet-stream",
        )
    finally:
        conn.close()


@core_routes.delete("/attachments/{attachment_id}")
def remove_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        att = kanban_db.delete_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        return {"ok": True, "id": attachment_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PATCH /tasks/:id  (status / assignee / priority / title / body)
# ---------------------------------------------------------------------------

class UpdateTaskBody(BaseModel):
    status: Optional[ShortText] = None
    assignee: Optional[ShortText] = None
    priority: Optional[int] = None
    title: Optional[ShortText] = None
    body: Optional[FreeText] = None
    result: Optional[FreeText] = None
    block_reason: Optional[FreeText] = None
    # Structured handoff fields — forwarded to complete_task when status
    # transitions to 'done'. Dashboard parity with ``hermes kanban
    # complete --summary ... --metadata ...``.
    summary: Optional[FreeText] = None
    metadata: Optional[dict] = None
    # Epic membership: explicit null detaches; absent leaves it untouched
    # (distinguished via model_fields_set in the handler).
    epic_id: Optional[ShortText] = None
    # Phase B: explicit null clears the override; absent leaves it untouched.
    model_override: Optional[ShortText] = None


@core_routes.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: UpdateTaskBody, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")

        # --- assignee ----------------------------------------------------
        if payload.assignee is not None:
            try:
                if payload.assignee:
                    try:
                        assignee = kanban_db.validate_spawnable_assignee(payload.assignee)
                    except ValueError:
                        # Repair/edit safety: active off-disk assignees remain
                        # visible so operators can edit metadata or explicitly
                        # clear/reassign them. Preserve an unchanged invalid
                        # value, but reject switching to a different invalid lane.
                        proposed = kanban_db.canonical_assignee(payload.assignee)
                        current = kanban_db.canonical_assignee(task.assignee)
                        if proposed == current:
                            assignee = task.assignee
                        else:
                            raise
                else:
                    assignee = None
                ok = True if assignee == task.assignee else kanban_db.assign_task(
                    conn, task_id, assignee,
                )
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if not ok:
                raise HTTPException(status_code=404, detail="task not found")

        # --- epic membership -----------------------------------------------
        if "epic_id" in payload.model_fields_set:
            try:
                ok = kanban_db.set_task_epic(conn, task_id, payload.epic_id or None)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not ok:
                raise HTTPException(status_code=404, detail="task not found")

        # --- model override (Phase B) ---------------------------------------
        if "model_override" in payload.model_fields_set:
            ok = kanban_db.set_task_model_override(
                conn, task_id, payload.model_override,
            )
            if not ok:
                raise HTTPException(status_code=404, detail="task not found")

        # --- status -------------------------------------------------------
        if payload.status is not None:
            s = payload.status
            ok = True
            if s == "done":
                ok = kanban_db.complete_task(
                    conn, task_id,
                    result=payload.result,
                    summary=payload.summary,
                    metadata=payload.metadata,
                )
            elif s == "blocked":
                ok = kanban_db.block_task(conn, task_id, reason=payload.block_reason)
            elif s == "scheduled":
                ok = kanban_db.schedule_task(conn, task_id, reason=payload.block_reason)
            elif s == "ready":
                # Re-open a blocked/scheduled task, or just an explicit status set.
                current = kanban_db.get_task(conn, task_id)
                if current and current.status in ("blocked", "scheduled"):
                    ok = kanban_db.unblock_task(conn, task_id)
                else:
                    # Direct status write for drag-drop (todo -> ready etc).
                    ok = _set_status_direct(conn, task_id, "ready")
            elif s == "archived":
                ok = kanban_db.archive_task(conn, task_id)
            elif s == "running":
                raise HTTPException(
                    status_code=400,
                    detail="Cannot set status to 'running' directly; use the dispatcher/claim path",
                )
            elif s in ("todo", "triage", "scheduled"):
                ok = _set_status_direct(conn, task_id, s)
            else:
                raise HTTPException(status_code=400, detail=f"unknown status: {s}")
            if not ok:
                # For ``ready``, name the blocking parent(s) so the dashboard
                # can render an actionable toast instead of a silent no-op.
                # See #26744.
                if s == "ready":
                    blockers = _parents_blocking_ready(conn, task_id)
                    if blockers:
                        names = ", ".join(
                            f"{p['title']!r} ({p['id']}, status={p['status']})"
                            for p in blockers
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Cannot move to 'ready': blocked by parent(s) "
                                f"not done — {names}"
                            ),
                        )
                raise HTTPException(
                    status_code=409,
                    detail=f"status transition to {s!r} not valid from current state",
                )

        # --- priority -----------------------------------------------------
        if payload.priority is not None:
            with kanban_db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET priority = ? WHERE id = ?",
                    (int(payload.priority), task_id),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'reprioritized', ?, ?)",
                    (task_id, json.dumps({"priority": int(payload.priority)}),
                     int(time.time())),
                )

        # --- title / body -------------------------------------------------
        if payload.title is not None or payload.body is not None:
            with kanban_db.write_txn(conn):
                sets, vals = [], []
                if payload.title is not None:
                    if not payload.title.strip():
                        raise HTTPException(status_code=400, detail="title cannot be empty")
                    sets.append("title = ?")
                    vals.append(payload.title.strip())
                if payload.body is not None:
                    sets.append("body = ?")
                    vals.append(payload.body)
                vals.append(task_id)
                conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals,
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'edited', NULL, ?)",
                    (task_id, int(time.time())),
                )

        updated = kanban_db.get_task(conn, task_id)
        return {"task": _task_dict(updated) if updated else None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

@core_routes.delete("/tasks/{task_id}")
def delete_task(task_id: str, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.delete_task(conn, task_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {"deleted": True, "task_id": task_id}
    finally:
        conn.close()


def _parents_blocking_ready(
    conn: sqlite3.Connection, task_id: str,
) -> list:
    """Return parent rows (``id``, ``title``, ``status``) that aren't ``done``
    and therefore prevent ``task_id`` from being promoted to ``ready``.

    Used to enrich the 409 response from :func:`update_task` so the
    dashboard can show an actionable toast (#26744) instead of a silent
    no-op.  Returns ``[]`` when nothing blocks the transition (e.g. no
    parents, or all parents already done).
    """
    rows = conn.execute(
        "SELECT t.id, t.title, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ? AND t.status != 'done'",
        (task_id,),
    ).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "status": r["status"]}
        for r in rows
    ]


def _set_status_direct(
    conn: sqlite3.Connection, task_id: str, new_status: str,
) -> bool:
    """Direct status write for drag-drop moves that aren't covered by the
    structured complete/block/unblock/archive verbs (e.g. todo<->ready,
    running<->ready). Appends a ``status`` event row for the live feed.

    When this transitions OFF ``running`` to anything other than the
    terminal verbs above (which own their own run closing), we close the
    active run with outcome='reclaimed' so attempt history isn't
    orphaned. ``running -> ready`` via drag-drop is the common case
    (user yanking a stuck worker back to the queue).
    """
    with kanban_db.write_txn(conn):
        # Snapshot current state so we know whether to close a run.
        prev = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if prev is None:
            return False

        # Guard: don't allow promoting to 'ready' unless all parents are done.
        # Prevents the dispatcher from spawning a child whose upstream work
        # hasn't completed (e.g. T4 dispatched while T3 is still blocked).
        if new_status == "ready":
            parent_statuses = conn.execute(
                "SELECT t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ?",
                (task_id,),
            ).fetchall()
            if parent_statuses and not all(
                p["status"] == "done" for p in parent_statuses
            ):
                return False

        was_running = prev["status"] == "running"
        reopening_satisfied_parent = (
            prev["status"] in {"done", "archived"}
            and new_status not in {"done", "archived"}
        )

        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (new_status, new_status, new_status, new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and new_status != "running" and prev["current_run_id"]:
            run_id = kanban_db._end_run(
                conn, task_id,
                outcome="reclaimed", status="reclaimed",
                summary=f"status changed to {new_status} (dashboard/direct)",
            )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": new_status}), int(time.time())),
        )
        if reopening_satisfied_parent:
            # A parent leaving done/archived invalidates any direct child that
            # was sitting in ready solely because that parent used to satisfy
            # the dependency gate. Demote those children immediately so the
            # dashboard does not keep advertising stale-ready work.
            for row in conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
                (task_id,),
            ).fetchall():
                child_id = row["child_id"]
                demoted = conn.execute(
                    "UPDATE tasks SET status = 'todo' "
                    "WHERE id = ? AND status = 'ready'",
                    (child_id,),
                )
                if demoted.rowcount == 1:
                    conn.execute(
                        "INSERT INTO task_events (task_id, kind, payload, created_at) "
                        "VALUES (?, 'status', ?, ?)",
                        (
                            child_id,
                            json.dumps(
                                {
                                    "status": "todo",
                                    "reason": "parent_reopened",
                                    "parent": task_id,
                                }
                            ),
                            int(time.time()),
                        ),
                    )
    # If we re-opened something, children may have gone stale.
    if new_status in {"done", "ready"}:
        kanban_db.recompute_ready(conn)
    return True


def _park_task_for_operator(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    allow_existing_active: bool,
) -> None:
    fresh = kanban_db.get_task(conn, task_id)
    if fresh is None:
        return
    if not allow_existing_active and fresh.status in ("done", "archived", "scheduled", "running"):
        return
    if fresh.status == "triage":
        _set_status_direct(conn, task_id, "todo")
    kanban_db.schedule_task(conn, task_id, reason=reason)


def _subscribe_task_to_home_channels(conn: sqlite3.Connection, task_id: str) -> None:
    for home in _configured_home_channels():
        kanban_db.add_notify_sub(
            conn,
            task_id=task_id,
            platform=home["platform"],
            chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None,
            notifier_profile=_active_profile_name(),
        )


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class CommentBody(BaseModel):
    body: FreeText
    author: Optional[ShortText] = "dashboard"


class AnswerTaskBody(BaseModel):
    answer: FreeText


@evidence_routes.post("/tasks/{task_id}/answer")
def answer_task_question(
    task_id: str,
    payload: AnswerTaskBody,
    board: Optional[str] = Query(None),
):
    if not payload.answer.strip():
        raise HTTPException(status_code=400, detail="answer is required")
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        status = kanban_db.answer_operator_question(
            conn,
            task_id,
            answer=payload.answer,
            author="operator",
        )
        if status is None:
            raise HTTPException(
                status_code=409,
                detail="Task ist keine aktuelle Operator-Frage",
            )
        return {"ok": True, "task_id": task_id, "status": status}
    finally:
        conn.close()


@core_routes.post("/tasks/{task_id}/comments")
def add_comment(task_id: str, payload: CommentBody, board: Optional[str] = Query(None)):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        kanban_db.add_comment(
            conn, task_id, author=payload.author or "dashboard", body=payload.body,
        )
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

class LinkBody(BaseModel):
    parent_id: ShortText
    child_id: ShortText


@core_routes.post("/links")
def add_link(payload: LinkBody, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        kanban_db.link_tasks(conn, payload.parent_id, payload.child_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@core_routes.delete("/links")
def delete_link(
    parent_id: str = Query(...),
    child_id: str = Query(...),
    board: Optional[str] = Query(None),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.unlink_tasks(conn, parent_id, child_id)
        return {"ok": bool(ok)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bulk actions (multi-select on the board)
# ---------------------------------------------------------------------------

class BulkTaskBody(BaseModel):
    ids: list[ShortText] = Field(max_length=_LIST_MAX_LENGTH)
    status: Optional[ShortText] = None
    assignee: Optional[ShortText] = None  # "" or None = unassign
    priority: Optional[int] = None
    archive: bool = False
    result: Optional[FreeText] = None
    summary: Optional[FreeText] = None
    metadata: Optional[dict] = None
    reclaim_first: bool = False


@core_routes.post("/tasks/bulk")
def bulk_update(payload: BulkTaskBody, board: Optional[str] = Query(None)):
    """Apply the same patch to every id in ``payload.ids``.

    This is an *independent* iteration — per-task failures don't abort
    siblings. Returns per-id outcome so the UI can surface partials.
    """
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    results: list[dict] = []
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        for tid in ids:
            entry: dict[str, Any] = {"id": tid, "ok": True}
            try:
                task = kanban_db.get_task(conn, tid)
                if task is None:
                    entry.update(ok=False, error="not found")
                    results.append(entry)
                    continue
                if payload.archive:
                    if not kanban_db.archive_task(conn, tid):
                        entry.update(ok=False, error="archive refused")
                if payload.status is not None and not payload.archive:
                    s = payload.status
                    if s == "done":
                        ok = kanban_db.complete_task(
                            conn, tid,
                            result=payload.result,
                            summary=payload.summary,
                            metadata=payload.metadata,
                        )
                    elif s == "blocked":
                        ok = kanban_db.block_task(conn, tid)
                    elif s == "ready":
                        cur = kanban_db.get_task(conn, tid)
                        if cur and cur.status in ("blocked", "scheduled"):
                            ok = kanban_db.unblock_task(conn, tid)
                        else:
                            ok = _set_status_direct(conn, tid, "ready")
                    elif s == "running":
                        entry.update(
                            ok=False,
                            error=(
                                "Cannot set status to 'running' directly; "
                                "use the dispatcher/claim path"
                            ),
                        )
                        results.append(entry)
                        continue
                    elif s == "scheduled":
                        ok = kanban_db.schedule_task(conn, tid)
                    elif s in {"todo", "triage"}:
                        ok = _set_status_direct(conn, tid, s)
                    else:
                        entry.update(ok=False, error=f"unknown status {s!r}")
                        results.append(entry)
                        continue
                    if not ok:
                        entry.update(ok=False, error=f"transition to {s!r} refused")
                if payload.assignee is not None:
                    try:
                        if payload.reclaim_first:
                            ok = kanban_db.reassign_task(
                                conn, tid, payload.assignee or None,
                                reclaim_first=True,
                            )
                        else:
                            ok = kanban_db.assign_task(
                                conn, tid, payload.assignee or None,
                            )
                        if not ok:
                            entry.update(ok=False, error="assign refused")
                    except RuntimeError as e:
                        entry.update(ok=False, error=str(e))
                if payload.priority is not None:
                    with kanban_db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET priority = ? WHERE id = ?",
                            (int(payload.priority), tid),
                        )
                        conn.execute(
                            "INSERT INTO task_events (task_id, kind, payload, created_at) "
                            "VALUES (?, 'reprioritized', ?, ?)",
                            (tid, json.dumps({"priority": int(payload.priority)}),
                             int(time.time())),
                        )
            except Exception as e:  # defensive — one bad id shouldn't kill the batch
                entry.update(ok=False, error=str(e))
            results.append(entry)
        return {"results": results}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Diagnostics — fleet-wide distress signals (hallucinations, crashes,
# spawn failures, stuck-blocked). See hermes_cli.kanban_diagnostics for
# the rule engine.
# ---------------------------------------------------------------------------

@core_routes.get("/diagnostics")
def list_diagnostics(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
    severity: Optional[str] = Query(
        None,
        description="Filter by severity: warning|error|critical",
    ),
):
    """Return ``[{task_id, task_title, task_status, task_assignee,
    diagnostics: [...]}, ...]`` for every task on the board with at
    least one active diagnostic.

    Severity-filterable so the UI can render "just the critical ones"
    or the CLI can grep. Useful for the board-header attention strip
    AND for ``hermes kanban diagnostics`` which shells to this
    endpoint when the dashboard's running, or invokes the engine
    directly when it isn't.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        diags_by_task = _compute_task_diagnostics(conn, task_ids=None)
        if not diags_by_task:
            return {"diagnostics": [], "count": 0}

        # Narrow by severity if asked.
        if severity:
            filtered: dict[str, list[dict]] = {}
            for tid, dl in diags_by_task.items():
                keep = [d for d in dl if kd.severity_at_or_above(d.get("severity"), severity)]
                if keep:
                    filtered[tid] = keep
            diags_by_task = filtered
            if not diags_by_task:
                return {"diagnostics": [], "count": 0}

        # Pull the task rows we need in one query so we can include
        # titles/statuses without a per-task lookup.
        ids = list(diags_by_task.keys())
        placeholders = ",".join(["?"] * len(ids))
        rows = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        }

        out = []
        for tid, dl in diags_by_task.items():
            r = rows.get(tid)
            out.append({
                "task_id": tid,
                "task_title": r["title"] if r else None,
                "task_status": r["status"] if r else None,
                "task_assignee": r["assignee"] if r else None,
                "diagnostics": dl,
            })
        # Sort: highest severity first, then most recent.
        from hermes_cli.kanban_diagnostics import SEVERITY_ORDER
        sev_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        def _sort_key(row):
            top = row["diagnostics"][0]
            return (
                -sev_idx.get(top.get("severity"), -1),
                -(top.get("last_seen_at") or 0),
            )
        out.sort(key=_sort_key)

        return {
            "diagnostics": out,
            "count": sum(len(d["diagnostics"]) for d in out),
        }
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# Worker visibility — cross-task active-worker list, per-run inspection,
# and live cross-worker event feed
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cross-worker live event feed (worker tab ticker)
# ---------------------------------------------------------------------------
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


def run_progress_value(run_row: sqlite3.Row, now_ts: int) -> Optional[float]:
    """S2: Honest additive 0..1 run progress from EXISTING persisted columns.

    elapsed = now - started_at; progress = elapsed / max_runtime_seconds.
    Returns None when max_runtime_seconds is missing/0 or started_at is null
    (claude-cli lanes, uncapped runs). No guessed values.
    """
    max_rt = run_row["max_runtime_seconds"] if "max_runtime_seconds" in run_row.keys() else None
    started = run_row["started_at"] if "started_at" in run_row.keys() else None
    if max_rt and max_rt > 0 and started and started > 0:
        elapsed = max(0, now_ts - int(started))
        return min(1.0, elapsed / float(max_rt))
    return None


@core_routes.get("/workers/active")
def list_active_workers(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return every currently-running worker on the board.

    A worker is a ``task_runs`` row whose ``ended_at`` is NULL and whose
    ``worker_pid`` is non-NULL, belonging to a task with ``status='running'``.

    Returns ``{workers: [...], count: N, checked_at: <epoch>}``.  Each
    worker entry carries enough context for the dashboard to link back to
    its task without a second round-trip.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        rows = conn.execute(
            """
            SELECT
                r.id          AS run_id,
                r.task_id,
                t.title       AS task_title,
                t.status      AS task_status,
                t.assignee    AS task_assignee,
                r.profile,
                r.worker_pid,
                r.started_at,
                r.claim_lock,
                r.claim_expires,
                r.last_heartbeat_at,
                r.max_runtime_seconds,
                r.status       AS run_status,
                r.outcome      AS run_outcome,
                t.result       AS block_reason,
                r.step_key,
                t.model_override AS model_override,
                r.metadata,
                r.requested_provider,
                r.requested_model,
                r.active_provider,
                r.active_model,
                r.model_state,
                r.model_source,
                r.model_observed_at,
                r.input_tokens,
                r.output_tokens
            FROM task_runs r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.ended_at IS NULL
              AND r.worker_pid IS NOT NULL
              AND t.status = 'running'
            ORDER BY r.started_at ASC
            """,
        ).fetchall()
        # Phase A (progress): latest heartbeat note per run (one grouped
        # query, no N+1) + duration percentiles per profile for the honest
        # ETA ("ueblich ~8 min - laeuft 5 min" instead of a fake percent).
        notes: dict[int, dict] = {}
        heartbeat_ticks: dict[int, list[int]] = {}
        run_ids = [int(row["run_id"]) for row in rows]
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            # MAX(id) statt MAX(created_at): Heartbeats derselben Sekunde
            # (Tool-Wechsel) brauchen einen deterministischen Tiebreaker.
            for n in conn.execute(
                f"SELECT e.run_id, json_extract(e.payload, '$.note') AS note, "
                f"       e.created_at AS at "
                f"FROM task_events e JOIN ("
                f"  SELECT MAX(id) AS id FROM task_events "
                f"  WHERE kind = 'heartbeat' AND run_id IN ({placeholders}) "
                f"    AND json_extract(payload, '$.note') IS NOT NULL "
                f"  GROUP BY run_id"
                f") m ON m.id = e.id",
                run_ids,
            ).fetchall():
                notes[int(n["run_id"])] = {"note": n["note"], "at": n["at"]}
            # Heartbeat ticks per run for the Puls-Leitstand band chart.
            # One grouped query; cap at 20 newest timestamps PER RUN. A global
            # LIMIT can starve quieter workers when one noisy run dominates the
            # newest events, so rank inside each run before filtering.
            heartbeat_ticks = {rid: [] for rid in run_ids}
            for h in conn.execute(
                f"""
                SELECT run_id, created_at
                  FROM (
                    SELECT run_id, created_at,
                           ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY id DESC) AS rn
                      FROM task_events
                     WHERE kind = 'heartbeat'
                       AND run_id IN ({placeholders})
                  )
                 WHERE rn <= 20
                 ORDER BY run_id, rn ASC
                """,
                run_ids,
            ).fetchall():
                rid = int(h["run_id"])
                heartbeat_ticks[rid].append(int(h["created_at"]))
            for rid in heartbeat_ticks:
                heartbeat_ticks[rid].reverse()
        eta = kanban_db.run_duration_percentiles(
            conn, [row["profile"] for row in rows],
        )
        workers = []
        legacy_resolver = _LegacyModelRouteResolver(conn, list(rows), board=board)
        # S2: run_progress is the additive, honest 0..1 run-progress signal.
        # Derived from ALREADY-persisted columns (started_at + max_runtime_seconds)
        # — no new migration, no guessed values. null when max_runtime_seconds is
        # missing/0 (claude-cli lanes, uncapped runs) so the UI falls back to the
        # ETA heuristic (etaFraction) rather than rendering a fake percent.
        now_ts = int(time.time())
        for row in rows:
            note = notes.get(int(row["run_id"]), {})
            prof_eta = eta.get((row["profile"] or "").strip(), {})
            model_override = row["model_override"] or None
            model_route = _run_model_route_fields(
                conn,
                row,
                board=board,
                legacy_resolver=legacy_resolver,
            )
            has_input_tokens = row["input_tokens"] is not None
            has_output_tokens = row["output_tokens"] is not None
            if has_input_tokens and has_output_tokens:
                token_status = "live"
                token_status_reason = None
            elif has_input_tokens or has_output_tokens:
                token_status = "partial"
                token_status_reason = "only one live token counter is available"
            else:
                token_status = "no_live_sample"
                token_status_reason = "task_runs has no live token counters for this active worker yet"
            workers.append({
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "task_title": row["task_title"],
                "task_status": row["task_status"],
                "task_assignee": row["task_assignee"],
                "profile": row["profile"],
                "worker_pid": row["worker_pid"],
                "started_at": row["started_at"],
                "claim_lock": row["claim_lock"],
                "claim_expires": row["claim_expires"],
                "last_heartbeat_at": row["last_heartbeat_at"],
                "max_runtime_seconds": row["max_runtime_seconds"],
                # C2: the SPA's WorkerSchema/health logic consumes these; they
                # were declared frontend-side but never sent, so blocked/offline
                # health (→ unlock/restart actions) never surfaced from live data.
                "run_status": row["run_status"],
                "run_outcome": row["run_outcome"],
                "block_reason": row["block_reason"],
                "last_heartbeat_note": note.get("note"),
                "last_heartbeat_note_at": note.get("at"),
                "heartbeat_ticks": heartbeat_ticks.get(int(row["run_id"]), []),
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "token_status": token_status,
                "token_status_reason": token_status_reason,
                "eta_p50_seconds": prof_eta.get("p50"),
                "eta_p90_seconds": prof_eta.get("p90"),
                # B1: step progress + model resolution
                "step_key": row["step_key"],
                "model_override": model_override,
                # S2: additives Run-Fortschritt 0..1 (elapsed/max_runtime).
                # null wenn kein Cap → UI fällt auf etaFraction-Heuristik zurück.
                "run_progress": run_progress_value(row, now_ts),
                **model_route,
            })
        # F4: expose the live concurrency cap (kanban.max_in_progress) so the UI
        # can show capacity/Engpass honestly — "3 von 3 Worker, warum dispatcht
        # nichts" instead of guessing. None when no cap is configured.
        cap: Optional[int] = None
        try:
            from hermes_cli.config import load_config
            _k = (load_config() or {}).get("kanban") or {}
            _cap = _k.get("max_in_progress")
            cap = int(_cap) if isinstance(_cap, (int, float)) and int(_cap) >= 1 else None
        except Exception:
            cap = None
        return {
            "workers": workers,
            "count": len(workers),
            "cap": cap,
            "checked_at": int(time.time()),
        }
    finally:
        conn.close()


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
# B2 — Task activity timeline (read-only)
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


def _decision_queue_block_reason_from_payload(raw_payload: Any) -> Optional[str]:
    if raw_payload is None:
        return None
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    reason = str(payload.get("reason") or "").strip()
    return reason or None


def _decision_queue_block_reasons(
    conn: sqlite3.Connection,
    task_ids: list[str],
) -> dict[str, Optional[str]]:
    task_ids = [task_id for task_id in task_ids if task_id]
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    try:
        rows = conn.execute(
            "SELECT task_id, payload FROM ("
            "  SELECT task_id, payload, "
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY task_id ORDER BY created_at DESC, id DESC"
            "         ) AS rn "
            "  FROM task_events "
            f"  WHERE kind = 'blocked' AND task_id IN ({placeholders})"
            ") WHERE rn = 1",
            task_ids,
        ).fetchall()
    except Exception:
        return {}
    return {
        row["task_id"]: _decision_queue_block_reason_from_payload(row["payload"])
        for row in rows
    }


def _enrich_decision_queue_block_reasons(
    conn: sqlite3.Connection,
    queue: dict[str, Any],
) -> dict[str, Any]:
    decisions = queue.get("decisions")
    if not isinstance(decisions, list):
        return queue
    task_ids = [
        str(row.get("task_id") or "")
        for row in decisions
        if isinstance(row, dict) and isinstance(row.get("operator_escalation"), dict)
    ]
    block_reasons = _decision_queue_block_reasons(conn, task_ids)
    for row in decisions:
        if not isinstance(row, dict):
            continue
        escalation = row.get("operator_escalation")
        if not isinstance(escalation, dict):
            continue
        evidence = escalation.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
            escalation["evidence"] = evidence
        evidence["block_reason"] = block_reasons.get(str(row.get("task_id") or ""))
    return queue


@control_routes.get("/decision-queue")
def get_decision_queue(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """N-E1: consolidated operator-decision feed.

    Folds every decision-ready board state (sticky_blocked, review_rejected,
    role_fit_held, budget_held, decompose_failed, stranded_by_stuck_parent)
    into one read-only list, one row per decision. Thin wrapper around
    :func:`kanban_db.decision_queue` (read-only, fail-soft per category).
    """
    board = _resolve_board(board)
    try:
        from hermes_cli.config import load_config

        kanban_config = ((load_config() or {}).get("kanban") or {})
    except Exception:
        kanban_config = {}

    conn = _conn(board=board)
    try:
        queue = kanban_db.decision_queue(conn, config=kanban_config)
        return _enrich_decision_queue_block_reasons(conn, queue)
    finally:
        conn.close()


@observability_routes.get("/release-status")
def get_release_status(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Read-only auto-release status (feeds a future dashboard tile).

    Returns the current ``release.autonomous`` kill-switch state, the last 10
    ``auto_release`` timeline events, and the last 5 ``release/pre-deploy/*``
    git anchors. Fail-soft on the anchors (subprocess/git trouble never blocks
    the endpoint).
    """
    from hermes_cli.auto_release import _release_config

    board = _resolve_board(board)
    cfg = _release_config()

    conn = _conn(board=board)
    try:
        rows = conn.execute(
            "SELECT task_id, created_at, payload FROM task_events "
            "WHERE kind = 'auto_release' ORDER BY created_at DESC, id DESC LIMIT 10",
        ).fetchall()
        recent = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            recent.append(
                {
                    "task_id": row["task_id"],
                    "created_at": row["created_at"],
                    "payload": payload,
                }
            )
    finally:
        conn.close()

    anchors: list[str] = []
    try:
        import subprocess

        from hermes_cli.auto_release import _repo_root

        proc = subprocess.run(
            ["git", "tag", "-l", "release/pre-deploy/*"],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        anchors = sorted(proc.stdout.split())[-5:]
    except Exception:
        anchors = []

    return {
        "autonomous": cfg.get("autonomous", False),
        "max_tier_autonomous": cfg.get("max_tier_autonomous", "review"),
        "recent": recent,
        "anchors": anchors,
    }


_RELEASE_TIERS = {"standard", "review", "critical"}


class ReleaseModeBody(BaseModel):
    # Both optional so a caller can flip just one knob (e.g. only Reichweite)
    # without having to resend the other — see set_release_mode_endpoint.
    autonomous: Optional[bool] = None
    max_tier_autonomous: Optional[str] = None


class ReleaseConcurrencyBody(BaseModel):
    # All optional so the coupled Risiko-Tab "Parallele Worker pro Profil"
    # lever can POST just {max_in_progress_per_profile, max_concurrent_per_repo}
    # without clobbering the independent global max_in_progress knob.
    max_in_progress: Optional[int] = None
    max_in_progress_per_profile: Optional[int] = None
    max_concurrent_per_repo: Optional[int] = None


def _read_root_kanban_cfg() -> dict:
    """Direct (uncached) read of the ``kanban`` block from the ROOT
    config.yaml — same direct-read style as ``_release_config()`` (not the
    cached ``load_config()`` — avoids any staleness right after an atomic
    write). Returns ``{}`` on any parse trouble or absent config, never
    raises (every ``_read_*`` caller below is advisory-only)."""
    try:
        import yaml

        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if not cfg_path.is_file():
            return {}
        with open(cfg_path, "r", encoding="utf-8") as fh:
            root_cfg = yaml.safe_load(fh) or {}
        kanban_cfg = root_cfg.get("kanban") or {}
        return kanban_cfg if isinstance(kanban_cfg, dict) else {}
    except Exception:
        return {}


def _dispatch_kwargs_for_tick(
    *, max_spawn_override: Optional[int] = None,
) -> dict[str, Any]:
    dispatch_kwargs = kanban_db.dispatch_kwargs_from_config(
        _read_root_kanban_cfg()
    )
    if max_spawn_override is not None:
        dispatch_kwargs["max_spawn"] = max_spawn_override
    return dispatch_kwargs


def _read_max_in_progress() -> int:
    """kanban.max_in_progress from the ROOT config.yaml. Default 3, the F4
    default already used by the /workers `cap` field."""
    raw = _read_root_kanban_cfg().get("max_in_progress")
    if isinstance(raw, (int, float)) and int(raw) >= 1:
        return int(raw)
    return 3


def _read_max_in_progress_per_profile() -> Optional[int]:
    """kanban.max_in_progress_per_profile from the ROOT config.yaml — the
    REAL value, or ``None`` when absent/invalid. Unlike ``max_in_progress``
    (default 3), this cap's config default is unlimited (config.py), so a
    fake ``1`` here would misrepresent an "unlimited" install as
    single-worker-per-profile."""
    raw = _read_root_kanban_cfg().get("max_in_progress_per_profile")
    if isinstance(raw, (int, float)) and int(raw) >= 1:
        return int(raw)
    return None


def _read_max_concurrent_per_repo() -> int:
    """kanban.max_concurrent_per_repo from the ROOT config.yaml, default 1
    (matches the dispatcher's own default —
    ``gateway.kanban_watchers._read_dispatch_caps``)."""
    raw = _read_root_kanban_cfg().get("max_concurrent_per_repo")
    if isinstance(raw, (int, float)) and int(raw) >= 1:
        return int(raw)
    return 1


def _read_serialize_by_repo() -> bool:
    """kanban.serialize_by_repo from the ROOT config.yaml, default True.
    Exposed read-only for the Risiko-Tab display — the coupled lever never
    changes it."""
    raw = _read_root_kanban_cfg().get("serialize_by_repo", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return True


def _read_red_streak() -> int:
    """Current consecutive-red-nights count (the "x" in the Risiko-Tab
    safety line "Auto-Stopp nach N roten · Streak x/N") — same ledger read
    ``auto_release.maybe_auto_release`` uses for ``pause_on_red_streak``.
    Advisory only: any failure degrades to 0, never blocks the endpoint."""
    try:
        from hermes_cli import vision_metrics as _vm

        return _vm.red_streak_from_head(_vm.read_gate_records())
    except Exception:
        return 0


def _release_mode_view() -> dict:
    """Read-only snapshot of the ``release.autonomous`` kill-switch and its
    companion policy knobs, sourced from the ROOT config.yaml via the same
    ``_release_config()`` used by the auto-release loop."""
    from hermes_cli.auto_release import _release_config

    cfg = _release_config()
    return {
        "autonomous": cfg["autonomous"],
        "max_tier_autonomous": cfg["max_tier_autonomous"],
        "pause_on_red_streak": cfg["pause_on_red_streak"],
        "red_streak": _read_red_streak(),
        "max_in_progress": _read_max_in_progress(),
        "max_in_progress_per_profile": _read_max_in_progress_per_profile(),
        "max_concurrent_per_repo": _read_max_concurrent_per_repo(),
        "serialize_by_repo": _read_serialize_by_repo(),
    }


@control_routes.get("/release-mode")
def get_release_mode_endpoint():
    """GET /release-mode — autonomous, max_tier_autonomous, pause_on_red_streak,
    red_streak, max_in_progress, max_in_progress_per_profile,
    max_concurrent_per_repo, serialize_by_repo.

    The read-side of the Risiko-Tab Hero cockpit; the POST twins
    (``/release-mode``, ``/release-concurrency``) flip these atomically.
    """
    return _release_mode_view()


@control_routes.post("/release-mode")
def set_release_mode_endpoint(payload: ReleaseModeBody):
    """POST /release-mode — flip ``release.autonomous`` and/or
    ``release.max_tier_autonomous`` atomically.

    Both fields are optional; only the fields present in the body are
    written (a Reichweite-only POST does not clobber autonomous, and vice
    versa). Backup → write → reload → return new state. Same auth/loopback
    protection as every other mutating kanban endpoint (enforced centrally
    by the web-server middleware on ``/api/plugins/kanban/...``).
    """
    if payload.autonomous is None and payload.max_tier_autonomous is None:
        raise HTTPException(status_code=400, detail="at least one of autonomous, max_tier_autonomous required")
    if payload.max_tier_autonomous is not None and payload.max_tier_autonomous not in _RELEASE_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"max_tier_autonomous must be one of {sorted(_RELEASE_TIERS)}",
        )

    from hermes_constants import get_default_hermes_root
    from utils import atomic_roundtrip_yaml_update

    cfg_path = get_default_hermes_root() / "config.yaml"
    backup_path = cfg_path.with_suffix(".yaml.bak")
    if cfg_path.is_file():
        backup_path.write_bytes(cfg_path.read_bytes())

    if payload.autonomous is not None:
        atomic_roundtrip_yaml_update(cfg_path, "release.autonomous", payload.autonomous)
    if payload.max_tier_autonomous is not None:
        atomic_roundtrip_yaml_update(cfg_path, "release.max_tier_autonomous", payload.max_tier_autonomous)

    # Reload through the same path the auto-release loop uses so the
    # returned state reflects what the next activation will observe.
    new_state = _release_mode_view()
    return {
        "ok": True,
        "autonomous": new_state["autonomous"],
        "max_tier_autonomous": new_state["max_tier_autonomous"],
        "pause_on_red_streak": new_state["pause_on_red_streak"],
        "red_streak": new_state["red_streak"],
        "max_in_progress": new_state["max_in_progress"],
        "backup": str(backup_path),
    }


@control_routes.post("/release-concurrency")
def set_release_concurrency_endpoint(payload: ReleaseConcurrencyBody):
    """POST /release-concurrency — set any of ``kanban.max_in_progress``,
    ``kanban.max_in_progress_per_profile``, ``kanban.max_concurrent_per_repo``
    atomically. All three fields are optional; only fields present in the
    body are written (mirrors ``set_release_mode_endpoint``'s partial-update
    contract) — the Risiko-Tab's coupled "Parallele Worker pro Profil" lever
    POSTs ``{max_in_progress_per_profile: N, max_concurrent_per_repo: N}``
    in one request without touching the independent global
    ``max_in_progress``.

    Validation is permissive (each present field just needs to be >= 1) —
    no cross-field ``<= max_in_progress`` guard here, that clamp lives in
    the UI. Backup → write → reload → return current concurrency values.
    Same auth/loopback protection as every other mutating kanban endpoint.
    """
    if (
        payload.max_in_progress is None
        and payload.max_in_progress_per_profile is None
        and payload.max_concurrent_per_repo is None
    ):
        raise HTTPException(
            status_code=400,
            detail="at least one of max_in_progress, max_in_progress_per_profile, "
            "max_concurrent_per_repo required",
        )
    if payload.max_in_progress is not None and payload.max_in_progress < 1:
        raise HTTPException(status_code=400, detail="max_in_progress must be >= 1")
    if (
        payload.max_in_progress_per_profile is not None
        and payload.max_in_progress_per_profile < 1
    ):
        raise HTTPException(
            status_code=400, detail="max_in_progress_per_profile must be >= 1"
        )
    if payload.max_concurrent_per_repo is not None and payload.max_concurrent_per_repo < 1:
        raise HTTPException(status_code=400, detail="max_concurrent_per_repo must be >= 1")

    from hermes_constants import get_default_hermes_root
    from utils import atomic_roundtrip_yaml_update

    cfg_path = get_default_hermes_root() / "config.yaml"
    backup_path = cfg_path.with_suffix(".yaml.bak")
    if cfg_path.is_file():
        backup_path.write_bytes(cfg_path.read_bytes())

    if payload.max_in_progress is not None:
        atomic_roundtrip_yaml_update(cfg_path, "kanban.max_in_progress", payload.max_in_progress)
    if payload.max_in_progress_per_profile is not None:
        atomic_roundtrip_yaml_update(
            cfg_path, "kanban.max_in_progress_per_profile", payload.max_in_progress_per_profile
        )
    if payload.max_concurrent_per_repo is not None:
        atomic_roundtrip_yaml_update(
            cfg_path, "kanban.max_concurrent_per_repo", payload.max_concurrent_per_repo
        )

    return {
        "ok": True,
        "max_in_progress": _read_max_in_progress(),
        "max_in_progress_per_profile": _read_max_in_progress_per_profile(),
        "max_concurrent_per_repo": _read_max_concurrent_per_repo(),
        "backup": str(backup_path),
    }


@control_routes.get("/epics")
def list_epics_endpoint(
    include_closed: bool = Query(True, description="Include closed epics"),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """N-E3: list durable epics with per-epic task/cost rollups (read-only)."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        epics = kanban_db.list_epics(conn, include_closed=include_closed)
        return {"epics": epics, "count": len(epics)}
    finally:
        conn.close()


@control_routes.get("/epics/{epic_id}")
def get_epic_endpoint(
    epic_id: str,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """N-E3: one epic with its member tasks + rollup. 404 if absent."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        epic = kanban_db.get_epic(conn, epic_id)
        if epic is None:
            raise HTTPException(status_code=404, detail=f"epic {epic_id} not found")
        return {"epic": epic}
    finally:
        conn.close()


class CreateEpicBody(BaseModel):
    title: ShortText
    body: Optional[FreeText] = None


@control_routes.post("/epics")
def create_epic_endpoint(
    payload: CreateEpicBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Create a durable epic from the board (UI parity with ``kanban epic create``)."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            eid = kanban_db.create_epic(conn, title=payload.title, body=payload.body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"epic": kanban_db.get_epic(conn, eid)}
    finally:
        conn.close()


@control_routes.post("/epics/{epic_id}/close")
def close_epic_endpoint(
    epic_id: str,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Close an epic (organisational act — member tasks stay untouched)."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if not kanban_db.close_epic(conn, epic_id):
            raise HTTPException(status_code=404, detail=f"epic {epic_id} not found")
        return {"epic": kanban_db.get_epic(conn, epic_id)}
    finally:
        conn.close()


# --- Lanes (night-sprint F1) — switchable profile→routing presets ---


_LANE_CLAUDE_CLI_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "claude-fable-5", "label": "Claude Fable 5", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
)
_LANE_CLAUDE_CLI_MODEL_IDS = {str(item["id"]) for item in _LANE_CLAUDE_CLI_MODELS}


def _lane_provider_label(provider_id: str, provider_row: dict[str, Any] | None = None) -> str:
    provider_id = (provider_id or "").strip()
    if provider_row is not None:
        for key in ("name", "label", "display_name"):
            value = provider_row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if not provider_id:
        return "API-Modelle"
    known = {
        "openai-codex": "OpenAI Codex",
        "openrouter": "OpenRouter",
        "kimi-coding": "Kimi Coding",
        "kimi-coding-cn": "Kimi Coding CN",
        "google": "Google Gemini",
        "anthropic": "Anthropic",
        "nous": "Nous",
    }
    return known.get(provider_id, provider_id)


def _lane_model_label(model_id: str) -> str:
    if not model_id:
        return model_id
    compact = model_id.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    return " ".join(part.upper() if part.lower() in {"gpt", "k2"} else part.capitalize() for part in compact.split())


def _append_lane_model_option(
    out: list[dict[str, Any]],
    seen: set[tuple[str, str | None, str]],
    *,
    model: str,
    runtime: str,
    group: str,
    provider: str | None = None,
    label: str | None = None,
    locked: bool = False,
    source: str | None = None,
) -> None:
    model = (model or "").strip()
    if not model:
        return
    if runtime == "hermes" and model in _LANE_CLAUDE_CLI_MODEL_IDS:
        return
    provider = provider.strip() if isinstance(provider, str) and provider.strip() else None
    key = (model, provider, runtime)
    if key in seen:
        return
    seen.add(key)
    row = {
        "id": model,
        "label": label or _lane_model_label(model),
        "runtime": runtime,
        "group": group,
        "provider": provider,
        "locked": locked,
    }
    if source:
        row["source"] = source
    out.append(row)


def _append_openrouter_extra_model_options(
    out: list[dict[str, Any]],
    seen: set[tuple[str, str | None, str]],
) -> None:
    """Add locally admitted OpenRouter models from config.yaml."""
    try:
        from hermes_cli.model_catalog import get_configured_provider_extra_models

        model_ids = get_configured_provider_extra_models("openrouter")
    except Exception:
        log.exception("lanes: failed to load configured OpenRouter extra models")
        return
    for model_id in model_ids:
        _append_lane_model_option(
            out,
            seen,
            model=model_id,
            runtime="hermes",
            group="OpenRouter",
            provider="openrouter",
            label=model_id,
            source="config",
        )


# Last good inventory-sourced model rows. The live inventory occasionally
# returns empty (provider API blip, mobile network, auth refresh in flight);
# this snapshot keeps the Lanes dropdown AND the /persist validator stable so a
# model that was valid moments ago is not suddenly rejected.
_LANE_INVENTORY_CACHE: list[dict] = []


def _lane_model_catalog(profiles: list[dict], active_lane: Optional[dict] = None) -> list[dict]:
    """Provider-aware model list for Lanes.

    Hermes-runtime rows come from the shared inventory/model-catalog substrate
    used by the main picker. Claude-CLI rows are explicit Cloud Max choices;
    selecting them must route through ``claude -p`` rather than an API provider.
    ``active_lane`` (a ``kanban_db.get_active_lane``/``list_lanes`` row) adds a
    resilience source for models the lane currently pins per profile — see the
    lane-pinned block below.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str]] = set()

    for item in _LANE_CLAUDE_CLI_MODELS:
        _append_lane_model_option(
            out,
            seen,
            model=str(item["id"]),
            label=str(item["label"]),
            runtime="claude-cli",
            group=str(item["group"]),
            provider=None,
            locked=bool(item.get("locked")),
            source="claude-cli",
        )

    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(
            load_picker_context(),
            include_unconfigured=True,
            picker_hints=True,
            capabilities=True,
            max_models=200,
        )
        for provider_row in payload.get("providers") or []:
            if not isinstance(provider_row, dict):
                continue
            provider = str(provider_row.get("slug") or provider_row.get("id") or "").strip()
            if not provider:
                continue
            authenticated = provider_row.get("authenticated")
            configured = provider_row.get("configured")
            has_models = bool(provider_row.get("models"))
            if authenticated is False and configured is False and not has_models:
                continue
            group = _lane_provider_label(provider, provider_row)
            for model in provider_row.get("models") or []:
                if not isinstance(model, str) or not model.strip():
                    continue
                _append_lane_model_option(
                    out,
                    seen,
                    model=model,
                    runtime="hermes",
                    group=group,
                    provider=provider,
                    label=model,
                    source="inventory",
                )
    except Exception:
        log.exception("lanes: failed to build dynamic model catalog")

    # Resilience: a fresh build that yielded no inventory rows (transient
    # provider/network failure) must not strip API models the operator just
    # picked — reuse the last good snapshot; refresh it whenever live succeeds.
    global _LANE_INVENTORY_CACHE
    inventory_rows = [dict(r) for r in out if r.get("source") == "inventory"]
    if inventory_rows:
        _LANE_INVENTORY_CACHE = inventory_rows
    elif _LANE_INVENTORY_CACHE:
        for r in _LANE_INVENTORY_CACHE:
            _append_lane_model_option(
                out,
                seen,
                model=str(r.get("id") or ""),
                runtime=str(r.get("runtime") or "hermes"),
                group=str(r.get("group") or "API-Modelle"),
                provider=r.get("provider"),
                label=str(r.get("label") or r.get("id") or ""),
                source="inventory-cache",
            )

    _append_openrouter_extra_model_options(out, seen)

    # Profile defaults are live config and must stay visible even if the
    # curated catalog does not know them yet.
    for prof in profiles:
        try:
            model = (prof.get("default_model") or "").strip()
            if not model:
                continue
            runtime = "claude-cli" if prof.get("worker_runtime") == "claude-cli" else "hermes"
            if runtime == "hermes" and model in _LANE_CLAUDE_CLI_MODEL_IDS:
                continue
            group = "Claude (Max-Abo)" if runtime == "claude-cli" else "API-Modelle"
            _append_lane_model_option(
                out,
                seen,
                model=model,
                runtime=runtime,
                group=group,
                provider=prof.get("default_provider") if runtime == "hermes" else None,
                label=model,
                locked=runtime == "claude-cli",
                source="profile-default",
            )
        except Exception:
            continue

    # Resilience: a model the ACTIVE LANE currently pins per profile must stay
    # representable even if it fell out of every other catalog source
    # (provider outage, removed from extra_models, general catalog drift) —
    # otherwise a single stale pin 400s the ENTIRE /persist call, including
    # unrelated corrections riding along in the same payload (2026-06-27
    # incident: a metered lane could not be turned off via the dashboard
    # because its own current pin was unrepresentable).
    for entry in ((active_lane or {}).get("profiles") or {}).values():
        try:
            model = (entry.get("model") or "").strip()
            if not model:
                continue
            runtime = "claude-cli" if entry.get("worker_runtime") == "claude-cli" else "hermes"
            if runtime == "hermes" and model in _LANE_CLAUDE_CLI_MODEL_IDS:
                continue
            group = "Claude (Max-Abo)" if runtime == "claude-cli" else "API-Modelle"
            _append_lane_model_option(
                out,
                seen,
                model=model,
                runtime=runtime,
                group=group,
                provider=entry.get("provider") if runtime == "hermes" else None,
                label=model,
                locked=runtime == "claude-cli",
                source="lane-pinned",
            )
        except Exception:
            continue
    return out


_LANE_PROFILE_CACHE_TTL_S = 30.0
_lane_profile_cache: Optional[tuple[float, list[dict]]] = None


def _scan_lane_profiles() -> list[dict]:
    """Direct profile-dir scan for the Lanes UI dropdowns.

    Deliberately NOT ``list_profiles()``: that helper additionally rglobs
    every profile's skills/ tree (~100 files per profile) and probes
    gateway pids — measured at ~5s per call with 11 profiles, which made
    GET /lanes time out on mobile. The editor only needs name, runtime,
    default model and description, all of which live in two small YAML
    files per profile (same seams the dispatcher uses).
    """
    import yaml
    from hermes_cli.profiles import _PROFILE_ID_RE, _get_profiles_root, read_profile_meta

    out: list[dict] = []
    root = _get_profiles_root()
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == "default" or not _PROFILE_ID_RE.match(entry.name):
            continue
        runtime = "hermes"
        claude_model = None
        model = None
        try:
            cfg_path = entry / "config.yaml"
            if cfg_path.is_file():
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                if isinstance(cfg, dict):
                    if cfg.get("worker_runtime") == "claude-cli":
                        runtime = "claude-cli"
                    cm = cfg.get("claude_model")
                    if isinstance(cm, str) and cm.strip():
                        claude_model = cm.strip()
                    model_cfg = cfg.get("model")
                    provider = None
                    if isinstance(model_cfg, str):
                        model = model_cfg
                    elif isinstance(model_cfg, dict):
                        model = model_cfg.get("default") or model_cfg.get("model")
                        provider = model_cfg.get("provider")
                    from hermes_cli.fallback_config import get_fallback_chain
                    fallback_providers = get_fallback_chain(cfg)
                else:
                    provider = None
                    fallback_providers = []
            else:
                provider = None
                fallback_providers = []
        except Exception:
            provider = None
            fallback_providers = []
            pass
        out.append({
            "name": entry.name,
            "worker_runtime": runtime,
            "default_model": claude_model if runtime == "claude-cli" else model,
            "default_provider": None if runtime == "claude-cli" else (
                provider.strip() if isinstance(provider, str) and provider.strip() else None
            ),
            "fallback_providers": [] if runtime == "claude-cli" else fallback_providers,
            "description": read_profile_meta(entry).get("description", ""),
            "locked": runtime == "claude-cli",
            "locked_reason": "Claude-CLI / claude -p excluded from this slice" if runtime == "claude-cli" else None,
        })
    return out


def _lane_profile_catalog() -> list[dict]:
    """Profile names + config defaults for the Lanes UI dropdowns.

    Fail-soft: any error yields an empty list — the UI then falls back to
    free-text profile entry. Cached for a short TTL because the catalog
    only changes when someone edits a profile's config.yaml, while the
    Lanes tab refetches after every mutation.
    """
    global _lane_profile_cache
    now = time.monotonic()
    if _lane_profile_cache is not None and now - _lane_profile_cache[0] < _LANE_PROFILE_CACHE_TTL_S:
        return _lane_profile_cache[1]
    try:
        out = _scan_lane_profiles()
    except Exception:
        return []
    _lane_profile_cache = (now, out)
    return out


@lane_routes.get("/lanes")
def list_lanes_endpoint(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: all lane presets (seeding api-standard/max-abo on first contact)
    plus the profile catalog for the editor dropdowns."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        lanes = kanban_db.list_lanes(conn)
        active_lane = next((l for l in lanes if l["active"]), None)
        profiles = _lane_profile_catalog()
        models = _lane_model_catalog(profiles, active_lane)
        profiles = [
            {**p, "kanban_spawn_health": _profile_spawn_health(p, profiles, models)}
            for p in profiles
        ]
        return {
            "lanes": lanes,
            "count": len(lanes),
            "active_id": next((l["id"] for l in lanes if l["active"]), None),
            "profiles": profiles,
            "models": models,
        }
    finally:
        conn.close()


class LaneBody(BaseModel):
    name: Optional[ShortText] = None
    profiles: Optional[dict] = None


class LaneSpawnCheckBody(BaseModel):
    profile: ShortText
    worker_runtime: Literal["hermes", "claude-cli"]
    model: Optional[ShortText] = None


class LaneOpenRouterModelImportBody(BaseModel):
    raw_text: Optional[FreeText] = None
    model_ids: Optional[list[ShortText]] = None


_OPENROUTER_MODEL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*(?::[A-Za-z0-9._+-]+)?$"
)
_OPENROUTER_IMPORT_LIMIT = 25
LANES_AUTH_SMOKE_ROLE_LIMIT = 12

class LaneAuthSmokeBody(BaseModel):
    lane_id: Optional[ShortText] = None
    roles: Optional[list[ShortText]] = None
    timeout_seconds: Optional[int] = 45


_LANES_PROVIDER_RE = re.compile(r"provider=([A-Za-z0-9_.:/-]+)")
_LANES_MODEL_RE = re.compile(r"model=([^\s,]+)")
_LANES_SESSION_RE = re.compile(r"session=([A-Za-z0-9_.:-]+)|\[([A-Za-z0-9_.:-]+)\]")
_LANES_AUTH_RE = re.compile(
    r"\b(401|403)\b|missing bearer|unauthorized|invalid api|empty API key|authentication",
    re.IGNORECASE,
)
_LANES_QUOTA_RE = re.compile(
    r"\b(402|429)\b|quota|rate limit|RESOURCE_EXHAUSTED",
    re.IGNORECASE,
)
_LANES_TIMEOUT_RE = re.compile(r"timeout|timed out", re.IGNORECASE)
_LANES_SECRET_TEXT_RE = re.compile(
    r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s,;]+|"
    r"(bearer\s+)[^\s,;]+|"
    r"(token\s+)[^\s,;]+|"
    r"([\"']?(?:api[_-]?key|token|secret)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;]+[\"']?|"
    r"([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|KEY)=)[^\s,;]+|"
    r"((?:sk-proj|sk|ghp)[_-][A-Za-z0-9_-]+)"
)


def _parse_lanes_auth_smoke_log(
    lines: list[str],
    *,
    session_id: Optional[str] = None,
) -> dict[str, object]:
    scoped_lines = [line for line in lines if session_id and session_id in line] if session_id else list(lines)
    if session_id and not scoped_lines:
        scoped_lines = list(lines)

    observed_provider: Optional[str] = None
    observed_model: Optional[str] = None
    parsed_session_id: Optional[str] = session_id
    fallback_activated = False
    error_class: Optional[str] = None

    for line in scoped_lines:
        if "fallback" in line.lower():
            fallback_activated = True
        provider_match = _LANES_PROVIDER_RE.search(line)
        if provider_match:
            observed_provider = provider_match.group(1)
        model_match = _LANES_MODEL_RE.search(line)
        if model_match:
            observed_model = model_match.group(1)
        session_match = _LANES_SESSION_RE.search(line)
        if session_match and not parsed_session_id:
            parsed_session_id = session_match.group(1) or session_match.group(2)
        if _LANES_AUTH_RE.search(line):
            error_class = "auth_error"
        elif _LANES_QUOTA_RE.search(line):
            error_class = "quota_or_rate_limit"
        elif _LANES_TIMEOUT_RE.search(line):
            error_class = "timeout"

    return {
        "observed_provider": observed_provider,
        "observed_model": observed_model,
        "fallback_activated": fallback_activated,
        "error_class": error_class,
        "session_id": parsed_session_id,
    }


def _redact_lanes_auth_smoke_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        for prefix in match.groups():
            if prefix:
                lowered = prefix.lower()
                if lowered.startswith(("sk-", "sk_", "sk-proj", "ghp_", "ghp-")):
                    return "<redacted>"
                return f"{prefix}<redacted>"
        return "<redacted>"

    return _LANES_SECRET_TEXT_RE.sub(repl, text)


def _build_lanes_auth_smoke_command(
    *,
    python_bin: str,
    profile: str,
    provider: str,
    model: str,
    token: str,
) -> list[str]:
    return [
        python_bin,
        "-m",
        "hermes_cli.main",
        "--profile",
        profile,
        "chat",
        "-q",
        f"Reply exactly {token}",
        "--max-turns",
        "1",
        "-Q",
        "--ignore-rules",
        "--source",
        "lanes-auth-smoke",
        "--provider",
        provider,
        "--model",
        model,
    ]


def _derive_lanes_auth_smoke_status(
    *,
    returncode: int | str,
    response_exact: bool,
    requested_provider: str,
    requested_model: str,
    observed_provider: Optional[str],
    observed_model: Optional[str],
    fallback_activated: bool,
    error_class: Optional[str],
) -> str:
    if error_class:
        return error_class
    if returncode == "timeout":
        return "timeout"
    if fallback_activated:
        return "fallback"
    if observed_provider and observed_provider != requested_provider:
        return "fallback"
    if observed_model and observed_model != requested_model:
        return "fallback"
    if returncode == 0 and response_exact and observed_provider == requested_provider and observed_model == requested_model:
        return "ok"
    return "error"


def _explain_lanes_auth_smoke_result(
    *,
    status: str,
    requested_provider: str,
    requested_model: str,
    observed_provider: Optional[str],
    observed_model: Optional[str],
    response_exact: bool,
    fallback_activated: bool,
    error_class: Optional[str],
) -> str:
    observed = f"{observed_provider or '-'}/{observed_model or '-'}"
    parts = [
        f"requested {requested_provider or '-'}/{requested_model or '-'}",
        f"observed {observed}",
        "exact response" if response_exact else "response was not exact",
    ]
    if fallback_activated or status == "fallback":
        parts.append("fallback activated")
    if error_class:
        parts.append(f"error_class={error_class}")
    return "; ".join(parts)


def _summarize_lanes_auth_smoke(
    results: list[dict[str, object]],
    *,
    total_role_count: int,
    checked_role_count: int,
    truncated: bool,
) -> dict[str, object]:
    blocking_statuses = {"auth_error", "quota_or_rate_limit", "timeout", "config_error", "error"}
    blocking_roles = [
        str(item.get("role") or item.get("profile") or "unknown")
        for item in results
        if item.get("status") in blocking_statuses
    ]
    fallback_roles = [
        str(item.get("role") or item.get("profile") or "unknown")
        for item in results
        if bool(item.get("fallback_activated")) or item.get("status") == "fallback"
    ]
    skipped_roles = [
        str(item.get("role") or item.get("profile") or "unknown")
        for item in results
        if item.get("status") == "skipped"
    ]
    ok_count = sum(1 for item in results if item.get("status") == "ok")

    if not results:
        decision = "blocked"
        next_action = "Keine Rollen geprüft; Lane-Konfiguration oder Profilkatalog prüfen."
    elif blocking_roles:
        decision = "blocked"
        first = blocking_roles[0]
        next_action = f"{first.capitalize()} zuerst reparieren oder bewusst auf ein funktionierendes Modell umstellen."
    elif fallback_roles:
        decision = "restricted"
        first = fallback_roles[0]
        next_action = f"{first.capitalize()} verwendet Fallback; requested/observed Route prüfen."
    elif skipped_roles or truncated or checked_role_count < total_role_count:
        decision = "restricted"
        next_action = "Nicht geprüfte oder übersprungene Rollen vor Live-Freigabe bewusst bewerten."
    else:
        decision = "ready"
        next_action = "Lane kann nach kontrolliertem Dashboard-Respawn erneut produktiv verifiziert werden."

    return {
        "decision": decision,
        "safe_to_activate": decision == "ready",
        "ok_count": ok_count,
        "blocking_roles": blocking_roles,
        "fallback_roles": fallback_roles,
        "skipped_roles": skipped_roles,
        "checked_role_count": checked_role_count,
        "total_role_count": total_role_count,
        "truncated": truncated,
        "recommended_next_action": next_action,
    }


def _select_lanes_auth_smoke_roles(
    lane: dict[str, object],
    requested_roles: list[str] | None,
    catalog: list[dict],
) -> list[dict[str, object]]:
    lane_profiles = lane.get("profiles") if isinstance(lane, dict) else {}
    if not isinstance(lane_profiles, dict):
        lane_profiles = {}
    catalog_by_name = {
        str(item.get("name") or ""): item
        for item in catalog
        if str(item.get("name") or "").strip()
    }

    requested = [str(role).strip() for role in (requested_roles or []) if str(role).strip()]
    if requested:
        names = requested
    else:
        names = list(catalog_by_name)
        names.extend(name for name in lane_profiles if name not in catalog_by_name)

    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        lane_entry = lane_profiles.get(name) if isinstance(lane_profiles.get(name), dict) else {}
        catalog_entry = catalog_by_name.get(name, {})
        runtime = str(lane_entry.get("worker_runtime") or catalog_entry.get("worker_runtime") or "hermes")
        provider = lane_entry.get("provider")
        if provider is None:
            provider = catalog_entry.get("default_provider")
        model = lane_entry.get("model")
        if model is None:
            model = catalog_entry.get("default_model")
        results.append({
            "role": name,
            "profile": name,
            "runtime": runtime,
            "provider": str(provider or ""),
            "model": str(model or ""),
        })
    return results


def _lanes_auth_smoke_profile_log_path(profile: str) -> Path:
    try:
        from hermes_cli.profiles import resolve_profile_env

        return Path(resolve_profile_env(profile)) / "logs" / "agent.log"
    except Exception:
        return Path.home() / ".hermes" / "profiles" / profile / "logs" / "agent.log"


def _count_lanes_auth_smoke_log_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except FileNotFoundError:
        return 0


def _read_lanes_auth_smoke_log_lines(path: Path, start_line: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[start_line:]
    except FileNotFoundError:
        return []


def _normalize_openrouter_import_token(value: object) -> str:
    token = str(value or "").strip().strip("`'\"[]{}()")
    if token.lower().startswith("openrouter:"):
        token = token.split(":", 1)[1].strip()
    return token


def _parse_openrouter_import_tokens(payload: LaneOpenRouterModelImportBody) -> list[str]:
    raw: list[str] = []
    if payload.model_ids:
        raw.extend(str(item) for item in payload.model_ids)
    if payload.raw_text:
        raw.extend(re.split(r"[\s,;]+", payload.raw_text))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _normalize_openrouter_import_token(item)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _openrouter_extra_models_from_config() -> list[str]:
    try:
        from hermes_cli.model_catalog import get_configured_provider_extra_models

        return get_configured_provider_extra_models("openrouter")
    except Exception:
        log.exception("lanes: failed to read OpenRouter extra_models from config")
        return []


def _write_openrouter_extra_models_to_config(model_ids: list[str]) -> None:
    from hermes_cli.config import get_config_path

    config_path = get_config_path()
    try:
        from utils import atomic_roundtrip_yaml_update

        atomic_roundtrip_yaml_update(
            config_path,
            "model_catalog.providers.openrouter.extra_models",
            model_ids,
        )
        return
    except ModuleNotFoundError as exc:
        if exc.name != "ruamel":
            raise

    from hermes_cli.config import read_raw_config
    from utils import atomic_yaml_write

    cfg = read_raw_config()
    model_catalog = cfg.setdefault("model_catalog", {})
    if not isinstance(model_catalog, dict):
        model_catalog = {}
        cfg["model_catalog"] = model_catalog
    providers = model_catalog.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        model_catalog["providers"] = providers
    openrouter = providers.setdefault("openrouter", {})
    if not isinstance(openrouter, dict):
        openrouter = {}
        providers["openrouter"] = openrouter
    openrouter["extra_models"] = model_ids
    atomic_yaml_write(config_path, cfg, sort_keys=False)


def _admit_openrouter_extra_models(model_ids: list[str]) -> tuple[list[str], list[str]]:
    existing = _openrouter_extra_models_from_config()
    seen = set(existing)
    merged = list(existing)
    added: list[str] = []
    for model_id in model_ids:
        if model_id in seen:
            continue
        seen.add(model_id)
        merged.append(model_id)
        added.append(model_id)
    if added:
        from hermes_cli.model_catalog import reset_cache as reset_model_catalog_cache

        _write_openrouter_extra_models_to_config(merged)
        reset_model_catalog_cache()
    return added, merged


def _smoke_openrouter_model_id(model_id: str) -> tuple[bool, str]:
    """Run a minimal OpenRouter completion through Hermes runtime plumbing."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent

        runtime = resolve_runtime_provider(requested="openrouter", target_model=model_id)
        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            model=model_id,
            enabled_toolsets=[],
            quiet_mode=True,
            platform="dashboard",
            credential_pool=runtime.get("credential_pool"),
            max_iterations=1,
            max_tokens=8,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=None,
        )
        agent.tools = []
        agent.valid_tool_names = set()
        response = (agent.chat("Reply with OK.") or "").strip()
        if not response:
            return False, "Smoke produced no response"
        return True, "Smoke ok"
    except Exception as exc:  # noqa: BLE001 - expose sanitized provider failure
        try:
            from hermes_cli.error_sanitize import safe_detail

            return False, safe_detail(exc, "OpenRouter smoke failed", log=log)
        except Exception:
            log.exception("OpenRouter smoke failed")
            return False, "OpenRouter smoke failed"


def _lane_model_runtime(
    model: Optional[str],
    profiles: list[dict],
    models: Optional[list[dict]] = None,
) -> Optional[str]:
    """Return the curated runtime for ``model`` when known.

    Unknown models are intentionally fail-soft; profile defaults can be added
    by ``_lane_model_catalog`` and genuinely custom provider ids still need the
    real worker path to decide whether they work.
    """
    model = (model or "").strip()
    if not model:
        return None
    catalog = models if models is not None else _lane_model_catalog(profiles)
    for item in catalog:
        if item.get("id") == model:
            runtime = item.get("runtime")
            return runtime if runtime in {"hermes", "claude-cli"} else None
    if model.startswith("claude-"):
        return "claude-cli"
    return None


def _profile_spawn_health(profile: dict, profiles: list[dict], models: list[dict]) -> dict:
    """Spawn-Health eines Katalog-Profils für GET /lanes.

    Gleiche Prüf-Seams wie POST /lanes/spawn-check (Model↔Runtime-Widerspruch,
    claude-Binary), aber auf den Katalog-Defaults des Profils — das Frontend
    erwartet das Feld pro Profil und disabled sonst die Triage-Eskalation.
    """
    runtime = profile.get("worker_runtime") or "hermes"
    model = profile.get("default_model")
    model_runtime = _lane_model_runtime(model, profiles, models)
    if model_runtime and model_runtime != runtime:
        return {
            "status": "unhealthy",
            "reason": f"Model {model!r} belongs to {model_runtime}, but profile runtime is {runtime}",
        }
    if runtime == "claude-cli" and not _claude_worker_available():
        return {
            "status": "unhealthy",
            "reason": "`claude` executable is not available for claude-cli workers",
        }
    return {"status": "healthy", "reason": None}


def _claude_worker_available() -> bool:
    import shutil

    binary = kanban_db._claude_worker_bin()
    if os.path.sep in binary:
        return os.path.exists(binary)
    return shutil.which(binary) is not None


@lane_routes.post("/lanes/spawn-check")
def lane_spawn_check_endpoint(payload: LaneSpawnCheckBody):
    """Read-only Lane worker/model health check for the dashboard.

    This mirrors the dispatcher's lane seams without creating a task or
    touching the board: profile must exist in the lean lane catalog, the
    selected model must not contradict the selected worker runtime, and the
    claude-cli path must have an executable available.
    """
    profiles = _lane_profile_catalog()
    models = _lane_model_catalog(profiles)
    profile = next((p for p in profiles if p.get("name") == payload.profile), None)
    dispatcher_path = payload.worker_runtime
    resolved_model = payload.model or (profile or {}).get("default_model") or None

    if profile is None:
        return {
            "status": "unhealthy",
            "reason": f"Profile {payload.profile!r} is not in the lane catalog",
            "dispatcher_path": dispatcher_path,
            "resolved_model": resolved_model,
        }

    model_runtime = _lane_model_runtime(resolved_model, profiles, models)
    if model_runtime and model_runtime != dispatcher_path:
        return {
            "status": "unhealthy",
            "reason": f"Model {resolved_model!r} belongs to {model_runtime}, but selected worker runtime is {dispatcher_path}",
            "dispatcher_path": dispatcher_path,
            "resolved_model": resolved_model,
        }

    if dispatcher_path == "claude-cli":
        if not _claude_worker_available():
            return {
                "status": "unhealthy",
                "reason": "`claude` executable is not available for claude-cli workers",
                "dispatcher_path": dispatcher_path,
                "resolved_model": resolved_model,
            }
        return {
            "status": "healthy",
            "reason": "Claude CLI worker executable is available",
            "dispatcher_path": dispatcher_path,
            "resolved_model": resolved_model,
        }

    return {
        "status": "healthy",
        "reason": "Hermes worker profile is available",
        "dispatcher_path": dispatcher_path,
        "resolved_model": resolved_model,
    }

def _extract_lanes_auth_smoke_session_id(text: str, lines: list[str], *, token: str) -> Optional[str]:
    for line in lines:
        if token not in line and "lanes-auth-smoke" not in line:
            continue
        match = _LANES_SESSION_RE.search(line)
        if match:
            return match.group(1) or match.group(2)
    match = _LANES_SESSION_RE.search(text)
    if match:
        return match.group(1) or match.group(2)
    for line in reversed(lines):
        match = _LANES_SESSION_RE.search(line)
        if match:
            return match.group(1) or match.group(2)
    return None


def _lanes_auth_smoke_response_exact(stdout: str, token: str) -> bool:
    for line in stdout.splitlines():
        cleaned = line.strip().strip("`\"'")
        if cleaned == token:
            return True
    return stdout.strip().strip("`\"'") == token


def _run_single_lanes_auth_smoke(role: dict[str, object], *, timeout_seconds: int) -> dict[str, object]:
    import secrets
    import subprocess
    import sys

    profile = str(role.get("profile") or role.get("role") or "").strip()
    provider = str(role.get("provider") or "").strip()
    model = str(role.get("model") or "").strip()
    runtime = str(role.get("runtime") or "hermes").strip()
    role_name = str(role.get("role") or profile or "unknown")

    base = {
        "role": role_name,
        "profile": profile,
        "runtime": runtime,
        "requested_provider": provider,
        "requested_model": model,
        "observed_provider": None,
        "observed_model": None,
        "response_exact": False,
        "fallback_activated": False,
        "auth_ok": False,
        "session_id": None,
    }
    if runtime != "hermes":
        return {
            **base,
            "status": "skipped",
            "error_class": None,
            "reason": "unsupported runtime for auth smoke",
        }
    if not profile or not provider or not model:
        return {
            **base,
            "status": "config_error",
            "error_class": "missing_profile_provider_or_model",
            "reason": "missing profile, provider, or model",
        }

    agent_root = Path(__file__).resolve().parents[3]
    log_path = _lanes_auth_smoke_profile_log_path(profile)
    before_lines = _count_lanes_auth_smoke_log_lines(log_path)
    token = f"lanes-auth-smoke-{profile}-{secrets.token_hex(3)}"
    command = _build_lanes_auth_smoke_command(
        python_bin=sys.executable,
        profile=profile,
        provider=provider,
        model=model,
        token=token,
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(agent_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
        returncode: int | str = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        returncode = "timeout"
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else "timeout"

    new_lines = _read_lanes_auth_smoke_log_lines(log_path, before_lines)
    session_id = _extract_lanes_auth_smoke_session_id(stdout + "\n" + stderr, new_lines, token=token)
    parsed = _parse_lanes_auth_smoke_log(new_lines, session_id=session_id)
    response_exact = _lanes_auth_smoke_response_exact(stdout, token)
    status = _derive_lanes_auth_smoke_status(
        returncode=returncode,
        response_exact=response_exact,
        requested_provider=provider,
        requested_model=model,
        observed_provider=parsed["observed_provider"],
        observed_model=parsed["observed_model"],
        fallback_activated=bool(parsed["fallback_activated"]),
        error_class=parsed["error_class"],
    )
    reason = _explain_lanes_auth_smoke_result(
        status=status,
        requested_provider=provider,
        requested_model=model,
        observed_provider=parsed["observed_provider"],
        observed_model=parsed["observed_model"],
        response_exact=response_exact,
        fallback_activated=bool(parsed["fallback_activated"]),
        error_class=parsed["error_class"],
    )

    return {
        **base,
        "observed_provider": parsed["observed_provider"],
        "observed_model": parsed["observed_model"],
        "response_exact": response_exact,
        "fallback_activated": parsed["fallback_activated"],
        "auth_ok": status == "ok",
        "status": status,
        "error_class": parsed["error_class"],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "session_id": parsed["session_id"] or session_id,
        "reason": reason,
        "observed_response": _redact_lanes_auth_smoke_text(stdout.strip()[:300]),
        "stderr_preview": _redact_lanes_auth_smoke_text(stderr.strip()[:300]),
    }


@lane_routes.post("/lanes/auth-smoke")
def lane_auth_smoke_endpoint(
    payload: LaneAuthSmokeBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    board = _resolve_board(board)
    timeout_seconds = min(max(int(payload.timeout_seconds or 45), 5), 60)
    conn = _conn(board=board)
    try:
        lanes = kanban_db.list_lanes(conn)
        lane = None
        if payload.lane_id:
            lane = next((item for item in lanes if item.get("id") == payload.lane_id), None)
        else:
            lane = next((item for item in lanes if item.get("active")), None)
        if lane is None:
            raise HTTPException(status_code=404, detail="lane_not_found")
    finally:
        conn.close()

    profiles = _lane_profile_catalog()
    requested_roles = payload.roles or []
    all_roles = _select_lanes_auth_smoke_roles(lane, requested_roles, profiles)
    truncated = len(all_roles) > LANES_AUTH_SMOKE_ROLE_LIMIT
    roles = all_roles[:LANES_AUTH_SMOKE_ROLE_LIMIT]
    results = [
        _run_single_lanes_auth_smoke(role, timeout_seconds=timeout_seconds)
        for role in roles
    ]
    summary = _summarize_lanes_auth_smoke(
        results,
        total_role_count=len(all_roles),
        checked_role_count=len(results),
        truncated=truncated,
    )
    return {
        "ok": bool(results) and all(item.get("status") in {"ok", "skipped"} for item in results),
        "lane_id": lane.get("id"),
        "source": "lanes-auth-smoke",
        "scope": {
            "requested_roles": requested_roles,
            "checked_role_count": len(results),
            "total_role_count": len(all_roles),
            "truncated": truncated,
            "role_limit": LANES_AUTH_SMOKE_ROLE_LIMIT,
        },
        "summary": summary,
        "results": results,
    }


@lane_routes.post("/lanes/openrouter-models/import")
def lane_openrouter_model_import_endpoint(payload: LaneOpenRouterModelImportBody):
    """Smoke pasted OpenRouter model IDs and admit successful ones to config."""
    tokens = _parse_openrouter_import_tokens(payload)
    if len(tokens) > _OPENROUTER_IMPORT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"At most {_OPENROUTER_IMPORT_LIMIT} model IDs can be smoked at once",
        )

    results: list[dict[str, str]] = []
    smoke_ok: list[str] = []
    for token in tokens:
        if not _OPENROUTER_MODEL_ID_RE.fullmatch(token):
            results.append({
                "id": token,
                "status": "invalid",
                "reason": "Expected an OpenRouter model id like vendor/model",
            })
            continue
        ok, reason = _smoke_openrouter_model_id(token)
        if ok:
            smoke_ok.append(token)
            results.append({"id": token, "status": "smoke_ok", "reason": reason})
        else:
            results.append({"id": token, "status": "failed", "reason": reason})

    added, configured = _admit_openrouter_extra_models(smoke_ok) if smoke_ok else ([], _openrouter_extra_models_from_config())
    added_set = set(added)
    for row in results:
        if row["status"] != "smoke_ok":
            continue
        if row["id"] in added_set:
            row["status"] = "admitted"
            row["reason"] = "Smoke ok; added to config"
        else:
            row["status"] = "already_configured"
            row["reason"] = "Smoke ok; already present in config"

    return {
        "results": results,
        "admitted": added,
        "configured": configured,
    }


@lane_routes.post("/lanes")
def create_lane_endpoint(
    payload: LaneBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: create a lane preset (inactive until explicitly activated)."""
    if not payload.name:
        raise HTTPException(status_code=400, detail="name is required")
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            lane = kanban_db.create_lane(
                conn, name=payload.name, profiles=payload.profiles,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"lane": lane}
    finally:
        conn.close()


@lane_routes.put("/lanes/{lane_id}")
def update_lane_endpoint(
    lane_id: str,
    payload: LaneBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: rename a lane and/or replace its profile mapping."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            lane = kanban_db.update_lane(
                conn, lane_id, name=payload.name, profiles=payload.profiles,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if lane is None:
            raise HTTPException(status_code=404, detail=f"lane {lane_id} not found")
        return {"lane": lane}
    finally:
        conn.close()


@lane_routes.delete("/lanes/{lane_id}")
def delete_lane_endpoint(
    lane_id: str,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: delete a lane. The active lane is protected (409)."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            ok = kanban_db.delete_lane(conn, lane_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail=f"lane {lane_id} not found")
        return {"deleted": lane_id}
    finally:
        conn.close()


@lane_routes.post("/lanes/{lane_id}/activate")
def activate_lane_endpoint(
    lane_id: str,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: make this the single active lane. Takes effect from the next
    worker spawn — the dispatcher hot-reads the active lane per spawn."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        lane = kanban_db.activate_lane(conn, lane_id)
        if lane is None:
            raise HTTPException(status_code=404, detail=f"lane {lane_id} not found")
        return {"lane": lane}
    finally:
        conn.close()


class LanePersistFallbackEntry(BaseModel):
    provider: ShortText
    model: ShortText


class LanePersistProfileEntry(BaseModel):
    worker_runtime: Literal["hermes", "claude-cli"]
    provider: Optional[ShortText] = None
    model: ShortText
    # ``None`` preserves the pre-K31 behaviour for older API clients that omit
    # this field. An explicit empty list is materially different: it clears the
    # profile fallback chain and the active-lane override.
    fallback_providers: Optional[list[LanePersistFallbackEntry]] = None


class LanePersistBody(BaseModel):
    profiles: dict[str, LanePersistProfileEntry]


@lane_routes.post("/lanes/persist")
def persist_lane_models_endpoint(
    payload: LanePersistBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Persist complete lane selections to profile configs and the active lane.

    The operation is all-or-nothing across every requested profile. Profile
    configs are snapshotted before the first write and atomically restored if a
    later config write or the active-lane mirror fails. Older clients that omit
    ``fallback_providers`` keep their existing chain; an explicit ``[]`` clears
    it deterministically.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        from hermes_cli import profiles as profiles_mod
        from utils import atomic_replace, atomic_roundtrip_yaml_update

        catalog_profiles = _lane_profile_catalog()
        known_profiles = {p["name"] for p in catalog_profiles}
        active_lane_for_catalog = kanban_db.get_active_lane(conn)
        models = _lane_model_catalog(catalog_profiles, active_lane_for_catalog)
        known_models = {m["id"] for m in models}

        unknown_profiles = [name for name in payload.profiles if name not in known_profiles]
        if unknown_profiles:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown profiles", "profiles": unknown_profiles},
            )

        bad_models: list[dict[str, str]] = []
        bad_runtime_models: list[dict[str, str]] = []
        bad_claude_providers: list[dict[str, str]] = []
        for name, entry in payload.profiles.items():
            if entry.model not in known_models:
                bad_models.append({"profile": name, "model": entry.model})
                continue
            model_runtime = _lane_model_runtime(entry.model, catalog_profiles, models)
            if model_runtime and model_runtime != entry.worker_runtime:
                bad_runtime_models.append(
                    {
                        "profile": name,
                        "model": entry.model,
                        "expected_runtime": model_runtime,
                        "worker_runtime": entry.worker_runtime,
                    }
                )
            if entry.worker_runtime == "claude-cli" and entry.provider:
                bad_claude_providers.append({"profile": name, "provider": str(entry.provider)})
        if bad_models:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown models", "models": bad_models},
            )
        if bad_runtime_models:
            raise HTTPException(
                status_code=400,
                detail={"error": "model runtime mismatch", "models": bad_runtime_models},
            )
        if bad_claude_providers:
            raise HTTPException(
                status_code=400,
                detail={"error": "claude-cli provider must be empty", "profiles": bad_claude_providers},
            )

        lanes = kanban_db.list_lanes(conn)
        active_id = next((lane["id"] for lane in lanes if lane["active"]), None)
        active_lane = next((lane for lane in lanes if lane["id"] == active_id), None)
        active_profiles = (active_lane or {}).get("profiles") or {}

        config_snapshots: dict[str, tuple[Path, bool, bytes, int | None]] = {}
        lane_profiles: dict[str, dict[str, Any]] = {}
        for name, entry in payload.profiles.items():
            canon = profiles_mod.normalize_profile_name(name)
            config_path = profiles_mod.get_profile_dir(canon) / "config.yaml"
            exists = config_path.exists()
            config_snapshots[name] = (
                config_path,
                exists,
                config_path.read_bytes() if exists else b"",
                config_path.stat().st_mode if exists else None,
            )
            existing = active_profiles.get(name) or {}
            fallback_rows = (
                existing.get("fallback_providers") or []
                if entry.fallback_providers is None
                else [row.model_dump() for row in entry.fallback_providers]
            )
            lane_profiles[name] = {
                "worker_runtime": entry.worker_runtime,
                "provider": (
                    None
                    if entry.worker_runtime == "claude-cli"
                    else (entry.provider or existing.get("provider"))
                ),
                "model": entry.model,
                "fallback_providers": fallback_rows,
            }

        def rollback_profile_configs() -> list[str]:
            errors: list[str] = []
            for profile_name, (config_path, existed, contents, mode) in config_snapshots.items():
                try:
                    if not existed:
                        config_path.unlink(missing_ok=True)
                        continue
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".{config_path.name}.lane-rollback-",
                        dir=str(config_path.parent),
                    )
                    tmp_path = Path(tmp_name)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(contents)
                            handle.flush()
                            os.fsync(handle.fileno())
                        if mode is not None:
                            os.chmod(tmp_path, mode)
                        atomic_replace(tmp_path, config_path)
                    finally:
                        tmp_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    log.exception("lanes/persist: rollback failed for %s", profile_name)
                    errors.append(f"{profile_name}: {rollback_exc}")
            return errors

        failed_profile = "__active_lane__"
        try:
            for name, entry in payload.profiles.items():
                failed_profile = name
                config_path = config_snapshots[name][0]
                if entry.worker_runtime == "claude-cli":
                    atomic_roundtrip_yaml_update(config_path, "claude_model", entry.model)
                    atomic_roundtrip_yaml_update(config_path, "worker_runtime", "claude-cli")
                else:
                    atomic_roundtrip_yaml_update(config_path, "model.default", entry.model)
                    # An absent provider preserves an operator-pinned provider.
                    if entry.provider:
                        atomic_roundtrip_yaml_update(config_path, "model.provider", entry.provider)
                    atomic_roundtrip_yaml_update(config_path, "worker_runtime", "hermes")
                if entry.fallback_providers is not None:
                    atomic_roundtrip_yaml_update(
                        config_path,
                        "fallback_providers",
                        [row.model_dump() for row in entry.fallback_providers],
                    )

            failed_profile = "__active_lane__"
            if lane_profiles and active_id is not None and active_lane is not None:
                merged_profiles = dict(active_profiles)
                merged_profiles.update(lane_profiles)
                kanban_db.update_lane(conn, active_id, profiles=merged_profiles)
        except Exception as exc:
            log.exception("lanes/persist: transaction failed at %s", failed_profile)
            rollback_errors = rollback_profile_configs()
            if rollback_errors:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "lane persist rollback failed",
                        "cause": str(exc),
                        "rollback_errors": rollback_errors,
                    },
                ) from exc
            return {
                "written": [],
                "failed": [
                    {
                        "profile": failed_profile,
                        "error": f"{exc}; transaction rolled back",
                    },
                ],
                "lanes": kanban_db.list_lanes(conn),
                "active_id": active_id,
            }

        return {
            "written": list(payload.profiles),
            "failed": [],
            "lanes": kanban_db.list_lanes(conn),
            "active_id": active_id,
        }
    finally:
        conn.close()


@observability_routes.get("/runs/summary")
def get_runs_summary(
    since_hours: int = Query(24, ge=1, le=720),
    board: Optional[str] = Query(None),
):
    """K7: root-grouped run summary (throughput / cost / cycle-time + recent
    roots) over the last ``since_hours``. Powers the RunSummaryTile.

    Registered BEFORE ``/runs/{run_id}`` so the literal ``summary`` segment
    isn't captured as a run id by the path-param route.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_summary(conn, since_hours=since_hours)
    finally:
        conn.close()


@observability_routes.get("/runs/reliability")
def get_runs_reliability(
    since_hours: int = Query(168, ge=1, le=24 * 90),
    baseline_hours: int = Query(720, ge=1, le=24 * 180),
    min_n: int = Query(5, ge=1, le=100),
    board: Optional[str] = Query(None),
):
    """Phase 3 (Statistik): per-profile reliability — outcome rates, retry
    quote and verifier verdicts attributed to the judged run — over a rolling
    window (default 7 d) plus a 30 d baseline. ``min_n`` mirrors the
    roster-stats damping (approve_rate is None below the threshold).

    Registered BEFORE ``/runs/{run_id}`` so the literal segment isn't
    captured as a run id.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_reliability(
            conn, since_hours=since_hours, baseline_hours=baseline_hours, min_n=min_n,
        )
    finally:
        conn.close()


PlanSpecText = Annotated[str, Field(max_length=60_000)]


class FunnelDraftEditBody(BaseModel):
    draft_text: PlanSpecText
    operator_note: Optional[FreeText] = ""


class DismissDispositionBody(BaseModel):
    reason: str = ""


@control_routes.get("/funnel/drafts")
def get_funnel_drafts(
    days: int = Query(30, ge=1, le=365),
    board: Optional[str] = Query(None),
):
    """Demand-Funnel Freigabe-Queue: fertige Funnel-Roots (family /
    discord-idee / fo-gap-audit) ohne Build-Kind — also Drafts, die auf den
    Operator-Klick warten. Nach der Freigabe (Build-Kind verlinkt) fallen
    sie aus der Liste; die Kette übernimmt das Flow-Board."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return {"drafts": kanban_funnel.list_drafts(conn, days=days)}
    finally:
        conn.close()


@control_routes.patch("/funnel/drafts/{task_id}")
def update_funnel_draft(task_id: str, body: FunnelDraftEditBody, board: Optional[str] = Query(None)):
    """Speichert eine Operator-bearbeitete Plan-Spec als kanonischen Draft."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            draft = kanban_funnel.save_draft_edit(
                conn,
                task_id,
                draft_text=body.draft_text,
                operator_note=body.operator_note or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"draft": draft}
    finally:
        conn.close()


@control_routes.post("/funnel/drafts/{task_id}/revise")
def revise_funnel_draft(task_id: str, body: FunnelDraftEditBody, board: Optional[str] = Query(None)):
    """Schickt einen Funnel-Draft mit Operator-Input zurück in den Spec-Loop."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            new_id = kanban_funnel.request_revision(
                conn,
                task_id,
                draft_text=body.draft_text,
                operator_note=body.operator_note or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"task": _task_dict(kanban_db.get_task(conn, new_id)), "superseded": task_id}
    finally:
        conn.close()


@control_routes.post("/funnel/drafts/{task_id}/approve")
def approve_funnel_draft(task_id: str, board: Optional[str] = Query(None)):
    """Freigabe eines Funnel-Drafts: legt den Build-Task als verlinktes Kind
    an (erbt created_by → Wert-Bilanz zählt die Kette einmal als nutzer;
    Parent ist done → Kind startet ready, der Dispatcher übernimmt)."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            new_id = kanban_funnel.approve_draft(conn, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"task": _task_dict(kanban_db.get_task(conn, new_id))}
    finally:
        conn.close()


@control_routes.post("/funnel/drafts/{task_id}/dismiss")
def dismiss_funnel_draft(task_id: str, board: Optional[str] = Query(None)):
    """Verwerfen eines Funnel-Drafts: archiviert den Root (mit Kommentar) —
    der Wunsch wird NICHT gebaut und fällt aus der Freigabe-Queue."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            kanban_funnel.dismiss_draft(conn, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "task_id": task_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Disposition-item lifecycle (FRD Phase 3b) — inbox actions for the operator.
#
# Operator can list open items, accept, dismiss with a reason, or promote to a
# parked (triage) fix-task. No auto-dispatch: the created task stays in triage
# until the operator or a subsequent flow releases it.
# ---------------------------------------------------------------------------


@control_routes.get("/disposition-items")
def get_disposition_items(
    status: str = Query("open"),
    board: Optional[str] = Query(None),
):
    """List disposition-ledger items filtered by status.

    Pass ``status=all`` to retrieve every item regardless of lifecycle state.
    Default is ``open`` (items awaiting operator decision).
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        effective_status: Optional[str] = None if status == "all" else status
        return {"items": kanban_db.list_disposition_items(conn, status=effective_status)}
    finally:
        conn.close()


@control_routes.post("/disposition-items/{item_id}/accept")
def accept_disposition_item(item_id: str, board: Optional[str] = Query(None)):
    """Mark a disposition-ledger item as accepted by the operator."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        updated = kanban_db.set_disposition_status(
            conn, item_id, status="accepted", decided_by="operator"
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"disposition item {item_id!r} not found")
        return {"item": updated}
    finally:
        conn.close()


@control_routes.post("/disposition-items/{item_id}/dismiss")
def dismiss_disposition_item(
    item_id: str,
    body: DismissDispositionBody,
    board: Optional[str] = Query(None),
):
    """Dismiss a disposition-ledger item, optionally recording a reason.

    The reason (if non-empty) is appended as a comment to the source task.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        updated = kanban_db.dismiss_disposition_item(conn, item_id, reason=body.reason)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"disposition item {item_id!r} not found")
        return {"item": updated}
    finally:
        conn.close()


@control_routes.post("/disposition-items/{item_id}/create-fix-task")
def create_fix_task_from_disposition(item_id: str, board: Optional[str] = Query(None)):
    """Create a parked fix-task from an open disposition-ledger item.

    The resulting task lands in ``triage`` status (no auto-dispatch). The
    disposition item transitions to ``task_created``. Idempotent: a second
    call returns the same fix-task rather than creating a duplicate.

    Returns 404 if the item does not exist; 409 if the item is not open.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            result = kanban_db.create_fix_task_from_disposition(conn, item_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail=f"disposition item {item_id!r} not found")
        return {"fix_task": result["fix_task"], "item": result["item"]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Strategist surface (G1) — the dedicated Vision-Flywheel proposal tray.
#
# Distinct from the Demand-Funnel queue above: those are user *wishes* awaiting
# a build; these are the *strategist-cron's* self-gated, ROI-annotated PlanSpecs
# ingested with ``freigabe: operator`` so they land held (root parked in
# ``scheduled``, F1) for fast operator triage — approve releases the chain,
# veto archives it. Only roots carry ``freigabe`` so the list is root-guarded.
# ---------------------------------------------------------------------------


@control_routes.get("/strategist/proposals")
def get_strategist_proposals(request: Request, board: Optional[str] = Query(None)):
    """List held ``freigabe: operator`` proposals + the current metric snapshot.

    Each proposal carries its Ziel-Kennzahl / ROI / Counter-Metrik (parsed from
    the strategist-stamped root body, ``None`` when absent) and the number of
    held subtasks it would dispatch on approval. Each also carries ``held_since``
    (the root's ``created_at``) and ``age_seconds`` (computed fresh per request,
    excluded from the ETag so the poll's 304 revalidation still fires as time
    passes); ``oldest_age_seconds`` is the max across all proposals, ``None`` when
    the list is empty. ``metrics`` is the distilled Vision snapshot (H1,
    ``vision-metrics.json``) as triage context, or ``None`` when no snapshot has
    been written yet. A weak ETag lets the SPA's poll revalidate to a 304 while
    nothing changed — consistent with the board tab.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        proposals = strategist_surface.held_operator_proposals(conn)
    finally:
        conn.close()
    metrics = strategist_surface.read_vision_metrics()
    payload: dict[str, Any] = {
        "proposals": proposals,
        "count": len(proposals),
        "metrics": metrics,
    }
    etag = 'W/"' + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:32] + '"'
    # age_seconds/oldest_age_seconds grow every second even when nothing about
    # the held proposals changed — added after the ETag hash (like checked_at)
    # so the poll's 304 revalidation keeps working as real time moves on.
    now = int(time.time())
    ages = [max(0, now - p["held_since"]) for p in proposals]
    for proposal, age in zip(proposals, ages):
        proposal["age_seconds"] = age
    payload["oldest_age_seconds"] = max(ages, default=None)
    payload["checked_at"] = now
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=http_status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


@control_routes.get("/strategist/disposition-digest")
def get_strategist_disposition_digest(request: Request):
    """Read-only: the current disposition digest (A3) or ``null`` when none yet.

    The digest is the Sonnet harvest step's clustering decision persisted to
    ``disposition_digest.json``: how many open follow-ups were triaged
    (``total_open``), how many reaped into PlanSpec proposals (``reaped``), the
    thematic ``clusters`` (each with ``theme``/``item_ids``/``severity``/
    ``recommendation`` drop|collect|planspec + optional ``planspec_key``) and the
    ``left`` list of consciously left/discarded items. ``digest`` is ``None``
    when no harvest has written one yet — consistent with the proposals
    endpoint's ``metrics: null``, so the SPA never sees a 404 on a fresh install.
    A weak ETag lets the poll revalidate to 304 while the file is unchanged."""
    digest = strategist_surface.read_disposition_digest()
    payload: dict[str, Any] = {"digest": digest}
    etag = 'W/"' + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:32] + '"'
    payload["checked_at"] = int(time.time())
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=http_status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


@control_routes.post("/strategist/proposals/{task_id}/approve")
def approve_strategist_proposal(task_id: str, board: Optional[str] = Query(None)):
    """Approve a held proposal → release the chain (held → ready/todo).

    Wraps :func:`kanban_db.release_freigabe_hold` (F1). Returns 409 when
    ``task_id`` is not a held ``freigabe: operator`` root (root-guard: a build
    child or an already-built/unknown task), so the veto/approve buttons only
    ever act on real proposals."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        released = kanban_db.release_freigabe_hold(conn, task_id, author="operator")
        if not released:
            raise HTTPException(
                status_code=409,
                detail=f"{task_id} ist kein freigebbarer freigabe:operator-Root",
            )
        return {"ok": True, "task_id": task_id, "released": True}
    finally:
        conn.close()


@control_routes.post("/strategist/proposals/{task_id}/veto")
def veto_strategist_proposal(task_id: str, board: Optional[str] = Query(None)):
    """Veto a held proposal → archive the chain (nothing builds).

    Wraps :func:`kanban_db.dismiss_freigabe_hold` (G1). Same root-guard as
    approve: 409 unless ``task_id`` is a held ``freigabe: operator`` root."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        vetoed = kanban_db.dismiss_freigabe_hold(conn, task_id, author="operator")
        if not vetoed:
            raise HTTPException(
                status_code=409,
                detail=f"{task_id} ist kein verwerfbarer freigabe:operator-Root",
            )
        return {"ok": True, "task_id": task_id, "vetoed": True}
    finally:
        conn.close()


class CompleteFreigabeBody(BaseModel):
    note: FreeText


@control_routes.post("/strategist/proposals/{task_id}/complete")
def complete_strategist_proposal(
    task_id: str, body: CompleteFreigabeBody, board: Optional[str] = Query(None),
):
    """Close a held proposal as done-elsewhere → archive the chain (the third
    disposition sibling of approve/veto: the work was done outside the
    chain, e.g. an operator-requested direct review).

    Wraps :func:`kanban_db.complete_freigabe_hold`. Same root-guard as
    approve/veto: 409 unless ``task_id`` is a held ``freigabe: operator``
    root. ``note`` (mandatory) records why the chain closed without
    building and is stored as a task comment."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            completed = kanban_db.complete_freigabe_hold(
                conn, task_id, author="operator", note=body.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not completed:
            raise HTTPException(
                status_code=409,
                detail=f"{task_id} ist kein schließbarer freigabe:operator-Root",
            )
        return {"ok": True, "task_id": task_id, "completed": True}
    finally:
        conn.close()


@control_routes.post("/tasks/{task_id}/veto-escalation")
def veto_operator_escalation_route(task_id: str, board: Optional[str] = Query(None)):
    """Veto an Autoresearch operator-escalation → archive it AND record the
    veto so the strategist's reflect learns to suppress the signal (Naht 3).

    Wraps :func:`kanban_db.veto_operator_escalation`. Source-guard: 409 unless
    *task_id* is a blocked escalation whose ``operator_escalation`` payload has
    ``source="autoresearch"`` — a stalled-worker block is not vetoable here."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        vetoed = kanban_db.veto_operator_escalation(conn, task_id, author="operator")
        if not vetoed:
            raise HTTPException(
                status_code=409,
                detail=f"{task_id} ist keine verwerfbare Autoresearch-Eskalation",
            )
        return {"ok": True, "task_id": task_id, "vetoed": True}
    finally:
        conn.close()


# ── Manuelle Trigger: Stratege (propose) + Gutachter (Bewerter) ──────────────
# Zwei operator-getriggerte Jobs für die /control-Buttons. Auth läuft über die
# globale ``auth_middleware`` (Worker ohne Session-Token können NICHT triggern).
# Detached gespawnt (Haus-Muster ``autoresearch_view._spawn_runner``). Propose
# nutzt weiter den flock-geschützten Runtime-Wrapper; harvest-watch nutzt den
# repo-seitig vorhandenen CLI-Callable, damit der Dashboard-Button nicht von
# einem Runtime-Wrapper-Modus abhängt, der ggf. nicht installiert ist.
_TRIGGER_LOG_DIR = os.path.expanduser("~/.hermes/logs/manual-triggers")
_STRATEGIST_CRON = os.path.expanduser("~/.hermes/scripts/strategist-cron.sh")
_TRIGGER_SPECS: dict[str, dict[str, Any]] = {
    "strategist-propose": {
        "argv": ["bash", _STRATEGIST_CRON, "propose"],
        "log": os.path.join(_TRIGGER_LOG_DIR, "strategist-propose.log"),
        "env": {},
    },
    "strategist-harvest-watch": {
        "argv": ["hermes", "vision", "strategist", "--mode", "harvest-watch"],
        "log": os.path.join(_TRIGGER_LOG_DIR, "strategist-harvest-watch.log"),
        "env": {},
    },
    "gutachter": {
        # Phase-A live: kommentiert am Vorschlag + Discord, KEIN dispatchbarer Task.
        "argv": ["bash", os.path.expanduser("~/agents/stratege-gutachter/run.sh")],
        "log": os.path.join(_TRIGGER_LOG_DIR, "gutachter.log"),
        "env": {"DELIVER_MODE": "live"},
    },
}
_TRIGGER_PROCS: dict[str, Any] = {}


def _trigger_env(extra: dict[str, str]) -> dict[str, str]:
    """Inherit the server env but guarantee hermes/claude/bash resolve on PATH
    (the dashboard service's PATH may be minimal vs. the cron unit's)."""
    env = os.environ.copy()
    prefix = [
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]
    env["PATH"] = ":".join(prefix + ([env["PATH"]] if env.get("PATH") else []))
    env.update(extra)
    return env


def _spawn_trigger(name: str):
    """Spawn a trigger job detached → its Popen, or None if one already runs."""
    import subprocess

    spec = _TRIGGER_SPECS[name]
    proc = _TRIGGER_PROCS.get(name)
    if proc is not None and proc.poll() is None:
        return None
    os.makedirs(_TRIGGER_LOG_DIR, exist_ok=True)
    logf = open(spec["log"], "ab", buffering=0)  # noqa: SIM115 (lebt für die Laufzeit des Jobs)
    p = subprocess.Popen(
        spec["argv"],
        stdout=logf,
        stderr=logf,
        start_new_session=True,
        env=_trigger_env(spec.get("env") or {}),
    )
    _TRIGGER_PROCS[name] = p
    return p


def _trigger_status(name: str) -> dict[str, Any]:
    proc = _TRIGGER_PROCS.get(name)
    running = proc is not None and proc.poll() is None
    exit_code = None if (proc is None or running) else proc.returncode
    log = _TRIGGER_SPECS[name]["log"]
    tail: list[str] = []
    last_modified: Optional[int] = None
    try:
        last_modified = int(os.path.getmtime(log))
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.read().splitlines()[-12:]
    except OSError:
        pass
    return {"running": running, "exit_code": exit_code,
            "last_modified": last_modified, "tail": tail}


@control_routes.post("/strategist/run-propose")
def run_strategist_propose():
    """Stratege-propose manuell anstoßen (derselbe Wrapper wie der 06:00-Timer)."""
    p = _spawn_trigger("strategist-propose")
    if p is None:
        return {"ok": False, "running": True, "detail": "Strategen-Lauf läuft bereits"}
    return {"ok": True, "name": "strategist-propose", "pid": p.pid}


@control_routes.post("/strategist/run-harvest-watch")
def run_strategist_harvest_watch():
    """Harvest-watch manuell über den Repo-CLI-Callable anstoßen."""
    p = _spawn_trigger("strategist-harvest-watch")
    if p is None:
        return {"ok": False, "running": True, "detail": "Harvest-watch-Lauf läuft bereits"}
    return {"ok": True, "name": "strategist-harvest-watch", "pid": p.pid}


@control_routes.post("/strategist/run-gutachter")
def run_gutachter():
    """Bewerter (stratege-gutachter) manuell anstoßen — Phase-A live (Kommentar+Discord)."""
    p = _spawn_trigger("gutachter")
    if p is None:
        return {"ok": False, "running": True, "detail": "Gutachter-Lauf läuft bereits"}
    return {"ok": True, "name": "gutachter", "pid": p.pid}


@control_routes.get("/strategist/run-status")
def strategist_run_status():
    """Running / letzter-Lauf-Status der manuellen Trigger (Button-Feedback)."""
    return {
        "propose": _trigger_status("strategist-propose"),
        "harvest_watch": _trigger_status("strategist-harvest-watch"),
        "gutachter": _trigger_status("gutachter"),
    }


@control_routes.get("/strategist/last-runs")
def strategist_last_runs() -> dict:
    """Jüngster Harvest- und Propose-Lauf aus der run-history.jsonl."""
    from hermes_cli import strategist

    return strategist.read_last_runs(strategist.default_state_dir())


@control_routes.get("/strategist/outcomes")
def get_strategist_outcomes(limit: int = Query(20, ge=1, le=200)) -> dict:
    """Wirkungs-Historie geshippter Lever (Ziel-2 ``lever-outcomes.json``).

    Auftrag → Wirkung rückverfolgbar: reads the outcome records the strategist
    reflect step measures and appends to, newest ``proposed_at`` first, capped
    at ``limit`` (default 20). Records are returned unmodified — same schema
    the writer persists (``lever_key``/``root_task_id``/``proposed_at``/
    ``baseline``/``metric_key``/``shipped_at``/``measured_at``/``current``/
    ``delta``/``verdict``/``status``). Empty list when no ledger has been
    written yet, never a 404 — consistent with the proposals/digest surfaces."""
    outcomes = strategist_surface.read_lever_outcomes(limit=limit)
    return {"outcomes": outcomes, "generated_at": int(time.time())}


@observability_routes.get("/runs/daily")
def get_runs_daily(
    days: int = Query(30, ge=1, le=365),
    board: Optional[str] = Query(None),
):
    """Phase 3 (Statistik): daily time series — delivered roots/tasks,
    cycle-time p50, cost burn and run outcomes per local calendar day.
    Empty days are included so charts get a continuous axis.

    Registered BEFORE ``/runs/{run_id}`` so the literal segment isn't
    captured as a run id.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_daily(conn, days=days)
    finally:
        conn.close()


@observability_routes.get("/runs/failures")
def get_runs_failures(
    hours: int = Query(48, ge=1, le=24 * 14),
    limit: int = Query(30, ge=1, le=100),
    board: Optional[str] = Query(None),
):
    """Phase F: triage strip — latest failed/blocked run per task (last
    ``hours``), only for tasks still awaiting operator action. Read-only;
    the actions themselves go through PATCH /tasks/{id} (status/model_override).

    Registered BEFORE ``/runs/{run_id}`` so the literal segment isn't
    captured as a run id.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_failures(conn, hours=hours, limit=limit)
    finally:
        conn.close()


@observability_routes.get("/runs/issues")
def get_runs_issues(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    board: Optional[str] = Query(None),
):
    """F6: recurring failures grouped by (profile, normalized error
    signature) over failed/blocked runs of the last ``days`` days. Read-only —
    no auto-task creation, no AI clustering.

    Registered BEFORE ``/runs/{run_id}`` so the literal segment isn't
    captured as a run id.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_issues(conn, days=days, limit=limit)
    finally:
        conn.close()


@observability_routes.get("/runs/costs")
def get_runs_costs(
    days: int = Query(7, ge=1, le=90),
    board: Optional[str] = Query(None),
):
    """F4 (Statistik): cost view — today + rolling window totals and a
    per-profile breakdown over the window. Reads only stamped task_runs
    columns plus ``metadata.cost_usd_equivalent`` (K17 subscription lanes
    carry an honest $0 in ``cost_usd``).

    Registered BEFORE ``/runs/{run_id}`` so the literal segment isn't
    captured as a run id.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_costs(conn, days=days)
    finally:
        conn.close()


@observability_routes.get("/runs/costs-series")
def get_runs_costs_series(
    days: int = Query(7, ge=1, le=90),
    board: Optional[str] = Query(None),
):
    """F4 (Statistik): per-day cost/token trend. Read-only, additive."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_costs_series(conn, days=days)
    finally:
        conn.close()


@observability_routes.get("/runs/subscription-burn")
def get_runs_subscription_burn(
    days: int = Query(7, ge=1, le=90),
    board: Optional[str] = Query(None),
):
    """Read-only Abo-Token-Burn by subscription lane, value class, and day."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.subscription_token_burn(conn, days=days)
    finally:
        conn.close()


@observability_routes.get("/runs/windowed-rollup")
def get_runs_windowed_rollup(
    hours: int = Query(24 * 7, ge=1, le=24 * 90),
    limit: int = Query(20, ge=1, le=100),
    board: Optional[str] = Query(None),
):
    """S1: root→worker→runner rollup over completed sink tasks.

    Registered BEFORE ``/runs/{run_id}`` so the literal segment isn't
    captured as a run id.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.runs_windowed_rollup(
            conn, since_hours=hours, max_roots=limit, board=board
        )
    finally:
        conn.close()


@observability_routes.get("/runs/recent-results")
def list_recent_results(
    limit: int = Query(12, ge=1, description="Maximum completed runs to return (capped at 50)"),
    since_hours: int = Query(48, ge=1, le=24 * 30, description="Lookback window in hours"),
    outcome: str = Query("completed", description="task_runs.outcome filter"),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return recent completed worker results for the Hermes tab.

    This intentionally does not overload /workers/active: active workers stay
    fast and narrow, while completed summaries/artifacts/followups are served
    through this read-only history endpoint.
    """
    board = _resolve_board(board)
    capped_limit = max(1, min(int(limit), 50))
    cutoff = int(time.time()) - int(since_hours) * 3600
    conn = _conn(board=board)
    try:
        rows = conn.execute(
            """
            SELECT
                r.id AS run_id,
                r.task_id,
                t.title AS task_title,
                t.status AS task_status,
                t.assignee AS task_assignee,
                r.profile,
                r.status AS run_status,
                r.outcome AS run_outcome,
                r.started_at,
                r.ended_at,
                r.summary,
                r.metadata
            FROM task_runs r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.ended_at IS NOT NULL
              AND r.ended_at >= ?
              AND r.outcome = ?
            ORDER BY r.ended_at DESC, r.id DESC
            LIMIT ?
            """,
            (cutoff, outcome, capped_limit),
        ).fetchall()
        results = [_recent_result_dict(conn, row) for row in rows]
        return {
            "results": results,
            "count": len(results),
            "checked_at": int(time.time()),
            "limit": capped_limit,
            "since_hours": int(since_hours),
            "outcome": outcome,
        }
    finally:
        conn.close()


@observability_routes.get("/runs/today-digest")
def list_today_digest(
    limit: int = Query(12, ge=1, description="Maximum digest items to return (capped at 50)"),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return today's human-readable outcome digest for /control.

    The digest is a scanner-friendly projection of completed task_runs since
    local midnight: summary, primary preserved deliverable + safe text excerpt,
    and the verifier/gate state in one row.
    """
    board = _resolve_board(board)
    capped_limit = max(1, min(int(limit), 50))
    day_start = _local_day_start()
    conn = _conn(board=board)
    try:
        rows = conn.execute(
            """
            SELECT
                r.id AS run_id,
                r.task_id,
                t.title AS task_title,
                t.status AS task_status,
                t.assignee AS task_assignee,
                r.profile,
                r.status AS run_status,
                r.outcome AS run_outcome,
                r.started_at,
                r.ended_at,
                r.summary,
                r.metadata
            FROM task_runs r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.ended_at IS NOT NULL
              AND r.ended_at >= ?
              AND r.outcome = 'completed'
            ORDER BY r.ended_at DESC, r.id DESC
            LIMIT ?
            """,
            (day_start, capped_limit),
        ).fetchall()
        items = [_today_digest_item(conn, row) for row in rows]
        return {
            "schema": "kanban-today-digest-v1",
            "items": items,
            "count": len(items),
            "checked_at": int(time.time()),
            "day_start": day_start,
            "timezone": "local",
            "limit": capped_limit,
        }
    finally:
        conn.close()


@observability_routes.get("/runs/blocked-completions")
def list_blocked_completions(
    since_hours: int = Query(48, ge=1, le=24 * 30, description="Lookback window in hours"),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return recent hallucination-blocked / advisory completion events.

    Sibling of ``/runs/recent-results``: where that endpoint shows
    successful worker handoffs, this surfaces completions the
    anti-hallucination gate *refused* (``completion_blocked_hallucination``)
    plus the advisory prose-scan warnings (``suspected_hallucinated_references``)
    so the operator sees them without querying the DB.
    """
    board = _resolve_board(board)
    cutoff = int(time.time()) - int(since_hours) * 3600
    conn = _conn(board=board)
    try:
        placeholders = ", ".join("?" for _ in _WARNING_EVENT_KINDS)
        rows = conn.execute(
            f"""
            SELECT
                e.id AS event_id,
                e.task_id,
                t.title AS task_title,
                t.status AS task_status,
                t.assignee,
                e.kind,
                e.payload,
                e.created_at
            FROM task_events e
            JOIN tasks t ON t.id = e.task_id
            WHERE e.kind IN ({placeholders})
              AND e.created_at >= ?
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 50
            """,
            (*_WARNING_EVENT_KINDS, cutoff),
        ).fetchall()
        blocked = [_blocked_completion_dict(row) for row in rows]

        rejection_rows = conn.execute(
            """
            SELECT
                r.id AS run_id,
                r.task_id,
                t.title AS task_title,
                t.status AS task_status,
                t.assignee,
                r.profile,
                r.started_at,
                r.ended_at,
                r.summary,
                r.metadata
            FROM task_runs r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.ended_at IS NOT NULL
              AND r.ended_at >= ?
              AND (
                    r.profile = 'verifier'
                 OR UPPER(COALESCE(r.summary, '')) LIKE '%REQUEST_CHANGES%'
                 OR UPPER(COALESCE(r.summary, '')) LIKE '%NEEDS_REVISION%'
                 OR UPPER(COALESCE(r.metadata, '')) LIKE '%REQUEST_CHANGES%'
                 OR UPPER(COALESCE(r.metadata, '')) LIKE '%NEEDS_REVISION%'
              )
            ORDER BY r.ended_at DESC, r.id DESC
            LIMIT 50
            """,
            (cutoff,),
        ).fetchall()
        for row in rejection_rows:
            metadata = _load_result_metadata(row["metadata"])
            verdict = _normalize_verifier_verdict(row["summary"] or "", metadata)
            if _is_verifier_rejection_run(conn, row, verdict):
                blocked.append(_verifier_rejection_dict(conn, row))
        blocked = sorted(blocked, key=lambda item: int(item.get("created_at") or 0), reverse=True)[:50]
        return {
            "blocked": blocked,
            "count": len(blocked),
            "checked_at": int(time.time()),
            "since_hours": int(since_hours),
        }
    finally:
        conn.close()


@core_routes.get("/runs/{run_id}")
def get_run_endpoint(
    run_id: int,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Direct lookup of a ``task_runs`` row by its integer id.

    Returns ``{run: {...}}`` using the same serialisation as the
    per-task run history embedded in ``GET /tasks/{task_id}``.
    404 when no such run exists.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        r = kanban_db.get_run(conn, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return {"run": _run_dict(conn, r)}
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


@core_routes.get("/runs/{run_id}/inspect")
def inspect_run_endpoint(
    run_id: int,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Live PID stats for a run's worker process via psutil.

    If the run has already ended, or has no recorded ``worker_pid``,
    returns ``{alive: false}`` with a human-readable ``reason``.

    When the process is live, returns CPU, memory, thread count, fd count,
    status, create_time, and cmdline.  ``access_denied`` is set when the
    OS refuses inspection rather than raising a 500.

    psutil availability: if psutil is not installed the endpoint still
    works but ``alive`` is always returned as ``false`` with
    ``reason="psutil not available"``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        r = kanban_db.get_run(conn, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    finally:
        conn.close()

    if r.ended_at is not None:
        return {"run_id": run_id, "alive": False, "reason": "run already ended"}
    if r.worker_pid is None:
        return {"run_id": run_id, "alive": False, "reason": "no worker_pid recorded"}

    pid = r.worker_pid

    if _psutil is None:
        return {"run_id": run_id, "alive": False, "pid": pid, "reason": "psutil not available"}

    try:
        proc = _psutil.Process(pid)
        info = proc.as_dict(attrs=[
            "cpu_percent", "memory_info", "num_threads",
            "status", "create_time", "cmdline",
        ])
        # num_fds is POSIX-only; skip gracefully on Windows.
        try:
            num_fds = proc.num_fds()
        except AttributeError:
            num_fds = None
        mem = info.get("memory_info")
        return {
            "run_id": run_id,
            "alive": True,
            "pid": pid,
            "cpu_percent": info.get("cpu_percent"),
            "memory_rss_bytes": mem.rss if mem else None,
            "memory_vms_bytes": mem.vms if mem else None,
            "num_threads": info.get("num_threads"),
            "num_fds": num_fds,
            "status": info.get("status"),
            "create_time": info.get("create_time"),
            "cmdline": info.get("cmdline"),
        }
    except _psutil.NoSuchProcess:
        return {"run_id": run_id, "alive": False, "pid": pid, "reason": "process not found"}
    except _psutil.AccessDenied:
        return {"run_id": run_id, "alive": True, "pid": pid, "error": "access denied"}


class TerminateRunBody(BaseModel):
    reason: Optional[FreeText] = None


@core_routes.post("/runs/{run_id}/terminate")
def terminate_run_endpoint(
    run_id: int,
    payload: TerminateRunBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Terminate the worker process backing an in-flight run.

    Resolves ``run_id`` to its parent ``task_id`` and routes through
    :func:`kanban_db.reclaim_task` so the SIGTERM->SIGKILL flow,
    run-outcome bookkeeping, and event-log append all match what the
    existing ``POST /tasks/{task_id}/reclaim`` endpoint does.

    Responses:
      * 200 ``{"ok": true, "run_id": ..., "task_id": ...}`` on success.
      * 404 when ``run_id`` is unknown.
      * 409 when the run has already ended, or the task is no longer in
        a claimable state.

    Closes the gap left by PR #28432, which shipped the read-only
    sibling endpoints (``/workers/active``, ``/runs/{run_id}``,
    ``/runs/{run_id}/inspect``) but no termination control surface.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        r = kanban_db.get_run(conn, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        if r.ended_at is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} already ended",
            )
        ok = kanban_db.reclaim_task(conn, r.task_id, reason=payload.reason)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot terminate run {run_id}: task {r.task_id} is no "
                    "longer in a reclaimable state"
                ),
            )
        return {"ok": True, "run_id": run_id, "task_id": r.task_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Control dashboard worker actions (C2) — operator recovery from the worker
# card. Every action maps onto an existing, proven kanban primitive; no new
# claim or dispatch logic is introduced here.
# ---------------------------------------------------------------------------
class WorkerActionBody(BaseModel):
    action: ShortText = Field(..., description="unlock | nudge | restart | dispatch | hold | resume")
    confirm: bool = False
    reason: Optional[FreeText] = None
    # B4: optional overrides applied on restart
    model_override: Optional[ShortText] = None
    assignee: Optional[ShortText] = None


_WORKER_ACTIONS = {"unlock", "nudge", "restart", "dispatch", "hold", "resume"}


@control_routes.post("/workers/{run_id}/action")
def worker_action_endpoint(
    run_id: int,
    payload: WorkerActionBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Operator worker-recovery actions for the Control dashboard (C2).

    * ``unlock``   – release a hung claim so the task is re-claimable
                     (``reclaim_task``: terminates the worker, task → ready).
    * ``nudge``    – soft, no-kill operator ping: appends a visible comment to
                     the task. Lowest-impact, fully reversible.
    * ``restart``  – reclaim the run, then run one dispatcher tick so it is
                     re-picked (``reclaim_task`` + ``dispatch_once``). No
                     gateway restart from here — that stays a separate,
                     deliberate action.
    * ``dispatch`` – run one dispatcher tick to pick up ready work
                     (``dispatch_once``).

    Mutating actions require ``confirm: true`` (the dashboard confirm dialog).
    Returns a structured ``{ok, action, detail, run_id, task_id}``; a guard
    refusal is ``ok: false`` at HTTP 200 so the UI shows the reason inline
    rather than throwing.
    """
    action = (payload.action or "").strip().lower()
    if action not in _WORKER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown worker action: {payload.action!r}")
    if not payload.confirm:
        return {"ok": False, "action": action, "run_id": run_id, "detail": "confirm required"}

    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if action == "dispatch":
            result = kanban_db.dispatch_once(
                conn,
                board=board,
                **_dispatch_kwargs_for_tick(),
            )
            n = len(getattr(result, "spawned", []) or [])
            log.info("control worker-action=dispatch board=%s spawned=%d", board, n)
            return {
                "ok": True, "action": action, "run_id": run_id,
                "detail": f"Dispatcher-Tick ausgeführt — {n} Worker gestartet.",
                "dispatch": asdict(result) if is_dataclass(result) else None,
            }

        run = kanban_db.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        task_id = run.task_id

        if action == "nudge":
            kanban_db.add_comment(
                conn, task_id, author="control-dashboard",
                body=(payload.reason or "Operator-Nudge: bitte Status prüfen / weitermachen."),
            )
            log.info("control worker-action=nudge run=%s task=%s", run_id, task_id)
            return {"ok": True, "action": action, "run_id": run_id, "task_id": task_id,
                    "detail": "Nudge als Kommentar gesetzt (kein Kill)."}

        if action == "unlock":
            ok = kanban_db.reclaim_task(conn, task_id, reason=(payload.reason or "control unlock"))
            log.info("control worker-action=unlock run=%s task=%s ok=%s", run_id, task_id, ok)
            if not ok:
                return {"ok": False, "action": action, "run_id": run_id, "task_id": task_id,
                        "detail": "Kein aktiver Claim zum Lösen (Task nicht running)."}
            return {"ok": True, "action": action, "run_id": run_id, "task_id": task_id,
                    "detail": "Claim gelöst — Task ist wieder beanspruchbar (ready)."}

        # B4 — hold: atomically stop the worker and park the task as blocked so
        # no Dispatcher tick can claim it between the kill and the block step.
        # The reason "operator hold" contains the word "operator" which matches
        # _AUTO_RETRY_QUESTION_RE inside auto_retry_blocked_tasks — so that
        # sweep classifies it as "operator_question" (non-retryable) and skips it.
        # hold_task performs the full transition in a single write_txn.
        if action == "hold":
            ok = kanban_db.hold_task(conn, task_id, reason="operator hold")
            if not ok:
                log.info("control worker-action=hold run=%s task=%s hold=False", run_id, task_id)
                return {"ok": False, "action": action, "run_id": run_id, "task_id": task_id,
                        "detail": "Konnte Task nicht halten (nicht running)."}
            log.info("control worker-action=hold run=%s task=%s parked=blocked", run_id, task_id)
            return {"ok": True, "action": action, "run_id": run_id, "task_id": task_id,
                    "detail": "Worker gestoppt und Task als operator_hold geparkt (kein Auto-Redispatch)."}

        # B4 — resume: release the operator_hold block back to ready/todo.
        if action == "resume":
            released = kanban_db.unblock_task(conn, task_id)
            log.info("control worker-action=resume run=%s task=%s released=%s", run_id, task_id, released)
            if not released:
                return {"ok": False, "action": action, "run_id": run_id, "task_id": task_id,
                        "detail": "Hold lösen fehlgeschlagen (Task nicht blocked/scheduled)."}
            return {"ok": True, "action": action, "run_id": run_id, "task_id": task_id,
                    "detail": "Hold aufgehoben — Task ist wieder beanspruchbar."}

        # restart: reclaim the run, then one dispatcher tick so it is re-picked.
        # B4: apply model_override / assignee overrides BEFORE re-dispatch.
        ok = kanban_db.reclaim_task(conn, task_id, reason=(payload.reason or "control restart"))
        if not ok:
            log.info("control worker-action=restart run=%s task=%s reclaimed=False", run_id, task_id)
            return {"ok": False, "action": action, "run_id": run_id, "task_id": task_id,
                    "detail": "Konnte Run nicht zurückholen (kein aktiver Claim)."}
        if payload.model_override:
            kanban_db.set_task_model_override(conn, task_id, payload.model_override)
        if payload.assignee:
            with kanban_db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = ? WHERE id = ?",
                    (payload.assignee.strip() or None, task_id),
                )
        redispatch = kanban_db.dispatch_once(
            conn,
            board=board,
            **_dispatch_kwargs_for_tick(),
        )
        log.info("control worker-action=restart run=%s task=%s reclaimed=True", run_id, task_id)
        return {"ok": True, "action": action, "run_id": run_id, "task_id": task_id,
                "detail": "Worker zurückgeholt und neu eingeplant.",
                "dispatch": asdict(redispatch) if is_dataclass(redispatch) else None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recovery actions — reclaim a running claim, reassign to a new profile
# ---------------------------------------------------------------------------

class ReclaimBody(BaseModel):
    reason: Optional[FreeText] = None


@core_routes.post("/tasks/{task_id}/reclaim")
def reclaim_task_endpoint(
    task_id: str,
    payload: ReclaimBody,
    board: Optional[str] = Query(None),
):
    """Release an active worker claim on a running task.

    Used by the dashboard recovery popover when an operator wants to
    abort a stuck worker (e.g. one that keeps hallucinating card ids)
    without waiting for the claim TTL. Maps 1:1 to
    ``hermes kanban reclaim <task_id> --reason ...``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.reclaim_task(conn, task_id, reason=payload.reason)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot reclaim {task_id}: not in a claimable state "
                    "(not running, or unknown id)"
                ),
            )
        return {"ok": True, "task_id": task_id}
    finally:
        conn.close()


class RepairBody(BaseModel):
    """R1 (P1-repair-action): operator repair of a recoverable deliverable miss.

    The only knob is the actor stamped on the repair event; ``confirm`` gates
    the mutation exactly like ``WorkerActionBody`` so the dashboard confirm
    dialog is honoured."""

    confirm: bool = False
    actor: Optional[ShortText] = None


@control_routes.post("/tasks/{task_id}/repair")
def repair_task_endpoint(
    task_id: str,
    payload: RepairBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Close the missing ``kanban_complete`` step for a deliverable that was
    posted but whose worker exited without terminalizing the task (R1).

    Maps 1:1 onto the proven primitive
    :func:`kanban_db.repair_deliverable_posted_not_completed` — it reads the
    latest ``deliverable_posted_not_completed`` event, and only when that
    carries clear evidence does it repair the missing lifecycle call. Code or
    worktree tasks route ``blocked → review`` through the normal worker/review/
    integration gates; only safe non-code deliverables retain the proven
    direct ``blocked → done`` repair. No review verdict is synthesized.

    Same guard contract as ``POST /workers/{run_id}/action``: the mutation
    requires ``confirm: true`` and a refusal (missing confirm, or nothing
    repairable) is ``ok: false`` at HTTP 200 so the UI shows the reason inline
    instead of throwing.
    """
    if not payload.confirm:
        return {"ok": False, "task_id": task_id, "detail": "confirm required"}

    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        actor = (payload.actor or "").strip() or "control-dashboard"
        ok = kanban_db.repair_deliverable_posted_not_completed(
            conn, task_id, actor=actor,
        )
        if not ok:
            log.info("control repair task=%s board=%s ok=False", task_id, board)
            return {
                "ok": False, "task_id": task_id,
                "detail": (
                    "Kein reparierbares deliverable_posted_not_completed "
                    "(Task nicht blocked oder kein Evidenz-Event)."
                ),
            }
        log.info("control repair task=%s board=%s actor=%s ok=True", task_id, board, actor)
        return {
            "ok": True, "task_id": task_id,
            "detail": (
                "Protokoll-Repair: fehlender kanban_complete nachgeschlossen; "
                "Code/Worktree läuft durch Review, sichere Non-Code-Ausgabe "
                "wird direkt terminalisiert."
            ),
        }
    finally:
        conn.close()


class SpecifyBody(BaseModel):
    """Optional author override. Nothing else is configurable from the
    dashboard — model + prompt come from ``auxiliary.triage_specifier``
    in config.yaml, same as the CLI."""

    author: Optional[ShortText] = None


@core_routes.post("/tasks/{task_id}/specify")
def specify_task_endpoint(
    task_id: str,
    payload: SpecifyBody,
    board: Optional[str] = Query(None),
):
    """Flesh out a triage-column task via the auxiliary LLM and promote
    it to ``todo``. Maps 1:1 to ``hermes kanban specify <task_id>``.

    Returns the outcome shape used by the CLI: ``{ok, task_id, reason,
    new_title}``. A non-OK outcome is NOT an HTTP error — the UI renders
    the reason inline (e.g. "no auxiliary client configured") so the
    operator knows what to fix, and retries without a page reload.

    This endpoint runs in FastAPI's threadpool (sync ``def``) because
    the underlying LLM call can take tens of seconds to minutes on
    reasoning models, which would block the event loop if we used
    ``async def`` without an explicit ``run_in_executor``.
    """
    board = _resolve_board(board)
    # Pin the board for the duration of this call so the specifier module
    # (which calls ``kb.connect()`` with no args) hits the right DB. Use a
    # context-local override rather than mutating the process-global
    # HERMES_KANBAN_BOARD env var — this endpoint runs in FastAPI's
    # threadpool, so two concurrent requests for different boards would
    # otherwise race on the shared env var and cross-write (issue #38323).
    with kanban_db.scoped_current_board(board or kanban_db.DEFAULT_BOARD):
        # Import lazily so a missing auxiliary client at import time
        # doesn't break plugin load.
        from hermes_cli import kanban_specify  # noqa: WPS433 (intentional)

        outcome = kanban_specify.specify_task(
            task_id,
            author=(payload.author or None),
        )

    return {
        "ok": bool(outcome.ok),
        "task_id": outcome.task_id,
        "reason": outcome.reason,
        "new_title": outcome.new_title,
    }


class ReassignBody(BaseModel):
    profile: Optional[ShortText] = None  # "" or None = unassign
    reclaim_first: bool = False
    reason: Optional[FreeText] = None


@core_routes.post("/tasks/{task_id}/reassign")
def reassign_task_endpoint(
    task_id: str,
    payload: ReassignBody,
    board: Optional[str] = Query(None),
):
    """Reassign a task to a different profile, optionally reclaiming first.

    Used by the dashboard recovery popover when an operator wants to
    retry a task with a different worker profile (e.g. switch to a
    smarter model after the assigned profile keeps hallucinating).
    Maps 1:1 to ``hermes kanban reassign <task_id> <profile> [--reclaim]``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.reassign_task(
            conn, task_id,
            payload.profile or None,
            reclaim_first=bool(payload.reclaim_first),
            reason=payload.reason,
        )
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot reassign {task_id}: unknown id, or still "
                    "running (pass reclaim_first=true to release the claim first)"
                ),
            )
        return {"ok": True, "task_id": task_id, "assignee": payload.profile or None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plugin config (read dashboard.kanban.* defaults from config.yaml)
# ---------------------------------------------------------------------------

@core_routes.get("/config")
def get_config():
    """Return kanban dashboard preferences from ~/.hermes/config.yaml.

    Reads the ``dashboard.kanban`` section if present; defaults otherwise.
    Used by the UI to pre-select tenant filters, toggle markdown rendering,
    or set column-width preferences without a round-trip per page load.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    dash_cfg = (cfg.get("dashboard") or {})
    # dashboard.kanban may itself be a dict; fall back to {}.
    k_cfg = dash_cfg.get("kanban") or {}
    return {
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True)),
    }


# ---------------------------------------------------------------------------
# Browser Web Push subscriptions
# ---------------------------------------------------------------------------

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


def _configured_home_channels() -> list[dict]:
    """Return every platform that has a home_channel set, fully hydrated.

    Thin delegate to :func:`gateway.config.configured_home_channels` — the
    shared single source of truth so the dashboard, the CLI subscribe-on-create
    path, and this module all resolve the same home channels.
    """
    try:
        from gateway.config import configured_home_channels
    except Exception:
        return []
    return configured_home_channels()


def _active_profile_name() -> str:
    """Return the current Hermes profile name for notify-sub ownership."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _home_sub_matches(sub: dict, home: dict) -> bool:
    """True if a notify_subs row corresponds to the given home channel."""
    return (
        sub.get("platform") == home["platform"]
        and str(sub.get("chat_id", "")) == str(home["chat_id"])
        and str(sub.get("thread_id") or "") == str(home["thread_id"] or "")
    )


@core_routes.get("/home-channels")
def get_home_channels(
    task_id: Optional[str] = Query(None),
    board: Optional[str] = Query(None),
):
    """List every platform with a home channel, plus whether *task_id*
    (if given) is currently subscribed to that home.

    When ``task_id`` is omitted, every entry's ``subscribed`` is ``false``
    — useful for the "no task selected" state of the UI.
    """
    homes = _configured_home_channels()
    subscribed_homes: set[tuple[str, str, str]] = set()
    if task_id:
        board = _resolve_board(board)
        conn = _conn(board=board)
        try:
            subs = kanban_db.list_notify_subs(conn, task_id)
        finally:
            conn.close()
        for sub in subs:
            key = (
                str(sub.get("platform") or ""),
                str(sub.get("chat_id") or ""),
                str(sub.get("thread_id") or ""),
            )
            subscribed_homes.add(key)
    result = []
    for home in homes:
        key = (home["platform"], home["chat_id"], home["thread_id"])
        result.append({**home, "subscribed": key in subscribed_homes})
    return {"home_channels": result}


@core_routes.post("/tasks/{task_id}/home-subscribe/{platform}")
def subscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Subscribe *task_id* to notifications routed to *platform*'s home channel.

    Idempotent — re-subscribing is a no-op at the DB layer. 404 if the
    platform has no home channel configured. 404 if the task doesn't exist.
    """
    homes = _configured_home_channels()
    home = next((h for h in homes if h["platform"] == platform), None)
    if not home:
        raise HTTPException(
            status_code=404,
            detail=f"No home channel configured for platform {platform!r}. "
                   f"Set one from the messenger via /sethome, or configure "
                   f"gateway.platforms.{platform}.home_channel in config.yaml.",
        )
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        kanban_db.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None,
            notifier_profile=_active_profile_name(),
        )
        return {"ok": True, "task_id": task_id, "home_channel": home}
    finally:
        conn.close()


@core_routes.delete("/tasks/{task_id}/home-subscribe/{platform}")
def unsubscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Remove any notify subscription on *task_id* that matches *platform*'s home."""
    homes = _configured_home_channels()
    home = next((h for h in homes if h["platform"] == platform), None)
    if not home:
        raise HTTPException(
            status_code=404,
            detail=f"No home channel configured for platform {platform!r}.",
        )
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        kanban_db.remove_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None,
        )
        return {"ok": True, "task_id": task_id, "home_channel": home}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stats (per-profile / per-status counts + oldest-ready age)
# ---------------------------------------------------------------------------

@core_routes.get("/stats")
def get_stats(board: Optional[str] = Query(None)):
    """Per-status + per-assignee counts + oldest-ready age.

    Designed for the dashboard HUD and for router profiles that need to
    answer "is this specialist overloaded?" without scanning the whole
    board themselves.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return kanban_db.board_stats(conn)
    finally:
        conn.close()


@core_routes.get("/assignees")
def get_assignees(board: Optional[str] = Query(None)):
    """Return known profiles and their board task counts."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return {"assignees": kanban_db.known_assignees(conn)}
    finally:
        conn.close()


class OrchestrationSettingsBody(BaseModel):
    orchestrator_profile: Optional[str] = None
    default_assignee: Optional[str] = None
    auto_decompose: Optional[bool] = None
    auto_promote_children: Optional[bool] = None


@core_routes.get("/orchestration")
def get_orchestration_settings():
    """Return explicit and effective Kanban orchestration settings."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
    explicit_orch = (kanban_cfg.get("orchestrator_profile") or "").strip()
    explicit_default = (kanban_cfg.get("default_assignee") or "").strip()
    auto_decompose = bool(kanban_cfg.get("auto_decompose", True))
    auto_promote_children = bool(kanban_cfg.get("auto_promote_children", True))

    resolved_orch = explicit_orch
    resolved_default = explicit_default
    try:
        from hermes_cli import profiles as profiles_mod

        active_default = profiles_mod.get_active_profile_name() or "default"
        if not resolved_orch or not profiles_mod.profile_exists(resolved_orch):
            resolved_orch = active_default
        if not resolved_default or not profiles_mod.profile_exists(resolved_default):
            resolved_default = active_default
    except Exception:
        active_default = "default"
        resolved_orch = resolved_orch or active_default
        resolved_default = resolved_default or active_default

    return {
        "orchestrator_profile": explicit_orch,
        "default_assignee": explicit_default,
        "auto_decompose": auto_decompose,
        "auto_promote_children": auto_promote_children,
        "resolved_orchestrator_profile": resolved_orch,
        "resolved_default_assignee": resolved_default,
        "active_profile": active_default,
    }


@core_routes.put("/orchestration")
def set_orchestration_settings(payload: OrchestrationSettingsBody):
    """Persist the supplied Kanban orchestration settings."""
    try:
        from hermes_cli.config import load_config, save_config

        cfg = load_config() or {}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to load config: {exc}")

    kanban_section = cfg.setdefault("kanban", {})
    if not isinstance(kanban_section, dict):
        kanban_section = {}
        cfg["kanban"] = kanban_section

    try:
        from hermes_cli import profiles as profiles_mod
    except Exception:
        profiles_mod = None

    for field in ("orchestrator_profile", "default_assignee"):
        value = getattr(payload, field)
        if value is None:
            continue
        name = (value or "").strip()
        if name and profiles_mod is not None:
            try:
                if not profiles_mod.profile_exists(name):
                    raise HTTPException(
                        status_code=400, detail=f"profile '{name}' does not exist"
                    )
            except HTTPException:
                raise
            except Exception:
                pass
        kanban_section[field] = name

    if payload.auto_decompose is not None:
        kanban_section["auto_decompose"] = bool(payload.auto_decompose)
    if payload.auto_promote_children is not None:
        kanban_section["auto_promote_children"] = bool(payload.auto_promote_children)

    try:
        save_config(cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to save config: {exc}")
    return get_orchestration_settings()


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

@core_routes.get("/tasks/{task_id}/log")
def get_task_log(
    task_id: str,
    tail: Optional[int] = Query(None, ge=1, le=2_000_000),
    board: Optional[str] = Query(None),
):
    """Return the worker's stdout/stderr log.

    ``tail`` caps the response size (bytes) so the dashboard drawer
    doesn't paginate megabytes into the browser. Returns 404 if the task
    has never spawned. The on-disk log is rotated at 2 MiB per
    ``_rotate_worker_log`` — a single ``.log.1`` is kept, no further
    generations, so disk usage per task is bounded at ~4 MiB.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    content = kanban_db.read_worker_log(task_id, tail_bytes=tail, board=board)
    log_path = kanban_db.worker_log_path(task_id, board=board)
    size = log_path.stat().st_size if log_path.exists() else 0
    return {
        "task_id": task_id,
        "path": str(log_path),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        # Truncated when the on-disk file was larger than the tail cap.
        "truncated": bool(tail and size > tail),
    }


# ---------------------------------------------------------------------------
# Dispatch nudge (optional quick-path so the UI doesn't wait 60 s)
# ---------------------------------------------------------------------------

@core_routes.post("/dispatch")
def dispatch(
    dry_run: bool = Query(False),
    max_n: int = Query(8, ge=1, le=32, alias="max"),
    board: Optional[str] = Query(None),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        result = kanban_db.dispatch_once(
            conn,
            dry_run=dry_run,
            board=board,
            **_dispatch_kwargs_for_tick(max_spawn_override=max_n),
        )
        # DispatchResult is a dataclass.
        try:
            return asdict(result)
        except TypeError:
            return {"result": str(result)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Boards CRUD (multi-project support)
# ---------------------------------------------------------------------------

class CreateBoardBody(BaseModel):
    slug: ShortText
    name: Optional[ShortText] = None
    description: Optional[FreeText] = None
    icon: Optional[ShortText] = None
    color: Optional[ShortText] = None
    default_workdir: Optional[str] = None
    switch: bool = False


class RenameBoardBody(BaseModel):
    name: Optional[ShortText] = None
    description: Optional[FreeText] = None
    icon: Optional[ShortText] = None
    color: Optional[ShortText] = None


def _board_counts(slug: str) -> dict[str, int]:
    """Return ``{status: count}`` for a board. Safe on an empty DB."""
    try:
        path = kanban_db.kanban_db_path(board=slug)
        if not path.exists():
            return {}
        conn = kanban_db.connect(board=slug)
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            return {r["status"]: int(r["n"]) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def _default_workspace_kind(board: dict[str, Any]) -> str:
    """Recommend a non-destructive task workspace from board metadata."""
    workdir = str(board.get("default_workdir") or "").strip()
    if not workdir:
        return "scratch"
    try:
        return "worktree" if kanban_db._git_toplevel(Path(workdir)) else "dir"
    except (OSError, ValueError):
        return "dir"


@core_routes.get("/boards")
def list_boards(include_archived: bool = Query(False)):
    """Return every board on disk with task counts and the active slug."""
    boards = kanban_db.list_boards(include_archived=include_archived)
    current = kanban_db.get_current_board()
    project_by_board: dict[str, Any] = {}
    project_conn: Optional[sqlite3.Connection] = None
    try:
        project_conn = projects_db.connect()
        for project in projects_db.list_projects(
            project_conn, include_archived=False
        ):
            if project.board_slug and project.board_slug not in project_by_board:
                project_by_board[project.board_slug] = project
    except Exception:
        # Board navigation remains available if projects.db is missing, locked,
        # or temporarily unreadable; such boards are explicitly unbound.
        project_by_board = {}
    finally:
        if project_conn is not None:
            try:
                project_conn.close()
            except Exception:
                pass
    for b in boards:
        project = project_by_board.get(b["slug"])
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
        b["default_workspace_kind"] = _default_workspace_kind(b)
        b["project_id"] = project.id if project else None
        b["project_slug"] = project.slug if project else None
        b["project_name"] = project.name if project else None
        b["project_bound"] = project is not None
    return {"boards": boards, "current": current}


@core_routes.post("/boards")
def create_board_endpoint(payload: CreateBoardBody):
    """Create a new board. Idempotent — ``slug`` collision returns existing."""
    default_workdir = None
    if payload.default_workdir:
        requested = Path(payload.default_workdir).expanduser()
        if not requested.is_absolute():
            raise HTTPException(
                status_code=400,
                detail="Project directory must be an absolute path.",
            )
        if not requested.is_dir():
            raise HTTPException(
                status_code=400,
                detail="Project directory must be an existing directory.",
            )
        default_workdir = str(requested.resolve())
    try:
        meta = kanban_db.create_board(
            payload.slug,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            color=payload.color,
            default_workdir=default_workdir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if payload.switch:
        try:
            kanban_db.set_current_board(meta["slug"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    meta["default_workspace_kind"] = _default_workspace_kind(meta)
    return {"board": meta, "current": kanban_db.get_current_board()}


@core_routes.patch("/boards/{slug}")
def rename_board(slug: str, payload: RenameBoardBody):
    """Update a board's display metadata (slug is immutable — create a new one to rename the directory)."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    meta = kanban_db.write_board_metadata(
        normed,
        name=payload.name,
        description=payload.description,
        icon=payload.icon,
        color=payload.color,
    )
    return {"board": meta}


@core_routes.delete("/boards/{slug}")
def delete_board(slug: str, delete: bool = Query(False, description="Hard-delete instead of archive")):
    """Archive (default) or hard-delete a board."""
    try:
        res = kanban_db.remove_board(slug, archive=not delete)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result": res, "current": kanban_db.get_current_board()}


@core_routes.post("/boards/{slug}/switch")
def switch_board(slug: str):
    """Persist ``slug`` as the active board for subsequent CLI / slash calls.

    Dashboard users pick boards via a client-side ``localStorage`` — this
    endpoint is for ``/kanban boards switch`` parity so gateway slash
    commands and the CLI share the same current-board pointer.
    """
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    kanban_db.set_current_board(normed)
    return {"current": normed}


# ---------------------------------------------------------------------------
# WebSocket: /events?since=<event_id>
# ---------------------------------------------------------------------------

# Poll interval for the event tail loop. SQLite WAL + 300 ms polling is
# the simplest and most robust approach; it adds a fraction of a percent
# of CPU and has no shared state to synchronize across workers.
_EVENT_POLL_SECONDS = 0.3


# ---------------------------------------------------------------------------
# Profile metadata & description editing (consumed by the kanban orchestrator)
# ---------------------------------------------------------------------------

class DescribeBody(BaseModel):
    description: Optional[FreeText] = None  # explicit user-authored text


class DescribeAutoBody(BaseModel):
    overwrite: bool = False


@core_routes.get("/profiles")
def list_profile_roster():
    """Return every installed profile with its description.

    Consumed by the dashboard's settings panel (orchestrator picker)
    and the profile-description editing UI. Profiles without a
    description still appear here — they're routable on name alone,
    just less precisely.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        profiles = profiles_mod.list_profiles()
    except Exception:
        log.exception("failed to list profiles")
        raise HTTPException(status_code=500, detail="failed to list profiles")
    return {
        "profiles": [
            {
                "name": p.name,
                "is_default": bool(p.is_default),
                "model": p.model or "",
                "provider": p.provider or "",
                "description": p.description or "",
                "description_auto": bool(p.description_auto),
                "skill_count": int(p.skill_count or 0),
            }
            for p in profiles
        ],
    }


@core_routes.patch("/profiles/{profile_name}")
def update_profile_description(profile_name: str, payload: DescribeBody):
    """Set or clear the description of a profile.

    Empty string clears the description; non-empty stores it as a
    user-authored description (``description_auto: false``) so the
    auto-describer won't overwrite it on a sweep without
    ``--overwrite``.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        canon = profiles_mod.normalize_profile_name(profile_name)
        if canon == "default":
            from hermes_constants import get_hermes_home  # type: ignore
            from pathlib import Path as _Path
            profile_dir = _Path(get_hermes_home())
        else:
            profile_dir = profiles_mod.get_profile_dir(canon)
        if not profile_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"profile '{profile_name}' not found")
        text = (payload.description or "").strip()
        profiles_mod.write_profile_meta(
            profile_dir,
            description=text,
            description_auto=False,
        )
    except HTTPException:
        raise
    except Exception:
        log.exception("failed to update profile")
        raise HTTPException(status_code=500, detail="failed to update profile")
    return {"ok": True, "profile": canon, "description": text}


@core_routes.post("/profiles/{profile_name}/describe-auto")
def auto_describe_profile(profile_name: str, payload: DescribeAutoBody):
    """Generate a description for the named profile via the auxiliary
    LLM (``auxiliary.profile_describer``). Persists with
    ``description_auto: true`` so the dashboard can surface a "review"
    badge.

    Maps 1:1 to ``hermes profile describe <name> --auto``. Non-OK
    outcomes are NOT HTTP errors — the UI renders the reason inline
    (e.g. "no auxiliary client configured") so the operator can fix
    config and retry without a page reload.
    """
    try:
        from hermes_cli import profile_describer  # noqa: WPS433 (intentional)
        outcome = profile_describer.describe_profile(
            profile_name,
            overwrite=bool(payload.overwrite),
        )
    except Exception:
        log.exception("describer crashed")
        raise HTTPException(status_code=500, detail="describer crashed")
    return {
        "ok": bool(outcome.ok),
        "profile": outcome.profile_name,
        "reason": outcome.reason,
        "description": outcome.description,
    }


# ---------------------------------------------------------------------------
# Decompose endpoint (built-in decomposer fan-out)
# ---------------------------------------------------------------------------

class DecomposeBody(BaseModel):
    author: Optional[ShortText] = None


@core_routes.post("/tasks/{task_id}/decompose")
def decompose_task_endpoint(
    task_id: str,
    payload: DecomposeBody,
    board: Optional[str] = Query(None),
):
    """Fan a triage-column task out into a graph of child tasks via the
    auxiliary LLM, routed to specialist profiles by description. Maps
    1:1 to ``hermes kanban decompose <task_id>``.

    Returns the outcome shape used by the CLI: ``{ok, task_id, reason,
    fanout, child_ids, new_title}``. A non-OK outcome is NOT an HTTP
    error — the UI renders the reason inline.

    Runs in FastAPI's threadpool (sync ``def``) because the LLM call
    can take minutes on reasoning models.
    """
    board = _resolve_board(board)
    # Context-local board pin (see specify endpoint above): this sync
    # endpoint runs in FastAPI's threadpool, so mutating the process-global
    # HERMES_KANBAN_BOARD env var would let concurrent requests for
    # different boards race and cross-write (issue #38323).
    with kanban_db.scoped_current_board(board or kanban_db.DEFAULT_BOARD):
        from hermes_cli import kanban_decompose  # noqa: WPS433 (intentional)
        outcome = kanban_decompose.decompose_task(
            task_id,
            author=(payload.author or None),
        )

    return {
        "ok": bool(outcome.ok),
        "task_id": outcome.task_id,
        "reason": outcome.reason,
        "fanout": bool(outcome.fanout),
        "child_ids": outcome.child_ids or [],
        "new_title": outcome.new_title,
    }


# ---------------------------------------------------------------------------
# PlanSpec hub — Vault PlanSpecs -> deterministic held Kanban chains
# ---------------------------------------------------------------------------

class PlanSpecPathBody(BaseModel):
    path: ShortText
    author: Optional[ShortText] = "dashboard"


class PlanSpecCompilePreviewBody(BaseModel):
    prose: FreeText


class PlanSpecProseIngestBody(BaseModel):
    prose: FreeText
    author: Optional[ShortText] = "dashboard"
    freigabe: Literal["operator", "sofort"] = "operator"


@planspec_routes.get("/planspecs")
def list_planspecs(
    scope: Literal["open", "all"] = Query("open"),
    valid: Optional[bool] = Query(None),
    limit: Optional[int] = Query(None, ge=0, le=500),
    q: Optional[str] = Query(None, max_length=256),
    board: Optional[str] = Query(None),
):
    from hermes_cli import planspecs  # noqa: WPS433 (intentional)
    from hermes_constants import get_hermes_home  # noqa: WPS433 (intentional)

    board = _resolve_board(board)
    records = planspecs.list_planspecs(
        scope=scope,
        valid=valid,
        limit=limit,
        search=q,
        include_kanban_status=True,
        board=board,
        prose_plans_root=get_hermes_home() / "dashboard" / "prose-plans",
    )
    return {"planspecs": records, "count": len(records)}


@planspec_routes.post("/planspecs/compile-preview")
def compile_planspec_preview(payload: PlanSpecCompilePreviewBody):
    from hermes_cli.plan_compiler import CompileBlocked  # noqa: WPS433 (intentional)
    from hermes_cli.plan_prose import compile_prose_plan, parse_prose_plan  # noqa: WPS433

    try:
        result = compile_prose_plan(parse_prose_plan(payload.prose))
    except CompileBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})
    return {
        "ok": True,
        "children": result.children,
        "repairs": result.repairs,
        "warnings": result.warnings,
    }


def _persist_dashboard_prose_plan(prose: str) -> Path:
    from hermes_constants import get_hermes_home  # noqa: WPS433 (intentional)

    digest = hashlib.sha256(prose.encode("utf-8")).hexdigest()[:16]
    root = get_hermes_home() / "dashboard" / "prose-plans"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"dashboard-prose-{digest}.md"
    if not path.exists() or path.read_text(encoding="utf-8") != prose:
        path.write_text(prose, encoding="utf-8")
    return path


@planspec_routes.post("/planspecs/ingest-prose")
def ingest_prose_planspec(payload: PlanSpecProseIngestBody, board: Optional[str] = Query(None)):
    from hermes_cli import planspecs  # noqa: WPS433 (intentional)

    board = _resolve_board(board)
    try:
        source_path = _persist_dashboard_prose_plan(payload.prose)
        prose_freigabe: Literal["complete", "operator"] = (
            "complete" if payload.freigabe == "sofort" else "operator"
        )
        return planspecs.ingest_prose_plan(
            source_path,
            board=board,
            author=payload.author or "dashboard",
            freigabe=prose_freigabe,
        )
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})


@planspec_routes.post("/planspecs/ingest")
def ingest_planspec(payload: PlanSpecPathBody, board: Optional[str] = Query(None)):
    from hermes_cli import planspecs  # noqa: WPS433 (intentional)

    board = _resolve_board(board)
    try:
        return planspecs.ingest_planspec(
            payload.path,
            board=board,
            author=payload.author or "dashboard",
        )
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})


@planspec_routes.post("/planspecs/sprint-prompt")
def sprint_prompt_for_planspec(payload: PlanSpecPathBody):
    from hermes_cli import planspecs  # noqa: WPS433 (intentional)

    try:
        return planspecs.sprint_prompt_for_planspec(payload.path)
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})


@planspec_routes.post("/planspecs/not-needed")
def mark_planspec_not_needed(payload: PlanSpecPathBody):
    from hermes_cli import planspecs  # noqa: WPS433 (intentional)

    try:
        return planspecs.mark_planspec_not_needed(
            payload.path,
            author=payload.author or "dashboard",
        )
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})


@planspec_routes.get("/planspecs/detail")
def get_planspec_detail(path: str = Query(..., max_length=1024)):
    """Return human-readable fields parsed from a PlanSpec .md file.

    The ``path`` parameter is attacker-influenced; security is enforced by
    ``parse_binding_planspec`` → ``resolve_planspec_path`` (same validator used
    by all other planspec endpoints) which:
      - resolves symlinks and ``..`` components via ``Path.resolve(strict=False)``
      - rejects anything whose resolved absolute path is not under
        ``DEFAULT_PLANS_ROOT`` (/home/piet/vault/03-Agents)
      - rejects non-``.md`` suffixes
      - raises ``PlanSpecNotFound`` (→ 404) when the file is missing and
        ``PlanSpecBlocked`` (→ 400) for traversal / bad suffix / malformed path
        / malformed spec.

    #13: the path is resolved + read EXACTLY ONCE (inside parse_binding_planspec)
    — we do NOT validate first and then re-resolve+read separately, which would
    open a TOCTOU window for a symlink swap between the two resolutions.  Error
    findings never carry the resolved server path (see resolve_planspec_path).

    Source = file parse only.  No DB read.

    A dashboard prose-plan source (under ``get_hermes_home()/dashboard/
    prose-plans/`` — see ``_persist_dashboard_prose_plan``) carries no YAML
    frontmatter, so it can never satisfy ``parse_binding_planspec``. It is
    tried FIRST via ``parse_prose_plan_detail``, which returns ``None`` (not
    an exception) for any path outside that dir — falling through to the
    unchanged binding-PlanSpec vault resolution below for every other path,
    including one outside BOTH roots (still 400, exactly as before).
    """
    from hermes_cli import planspecs  # noqa: WPS433 (intentional)
    from hermes_constants import get_hermes_home  # noqa: WPS433 (intentional)

    try:
        prose_detail = planspecs.parse_prose_plan_detail(
            path, prose_plans_root=get_hermes_home() / "dashboard" / "prose-plans"
        )
    except planspecs.PlanSpecNotFound as exc:
        raise HTTPException(status_code=404, detail={"findings": exc.findings})
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})
    if prose_detail is not None:
        return prose_detail

    # Single resolution+read. Distinguish 404 (file missing) from 400 (traversal /
    # bad path / malformed spec) off the *exception type* — not by substring-
    # matching the finding text, which would silently break if wording changes.
    try:
        spec = planspecs.parse_binding_planspec(path)
    except planspecs.PlanSpecNotFound as exc:
        raise HTTPException(status_code=404, detail={"findings": exc.findings})
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})

    fm = spec.frontmatter

    # Map acceptance_criteria: list of dicts (structured) or strings (legacy).
    raw_ac = fm.get("acceptance_criteria") or []
    if isinstance(raw_ac, list):
        ac_out = []
        for item in raw_ac:
            if isinstance(item, dict):
                ac_out.append(item)
            else:
                ac_out.append({"statement": str(item)})
    else:
        ac_out = []

    # Map anti_scope: list of strings or a single string.
    raw_anti = fm.get("anti_scope") or []
    if isinstance(raw_anti, list):
        anti_scope_out = [str(x) for x in raw_anti]
    elif raw_anti:
        anti_scope_out = [str(raw_anti)]
    else:
        anti_scope_out = []

    # Map evidence_required: list of strings or a single string.
    raw_ev = fm.get("evidence_required") or []
    if isinstance(raw_ev, list):
        evidence_out = [str(x) for x in raw_ev]
    elif raw_ev:
        evidence_out = [str(raw_ev)]
    else:
        evidence_out = []

    # Map children → subtasks list with {id, title, lane, deps}.
    subtasks = [
        {
            "id": child.get("planspec_subtask_id") or "",
            "title": child.get("title") or "",
            "lane": child.get("planspec_lane") or child.get("assignee") or "",
            "deps": child.get("planspec_deps") or [],
        }
        for child in spec.children
    ]

    return {
        "goal": str(fm.get("goal") or fm.get("topic") or spec.topic or ""),
        "acceptance_criteria": ac_out,
        "anti_scope": anti_scope_out,
        "evidence_required": evidence_out,
        "freigabe": spec.freigabe,
        "live_test_depth": spec.live_test_depth,
        "subtasks": subtasks,
    }


# ---------------------------------------------------------------------------
# Terminal handoff (ATH-S5) — materialise an operator-authored PlanSpec draft
# (built in the Agent-Terminals view from selected text / captured lines) into a
# .md under the plans root, then reuse the EXISTING validate / ingest pipeline.
# No DB write logic here; nothing dispatches. Validate and Ingest are distinct,
# separately-clicked steps — the frontend never auto-fires either.
# ---------------------------------------------------------------------------

class HandoffDraftBody(BaseModel):
    content: PlanSpecText
    # Optional filename slug; when absent the first non-empty draft line is used.
    slug: Optional[ShortText] = None
    author: Optional[ShortText] = "dashboard"


def _handoff_slug(body: "HandoffDraftBody") -> str:
    from hermes_cli import terminal_handoff  # noqa: WPS433 (intentional)

    if body.slug and body.slug.strip():
        return terminal_handoff.slugify(body.slug)
    first_line = next(
        (ln.strip() for ln in (body.content or "").splitlines() if ln.strip()),
        "",
    )
    # Drop a leading markdown/yaml marker so a slug derives from real words.
    first_line = first_line.lstrip("#").lstrip("-").strip().strip('"').strip("'")
    return terminal_handoff.slugify(first_line)


@planspec_routes.post("/planspecs/validate")
def validate_planspec_draft(payload: HandoffDraftBody):
    """Read-only PlanSpec validation for a handoff draft.

    Materialises ``content`` to a draft .md under the plans root and runs the
    EXISTING deterministic validator (``planspecs.validate_planspec``) on it.
    Never raises — an invalid draft comes back as ``disposition: "invalid"`` with
    the findings, so the UI can show *why* without an error toast. Writes only the
    draft file; touches no DB and dispatches nothing.
    """
    from hermes_cli import planspecs, terminal_handoff  # noqa: WPS433 (intentional)

    root = planspecs.DEFAULT_PLANS_ROOT
    path = terminal_handoff.write_handoff_draft(
        payload.content, slug=_handoff_slug(payload), plans_root=root
    )
    return planspecs.validate_planspec(path, plans_root=root)


@planspec_routes.post("/planspecs/ingest-draft")
def ingest_planspec_draft(payload: HandoffDraftBody, board: Optional[str] = Query(None)):
    """Ingest a handoff draft via the EXISTING PlanSpec ingest path.

    Materialises ``content`` to the same draft .md, then delegates to
    ``planspecs.ingest_planspec`` (which owns all Kanban DB writes — no SQL here).
    A structural / rubric / judge failure surfaces as 400 with the findings, the
    same contract as ``/planspecs/ingest``. This creates a *held* chain
    (``freigabe: operator`` by default) — it does NOT dispatch.
    """
    from hermes_cli import planspecs, terminal_handoff  # noqa: WPS433 (intentional)

    board = _resolve_board(board)
    root = planspecs.DEFAULT_PLANS_ROOT
    path = terminal_handoff.write_handoff_draft(
        payload.content, slug=_handoff_slug(payload), plans_root=root
    )
    try:
        return planspecs.ingest_planspec(
            path,
            board=board,
            author=payload.author or "dashboard",
            plans_root=root,
        )
    except planspecs.PlanSpecBlocked as exc:
        raise HTTPException(status_code=400, detail={"findings": exc.findings})


# ---------------------------------------------------------------------------
# Flow capture Phase B — backend-driven planning (documented/lean) + gate + spec
# ---------------------------------------------------------------------------

class FlowCaptureBody(BaseModel):
    title: ShortText
    # "document" → rich decompose + durable Vault plan-spec; "lean" → base
    # decompose, no spec. The lean method is routed here ONLY for the lean+GATE
    # combo — lean+auto stays on the plain POST /tasks (Stufe-A) tick path.
    method: ShortText = "document"
    gate: bool = False
    tenant: Optional[ShortText] = "flow-capture"
    priority: int = 0
    notify_home: bool = True
    author: Optional[ShortText] = None
    # Phase C levers carried from the Flow capture sheet (gated chains only, so
    # they are consumed at release). ``review_tier`` is stamped on the root (the
    # chain Review-pill shows it at once; children inherit at release).
    # ``inject_scout`` is persisted as a root intent the release path honours.
    # Both optional → a lever-less capture is byte-identical to today.
    review_tier: Optional[Literal["standard", "review", "critical"]] = None
    inject_scout: bool = False
    # Optional short description: stored as the root body so the risk heuristic has
    # substance (not just the title) when it auto-classifies the review tier. The
    # capture sheet also feeds it to GET /flow/suggest-tier for the proposal.
    description: Optional[ShortText] = None


class FlowReleaseBody(BaseModel):
    assignee_overrides: dict[str, Optional[str]] = Field(default_factory=dict)
    release_level: Literal["merge", "live"] = "merge"
    # Phase C operator levers (both optional → calls without them are
    # byte-identical to today). ``review_tier`` is applied chain-wide to every
    # child; ``inject_scout`` prepends one read-only scout recon task before the
    # entry children of the released chain. Both are TRI-STATE (``None`` = the
    # release did not specify → fall back to the capture-step intent; an explicit
    # value — including ``inject_scout: false`` — vetoes the captured intent).
    review_tier: Optional[Literal["standard", "review", "critical"]] = None
    inject_scout: Optional[bool] = None


class FlowSizingBody(BaseModel):
    action: Literal["merge", "split"]
    task_ids: list[ShortText] = Field(default_factory=list, max_length=8)
    title: Optional[ShortText] = None
    body: Optional[FreeText] = None
    assignee: Optional[ShortText] = None


class FlowTimeoutSweepBody(BaseModel):
    timeout_seconds: Optional[int] = None


def _flow_gate_timeout_seconds() -> int:
    raw = os.environ.get("HERMES_FLOW_GATE_TIMEOUT_SECONDS", "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            parsed = int(raw)
            if parsed > 0:
                return parsed
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        k_cfg = ((cfg.get("dashboard") or {}).get("kanban") or {})
        parsed = int(k_cfg.get("flow_gate_timeout_seconds") or 0)
        if parsed > 0:
            return parsed
    except Exception:
        pass
    return 30 * 60


def _flow_gate_soft_cost_usd() -> float:
    raw = os.environ.get("HERMES_FLOW_GATE_SOFT_COST_USD", "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            parsed = float(raw)
            if parsed > 0:
                return parsed
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        k_cfg = ((cfg.get("dashboard") or {}).get("kanban") or {})
        parsed = float(k_cfg.get("flow_gate_soft_cost_usd") or 0)
        if parsed > 0:
            return parsed
    except Exception:
        pass
    return 1.0


def _flow_gate_child_order(conn: sqlite3.Connection, root_id: str) -> dict[str, int]:
    try:
        for event in reversed(kanban_db.list_events(conn, root_id)):
            if event.kind != "decomposed":
                continue
            child_ids = (event.payload or {}).get("child_ids")
            if isinstance(child_ids, list):
                return {
                    str(child_id): idx
                    for idx, child_id in enumerate(child_ids)
                    if isinstance(child_id, str)
                }
    except Exception:
        pass
    return {}


def _flow_gate_child_rows(conn: sqlite3.Connection, root_id: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT t.*
          FROM task_links l
          JOIN tasks t ON t.id = l.parent_id
         WHERE l.child_id = ?
           AND t.status != 'archived'
         ORDER BY t.created_at ASC, t.id ASC
        """,
        (root_id,),
    ).fetchall()
    order = _flow_gate_child_order(conn, root_id)
    if not order:
        return rows
    return sorted(rows, key=lambda row: (order.get(row["id"], len(order)), row["created_at"], row["id"]))


def _flow_gate_child_ids(conn: sqlite3.Connection, root_id: str) -> list[str]:
    return [r["id"] for r in _flow_gate_child_rows(conn, root_id)]


def _flow_gate_lanes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    names: set[str] = {"default"}
    lanes_out: list[dict[str, Any]] = []
    try:
        lanes = kanban_db.list_lanes(conn)
    except Exception:
        lanes = []
    for lane in lanes:
        profiles = [
            str(p).strip()
            for p in (lane.get("profiles") or [])
            if str(p).strip()
        ]
        names.update(profiles)
        lanes_out.append({
            "id": lane.get("id"),
            "name": lane.get("name") or lane.get("id") or "lane",
            "active": bool(lane.get("active")),
            "profiles": profiles,
        })
    for profile in _lane_profile_catalog():
        name = str(profile.get("name") or "").strip()
        if name:
            names.add(name)
    return [
        {"id": "profiles", "name": "Profile", "active": False, "profiles": sorted(names)}
    ] + lanes_out


def _flow_gate_profile_cost_stats(conn: sqlite3.Connection, *, last_n: int = 50) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    profile,
                    cost_usd,
                    ROW_NUMBER() OVER (
                        PARTITION BY profile
                        ORDER BY started_at DESC, id DESC
                    ) AS rn
                  FROM task_runs
                 WHERE profile IS NOT NULL
                   AND cost_usd IS NOT NULL
                   AND ended_at IS NOT NULL
            )
            SELECT profile, COUNT(*) AS runs, AVG(cost_usd) AS avg_cost_usd
              FROM ranked
             WHERE rn <= ?
             GROUP BY profile
            """,
            (int(last_n),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[row["profile"]] = {
            "runs": int(row["runs"] or 0),
            "avg_cost_usd": float(row["avg_cost_usd"] or 0.0),
        }
    return out


def _flow_gate_risk(task: kanban_db.Task, stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    tone = "low"
    profile_stats = stats.get(task.assignee or "")
    if not task.assignee:
        tone = "medium"
        reasons.append("unassigned")
    elif not profile_stats:
        tone = "medium"
        reasons.append("no recent lane history")
    else:
        blocked = float(profile_stats.get("blocked_pct") or 0.0)
        timeout = float(profile_stats.get("timeout_pct") or 0.0)
        if timeout >= 25.0 or blocked >= 40.0:
            tone = "high"
            reasons.append("recent failure rate high")
        elif timeout >= 10.0 or blocked >= 15.0:
            tone = "medium"
            reasons.append("recent blocks/timeouts")
    body_len = len(task.body or "")
    if body_len > 6_000:
        tone = "high"
        reasons.append("large task body")
    elif body_len > 2_500 and tone == "low":
        tone = "medium"
        reasons.append("larger than typical")
    return {"tone": tone, "reasons": reasons}


def _flow_gate_estimate(
    children: list[kanban_db.Task],
    stats: dict[str, dict[str, Any]],
    cost_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    total_cost = 0.0
    total_tokens = 0
    for task in children:
        profile = task.assignee or "default"
        profile_stats = stats.get(profile, {})
        avg_tokens = profile_stats.get("avg_tokens")
        if avg_tokens is None:
            text_len = len(task.title or "") + len(task.body or "")
            est_tokens = max(800, int(text_len / 4) + 1_000)
            token_source = "size-fallback"
        else:
            est_tokens = max(1, int(avg_tokens))
            token_source = "recent-profile-average"
        avg_cost = cost_stats.get(profile, {}).get("avg_cost_usd")
        if avg_cost is not None:
            est_cost = float(avg_cost)
            cost_source = "recent-profile-average"
        else:
            est_cost = est_tokens * 0.0000025
            cost_source = "token-fallback"
        total_tokens += est_tokens
        total_cost += est_cost
        items.append({
            "task_id": task.id,
            "profile": profile,
            "estimated_tokens": est_tokens,
            "estimated_cost_usd": round(est_cost, 6),
            "token_source": token_source,
            "cost_source": cost_source,
        })
    soft_limit = _flow_gate_soft_cost_usd()
    return {
        "estimated_tokens": total_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "soft_limit_usd": soft_limit,
        "warning": total_cost > soft_limit,
        "items": items,
    }


def _flow_gate_payload(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    root = kanban_db.get_task(conn, root_id)
    if root is None:
        raise HTTPException(status_code=404, detail=f"task {root_id} not found")
    child_tasks = [
        kanban_db.Task.from_row(row)
        for row in _flow_gate_child_rows(conn, root_id)
    ]
    stats = kanban_db.profile_outcome_stats(conn)
    cost_stats = _flow_gate_profile_cost_stats(conn)
    timeout_seconds = _flow_gate_timeout_seconds()
    children: list[dict[str, Any]] = []
    held_count = 0
    for task in child_tasks:
        if task.status == "scheduled":
            held_count += 1
        children.append({
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee": task.assignee,
            "parents": kanban_db.parent_ids(conn, task.id),
            "risk": _flow_gate_risk(task, stats),
            "created_at": task.created_at,
            "age_seconds": max(0, int(time.time()) - int(task.created_at)),
        })
    return {
        "root_id": root_id,
        "root_status": root.status,
        "children": children,
        "held_count": held_count,
        "release_levels": ["merge", "live"],
        "timeout_seconds": timeout_seconds,
        "timeout_at": (int(root.created_at) + timeout_seconds) if child_tasks else None,
        "auto_dispatch_eligible": bool(child_tasks and held_count > 0 and int(time.time()) - int(root.created_at) >= timeout_seconds),
        "lanes": _flow_gate_lanes(conn),
        "cost_estimate": _flow_gate_estimate(child_tasks, stats, cost_stats),
    }


def _append_flow_gate_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    with kanban_db.write_txn(conn):
        kanban_db._append_event(conn, task_id, kind, payload)


def _chain_graph(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    if kanban_db.get_task(conn, root_id) is None:
        raise HTTPException(status_code=404, detail=f"task {root_id} not found")

    nodes: set[str] = {root_id}
    edges: set[tuple[str, str]] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (current,),
        ).fetchall()
        for row in rows:
            parent = row["parent_id"]
            edge = (parent, current)
            edges.add(edge)
            if parent not in nodes:
                nodes.add(parent)
                stack.append(parent)

    children_by_parent: dict[str, list[str]] = {}
    parents_by_child: dict[str, list[str]] = {}
    for parent, child in sorted(edges):
        children_by_parent.setdefault(parent, []).append(child)
        parents_by_child.setdefault(child, []).append(parent)

    # Progress rollup mirrors the board-card contract: for every node that has
    # outgoing task_links, report how many direct children are done / total.
    # The frontend treats ``None`` as "no children or legacy backend".
    progress: dict[str, dict[str, int]] = {}
    if nodes:
        placeholders = ",".join("?" for _ in nodes)
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.status AS cstatus "
            "FROM task_links l JOIN tasks t ON t.id = l.child_id "
            f"WHERE l.parent_id IN ({placeholders})",
            tuple(nodes),
        ).fetchall():
            p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
            p["total"] += 1
            if row["cstatus"] == "done":
                p["done"] += 1

    depth_cache: dict[str, int] = {}

    def depth(node: str, seen: Optional[set[str]] = None) -> int:
        if node in depth_cache:
            return depth_cache[node]
        seen = set(seen or set())
        if node in seen:
            return 0
        seen.add(node)
        parents = parents_by_child.get(node) or []
        value = 0 if not parents else max(depth(parent, seen) + 1 for parent in parents)
        depth_cache[node] = value
        return value

    now = int(time.time())

    # Per-node cost aggregates — a single query over all chain nodes so the
    # loop below doesn't issue one query per node.  Fail-soft on pre-K5a DBs.
    node_costs: dict[str, dict[str, Any]] = {}
    if nodes:
        placeholders = ",".join("?" for _ in nodes)
        try:
            for row in conn.execute(
                f"""
                SELECT
                    task_id,
                    CAST(COALESCE(SUM(input_tokens), 0) AS INTEGER)  AS input_tokens,
                    CAST(COALESCE(SUM(output_tokens), 0) AS INTEGER) AS output_tokens,
                    COALESCE(SUM(cost_usd), 0.0)                     AS cost_usd,
                    COALESCE(SUM(COALESCE(
                        json_extract(metadata, '$.cost_usd_equivalent'), 0.0
                    )), 0.0)                                          AS cost_usd_equivalent
                FROM task_runs
                WHERE task_id IN ({placeholders})
                GROUP BY task_id
                """,
                tuple(nodes),
            ).fetchall():
                c_usd = float(row["cost_usd"])
                c_equiv = float(row["cost_usd_equivalent"])
                node_costs[row["task_id"]] = {
                    "input_tokens": int(row["input_tokens"]),
                    "output_tokens": int(row["output_tokens"]),
                    "cost_usd": c_usd,
                    "cost_usd_equivalent": c_equiv,
                    "cost_effective_usd": c_usd + c_equiv,
                }
        except sqlite3.OperationalError:
            pass  # pre-K5a: cost/token columns absent — leave node_costs empty

    _zero_costs: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "cost_usd_equivalent": 0.0,
        "cost_effective_usd": 0.0,
    }

    # Per-node review-role runs — ALL task_runs (not just latest_run), single
    # query over all chain nodes mirroring the cost/progress rollups above.
    # Frontend Rollen-Track (FIX-5) renders {profile,status,verdict} per role
    # for the focused node. The stored ``task_runs.verdict`` column is the
    # authoritative, pre-normalised gate outcome (APPROVED/REQUEST_CHANGES) —
    # it already reconciles each role's own vocabulary (e.g. the critic's
    # ``uphold``/``overturn``), which ``_normalize_verifier_verdict`` does NOT
    # (measured: ~29% of live review runs disagree). Read the column directly.
    # Fail-soft on pre-review-gate DBs where the column is absent.
    review_roles_by_task: dict[str, list[dict[str, Any]]] = {}
    if nodes:
        placeholders = ",".join("?" for _ in nodes)
        try:
            for row in conn.execute(
                f"""
                SELECT task_id, profile, status, verdict
                  FROM task_runs
                 WHERE task_id IN ({placeholders})
                 ORDER BY task_id, started_at, id
                """,
                tuple(nodes),
            ).fetchall():
                profile = row["profile"]
                if not profile:
                    continue
                review_roles_by_task.setdefault(row["task_id"], []).append({
                    "profile": profile,
                    "status": row["status"],
                    "verdict": row["verdict"],
                })
        except sqlite3.OperationalError:
            pass  # pre-review-gate DBs: task_runs.verdict column absent

    latest_runs_by_task: dict[str, sqlite3.Row] = {}
    if nodes:
        placeholders = ",".join("?" for _ in nodes)
        try:
            for row in conn.execute(
                f"""
                SELECT r.*
                  FROM task_runs AS r
                 WHERE r.task_id IN ({placeholders})
                   AND r.id = (
                       SELECT r2.id
                         FROM task_runs AS r2
                        WHERE r2.task_id = r.task_id
                        ORDER BY r2.started_at DESC, r2.id DESC
                        LIMIT 1
                   )
                """,
                tuple(nodes),
            ).fetchall():
                latest_runs_by_task[row["task_id"]] = row
        except sqlite3.OperationalError:
            pass
    legacy_resolver = _LegacyModelRouteResolver(
        conn,
        list(latest_runs_by_task.values()),
    )

    out_nodes: list[dict[str, Any]] = []
    for node_id in sorted(nodes, key=lambda item: (depth(item), item)):
        task = kanban_db.get_task(conn, node_id)
        if task is None:
            continue
        run = latest_runs_by_task.get(node_id)
        run_payload = None
        if run is not None:
            started = run["started_at"]
            ended = run["ended_at"]
            heartbeat = run["last_heartbeat_at"]
            run_payload = {
                "id": run["id"],
                "profile": run["profile"],
                "status": run["status"],
                "outcome": run["outcome"],
                "started_at": started,
                "ended_at": ended,
                "last_heartbeat_at": heartbeat,
                "runtime_seconds": (
                    max(0, int((ended or now) - started))
                    if started is not None else None
                ),
                "heartbeat_age_seconds": (
                    max(0, now - int(heartbeat))
                    if heartbeat is not None else None
                ),
                # S2: additiver Run-Fortschritt — elapsed/max_runtime_seconds.
                # null bei fehlendem Cap → FleetView-Fokus-Rail nutzt DAG-fallback.
                "run_progress": run_progress_value(run, now),
            }
            run_payload.update(
                _run_model_route_fields(
                    conn,
                    run,
                    legacy_resolver=legacy_resolver,
                )
            )
        costs = node_costs.get(node_id, _zero_costs)
        out_nodes.append({
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee": task.assignee,
            "level": depth(node_id),
            "parents": sorted(parents_by_child.get(node_id, [])),
            "children": sorted(children_by_parent.get(node_id, [])),
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "last_heartbeat_at": task.last_heartbeat_at,
            "runtime_seconds": (
                max(0, int(((task.completed_at or now) - task.started_at)))
                if task.started_at is not None else None
            ),
            "progress": progress.get(task.id),
            "latest_run": run_payload,
            "review_roles": review_roles_by_task.get(node_id, []),
            "cost_usd": costs["cost_usd"],
            "cost_usd_equivalent": costs["cost_usd_equivalent"],
            "cost_effective_usd": costs["cost_effective_usd"],
            "input_tokens": costs["input_tokens"],
            "output_tokens": costs["output_tokens"],
        })
    return {
        "schema": "kanban-chain-graph-v1",
        "root_id": root_id,
        "checked_at": now,
        "nodes": out_nodes,
        "edges": [
            {"from": parent, "to": child}
            for parent, child in sorted(edges, key=lambda edge: (depth(edge[0]), edge[0], depth(edge[1]), edge[1]))
        ],
    }


def _merge_flow_children(
    conn: sqlite3.Connection,
    root_id: str,
    keep_id: str,
    merge_id: str,
) -> dict[str, Any]:
    if keep_id == merge_id:
        raise HTTPException(status_code=400, detail="merge requires two distinct child ids")
    # Membership, existence and status are read INSIDE the IMMEDIATE txn and every
    # write is CAS-guarded on status = 'scheduled'. A pre-txn read would be a TOCTOU:
    # a concurrent claim between check and write would be silently overwritten and
    # still logged as `archived`. On a lost CAS we raise 409 -> write_txn rolls the
    # whole merge back, so no archive event survives a failed transition.
    with kanban_db.write_txn(conn):
        child_ids = set(_flow_gate_child_ids(conn, root_id))
        if keep_id not in child_ids or merge_id not in child_ids:
            raise HTTPException(
                status_code=400, detail="both merge ids must be children of the flow root"
            )
        keep = kanban_db.get_task(conn, keep_id)
        merged = kanban_db.get_task(conn, merge_id)
        if keep is None or merged is None:
            raise HTTPException(status_code=404, detail="merge child not found")
        if keep.status != "scheduled" or merged.status != "scheduled":
            raise HTTPException(
                status_code=409, detail="only scheduled flow children can be merged"
            )
        keep_body = keep.body or ""
        merged_body = merged.body or ""
        next_body = (
            keep_body.rstrip()
            + "\n\n---\nMerged from "
            + merge_id
            + "\n\n"
            + merged_body.lstrip()
        ).strip()
        cur = conn.execute(
            "UPDATE tasks SET title = ?, body = ? WHERE id = ? AND status = 'scheduled'",
            (f"{keep.title} + {merged.title}"[:_SHORT_TEXT_MAX_LENGTH], next_body, keep_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409, detail=f"flow child {keep_id} changed state during merge"
            )
        for row in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (merge_id,),
        ).fetchall():
            parent_id = row["parent_id"]
            if parent_id != keep_id:
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent_id, keep_id),
                )
        for row in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?",
            (merge_id,),
        ).fetchall():
            child_id = row["child_id"]
            if child_id != keep_id:
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (keep_id, child_id),
                )
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
            (merge_id, merge_id),
        )
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL WHERE id = ? AND status = 'scheduled'",
            (merge_id,),
        )
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409, detail=f"flow child {merge_id} changed state during merge"
            )
        kanban_db._append_event(
            conn,
            merge_id,
            "archived",
            {"via": "flow_gate_merge", "root_id": root_id, "merged_into": keep_id},
        )
        kanban_db._append_event(
            conn,
            keep_id,
            "flow_gate_sizing",
            {"action": "merge", "root_id": root_id, "merged_id": merge_id},
        )
        kanban_db._append_event(
            conn,
            root_id,
            "flow_gate_sizing",
            {"action": "merge", "kept_id": keep_id, "merged_id": merge_id},
        )
    kanban_db.recompute_ready(conn)
    return {"action": "merge", "kept_id": keep_id, "archived_id": merge_id}


def _split_flow_child(
    conn: sqlite3.Connection,
    root_id: str,
    task_id: str,
    *,
    title: Optional[str],
    body: Optional[str],
    assignee: Optional[str],
) -> dict[str, Any]:
    child_ids = set(_flow_gate_child_ids(conn, root_id))
    if task_id not in child_ids:
        raise HTTPException(status_code=400, detail="split id must be a child of the flow root")
    original = kanban_db.get_task(conn, task_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    if original.status != "scheduled":
        raise HTTPException(status_code=409, detail="only scheduled flow children can be split")
    parents = [p for p in kanban_db.parent_ids(conn, task_id) if p != root_id]
    new_id = kanban_db.create_task(
        conn,
        title=(title or f"{original.title} / split").strip(),
        body=body or original.body,
        assignee=(assignee or original.assignee),
        created_by="flow-gate",
        workspace_kind=original.workspace_kind,
        workspace_path=original.workspace_path,
        tenant=original.tenant,
        priority=original.priority,
        parents=parents,
        kind="code",
    )
    kanban_db.link_tasks(conn, new_id, root_id)
    kanban_db.schedule_task(conn, new_id, reason=f"Flow-Gate split from {task_id}")
    _append_flow_gate_event(
        conn,
        task_id,
        "flow_gate_sizing",
        {"action": "split_source", "root_id": root_id, "new_id": new_id},
    )
    _append_flow_gate_event(
        conn,
        root_id,
        "flow_gate_sizing",
        {"action": "split", "source_id": task_id, "new_id": new_id},
    )
    return {"action": "split", "source_id": task_id, "new_id": new_id}


def _flow_capture_intent(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    """Return the ``{review_tier, inject_scout}`` the operator chose at the Flow
    *capture* step (persisted as a ``flow_capture_opts`` event), or ``{}`` when
    none. This lets a gated chain's release honour the capture-step levers even
    when the release call itself omits them — the operator just clicks "Kette
    starten", or the timeout sweep releases the chain autonomously."""
    try:
        row = conn.execute(
            """
            SELECT payload FROM task_events
             WHERE task_id = ? AND kind = 'flow_capture_opts'
             ORDER BY id DESC LIMIT 1
            """,
            (root_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or not row["payload"]:
        return {}
    try:
        data = json.loads(row["payload"])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _release_flow_gate(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    assignee_overrides: dict[str, Optional[str]],
    release_level: Literal["merge", "live"],
    reason: str,
    review_tier: Optional[str] = None,
    inject_scout: Optional[bool] = None,
) -> dict[str, Any]:
    root = kanban_db.get_task(conn, root_id)
    if root is None:
        raise HTTPException(status_code=404, detail=f"task {root_id} not found")
    # Fall back to the capture-step levers (persisted at flow-capture) when this
    # release call did not SPECIFY them (param is None). An explicit release-time
    # value always wins — including ``inject_scout=False`` to veto a captured
    # scout — so the operator keeps full control at "Kette starten"; the capture
    # intent only fills the gap for a bare release / the autonomous sweep. Using
    # ``is None`` (not falsiness) is what makes an explicit False distinguishable
    # from "not specified".
    _intent = _flow_capture_intent(conn, root_id)
    if review_tier is None:
        review_tier = _intent.get("review_tier")
    if inject_scout is None:
        inject_scout = bool(_intent.get("inject_scout", False))
    child_ids = _flow_gate_child_ids(conn, root_id)
    child_set = set(child_ids)
    overrides: dict[str, Optional[str]] = {}
    for raw_id, raw_profile in (assignee_overrides or {}).items():
        child_id = str(raw_id).strip()
        if child_id not in child_set:
            raise HTTPException(
                status_code=400,
                detail=f"assignee override {child_id!r} is not a child of {root_id}",
            )
        profile = str(raw_profile).strip() if raw_profile is not None else ""
        overrides[child_id] = profile or None
    for child_id, profile in overrides.items():
        if not kanban_db.reassign_task(
            conn,
            child_id,
            profile,
            reason=f"Flow-Gate lane override before {release_level} release",
        ):
            raise HTTPException(status_code=409, detail=f"could not reassign {child_id}")
    released: list[str] = []
    for child_id in child_ids:
        child = kanban_db.get_task(conn, child_id)
        if child is not None and child.status == "scheduled":
            if kanban_db.unblock_task(conn, child_id):
                released.append(child_id)

    # Phase C lever: a chain-wide review_tier is stamped on the children RELEASED
    # this call (chain-start) so the staged-review resolver (verifier→reviewer→
    # critic) governs them. Scoped to ``released`` — NOT all child_ids — so a
    # later release (released == 0) never re-routes an already-started/done child
    # (Grill-Entscheid: tier is set at start only, no mid-flight edit). The typed
    # body constrains the value; the setter validates again (defense-in-depth).
    tier_value = (review_tier or "").strip().lower() or None
    if tier_value is not None:
        for child_id in released:
            kanban_db.set_task_review_tier(conn, child_id, tier_value)

    # Phase C lever: prepend ONE read-only scout recon task before the entry
    # children (released children with no in-chain parent), so the cheap scout
    # surfaces findings before the coders run. Only when this call actually
    # released children — a re-release (nothing scheduled) must not spawn a
    # second scout. A freshly created scout has no links, so link_tasks can
    # never cycle; it demotes each ready entry child to todo (waiting on scout).
    scout_id: Optional[str] = None
    if inject_scout and released:
        # Entry children = released children with no in-chain parent. Also skip any
        # that ALREADY carry a scout predecessor — when auto_scout_on_critical is on
        # and tier=critical, set_task_review_tier (above) injected a per-child scout;
        # the explicit inject_scout must not add a SECOND scout to the same child.
        entry_children = [
            cid for cid in released
            if not (set(kanban_db.parent_ids(conn, cid)) & child_set)
            and kanban_db.scout_predecessor_id(conn, cid) is None
        ]
        if entry_children:
            # Inherit the entry children's scope into the scout body so a
            # fanned-out scout reconns each released slice against its real
            # task body (allowed paths / scope_contract / anti-scope), not a
            # generic instruction it would broaden from its own title.
            scout_id = kanban_db.create_task(
                conn,
                title=f"Scout: {root.title}",
                body=kanban_db._scout_recon_body(
                    [kanban_db.get_task(conn, cid) for cid in entry_children]
                ),
                assignee="scout",
                created_by="flow-gate",
                priority=root.priority,
                tenant=root.tenant,
                max_runtime_seconds=kanban_db._scout_max_runtime_seconds(),
            )
            for cid in entry_children:
                kanban_db.link_tasks(conn, scout_id, cid)

    event_payload: dict[str, Any] = {
        "released_ids": released,
        "release_level": release_level,
        "assignee_overrides": overrides,
        "reason": reason,
    }
    # Phase C keys are added ONLY when set, so a no-option release records a
    # byte-identical event (no present-as-null keys for old consumers).
    if tier_value is not None:
        event_payload["review_tier"] = tier_value
    if scout_id is not None:
        event_payload["scout_id"] = scout_id
    _append_flow_gate_event(conn, root_id, "flow_gate_released", event_payload)
    # If this flow-capture root is ALSO a freigabe:operator hold, releasing its
    # children via the flow gate must clear the operator hold at the root too —
    # otherwise the root stays scheduled+freigabe=operator and keeps masquerading
    # as a pending proposal in held_operator_proposals (and stays approve/veto-
    # able, which would archive or double-release this already-building chain).
    # release_freigabe_hold flips the root scheduled->todo + records
    # freigabe_released exactly as the F1 strategist-approve path does; it is a
    # no-op for a non-operator root, and the children are already unblocked above
    # so its own child loop finds nothing scheduled (no double-release).
    kanban_db.release_freigabe_hold(conn, root_id, author="flow-gate")
    root_after = kanban_db.get_task(conn, root_id)
    root_freigabe_row = conn.execute(
        "SELECT freigabe FROM tasks WHERE id=?",
        (root_id,),
    ).fetchone()
    root_freigabe = (
        str(root_freigabe_row["freigabe"] or "").strip().lower()
        if root_freigabe_row is not None and "freigabe" in root_freigabe_row.keys()
        else ""
    )
    root_released = False
    if (
        root_after is not None
        and root_after.status == "scheduled"
        and root_freigabe == "complete"
    ):
        root_released = kanban_db.unblock_task(conn, root_id)
        if root_released:
            _append_flow_gate_event(
                conn,
                root_id,
                "flow_gate_root_released",
                {"reason": reason, "release_level": release_level},
            )
    result: dict[str, Any] = {
        "ok": True,
        "task_id": root_id,
        "released": len(released),
        "released_ids": released,
        "release_level": release_level,
        "assignee_overrides": overrides,
    }
    # Echo the Phase C levers only when set (byte-identical response otherwise).
    if tier_value is not None:
        result["review_tier"] = tier_value
    if scout_id is not None:
        result["scout_id"] = scout_id
    if root_released:
        result["root_released"] = True
    return result


@flow_release_routes.get("/flow/suggest-tier")
def flow_suggest_tier(title: str = Query(""), description: str = Query("")):
    """Propose a review tier for the capture sheet from the deterministic risk
    heuristic over title+description. The operator sees the proposal pre-filled
    and may raise it freely (a downgrade below the floor needs a deliberate ack at
    release). Self-gating default — the same classifier the resolver uses."""
    from hermes_cli.control_plane_gate import classify_review_tier
    spec = {"objective": title or "", "goal": description or "", "scope": description or ""}
    return {"tier": classify_review_tier(spec)}


@flow_release_routes.post("/tasks/flow-capture")
def flow_capture(payload: FlowCaptureBody, board: Optional[str] = Query(None)):
    """Create a root, PARK it in ``scheduled`` (invisible to the gateway's
    triage-only auto-decompose tick), then plan it via the aux decomposer.

    ``method='document'`` renders a durable Vault plan-spec (narrative +
    subtask table) from the same object it creates the subtasks from — one
    truth, no drift. ``gate=True`` holds the subtasks in ``scheduled`` until
    released via ``/tasks/{id}/flow-release``; ``gate=False`` auto-promotes
    them like today.

    Runs in FastAPI's threadpool (sync ``def``) because the LLM planning call
    can take a while. A non-OK plan leaves the root safely parked in
    ``scheduled`` (the operator can dispatch/decompose it manually).
    """
    method = (payload.method or "document").strip().lower()
    if method not in ("document", "lean"):
        raise HTTPException(status_code=400, detail="method must be 'document' or 'lean'")
    board = _resolve_board(board)

    # 1) Create the root in triage, then park it in scheduled (triage -> todo
    #    -> scheduled) inside ONE connection so no gateway tick interleaves
    #    before it is safely parked. Mirrors the create_task park path.
    conn = _conn(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=payload.title,
            body=(payload.description or None),
            assignee=None,
            created_by="dashboard",
            tenant=payload.tenant,
            priority=payload.priority,
            triage=True,
            # Phase C: stamp the chosen tier on the root so the chain Review-pill
            # shows it immediately on the held chain. create_task sets the column
            # directly (no auto-scout hook); children inherit at release.
            review_tier=payload.review_tier,
        )
        _park_task_for_operator(
            conn,
            task_id,
            reason="Flow-Plan: geparkt während der Planung",
            allow_existing_active=True,
        )
        # Phase C: persist the capture-step levers as a root intent so the gated
        # chain's release honours them even when the operator just clicks "Kette
        # starten" (or the timeout sweep releases autonomously) without re-picking
        # them. Recorded only when an actual lever was set → byte-identical else.
        if payload.review_tier or payload.inject_scout:
            _append_flow_gate_event(
                conn,
                task_id,
                "flow_capture_opts",
                {
                    "review_tier": payload.review_tier,
                    "inject_scout": bool(payload.inject_scout),
                },
            )
        if payload.notify_home:
            _subscribe_task_to_home_channels(conn, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

    # 2) Plan synchronously, board-pinned (the sync endpoint runs in the
    #    threadpool, so mutating the process-global board env would let
    #    concurrent requests race — pin context-locally instead). The planner
    #    expects the root parked in 'scheduled' and fans out atomically from it.
    with kanban_db.scoped_current_board(board or kanban_db.DEFAULT_BOARD):
        from hermes_cli import kanban_decompose  # noqa: WPS433 (intentional)
        outcome = kanban_decompose.plan_and_document(
            task_id,
            gate=bool(payload.gate),
            document=(method == "document"),
            author=(payload.author or None),
        )

    return {
        "ok": bool(outcome.ok),
        "task_id": outcome.task_id,
        "reason": outcome.reason,
        "fanout": bool(outcome.fanout),
        "child_ids": outcome.child_ids or [],
        "new_title": outcome.new_title,
        "spec_relpath": outcome.spec_relpath,
        "gated": bool(outcome.gated),
        "method": method,
    }


@flow_release_routes.get("/tasks/{task_id}/flow-gate")
def flow_gate(task_id: str, board: Optional[str] = Query(None)):
    """Return the proposed gated chain before dispatch.

    This is the operator-facing pre-release contract: held children, available
    lanes, per-child risk, a soft cost estimate, and timeout eligibility.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return _flow_gate_payload(conn, task_id)
    finally:
        conn.close()


@flow_release_routes.post("/tasks/{task_id}/flow-gate/sizing")
def flow_gate_sizing(
    task_id: str,
    payload: FlowSizingBody,
    board: Optional[str] = Query(None),
):
    """Merge or split held Flow children before release."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        if payload.action == "merge":
            ids = [x for x in payload.task_ids if x]
            if len(ids) != 2:
                raise HTTPException(status_code=400, detail="merge requires exactly two task_ids")
            result = _merge_flow_children(conn, task_id, ids[0], ids[1])
        else:
            ids = [x for x in payload.task_ids if x]
            if len(ids) != 1:
                raise HTTPException(status_code=400, detail="split requires exactly one task_id")
            result = _split_flow_child(
                conn,
                task_id,
                ids[0],
                title=payload.title,
                body=payload.body,
                assignee=payload.assignee,
            )
        return {"ok": True, "task_id": task_id, **result, "gate": _flow_gate_payload(conn, task_id)}
    finally:
        conn.close()


@flow_release_routes.post("/tasks/flow-gate/timeout-sweep")
def flow_gate_timeout_sweep(
    payload: FlowTimeoutSweepBody | None = None,
    board: Optional[str] = Query(None),
):
    """Release gated chains whose root has exceeded the configured hold time."""
    board = _resolve_board(board)
    timeout_seconds = int(payload.timeout_seconds) if payload and payload.timeout_seconds else _flow_gate_timeout_seconds()
    if timeout_seconds <= 0:
        raise HTTPException(status_code=400, detail="timeout_seconds must be > 0")
    conn = _conn(board=board)
    try:
        cutoff = int(time.time()) - timeout_seconds
        # Scope the sweep to genuine Flow/PlanSpec gate roots only: a root that
        # carries an explicit ``flow_plan``/``specified``(planspec_ingest) event
        # marker, or whose tenant is one of the gate-owning tenants. A bare
        # "parent with scheduled children" is NOT swept — otherwise unrelated
        # ``scheduled`` tasks could be released by accident (B4-F2).
        rows = conn.execute(
            """
            SELECT DISTINCT root.id
              FROM task_links l
              JOIN tasks child ON child.id = l.parent_id
              JOIN tasks root ON root.id = l.child_id
             WHERE child.status = 'scheduled'
               AND root.created_at <= ?
               AND root.status != 'archived'
               -- Never auto-release a freigabe='operator' hold (a strategist
               -- PlanSpec awaiting explicit operator approve/veto). Only the
               -- /approve path (release_freigabe_hold, author='operator') may
               -- clear it; the autonomous timeout-sweep must leave it parked.
               AND (root.freigabe IS NULL OR root.freigabe != 'operator')
               AND (
                    root.tenant IN ('planspec', 'flow-capture')
                    OR EXISTS (
                         SELECT 1 FROM task_events e
                          WHERE e.task_id = root.id
                            AND (
                                 e.kind = 'flow_plan'
                                 OR (e.kind = 'specified'
                                     AND json_extract(e.payload, '$.source')
                                         = 'planspec_ingest')
                            )
                    )
               )
            """,
            (cutoff,),
        ).fetchall()
        released: list[dict[str, Any]] = []
        for row in rows:
            result = _release_flow_gate(
                conn,
                row["id"],
                assignee_overrides={},
                release_level="merge",
                reason=f"timeout-sweep after {timeout_seconds}s",
            )
            if result["released"]:
                released.append(result)
        return {
            "ok": True,
            "timeout_seconds": timeout_seconds,
            "released_roots": released,
            "released": sum(int(r["released"]) for r in released),
        }
    finally:
        conn.close()


@flow_release_routes.post("/tasks/{task_id}/flow-release")
def flow_release(
    task_id: str,
    payload: FlowReleaseBody | None = None,
    board: Optional[str] = Query(None),
):
    """Release (Flow "Go ausführen") a gated plan: unblock every child of this
    root currently held in ``scheduled`` so the dispatcher can pick them up.

    DAG-correct via ``unblock_task`` — a parent-free child goes straight to
    ``ready``, a child still waiting on siblings goes to ``todo`` and
    ``recompute_ready`` promotes it when its parents finish. Idempotent: a
    second call finds no scheduled children and releases none. The optional
    body lets the gate apply lane overrides and record whether the operator
    released for merge-only or live execution.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        body = payload or FlowReleaseBody()
        return _release_flow_gate(
            conn,
            task_id,
            assignee_overrides=body.assignee_overrides,
            release_level=body.release_level,
            reason="operator-release",
            review_tier=body.review_tier,
            inject_scout=body.inject_scout,
        )
    finally:
        conn.close()


@flow_release_routes.get("/tasks/{task_id}/chain-graph")
def get_chain_graph(task_id: str, board: Optional[str] = Query(None)):
    """Return a left-to-right DAG for a flow/root task.

    Traverses dependency parents from ``task_id`` and includes per-node runtime
    and latest-run heartbeat data for the /control chain-visualization tab.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return _chain_graph(conn, task_id)
    finally:
        conn.close()


def _resolve_chain_root(conn: sqlite3.Connection, task_id: str) -> str:
    """Walk child_ids downward from ``task_id`` to find the chain sink/root.

    In the Kanban link convention ``task_links(parent_id=work_node,
    child_id=sink)`` the orchestration root is the node that has no children
    (never appears as a ``parent_id`` in the links it is part of). When
    ``task_id`` is already the root this returns it unchanged. Cycle-safe.
    """
    seen: set[str] = set()
    current = task_id
    while True:
        seen.add(current)
        children = kanban_db.child_ids(conn, current)
        if not children:
            return current  # sink — no further children
        # Pick the first unseen child; if all seen (cycle), stop here.
        nxt = next((c for c in children if c not in seen), None)
        if nxt is None:
            return current
        current = nxt


class ChainCancelBody(BaseModel):
    confirm: bool = False


class ReleaseGateBody(BaseModel):
    confirm: bool = False


@flow_release_routes.post("/tasks/{task_id}/release-gate")
def release_gate_endpoint(
    task_id: str,
    payload: ReleaseGateBody,
    board: Optional[str] = Query(None),
):
    if not payload.confirm:
        return {"ok": False, "detail": "confirm required"}

    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        row = kanban_db.get_task(conn, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        parked = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'release_gate_parked' LIMIT 1",
            (task_id,),
        ).fetchone()
        if parked is None:
            raise HTTPException(status_code=409, detail="not a parked release-gate task")
        if row.status in {"done", "archived"}:
            raise HTTPException(status_code=409, detail=f"release-gate already {row.status}")
        if row.status != "blocked":
            raise HTTPException(status_code=409, detail=f"release-gate not blocked: {row.status}")

        from hermes_cli import kanban_worktrees  # noqa: WPS433 (intentional lazy import)

        # Launch the activation DETACHED (systemd transient unit), never inline:
        # the activation restarts THIS dashboard backend, and a synchronous run
        # would be killed by its own ``systemctl restart`` before it could write
        # the child's result — the self-termination trap. The detached unit runs
        # the gate + real restart and writes the child green/escalated itself.
        result = kanban_worktrees.spawn_release_gate_activation(task_id, board=board)
        return {
            "ok": bool(result.get("ok")),
            "status": "activating" if result.get("ok") else "spawn_failed",
            "unit": result.get("unit"),
            "detail": result.get("detail"),
        }
    finally:
        conn.close()


@flow_release_routes.post("/tasks/{root_id}/cancel-chain")
def cancel_chain_endpoint(
    root_id: str,
    payload: ChainCancelBody,
    board: Optional[str] = Query(None),
):
    if not payload.confirm:
        return {"ok": False, "detail": "confirm required"}

    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, root_id) is None:
            raise HTTPException(status_code=404, detail=f"task {root_id} not found")
        chain_root = _resolve_chain_root(conn, root_id)
        if kanban_db.get_task(conn, chain_root) is None:
            raise HTTPException(status_code=404, detail=f"task {chain_root} not found")
        result = kanban_db.cancel_chain(conn, chain_root)
        log.info(
            "control chain-cancel board=%s root=%s held=%d terminated=%d skipped=%d",
            board,
            chain_root,
            len(result["held"]),
            len(result["terminated"]),
            len(result["skipped"]),
        )
        return {"ok": True, "root_id": chain_root, **result}
    finally:
        conn.close()


@flow_release_routes.get("/tasks/{task_id}/chain-costs")
def get_chain_costs(task_id: str, board: Optional[str] = Query(None)):
    """Return token/$ aggregates for the chain that contains ``task_id``.

    Resolves to the chain root (sink) even when called on an interior work
    node, then delegates to ``kanban_db.chain_cost_breakdown``.

    Response schema ``kanban-chain-costs-v1``::

        {
            "schema":  "kanban-chain-costs-v1",
            "root_id": str,
            "totals":  {"input_tokens": int, "output_tokens": int,
                        "cost_usd": float, "run_count": int},
            "by_lane": [
                {"profile": str, "input_tokens": int, "output_tokens": int,
                 "cost_usd": float, "run_count": int},
                ...  # descending by cost_usd
            ],
        }
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        root_id = _resolve_chain_root(conn, task_id)
        return kanban_db.chain_cost_breakdown(conn, root_id)
    finally:
        conn.close()


@flow_release_routes.get("/tasks/{task_id}/flow-plan")
def get_flow_plan(task_id: str):
    """Serve the durable Vault plan-spec for a documented Flow capture root.

    The spec is keyed by task id (``<flow_plans_dir>/<task_id>.md``); 404 when
    none exists (lean captures and non-Flow tasks have no spec)."""
    # task_id is the filename stem — restrict to the canonical id charset so a
    # crafted value can't traverse out of the flow-plans dir.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", task_id or ""):
        raise HTTPException(status_code=400, detail="invalid task id")
    from hermes_cli import kanban_decompose  # noqa: WPS433 (intentional)
    path = kanban_decompose.flow_plan_path(task_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no flow-plan spec for this task")
    return FileResponse(
        path,
        media_type="text/markdown; charset=utf-8",
        filename=path.name,
        content_disposition_type="inline",
    )


class PlanSpecApproveBody(BaseModel):
    root_task_id: ShortText
    lane_models: Optional[dict[str, ShortText]] = None
    assignee_overrides: Optional[dict[str, ShortText]] = None
    inject_scout: Optional[bool] = None
    dry_run: bool = False


@planspec_routes.post("/planspecs/approve")
def approve_planspec(body: PlanSpecApproveBody, board: Optional[str] = Query(None)):
    """Composed PlanSpec-release: validate hold, apply lane overrides, optionally
    inject a scout, then release the freigabe:operator hold.

    Body fields:
    - ``root_task_id``: the held ``freigabe: operator`` root task (required).
    - ``lane_models``: legacy mapping of lane/assignee → model_id; every chain task
      whose assignee matches a key receives a ``model_override``.
    - ``assignee_overrides``: mapping of lane/assignee → new profile; every chain task
      whose assignee matches a key is reassigned and has ``model_override`` cleared.
    - ``inject_scout``: prepend exactly one scout task before the entry children;
      idempotent (no second scout if one already exists).
    - ``dry_run``: validate and report planned actions without writing anything.

    Returns ``{released, overrides_applied, scout_injected, dry_run}``.

    Errors:
    - 404 when ``root_task_id`` is unknown.
    - 409 when ``root_task_id`` is not a held ``freigabe: operator`` root.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        root_task_id = body.root_task_id.strip()

        # --- Guard: task must exist (404) -----------------------------------------
        # freigabe is a DB column not in the Task dataclass → query directly.
        row = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id = ?",
            (root_task_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"error": f"task {root_task_id!r} not found"},
            )

        # --- Guard: must be a held freigabe:operator root (409) -------------------
        # Only accept status='scheduled': a 'todo' root is already released (the
        # hold was cleared by a prior approve call).  Allowing 'todo' here would
        # re-execute overrides/scout on an already-live chain — violating the
        # 'gehaltener Root' requirement and masking Doppel-Approve as a no-op 200.
        freigabe_value = str(row["freigabe"] or "").strip().lower()
        if freigabe_value != "operator":
            raise HTTPException(
                status_code=409,
                detail={"error": f"{root_task_id} ist kein freigabe:operator-Root"},
            )
        if row["status"] != "scheduled":
            raise HTTPException(
                status_code=409,
                detail={"error": f"{root_task_id} hat Status {row['status']!r} und ist nicht freigabefähig (erwartet: scheduled)"},
            )

        root = kanban_db.get_task(conn, root_task_id)

        # --- Collect chain members (root + all transitive parents via chain graph) -
        chain_ids = kanban_db._chain_member_ids_from_sink(conn, root_task_id)

        # --- Compute lane/profile overrides for chain members ---------------------
        lane_models: dict[str, str] = dict(body.lane_models or {})
        assignee_overrides: dict[str, str] = {}
        for lane, assignee in dict(body.assignee_overrides or {}).items():
            normalized_lane = str(lane or "").strip()
            normalized_assignee = str(assignee or "").strip()
            if not normalized_lane or not normalized_assignee:
                continue
            try:
                assignee_overrides[normalized_lane] = kanban_db.validate_spawnable_assignee(normalized_assignee) or normalized_assignee
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        override_targets: list[str] = []
        assignee_targets: list[str] = []
        for tid in chain_ids:
            task = kanban_db.get_task(conn, tid)
            if task is None:
                continue
            assignee = str(task.assignee or "").strip()
            if assignee in assignee_overrides:
                assignee_targets.append(tid)
            elif assignee in lane_models:
                override_targets.append(tid)

        # --- Determine scout injection: entry tasks + global dedup ------------------
        # Entry tasks = all transitive chain members (not just direct children of
        # root) that have no in-chain parent themselves — i.e. the true leaves of
        # the Kanban DAG.  Using only parent_ids(root) missed nodes in chains
        # deeper than one hop (entry → middle → root).
        #
        # Global dedup (Blocker 2): if ANY task in the chain already carries
        # assignee='scout', we must not inject a second scout anywhere in the
        # chain — not just under the direct root children.
        should_inject_scout = bool(body.inject_scout)
        scout_would_inject = False
        scout_entry_children: list[str] = []
        if should_inject_scout:
            chain_set = set(chain_ids)
            # Global dedup: abort scout injection if a scout already lives
            # anywhere in the chain (assignee check on every chain member).
            chain_already_scouted = any(
                (t := kanban_db.get_task(conn, tid)) is not None
                and str(t.assignee or "").strip() == "scout"
                for tid in chain_ids
            )
            if not chain_already_scouted:
                # Entry tasks = chain members with no in-chain parent.
                for cid in chain_ids:
                    if cid == root_task_id:
                        # Root is the sink — never an entry task.
                        continue
                    cid_parents_in_chain = set(kanban_db.parent_ids(conn, cid)) & chain_set
                    if cid_parents_in_chain:
                        # Has an in-chain parent → not an entry task.
                        continue
                    scout_entry_children.append(cid)
                if scout_entry_children:
                    scout_would_inject = True

        # --- dry_run: return planned actions without writing ----------------------
        if body.dry_run:
            planned: list[dict] = []
            for tid in override_targets:
                task = kanban_db.get_task(conn, tid)
                if task is not None:
                    assignee = str(task.assignee or "").strip()
                    planned.append({
                        "action": "model_override",
                        "task_id": tid,
                        "assignee": assignee,
                        "model": lane_models[assignee],
                    })
            for tid in assignee_targets:
                task = kanban_db.get_task(conn, tid)
                if task is not None:
                    assignee = str(task.assignee or "").strip()
                    planned.append({
                        "action": "assignee_override",
                        "task_id": tid,
                        "from": assignee,
                        "to": assignee_overrides[assignee],
                        "model_override": None,
                    })
            if scout_would_inject:
                planned.append({
                    "action": "inject_scout",
                    "entry_children": scout_entry_children,
                })
            planned.append({
                "action": "release_freigabe_hold",
                "task_id": root_task_id,
            })
            return {
                "released": False,
                "overrides_applied": len(override_targets),
                "assignee_overrides_applied": len(assignee_targets),
                "scout_injected": False,
                "dry_run": True,
                "planned_actions": planned,
            }

        # --- Apply overrides + scout + release atomically --------------------------
        # Blocker 1 (Codex review): model_override writes and scout creation used to
        # run in their own committed transactions BEFORE release_freigabe_hold. A
        # late release failure (409/race/exception) left orphaned overrides/scout on
        # a chain that was never actually released. Fix: guard recheck, overrides,
        # scout injection and the freigabe-root release now share ONE write_txn —
        # any failure path rolls back everything. create_task/link_tasks/
        # release_freigabe_hold each open their own write_txn internally (nesting
        # raises — "cannot start a transaction within a transaction"), so this uses
        # their *_in_txn cores instead (established pattern, see
        # _release_freigabe_hold_root_in_txn).
        overrides_applied = 0
        assignee_overrides_applied = 0
        scout_injected = False
        with kanban_db.write_txn(conn):
            # Recheck the hold guard INSIDE the transaction: write_txn's
            # BEGIN IMMEDIATE serializes writers, so a second approve that raced
            # past the pre-txn read above (same status='scheduled' snapshot) is
            # caught here before any write lands — closes the concurrent
            # double-approve window (Blocker 1/2), on top of the ordinary
            # already-released 409 the top-of-function guard already covers.
            guard_row = conn.execute(
                "SELECT status, freigabe FROM tasks WHERE id = ?",
                (root_task_id,),
            ).fetchone()
            if guard_row is None or str(guard_row["freigabe"] or "").strip().lower() != "operator":
                raise HTTPException(
                    status_code=409,
                    detail={"error": f"{root_task_id} ist kein freigabe:operator-Root"},
                )
            if guard_row["status"] != "scheduled":
                raise HTTPException(
                    status_code=409,
                    detail={"error": f"{root_task_id} hat Status {guard_row['status']!r} und ist nicht freigabefähig (erwartet: scheduled)"},
                )

            for tid in assignee_targets:
                task = kanban_db.get_task(conn, tid)
                if task is None:
                    continue
                assignee = str(task.assignee or "").strip()
                new_assignee = assignee_overrides[assignee]
                cur = conn.execute(
                    "SELECT assignee, model_override FROM tasks WHERE id = ?",
                    (tid,),
                ).fetchone()
                if cur is None:
                    continue
                if cur["assignee"] != new_assignee or cur["model_override"] is not None:
                    conn.execute(
                        "UPDATE tasks SET assignee = ?, model_override = NULL WHERE id = ?",
                        (new_assignee, tid),
                    )
                    kanban_db._append_event(
                        conn,
                        tid,
                        "assignee_override",
                        {"actor": "planspec-approve", "from": assignee, "to": new_assignee, "model_override": None},
                    )
                    assignee_overrides_applied += 1

            for tid in override_targets:
                task = kanban_db.get_task(conn, tid)
                if task is None:
                    continue
                assignee = str(task.assignee or "").strip()
                model_id = lane_models[assignee]
                if kanban_db._set_task_model_override_in_txn(conn, tid, model_id):
                    overrides_applied += 1

            if should_inject_scout and scout_entry_children:
                # Blocker 2: dedup recheck immediately before creation, inside this
                # same transaction — a second racing approve only reaches this
                # point after the first one committed (BEGIN IMMEDIATE serializes
                # writers), so it now sees the just-created scout and skips.
                chain_already_scouted = any(
                    (t := kanban_db.get_task(conn, tid)) is not None
                    and str(t.assignee or "").strip() == "scout"
                    for tid in chain_ids
                )
                if not chain_already_scouted:
                    # Create exactly ONE scout that covers all entry children (same
                    # pattern as _release_flow_gate: a single scout title=root.title,
                    # body from _scout_recon_body, linked as parent of each entry
                    # child). Deterministic idempotency_key: a retried/duplicate
                    # approve call for this root re-finds the same scout instead of
                    # creating a second one, even outside the write-lock race window.
                    entry_tasks = [kanban_db.get_task(conn, cid) for cid in scout_entry_children]
                    scout_id = kanban_db._create_scout_task_in_txn(
                        conn,
                        title=f"Scout: {root.title}",
                        body=kanban_db._scout_recon_body(entry_tasks),
                        created_by="planspec-approve",
                        priority=root.priority,
                        tenant=root.tenant,
                        max_runtime_seconds=kanban_db._scout_max_runtime_seconds(),
                        idempotency_key=f"planspec-approve-scout:{root_task_id}",
                    )
                    for cid in scout_entry_children:
                        kanban_db._link_tasks_in_txn(conn, scout_id, cid)
                    scout_injected = True

            # --- Release the freigabe hold (root flip only — see below) -----------
            released = kanban_db._release_freigabe_hold_root_in_txn(conn, root_task_id, author="operator")
            if not released:
                raise HTTPException(
                    status_code=409,
                    detail={"error": f"{root_task_id} konnte nicht freigegeben werden"},
                )

        # --- Post-commit follow-ups (children + auto-scout) ------------------------
        # release_freigabe_hold's own child-unblock/recompute_ready/auto-scout tail
        # runs OUTSIDE its root write_txn (unblock_task/recompute_ready open their
        # own write_txns — nested write_txn is the same documented pitfall as
        # above). Duplicated here (not a call to release_freigabe_hold) because the
        # root flip already happened in our transaction above; calling the public
        # wrapper again would just re-stamp a redundant idempotent
        # 'freigabe_released' event for every approve. Mirrors
        # release_freigabe_hold's tail exactly.
        chain_child_ids = [tid for tid in chain_ids if tid != root_task_id]
        for child_id in chain_child_ids:
            child = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (child_id,)
            ).fetchone()
            if child is not None and child["status"] == "scheduled":
                kanban_db.unblock_task(conn, child_id)
        kanban_db.recompute_ready(conn)
        _rg_cfg = kanban_db._review_gate_config()
        if _rg_cfg.get("auto_scout_on_critical", False):
            for child_id in chain_child_ids:
                kanban_db._maybe_inject_critical_scout(conn, child_id, cfg=_rg_cfg)

        return {
            "released": True,
            "overrides_applied": overrides_applied,
            "assignee_overrides_applied": assignee_overrides_applied,
            "scout_injected": scout_injected,
            "dry_run": False,
        }
    finally:
        conn.close()


@core_routes.websocket("/events")
async def stream_events(ws: WebSocket):
    # Authorize the upgrade via the dashboard's canonical WS gate so the
    # correct credential is accepted in every mode (loopback token / gated
    # single-use ticket / server-internal credential). Browsers can't set
    # Authorization on a WS upgrade, so the credential rides in the query
    # string — the browser SDK's buildWsUrl() assembles it.
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    try:
        since_raw = ws.query_params.get("since", "0")
        try:
            cursor = int(since_raw)
        except ValueError:
            cursor = 0

        # Board selection — pinned at the WS handshake; re-subscribe to
        # switch boards. Changing boards mid-stream would require
        # reconciling two cursors, so the UI just opens a new WS on
        # board change.
        ws_board_raw = ws.query_params.get("board")
        try:
            ws_board = kanban_db._normalize_board_slug(ws_board_raw) if ws_board_raw else None
        except ValueError:
            ws_board = None

        def _fetch_new(cursor_val: int) -> tuple[int, list[dict]]:
            conn = kanban_db.connect(board=ws_board)
            try:
                rows = conn.execute(
                    "SELECT id, task_id, run_id, kind, payload, created_at "
                    "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 200",
                    (cursor_val,),
                ).fetchall()
                out: list[dict] = []
                new_cursor = cursor_val
                for r in rows:
                    try:
                        payload = json.loads(r["payload"]) if r["payload"] else None
                    except Exception:
                        payload = None
                    if r["kind"] == kanban_db.OPERATOR_ESCALATION_EVENT:
                        # operator_escalation is not a kanban lifecycle hook
                        # today. Bridge only this one event kind from the
                        # existing dashboard poll instead of widening the
                        # WebSocket/event fan-out surface.
                        _handle_operator_escalation_event_for_push(
                            event_id=int(r["id"]),
                            task_id=str(r["task_id"]),
                            board=ws_board,
                            payload=payload if isinstance(payload, dict) else None,
                        )
                    out.append({
                        "id": r["id"],
                        "task_id": r["task_id"],
                        "run_id": r["run_id"],
                        "kind": r["kind"],
                        "payload": payload,
                        "created_at": r["created_at"],
                    })
                    new_cursor = r["id"]
                return new_cursor, out
            finally:
                conn.close()

        while True:
            cursor, events = await asyncio.to_thread(_fetch_new, cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
            await asyncio.sleep(_EVENT_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        # Normal shutdown path: dashboard process exit (Ctrl-C) cancels the
        # websocket task while it is sleeping in the poll loop.
        # CancelledError is a BaseException in 3.8+ so the bare Exception
        # handler below would not catch it; without this clause Uvicorn
        # surfaces the cancellation as an application traceback. Quiet it.
        return
    except Exception as exc:  # defensive: never crash the dashboard worker
        log.warning("Kanban event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass

"""OpenClaw dispatch routes for Hermes Control (Glue #3 — dashboard).

Slice-1/2 wired the *backend* OpenClaw path: a kanban task whose assignee is
``openclaw:<agent>`` is intercepted by ``kanban_db.dispatch_once``, signed +
POSTed to Mission Control via the existing signer, left ``running``, and then
polled back into the task by ``kanban_db.poll_openclaw_results``.

This module adds the *operator-facing* surface the dashboard's OpenClaw tab
needs, mirroring the FastAPI style of ``hermes_cli.openclaw_view``
(``register_*_routes(app)`` + ``@app.get``/``@app.post`` decorators, behind the
existing ``/api/`` session-token middleware):

* ``POST /api/openclaw/dispatch`` — create a kanban task with the
  ``openclaw:<agent>`` assignee so the next dispatcher tick signs + submits it.
  NO secret/hmac material touches this path; the only privileged step (signing)
  happens later inside the dispatcher, exactly as for a CLI-created task.
* ``GET /api/openclaw/dispatched`` — read-only list of the ``openclaw:%`` tasks
  with their latest run's MC correlation (mc_task_id / workflow_id / result).
  Never exposes secrets.

Additive + feature-gated: only ``openclaw:<agent>`` assignees are touched; the
normal kanban path is 100% unaffected.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hermes_cli import kanban_db

# The four OpenClaw agents the signer + ``OPENCLAW_AGENT_TO_OPERATION`` map
# knows how to route. Kept in sync with kanban_db deliberately rather than
# imported blindly so an unknown agent yields a clean 400 here instead of a
# later spawn-failure deep in the dispatcher.
_VALID_AGENTS = ("atlas", "lens", "forge", "pixel")

# All four OpenClaw agents (atlas/lens/forge/pixel) are normal workers; none
# requires a separate operator-lock acknowledgement. ``operator_lock_acknowledged``
# is retained on the request body for backward compatibility but is ignored.
_OPERATOR_LOCK_AGENTS = ()  # type: tuple[str, ...]


class OpenClawDispatchBody(BaseModel):
    title: str
    description: Optional[str] = None
    agent: str
    board: Optional[str] = None
    deliver_to: Optional[str] = None
    # Deprecated/ignored: pixel is a normal read-only UI worker now. Kept so an
    # older client that still sends the field does not get a 422.
    operator_lock_acknowledged: bool = False


def _validate_deliver_to(value: Optional[str]) -> Optional[str]:
    """Accept only a Discord snowflake (17-20 digits), matching the signer's
    payload regex. Returns the cleaned id, or None when not provided. Raises
    HTTPException(400) on a malformed override so a bad channel id can't reach
    the signer at dispatch time."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if not (cleaned.isdigit() and 17 <= len(cleaned) <= 20):
        raise HTTPException(
            status_code=400,
            detail="deliver_to must be a Discord channel id (17-20 digits)",
        )
    return cleaned


def _compose_body(description: Optional[str], deliver_to: Optional[str]) -> Optional[str]:
    """Build the kanban task body.

    ``tasks`` has no metadata column, and ``_openclaw_deliver_to`` reads a
    ``deliver_to`` key off the task's metadata — a forward-looking hook that is
    dormant until a task object carries metadata. We persist the operator's
    override as a machine-parseable marker line on the body so it is (a) visible
    in the human trail and (b) recoverable later without a schema change. The
    default channel still applies functionally today.
    """
    parts: list[str] = []
    text = (description or "").strip()
    if text:
        parts.append(text)
    if deliver_to:
        parts.append(f"[openclaw_deliver_to:{deliver_to}]")
    if not parts:
        return None
    return "\n\n".join(parts)


def create_openclaw_dispatch(body: OpenClawDispatchBody) -> Any:
    """Create a kanban task that the dispatcher will sign + submit to MC.

    Validates agent + deliver_to, then creates a ``ready`` task
    with the ``openclaw:<agent>`` assignee. Returns ``{ok, taskId}``. Never
    touches secret/hmac material — signing happens later in the dispatcher.
    """
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    agent = (body.agent or "").strip().lower()
    if agent not in _VALID_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"agent must be one of {', '.join(_VALID_AGENTS)}",
        )

    if agent in _OPERATOR_LOCK_AGENTS and body.operator_lock_acknowledged is not True:
        raise HTTPException(
            status_code=400,
            detail=f"agent '{agent}' requires operator_lock_acknowledged=true",
        )  # _OPERATOR_LOCK_AGENTS is empty — retained as an inert hook.

    deliver_to = _validate_deliver_to(body.deliver_to)

    conn = kanban_db.connect(board=body.board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=title,
            body=_compose_body(body.description, deliver_to),
            assignee=f"{kanban_db.OPENCLAW_ASSIGNEE_PREFIX}{agent}",
            created_by="dashboard-openclaw",
            board=body.board,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()

    return {"ok": True, "taskId": task_id}


def _openclaw_meta_from_run(metadata_json: Optional[str]) -> dict[str, Any]:
    """Best-effort extract the ``openclaw`` correlation block from a run's
    metadata JSON. Never raises; returns {} when absent/unparseable."""
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    oc = parsed.get("openclaw")
    return oc if isinstance(oc, dict) else {}


def _agent_from_assignee(assignee: Optional[str]) -> Optional[str]:
    if not isinstance(assignee, str):
        return None
    if not assignee.startswith(kanban_db.OPENCLAW_ASSIGNEE_PREFIX):
        return None
    return assignee[len(kanban_db.OPENCLAW_ASSIGNEE_PREFIX):].strip().lower() or None


def list_openclaw_dispatched(board: Optional[str] = None) -> dict[str, Any]:
    """Read-only list of ``openclaw:%`` tasks + their latest run's MC link.

    Joins each task to its most-recent run (by ``started_at``/``id``) and
    surfaces the openclaw correlation persisted on that run's metadata. Never
    exposes secrets — only the MC task id, workflow id, operation, poll state
    and the result summary that already lives on the run. Degrades to an empty
    list on any read error so the dashboard tab never blanks."""
    try:
        conn = kanban_db.connect(board=board)
    except Exception as exc:
        return {"tasks": [], "stale": str(exc) or "kanban board unavailable"}
    try:
        rows = conn.execute(
            """
            SELECT
                t.id          AS id,
                t.title       AS title,
                t.assignee    AS assignee,
                t.status      AS status,
                t.created_at  AS created_at,
                r.metadata    AS run_metadata,
                r.summary     AS run_summary,
                COALESCE(r.ended_at, r.started_at) AS updated_at
              FROM tasks t
              LEFT JOIN task_runs r
                ON r.id = (
                    SELECT id FROM task_runs
                     WHERE task_id = t.id
                     ORDER BY started_at DESC, id DESC
                     LIMIT 1
                )
             WHERE t.assignee LIKE ?
               AND t.status != 'archived'
             ORDER BY t.created_at DESC
             LIMIT 100
            """,
            (kanban_db.OPENCLAW_ASSIGNEE_PREFIX + "%",),
        ).fetchall()
    except Exception as exc:
        conn.close()
        return {"tasks": [], "stale": str(exc) or "kanban read failed"}

    out: list[dict[str, Any]] = []
    try:
        for row in rows:
            oc = _openclaw_meta_from_run(row["run_metadata"])
            result_summary = row["run_summary"]
            out.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "agent": _agent_from_assignee(row["assignee"]),
                    "status": row["status"],
                    "mc_task_id": oc.get("mc_task_id"),
                    "workflow_id": oc.get("workflow_id"),
                    "poll_state": oc.get("poll_state"),
                    "result_summary": result_summary,
                    "updated_at": row["updated_at"] if row["updated_at"] is not None else row["created_at"],
                }
            )
    finally:
        conn.close()

    return {"tasks": out}


def register_openclaw_dispatch_routes(app: Any) -> None:
    """Register the OpenClaw dispatch API routes before the SPA catch-all."""

    @app.post("/api/openclaw/dispatch")
    def openclaw_dispatch(body: OpenClawDispatchBody) -> Any:
        return create_openclaw_dispatch(body)

    @app.get("/api/openclaw/dispatched")
    def openclaw_dispatched(board: Optional[str] = None) -> Any:
        return list_openclaw_dispatched(board=board)

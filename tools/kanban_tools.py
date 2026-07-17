"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session — including the
``default`` Orchestrator profile while it lacks the ``kanban`` toolset — still
sees **zero** kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import check_goal_mode_completion
from hermes_cli.kanban_decompose import _VALID_TASK_KINDS
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (explicit orchestrator/board-router profiles only; the default
       Orchestrator profile remains conversation/planning-only unless a later
       config gate deliberately adds this toolset).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - Phase A (progress): the runtime's activity description ("welches Tool
#     läuft gerade") rides along as the heartbeat note so /workers/active
#     can answer "macht gerade: X" — the explicit tool still carries
#     model-supplied notes.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_AUTO_HEARTBEAT_NOTE_MAX_CHARS = 200
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env(note: Optional[str] = None) -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                clipped = (
                    str(note).strip()[:_AUTO_HEARTBEAT_NOTE_MAX_CHARS] or None
                    if note is not None else None
                )
                kb.heartbeat_worker(conn, tid, note=clipped, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def record_current_worker_model_route_from_env(
    *,
    provider: Optional[str],
    model: Optional[str],
    state: str,
    source: str,
    observed_at: Optional[int] = None,
    api_request_id: Optional[str] = None,
) -> bool:
    """Best-effort runtime model telemetry for the current kanban run.

    Unlike the activity heartbeat this is intentionally not rate-limited: the
    provider may fall back between two adjacent API requests, and the board
    must reflect that route change immediately. The DB writer owns stale-run
    rejection and event deduplication. This bridge never raises into the model
    loop.
    """
    tid = os.environ.get("HERMES_KANBAN_TASK")
    run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not tid or not run_id_raw:
        return False
    try:
        run_id = int(run_id_raw)
    except (TypeError, ValueError):
        return False
    try:
        kb, conn = _connect()
        try:
            return bool(
                kb.update_run_model_route(
                    conn,
                    tid,
                    run_id,
                    provider=provider,
                    model=model,
                    state=state,
                    source=source,
                    observed_at=observed_at,
                    api_request_id=api_request_id,
                )
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        logger.debug("model-route bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            def _comment_dict(c):
                return {
                    "author": c.author, "body": c.body,
                    "created_at": c.created_at,
                    "kind": getattr(c, "kind", "comment"),
                }

            def _event_dict(e):
                return {
                    "kind": e.kind, "payload": e.payload,
                    "created_at": e.created_at, "run_id": e.run_id,
                }

            mode = str(args.get("mode") or "worker_slim").strip()
            if mode not in {"worker_slim", "full"}:
                return tool_error("mode must be one of: worker_slim, full")

            if mode == "worker_slim" and os.environ.get("HERMES_KANBAN_TASK"):
                return json.dumps({
                    "task": {
                        "id": task.id,
                        "title": task.title,
                        "assignee": task.assignee,
                        "status": task.status,
                        "tenant": task.tenant,
                        "priority": task.priority,
                        "workspace_kind": task.workspace_kind,
                        "workspace_path": task.workspace_path,
                        "current_run_id": task.current_run_id,
                        "model_override": task.model_override,
                    },
                    "parents": parents,
                    "children": children,
                    "comments": [_comment_dict(c) for c in comments[-8:]],
                    "events": [_event_dict(e) for e in events[-10:]],
                    "runs": [_run_dict(r) for r in runs[-3:]],
                    "truncated": {
                        "comments_omitted": max(0, len(comments) - 8),
                        "events_omitted": max(0, len(events) - 10),
                        "runs_omitted": max(0, len(runs) - 3),
                    },
                    # Also surface the worker's own context block so the
                    # agent can include it directly if it wants. This is
                    # the same string build_worker_context returns to the
                    # dispatcher at spawn time.
                    "worker_context": kb.build_worker_context(conn, tid, profile="worker_slim"),
                })

            return json.dumps({
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "comments": [_comment_dict(c) for c in comments],
                "events": [_event_dict(e) for e in events[-50:]],   # cap; full log via CLI
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    # Versioned-bundle gate (PlanSpec C landing): when the worker opts into the
    # structured completion bundle (metadata carries schema_version), that bundle
    # must be complete. Reject IN-FLIGHT and echo the missing fields back so the
    # worker re-completes within this run — instead of landing a half-filled
    # bundle that a downstream gate would auto-block (no Reject→Auto-Block loop).
    from hermes_cli.disposition import validate_completion_bundle

    bundle_missing = validate_completion_bundle(metadata)
    if bundle_missing:
        return tool_error(
            "kanban_complete blocked: the completion bundle is missing required "
            "field(s): " + ", ".join(bundle_missing) + ". Your task is still "
            "in-flight (no state change). Add the field(s) to metadata and call "
            "kanban_complete again. Required bundle shape: "
            '{"schema_version": 1, "gates": {"exit_code": 0}, '
            '"AC": "<how each AC is met>", "residual_risk": "<one line>"}.'
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Shared with the CLI `kanban complete` verb via
            # hermes_cli.goals.check_goal_mode_completion so the two
            # enforcement points can't diverge (SYNC NOTE in
            # hermes_cli/kanban.py:_cmd_complete points back here).
            task = kb.get_task(conn, tid)
            if task and task.goal_mode:
                rejection = check_goal_mode_completion(
                    task_id=tid,
                    task_title=task.title,
                    task_body=task.body,
                    handoff_text=(summary or result or ""),
                )
                if rejection:
                    return tool_error(rejection)

            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                    # Opt in to the Phase 2 review gate: code-bearing
                    # completions get parked in 'review' for an independent
                    # verifier. complete_task() decides per-task (gate enabled,
                    # code role, not already a verifier/review run); a verifier
                    # approving its own review run still goes terminal 'done'.
                    review_gate=True,
                )
            except kb.ArtifactPreservationError as artifact_err:
                return tool_error(
                    f"kanban_complete could not preserve the declared artifacts: "
                    f"{artifact_err}. Your task is still in-flight and its "
                    f"scratch workspace was kept. Fix the artifact path or "
                    f"storage error, then retry kanban_complete with the same handoff."
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            except kb.WorkerGateError as gate_err:
                return tool_error(
                    f"kanban_complete blocked: worker gate failed on "
                    f"`{gate_err.command}` (exit {gate_err.returncode}). Your task "
                    f"is still in-flight (no state change). Fix the failure in your "
                    f"workspace, then call kanban_complete again.\n\n"
                    f"--- failing output (tail) ---\n{gate_err.output_tail}"
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    # B: structured reviewer findings (optional) → reviewer_metadata so the
    # auto-retry feedback can render them for the coder. Absent → None = today.
    bf = args.get("blocking_findings")
    rv = args.get("required_verification")
    reviewer_metadata = None
    if bf or rv:
        reviewer_metadata = {
            "verdict": "REQUEST_CHANGES",
            "blocking_findings": [str(x) for x in (bf or []) if str(x).strip()],
            "required_verification": [str(x) for x in (rv or []) if str(x).strip()],
        }
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        task = kb.get_task(conn, tid)
        if task and task.goal_mode and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS:
            conn.close()
            return tool_error(
                f"goal_mode tasks can only block with kind in "
                f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). "
                f"If the task is actually finished or cannot proceed for "
                f"another reason, call kanban_complete instead — the "
                f"completion judge will evaluate it."
            )
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind=kind,
                expected_run_id=_worker_run_id(tid),
                reviewer_metadata=reviewer_metadata,
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    # B3: optional token sampling — int-coerce, None-safe.
    def _to_int_or_none(val: object) -> Optional[int]:
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    input_tokens = _to_int_or_none(args.get("input_tokens"))
    output_tokens = _to_int_or_none(args.get("output_tokens"))
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_continue(args: dict, **kw) -> str:
    """Close the current run as iteration-budget exhausted and requeue/block."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    if not summary or not str(summary).strip():
        return tool_error(
            "summary is required — explain what was completed and where the "
            "next run should continue"
        )
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.record_iteration_budget_exhausted(
                conn,
                tid,
                summary=str(summary),
                metadata=metadata,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not continue {tid} (unknown id, not running, or run guard mismatch)"
                )
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=task.status if task else None,
                continuation_count=(
                    task.continuation_count if task else None
                ),
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_continue: {e}")
    except Exception as e:
        logger.exception("kanban_continue failed")
        return tool_error(f"kanban_continue: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


_RECOVERY_ROOT_REF_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
_SERIAL_RECOVERY_STOP_THRESHOLD = 2
_SERIAL_RECOVERY_STOP_WINDOW_SECONDS = 2 * 3600
# The new card must itself read as a RECOVERY attempt — not merely mention a
# task id in passing (an audit/doc/report card citing a t_<id> is not a
# recovery card). Cross-family review finding 4 (2026-07-17): the original
# guard fired on ANY t_<id> mention, false-positiving on legitimate follow-ups
# that happen to cite an id with superseded history.
# retr(?:y\w*|ies|ied) (not retr(?:y|ies|ied)): the previous inner group's
# trailing \b was fine for "retry"/"retried"/"retries" but silently missed
# "retrying" — "retr" + "y" matched, then \b failed between "y" and "i"
# (both word chars), so the whole alternative was rejected (2026-07-17 pass 3
# review finding). y\w* lets the inflection continue (retrying, retryable,
# ...) while still requiring the overall \b...\b word-boundary wrap.
_RECOVERY_MARKER_RE = re.compile(
    r"(?i)\b(recover(?:y|ing|ed)?|retr(?:y\w*|ies|ied)|re-?dispatch|supersed(?:ed|es)?|"
    r"erneut|nochmal|wiederhol\w*|failure|fehlgeschlagen)\b"
)


def _escape_like(text: str) -> str:
    """Escape ``%``/``_``/``\\`` for a SQL ``LIKE ... ESCAPE '\\'`` pattern."""
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _serial_recovery_stop_reason(
    conn: Any, *, title: str, body: Optional[str], created_by: str,
) -> Optional[str]:
    """Refuse a new recovery card when >=2 SUPERSEDED-archived predecessors for
    the same root already exist within the last 2h — a bounded stop against
    an agent looping on 'create a fresh recovery card' after each supersede
    instead of escalating.

    Gates before this ever queries the DB: (a) the NEW card must itself
    read as a recovery attempt (``_RECOVERY_MARKER_RE``), not merely mention a
    task id — an audit/doc card citing a ``t_<id>`` with superseded history is
    not a recovery loop; (b) root references are ``t_<8-hex>`` task ids found
    in the new card's own title/body, matched via an ESCAPEd LIKE (a bare
    ``%t_deadbeef%`` would also match ``tXdeadbeef`` — SQL LIKE's ``_`` is a
    single-char wildcard) against the same created_by's archived cards from
    the same window that were actually archived via the SUPERSEDED path
    (kanban_db.reason_should_archive_as_superseded, the same classifier
    ``block_task`` uses to emit ``superseded_archived``).

    A THIRD gate applies to each candidate predecessor row: (c) the
    predecessor's own title/body must ALSO read as a recovery attempt
    (``_RECOVERY_MARKER_RE``) — cross-family review finding (2026-07-17 pass
    3): the LIKE match alone only proves the predecessor MENTIONS the root id,
    not that it WAS a recovery attempt. Two superseded AUDIT-style cards that
    merely cite the root (e.g. an incident writeup) must not count toward the
    threshold and block the first genuine recovery card.

    Returns the deterministic error string, or ``None`` when creation may proceed.
    """
    text = f"{title}\n{body or ''}"
    if not _RECOVERY_MARKER_RE.search(text):
        return None
    root_refs = set(_RECOVERY_ROOT_REF_RE.findall(text))
    if not root_refs:
        return None
    cutoff = int(time.time()) - _SERIAL_RECOVERY_STOP_WINDOW_SECONDS
    for root_ref in root_refs:
        like_pattern = f"%{_escape_like(root_ref)}%"
        rows = conn.execute(
            "SELECT DISTINCT t.id, t.title, t.body FROM tasks t "
            "JOIN task_events e ON e.task_id = t.id AND e.kind = 'superseded_archived' "
            "WHERE t.status = 'archived' AND t.created_by = ? AND t.created_at >= ? "
            "AND (t.title LIKE ? ESCAPE '\\' OR t.body LIKE ? ESCAPE '\\')",
            (created_by, cutoff, like_pattern, like_pattern),
        ).fetchall()
        recovery_rows = [
            r for r in rows
            if _RECOVERY_MARKER_RE.search(f"{r['title']}\n{r['body'] or ''}")
        ]
        if len(recovery_rows) >= _SERIAL_RECOVERY_STOP_THRESHOLD:
            return (
                "serial-recovery stop: >=2 superseded Vorgaenger fuer dieselbe "
                "Wurzel — eskaliere an den Operator statt neue Karten zu bauen"
            )
    return None


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    # Reject an assignee that is not a runnable Hermes profile. The
    # dispatcher's profile_exists gate would otherwise skip the task on every
    # tick, so catch it here and give the worker immediate feedback instead of
    # letting it fan out a card that can never spawn. Alias normalization is
    # centralized in kanban_db so back-compat names such as ``coder-claude``
    # resolve consistently.
    try:
        from hermes_cli import kanban_db
        assignee = kanban_db.validate_spawnable_assignee(str(assignee))
    except ValueError as exc:
        return tool_error(str(exc))
    body = args.get("body")
    # B1a: do NOT pre-mutate assignee/kind here — create_task owns the write.
    # Preview only so we can surface the contract warning after create (using
    # the *original* assignee/kind; re-running post-create would no-op).
    _orig_assignee_for_contract = str(assignee)
    _orig_kind_for_contract = args.get("kind")
    _, _, inventory_lane_contract = kanban_db.apply_inventory_lane_contract(
        assignee=_orig_assignee_for_contract,
        title=str(title).strip(),
        body=body,
        kind=_orig_kind_for_contract,
    )
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    project_id = args.get("project") or args.get("project_id")
    priority = args.get("priority")
    # Resolve workspace. If the caller passed one explicitly, honor it.
    # Otherwise, a dispatcher-spawned worker (HERMES_KANBAN_TASK set)
    # inherits its own running task's workspace, so a worker editing a
    # dir:/worktree project that spawns a follow-up child keeps the child
    # in that project instead of a throwaway scratch dir. Orchestrators
    # (kanban toolset, no HERMES_KANBAN_TASK) and CLI/dashboard callers
    # fall back to scratch as before. Explicit None path stays None.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    _inherit_workspace = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    max_iterations = args.get("max_iterations")
    max_continuations = args.get("max_continuations")
    initial_status = args.get("initial_status") or "running"
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    kind = args.get("kind")
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    # B3a: dispatcher-spawned workers must not create orphan recovery roots.
    # Empty parents → auto-parent to the current task (same recovery leaf chain).
    auto_parented = False
    worker_tid = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if worker_tid and not list(parents):
        parents = [worker_tid]
        auto_parented = True
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # B3a: only auto-parent when the worker task exists on this board
            # (cross-HERMES_HOME tests / multi-board won't invent a dangling parent).
            if auto_parented:
                if kb.get_task(conn, worker_tid) is None:
                    parents = []
                    auto_parented = False
            # Inherit the spawning worker's own task workspace when the
            # caller didn't specify one (see resolution note above).
            if _inherit_workspace:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.workspace_kind:
                        workspace_kind = _self_task.workspace_kind
                        workspace_path = _self_task.workspace_path
            _created_by = os.environ.get("HERMES_PROFILE") or "worker"
            # Cross-family review finding 5 (2026-07-17): re-check as the LAST
            # thing before create_task (not earlier in this handler) and under
            # an explicit IMMEDIATE write lock (kb.write_txn), so a second
            # concurrent _handle_create for the same root can't observe the
            # same stale "not yet stopped" snapshot this one just saw — SQLite
            # serializes IMMEDIATE writers. This does NOT make check+insert
            # fully atomic: create_task() opens its OWN write_txn immediately
            # after (nesting write_txn here would raise — "BEGIN within a
            # transaction" is a documented anti-pattern in this codebase), so
            # a sub-millisecond gap between the two transactions remains.
            # Accepted: this is a bounded-economy guard against a looping
            # agent, not a hard security boundary — full atomicity would need
            # an in-txn create_task variant, out of scope here.
            with kb.write_txn(conn):
                _stop_reason = _serial_recovery_stop_reason(
                    conn, title=str(title).strip(), body=body, created_by=_created_by,
                )
            if _stop_reason:
                return tool_error(_stop_reason)
            # (a) planspec_source-Vererbung: resolve BEFORE create_task so a
            # racing idempotent hit can never see a half-applied source, and
            # so conflicting parent sources are decided deterministically
            # up front rather than "silently take the first" (cross-family
            # review finding 6). A child of a PlanSpec-ingested parent
            # inherits the parent's planspec_source, so downstream provenance
            # (e.g. project_strategist_outcomes) still resolves the lever for
            # cards spawned mid-chain, not just the ingested ones. If parents
            # disagree on the source, inherit nothing rather than guess.
            _parent_sources = {
                src
                for _pid in parents
                if (src := kb.planspec_source_for_task(conn, _pid))
            }
            _planspec_source_to_inherit = (
                next(iter(_parent_sources)) if len(_parent_sources) == 1 else None
            )
            # An idempotency_key that already resolves to an existing,
            # non-archived task means create_task() below will return that
            # EXISTING task's id, not create a new one (see create_task's own
            # "Idempotency check" fast path). Never touch a pre-existing
            # task's planspec_source on a retry — that would corrupt an
            # unrelated task's provenance every time this is called again
            # with different parents.
            _is_idempotent_hit = bool(idempotency_key) and conn.execute(
                "SELECT 1 FROM tasks WHERE idempotency_key = ? "
                "AND status != 'archived' LIMIT 1",
                (idempotency_key,),
            ).fetchone() is not None
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                max_iterations=(
                    int(max_iterations) if max_iterations is not None else None
                ),
                max_continuations=(
                    int(max_continuations) if max_continuations is not None else None
                ),
                skills=skills,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                initial_status=str(initial_status),
                created_by=_created_by,
                session_id=session_id,
                kind=str(kind).strip().lower() if kind is not None else None,
            )
            if _planspec_source_to_inherit and not _is_idempotent_hit:
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET planspec_source = ? WHERE id = ?",
                        (_planspec_source_to_inherit, new_tid),
                    )
            new_task = kb.get_task(conn, new_tid)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            payload = {
                "task_id": new_tid,
                "status": new_task.status if new_task else None,
                "subscribed": subscribed,
            }
            if inventory_lane_contract:
                payload["inventory_lane_contract"] = inventory_lane_contract
                payload["assignee"] = new_task.assignee if new_task else assignee
            if auto_parented:
                payload["auto_parent"] = worker_tid
                payload["parent_contract"] = (
                    "worker_create_requires_parent: empty parents auto-linked "
                    f"to HERMES_KANBAN_TASK={worker_tid}"
                )
            return _ok(**payload)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``
      and ``HERMES_SESSION_CHAT_ID`` are set in ContextVars by the
      messaging gateway before agent dispatch. The notification poller
      already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the TUI notification
      poller (``tui_gateway/server.py``) reads ``kanban_notify_subs``
      for these rows and posts the completion message into the running
      session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        notifier_profile = os.environ.get("HERMES_PROFILE")

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id,
            notifier_profile=notifier_profile,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            return _ok(task_id=str(tid), status="ready")
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_rewire_superseding_review(args: dict, **kw) -> str:
    """Explicitly replace a superseded review parent edge with a new review."""
    guard = _require_orchestrator_tool("kanban_rewire_superseding_review")
    if guard:
        return guard
    required = ["source_task", "old_review_task", "new_review_task", "reason"]
    missing = [name for name in required if not args.get(name)]
    if missing:
        return tool_error("missing required args: " + ", ".join(missing))
    try:
        kb, conn = _connect()
        try:
            payload = kb.rewire_superseding_review_parent(
                conn,
                source_task=str(args["source_task"]),
                old_review_task=str(args["old_review_task"]),
                new_review_task=str(args["new_review_task"]),
                reason=str(args["reason"]),
            )
            return _ok(**payload)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_rewire_superseding_review: {e}")
    except Exception as e:
        logger.exception("kanban_rewire_superseding_review failed")
        return tool_error(f"kanban_rewire_superseding_review: {e}")


def _handle_ensure_needs_revision_fix(args: dict, **kw) -> str:
    """Create/return the deterministic fix task for a NEEDS_REVISION verdict."""
    guard = _require_orchestrator_tool("kanban_ensure_needs_revision_fix")
    if guard:
        return guard
    required = ["source_task", "review_task", "reviewer_metadata", "reason"]
    missing = [name for name in required if not args.get(name)]
    if missing:
        return tool_error("missing required args: " + ", ".join(missing))
    reviewer_metadata = args.get("reviewer_metadata")
    if not isinstance(reviewer_metadata, dict):
        return tool_error("reviewer_metadata must be an object")
    try:
        kb, conn = _connect()
        try:
            payload = kb.ensure_needs_revision_fix_task(
                conn,
                source_task=str(args["source_task"]),
                review_task=str(args["review_task"]),
                reviewer_metadata=reviewer_metadata,
                reason=str(args["reason"]),
            )
            return _ok(**payload)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_ensure_needs_revision_fix: {e}")
    except Exception as e:
        logger.exception("kanban_ensure_needs_revision_fix failed")
        return tool_error(f"kanban_ensure_needs_revision_fix: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
            "mode": {
                "type": "string",
                "enum": ["worker_slim", "full"],
                "description": (
                    "worker_slim (default) returns bounded comments/events/runs "
                    "for dispatcher-spawned workers; full preserves the legacy "
                    "diagnostic payload."
                ),
            },
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Review-originated runs MUST include "
                    "``metadata.review_verdict`` with APPROVED or "
                    "REQUEST_CHANGES; free-form summary text never authorizes "
                    "a terminal review transition. The kanban DB normalizes "
                    "structured synonyms only for review-originated runs. "
                    "Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. This is "
                    "ALSO how you keep a file alive: a scratch "
                    "workspace is deleted the moment you complete, but "
                    "each listed artifact under the workspace is copied "
                    "to ~/.hermes/reports/by-task/<task-id>/ first. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk at completion. Files inside a managed scratch "
                    "workspace are copied to durable task attachments before "
                    "cleanup; a missing declared scratch artifact keeps the "
                    "task in-flight so you can fix the path and retry."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to why you are stuck. "
        "Use ``kind`` to distinguish a dependency, needed human input, a "
        "capability wall, or a transient failure. ``reason`` is shown on the "
        "board and included in context when someone unblocks you. "
        "Use for genuine blockers only — don't block on things you can "
        "resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered, in one or two sentences. "
                    "Don't paste the whole conversation; the human has "
                    "the board and can ask follow-ups via comments. Reviewer "
                    "runs can put NEEDS_REVISION/REQUEST_CHANGES in the reason; "
                    "the kanban DB normalizes it into task_runs.verdict only "
                    "when the run originated from review."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient"],
                "description": (
                    "Why you are blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Goal-mode tasks may use only dependency or needs_input."
                ),
            },
            "blocking_findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Reviewer use (B): the concrete, actionable findings the "
                    "coder must fix before re-submitting — one per item."
                ),
            },
            "required_verification": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Reviewer use (B): the checks the coder must re-run before "
                    "re-submitting (e.g. a failing test or gate)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_CONTINUE_SCHEMA = {
    "name": "kanban_continue",
    "description": (
        "Report that your current worker run exhausted its tool-calling "
        "iteration budget before the task was complete. Hermes closes the "
        "current run as iteration_budget_exhausted and either requeues the "
        "task for a bounded continuation or blocks it when the task's "
        "continuation cap is exhausted/disabled. Use only for genuine "
        "iteration-budget exhaustion; use kanban_complete when done or "
        "kanban_block for human-input blockers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Required handoff for the next continuation run: what "
                    "you completed, key evidence, and exactly where to resume."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional structured facts for the next run, e.g. "
                    "{\"next_step\": \"run focused pytest\", "
                    "\"changed_files\": [...]}."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["summary"],
    },
}


KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "kind": {
                "type": "string",
                # Derived from _VALID_TASK_KINDS (hermes_cli/kanban_decompose.py)
                # instead of hard-coded, so this schema can't silently drift from
                # the CLI's `--kind` choices (regression: this enum previously
                # omitted 'analysis', the read-only counter-class to 'code').
                "enum": sorted(_VALID_TASK_KINDS),
                "description": (
                    "Coarse task lane. Use 'code' only for implementation "
                    "work assigned to code lanes such as coder, coder-claude, "
                    "or premium; reviewer/critic/research lanes are "
                    "verdict/research-only. 'analysis' is the read-only "
                    "counter-class to 'code'."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "description": (
                    "Per-task worker turn/tool budget override. The dispatcher "
                    "passes it as --max-turns and HERMES_MAX_ITERATIONS so hard "
                    "coder cards can finish without hitting the profile default."
                ),
            },
            "max_continuations": {
                "type": "integer",
                "description": (
                    "How many automatic continuation attempts are allowed after "
                    "explicit iteration-budget exhaustion. Defaults to board policy."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_REWIRE_SUPERSEDING_REVIEW_SCHEMA = {
    "name": "kanban_rewire_superseding_review",
    "description": (
        "Orchestrator-only explicit helper for first-class superseding review "
        "relations. Removes the old review parent edge from source_task, adds "
        "new_review_task as the parent, and writes an audit event with "
        "source_task, old_review_task, new_review_task, old_parent_removed, "
        "new_parent_added, and reason. Does not unblock or complete the source."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_task": {"type": "string", "description": "Task whose parent review edge is rewired."},
            "old_review_task": {"type": "string", "description": "Superseded review task id."},
            "new_review_task": {"type": "string", "description": "Superseding review task id."},
            "reason": {"type": "string", "description": "Human-readable audit reason."},
        },
        "required": ["source_task", "old_review_task", "new_review_task", "reason"],
    },
}

KANBAN_ENSURE_NEEDS_REVISION_FIX_SCHEMA = {
    "name": "kanban_ensure_needs_revision_fix",
    "description": (
        "Orchestrator-only helper that deterministically creates or returns the "
        "idempotent fix task for a Reviewer NEEDS_REVISION verdict. The source "
        "task remains blocked/pending until a later explicit finalization gate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_task": {"type": "string", "description": "Original source task id."},
            "review_task": {"type": "string", "description": "Reviewer task that returned NEEDS_REVISION."},
            "reviewer_metadata": {"type": "object", "description": "Reviewer metadata with verdict=NEEDS_REVISION."},
            "reason": {"type": "string", "description": "Audit reason for creating/returning the fix task."},
        },
        "required": ["source_task", "review_task", "reviewer_metadata", "reason"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_continue",
    toolset="kanban",
    schema=KANBAN_CONTINUE_SCHEMA,
    handler=_handle_continue,
    check_fn=_check_kanban_mode,
    emoji="▶",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_rewire_superseding_review",
    toolset="kanban",
    schema=KANBAN_REWIRE_SUPERSEDING_REVIEW_SCHEMA,
    handler=_handle_rewire_superseding_review,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🔀",
)

registry.register(
    name="kanban_ensure_needs_revision_fix",
    toolset="kanban",
    schema=KANBAN_ENSURE_NEEDS_REVISION_FIX_SCHEMA,
    handler=_handle_ensure_needs_revision_fix,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🛠",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)

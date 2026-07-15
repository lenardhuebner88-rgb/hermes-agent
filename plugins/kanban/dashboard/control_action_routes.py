"""Operator worker-control and repair action routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

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



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)

"""Cost, reliability, evidence, funnel, and strategist readmodel routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

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



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)


"""Decision queue, release controls, and epic extension routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

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
            "SELECT e.task_id, t.title AS task_title, e.created_at, e.payload "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'auto_release' ORDER BY e.created_at DESC, e.id DESC LIMIT 10",
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
                    "task_title": row["task_title"],
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



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)

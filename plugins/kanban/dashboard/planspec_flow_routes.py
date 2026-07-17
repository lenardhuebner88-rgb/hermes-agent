"""PlanSpec, Flow, release, and delivery dashboard extension routes.

This module executes against the stable dashboard API context but owns all
local PlanSpec/Flow handler definitions.  The public plugin module imports it
at the former declaration point, preserving route order and compatibility
exports without keeping these handlers in the upstream-core file.
"""

from __future__ import annotations

# ``extension_runtime.load_api_extension`` injects the parent API names and
# ``_API_CONTEXT_NAMES`` before executing this module.

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

    # Additive-hold invariant (cross-family review finding, 2026-07-17 pass 3):
    # a PlanSpec root can be dual-held (``live_test_depth: ui-real`` AND
    # ``freigabe: operator``, both set at decompose time — see
    # ``decompose_triage_task``). This function itself OWNS and resolves the
    # freigabe:operator side (the trailing ``release_freigabe_hold`` call
    # below acks + flips it within this same request) — that side is not a
    # "foreign" hold and must not gate this loop, or every first flow-release
    # on a freigabe:operator-only root would wrongly skip its own release.
    # The ui-real side is different: flow-release has no lever for it at all
    # (only ``release_uireal_root`` acks it, a separate operator action). So
    # only an outstanding ui-real hold on a still-'scheduled' root must block
    # the child-unblock loop — releasing children here while ui-real is
    # un-acked (as this function used to, unconditionally) is exactly the
    # additive-hold bypass: children go live behind a hold this call cannot
    # itself clear.
    root_row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (root_id,),
    ).fetchone()
    chain_held = bool(
        root_row is not None
        and root_row["status"] == "scheduled"
        and kanban_db._uireal_hold_still_active(conn, root_id)
    )

    released: list[str] = []
    if not chain_held:
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
    if chain_held:
        # Truthfully record that this call found an outstanding co-hold and
        # promoted nothing (released is empty), not just an idempotent
        # "nothing was scheduled" no-op.
        event_payload["chain_held"] = True
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
    if chain_held:
        result["chain_held"] = True
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

            # --- Release the freigabe hold (root flip only — see below) -----------
            # Moved BEFORE scout injection (cross-family review finding, 2026-07-17
            # pass 3): _release_freigabe_hold_root_in_txn does NOT guarantee the
            # root was actually flipped — ui-real and freigabe:operator are
            # ADDITIVE holds (see its docstring): if the root also carries an
            # unreleased ui-real hold, the freigabe ack is recorded but the root
            # stays 'scheduled'. Computing root_fully_released FIRST lets the
            # scout-injection block below gate on it, so a dual-held root that is
            # only half-released never gets a dispatchable scout ahead of the
            # still-outstanding co-hold.
            released = kanban_db._release_freigabe_hold_root_in_txn(conn, root_task_id, author="operator")
            if not released:
                raise HTTPException(
                    status_code=409,
                    detail={"error": f"{root_task_id} konnte nicht freigegeben werden"},
                )
            # Re-read the committed status to know whether promotion is due.
            root_status_row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (root_task_id,),
            ).fetchone()
            root_fully_released = (
                root_status_row is not None and root_status_row["status"] == "todo"
            )

            if should_inject_scout and scout_entry_children and root_fully_released:
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
                    # creating a second one, even outside the write-lock race window
                    # — including the case where the FIRST approve (dual-held root)
                    # skipped the scout and a LATER approve (after the co-hold was
                    # released) is the one that actually creates it.
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

        # --- Post-commit follow-ups (children + auto-scout) ------------------------
        # release_freigabe_hold's own child-unblock/recompute_ready/auto-scout tail
        # runs OUTSIDE its root write_txn (unblock_task/recompute_ready open their
        # own write_txns — nested write_txn is the same documented pitfall as
        # above). Duplicated here (not a call to release_freigabe_hold) because the
        # root flip already happened in our transaction above; calling the public
        # wrapper again would just re-stamp a redundant idempotent
        # 'freigabe_released' event for every approve. Mirrors
        # release_freigabe_hold's tail exactly — including its co-hold guard: if
        # the ui-real hold is still active the root stayed 'scheduled', so the
        # chain's children must stay held too (no unblock/recompute/scout).
        if root_fully_released:
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



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)

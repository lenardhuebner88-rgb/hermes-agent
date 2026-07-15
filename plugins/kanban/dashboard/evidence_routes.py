"""Evidence and operator-answer dashboard extension routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

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



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)


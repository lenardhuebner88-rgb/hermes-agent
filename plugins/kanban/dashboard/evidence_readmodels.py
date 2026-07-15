"""Evidence, artifact, result, and diagnostic dashboard readmodels."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

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

__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)

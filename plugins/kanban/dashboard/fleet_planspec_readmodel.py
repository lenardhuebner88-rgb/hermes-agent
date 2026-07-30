"""Read-only Fleet → Plan projections and transition previews.

This fork-owned edge keeps Plan-specific UI semantics out of the PlanSpec core.
It never creates a task.  The mutation endpoint consumes the preview digest as
an optimistic concurrency token before delegating to the established ingest.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Iterable


PLAN_ACTION_STATES = (
    "draft",
    "ready",
    "held",
    "handed_off",
    "running",
    "blocked",
    "completed",
    "archived",
)


def _status_slug(value: Any) -> str:
    return "-".join(str(value or "").strip().lower().replace("_", "-").split())


def classify_plan_record(record: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(action_state, action_reason, next_action)`` for one record."""

    kanban_state = _status_slug(record.get("kanban_state"))
    record_status = _status_slug(record.get("status"))
    closed_reason = _status_slug(record.get("closed_reason"))
    root_id = str(record.get("kanban_root_task_id") or "").strip()
    valid = bool(record.get("valid"))
    findings = list(record.get("ingest_findings") or []) + list(record.get("errors") or [])

    if kanban_state == "archived":
        return "archived", "Die zugehörige Kette ist archiviert.", "none"
    if kanban_state in {"completed", "done"}:
        return "completed", "Die zugehörige Kette ist abgeschlossen.", "open_result"
    if record_status == "archived" or closed_reason == "archived":
        return "archived", "Die PlanSpec ist archiviert.", "none"
    if record_status in {"closed", "completed", "done", "implemented"}:
        return "completed", "Die PlanSpec ist abgeschlossen.", "open_result"
    if kanban_state == "blocked":
        return "blocked", "Die übergebene Kette ist blockiert.", "open_task"
    if kanban_state == "running":
        return "running", "Die übergebene Kette wird ausgeführt.", "open_task"
    # A CLOSED PlanSpec is a TERMINAL state -- never a blocker and never a draft.
    #
    # The ingest precheck answers "may I hand this to the board?", and for a
    # closed spec "no" is the CORRECT answer (``closed status: shipped``).  That
    # verdict used to fall through to ``ingest_would_block`` below and painted
    # every shipped or obsolete plan red as "Blockiert" -- measured 2026-07-29:
    # 59 of 67 blocked badges were closed specs, and closing one MORE plan made
    # the count go UP.  Likewise an unparseable spec that shipped last month is
    # not a "draft" waiting to be written.
    #
    # Match on ``open``/``closed_reason``, NOT on a set of status strings: the
    # real vault carries free text like "SHIPPED 2026-06-19 (89527c494 -- ...)"
    # and "obsolete -- fix shipped+dogfood-proven 2026-06-18", so any literal
    # enumeration would silently miss them.
    # ``open is False`` -- NOT ``not open``: a record that simply does not carry
    # the field (other callers, fixtures) must stay in the normal flow rather
    # than be silently declared closed.
    #
    # This check sits ABOVE the ``root_id`` branch on purpose. ``kanban_root_task_id``
    # falls back to the PlanSpec *frontmatter* (see hermes_cli.planspecs.list_planspecs),
    # so a shipped plan keeps a root stamp long after its chain is gone. Measured
    # 2026-07-30: all four "Im Board" entries in the live register were closed specs
    # whose stamped root was ``done`` or absent from every board, while
    # ``kanban_state`` read ``not_ingested``. A stamp is provenance, not a live state.
    # Live chain states (archived/completed/blocked/running) are decided above and
    # still outrank a closed source.
    if record.get("open") is False or closed_reason:
        closed_slug = f"{closed_reason} {record_status}"
        if any(mark in closed_slug for mark in ("obsolete", "superseded", "not-needed")):
            return "archived", "Die PlanSpec wurde als nicht mehr nötig geschlossen.", "none"
        return "completed", "Die PlanSpec ist abgeschlossen.", "open_result"
    if root_id:
        if (
            kanban_state == "queued"
            and _status_slug(record.get("kanban_root_status")) == "scheduled"
            and str(record.get("freigabe") or "").strip().lower() == "operator"
        ):
            return "held", "Die Kette wartet auf Operator-Freigabe.", "release"
        return "handed_off", "Der Plan wurde als Kette ans Board übergeben.", "open_task"
    if not valid:
        reason = findings[0] if findings else "Der Plan ist noch nicht bindend."
        return "draft", reason, "edit"
    if bool(record.get("ingest_would_block")):
        reason = findings[0] if findings else "Die Übergabeprüfung meldet Blocker."
        return "blocked", reason, "review_findings"
    return "ready", "Übergabeprüfung ist sauber.", "preview_ingest"


def _spec_dependency_count(spec: Any) -> int:
    return sum(len(list(child.get("planspec_deps") or [])) for child in spec.children)


def enrich_plan_record(record: dict[str, Any], *, planspecs: Any) -> dict[str, Any]:
    """Add Fleet list semantics without weakening malformed-record handling."""

    state, reason, next_action = classify_plan_record(record)
    enriched = {
        **record,
        "action_state": state,
        "action_reason": reason,
        "next_action": next_action,
        "finding_count": len(
            {
                str(item)
                for item in [
                    *(record.get("ingest_findings") or []),
                    *(record.get("errors") or []),
                ]
                if str(item).strip()
            }
        ),
        "dependency_count": 0,
        "target_board": None,
    }
    if not record.get("binding") or not record.get("valid"):
        return enriched
    try:
        spec = planspecs.parse_binding_planspec(record["path"])
    except Exception:
        return enriched
    enriched["dependency_count"] = _spec_dependency_count(spec)
    enriched["target_board"] = spec.board
    return enriched


def summarize_plan_records(
    records: Iterable[dict[str, Any]],
    *,
    observed_at: int | None = None,
) -> dict[str, Any]:
    counts = {state: 0 for state in PLAN_ACTION_STATES}
    total = 0
    for record in records:
        total += 1
        state = str(record.get("action_state") or "draft")
        counts[state if state in counts else "draft"] += 1
    return {
        **counts,
        "total_matching": total,
        "observed_at": int(time.time() if observed_at is None else observed_at),
    }


def build_plan_list_response(
    records: list[dict[str, Any]],
    *,
    planspecs: Any,
    limit: int | None,
) -> dict[str, Any]:
    enriched = [enrich_plan_record(record, planspecs=planspecs) for record in records]
    visible = enriched[:limit] if limit is not None else enriched
    return {
        "planspecs": visible,
        "count": len(visible),
        "summary": summarize_plan_records(enriched),
    }


def _list_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value:
        return [str(value)]
    return []


def _criteria_field(value: Any) -> list[str]:
    """Normalise one authored ``acceptance_criteria`` list to display strings.

    Canon ``planspec-taskgraph.md`` puts acceptance criteria per subtask; the
    plan-level list is the fallback that threads down via ``applies_to``. Both
    forms occur in the real Vault as plain strings and as mappings, so the
    display projection accepts either instead of dropping the mapping form.
    """

    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("statement") or item.get("text") or "").strip()
            marker = str(item.get("id") or "").strip()
            if text:
                out.append(f"{marker}: {text}" if marker else text)
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def build_binding_detail(spec: Any) -> dict[str, Any]:
    """Project the authored contract and executable step details."""

    fm = spec.frontmatter
    raw_ac = fm.get("acceptance_criteria") or []
    acceptance_criteria = []
    if isinstance(raw_ac, list):
        acceptance_criteria = [
            item if isinstance(item, dict) else {"statement": str(item)}
            for item in raw_ac
        ]
    hints_by_id = {subtask.id: subtask for subtask in spec.hints.subtasks}
    subtasks = []
    for child in spec.children:
        step_id = str(child.get("planspec_subtask_id") or "")
        authored = hints_by_id.get(step_id)
        subtasks.append(
            {
                "id": step_id,
                "title": str(child.get("title") or ""),
                "lane": str(child.get("planspec_lane") or child.get("assignee") or ""),
                "review_tier": str(child.get("review_tier") or "") or None,
                "deps": list(child.get("planspec_deps") or []),
                "scope_files": list(getattr(authored, "scope_files", []) or []),
            }
        )
    return {
        "goal": str(fm.get("goal") or fm.get("topic") or spec.topic or ""),
        "acceptance_criteria": acceptance_criteria,
        "anti_scope": _list_field(fm.get("anti_scope")),
        "evidence_required": _list_field(fm.get("evidence_required")),
        "freigabe": spec.freigabe,
        "live_test_depth": spec.live_test_depth,
        "target_board": spec.board,
        "dependency_count": _spec_dependency_count(spec),
        "finding_count": 0,
        "subtasks": subtasks,
    }


def build_readable_plan_detail(path: str | Path, *, planspecs: Any) -> dict[str, Any]:
    """Read one allowed Vault PlanSpec for display without applying ingest gates.

    Detail reading and execution eligibility are deliberately separate. Closed,
    legacy, and partially authored plans remain inspectable; transition preview
    and ingest continue to use the strict binding parser.
    """

    resolved = planspecs.resolve_planspec_path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise planspecs.PlanSpecBlocked(
            [f"planspec is not valid utf-8: {exc}"]
        ) from exc

    try:
        frontmatter, body = planspecs._extract_frontmatter(text)
    except Exception as exc:
        findings = list(getattr(exc, "findings", None) or [str(exc)])
        return {
            "goal": resolved.stem,
            "acceptance_criteria": [],
            "acceptance_criteria_total": 0,
            "anti_scope": [],
            "evidence_required": [],
            "freigabe": "",
            "live_test_depth": "",
            "target_board": None,
            "dependency_count": 0,
            "finding_count": len(findings),
            "subtasks": [],
            "source_state": "invalid",
            "source_findings": findings,
            "full_text": text,
        }

    findings: list[str] = []
    raw_hints = frontmatter.get("taskgraph_hints")
    raw_subtasks: list[Any] = []
    binding = False
    if isinstance(raw_hints, dict):
        binding = bool(raw_hints.get("binding"))
        raw = raw_hints.get("subtasks") or []
        if isinstance(raw, list):
            raw_subtasks = raw
        else:
            findings.append("taskgraph_hints.subtasks must be a list")
    else:
        findings.append("taskgraph_hints must be a YAML mapping")
    if not binding:
        findings.append("taskgraph_hints.binding must be true")

    status = str(frontmatter.get("status") or "").strip()
    closed_reason = planspecs._closed_reason(status)
    if closed_reason:
        findings.append(closed_reason)

    live_test_depth = str(frontmatter.get("live_test_depth") or "smoke").strip()
    if live_test_depth not in planspecs.LIVE_TEST_DEPTHS:
        findings.append(
            "live_test_depth must be one of: "
            + ", ".join(sorted(planspecs.LIVE_TEST_DEPTHS))
        )
    freigabe = str(frontmatter.get("freigabe") or "").strip()
    if not freigabe:
        findings.append("freigabe is required")

    raw_ac = frontmatter.get("acceptance_criteria") or []
    acceptance_criteria = [
        item if isinstance(item, dict) else {"statement": str(item)}
        for item in raw_ac
    ] if isinstance(raw_ac, list) else []

    subtasks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_subtasks):
        if not isinstance(raw, dict):
            findings.append(f"subtask {index + 1} must be a YAML mapping")
            continue
        deps = _list_field(raw.get("deps"))
        subtasks.append(
            {
                "id": str(raw.get("id") or f"step-{index + 1}"),
                "title": str(raw.get("title") or ""),
                "lane": str(raw.get("lane") or ""),
                "review_tier": str(raw.get("review_tier") or "") or None,
                "deps": deps,
                "scope_files": _list_field(raw.get("scope_files")),
                "body": str(raw.get("body") or ""),
                "instructions": str(raw.get("instructions") or ""),
                "kind": str(raw.get("kind") or "") or None,
                "max_iterations": raw.get("max_iterations"),
                "acceptance_criteria": _criteria_field(raw.get("acceptance_criteria")),
            }
        )

    topic = (
        str(frontmatter.get("topic") or "").strip()
        or str(frontmatter.get("title") or "").strip()
        or planspecs._first_heading(body)
        or resolved.stem
    )
    source_state = "closed" if closed_reason else (
        "binding" if binding and not findings else "draft"
    )
    return {
        "goal": str(frontmatter.get("goal") or topic),
        "acceptance_criteria": acceptance_criteria,
        # Plan-level AC is the FALLBACK, not the norm: Canon puts criteria per
        # subtask. Reporting only the plan-level count made the two live open
        # plans -- 23 authored criteria (12+11) and 5 -- render as
        # "Keine Kriterien im Plan" (measured 2026-07-30).
        "acceptance_criteria_total": len(acceptance_criteria)
        + sum(len(item["acceptance_criteria"]) for item in subtasks),
        "anti_scope": _list_field(frontmatter.get("anti_scope")),
        "evidence_required": _list_field(frontmatter.get("evidence_required")),
        "freigabe": freigabe,
        "live_test_depth": live_test_depth,
        "target_board": str(frontmatter.get("board") or "").strip() or None,
        "dependency_count": sum(len(item["deps"]) for item in subtasks),
        "finding_count": len(findings),
        "subtasks": subtasks,
        "source_state": source_state,
        "source_findings": findings,
        "full_text": body.strip(),
    }


def stable_source_digest(path: str | Path, *, planspecs: Any) -> tuple[str, Any]:
    """Parse and hash one stable PlanSpec snapshot.

    The Vault is a trusted local source, but edits may race the dashboard.  File
    identity/mtime/size are checked around parse and read; a moving source is
    rejected instead of previewing one revision and ingesting another.
    """

    resolved = planspecs.resolve_planspec_path(path)
    before = resolved.stat()
    spec = planspecs.parse_binding_planspec(resolved)
    raw = resolved.read_bytes()
    after = resolved.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise RuntimeError("planspec source changed while it was being read")
    return hashlib.sha256(raw).hexdigest(), spec


def build_transition_preview(
    path: str | Path,
    *,
    board: str | None,
    planspecs: Any,
    kanban_db: Any,
) -> dict[str, Any]:
    """Return the exact, write-free Plan → Board transition contract."""

    source_digest, spec = stable_source_digest(path, planspecs=planspecs)
    # Validate the already parsed snapshot. Calling validate_planspec(path)
    # here would read the file a second time and could pair revision A's digest
    # with revision B's findings.
    findings = [
        str(item)
        for item in planspecs._collect_spec_rubric_findings(spec)
    ]
    blocking_findings = planspecs._blocking_spec_rubric_findings(findings)
    signed = planspecs._spec_is_signed(spec)
    role_misuse = any(
        finding.startswith(planspecs._ROLE_MISUSE_PREFIX)
        for finding in findings
    )
    if not findings:
        disposition = "clean"
    elif signed or not blocking_findings:
        disposition = "warn"
    else:
        disposition = "block"
    target_board = str(planspecs._resolve_target_board(spec, board))
    metadata = kanban_db.read_board_metadata(target_board)
    default_workdir = str(metadata.get("default_workdir") or "").strip()
    # Mirror ingest_planspec + decompose_triage_task: the held root is scratch;
    # code children use the board default dir when configured, while explicit
    # child workspace kinds win.
    child_workspace_modes = set()
    for child in spec.children:
        explicit = str(child.get("workspace_kind") or "").strip()
        if explicit:
            child_workspace_modes.add(explicit)
        elif default_workdir and child.get("kind") == "code":
            child_workspace_modes.add("dir")
        else:
            child_workspace_modes.add("scratch")
    if len(child_workspace_modes) == 1:
        workspace_mode = next(iter(child_workspace_modes))
    elif len(child_workspace_modes) > 1:
        workspace_mode = "mixed"
    else:
        workspace_mode = "scratch"
    review_floor = planspecs._spec_max_review_tier(spec)
    lanes_resolvable = not any(
        finding.lower().startswith(("unknown lane:", "cc-instrument as lane"))
        for finding in findings
    )
    would_block = disposition == "block" or role_misuse
    return {
        "source_digest": source_digest,
        "action": "create_held_chain",
        "target_board": target_board,
        "root_cards_created": 1,
        "child_tasks_created": len(spec.children),
        "starts_worker": False,
        "live_test_depth": spec.live_test_depth,
        "workspace_mode": workspace_mode,
        "review_floor": review_floor,
        # Binding ingest creates exactly the authored children; it never
        # synthesizes an additional scouting card.
        "auto_scout_tasks": 0,
        "lanes_resolvable": lanes_resolvable,
        "blocking_findings": findings if would_block else [],
        "warnings": [] if would_block else findings,
        "can_ingest": disposition in {"clean", "warn"} and lanes_resolvable and not would_block,
    }

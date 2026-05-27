"""Discord-first Kanban result report renderer.

The module is intentionally read-only: callers pass an open Kanban SQLite
connection, and the builder only reads tasks, links, runs, comments, and events.
The rendered Markdown follows the recovered Combined Kanban Report Template v1:
human sections first, then one fenced ``json kanban-report-v1`` payload that is
canonical for downstream agents/importers.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from collections import Counter
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb

CONTRACT_VERSION = 1
REPORT_TYPE = "kanban_result_report"
PRODUCER = {"name": "hermes-kanban-discord-report", "version": "0.1.0"}
ACTIVE_STATUSES = {"running", "ready", "todo", "triage", "scheduled"}
DEFAULT_TITLE = "Untitled Kanban report"
DEFAULT_SCOPE_DESCRIPTION = "Hermes Kanban board"


def _utc_now_z() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_generated_at(value: str | None) -> str:
    if not value:
        return _utc_now_z()
    raw = str(value).strip()
    if not raw:
        return _utc_now_z()
    if raw.endswith("Z"):
        return raw
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def _assignee_obj(assignee: str | None) -> dict[str, str]:
    if not assignee:
        return {"slug": "unknown", "kind": "unknown"}
    return {"slug": str(assignee), "kind": "profile"}


def _latest_closed_run(runs: Iterable[kb.Run]) -> kb.Run | None:
    closed = [run for run in runs if run.ended_at is not None]
    if not closed:
        return None
    return closed[-1]


def _latest_summary(task: kb.Task, runs: list[kb.Run]) -> str:
    latest = _latest_closed_run(runs)
    if latest and latest.summary:
        return str(latest.summary)
    if task.result:
        return str(task.result)
    return ""


def _latest_metadata(runs: list[kb.Run]) -> dict[str, Any]:
    latest = _latest_closed_run(runs)
    if latest and isinstance(latest.metadata, dict):
        return latest.metadata
    return {}


def _primary_artifact_or_receipt(metadata: dict[str, Any]) -> str:
    for key in ("receipt_path", "receipt_reference", "diff_path", "artifact_path"):
        value = metadata.get(key)
        if value:
            return str(value)
    artifacts = metadata.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        first = artifacts[0]
        if isinstance(first, dict):
            return str(first.get("ref") or first.get("path") or first.get("url") or "none")
        return str(first)
    return "none"


def _task_artifacts(metadata: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    artifacts = metadata.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict):
                ref = item.get("ref") or item.get("path") or item.get("url")
                if ref:
                    out.append(
                        {
                            "kind": str(item.get("kind") or "file"),
                            "ref": str(ref),
                            "description": str(item.get("description") or "task artifact"),
                        }
                    )
            elif item:
                out.append({"kind": "file", "ref": str(item), "description": "task artifact"})
    for key in ("receipt_path", "receipt_reference", "diff_path", "artifact_path"):
        value = metadata.get(key)
        if value and not any(artifact["ref"] == str(value) for artifact in out):
            out.append({"kind": "file", "ref": str(value), "description": key})
    return out


def _blocked_reason(runs: list[kb.Run], events: list[kb.Event]) -> str:
    for run in reversed(runs):
        if run.outcome == "blocked" and run.summary:
            return str(run.summary)
    for event in reversed(events):
        if event.kind == "blocked" and isinstance(event.payload, dict):
            reason = event.payload.get("reason")
            if reason:
                return str(reason)
    return ""


def _dependency_summary(parent_ids: list[str]) -> str:
    if not parent_ids:
        return "none"
    return ", ".join(f"`{pid}`" for pid in parent_ids)


def _next_step(status: str) -> str:
    if status == "running":
        return "worker is running"
    if status == "ready":
        return "waiting for dispatcher claim"
    if status == "todo":
        return "waiting on parent dependencies"
    if status == "triage":
        return "needs specification"
    if status == "scheduled":
        return "waiting for scheduled time"
    return "not provided"


def _task_sort_key(task: kb.Task) -> tuple[int, int, str]:
    return (-int(task.priority or 0), int(task.created_at or 0), task.id)


def _select_tasks(
    conn,
    *,
    root_task_id: str | None,
    include_archived: bool,
    included_statuses: set[str] | None,
) -> list[kb.Task]:
    tasks = kb.list_tasks(conn, include_archived=include_archived)
    by_id = {task.id: task for task in tasks}
    if root_task_id:
        selected_ids = {root_task_id}
        selected_ids.update(kb.parent_ids(conn, root_task_id))
        selected_ids.update(kb.child_ids(conn, root_task_id))
        tasks = [task for task in tasks if task.id in selected_ids]
        if root_task_id not in by_id:
            raise ValueError(f"no such task: {root_task_id}")
    if included_statuses is not None:
        tasks = [task for task in tasks if task.status in included_statuses]
    return sorted(tasks, key=_task_sort_key)


def build_discord_report(
    conn,
    *,
    board: str | None = None,
    tenant: str | None = None,
    generated_at: str | None = None,
    report_title: str | None = None,
    scope_description: str | None = None,
    root_task_id: str | None = None,
    included_statuses: Iterable[str] | None = None,
    include_archived: bool = False,
    summary: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Combined Kanban Report Template v1 payload from board state."""
    status_filter = {str(status) for status in included_statuses} if included_statuses else None
    tasks = _select_tasks(
        conn,
        root_task_id=root_task_id,
        include_archived=include_archived,
        included_statuses=status_filter,
    )
    task_ids = {task.id for task in tasks}
    count_by_status: Counter[str] = Counter(task.status for task in tasks)
    active_total = sum(1 for task in tasks if task.status in ACTIVE_STATUSES)

    task_payloads: list[dict[str, Any]] = []
    relationships: list[dict[str, str]] = []
    next_actions: list[dict[str, str]] = []

    for task in tasks:
        parent_ids = kb.parent_ids(conn, task.id)
        child_ids = kb.child_ids(conn, task.id)
        runs = kb.list_runs(conn, task.id)
        events = kb.list_events(conn, task.id)
        metadata = _latest_metadata(runs)
        assignee = _assignee_obj(task.assignee)
        task_summary = _latest_summary(task, runs)
        blocked_reason = _blocked_reason(runs, events) if task.status == "blocked" else ""
        task_payload: dict[str, Any] = {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee": assignee,
            "summary": task_summary,
            "parent_ids": parent_ids,
            "child_ids": child_ids,
        }
        if blocked_reason:
            task_payload["blocked_reason"] = blocked_reason
        if task.status in ACTIVE_STATUSES:
            task_payload["next_step"] = _next_step(task.status)
            task_payload["dependency_summary"] = _dependency_summary(parent_ids)
        task_artifacts = _task_artifacts(metadata)
        if task_artifacts:
            task_payload["artifacts"] = task_artifacts
        task_payloads.append(task_payload)

        for child_id in child_ids:
            if child_id in task_ids:
                relationships.append(
                    {"parent_id": task.id, "child_id": child_id, "type": "blocks_until_done"}
                )

        owner = assignee["slug"]
        if task.status == "blocked":
            desc = f"Resolve blocker: {blocked_reason or 'not provided'}"
            next_actions.append(
                {"kind": "unblock", "owner": owner, "description": desc, "task_id": task.id}
            )
        elif task.status in ACTIVE_STATUSES:
            next_actions.append(
                {
                    "kind": "advance",
                    "owner": owner,
                    "description": _next_step(task.status),
                    "task_id": task.id,
                }
            )

    relationships.sort(key=lambda item: (item["parent_id"], item["child_id"]))
    status = "empty" if not task_payloads else "ok"
    error_list = list(errors or [])
    warning_list = list(warnings or [])
    if error_list:
        status = "error"
    if warning_list and status == "ok":
        status = "partial"
    if summary:
        report_summary = str(summary)
    elif not task_payloads:
        report_summary = "No tasks matched the report scope."
    else:
        report_summary = (
            f"Kanban report covers {len(task_payloads)} task(s): "
            f"{count_by_status.get('done', 0)} done, {active_total} active, "
            f"{count_by_status.get('blocked', 0)} blocked."
        )

    structured = {
        "report_type": REPORT_TYPE,
        "contract_version": CONTRACT_VERSION,
        "generated_at": _normalize_generated_at(generated_at),
        "producer": dict(PRODUCER),
        "board": {"id": board or "default", "tenant": tenant},
        "scope": {
            "description": scope_description or DEFAULT_SCOPE_DESCRIPTION,
            "root_task_id": root_task_id,
            "included_statuses": sorted(status_filter) if status_filter is not None else None,
            "filters": {"include_archived": bool(include_archived)},
        },
        "status": status,
        "summary": report_summary,
        "counts": {
            "tasks_total": len(task_payloads),
            "by_status": dict(sorted(count_by_status.items())),
            "active_total": active_total,
        },
        "tasks": task_payloads,
        "relationships": relationships,
        "errors": error_list,
        "warnings": warning_list,
        "next_actions": next_actions,
        "artifacts": list(artifacts or []),
    }
    return {"report_title": report_title or DEFAULT_TITLE, "structured": structured}


def _human_title(title: str) -> str:
    return _truncate_text(title or DEFAULT_TITLE, 120)


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _summary_lines(report: dict[str, Any], *, split_notice: str | None = None) -> list[str]:
    payload = report["structured"]
    counts = payload.get("counts") or {}
    by_status = counts.get("by_status") or {}
    warnings = payload.get("warnings") or []
    errors = payload.get("errors") or []
    risk_line = "Main risk: review warnings/errors before acting." if (warnings or errors) else "Confidence: structured JSON is the source of truth."
    lines = [
        f"**{_human_title(report.get('report_title') or DEFAULT_TITLE)}**",
        f"`{payload['scope']['description']}` · {payload['generated_at']} · {counts.get('tasks_total', 0)} tasks · status: `{payload['status']}`",
        "",
        "**Summary**",
        f"- {payload.get('summary') or 'No summary provided.'}",
        "- "
        f"Done: {by_status.get('done', 0)} · Active: {counts.get('active_total', 0)} · "
        f"Blocked: {by_status.get('blocked', 0)} · Warnings: {len(warnings)} · Errors: {len(errors)}",
        f"- {risk_line}",
    ]
    if split_notice:
        lines.append(f"- {split_notice}")
    return lines


def _warnings_errors_lines(payload: dict[str, Any]) -> list[str]:
    errors = payload.get("errors") or []
    warnings = payload.get("warnings") or []
    if not errors and not warnings:
        return []
    lines = ["", "**Warnings / errors**"]
    for error in errors:
        task_suffix = f" · task: `{error.get('task_id')}`" if error.get("task_id") else ""
        lines.append(f"- `{error.get('code', 'error')}` {error.get('message', '')}{task_suffix}")
        lines.append(
            f"  Impact: {error.get('impact', 'unknown')} · Recoverable: {error.get('recoverable', 'unknown')}"
        )
    for warning in warnings:
        task_suffix = f" · task: `{warning.get('task_id')}`" if warning.get("task_id") else ""
        lines.append(f"- `{warning.get('code', 'warning')}` {warning.get('message', '')}{task_suffix}")
        lines.append(f"  Action: {warning.get('action', 'review structured block')}")
    return lines


def _blocked_lines(payload: dict[str, Any]) -> list[str]:
    blocked = [task for task in payload.get("tasks", []) if task.get("status") == "blocked"]
    lines = ["", "**Blocked**"]
    if not blocked:
        return lines + ["- None."]
    for task in blocked:
        assignee = task.get("assignee") or {"slug": "unknown"}
        reason = task.get("blocked_reason") or "not provided"
        lines.append(
            f"- `{task['id']}` **{_truncate_text(task.get('title'), 100)}** — @{assignee.get('slug', 'unknown')} · `{task.get('status')}`"
        )
        lines.append(f"  Reason: {_truncate_text(reason, 180)} · Needs: review block reason")
    return lines


def _active_lines(payload: dict[str, Any]) -> list[str]:
    active = [task for task in payload.get("tasks", []) if task.get("status") in ACTIVE_STATUSES]
    lines = ["", "**Active / review**"]
    if not active:
        return lines + ["- None."]
    for task in active:
        assignee = task.get("assignee") or {"slug": "unknown"}
        lines.append(
            f"- `{task['id']}` **{_truncate_text(task.get('title'), 100)}** — @{assignee.get('slug', 'unknown')} · `{task.get('status')}`"
        )
        lines.append(
            f"  Next: {_truncate_text(task.get('next_step') or 'not provided', 180)} · Depends on: {task.get('dependency_summary') or 'none'}"
        )
    return lines


def _completed_lines(payload: dict[str, Any]) -> list[str]:
    completed = [task for task in payload.get("tasks", []) if task.get("status") == "done"]
    lines = ["", "**Completed**"]
    if not completed:
        return lines + ["- None."]
    for task in completed:
        assignee = task.get("assignee") or {"slug": "unknown"}
        artifacts = task.get("artifacts") or []
        evidence = artifacts[0].get("ref") if artifacts and isinstance(artifacts[0], dict) else "none"
        lines.append(
            f"- `{task['id']}` **{_truncate_text(task.get('title'), 100)}** — @{assignee.get('slug', 'unknown')}"
        )
        lines.append(
            f"  Result: {_truncate_text(task.get('summary') or 'no summary', 180)} · Evidence: {evidence or 'none'}"
        )
    return lines


def _next_action_lines(payload: dict[str, Any]) -> list[str]:
    actions = payload.get("next_actions") or []
    lines = ["", "**Next actions**"]
    if not actions:
        return lines + ["- None."]
    for idx, action in enumerate(actions, start=1):
        task_suffix = f" · task: `{action.get('task_id')}`" if action.get("task_id") else ""
        lines.append(
            f"{idx}. {action.get('owner') or 'unassigned'}: {_truncate_text(action.get('description'), 180)}{task_suffix}"
        )
    return lines


def _structured_lines(payload: dict[str, Any]) -> list[str]:
    return [
        "",
        "**Structured Report**",
        "```json kanban-report-v1",
        json.dumps(payload, indent=2, ensure_ascii=False),
        "```",
    ]


def _report_lines(report: dict[str, Any], *, split_notice: str | None = None) -> list[str]:
    payload = report["structured"]
    lines = _summary_lines(report, split_notice=split_notice)
    lines.extend(_warnings_errors_lines(payload))
    lines.extend(_blocked_lines(payload))
    lines.extend(_active_lines(payload))
    lines.extend(_completed_lines(payload))
    lines.extend(_next_action_lines(payload))
    lines.extend(_structured_lines(payload))
    return lines


def render_discord_report_markdown(report: dict[str, Any]) -> str:
    """Render one complete Markdown report in the canonical section order."""
    return "\n".join(_report_lines(report)).rstrip() + "\n"


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def _part_prefix(title: str, idx: int, total: int) -> str:
    return f"Part {idx}/{total} — {_human_title(title)}\n"


def _pointer_payload(report: dict[str, Any], artifact_path: str) -> dict[str, Any]:
    original = report["structured"]
    payload = copy.deepcopy(original)
    payload["status"] = "partial" if original.get("status") != "error" else "error"
    payload["tasks"] = []
    payload["relationships"] = []
    warnings = list(payload.get("warnings") or [])
    warnings.append(
        {
            "code": "structured_payload_externalized",
            "message": "Structured payload exceeded the Discord chunk limit and was externalized.",
            "recoverable": True,
            "action": "Read the full structured payload from the artifact reference.",
        }
    )
    payload["warnings"] = warnings
    payload["next_actions"] = []
    artifacts = list(payload.get("artifacts") or [])
    artifacts.append(
        {
            "kind": "file",
            "ref": artifact_path,
            "description": "Full structured kanban-report-v1 payload",
        }
    )
    payload["artifacts"] = artifacts
    payload["summary"] = original.get("summary") or "Structured payload externalized."
    return payload


def _split_body_sections(report: dict[str, Any]) -> list[str]:
    payload = report["structured"]
    sections = [
        _join_lines(_blocked_lines(payload)),
        _join_lines(_active_lines(payload)),
        _join_lines(_completed_lines(payload)),
        _join_lines(_next_action_lines(payload)),
    ]
    return [section for section in sections if section.strip()]


def _pack_chunks_with_prefix(
    chunks: list[str],
    *,
    title: str,
    soft_cap: int,
    footer: str | None,
) -> list[str]:
    # We may need to retry because the Part x/y prefix grows once total is known.
    total = max(1, len(chunks))
    while True:
        packed: list[str] = []
        changed = False
        for idx, chunk in enumerate(chunks, start=1):
            prefix = "" if idx == 1 else _part_prefix(title, idx, total)
            suffix = footer if footer and idx == total else ""
            candidate = prefix + chunk.rstrip() + ("\n" + suffix if suffix else "") + "\n"
            if len(candidate) <= soft_cap:
                packed.append(candidate)
                continue
            # Split only on whole lines. This is a last-resort top-N human truncation
            # path; JSON chunks are checked before this helper is called.
            budget = soft_cap - len(prefix) - len("\n… truncated human detail; see structured report/artifact.\n")
            body = chunk.rstrip()
            if budget > 0 and len(body) > budget:
                candidate = (
                    prefix
                    + body[:budget].rstrip()
                    + "\n… truncated human detail; see structured report/artifact.\n"
                )
                if suffix and len(candidate) + len(suffix) + 1 <= soft_cap:
                    candidate = candidate.rstrip() + "\n" + suffix + "\n"
                packed.append(candidate)
                continue
            changed = True
            half = max(1, len(chunk) // 2)
            chunks = chunks[: idx - 1] + [chunk[:half], chunk[half:]] + chunks[idx:]
            break
        if not changed:
            return packed
        total = len(chunks)


def split_discord_report(
    report: dict[str, Any],
    *,
    soft_cap: int = 1800,
    artifact_path: str | None = None,
) -> list[str]:
    """Split report Markdown for Discord without breaking JSON validity.

    If the complete report fits, a single canonical report is returned. If it
    does not fit, chunk 1 is the executive summary, human detail is chunked by
    section, and one final chunk carries either the complete JSON block or a
    compact pointer payload when the JSON block alone exceeds ``soft_cap``.
    """
    cap = max(200, int(soft_cap))
    full = render_discord_report_markdown(report)
    if len(full) <= cap:
        return [full]

    title = report.get("report_title") or DEFAULT_TITLE
    artifact = artifact_path or "external kanban-report-v1 artifact required"
    summary = _join_lines(
        _summary_lines(report, split_notice="Structured block in final part or external artifact.")
        + _warnings_errors_lines(report["structured"])
    )
    body_chunks = _split_body_sections(report)

    structured_chunk = _join_lines(_structured_lines(report["structured"]))
    if len(_part_prefix(title, 9, 9)) + len(structured_chunk) > cap:
        structured_chunk = _join_lines(_structured_lines(_pointer_payload(report, artifact)))
    chunks = [summary, *body_chunks, structured_chunk]
    footer = f"_Full report: {artifact}_" if artifact_path else None
    return _pack_chunks_with_prefix(chunks, title=title, soft_cap=cap, footer=footer)


_CODE_FENCE_RE = re.compile(r"```json kanban-report-v1\n(.*?)\n```", re.S)


def extract_structured_payload(markdown: str) -> dict[str, Any]:
    """Parse the preferred ``json kanban-report-v1`` block from rendered output."""
    match = _CODE_FENCE_RE.search(markdown)
    if not match:
        raise ValueError("structured_payload_missing")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("structured_payload_invalid") from exc

"""Kanban sprint-closure helper — pre-complete a parent with a
structured delivery-receipt comment.

Mirrors the manual sprint-closure pattern documented in the feedback
memory ``hermes-sprint-closure-pattern``: before flipping a decomposed
parent to ``done`` it is essential to commit a comprehensive
SPRINT CLOSURE comment that aggregates the kid outcomes — kid receipts
survive scratch-workspace cleanup and any later DB recovery, so a
``hermes kanban show <parent-id>`` after the fact still reconstructs
the full sprint story.

Invoked via ``hermes kanban close-sprint <parent-id> [--auto-summary | --comment FILE]``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)


_CLOSURE_HEADER = "SPRINT CLOSURE"


@dataclass
class KidReceipt:
    """Per-kid summary used to assemble the parent's closure comment."""

    task_id: str
    title: str
    status: str
    assignee: Optional[str]
    runtime_seconds: Optional[int]
    summary: Optional[str]
    artifacts: list[str]


@dataclass
class CloseSprintOutcome:
    """Result of a ``close_sprint`` call. ``ok=False`` covers expected
    failure modes (unknown parent, parent already done, kids still
    open) without raising.
    """

    parent_id: str
    ok: bool
    reason: str = ""
    comment_body: Optional[str] = None
    comment_id: Optional[int] = None
    completed: bool = False
    kid_receipts: Optional[list[KidReceipt]] = None


def _profile_author() -> str:
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "close-sprint"
    )


def _kid_receipt(conn, kid_id: str) -> KidReceipt:
    task = kb.get_task(conn, kid_id)
    if task is None:
        return KidReceipt(
            task_id=kid_id,
            title="(unknown task)",
            status="unknown",
            assignee=None,
            runtime_seconds=None,
            summary=None,
            artifacts=[],
        )
    runs = kb.list_runs(conn, kid_id, include_active=False)
    runtime_seconds: Optional[int] = None
    summary: Optional[str] = None
    artifacts: list[str] = []
    if runs:
        last = runs[-1]
        if last.ended_at is not None and last.started_at is not None:
            runtime_seconds = int(last.ended_at - last.started_at)
        summary = (last.summary or "").strip() or None
        if isinstance(last.metadata, dict):
            raw = last.metadata.get("artifacts")
            if isinstance(raw, list):
                artifacts = [str(a) for a in raw if isinstance(a, str)]
    return KidReceipt(
        task_id=kid_id,
        title=task.title or "(untitled)",
        status=task.status,
        assignee=task.assignee,
        runtime_seconds=runtime_seconds,
        summary=summary,
        artifacts=artifacts,
    )


def _fmt_runtime(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def _bullet_summary(summary: Optional[str], *, max_len: int = 240) -> str:
    if not summary:
        return "(no run-summary recorded)"
    flat = " ".join(summary.split())
    return flat if len(flat) <= max_len else flat[: max_len - 1] + "…"


def _try_auto_summary(parent_title: str, kids: list[KidReceipt]) -> Optional[str]:
    """Best-effort one-paragraph overview via the auxiliary LLM client.

    Returns ``None`` when the aux client isn't configured (matches the
    decomposer's graceful-degradation pattern). Callers fall back to a
    deterministic one-liner in that case.
    """
    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.debug("close-sprint: aux client import failed: %s", exc)
        return None
    try:
        client, model = get_text_auxiliary_client("kanban_close_sprint")
    except Exception as exc:
        logger.debug("close-sprint: get_text_auxiliary_client failed: %s", exc)
        return None
    if client is None or not model:
        return None
    kid_lines = []
    for k in kids:
        kid_lines.append(
            f"- {k.task_id} [{k.assignee or '-'}] ({k.status}, "
            f"runtime {_fmt_runtime(k.runtime_seconds)}): {_bullet_summary(k.summary, max_len=200)}"
        )
    user_msg = (
        f"Parent sprint: {parent_title}\n\n"
        f"Kid receipts ({len(kids)}):\n"
        + "\n".join(kid_lines)
        + "\n\nWrite a 2–3 sentence factual overview of what this sprint "
        "delivered. No hype, no speculation, no future-work commentary. "
        "German is fine. Plain prose only."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You summarize completed kanban sprints in 2–3 "
                        "factual sentences. No bullet points, no headers, "
                        "no hype words like 'successfully' or 'great'."
                    ),
                },
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=300,
            timeout=120,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.debug("close-sprint: aux LLM call failed: %s", exc)
        return None
    try:
        out = resp.choices[0].message.content or ""
    except Exception:
        out = ""
    out = " ".join(out.split())
    return out or None


def build_closure_comment(
    conn,
    parent_id: str,
    *,
    auto_summary: bool = False,
) -> tuple[str, list[KidReceipt]]:
    """Build the SPRINT CLOSURE comment body for ``parent_id``.

    Returns ``(comment_body, kid_receipts)``. Pure-function-ish: only
    reads the DB, never writes. Callers wire the actual comment-add
    and parent completion.
    """
    parent = kb.get_task(conn, parent_id)
    if parent is None:
        raise ValueError(f"unknown task {parent_id}")
    kid_ids = kb.child_ids(conn, parent_id)
    kids = [_kid_receipt(conn, kid) for kid in kid_ids]

    overview: Optional[str] = None
    if auto_summary and kids:
        overview = _try_auto_summary(parent.title or "(untitled)", kids)
    if not overview:
        done = sum(1 for k in kids if k.status == "done")
        blocked = sum(1 for k in kids if k.status == "blocked")
        archived = sum(1 for k in kids if k.status == "archived")
        if kids:
            overview = (
                f"All {done} of {len(kids)} kid(s) closed "
                f"(done={done}, blocked={blocked}, archived={archived})."
            )
        else:
            overview = "No kids linked to this sprint parent."

    lines: list[str] = []
    lines.append(
        f"{_CLOSURE_HEADER} by {_profile_author()}. "
        f"All {sum(1 for k in kids if k.status == 'done')} of {len(kids)} kids done. "
        "Deliverables:"
    )
    lines.append("")
    lines.append(overview)
    lines.append("")

    # DELIVERY: aggregated artifact paths across all kids.
    all_artifacts: list[str] = []
    seen: set[str] = set()
    for kid in kids:
        for a in kid.artifacts:
            if a not in seen:
                seen.add(a)
                all_artifacts.append(a)
    lines.append("DELIVERY:")
    if all_artifacts:
        for a in all_artifacts:
            lines.append(f"- {a}")
    else:
        lines.append("- (no run-recorded artifacts; check `~/.hermes/reports/` for persisted outputs)")
    lines.append("")

    # KID RECEIPTS: id, assignee, status, runtime, one-line outcome.
    lines.append("KID RECEIPTS:")
    if not kids:
        lines.append("- (no kids linked)")
    else:
        for kid in kids:
            lines.append(
                f"- {kid.task_id} ({kid.assignee or '-'}, "
                f"{_fmt_runtime(kid.runtime_seconds)}, {kid.status}): "
                f"{_bullet_summary(kid.summary)}"
            )
    lines.append("")

    # VALIDATION / OUT-OF-SCOPE / FOLLOW-UPS — placeholders the operator
    # can extend with `--comment FILE` if they want concrete entries.
    lines.append("VALIDATION:")
    lines.append("- (not auto-recorded; add a reviewer verdict or operator audit here)")
    lines.append("")
    lines.append("OUT-OF-SCOPE (explicit, not done): (none recorded)")
    lines.append("")
    lines.append("KNOWN FOLLOW-UPS:")
    lines.append("- (none recorded)")

    return "\n".join(lines), kids


def close_sprint(
    conn,
    parent_id: str,
    *,
    auto_summary: bool = False,
    comment_override: Optional[str] = None,
    author: Optional[str] = None,
    result: Optional[str] = None,
    summary_override: Optional[str] = None,
    require_kids_done: bool = True,
) -> CloseSprintOutcome:
    """Set a SPRINT CLOSURE comment on ``parent_id`` and then complete it.

    Pre-checks:
    - parent must exist;
    - parent must not already be ``done`` / ``archived``;
    - when ``require_kids_done=True`` (default), every kid must be in
      a terminal state (``done`` / ``archived``).

    The closure comment body is either taken from ``comment_override``
    (when provided) or assembled via :func:`build_closure_comment`. The
    aggregated kid-receipts are always returned in the outcome so
    callers can act on the receipts independently of the comment text.
    """
    parent = kb.get_task(conn, parent_id)
    if parent is None:
        return CloseSprintOutcome(parent_id, False, "unknown parent task id")
    if parent.status in ("done", "archived"):
        return CloseSprintOutcome(
            parent_id, False,
            f"parent already terminal (status={parent.status!r})",
        )

    kid_ids = kb.child_ids(conn, parent_id)
    kids: list[KidReceipt] = [_kid_receipt(conn, k) for k in kid_ids]
    if require_kids_done and kids:
        non_terminal = [
            k.task_id for k in kids if k.status not in ("done", "archived")
        ]
        if non_terminal:
            return CloseSprintOutcome(
                parent_id, False,
                f"{len(non_terminal)} kid(s) still open: {non_terminal}",
                kid_receipts=kids,
            )

    if comment_override is not None:
        body = comment_override.strip()
        if not body:
            return CloseSprintOutcome(
                parent_id, False, "comment override is empty",
                kid_receipts=kids,
            )
        if _CLOSURE_HEADER not in body:
            body = f"{_CLOSURE_HEADER} (operator override):\n\n{body}"
    else:
        body, kids = build_closure_comment(
            conn, parent_id, auto_summary=auto_summary,
        )

    audit_author = author or _profile_author()
    try:
        comment_id = kb.add_comment(conn, parent_id, audit_author, body)
    except ValueError as exc:
        return CloseSprintOutcome(
            parent_id, False, f"failed to add comment: {exc}",
            kid_receipts=kids,
            comment_body=body,
        )

    completion_summary = summary_override
    if completion_summary is None:
        # Default: lift the first non-empty line of the closure comment.
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line and not line.startswith(_CLOSURE_HEADER):
                completion_summary = line
                break
    if not completion_summary:
        completion_summary = "Sprint closed with structured receipt comment."

    completed = kb.complete_task(
        conn, parent_id,
        result=result or "Sprint deliverable complete",
        summary=completion_summary,
        metadata={"closure_comment_id": comment_id},
    )

    return CloseSprintOutcome(
        parent_id, True, "" if completed else "comment added but parent could not complete",
        comment_body=body,
        comment_id=int(comment_id),
        completed=bool(completed),
        kid_receipts=kids,
    )


def read_comment_file(path: str) -> str:
    """Read ``path`` (``-`` for stdin) and return the contents stripped.

    Wrapped here so the CLI can call it without re-implementing the
    stdin convention and so tests can exercise it directly.
    """
    if path == "-":
        import sys
        return sys.stdin.read().strip()
    return Path(path).expanduser().read_text(encoding="utf-8").strip()

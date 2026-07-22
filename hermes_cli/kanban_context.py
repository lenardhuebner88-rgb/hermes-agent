"""Worker-context rendering helpers for Hermes Kanban.

This module holds bounded, side-effect-free context policy so ``kanban_db``
does not have to own every worker prompt detail directly.
"""

from __future__ import annotations

import time
from typing import Optional


_CTX_MAX_PRIOR_ATTEMPTS = 10
_CTX_MAX_COMMENTS = 30
_CTX_MAX_FIELD_BYTES = 4 * 1024
_CTX_MAX_BODY_BYTES = 8 * 1024
_CTX_MAX_COMMENT_BYTES = 2 * 1024

_CTX_CAP_PROFILES = {
    "full": {
        "prior_attempts": _CTX_MAX_PRIOR_ATTEMPTS,
        "comments": _CTX_MAX_COMMENTS,
        "field_bytes": _CTX_MAX_FIELD_BYTES,
        "body_bytes": _CTX_MAX_BODY_BYTES,
        "comment_bytes": _CTX_MAX_COMMENT_BYTES,
        "role_history": 5,
    },
    # Operator inspection is deliberately bounded too, while retaining the
    # historical full-profile extraction caps.
    "operator_detail": {
        "prior_attempts": _CTX_MAX_PRIOR_ATTEMPTS,
        "comments": _CTX_MAX_COMMENTS,
        "field_bytes": _CTX_MAX_FIELD_BYTES,
        "body_bytes": _CTX_MAX_BODY_BYTES,
        "comment_bytes": _CTX_MAX_COMMENT_BYTES,
        "role_history": 5,
    },
    # Compact review context: keep the wide opening-body window (AC, bounded
    # diff, gates all live in the body) and the per-field byte cap (previous
    # stage findings / delta / residual risk live in the most recent run
    # summaries + metadata), but BOUND the review-irrelevant noise — deep
    # attempt history, comment storms, and cross-task role history — so the
    # verdict-only reviewer reads evidence, not backlog.
    "reviewer_review": {
        "prior_attempts": 4,
        "comments": 10,
        "field_bytes": _CTX_MAX_FIELD_BYTES,
        "body_bytes": 32 * 1024,
        "comment_bytes": _CTX_MAX_COMMENT_BYTES,
        "role_history": 1,
    },
    "worker_slim": {
        "prior_attempts": 3,
        "comments": 8,
        "field_bytes": 1536,
        "body_bytes": 4 * 1024,
        "comment_bytes": 1024,
        "role_history": 2,
    },
    "retry": {
        "prior_attempts": 1,
        "comments": 4,
        "field_bytes": 1024,
        "body_bytes": 2560,
        "comment_bytes": 512,
        "role_history": 0,
    },
}


def cap_text(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
    """Truncate a string to ``limit`` chars with a visible ellipsis."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def _is_review_run(task) -> bool:
    """True when the current run against *task* is a review-stage run.

    Detection keys off the task's *review status* first: under the same-card
    review economy the verifier/reviewer stage runs on the coder's own card, so
    the persistent ``assignee`` stays ``coder`` and an assignee check would miss
    the review run entirely. The legacy dedicated review card
    (``assignee=reviewer`` + ``kind=review``) remains a valid signal so
    stand-alone review tasks keep the compact profile.
    """
    if (getattr(task, "status", "") or "").strip().lower() == "review":
        return True
    return (
        (getattr(task, "assignee", "") or "").strip().lower() == "reviewer"
        and (getattr(task, "kind", "") or "").strip().lower() == "review"
    )


def context_profile_for_task(task, profile: str) -> str:
    """Return the effective context cap profile for a task."""
    if int(getattr(task, "continuation_count", 0) or 0) > 0 and profile in {
        "worker_slim",
        "full",
    }:
        return "retry"
    if profile == "full" and _is_review_run(task):
        return "reviewer_review"
    return profile


def context_caps(profile: str) -> dict:
    """Return the configured cap profile, falling back to ``full``."""
    return _CTX_CAP_PROFILES.get(profile, _CTX_CAP_PROFILES["full"])


def render_comment_thread(
    comments: list,
    *,
    max_comments: int = _CTX_MAX_COMMENTS,
    comment_bytes: int = _CTX_MAX_COMMENT_BYTES,
) -> list[str]:
    """Render a task's comment thread as worker-context lines."""
    if not comments:
        return []
    directives = [c for c in comments if getattr(c, "kind", "comment") == "directive"]
    regular = [c for c in comments if getattr(c, "kind", "comment") != "directive"]

    lines: list[str] = []

    if directives:
        lines.append("## ⚠️ OPERATOR DIRECTIVE — supersedes the task body above")
        for c in directives:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            safe_author = (c.author or "").replace("`", "")
            lines.append(f"operator directive `{safe_author}` at {ts}:")
            lines.append(cap_text(c.body, comment_bytes))
            lines.append("")

    if regular:
        if len(regular) > max_comments:
            omitted_c = len(regular) - max_comments
            shown_c = regular[-max_comments:]
        else:
            omitted_c = 0
            shown_c = regular
        lines.append("## Comment thread")
        if omitted_c:
            lines.append(
                f"_({omitted_c} earlier comment{'s' if omitted_c != 1 else ''} "
                f"omitted; showing most recent {len(shown_c)})_"
            )
        for c in shown_c:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            safe_author = (c.author or "").replace("`", "")
            lines.append(f"comment from worker `{safe_author}` at {ts}:")
            lines.append(cap_text(c.body, comment_bytes))
            lines.append("")
    return lines


"""Pure, budgeted rendering for Kanban worker briefs.

Database access and overflow-artifact materialization deliberately live at the
launch boundary in :mod:`hermes_cli.kanban_db`. This module only turns a
canonical task/section input into a bounded payload plus telemetry.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

RENDERER_VERSION = "worker-brief-v1"
SECTION_ORDER = (
    "assignment",
    "parent_evidence",
    "findings",
    "comments",
    "runs",
    "recent_work",
    "review_diff",
)
SECTION_LABELS = {
    "assignment": "Assignment, acceptance criteria, and scope",
    "parent_evidence": "Parent and scout evidence",
    "findings": "Current findings",
    "comments": "Relevant comments",
    "runs": "Prior runs",
    "recent_work": "Recent work",
    "review_diff": "Review diff",
}
PROFILE_BUDGETS = {
    "worker_slim": {"total": 48000, "assignment": 24000, "parent_evidence": 10000, "findings": 6000, "comments": 7000, "runs": 6000, "recent_work": 3000, "review_diff": 8000},
    "retry": {"total": 56000, "assignment": 24000, "parent_evidence": 10000, "findings": 10000, "comments": 7000, "runs": 10000, "recent_work": 3000, "review_diff": 8000},
    "reviewer_review": {"total": 64000, "assignment": 22000, "parent_evidence": 12000, "findings": 8000, "comments": 8000, "runs": 8000, "recent_work": 2000, "review_diff": 16000},
    "operator_detail": {"total": 80000, "assignment": 28000, "parent_evidence": 16000, "findings": 12000, "comments": 12000, "runs": 12000, "recent_work": 6000, "review_diff": 20000},
}
_RELATIVE_TIME_RE = re.compile(r"\b(?:just now|\d+\s*(?:s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hours?|d|days?|w|weeks?)\s+ago)\b", re.IGNORECASE)


@dataclass(frozen=True)
class BriefRecord:
    """One indivisible record in a priority-ordered brief section."""
    text: str
    canonical_text: str | None = None
    key: str | None = None


@dataclass(frozen=True)
class WorkerBriefInput:
    """Canonical task input consumed by render_worker_brief."""
    task_id: str
    title: str
    header: str
    sections: Mapping[str, Sequence[BriefRecord]]


@dataclass(frozen=True)
class RenderedWorkerBrief:
    payload: str
    manifest: dict[str, Any]
    overflows: Mapping[str, str]


def _canonical_record(record: BriefRecord) -> str:
    text = record.canonical_text if record.canonical_text is not None else record.text
    return _RELATIVE_TIME_RE.sub("<relative-time>", text).rstrip()


def _fingerprint(task: WorkerBriefInput, *, phase: str, audience: str, profile: str) -> str:
    canonical = {
        "renderer_version": RENDERER_VERSION,
        "task_id": task.task_id,
        "title": task.title,
        "phase": phase,
        "profile": profile,
        "header": _RELATIVE_TIME_RE.sub("<relative-time>", task.header).rstrip(),
        "sections": {
            name: [{"key": record.key, "text": _canonical_record(record)} for record in task.sections.get(name, ())]
            for name in SECTION_ORDER
        },
    }
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_worker_brief(task: WorkerBriefInput, *, phase: str, audience: str, profile: str) -> RenderedWorkerBrief:
    """Render one bounded worker payload without I/O or side effects.

    Records are admitted whole, in section/record priority order. Any record
    that exceeds a section or total budget is omitted visibly and returned in
    overflows for the launch boundary to materialize.
    """
    if phase not in {"execute", "retry", "verify", "review"}:
        raise ValueError(f"unsupported worker brief phase: {phase}")
    if profile not in PROFILE_BUDGETS:
        raise ValueError(f"unsupported worker brief profile: {profile}")
    budgets = PROFILE_BUDGETS[profile]
    prefix = task.header.rstrip()
    parts = [prefix] if prefix else []
    used = len(prefix)
    overflows: dict[str, str] = {}
    section_counts: dict[str, dict[str, int]] = {}

    for name in SECTION_ORDER:
        records = tuple(task.sections.get(name, ()))
        if not records:
            continue
        heading = f"## {SECTION_LABELS[name]}"
        section_used = 0
        included: list[str] = []
        omitted: list[BriefRecord] = []
        for record in records:
            text = record.text.rstrip()
            if not text:
                continue
            record_cost = len(text) + (2 if included else 0)
            projected_total = used + len(heading) + 4 + section_used + record_cost
            if section_used + record_cost <= int(budgets[name]) and projected_total <= int(budgets["total"]):
                included.append(text)
                section_used += record_cost
            else:
                omitted.append(record)
        block_lines = [heading]
        if included:
            block_lines.append("\n\n".join(included))
        if omitted:
            block_lines.append(f"[{len(omitted)} record(s) omitted at record boundaries; full overflow is materialized at launch.]")
            overflows[name] = "\n\n".join(record.text.rstrip() for record in records if record.text.rstrip()) + "\n"
        block = "\n".join(block_lines)
        parts.append(block)
        used += len(block) + 2
        section_counts[name] = {
            "available": len(records),
            "included": len(included),
            "omitted": len(omitted),
            "included_chars": section_used,
        }

    payload = "\n\n".join(parts).rstrip() + "\n"
    manifest = {
        "renderer_version": RENDERER_VERSION,
        "phase": phase,
        "audience": audience,
        "profile": profile,
        "chars": len(payload),
        "token_estimate": (len(payload) + 3) // 4,
        "section_counts": section_counts,
        "omitted_records": sum(v["omitted"] for v in section_counts.values()),
        "payload_fingerprint": _fingerprint(task, phase=phase, audience=audience, profile=profile),
    }
    return RenderedWorkerBrief(payload=payload, manifest=manifest, overflows=overflows)

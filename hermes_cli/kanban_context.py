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
    "reviewer_review": {
        "prior_attempts": _CTX_MAX_PRIOR_ATTEMPTS,
        "comments": _CTX_MAX_COMMENTS,
        "field_bytes": _CTX_MAX_FIELD_BYTES,
        "body_bytes": 32 * 1024,
        "comment_bytes": _CTX_MAX_COMMENT_BYTES,
        "role_history": 5,
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


def context_profile_for_task(task, profile: str) -> str:
    """Return the effective context cap profile for a task."""
    if int(getattr(task, "continuation_count", 0) or 0) > 0 and profile in {
        "worker_slim",
        "full",
    }:
        return "retry"
    if (
        profile == "full"
        and (getattr(task, "assignee", "") or "").strip().lower() == "reviewer"
        and (getattr(task, "kind", "") or "").strip().lower() == "review"
    ):
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

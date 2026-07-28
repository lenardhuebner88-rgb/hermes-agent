"""Fork-owned rendering boundary for goal-judge task context.

The generic goal engine lives in upstream-owned ``hermes_cli.goals``.  Hermes'
Kanban integration needs two fork-specific guarantees around that engine:

* persisted structured acceptance criteria must reach the judge, and
* long task bodies must retain their goal head, acceptance section, and
  post-acceptance gates within a strict configurable character budget.

Keeping those policies here limits upstream-file changes to narrow calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable


DEFAULT_GOAL_JUDGE_GOAL_CHARS = 8_000
MAX_GOAL_JUDGE_GOAL_CHARS = 32_000
GOAL_MIDDLE_OMITTED_MARKER = "\n\n[... omitted middle content ...]\n\n"
GOAL_TRUNCATION_SUFFIX = "… [truncated]"

_ACCEPTANCE_CRITERIA_HEADING_RE = re.compile(
    r"^[ \t]{0,3}(?P<hashes>#{1,6})[ \t]*"
    r"(?:Akzeptanzkriterien|Acceptance Criteria)[ \t]*:?[ \t]*#*[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}(?P<hashes>#{1,6})[ \t]+", re.MULTILINE)
_ACCEPTANCE_CRITERIA_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?AC"
    r"(?:[ \t]*\d+[ \t]*[:.)-]|[ \t]*[:-][ \t]*).*$",
    re.IGNORECASE | re.MULTILINE,
)
_AC_ID_PREFIX_RE = re.compile(r"^ac(?=[-_\s\d])[-_\s]*", re.IGNORECASE)


@dataclass(frozen=True)
class AcceptanceSection:
    """A recognized acceptance section and its location in the source goal."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _AcceptanceItem:
    """One normalized persisted acceptance criterion."""

    label: str
    statement: str
    details: tuple[tuple[str, str], ...]
    rendered_statement: str


def resolve_goal_chars(
    *,
    config_loader: Callable[[], dict[str, Any]] | None = None,
) -> int:
    """Resolve the configured goal budget and enforce its hard upper bound."""
    try:
        if config_loader is None:
            from hermes_cli.config import load_config

            config_loader = load_config
        cfg = config_loader()
        value = int(
            (cfg.get("auxiliary") or {})
            .get("goal_judge", {})
            .get("goal_chars", DEFAULT_GOAL_JUDGE_GOAL_CHARS)
        )
        if value > 0:
            return min(value, MAX_GOAL_JUDGE_GOAL_CHARS)
    except Exception:
        pass
    return DEFAULT_GOAL_JUDGE_GOAL_CHARS


def acceptance_criteria_section(goal: str) -> AcceptanceSection | None:
    """Locate a recognized acceptance section without mutating its text."""
    heading = _ACCEPTANCE_CRITERIA_HEADING_RE.search(goal)
    if heading:
        heading_level = len(heading.group("hashes"))
        end = len(goal)
        for candidate in _MARKDOWN_HEADING_RE.finditer(goal, heading.end()):
            if len(candidate.group("hashes")) <= heading_level:
                end = candidate.start()
                break
        return AcceptanceSection(
            start=heading.start(),
            end=end,
            text=goal[heading.start() : end].rstrip(),
        )

    lines = list(_ACCEPTANCE_CRITERIA_LINE_RE.finditer(goal))
    if lines:
        return AcceptanceSection(
            start=lines[0].start(),
            end=lines[-1].end(),
            text="\n".join(match.group(0) for match in lines),
        )
    return None


def _truncate_prefix(text: str, limit: int) -> str:
    """Keep a prefix within *limit*, including a visible truncation suffix."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(GOAL_TRUNCATION_SUFFIX):
        return text[:limit]
    prefix = text[: limit - len(GOAL_TRUNCATION_SUFFIX)].rstrip()
    return prefix + GOAL_TRUNCATION_SUFFIX


def _truncate_middle(text: str, limit: int) -> str:
    """Keep both edges of critical text when the text itself exceeds budget."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(GOAL_MIDDLE_OMITTED_MARKER):
        return text[:limit]
    available = limit - len(GOAL_MIDDLE_OMITTED_MARKER)
    left = (available + 1) // 2
    right = available - left
    if right <= 0:
        return text[:left] + GOAL_MIDDLE_OMITTED_MARKER
    return text[:left].rstrip() + GOAL_MIDDLE_OMITTED_MARKER + text[-right:].lstrip()


def _allocate_edges(
    budget: int,
    *,
    prefix_length: int,
    suffix_length: int,
) -> tuple[int, int]:
    """Split edge budget fairly and donate unused capacity to the other edge."""
    if budget <= 0:
        return 0, 0
    if suffix_length <= 0:
        return min(prefix_length, budget), 0

    prefix_budget = min(prefix_length, (budget + 1) // 2)
    suffix_budget = min(suffix_length, budget - prefix_budget)
    remaining = budget - prefix_budget - suffix_budget
    if remaining:
        add_suffix = min(suffix_length - suffix_budget, remaining)
        suffix_budget += add_suffix
        remaining -= add_suffix
    if remaining:
        prefix_budget += min(prefix_length - prefix_budget, remaining)
    return prefix_budget, suffix_budget


def _compose_rendered_goal(
    *,
    prefix: str,
    criteria: str,
    suffix: str,
    prefix_budget: int,
    criteria_budget: int,
    suffix_budget: int,
) -> str:
    prefix_text = prefix[:prefix_budget].rstrip()
    criteria_text = _truncate_middle(criteria, criteria_budget)
    suffix_text = _truncate_prefix(suffix, suffix_budget)

    parts: list[str] = []
    if prefix_text:
        parts.append(prefix_text)
    if criteria_text:
        if prefix_text:
            parts.append(
                "\n\n" if prefix_budget >= len(prefix) else GOAL_MIDDLE_OMITTED_MARKER
            )
        parts.append(criteria_text)
    if suffix_text:
        parts.append("\n\n")
        parts.append(suffix_text)
    return "".join(parts)


def render_goal_for_judge(goal: str, *, limit: int | None = None) -> str:
    """Render a goal within budget while prioritizing its completion contract.

    Priority is: the complete acceptance section when it can fit, the opening
    goal context, then post-acceptance gates/deliverables.  Any unavoidable
    truncation is explicit, and the returned string never exceeds *limit*.
    """
    if limit is None:
        limit = resolve_goal_chars()
    else:
        try:
            limit = min(int(limit), MAX_GOAL_JUDGE_GOAL_CHARS)
        except (TypeError, ValueError):
            limit = DEFAULT_GOAL_JUDGE_GOAL_CHARS
        if limit <= 0:
            limit = DEFAULT_GOAL_JUDGE_GOAL_CHARS

    if len(goal) <= limit:
        return goal

    section = acceptance_criteria_section(goal)
    if section is None:
        return _truncate_prefix(goal, limit)

    prefix = goal[: section.start].rstrip()
    criteria = section.text
    suffix = goal[section.end :].lstrip()

    # Reserve the longer visible omission marker up front. If the full prefix
    # fits, composition uses a shorter paragraph separator and stays below the
    # hard boundary without a second, trim-sensitive allocation pass.
    overhead = len(GOAL_MIDDLE_OMITTED_MARKER) if prefix else 0
    if suffix:
        overhead += 2
    content_budget = limit - overhead
    if content_budget <= 0:
        return _truncate_middle(goal, limit)

    edge_count = int(bool(prefix)) + int(bool(suffix))
    total_edge_length = len(prefix) + len(suffix)
    minimum_edge_budget = min(
        total_edge_length,
        content_budget,
        max(edge_count, content_budget // 8),
    )
    if len(criteria) <= content_budget - minimum_edge_budget:
        # Keep a complete acceptance contract whenever it fits alongside a
        # meaningful amount of surrounding context.
        criteria_budget = len(criteria)
        edge_budget = content_budget - criteria_budget
    else:
        # If the AC itself cannot fit, keep enough head/gate context for the
        # judge to understand what the truncated contract belongs to.
        edge_budget = min(
            total_edge_length,
            max(edge_count, content_budget // 3),
            max(0, content_budget - 1),
        )
        criteria_budget = content_budget - edge_budget
    prefix_budget, suffix_budget = _allocate_edges(
        edge_budget,
        prefix_length=len(prefix),
        suffix_length=len(suffix),
    )
    return _compose_rendered_goal(
        prefix=prefix,
        criteria=criteria,
        suffix=suffix,
        prefix_budget=prefix_budget,
        criteria_budget=criteria_budget,
        suffix_budget=suffix_budget,
    )


def _acceptance_item(item: Any, index: int) -> _AcceptanceItem | None:
    """Normalize one persisted AC item for deduplication and rendering."""
    if isinstance(item, str):
        statement = " ".join(item.split())
        if not statement:
            return None
        if re.match(r"^(?:[-*+][ \t]+)?AC(?:[-_\s\d:]|$)", statement, re.I):
            rendered_statement = statement.lstrip("-*+ \t")
        else:
            rendered_statement = f"AC-{index}: {statement}"
        return _AcceptanceItem(
            label=f"AC-{index}",
            statement=statement,
            details=(),
            rendered_statement=rendered_statement,
        )

    if not isinstance(item, dict):
        return None
    statement = " ".join(str(item.get("statement") or "").split())
    if not statement:
        return None

    raw_id = " ".join(str(item.get("id") or index).split())
    core_id = _AC_ID_PREFIX_RE.sub("", raw_id).strip("-_ ") or str(index)
    criterion_label = f"AC-{core_id}"
    details: list[tuple[str, str]] = []
    for key, detail_label in (
        ("verification", "Verification"),
        ("done_signal", "Done signal"),
    ):
        value = " ".join(str(item.get(key) or "").split())
        if value:
            details.append((detail_label, value))
    return _AcceptanceItem(
        label=criterion_label,
        statement=statement,
        details=tuple(details),
        rendered_statement=f"{criterion_label}: {statement}",
    )


def _merge_acceptance_lines(body: str, lines: list[str]) -> str:
    """Place missing structured facts inside the body's protected AC section."""
    if not lines:
        return body

    rendered = "\n".join(lines)
    section = acceptance_criteria_section(body)
    if section is None:
        block = "## Acceptance Criteria\n" + rendered
        return f"{body.rstrip()}\n\n{block}" if body else block

    before = body[: section.end].rstrip()
    after = body[section.end :].lstrip()
    merged = f"{before}\n{rendered}"
    if after:
        merged += f"\n\n{after}"
    return merged


def render_task_goal(task: Any) -> str:
    """Build one canonical judge goal from a task row-like object."""
    title = str(getattr(task, "title", "") or "").strip()
    body = str(getattr(task, "body", "") or "").strip()
    raw_criteria = getattr(task, "acceptance_criteria", None)

    parsed: Any = raw_criteria
    if isinstance(raw_criteria, str):
        try:
            parsed = json.loads(raw_criteria)
        except (TypeError, json.JSONDecodeError):
            parsed = None

    rendered_criteria: list[str] = []
    body_section = acceptance_criteria_section(body)
    protected_body = body_section.text if body_section is not None else ""
    if isinstance(parsed, list):
        for index, item in enumerate(parsed, start=1):
            criterion = _acceptance_item(item, index)
            if criterion is None:
                continue
            statement_missing = criterion.statement not in protected_body
            missing_details = [
                (label, value)
                for label, value in criterion.details
                if value not in protected_body
            ]
            if not statement_missing and not missing_details:
                continue

            if statement_missing:
                lines = [f"- {criterion.rendered_statement}"]
            else:
                lines = [f"- Structured details for {criterion.label}:"]
            lines.extend(f"  {label}: {value}" for label, value in missing_details)
            rendered_criteria.append("\n".join(lines))

    parts: list[str] = []
    if title:
        parts.append(title)
    merged_body = _merge_acceptance_lines(body, rendered_criteria)
    if merged_body:
        parts.append(merged_body)
    return "\n\n".join(parts).strip()


__all__ = [
    "AcceptanceSection",
    "DEFAULT_GOAL_JUDGE_GOAL_CHARS",
    "MAX_GOAL_JUDGE_GOAL_CHARS",
    "GOAL_MIDDLE_OMITTED_MARKER",
    "GOAL_TRUNCATION_SUFFIX",
    "acceptance_criteria_section",
    "render_goal_for_judge",
    "render_task_goal",
    "resolve_goal_chars",
]

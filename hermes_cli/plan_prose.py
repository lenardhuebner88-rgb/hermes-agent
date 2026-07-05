"""Deterministic prose Plan parser and compiler.

The prose Plan format is intentionally small: sessions write ordinary
Markdown, and this module extracts only the fixed fields that can be translated
without model judgment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from hermes_cli.control_plane_gate import classify_review_tier
from hermes_cli.plan_compiler import (
    BindingSubtask,
    CompileBlocked,
    TaskgraphHints,
    assert_acyclic,
    taskgraph_hints_to_children,
)


@dataclass(frozen=True)
class ProseSlice:
    title: str
    lane: str | None = None
    done_when: str | None = None
    files: list[str] = field(default_factory=list)
    risk: str | None = None
    deps: list[str] = field(default_factory=list)
    body: str = ""

    @property
    def ambiguous(self) -> bool:
        return not (self.done_when or "").strip() and not (self.body or "").strip()


@dataclass(frozen=True)
class ProsePlan:
    title: str
    goal: str
    slices: list[ProseSlice]


@dataclass(frozen=True)
class CompileResult:
    children: list[dict[str, Any]]
    repairs: list[str]
    warnings: list[str]
    hints: TaskgraphHints


_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_GOAL_RE = re.compile(r"^\s*(?:\*\*)?Goal:(?:\*\*)?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_SLICE_RE = re.compile(r"^##\s+Slice:\s*(.+?)\s*$", re.MULTILINE)
_KEY_RE = re.compile(r"^\s*-\s*([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _title_key(value: str) -> str:
    return " ".join(value.lower().split())


def _slice_id(index: int) -> str:
    return f"S{index + 1}"


def parse_prose_plan(text: str) -> ProsePlan:
    """Parse the fixed prose Plan Markdown format.

    Unknown slice keys are ignored. Optional known keys stay unset when absent,
    so callers can report or repair them explicitly.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").removeprefix("\ufeff")
    title_match = _TITLE_RE.search(normalized)
    goal_match = _GOAL_RE.search(normalized)
    title = title_match.group(1).strip() if title_match else "Untitled Plan"
    goal = goal_match.group(1).strip() if goal_match else ""

    matches = list(_SLICE_RE.finditer(normalized))
    slices: list[ProseSlice] = []
    for index, match in enumerate(matches):
        section_start = match.end()
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        section = normalized[section_start:section_end]
        metadata: dict[str, str] = {}
        body_lines: list[str] = []
        for line in section.splitlines():
            key_match = _KEY_RE.match(line)
            if key_match:
                key = key_match.group(1).strip().lower().replace("_", "-")
                metadata[key] = key_match.group(2).strip()
            elif line.strip():
                body_lines.append(line.strip())

        slices.append(
            ProseSlice(
                title=match.group(1).strip(),
                lane=metadata.get("lane") or None,
                done_when=metadata.get("done-when") or None,
                files=_csv(metadata.get("files", "")),
                risk=metadata.get("risk") or None,
                deps=_csv(metadata.get("deps", "")),
                body="\n".join(body_lines).strip(),
            )
        )

    return ProsePlan(title=title, goal=goal, slices=slices)


def _review_tier(plan: ProsePlan, slice_: ProseSlice) -> str:
    text = "\n".join(
        item
        for item in (
            plan.title,
            plan.goal,
            slice_.title,
            slice_.done_when or "",
            slice_.risk or "",
            slice_.body,
        )
        if item
    )
    return classify_review_tier(
        {
            "goal": plan.goal,
            "objective": text,
            "risk_class": slice_.risk or "",
            "scope": " ".join(slice_.files),
        }
    )


def _body_for_slice(plan: ProsePlan, slice_: ProseSlice) -> str:
    parts = []
    if plan.goal.strip():
        parts.append(f"Plan goal: {plan.goal.strip()}")
    if slice_.body.strip():
        parts.append(slice_.body.strip())
    if slice_.done_when:
        parts.append(f"Done when: {slice_.done_when}")
    if slice_.files:
        parts.append("Files: " + ", ".join(slice_.files))
    if slice_.risk:
        parts.append(f"Risk: {slice_.risk}")
    return "\n\n".join(parts)


def compile_prose_plan(plan: ProsePlan) -> CompileResult:
    """Compile a parsed prose Plan into Kanban child dictionaries.

    The translation is deterministic and intentionally conservative: unknown
    dependencies and cycles block, while underspecified slices are surfaced as
    warnings or explicit repair notes.
    """
    repairs: list[str] = []
    warnings: list[str] = []
    title_to_id: dict[str, str] = {}
    duplicate_titles: set[str] = set()
    ids = [_slice_id(index) for index, _slice in enumerate(plan.slices)]

    for index, slice_ in enumerate(plan.slices):
        key = _title_key(slice_.title)
        if key in title_to_id:
            duplicate_titles.add(slice_.title)
            warnings.append(f"ambiguous duplicate slice title reported: {slice_.title}")
            continue
        title_to_id[key] = ids[index]

    deps_by_id: dict[str, list[str]] = {}
    for index, slice_ in enumerate(plan.slices):
        task_id = ids[index]
        if slice_.deps:
            resolved: list[str] = []
            for dep_title in slice_.deps:
                dep_id = title_to_id.get(_title_key(dep_title))
                if dep_id is None:
                    raise CompileBlocked([f"slice {slice_.title!r} depends on unknown slice {dep_title!r}"])
                resolved.append(dep_id)
            deps_by_id[task_id] = resolved
        elif index > 0:
            deps_by_id[task_id] = [ids[index - 1]]
            repairs.append(
                f"slice {slice_.title!r}: deps missing; repaired to depend on previous slice {plan.slices[index - 1].title!r}"
            )
        else:
            deps_by_id[task_id] = []

        if slice_.ambiguous:
            warnings.append(f"ambiguous slice reported: {slice_.title!r} lacks done-when and body")

    if len(plan.slices) > 12:
        warnings.append(f"over-decomposition warning: {len(plan.slices)} slices in one prose Plan")

    assert_acyclic(ids, deps_by_id)

    subtasks: list[BindingSubtask] = []
    for index, slice_ in enumerate(plan.slices):
        lane = slice_.lane
        if not lane:
            lane = "coder"
            repairs.append(f"slice {slice_.title!r}: lane missing; repaired to coder")
        subtasks.append(
            BindingSubtask(
                id=ids[index],
                title=slice_.title,
                lane=lane,
                deps=deps_by_id[ids[index]],
                body=_body_for_slice(plan, slice_),
                acceptance_criteria=[slice_.done_when] if slice_.done_when else [],
                review_tier=_review_tier(plan, slice_),
            )
        )

    hints = TaskgraphHints(binding=True, subtasks=subtasks)
    children = taskgraph_hints_to_children(hints)
    return CompileResult(children=children, repairs=repairs, warnings=warnings, hints=hints)

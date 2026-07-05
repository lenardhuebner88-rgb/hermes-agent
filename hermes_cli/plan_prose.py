"""Deterministic prose Plan parser and compiler.

The prose Plan format is intentionally small: sessions write ordinary
Markdown, and this module extracts only the fixed fields that can be translated
without model judgment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


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


_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_GOAL_RE = re.compile(r"^\s*(?:\*\*)?Goal:(?:\*\*)?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_SLICE_RE = re.compile(r"^##\s+Slice:\s*(.+?)\s*$", re.MULTILINE)
_KEY_RE = re.compile(r"^\s*-\s*([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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

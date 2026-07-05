from __future__ import annotations

from hermes_cli.plan_prose import parse_prose_plan


def test_parse_two_slices():
    plan = parse_prose_plan(
        """# Plan to Board
**Goal:** Compile prose into deterministic Kanban children.

## Slice: Parse prose format
- lane: coder
- done-when: Parser returns two slices with titles.
- files: hermes_cli/plan_prose.py, tests/hermes_cli/test_plan_prose.py

## Slice: Compile children
- done-when: Children contain dependency indices.
- deps: Parse prose format
"""
    )

    assert plan.title == "Plan to Board"
    assert plan.goal == "Compile prose into deterministic Kanban children."
    assert [slice_.title for slice_ in plan.slices] == [
        "Parse prose format",
        "Compile children",
    ]
    assert plan.slices[0].lane == "coder"
    assert plan.slices[0].done_when == "Parser returns two slices with titles."
    assert plan.slices[0].files == [
        "hermes_cli/plan_prose.py",
        "tests/hermes_cli/test_plan_prose.py",
    ]
    assert plan.slices[1].deps == ["Parse prose format"]


def test_ambiguous_slice_flagged():
    plan = parse_prose_plan(
        """# Thin Plan
**Goal:** Keep ambiguous slices visible.

## Slice: Needs sharper done signal
"""
    )

    assert len(plan.slices) == 1
    assert plan.slices[0].ambiguous is True

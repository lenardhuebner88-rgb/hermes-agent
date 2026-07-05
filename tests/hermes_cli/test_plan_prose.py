from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_cli.plan_compiler import CompileBlocked, TaskgraphHints
from hermes_cli.plan_prose import compile_prose_plan, parse_prose_plan


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


def test_multi_node_cycle_blocks():
    plan = parse_prose_plan(
        """# Cyclic Plan
**Goal:** Cycles are hard blockers.

## Slice: First
- done-when: First completes.
- deps: Second

## Slice: Second
- done-when: Second completes.
- deps: First
"""
    )

    with pytest.raises(CompileBlocked):
        compile_prose_plan(plan)


def test_binding_hints_multi_node_cycle_blocks():
    with pytest.raises(ValidationError) as exc:
        TaskgraphHints.model_validate(
            {
                "binding": True,
                "subtasks": [
                    {"id": "S1", "title": "First", "lane": "coder", "deps": ["S2"]},
                    {"id": "S2", "title": "Second", "lane": "coder", "deps": ["S1"]},
                ],
            }
        )

    assert "dependency cycle detected" in str(exc.value)


def test_missing_lane_autorepaired():
    result = compile_prose_plan(
        parse_prose_plan(
            """# Lane Repair
**Goal:** Missing lanes get deterministic defaults.

## Slice: Build parser
- done-when: Parser tests pass.
"""
        )
    )

    assert result.children[0]["assignee"] == "coder"
    assert any("lane missing" in repair and "Build parser" in repair for repair in result.repairs)


def test_ambiguous_slice_warns():
    result = compile_prose_plan(
        parse_prose_plan(
            """# Warning Plan
**Goal:** Ambiguity is visible.

## Slice: Needs definition
"""
        )
    )

    assert len(result.children) == 1
    assert any("ambiguous slice" in warning and "Needs definition" in warning for warning in result.warnings)


def test_deps_from_order():
    result = compile_prose_plan(
        parse_prose_plan(
            """# Ordered Plan
**Goal:** Omitted deps become a chain.

## Slice: First
- done-when: First done.

## Slice: Second
- done-when: Second done.

## Slice: Third
- done-when: Third done.
"""
        )
    )

    assert result.children[0]["parents"] == []
    assert result.children[1]["parents"] == [0]
    assert result.children[2]["parents"] == [1]

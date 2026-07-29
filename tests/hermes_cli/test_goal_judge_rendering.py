"""Regression coverage for bounded goal-judge task rendering.

These tests exercise the public judge/completion paths around the fork's
Kanban goal-mode integration.  Keep them separate from upstream's generic
``test_goals.py`` so future upstream syncs do not interleave fork-only tests
with the upstream-owned module.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _judge_response(content: str = '{"verdict": "done", "reason": "ok"}'):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _capture_judge_prompt(monkeypatch, goal: str, **judge_kwargs) -> str:
    from hermes_cli import goals

    captured: dict[str, str] = {}

    def _fake_call_llm(**kwargs):
        captured["prompt"] = next(
            message["content"]
            for message in kwargs["messages"]
            if message["role"] == "user"
        )
        return _judge_response()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _fake_call_llm)
    goals.judge_goal(goal, "completed with evidence", **judge_kwargs)
    return captured["prompt"]


def _set_goal_limit(monkeypatch, limit: int) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"auxiliary": {"goal_judge": {"goal_chars": limit}}},
    )


def test_full_acceptance_section_wins_over_omitted_middle(monkeypatch):
    """A long middle must not consume budget needed by a complete AC block."""
    from hermes_cli import goals

    _set_goal_limit(monkeypatch, 240)
    acceptance = (
        "## Acceptance Criteria\n"
        "- AC-1: the complete criterion reaches the judge together with "
        + ("supporting detail " * 4)
        + "AC_TAIL_SENTINEL"
    )
    goal = "GOAL_HEAD_SENTINEL\n\n" + ("middle context " * 80) + "\n" + acceptance

    rendered = goals._render_goal_for_judge(goal)

    assert len(acceptance) + len("GOAL_HEAD_SENTINEL") < 240
    assert len(rendered) <= 240
    assert "GOAL_HEAD_SENTINEL" in rendered
    assert "AC_TAIL_SENTINEL" in rendered
    assert "[... omitted middle content ...]" in rendered


def test_post_acceptance_gates_use_remaining_budget(monkeypatch):
    """Gates after the AC block are contract text, not disposable tail noise."""
    from hermes_cli import goals

    _set_goal_limit(monkeypatch, 320)
    goal = (
        "GOAL_HEAD_SENTINEL\n\n"
        + ("implementation detail " * 70)
        + "\n## Acceptance Criteria\n"
        "- AC-1: AC_SENTINEL\n"
        "## Gates\n"
        "- GATE_SENTINEL\n"
        "## Deliverable\n"
        "- DELIVERABLE_SENTINEL\n"
    )

    rendered = goals._render_goal_for_judge(goal)

    assert len(rendered) <= 320
    assert "GOAL_HEAD_SENTINEL" in rendered
    assert "AC_SENTINEL" in rendered
    assert "GATE_SENTINEL" in rendered
    assert "DELIVERABLE_SENTINEL" in rendered
    assert "[... omitted middle content ...]" in rendered


def test_whitespace_at_budget_boundary_cannot_cut_complete_acceptance():
    """Whitespace trimming must not create fake slack that later cuts the AC."""
    from hermes_cli.goal_judge_rendering import render_goal_for_judge

    prefix = ("H" * 138) + (" " * 10) + ("T" * 852)
    acceptance = "## Acceptance Criteria\n- AC-1: " + ("C" * 28)
    goal = prefix + "\n" + acceptance

    rendered = render_goal_for_judge(goal, limit=240)

    assert len(acceptance) == 59
    assert len(rendered) <= 240
    assert acceptance in rendered


def test_oversized_acceptance_keeps_meaningful_head_and_post_ac_gate():
    """An AC larger than the budget must not reduce both edges to one char."""
    from hermes_cli.goal_judge_rendering import render_goal_for_judge

    goal = (
        "GOAL_HEAD_SENTINEL\n"
        + ("head context " * 80)
        + "\n## Acceptance Criteria\n"
        + ("criterion detail " * 160)
        + "\n## Gates\n"
        "- GATE_SENTINEL\n" + ("gate detail " * 40)
    )

    rendered = render_goal_for_judge(goal, limit=600)

    assert len(rendered) <= 600
    assert "GOAL_HEAD_SENTINEL" in rendered
    assert "## Acceptance Criteria" in rendered
    assert "GATE_SENTINEL" in rendered


def test_structured_task_acceptance_reaches_completion_judge(monkeypatch):
    """The persisted AC column is part of the real completion-gate prompt."""
    from hermes_cli import goals

    captured: dict[str, str] = {}

    def _fake_call_llm(**kwargs):
        captured["prompt"] = next(
            message["content"]
            for message in kwargs["messages"]
            if message["role"] == "user"
        )
        return _judge_response()

    task = SimpleNamespace(
        id="t_structured",
        title="Ship the structured task",
        body="Short body without an inline acceptance section.",
        acceptance_criteria=json.dumps([
            {
                "id": "AC-STRUCT",
                "statement": "STRUCTURED_AC_SENTINEL is proven",
                "verification": "focused test output is attached",
            }
        ]),
        goal_mode=True,
        kind="research",
        assignee="worker",
    )
    monkeypatch.setattr(goals, "goal_judge_available", lambda: True)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _fake_call_llm)

    rejection = goals.check_goal_mode_completion(
        task=task,
        handoff_text="STRUCTURED_AC_SENTINEL is proven by focused test output",
    )

    assert rejection is None
    assert "STRUCTURED_AC_SENTINEL" in captured["prompt"]
    assert "focused test output is attached" in captured["prompt"]


def test_structured_details_extend_existing_body_ac_without_duplication():
    """Structured facts belong in the body's protected AC section, not before it."""
    from hermes_cli.goal_judge_rendering import (
        acceptance_criteria_section,
        render_task_goal,
    )

    statement = "EXISTING_AC_SENTINEL is proven"
    verification = "VERIFICATION_SENTINEL from the focused test"
    task = SimpleNamespace(
        title="Ship existing inline criteria",
        body=(
            "Problem context.\n\n"
            "## Akzeptanzkriterien\n"
            f"- AC-1: {statement}\n"
            "## Gates\n"
            "- GATE_SENTINEL\n"
        ),
        acceptance_criteria=[
            {
                "id": "AC-1",
                "statement": statement,
                "verification": verification,
            }
        ],
    )

    rendered = render_task_goal(task)
    section = acceptance_criteria_section(rendered)

    assert rendered.count(statement) == 1
    assert rendered.count(verification) == 1
    assert section is not None
    assert verification in section.text
    assert "GATE_SENTINEL" in rendered[section.end :]


def test_string_acceptance_items_are_deduplicated_individually():
    """The common persisted list[str] shape adds only genuinely missing ACs."""
    from hermes_cli.goal_judge_rendering import render_task_goal

    task = SimpleNamespace(
        title="Ship string criteria",
        body=(
            "## Acceptance Criteria\n- AC-1: EXISTING_STRING_AC\n## Gates\n- tests pass"
        ),
        acceptance_criteria=[
            "AC-1: EXISTING_STRING_AC",
            "AC-2: MISSING_STRING_AC",
        ],
    )

    rendered = render_task_goal(task)

    assert rendered.count("EXISTING_STRING_AC") == 1
    assert rendered.count("MISSING_STRING_AC") == 1


def test_structured_ac_is_protected_when_statement_only_appears_in_prose():
    """A prose mention is not a substitute for a bounded acceptance section."""
    from hermes_cli.goal_judge_rendering import (
        acceptance_criteria_section,
        render_task_goal,
    )

    task = SimpleNamespace(
        title="Ship prose-only task",
        body="Problem context mentions PROSE_ONLY_AC but has no acceptance heading.",
        acceptance_criteria=["AC-1: PROSE_ONLY_AC"],
    )

    rendered = render_task_goal(task)
    section = acceptance_criteria_section(rendered)

    assert section is not None
    assert "AC-1: PROSE_ONLY_AC" in section.text


@pytest.mark.parametrize(
    "judge_kwargs",
    [
        pytest.param(
            {"contract": None, "subgoals": ["extra evidence is present"]},
            id="subgoals",
        ),
        pytest.param("contract", id="contract"),
    ],
)
def test_all_judge_prompt_variants_use_bounded_goal_renderer(monkeypatch, judge_kwargs):
    """Contract and subgoal prompts must not retain the historical 2k prefix."""
    from hermes_cli import goals

    if judge_kwargs == "contract":
        judge_kwargs = {
            "contract": goals.GoalContract(verification="tests pass"),
        }
    _set_goal_limit(monkeypatch, 600)
    goal = (
        "GOAL_HEAD_SENTINEL\n\n" + ("middle " * 500) + "\n## Acceptance Criteria\n"
        "- AC-1: VARIANT_AC_SENTINEL\n"
    )

    prompt = _capture_judge_prompt(monkeypatch, goal, **judge_kwargs)

    assert "GOAL_HEAD_SENTINEL" in prompt
    assert "VARIANT_AC_SENTINEL" in prompt
    assert "[... omitted middle content ...]" in prompt


def test_goal_char_config_has_a_hard_maximum(monkeypatch):
    from hermes_cli import goals

    _set_goal_limit(monkeypatch, 10_000_000)

    assert goals._goal_judge_goal_chars() == 32_000


def test_explicit_limit_is_enforced_independently_of_config(monkeypatch):
    from hermes_cli.goal_judge_rendering import render_goal_for_judge

    _set_goal_limit(monkeypatch, 10_000)
    rendered = render_goal_for_judge(
        "HEAD\n" + ("middle " * 100) + "\n## Acceptance Criteria\n- AC-1: tail",
        limit=64,
    )

    assert len(rendered) <= 64


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_acceptance_section_equality():
    """Kill comparison_flip L44: flipped __eq__ would make identical sections unequal."""
    from hermes_cli.goal_judge_rendering import AcceptanceSection

    a = AcceptanceSection(start=1, end=5, text="hello")
    b = AcceptanceSection(start=1, end=5, text="hello")
    c = AcceptanceSection(start=1, end=6, text="hello")
    assert a == b
    assert a != c


def test_acceptance_item_equality():
    """Kill comparison_flip L53: flipped __eq__ would make identical items unequal."""
    from hermes_cli.goal_judge_rendering import _AcceptanceItem

    a = _AcceptanceItem(label="AC-1", statement="do it", details=(), rendered_statement="AC-1: do it")
    b = _AcceptanceItem(label="AC-1", statement="do it", details=(), rendered_statement="AC-1: do it")
    c = _AcceptanceItem(label="AC-2", statement="do it", details=(), rendered_statement="AC-2: do it")
    assert a == b
    assert a != c


def test_resolve_goal_chars_caps_at_maximum():
    """Kill comparison_flip L76: flipped min() would return value above cap."""
    from hermes_cli.goal_judge_rendering import MAX_GOAL_JUDGE_GOAL_CHARS, resolve_goal_chars

    loader = lambda: {"auxiliary": {"goal_judge": {"goal_chars": MAX_GOAL_JUDGE_GOAL_CHARS + 100}}}
    assert resolve_goal_chars(config_loader=loader) == MAX_GOAL_JUDGE_GOAL_CHARS


def test_acceptance_section_ends_at_same_level_heading():
    """Kill comparison_flip L89: flipped <= to > would not stop at same-level heading."""
    from hermes_cli.goal_judge_rendering import acceptance_criteria_section

    goal = "## Acceptance Criteria\n- AC-1: test\n## Next Section\ncontent"
    section = acceptance_criteria_section(goal)
    assert section is not None
    assert "Next Section" not in section.text
    assert "AC-1" in section.text


def test_truncate_prefix_small_limit():
    """Kill comparison_flip L114: flipped <= would compute negative slice index."""
    from hermes_cli.goal_judge_rendering import GOAL_TRUNCATION_SUFFIX, _truncate_prefix

    limit = len(GOAL_TRUNCATION_SUFFIX) - 1
    text = "a long text that exceeds the limit"
    result = _truncate_prefix(text, limit)
    assert len(result) == limit
    assert result == text[:limit]


def test_truncate_middle_small_limit():
    """Kill comparison_flip L126: flipped <= would compute negative available space."""
    from hermes_cli.goal_judge_rendering import GOAL_MIDDLE_OMITTED_MARKER, _truncate_middle

    limit = len(GOAL_MIDDLE_OMITTED_MARKER) - 1
    text = "a long text that exceeds the limit significantly"
    result = _truncate_middle(text, limit)
    assert len(result) == limit
    assert result == text[:limit]


def test_acceptance_item_empty_id_falls_back_to_index():
    """Kill comparison_flip L286: flipped or would use empty string instead of index."""
    from hermes_cli.goal_judge_rendering import _acceptance_item

    item = {"id": "ac-", "statement": "do the thing"}
    result = _acceptance_item(item, 7)
    assert result is not None
    assert result.label == "AC-7"


def test_acceptance_item_includes_verification_detail():
    """Kill comparison_flip L294: flipped truthiness would skip non-empty details."""
    from hermes_cli.goal_judge_rendering import _acceptance_item

    item = {"statement": "do it", "verification": "run pytest"}
    result = _acceptance_item(item, 1)
    assert result is not None
    assert ("Verification", "run pytest") in result.details


def test_render_task_goal_skips_criterion_present_in_body():
    """Kill comparison_flip L344/L350: flipped 'not in' or skip condition would
    render criteria already present in the body's acceptance section."""
    from hermes_cli.goal_judge_rendering import render_task_goal

    task = SimpleNamespace(
        title="My Task",
        body="## Acceptance Criteria\n- AC-1: The widget renders correctly\n",
        acceptance_criteria=json.dumps([
            {"id": "1", "statement": "The widget renders correctly"},
        ]),
    )
    result = render_task_goal(task)
    assert result.count("The widget renders correctly") == 1


def test_acceptance_item_custom_id_preserved():
    """Kill bool_op_swap L285: or->and would replace a truthy custom id with index."""
    from hermes_cli.goal_judge_rendering import _acceptance_item

    item = {"id": "custom", "statement": "do the thing"}
    result = _acceptance_item(item, 7)
    assert result is not None
    assert result.label == "AC-custom"


def test_render_task_goal_includes_title():
    """Kill bool_op_swap L325: or->and would discard a truthy title."""
    from hermes_cli.goal_judge_rendering import render_task_goal

    task = SimpleNamespace(title="My Title", body="some body", acceptance_criteria=None)
    result = render_task_goal(task)
    assert result.startswith("My Title")


def test_resolve_goal_chars_zero_returns_default():
    """Kill comparison_swap L75: > -> >= would let value=0 through min() returning 0."""
    from hermes_cli.goal_judge_rendering import DEFAULT_GOAL_JUDGE_GOAL_CHARS, resolve_goal_chars

    loader = lambda: {"auxiliary": {"goal_judge": {"goal_chars": 0}}}
    assert resolve_goal_chars(config_loader=loader) == DEFAULT_GOAL_JUDGE_GOAL_CHARS


def test_truncate_prefix_limit_equals_suffix_length():
    """Kill comparison_swap L114: <= -> < would fall through when limit == len(suffix)."""
    from hermes_cli.goal_judge_rendering import GOAL_TRUNCATION_SUFFIX, _truncate_prefix

    limit = len(GOAL_TRUNCATION_SUFFIX)
    text = "a long text that exceeds the limit significantly"
    result = _truncate_prefix(text, limit)
    assert result == text[:limit]


def test_truncate_middle_limit_equals_marker_length():
    """Kill comparison_swap L126: <= -> < would fall through when limit == len(marker)."""
    from hermes_cli.goal_judge_rendering import GOAL_MIDDLE_OMITTED_MARKER, _truncate_middle

    limit = len(GOAL_MIDDLE_OMITTED_MARKER)
    text = "a long text that exceeds the limit significantly"
    result = _truncate_middle(text, limit)
    assert result == text[:limit]

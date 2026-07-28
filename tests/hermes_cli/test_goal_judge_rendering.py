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

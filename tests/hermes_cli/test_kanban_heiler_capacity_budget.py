"""Regression coverage for live iteration-budget failure classification."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.parametrize("budget", (60, 30, 90, 20))
@pytest.mark.parametrize("outcome", ("timed_out", "gave_up"))
def test_live_iteration_budget_exhaustion_is_capacity(budget, outcome):
    error = (
        f"Iteration budget exhausted ({budget}/{budget}) — task could not "
        "complete within the allowed iterations"
    )

    heiler_class, _ = kb._classify_failure(error=error, outcome=outcome)

    assert heiler_class == kb.HEILER_CLASS_CAPACITY


def test_live_wall_clock_timeout_remains_transient():
    heiler_class, _ = kb._classify_failure(
        error="elapsed 306s > limit 300s",
        outcome="timed_out",
    )

    assert heiler_class == kb.HEILER_CLASS_TRANSIENT


@pytest.mark.parametrize(
    ("defect", "expected_class"),
    (
        ("reviewer findings: REQUEST_CHANGES", kb.HEILER_CLASS_REAL_BUG),
        ("acceptance criteria cannot be met", kb.HEILER_CLASS_BAD_SPEC),
    ),
)
def test_budget_exhaustion_does_not_mask_real_defect(defect, expected_class):
    error = (
        "Iteration budget exhausted (60/60) — task could not complete within "
        f"the allowed iterations; {defect}"
    )

    heiler_class, _ = kb._classify_failure(error=error, outcome="timed_out")

    assert heiler_class == expected_class

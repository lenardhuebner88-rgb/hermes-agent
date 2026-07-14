"""Heiler evidence must quote the real silent-block failure signal."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def _task_row(task_id: str) -> sqlite3.Row:
    """Return the same sqlite3.Row surface the silent-block sweep passes."""
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT ? AS id, ? AS title, 'blocked' AS status, "
            "'coder' AS assignee, 0 AS auto_retry_count",
            (task_id, f"Silent block {task_id}"),
        ).fetchone()


def _classify_silent_block(
    task_id: str,
    reason: str,
    *,
    trigger_outcome: str = "blocked",
    blocked_kind: str = "retryable",
    stall_class: str | None = None,
) -> tuple[dict, str, dict, dict]:
    escalation = kb._silent_block_escalation_payload(
        row=_task_row(task_id),
        reason=reason,
        blocked_kind=blocked_kind,
        trigger_outcome=trigger_outcome,
    )
    if stall_class is not None:
        escalation["evidence"]["stall_class"] = stall_class
    heiler_class, evidence = kb._classify_escalation_payload(escalation)
    classification = kb._heiler_classification_payload(
        heiler_class=heiler_class,
        evidence=evidence,
        source=kb.HEILER_SOURCE_SILENT_BLOCK,
        blocked=True,
    )
    return escalation, heiler_class, evidence, classification


def test_text_evidence_quotes_the_matching_real_block_reason():
    real_reason = "REQUEST_CHANGES — AC-1 ist unerfüllt"

    escalation, heiler_class, evidence, _ = _classify_silent_block(
        "t_reviewer_rejection",
        real_reason,
    )

    assert escalation["evidence"]["last_error"] == real_reason
    assert heiler_class == kb.HEILER_CLASS_REAL_BUG
    assert evidence["signal_source"] == "text"
    assert evidence["excerpt"] == real_reason
    assert evidence["matched"] in evidence["excerpt"].lower()


def test_distinct_real_block_reasons_get_distinct_fingerprints():
    first = _classify_silent_block(
        "t_reviewer_rejection",
        "REQUEST_CHANGES — AC-1 ist unerfüllt",
    )[3]
    second = _classify_silent_block(
        "t_red_gate",
        "gate failed: integration test did not persist the receipt",
    )[3]

    assert first["class"] == second["class"] == kb.HEILER_CLASS_REAL_BUG
    assert first["fingerprint"] != second["fingerprint"]


def test_empty_real_block_reason_does_not_fingerprint_the_wrapper():
    escalation, heiler_class, evidence, classification = _classify_silent_block(
        "t_empty_reason",
        "",
    )

    assert escalation["evidence"]["last_error"] == ""
    assert escalation["why_now"].startswith("settled block (last run outcome:")
    assert heiler_class == kb.HEILER_CLASS_UNCLASSIFIED
    assert "excerpt" not in evidence
    assert "fingerprint" not in classification


@pytest.mark.parametrize(
    ("reason", "trigger_outcome", "expected_class"),
    [
        (
            "REQUEST_CHANGES — AC-1 ist unerfüllt",
            "blocked",
            kb.HEILER_CLASS_REAL_BUG,
        ),
        ("opaque worker failure", "timed_out", kb.HEILER_CLASS_TRANSIENT),
        (
            "awaiting operator decision before proceeding",
            "blocked",
            kb.HEILER_CLASS_OPERATOR_GATED,
        ),
        ("", "blocked", kb.HEILER_CLASS_UNCLASSIFIED),
    ],
)
def test_silent_block_live_classes_remain_stable(
    reason: str,
    trigger_outcome: str,
    expected_class: str,
):
    _, heiler_class, _, classification = _classify_silent_block(
        f"t_{expected_class}",
        reason,
        trigger_outcome=trigger_outcome,
    )

    assert heiler_class == expected_class
    assert classification["class"] == expected_class


def test_single_field_callers_keep_their_exact_excerpts():
    failure_error = "gate failed: pytest 3 tests failed"
    _, failure_evidence = kb._classify_failure(
        error=failure_error,
        outcome="gave_up",
    )
    assert failure_evidence["excerpt"] == failure_error

    stall_reason = "auto_decompose failed 3 times"
    _, stall_evidence = kb._classify_failure(
        stall_class="triage_decompose_failed",
        reason=stall_reason,
    )
    assert stall_evidence["excerpt"] == stall_reason


@pytest.mark.parametrize(
    ("blocked_kind", "reason", "expected_match"),
    [
        (
            "operator_question",
            "REQUEST_CHANGES — choose whether the authorized push is in scope",
            "operator_question",
        ),
        (
            "needs_operator",
            "NEEDS_REVISION — operator must choose the acceptable trade-off",
            "needs_operator",
        ),
    ],
)
def test_structured_operator_hold_wins_over_generic_review_verdict_prose(
    blocked_kind: str,
    reason: str,
    expected_match: str,
):
    _, heiler_class, evidence, _ = _classify_silent_block(
        f"t_{blocked_kind}",
        reason,
        blocked_kind=blocked_kind,
    )

    assert heiler_class == kb.HEILER_CLASS_OPERATOR_GATED
    assert evidence["matched"] == expected_match
    assert evidence["signal_source"] == "blocked_kind"


@pytest.mark.parametrize(
    ("reason", "trigger_outcome", "stall_class", "expected_class"),
    [
        (
            "REQUEST_CHANGES — gate failed: pytest tests failed",
            "blocked",
            None,
            kb.HEILER_CLASS_REAL_BUG,
        ),
        (
            "REQUEST_CHANGES — release gate returned an opaque failure",
            "release_gate_red",
            None,
            kb.HEILER_CLASS_REAL_BUG,
        ),
        (
            "NEEDS_REVISION — decomposition produced no runnable work",
            "blocked",
            "triage_decompose_failed",
            kb.HEILER_CLASS_BAD_SPEC,
        ),
    ],
)
def test_structured_defects_override_operator_hold(
    reason: str,
    trigger_outcome: str,
    stall_class: str | None,
    expected_class: str,
):
    _, heiler_class, _, _ = _classify_silent_block(
        "t_structured_defect",
        reason,
        blocked_kind="operator_question",
        trigger_outcome=trigger_outcome,
        stall_class=stall_class,
    )

    assert heiler_class == expected_class


def test_structured_red_worker_gate_overrides_operator_hold():
    escalation = kb._silent_block_escalation_payload(
        row=_task_row("t_structured_gate"),
        reason="REQUEST_CHANGES — operator review required",
        blocked_kind="operator_question",
        trigger_outcome="blocked",
    )
    escalation["evidence"]["worker_gate"] = {
        "passed": False,
        "exit_codes": [1],
    }

    heiler_class, evidence = kb._classify_escalation_payload(escalation)

    assert heiler_class == kb.HEILER_CLASS_REAL_BUG
    assert evidence["matched"] == "worker_gate.passed=false"
    assert evidence["signal_source"] == "worker_gate"

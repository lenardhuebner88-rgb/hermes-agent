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
) -> tuple[dict, str, dict, dict]:
    escalation = kb._silent_block_escalation_payload(
        row=_task_row(task_id),
        reason=reason,
        blocked_kind="retryable",
        trigger_outcome=trigger_outcome,
    )
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

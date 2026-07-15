"""Structural block kinds declassify otherwise opaque silent blocks."""

from __future__ import annotations

from copy import deepcopy
import sqlite3

import pytest

from hermes_cli import kanban_db as kb


LIVE_OPERATOR_QUESTION_REASON = (
    "operator question: write-path change on freigabe/flow archive - "
    "needs your approval"
)
LIVE_TASK_BODY = """## Defect
Several archive paths set `status='archived'` WITHOUT emitting a `task_events` row of
kind='archived', and without stamping `completed_at`:

| path | file:line | 'archived' event on the archived row? |
|---|---|---|
| dismiss_freigabe_hold — the ROOT itself | kanban_db.py:13271 | no (only freigabe_vetoed) |
| complete_freigabe_hold — the ROOT itself | kanban_db.py:13347 | no (only freigabe_completed) |
| veto_operator_escalation | kanban_db.py:13405 | no (only freigabe_vetoed) |
| _merge_flow_children (Flow tab merge) | plugin_api.py:8402 | no event on merge_id at all |

`GET /board/archive` therefore cannot date these rows exactly. On the live board this is
111 of 4213 archived tasks (2.6%), 18 of them vetoed freigabe roots.

## Mitigation already shipped (main 14f50e2f3)
`archived_at` now falls back to the task's NEWEST event before falling back to `created_at`, so
these rows are dated by (roughly) when they left the board instead of by the day they were born.
That is a proxy, not truth.

## Proper fix
Emit a durable `archived` event on every path that archives a task (the four above). Then
`archived_at` is exact for all future rows and the proxy fallback only ever covers history.

## Provenance
Found by the Codex cross-family review lens (2026-07-13), CONFIRMED by an auditor pass with the
table above, and the 2.6% blast radius measured against a read-only copy of the live board."""
LIVE_REVIEWER_REJECTION_REASON = (
    "Urteil: NEEDS_REVISION\n"
    "Warum: Der Silent-Block-Guard ignoriert den nun autoritativen `block_kind` "
    "und kann explizite `transient`-Blocks mit frageartiger Prosa eskalieren und "
    "dadurch dauerhaft vom Retry ausschließen. Zusätzlich überspringt der neue "
    "`needs_input`-Early-Return die bestehende Late-`RESULT:`-Completion.\n"
    "Fix: Block-Kind-Auflösung zwischen Retry-Lane und Silent-Sweep "
    "vereinheitlichen und Late-Result vor dem needs_input-Skip prüfen. Benötigte "
    "Verifikation: Gateway-Reihenfolge für transient/frageartige Reason, "
    "needs_input/harmlose Reason im Sweep und needs_input mit nachträglichem "
    "Worker-RESULT regressieren. Residual Risk: Keine mechanischen Gates erneut "
    "ausgeführt; diese sind durch den Verifier bereits belegt."
)

# Read-only capture of live operator_escalation event 50838 for t_95e9246c.
LIVE_STORED_ESCALATION_PAYLOAD = {
    "task": {
        "id": "t_95e9246c",
        "title": (
            "Archive paths that never emit an 'archived' event "
            "(freigabe roots, flow merge)"
        ),
        "status": "blocked",
        "assignee": "coder",
    },
    "why_now": (
        "settled block (last run outcome: blocked) with no operator_escalation — "
        "the self-healing retry lane will not (further) act on it"
    ),
    "attempts_already_made": 0,
    "evidence": {
        "trigger_outcome": "blocked",
        "last_error": LIVE_OPERATOR_QUESTION_REASON,
        "blocked_kind": "operator_question",
        "source": "silent_block_sweep",
    },
    "recommended_human_action": (
        "inspect the task, answer any operator question, and decide whether "
        "to unblock/reassign/close — the worker loop cannot proceed alone"
    ),
    "blocked_action_boundary": [
        "DB schema/data mutation",
        "destructive delete",
        "secrets/credentials",
    ],
}


def _live_task_row(explicit_block_kind: str | None) -> sqlite3.Row:
    """Return the sqlite3.Row shape read by ``escalate_silent_blocks_sweep``."""
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT ? AS id, ? AS title, 'blocked' AS status, 'coder' AS assignee,
                   ? AS body, 0 AS auto_retry_count, ? AS block_kind
            """,
            (
                LIVE_STORED_ESCALATION_PAYLOAD["task"]["id"],
                LIVE_STORED_ESCALATION_PAYLOAD["task"]["title"],
                LIVE_TASK_BODY,
                explicit_block_kind,
            ),
        ).fetchone()


def _production_payload(
    reason: str,
    *,
    explicit_block_kind: str | None = None,
    verdict: str | None = None,
) -> tuple[str, dict]:
    """Run the same classifier -> payload-builder chain as the silent sweep."""
    row = _live_task_row(explicit_block_kind)
    blocked_kind = kb._blocked_kind_for_auto_retry(
        reason,
        explicit_block_kind=row["block_kind"],
        verdict=verdict,
        auto_retry_count=int(row["auto_retry_count"] or 0),
        body_hash=kb._task_body_hash(row["body"]),
        last_auto_retry_body_hash=None,
    )
    payload = kb._silent_block_escalation_payload(
        row=row,
        reason=reason,
        blocked_kind=blocked_kind,
        trigger_outcome="blocked",
    )
    return blocked_kind, payload


def test_live_operator_question_full_production_chain_matches_stored_payload():
    blocked_kind, payload = _production_payload(LIVE_OPERATOR_QUESTION_REASON)

    assert blocked_kind == "operator_question"
    assert payload == LIVE_STORED_ESCALATION_PAYLOAD

    heiler_class, evidence = kb._classify_escalation_payload(payload)
    assert heiler_class == kb.HEILER_CLASS_OPERATOR_GATED
    assert evidence["signal_source"] == "blocked_kind"
    assert evidence["matched"] == "operator_question"


def test_explicit_needs_input_passes_through_full_production_chain():
    blocked_kind, payload = _production_payload(
        LIVE_OPERATOR_QUESTION_REASON,
        explicit_block_kind="needs_input",
    )

    assert blocked_kind == "needs_input"
    heiler_class, evidence = kb._classify_escalation_payload(payload)
    assert heiler_class == kb.HEILER_CLASS_OPERATOR_GATED
    assert evidence["signal_source"] == "blocked_kind"
    assert evidence["matched"] == "needs_input"


def test_structural_kind_does_not_mask_live_reviewer_rejection():
    blocked_kind, payload = _production_payload(
        LIVE_REVIEWER_REJECTION_REASON,
        explicit_block_kind="needs_input",
        verdict="REQUEST_CHANGES",
    )

    assert blocked_kind == "needs_input"
    heiler_class, evidence = kb._classify_escalation_payload(payload)
    assert heiler_class == kb.HEILER_CLASS_REAL_BUG
    assert evidence["signal_source"] == "text"
    assert evidence["matched"] == "needs_revision"


@pytest.mark.parametrize(
    ("blocked_kind", "gate_key", "gate_evidence"),
    [
        ("operator_question", "worker_gate", {"passed": False}),
        ("needs_input", "gate", {"status": "red"}),
    ],
)
def test_structural_kind_does_not_mask_opaque_red_gate(
    blocked_kind,
    gate_key,
    gate_evidence,
):
    payload = deepcopy(LIVE_STORED_ESCALATION_PAYLOAD)
    payload["why_now"] = "opaque silent block"
    payload["evidence"].update(
        last_error="",
        blocked_kind=blocked_kind,
    )
    payload["evidence"][gate_key] = gate_evidence

    heiler_class, evidence = kb._classify_escalation_payload(payload)

    assert heiler_class == kb.HEILER_CLASS_UNCLASSIFIED
    assert evidence["signal_source"] == "default"


def test_transient_normalization_does_not_map_retryable_default():
    blocked_kind, payload = _production_payload(
        "opaque worker park",
        explicit_block_kind="transient",
    )

    assert blocked_kind == "retryable"
    heiler_class, evidence = kb._classify_escalation_payload(payload)
    assert heiler_class == kb.HEILER_CLASS_UNCLASSIFIED
    assert evidence["signal_source"] == "default"


@pytest.mark.parametrize("blocked_kind", ["", None])
def test_missing_structural_kind_preserves_default(blocked_kind: str | None):
    payload = deepcopy(LIVE_STORED_ESCALATION_PAYLOAD)
    payload["evidence"]["blocked_kind"] = blocked_kind

    heiler_class, evidence = kb._classify_escalation_payload(payload)
    assert heiler_class == kb.HEILER_CLASS_UNCLASSIFIED
    assert evidence["signal_source"] == "default"
    assert evidence["matched"] == "default"


def test_release_gate_candidate_keeps_specific_provenance():
    payload = deepcopy(LIVE_STORED_ESCALATION_PAYLOAD)
    payload["evidence"].update(
        last_error=kb.RELEASE_GATE_BLOCK_REASON,
        release_gate_candidate=True,
    )

    heiler_class, evidence = kb._classify_escalation_payload(payload)
    assert heiler_class == kb.HEILER_CLASS_OPERATOR_GATED
    assert evidence["signal_source"] == "release_gate_candidate"
    assert evidence["matched"] == "release_gate_candidate"

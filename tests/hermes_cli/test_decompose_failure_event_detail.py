"""decompose_attempt_failed events must retain DecomposeOutcome.detail.

Live telemetry for t_afc459f0 only ever recorded
``{"reason": "LLM error: BadRequestError"}`` — the original exception
message (context-length, bad extra_body, …) was dropped before the event
payload. 392a2eb30 put the detail on DecomposeOutcome; this suite pins the
DB-telemetry leg that still discarded it.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_decompose import DecomposeOutcome, _format_exc_detail


# Authentic shape of the BadRequestError messages that hit the live board
# (see tests/run_agent/test_413_compression.py and the t_afc459f0 stall).
_REAL_CTX_LEN_MSG = (
    "Error code: 400 - This endpoint's maximum context length is 128000 tokens. "
    "However, you requested about 270460 tokens."
)


def _failed_events(conn, task_id: str) -> list[dict]:
    return [
        e.payload or {}
        for e in kb.list_events(conn, task_id)
        if e.kind == kb.DECOMPOSE_ATTEMPT_FAILED_EVENT
    ]


def test_record_decompose_failure_without_detail_is_byte_identical(kanban_home):
    """Back-compat: omitting detail must not change the event payload."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-detail", triage=True)
        kb.record_decompose_failure(
            conn, tid, reason="LLM error: BadRequestError",
        )
        payloads = _failed_events(conn, tid)

    assert len(payloads) == 1
    assert payloads[0] == {"reason": "LLM error: BadRequestError"}
    assert "error_detail" not in payloads[0]
    # Raw JSON stored in the row is also free of error_detail.
    with kb.connect_closing() as conn:
        raw = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (tid, kb.DECOMPOSE_ATTEMPT_FAILED_EVENT),
        ).fetchone()["payload"]
    assert json.loads(raw) == {"reason": "LLM error: BadRequestError"}
    assert "error_detail" not in raw


def test_record_decompose_failure_writes_capped_single_line_error_detail(
    kanban_home,
):
    """Optional detail lands as error_detail: single-line, <=500 chars."""
    messy = "line1\nline2\n" + ("x" * 600)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="with-detail", triage=True)
        kb.record_decompose_failure(
            conn,
            tid,
            reason="LLM error: BadRequestError",
            detail=messy,
        )
        payloads = _failed_events(conn, tid)

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["reason"] == "LLM error: BadRequestError"
    assert "error_detail" in payload
    detail = payload["error_detail"]
    assert "\n" not in detail
    assert "line1 line2" in detail
    assert len(detail) <= 500


def test_reason_string_stays_classification_key_with_detail(kanban_home):
    """reason stays EXACTLY 'LLM error: <ClassName>' so transient carve-out holds."""
    detail = _format_exc_detail(Exception(_REAL_CTX_LEN_MSG))
    reason = "LLM error: BadRequestError"
    assert kb._decompose_failure_is_transient(reason) is False

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="classify", triage=True)
        kb.record_decompose_failure(conn, tid, reason=reason, detail=detail)
        payloads = _failed_events(conn, tid)

    assert payloads[0]["reason"] == reason
    assert "maximum context length" in payloads[0]["error_detail"]
    # Classification still keys off reason, not the free-text detail.
    assert kb._decompose_failure_is_transient(payloads[0]["reason"]) is False


def test_gateway_bump_forwards_outcome_detail_into_event(kanban_home):
    """Production gateway path: _bump_decompose_counter forwards outcome.detail.

    Mirrors gateway/kanban_watchers.py ok=False branch: a decompose_task
    ok=False outcome with real _format_exc_detail shape must land the
    distinctive substring in the event payload.
    """
    from gateway import kanban_watchers as watchers

    detail = _format_exc_detail(Exception(_REAL_CTX_LEN_MSG))
    assert "maximum context length" in detail

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="gateway-fwd", triage=True)
        outcome = DecomposeOutcome(
            task_id=tid,
            ok=False,
            reason="LLM error: BadRequestError",
            detail=detail,
        )
        # Real production helper (module-level, used by the auto-decompose tick).
        watchers._bump_decompose_counter(
            tid, ok=False, reason=outcome.reason, detail=outcome.detail,
        )
        payloads = _failed_events(conn, tid)

    assert len(payloads) == 1
    assert payloads[0]["reason"] == "LLM error: BadRequestError"
    assert "maximum context length" in payloads[0]["error_detail"]


def test_cli_decompose_forwards_outcome_detail_into_event(kanban_home, capsys):
    """Production CLI path: _cmd_decompose passes outcome.detail through."""
    from hermes_cli import kanban as kb_cli

    detail = _format_exc_detail(Exception(_REAL_CTX_LEN_MSG))

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-fwd", triage=True)

    fake = DecomposeOutcome(
        task_id=tid,
        ok=False,
        reason="LLM error: BadRequestError",
        detail=detail,
    )
    ns = argparse.Namespace(
        task_id=tid, all_triage=False, tenant=None,
        author="tester", json=False,
    )
    with patch(
        "hermes_cli.kanban_decompose.decompose_task",
        return_value=fake,
    ):
        rc = kb_cli._cmd_decompose(ns)
    assert rc == 1

    with kb.connect_closing() as conn:
        payloads = _failed_events(conn, tid)

    assert len(payloads) == 1
    assert payloads[0]["reason"] == "LLM error: BadRequestError"
    assert "maximum context length" in payloads[0]["error_detail"]
    # Cap stderr noise from the CLI failure path.
    _ = capsys.readouterr()

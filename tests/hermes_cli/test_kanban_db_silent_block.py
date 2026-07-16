"""Kanban DB tests: silent block.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

from tests.hermes_cli._kanban_test_helpers import (
    _operator_escalations,
    _escalation_event,
    _heiler_events,
)

def _force_review_settled_block(conn, title="review rework"):
    """Create a settled REQUEST_CHANGES block with exhausted auto_retry budget."""
    t = kb.create_task(conn, title=title, assignee="coder")
    conn.execute(
        "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
        (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
    )
    kb.claim_task(conn, t)
    # Review-origin claim so block_task stamps review_revision.
    run_id = kb.get_task(conn, t).current_run_id
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            t,
            "claimed",
            {"source_status": "review", "run_id": run_id},
            run_id=run_id,
        )
    assert kb.block_task(
        conn,
        t,
        reason="Urteil: NEEDS_REVISION\nWarum: missing regression for focus history",
        expected_run_id=run_id,
    )
    task = kb.get_task(conn, t)
    assert task.status == "blocked"
    assert task.block_kind == "review_revision"
    return t


def test_silent_block_sweep_escalates_operator_question_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="needs op", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential should I use?")
        # operator_question → settled, no escalation yet → silent
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        assert _operator_escalations(conn, t) == []

        res = kb.escalate_silent_blocks_sweep(conn, now=base)

        assert [e["task_id"] for e in res["escalated"]] == [t]
        assert len(_operator_escalations(conn, t)) == 1
        # silent set drained + idempotent re-run adds nothing
        assert kb.silent_block_task_ids(conn, now=base) == []
        kb.escalate_silent_blocks_sweep(conn, now=base)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_skips_transient_retryable_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="transient", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")
        # within retry budget + recent → the auto-retry lane is still on it
        assert kb.silent_block_task_ids(conn, now=base) == []
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert _operator_escalations(conn, t) == []


def test_silent_block_sweep_escalates_when_retry_budget_exhausted(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="exhausted", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="still broken")
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_escalates_block_without_run(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="parked", assignee="alice")
        # raw flip to blocked, no blocked run (mirrors contract/integration park)
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                t,
                "blocked",
                {"kind": None, "recurrences": 1, "status": "blocked"},
            )
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        escalation = _escalation_event(conn, t)
        heiler = _heiler_events(conn, t)[0]

    assert escalation.payload["evidence"]["last_error"] == ""
    assert heiler.payload["class"] == kb.HEILER_CLASS_UNCLASSIFIED
    assert heiler.payload["evidence"]["signal_source"] == "default"
    assert "excerpt" not in heiler.payload["evidence"]
    assert "fingerprint" not in heiler.payload


def test_silent_block_sweep_uses_blocked_event_reason_without_run_text(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    reason = (
        "no actionable implementation spec: RAW-FLIP-EVENT-REASON "
        + "x" * 500
    )
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="event reason", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason=reason)
        blocked_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert json.loads(blocked_event["payload"])["reason"] == reason

        # Contract/integration parks can retain only the production blocked-event
        # payload. Remove the duplicate run text while preserving that real shape.
        conn.execute(
            "UPDATE task_runs SET summary = NULL, error = NULL WHERE task_id = ?",
            (t,),
        )
        conn.commit()
        run = conn.execute(
            "SELECT summary, error FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert run is not None
        assert not (run["summary"] or "").strip()
        assert not (run["error"] or "").strip()

        result = kb.escalate_silent_blocks_sweep(
            conn,
            retry_limit=kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
            failure_limit=kb.DEFAULT_FAILURE_LIMIT,
            backoff_seconds=kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
        )
        escalation = _escalation_event(conn, t)
        heiler = _heiler_events(conn, t)[0]

    assert [item["task_id"] for item in result["escalated"]] == [t]
    assert escalation.payload["evidence"]["last_error"] == reason[:500]
    assert heiler.payload["class"] == kb.HEILER_CLASS_BAD_SPEC
    assert heiler.payload["evidence"]["signal_source"] == "text"
    assert heiler.payload["evidence"]["excerpt"] == reason[:300]
    assert heiler.payload["fingerprint"] == kb._error_fingerprint(reason[:300])


def test_silent_block_sweep_escalates_transient_past_grace(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A retryable block inside budget but blocked far longer than self-heal
    could take (lane disabled/stuck) must still surface — the guarantee holds
    independent of the auto_retry_blocked config flag."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")
        grace = kb._self_heal_grace_seconds(
            kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
            kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
        )
        # still inside grace → transient, not surfaced
        assert kb.silent_block_task_ids(conn, now=base + grace) == []
        # past grace → settled, surfaced
        assert kb.silent_block_task_ids(conn, now=base + grace + 1) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base + grace + 1)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_does_not_re_escalate_existing(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="already", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which path?")
        kb._append_event(conn, t, kb.OPERATOR_ESCALATION_EVENT, {"why_now": "x"})
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == []
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_writes_inline_heiler_classification(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """ESCALATION-INLINE-CLASSIFY-S1 (defense-in-depth): the silent-block sweep
    pairs a heiler_classification AT the escalation site, in the same write_txn,
    so coverage is complete the instant the escalation is written — no separate
    classify_escalations_sweep poll required. Exactly one classification,
    referencing the escalation event, tagged with the inline silent-block
    source, with a belegter (signal-source) evidence reference, not a guess
    (AC-2)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="classify", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential?")
        # Only the silent-block sweep runs — deliberately NOT the classify sweep.
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        heilers = _heiler_events(conn, t)

    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_SILENT_BLOCK
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert heilers[0].payload["blocked"] is True
    assert heilers[0].payload["evidence"].get("signal_source")


def test_silent_block_sweep_inline_matches_sweep_and_sweep_skips(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """The inline class is byte-identical to what the backfill sweep would
    derive from the same persisted escalation payload (defense-in-depth, NOT
    divergence), and classify_escalations_sweep then adds nothing because the
    escalation is already paired."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="classify", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential?")
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        inline = _heiler_events(conn, t)[0]
        expected_class, _ = kb._classify_escalation_payload(esc.payload)

        summary = kb.classify_escalations_sweep(conn, now=base)
        heilers = _heiler_events(conn, t)

    assert inline.payload["class"] == expected_class
    assert summary["classified"] == []
    assert len(heilers) == 1


def test_silent_block_sweep_carries_real_run_outcome(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """SILENT-BLOCK-AUTONOMY-S1 + reason fidelity: budget exhaustion settles
    without a blocked run. The sweep routes to hold_capacity (no operator page,
    no re-dispatch) and classifies capacity from the real last-run message."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="budget", assignee="alice")
        kb.claim_task(conn, t)
        # close the run as budget-exhausted (NOT 'blocked') then flip to blocked,
        # mirroring the iteration-budget park: no blocked run for the lane.
        with kb.write_txn(conn):
            kb._end_run(
                conn, t, outcome="iteration_budget_exhausted",
                status="iteration_budget_exhausted",
                summary="iteration budget exhausted; continuation limit "
                        "exhausted (60/60)",
            )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert any(h["task_id"] == t for h in res["autonomy_held"])
        assert _operator_escalations(conn, t) == []
        heiler = _heiler_events(conn, t)[0]
        routes = [
            e for e in kb.list_events(conn, t) if e.kind == kb.AUTONOMY_ROUTED_EVENT
        ]

    assert heiler.payload["class"] == kb.HEILER_CLASS_CAPACITY
    assert routes[-1].payload["action"] == "hold_capacity"
    # silent set drained via hold (no operator page)
    with kb.connect_closing() as conn:
        assert kb.silent_block_task_ids(conn, now=base) == []


def test_escalation_sweep_classifies_capacity_protocol_and_deliverable_signatures(
    kanban_home,
):
    """Persisted escalation payloads retain all three non-default classes."""
    with kb.connect_closing() as conn:
        task_ids = [
            kb.create_task(conn, title=title, assignee="coder")
            for title in ("timeout", "protocol", "deliverable")
        ]
        payloads = (
            {"why_now": "settled block", "evidence": {
                "trigger_outcome": "timed_out", "last_error": ""}},
            {"why_now": "settled block", "evidence": {
                "trigger_outcome": "blocked",
                "last_error": "worker exited cleanly (rc=0) without calling "
                "kanban_complete or kanban_block — protocol violation"}},
            {"why_now": "settled block", "evidence": {
                "trigger_outcome": "deliverable_posted_not_completed",
                "last_error": ""}},
        )
        for task_id, payload in zip(task_ids, payloads):
            kb._append_event(conn, task_id, kb.OPERATOR_ESCALATION_EVENT, payload)
        summary = kb.classify_escalations_sweep(conn)
        classifications = [
            [event for event in kb.list_events(conn, task_id)
             if event.kind == kb.HEILER_CLASSIFICATION_EVENT][0]
            for task_id in task_ids
        ]

    assert [item["class"] for item in summary["classified"]] == [
        kb.HEILER_CLASS_CAPACITY,
        kb.HEILER_CLASS_PROTOCOL_NONCOMPLIANCE,
        kb.HEILER_CLASS_OPERATOR_GATED,
    ]
    assert [event.payload["evidence"]["matched"] for event in classifications] == [
        "timed_out",
        "without calling kanban_complete",
        "deliverable_posted_not_completed",
    ]


def test_silent_block_sweep_classifies_missing_spec_block_as_bad_spec(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A settled block whose reason is a spec gap classifies bad-spec, not the
    real-bug default — the dominant live silent-block mislabel: a real
    block-error IS carried, the classifier just lacked the spec-gap signal."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="vague", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        kb.block_task(
            conn, t,
            reason="No actionable implementation spec: title is too vague",
        )
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        heiler = _heiler_events(conn, t)[0]

    assert heiler.payload["class"] == kb.HEILER_CLASS_BAD_SPEC


def test_silent_block_sweep_classifies_superseded_block_as_operator_intent(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """HEILER-CLASSIFY-SIGNAL-GAP-S2 + B4a: a SUPERSEDED block reason is
    operator-intent (not a product defect). Since 2026-07-15 the path also
    auto-archives so it never sits as a silent-block zombie for escalation
    (live precedent: t_2491b29e)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    reason = (
        "Superseded: operator requested direct Claude CLI review "
        "instead of Kanban reviewer."
    )
    assert kb.reason_looks_superseded(reason)
    # Heiler text path still treats "superseded:" as operator-intent.
    assert any(
        cls == kb.HEILER_CLASS_OPERATOR_INTENT
        and any("superseded:" in s for s in signals)
        for cls, signals in kb._HEILER_TEXT_SIGNALS
    )
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="superseded review", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason=reason)
        # B4a: terminal archive, not a sticky blocked silent-sweep candidate.
        assert kb.get_task(conn, t).status == "archived"
        assert kb.silent_block_task_ids(conn, now=base) == []


def test_silent_block_sweep_completed_outcome_avoids_default_bucket(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """HEILER-CLASSIFY-SIGNAL-GAP-S2: a settled block parked via a raw status
    flip AFTER a green run (release-gate park: t_76401275/t_6931affd) has no
    blocked run, so the escalation falls back to the completed run's summary
    — and a passing run is not a product defect, so it must not land in the
    real-bug default."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="release gate park", assignee="verifier")
        kb.claim_task(conn, t)
        with kb.write_txn(conn):
            kb._end_run(
                conn, t, outcome="completed", status="completed",
                summary="release gate green after 0 fixer attempt(s)",
            )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        heiler = _heiler_events(conn, t)[0]

    assert esc.payload["evidence"]["trigger_outcome"] == "completed"
    assert "release gate green" in esc.payload["evidence"]["last_error"]
    assert heiler.payload["class"] == kb.HEILER_CLASS_OPERATOR_INTENT


def test_silent_block_sweep_carves_out_strategist_meta_task(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """STRATEGIST-META-CARVEOUT: a blocked strategist-cron task (the loop's own
    output) is NOT swept — so the self-improvement loop never reads its own
    parked proposal back as a real-bug product-defect signal. A real code task
    blocked the same way IS still surfaced (AC-2: carve-out strictly scoped to
    created_by=strategist-cron, not real code tasks)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        meta = kb.create_task(
            conn, title="strategist proposal", assignee="alice",
            created_by=kb.STRATEGIST_CREATED_BY,
        )
        kb.claim_task(conn, meta)
        kb.block_task(conn, meta, reason="Which lever should I pull?")
        real = kb.create_task(
            conn, title="real code task", assignee="bob", created_by="user",
        )
        kb.claim_task(conn, real)
        kb.block_task(conn, real, reason="Which credential should I use?")

        ids = kb.silent_block_task_ids(conn, now=base)
        assert meta not in ids
        assert real in ids

        res = kb.escalate_silent_blocks_sweep(conn, now=base)

        meta_operator_escalations = _operator_escalations(conn, meta)
        meta_heiler_events = _heiler_events(conn, meta)
        real_operator_escalations = _operator_escalations(conn, real)

    assert meta not in [e["task_id"] for e in res["escalated"]]
    assert meta_operator_escalations == []
    assert meta_heiler_events == []
    # the real code task is untouched by the carve-out — still surfaced
    assert len(real_operator_escalations) == 1


def test_silent_block_autonomy_reready_review_once(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Settled review rework re-readies once (no operator page) within token bound."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = _force_review_settled_block(conn)
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert any(r["task_id"] == t and r["action"] == "reready_review" for r in res["autonomy_reready"])
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert _operator_escalations(conn, t) == []
        routes = [e for e in kb.list_events(conn, t) if e.kind == kb.AUTONOMY_ROUTED_EVENT]
        assert len(routes) == 1
        assert routes[0].payload["action"] == "reready_review"
        # Findings preserved for the next worker without an extra model call.
        comments = kb.list_comments(conn, t)
        assert any("NEEDS_REVISION" in (c.body or "") for c in comments)


def test_silent_block_autonomy_review_second_episode_escalates(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Lifetime cap: after one autonomy reready, a new block episode escalates
    under default maxes (no test override). Prevents review thrash token loops."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = _force_review_settled_block(conn, title="review loop")
        res1 = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res1["autonomy_reready"]
        assert kb._autonomy_reready_total(conn, t) == 1
        # Second worker attempt: claim + review block again (new episode).
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                t,
                "claimed",
                {"source_status": "review", "run_id": run_id},
                run_id=run_id,
            )
        assert kb.block_task(
            conn,
            t,
            reason="Urteil: NEEDS_REVISION\nWarum: still broken after autonomy reready",
            expected_run_id=run_id,
        )
        # Episode-local count is 0, but task-lifetime total is 1 → escalate.
        assert kb._autonomy_reready_count(conn, t) == 0
        assert kb._autonomy_reready_total(conn, t) == 1
        res2 = kb.escalate_silent_blocks_sweep(conn, now=base)  # default bounds
        assert any(e["task_id"] == t for e in res2["escalated"])
        assert res2["autonomy_reready"] == []
        assert len(_operator_escalations(conn, t)) == 1
        esc = _operator_escalations(conn, t)[0]
        assert esc.payload.get("autonomy_reready_exhausted") is True
        assert kb.get_task(conn, t).status == "blocked"


def test_silent_block_autonomy_hold_capacity_no_operator(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="token cap", assignee="coder")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (t,)).fetchone()
        assert kb._park_budget_runaway(conn, row, token_sum=5000, cap=100, runs=3)
        # Budget park already wrote operator_escalation historically — clear that
        # path by using a raw capacity-stamped block without prior escalation.
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (t,))
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='capacity', "
            "auto_retry_count=? WHERE id=?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert any(h["action"] == "hold_capacity" for h in res["autonomy_held"])
        assert _operator_escalations(conn, t) == []
        assert kb.get_task(conn, t).status == "blocked"
        # Hold removes from silent set; re-sweep is a no-op.
        assert kb.silent_block_task_ids(conn, now=base) == []
        res2 = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res2["escalated"] == []
        assert res2["autonomy_held"] == []


def test_silent_block_autonomy_hold_integration(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="merge park", assignee="coder")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        assert kb._park_integration(
            conn, t, {"reason": "post-merge gate failed: pytest"}, expected_run_id=run_id
        )
        assert kb.get_task(conn, t).block_kind == "integration"
        # Integration park does not write operator_escalation → silent set.
        assert t in kb.silent_block_task_ids(conn, now=base)
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert any(h["action"] == "hold_integration" for h in res["autonomy_held"])
        assert _operator_escalations(conn, t) == []


def test_review_request_changes_never_false_operator_question():
    """Autonomy gap: review findings often contain regex bait (token, ?,
    migration, delete). After the first auto-retry those must stay retryable
    so the second budget slot and reready_review still fire — not operator_question.
    """
    bait = (
        "Urteil: NEEDS_REVISION\n"
        "Warum: token accounting wrong; which migration to delete?\n"
        "Fix: push deploy-safe alter path."
    )
    # First pass — already protected historically
    assert (
        kb._blocked_kind_for_auto_retry(
            bait, verdict="REQUEST_CHANGES", auto_retry_count=0
        )
        == "retryable"
    )
    # Second pass after auto-retry with CHANGED body — previously false operator_question
    assert (
        kb._blocked_kind_for_auto_retry(
            bait,
            verdict="REQUEST_CHANGES",
            auto_retry_count=1,
            body_hash="aaa",
            last_auto_retry_body_hash="bbb",
        )
        == "retryable"
    )
    # review_revision kind without explicit verdict still protected
    assert (
        kb._blocked_kind_for_auto_retry(
            bait,
            explicit_block_kind="review_revision",
            auto_retry_count=1,
            body_hash="aaa",
            last_auto_retry_body_hash="bbb",
        )
        == "retryable"
    )
    # Same-body brake still holds (quality, not thrash)
    assert (
        kb._blocked_kind_for_auto_retry(
            bait,
            verdict="REQUEST_CHANGES",
            auto_retry_count=1,
            body_hash="same",
            last_auto_retry_body_hash="same",
        )
        == "needs_operator"
    )
    # Non-review prose still pages for real operator questions
    assert (
        kb._blocked_kind_for_auto_retry(
            "Which credential should I use?", auto_retry_count=0
        )
        == "operator_question"
    )


def test_blocked_kind_superseded_and_capacity_never_auto_retryable():
    """Skill-audit fan-out: SUPERSEDED / capacity prose must not re-dispatch
    when block_kind is NULL (writers often leave it empty).
    """
    superseded_reasons = [
        "SUPERSEDED: Dieser Synthesepfad ist redundant. Nicht erneut dispatchen.",
        "superseded: old review path; do not re-dispatch",
        "SUPERSEDED wegen reinem Kapazitaetsfehler. Nicht manuell freigeben.",
        "Path archived — kein Re-Dispatch.",
    ]
    for reason in superseded_reasons:
        assert (
            kb._blocked_kind_for_auto_retry(reason) == "superseded"
        ), reason

    capacity_reasons = [
        "Iteration budget exhausted (12/12) — task could not complete "
        "within the allowed iterations",
        "budget exhausted (6/6)",
        "tool-calling iteration budget exhausted",
    ]
    for reason in capacity_reasons:
        assert kb._blocked_kind_for_auto_retry(reason) == "capacity", reason

    # Explicit kinds still win
    assert (
        kb._blocked_kind_for_auto_retry(
            "anything", explicit_block_kind="capacity"
        )
        == "capacity"
    )
    # Harmless block prose stays retryable
    assert (
        kb._blocked_kind_for_auto_retry("Worker noted which assertion failed")
        == "retryable"
    )
    # Review feedback path still preferred over capacity/supersede bait in findings
    assert (
        kb._blocked_kind_for_auto_retry(
            "NEEDS_REVISION: iteration budget note is irrelevant here",
            verdict="REQUEST_CHANGES",
        )
        == "retryable"
    )


def test_blocked_kind_deterministic_fail_fast_markers_need_operator():
    """Deterministic spawn-refusal / decompose-LLM-client-error markers never
    self-heal on retry — auto-retry must escalate, not respawn (live incident
    2026-07-15, t_df10f6b1)."""
    for marker in kb._DETERMINISTIC_SPAWN_FAILURE_MARKERS:
        reason = f"worktree provisioning: {marker} for assignee coder"
        assert kb._blocked_kind_for_auto_retry(reason) == "needs_operator", marker

    for reason in kb._DECOMPOSE_DETERMINISTIC_LLM_ERROR_REASONS:
        assert kb._blocked_kind_for_auto_retry(reason) == "needs_operator", reason
        # Case-insensitive, matching how reasons are recorded/read elsewhere.
        assert kb._blocked_kind_for_auto_retry(reason.upper()) == "needs_operator"


def test_dispatch_auto_retry_needs_operator_deterministic_spawn_failure_marker(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Auto-retry must not respawn a task blocked on a deterministic fail-fast
    marker, even after the normal backoff window (t_df10f6b1)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(
            conn,
            t,
            reason="worktree provisioning: spawn_refused_allowlist_unenforceable",
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [
            e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"
        ][-1]
        assert event.payload["blocked_kind"] == "needs_operator"


def test_block_task_superseded_auto_archives(kanban_home):
    """B4a: SUPERSEDED block leaves the active board (archived, not blocked)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="dead synthesis path", assignee="research")
        kb.claim_task(conn, t)
        assert kb.block_task(
            conn,
            t,
            reason=(
                "SUPERSEDED: Dieser Synthesepfad ist redundant. "
                "Nicht erneut dispatchen."
            ),
        )
        task = kb.get_task(conn, t)
        assert task.status == "archived"
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (t,),
            )
        ]
        assert "blocked" in kinds
        assert "archived" in kinds
        assert "superseded_archived" in kinds


def test_block_task_capacity_without_supersede_stays_blocked(kanban_home):
    """Capacity death alone parks blocked (no auto-archive without SUPERSEDED)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="capacity", assignee="research")
        kb.claim_task(conn, t)
        assert kb.block_task(
            conn,
            t,
            reason=(
                "Iteration budget exhausted (12/12) — task could not complete "
                "within the allowed iterations"
            ),
            kind="capacity",
        )
        assert kb.get_task(conn, t).status == "blocked"


def test_dispatch_auto_retry_skips_superseded_and_capacity_null_kind(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Capacity null-kind stays blocked and non-retryable; SUPERSEDED archives
    at block time so it never re-enters auto_retry.
    """
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t_sup = kb.create_task(conn, title="superseded path", assignee="coder")
        kb.claim_task(conn, t_sup)
        kb.block_task(
            conn,
            t_sup,
            reason=(
                "SUPERSEDED: Dieser Synthesepfad ist redundant. "
                "Nicht erneut dispatchen."
            ),
        )
        assert kb.get_task(conn, t_sup).status == "archived"

        t_cap = kb.create_task(conn, title="capacity death", assignee="research")
        kb.claim_task(conn, t_cap)
        kb.block_task(
            conn,
            t_cap,
            reason=(
                "Iteration budget exhausted (12/12) — task could not complete "
                "within the allowed iterations"
            ),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = NULL WHERE id = ?", (t_cap,)
            )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t_sup).status == "archived"
        assert kb.get_task(conn, t_cap).status == "blocked"


def test_apply_inventory_lane_contract_reroutes_research_inventory():
    assignee, kind, warn = kb.apply_inventory_lane_contract(
        assignee="research",
        title="Hermes Skill-Audit A: Inventar, Struktur und technische Hygiene",
        body=(
            "Read-only audit der aktiven Hermes-Skill-Landschaft. "
            "Scope: /home/piet/.hermes/skills sowie profiles/*/skills; "
            "Inventar aller SKILL.md, Frontmatter, Duplikate."
        ),
        kind=None,
    )
    assert assignee == "coder"
    assert kind == "analysis"
    assert warn and "inventory_lane_contract" in warn
    # Existing kind=research is forced to analysis (not left as research).
    _, kind2, _ = kb.apply_inventory_lane_contract(
        assignee="research",
        title="Skill-Audit Inventar",
        body="Enumerate all SKILL.md under ~/.hermes/skills",
        kind="research",
    )
    assert kind2 == "analysis"


def test_nicht_manuell_freigeben_alone_does_not_archive(kanban_home):
    """Archive requires strong SUPERSEDED markers, not freigabe prose alone."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="freigabe note", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(
            conn,
            t,
            reason="Nicht manuell freigeben — wait for operator hold.",
        )
        assert kb.get_task(conn, t).status == "blocked"
        # Still non-retryable via the broader SUPERSEDED skip-retry RE.
        assert kb._blocked_kind_for_auto_retry(
            "Nicht manuell freigeben — wait for operator hold."
        ) == "superseded"


def test_apply_inventory_lane_contract_spares_synthesis_research():
    assignee, kind, warn = kb.apply_inventory_lane_contract(
        assignee="research",
        title="Hermes Skill-Audit: finale Synthese aus Critic + Scan",
        body=(
            "Synthetisiere ausschließlich die Parent-Handoffs. "
            "Keine eigenen breiten Scans."
        ),
        kind="research",
    )
    assert assignee == "research"
    assert kind == "research"
    assert warn is None


def test_create_task_inventory_reroutes_to_coder(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Skill-Audit: Inventar und Frontmatter-Validierung",
            body=(
                "Enumerate all active SKILL.md under ~/.hermes/skills "
                "and validate frontmatter."
            ),
            assignee="research",
            max_iterations=12,
        )
        task = kb.get_task(conn, tid)
        assert task.assignee == "coder"
        assert task.kind == "analysis"
        payload = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
            (tid,),
        ).fetchone()["payload"]
        data = json.loads(payload)
        assert "inventory_lane_contract" in data


def test_review_prose_bait_uses_second_auto_retry_not_operator(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """End-to-end: REQUEST_CHANGES with regex bait after one auto_retry still
    gets attempt 2 — does not settle as operator_question."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    bait = "NEEDS_REVISION: fix token path; which delete/migration is safe?"
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review bait", assignee="coder", body="v1")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        with kb.write_txn(conn):
            kb._append_event(
                conn, t, "claimed",
                {"source_status": "review", "run_id": run_id},
                run_id=run_id,
            )
        assert kb.block_task(
            conn, t, reason=bait, expected_run_id=run_id,
        )
        assert kb.get_task(conn, t).block_kind == "review_revision"
        # First auto-retry
        retried = kb.auto_retry_blocked_tasks(
            conn, backoff_seconds=0, retry_limit=2
        )
        assert retried == [(t, 1)]
        assert kb.get_task(conn, t).status == "ready"
        # Second review block with changed body (coder attempted a fix)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("v2-fix", t))
        kb.claim_task(conn, t)
        run_id2 = kb.get_task(conn, t).current_run_id
        with kb.write_txn(conn):
            kb._append_event(
                conn, t, "claimed",
                {"source_status": "review", "run_id": run_id2},
                run_id=run_id2,
            )
        assert kb.block_task(
            conn, t, reason=bait + " still", expected_run_id=run_id2,
        )
        # Must still be retryable — not operator_question
        kind = kb._blocked_kind_for_auto_retry(
            bait + " still",
            explicit_block_kind=kb.get_task(conn, t).block_kind,
            verdict="REQUEST_CHANGES",
            auto_retry_count=1,
            body_hash=kb._task_body_hash("v2-fix"),
            last_auto_retry_body_hash=kb._latest_auto_retry_body_hash(conn, t),
        )
        assert kind == "retryable"
        retried2 = kb.auto_retry_blocked_tasks(
            conn, backoff_seconds=0, retry_limit=2
        )
        assert retried2 == [(t, 2)]
        assert kb.get_task(conn, t).status == "ready"
        assert _operator_escalations(conn, t) == []


def test_resolve_settled_block_autonomy_route_unit():
    assert (
        kb._resolve_settled_block_autonomy_route(
            block_kind="review_revision",
            blocked_kind="retryable",
            trigger_outcome="blocked",
            reason="NEEDS_REVISION: fix X",
            verdict="REQUEST_CHANGES",
            reready_count=0,
            reready_total=0,
        )
        == "reready_review"
    )
    # Episode spent
    assert (
        kb._resolve_settled_block_autonomy_route(
            block_kind="review_revision",
            blocked_kind="retryable",
            trigger_outcome="blocked",
            reason="NEEDS_REVISION: fix X",
            verdict="REQUEST_CHANGES",
            reready_count=1,
            reready_total=1,
        )
        == "escalate"
    )
    # New episode but lifetime spent → still escalate (token hard stop)
    assert (
        kb._resolve_settled_block_autonomy_route(
            block_kind="review_revision",
            blocked_kind="retryable",
            trigger_outcome="blocked",
            reason="NEEDS_REVISION: fix X",
            verdict="REQUEST_CHANGES",
            reready_count=0,
            reready_total=1,
        )
        == "escalate"
    )
    assert (
        kb._resolve_settled_block_autonomy_route(
            block_kind="capacity",
            blocked_kind="capacity",
            trigger_outcome="blocked",
            reason="input-token cap exceeded",
            verdict=None,
            reready_count=0,
        )
        == "hold_capacity"
    )
    # Bare gave_up is NOT capacity-hold (circuit breaker must page).
    assert (
        kb._resolve_settled_block_autonomy_route(
            block_kind=None,
            blocked_kind="retryable",
            trigger_outcome="gave_up",
            reason="consecutive failures",
            verdict=None,
            reready_count=0,
        )
        == "escalate"
    )
    assert (
        kb._resolve_settled_block_autonomy_route(
            block_kind="needs_input",
            blocked_kind="needs_input",
            trigger_outcome="blocked",
            reason="which credential?",
            verdict=None,
            reready_count=0,
        )
        == "escalate"
    )


def test_dispatch_max_spawn_counts_existing_running_tasks(
    kanban_home, all_assignees_spawnable
):
    """max_spawn is a live concurrency cap, not a per-tick spawn cap.

    Without counting tasks already in ``running``, every dispatcher tick can
    launch up to ``max_spawn`` more workers while previous workers are still
    alive. Long-running boards then accumulate unbounded worker subprocesses.
    """
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        running_a = kb.create_task(conn, title="running-a", assignee="alice")
        running_b = kb.create_task(conn, title="running-b", assignee="bob")
        ready = kb.create_task(conn, title="ready", assignee="carol")
        kb.claim_task(conn, running_a)
        kb.claim_task(conn, running_b)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

        assert res.spawned == []
        assert spawns == []
        assert kb.get_task(conn, ready).status == "ready"


def test_dispatch_max_spawn_fills_remaining_capacity(
    kanban_home, all_assignees_spawnable
):
    """When below cap, dispatch only fills available worker slots."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        ready_a = kb.create_task(conn, title="ready-a", assignee="bob")
        ready_b = kb.create_task(conn, title="ready-b", assignee="carol")
        kb.claim_task(conn, running)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

        assert len(res.spawned) == 1
        assert spawns == [ready_a]
        assert kb.get_task(conn, ready_a).status == "running"
        assert kb.get_task(conn, ready_b).status == "ready"


def test_dispatch_dry_run_max_spawn_counts_would_be_spawns(
    kanban_home, all_assignees_spawnable
):
    """Dry-run dispatch must stop after the max_spawn would-be spawns."""
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alice")
        second = kb.create_task(conn, title="second", assignee="bob")
        third = kb.create_task(conn, title="third", assignee="carol")

        res = kb.dispatch_once(conn, dry_run=True, max_spawn=1)

        assert res.spawned == [(first, "alice", "")]
        assert kb.get_task(conn, first).status == "ready"
        assert kb.get_task(conn, second).status == "ready"
        assert kb.get_task(conn, third).status == "ready"


def test_dispatch_max_in_progress_with_max_spawn_fills_remaining_capacity(
    kanban_home, all_assignees_spawnable
):
    """max_in_progress and max_spawn combine as live concurrency caps."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        ready_a = kb.create_task(conn, title="ready-a", assignee="bob")
        ready_b = kb.create_task(conn, title="ready-b", assignee="carol")
        ready_c = kb.create_task(conn, title="ready-c", assignee="alice")
        kb.claim_task(conn, running)

        res = kb.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            max_in_progress=3,
            max_spawn=10,
        )

        assert len(res.spawned) == 2
        assert spawns == [ready_a, ready_b]
        assert kb.get_task(conn, ready_a).status == "running"
        assert kb.get_task(conn, ready_b).status == "running"
        assert kb.get_task(conn, ready_c).status == "ready"


def test_dispatch_reclaims_stale_before_spawning(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="alice")
        kb.claim_task(conn, t)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 1, t),
        )
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.reclaimed == 1


# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------

def test_respawn_guard_none_on_fresh_task(kanban_home):
    """A fresh task with no failures or runs is not guarded."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="fresh", assignee="alice")
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_blocker_auth_on_quota_error(kanban_home):
    """'quota' in last_failure_error triggers blocker_auth."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="quota-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("API quota exceeded: rate limit hit", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_auth_error(kanban_home):
    """'unauthorized' in last_failure_error triggers blocker_auth."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="auth-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("403 Forbidden: unauthorized to access resource", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "recent_success"


def test_respawn_guard_newer_timeout_supersedes_recent_success(kanban_home):
    """A newer failed attempt must clear the prior success guard.

    Regression evidence from the live board: a task had earlier completed runs,
    then a newer timeout requeued it to ready; dispatch still emitted
    respawn_guarded/recent_success and left the task idle.
    """
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="timeout-after-success", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 600, now - 540),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'failed', 'timed_out', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_rejected_verdict_allows_fix_run(kanban_home):
    """K3 regression: a verifier REQUEST_CHANGES on the latest run invalidates
    recent_success — the review happened and DEMANDED a fix run. Without this
    the CommandHome inline-resolve (unblock + tick) silently stalls for the
    full success window. An APPROVED verdict keeps the guard."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="rejected-task", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'review', 'completed', ?, ?)",
            (t, now - 240, now - 180),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, verdict, started_at, ended_at) "
            "VALUES (?, 'verifier', 'done', 'completed', 'REQUEST_CHANGES', ?, ?)",
            (t, now - 120, now - 60),
        )
        assert kb.check_respawn_guard(conn, t) is None

        # Control: APPROVED on the latest run keeps recent_success.
        t2 = kb.create_task(conn, title="approved-task", assignee="alice")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'review', 'completed', ?, ?)",
            (t2, now - 240, now - 180),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, verdict, started_at, ended_at) "
            "VALUES (?, 'verifier', 'done', 'completed', 'APPROVED', ?, ?)",
            (t2, now - 120, now - 60),
        )
        assert kb.check_respawn_guard(conn, t2) == "recent_success"


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A GitHub PR URL in a recent comment triggers active_pr."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """A GitHub PR URL in a comment older than the PR window does not block."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="old-pr", assignee="alice")
        old_ts = int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 60
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', "
            "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
            (t, old_ts),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with an active PR comment."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kb.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


def test_silent_block_sweep_classifies_born_blocked_as_operator_intent(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="Operator-created parked task",
            assignee="worker",
            initial_status="blocked",
        )

        summary = kb.escalate_silent_blocks_sweep(
            conn,
            now=base,
        )
        escalation = _escalation_event(conn, task_id)
        classification = _heiler_events(conn, task_id)[0]

    assert summary["escalated"] == [
        {"task_id": task_id, "blocked_kind": "born_blocked"}
    ]
    assert escalation.payload["evidence"]["last_error"] == (
        "born blocked (initial_status=blocked)"
    )
    assert escalation.payload["evidence"]["blocked_kind"] == "born_blocked"
    assert classification.payload["class"] == kb.HEILER_CLASS_OPERATOR_INTENT
    assert classification.payload["evidence"]["matched"] == "born_blocked"
    assert classification.payload["evidence"]["signal_source"] == "blocked_kind"


def test_archived_dependency_sweep_skips_child_held_by_scheduled_operator_root(
    kanban_home, all_assignees_spawnable
):
    """A legacy archived link must not page for a chain the operator still holds."""
    with kb.connect_closing() as conn:
        root_id = kb.create_task(
            conn,
            title="operator-held root",
            triage=True,
            freigabe="operator",
        )
        child_id = kb.create_task(conn, title="held build child", assignee="coder")
        kb.link_tasks(conn, child_id, root_id)

        archived_parent_id = kb.create_task(conn, title="legacy archived parent")
        assert kb.archive_task(conn, archived_parent_id) is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id IN (?, ?)",
                (root_id, child_id),
            )
            # Simulate a pre-unlink legacy row retained after its parent archived.
            conn.execute(
                "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
                (archived_parent_id, child_id),
            )

        held_child = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        assert held_child is not None
        assert kb._is_operator_held(conn, held_child) is True

        summary = kb.escalate_silent_blocks_sweep(conn, now=1_800_000_000)
        events = kb.list_events(conn, child_id)

    assert summary["archived_dependency_escalated"] == []
    assert "operator_escalation" not in [event.kind for event in events]

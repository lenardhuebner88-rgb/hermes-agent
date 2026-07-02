"""TDD tests for FRD Phase 1a — disposition insert on task completion.

Covers:
  1. _record_disposition_items: 2 valid items → 2 ledger rows (status=open).
  2. Empty-list / missing-key → 0 rows, no error (no-op).
  3. Dedup on retry: same item twice → only 1 row inserted.
  4. Best-effort: insert raises → _record_disposition_items swallows, does not re-raise.
  5. Integration: complete_task with disposition-metadata → items in ledger + task done.
  6. Integration-empty: complete_task without disposition → task done, 0 rows, no error.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path, monkeypatch):
    """Fresh kanban DB in a temp HERMES_HOME; yields an open connection."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kb.connect(db_path=db_path)
    yield conn
    conn.close()


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME used for complete_task integration tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture()
def review_gate_on(monkeypatch):
    """Enable the code-task review gate with an available verifier profile."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder", "premium"}),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    return True


# ---------------------------------------------------------------------------
# Sample metadata blobs
# ---------------------------------------------------------------------------

_TWO_ITEMS_METADATA = {
    "residual_risk": "none",
    "disposition": {
        "items": [
            {
                "typ": "risk",
                "disposition": "delegate",
                "next_action": "ping security team",
                "severity": "real-risk",
                "evidence": "src/auth.py:42",
            },
            {
                "typ": "follow_up",
                "disposition": "defer",
                "next_action": "add regression test in sprint 3",
                "severity": "none",
                "evidence": "tests/test_auth.py",
            },
        ]
    },
}

_EMPTY_ITEMS_METADATA = {
    "residual_risk": "none",
    "disposition": {"items": []},
}

_NO_DISPOSITION_METADATA = {
    "residual_risk": "none",
    "changed_files": ["src/foo.py"],
}


# ===========================================================================
# 1. _record_disposition_items: 2 valid items → 2 rows (status=open, fields ok)
# ===========================================================================


def test_record_two_items_creates_two_open_rows(db_conn):
    task_id = "t_rec001"
    kb._record_disposition_items(db_conn, task_id, _TWO_ITEMS_METADATA)

    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert len(rows) == 2
    # All must be status=open
    assert all(r["status"] == "open" for r in rows)
    # source_task_id must match
    assert all(r["source_task_id"] == task_id for r in rows)
    # Verify field round-trip for the risk item
    risk_rows = [r for r in rows if r["typ"] == "risk"]
    assert len(risk_rows) == 1
    r = risk_rows[0]
    assert r["disposition"] == "delegate"
    assert r["next_action"] == "ping security team"
    assert r["severity"] == "real-risk"
    assert r["evidence"] == "src/auth.py:42"
    # And the follow_up item
    fu_rows = [r for r in rows if r["typ"] == "follow_up"]
    assert len(fu_rows) == 1
    fu = fu_rows[0]
    assert fu["disposition"] == "defer"
    assert fu["next_action"] == "add regression test in sprint 3"
    assert fu["severity"] == "none"


# ===========================================================================
# 2. Empty-list → 0 rows, no error
# ===========================================================================


def test_record_empty_items_list_is_noop(db_conn):
    task_id = "t_empty"
    kb._record_disposition_items(db_conn, task_id, _EMPTY_ITEMS_METADATA)
    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert rows == []


def test_record_no_disposition_key_is_noop(db_conn):
    task_id = "t_nokey"
    kb._record_disposition_items(db_conn, task_id, _NO_DISPOSITION_METADATA)
    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert rows == []


def test_record_none_metadata_is_noop(db_conn):
    task_id = "t_none"
    kb._record_disposition_items(db_conn, task_id, None)
    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert rows == []


# ===========================================================================
# 3. Dedup: calling twice with the same items → only 1 row per item
# ===========================================================================


def test_dedup_second_call_does_not_duplicate(db_conn):
    task_id = "t_dedup"
    kb._record_disposition_items(db_conn, task_id, _TWO_ITEMS_METADATA)
    kb._record_disposition_items(db_conn, task_id, _TWO_ITEMS_METADATA)  # retry
    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert len(rows) == 2  # still 2, not 4


# ===========================================================================
# 4. Best-effort: insert raises → function swallows, does NOT re-raise
# ===========================================================================


def test_best_effort_swallows_insert_error(db_conn, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "insert_disposition_item", _explode)
    task_id = "t_err"
    # Must not raise — even when insert is broken
    kb._record_disposition_items(db_conn, task_id, _TWO_ITEMS_METADATA)


# ===========================================================================
# 5. Integration: complete_task with disposition → items in ledger + task done
# ===========================================================================


def test_complete_task_with_disposition_inserts_items(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="integration test task", assignee="coder")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            result="done",
            summary="all good",
            metadata=_TWO_ITEMS_METADATA,
        )
    assert ok is True

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        rows = kb.list_disposition_items(conn, source_task_id=tid)

    assert len(rows) == 2
    assert all(r["status"] == "open" for r in rows)
    typs = {r["typ"] for r in rows}
    assert typs == {"risk", "follow_up"}


# ===========================================================================
# 6. Integration-empty: complete_task without disposition → done, 0 rows, no error
# ===========================================================================


def test_complete_task_without_disposition_is_backward_compat(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy task", assignee="coder")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            result="done",
            summary="finished",
            metadata=_NO_DISPOSITION_METADATA,
        )
    assert ok is True

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        rows = kb.list_disposition_items(conn, source_task_id=tid)

    assert rows == []


def test_review_gate_verified_done_records_coder_disposition(
    kanban_home, review_gate_on
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review-gated task", assignee="coder")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn,
            tid,
            result="implementation done",
            summary="impl ready for verifier",
            metadata=_TWO_ITEMS_METADATA,
            review_gate=True,
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "review"
        # The ledger is filled when the review-gated task reaches verified done,
        # not when the implementer merely parks it in review.
        assert kb.list_disposition_items(conn, source_task_id=tid) == []

        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None and claimed.status == "running"
        ok = kb.complete_task(
            conn,
            tid,
            result="APPROVED",
            summary="verifier approved",
            metadata=_NO_DISPOSITION_METADATA,
            review_gate=True,
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
        rows = kb.list_disposition_items(conn, source_task_id=tid)

    assert len(rows) == 2
    assert {r["typ"] for r in rows} == {"risk", "follow_up"}


def test_intermediate_review_stage_records_disposition(
    kanban_home, review_gate_on
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="critical review-gated task",
            assignee="coder",
            review_tier="critical",
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            result="implementation done",
            summary="impl ready for staged review",
            metadata=_NO_DISPOSITION_METADATA,
            review_gate=True,
        ) is True
        assert kb.get_task(conn, tid).status == "review"

        claimed = kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        assert claimed is not None and claimed.status == "running"
        assert kb.complete_task(
            conn,
            tid,
            result="APPROVED",
            summary="verifier approved with follow-up",
            metadata=_TWO_ITEMS_METADATA,
            review_gate=True,
        ) is True

        task = kb.get_task(conn, tid)
        review_target = kb._review_chain_target(conn, tid, kb._review_gate_config())
        rows = kb.list_disposition_items(conn, source_task_id=tid)

    assert task.status == "review"
    assert review_target == "reviewer"
    assert len(rows) == 2
    assert {r["typ"] for r in rows} == {"risk", "follow_up"}


def test_workflow_step_completion_records_disposition(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="workflow task", assignee="planner")
        kb.claim_task(conn, tid)
        current_run_id = kb.get_task(conn, tid).current_run_id

        ok = kb._advance_workflow_step(
            conn,
            tid,
            next_step_key="review",
            next_assignee="reviewer",
            result="planner step complete",
            summary="handoff to reviewer",
            metadata=_TWO_ITEMS_METADATA,
            verified_cards=[],
            expected_run_id=current_run_id,
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.current_step_key == "review"
        rows = kb.list_disposition_items(conn, source_task_id=tid)

        # A retried/advisory replay of the same metadata must stay idempotent.
        kb._record_disposition_items(conn, tid, _TWO_ITEMS_METADATA)
        assert len(kb.list_disposition_items(conn, source_task_id=tid)) == 2

    assert len(rows) == 2
    assert {r["typ"] for r in rows} == {"risk", "follow_up"}


# ===========================================================================
# 7. Auto-triage: worker-done/drop landet terminal, nie in der Operator-Queue
# ===========================================================================

_AUTO_TRIAGE_METADATA = {
    "residual_risk": "low",
    "disposition": {
        "items": [
            {
                "typ": "risk",
                "disposition": "delegate",
                "next_action": "ping security team",
                "severity": "real-risk",
                "evidence": "src/auth.py:42",
            },
            {
                "typ": "still_open",
                "disposition": "drop",
                "next_action": "",
                "severity": "none",
                "evidence": "obsolete after refactor",
            },
            {
                "typ": "follow_up",
                "disposition": "done",
                "next_action": "already covered by test",
                "severity": "none",
                "evidence": "tests/test_x.py",
            },
            {
                "typ": "risk",
                "disposition": "defer",
                "next_action": "note width assumption",
                "severity": "scope-note",
                "evidence": "ui only",
            },
        ]
    },
}


def test_record_auto_triages_worker_done_and_drop(db_conn):
    task_id = "t_auto001"
    kb._record_disposition_items(db_conn, task_id, _AUTO_TRIAGE_METADATA)

    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert len(rows) == 4

    # delegate/real-risk UND defer/scope-note bleiben offen (Operator-Queue
    # bzw. Strategist-Harvest); nur done/drop sind auto-terminal.
    open_rows = [r for r in rows if r["status"] == "open"]
    assert sorted(r["disposition"] for r in open_rows) == ["defer", "delegate"]

    auto_rows = [r for r in rows if r["status"] == "accepted"]
    assert sorted(r["disposition"] for r in auto_rows) == ["done", "drop"]
    assert all(r["decided_by"] == "auto-triage" for r in auto_rows)
    assert all(r["decided_at"] is not None for r in auto_rows)


def test_record_auto_triage_dedup_still_holds(db_conn):
    """Retry completion must not resurrect auto-triaged items as duplicates."""
    task_id = "t_auto002"
    kb._record_disposition_items(db_conn, task_id, _AUTO_TRIAGE_METADATA)
    kb._record_disposition_items(db_conn, task_id, _AUTO_TRIAGE_METADATA)

    rows = kb.list_disposition_items(db_conn, source_task_id=task_id)
    assert len(rows) == 4

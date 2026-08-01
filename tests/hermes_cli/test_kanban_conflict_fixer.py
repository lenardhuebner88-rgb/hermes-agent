"""Regression coverage for bounded conflict-park fixer worker instructions."""

from __future__ import annotations

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb


@pytest.fixture()
def review_gate_on(monkeypatch):
    """Pin the prod-shaped review gate: enabled, ``premium`` is a code role.

    Deterministic by construction — no read of the live root ``config.yaml``
    (same monkeypatch pattern as ``test_kanban_db_escalation.py``), so the test
    cannot drift when the operator retunes the board policy.
    """
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder", "premium"}),
            "acceptance_roles": frozenset(),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
            "auto_scout_on_critical": False,
            "scout_max_runtime_seconds": None,
            "standard_uses_llm_verifier": True,
            "judge_at_chain_tip": False,
            "critical_reviews_each_slice": True,
            "max_review_rounds": 3,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    return True


def test_conflict_fixer_brief_instructs_terminal_kanban_actions_and_keeps_cage():
    body = kb._conflict_fixer_body(
        parent_id="t_parent",
        parent_title="parked finalizer",
        root_id="t_root",
        branch="kanban/t_root",
        reason="integration parked: merge conflict",
        attempt=1,
    )

    assert "call kanban_complete" in body
    assert "call kanban_block" in body
    assert "NEVER push, merge, switch, or reset another branch" in body
    assert "operator" in body


def test_completed_conflict_fixer_resumes_its_parent_without_retry_sweep(kanban_home):
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason)
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": parent_id,
                    "conflict_fingerprint": kb._conflict_fingerprint(reason),
                },
            )

        assert kb.complete_task(conn, child_id, summary="conflict fixed")
        parent = kb.get_task(conn, parent_id)
        resume_events = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert parent is not None
    assert parent.status == "ready"
    assert [event.payload for event in resume_events] == [
        {
            "child_id": child_id,
            "conflict_fingerprint": kb._conflict_fingerprint(reason),
            "status": "ready",
        }
    ]


def test_review_gated_conflict_fixer_resumes_parent_only_after_approval(
    kanban_home, review_gate_on
):
    """Prod path: gate on + ``premium`` code role → resume on the SECOND complete.

    In production ``kanban.review_gate.enabled`` is true and the fixer assignee
    (``premium``) is a code role, so a fixer completion first parks in ``review``.
    The parked parent must stay blocked until the verifier APPROVES; only the
    second ``complete_task`` (the run that originated from review) may emit
    ``conflict_fixer_parent_resumed``.
    """
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    fingerprint = kb._conflict_fingerprint(reason)
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason)
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {"parent_id": parent_id, "conflict_fingerprint": fingerprint},
            )
        assert kb.claim_task(conn, child_id) is not None

        # Phase 1 — worker completion is parked for review, NOT done.
        assert kb.complete_task(
            conn,
            child_id,
            result="conflict resolved",
            summary="conflict fixed",
            review_gate=True,
        ) is True
        assert kb.get_task(conn, child_id).status == "review"
        parked_parent = kb.get_task(conn, parent_id)
        assert parked_parent.status == "blocked"
        assert [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ] == []

        # Phase 2 — verifier claims the review and APPROVES.
        claimed = kb.claim_review_task(conn, child_id, reviewer_profile="verifier")
        assert claimed is not None and claimed.status == "running"
        assert kb.complete_task(
            conn,
            child_id,
            result="APPROVED",
            summary="verifier approved the fix",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        ) is True

        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        resume_events = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert child.status == "done"
    assert parent.status == "ready"
    assert parent.block_kind is None
    assert [event.payload for event in resume_events] == [
        {
            "child_id": child_id,
            "conflict_fingerprint": fingerprint,
            "status": "ready",
        }
    ]

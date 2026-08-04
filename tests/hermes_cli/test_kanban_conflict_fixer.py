"""Regression coverage for bounded conflict-park fixer worker instructions."""

from __future__ import annotations

from pathlib import Path

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


def _spawn_conflict_fixer(conn, tmp_path: Path) -> tuple[str, str]:
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
    assert kb.claim_task(conn, parent_id) is not None
    assert kb.block_task(conn, parent_id, reason=reason, kind="integration")
    worktree = tmp_path / "repo" / ".worktrees" / "kanban" / parent_id
    worktree.mkdir(parents=True)
    kb.set_workspace_path(conn, parent_id, str(worktree))
    parent = conn.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone()
    assert parent is not None
    child_id = kb._create_conflict_park_fixer_subtask(
        conn,
        parent,
        reason=reason,
        root_id=parent_id,
        wt=worktree,
        attempt=1,
    )
    assert child_id is not None
    return parent_id, child_id


def test_conflict_fixer_uses_configured_profile(kanban_home, tmp_path):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  conflict_fixer_profile: conflict-fixer\n",
        encoding="utf-8",
    )

    with kb.connect_closing() as conn:
        _, child_id = _spawn_conflict_fixer(conn, tmp_path)
        child = kb.get_task(conn, child_id)

    assert child is not None
    assert child.assignee == "conflict-fixer"


@pytest.mark.parametrize(
    "config_text",
    ["", "kanban:\n  conflict_fixer_profile: '  '\n"],
    ids=["missing", "empty"],
)
def test_conflict_fixer_defaults_to_premium(kanban_home, tmp_path, config_text):
    if config_text:
        (kanban_home / "config.yaml").write_text(config_text, encoding="utf-8")

    with kb.connect_closing() as conn:
        _, child_id = _spawn_conflict_fixer(conn, tmp_path)
        child = kb.get_task(conn, child_id)

    assert child is not None
    assert child.assignee == "premium"


def test_unlisted_conflict_fixer_role_still_requires_independent_review(
    kanban_home, tmp_path, monkeypatch, review_gate_on
):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  conflict_fixer_profile: conflict-fixer\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        kb,
        "_worker_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )

    with kb.connect_closing() as conn:
        parent_id, child_id = _spawn_conflict_fixer(conn, tmp_path)
        assert kb.claim_task(conn, child_id) is not None

        assert kb.complete_task(
            conn,
            child_id,
            result="conflict resolved",
            summary="conflict fixed",
            review_gate=True,
        ) is True

        child = kb.get_task(conn, child_id)
        parent = kb.get_task(conn, parent_id)
        resumed = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert child is not None and child.status == "review"
    assert parent is not None and parent.status == "blocked"
    assert resumed == []


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


def _park_with_fixer(
    conn,
    *,
    reason: str,
    attempt: int = 1,
    root_id: str | None = None,
    parent_id: str | None = None,
):
    """Park a finalizer and dispatch ONE fixer card the prod way.

    Mirrors :func:`kb._create_conflict_park_fixer_subtask`'s event pair
    (``conflict_fixer_dispatched`` on the parent, ``conflict_fixer_for`` on the
    child) without needing a real worktree.
    """
    fingerprint = kb._conflict_fingerprint(reason)
    if parent_id is None:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason, kind="integration")
    root_id = root_id or parent_id
    child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            kb.CONFLICT_FIXER_DISPATCHED_EVENT,
            {
                "child_id": child_id,
                "root_id": root_id,
                "attempt": attempt,
                "limit": kb.CONFLICT_FIXER_MAX_ATTEMPTS,
                "conflict_fingerprint": fingerprint,
            },
        )
        kb._append_event(
            conn,
            child_id,
            "conflict_fixer_for",
            {
                "parent_id": parent_id,
                "root_id": root_id,
                "attempt": attempt,
                "conflict_fingerprint": fingerprint,
            },
        )
    return parent_id, child_id, fingerprint


def _outcomes(conn, task_id):
    return [
        event.payload
        for event in kb.list_events(conn, task_id)
        if event.kind == kb.CONFLICT_FIXER_OUTCOME_EVENT
    ]


def test_resolved_fixer_stamps_outcome_on_card_and_park(kanban_home):
    """A resume is not just a status flip — both cards carry the verdict."""
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id, child_id, fingerprint = _park_with_fixer(conn, reason=reason)

        assert kb.complete_task(conn, child_id, summary="conflict fixed")

        child_outcomes = _outcomes(conn, child_id)
        parent_outcomes = _outcomes(conn, parent_id)
        parent = kb.get_task(conn, parent_id)

    assert parent.status == "ready"
    expected = {
        "outcome": "resolved",
        "reason_code": "parent_resumed",
        "child_id": child_id,
        "parent_id": parent_id,
        "conflict_fingerprint": fingerprint,
        "status": "ready",
    }
    assert child_outcomes == [expected]
    assert parent_outcomes == [expected]


def test_refused_resume_is_recorded_instead_of_failing_silently(kanban_home):
    """Park changed under the fixer → ``not_resumed`` with a machine reason.

    Before this event the board showed a *done* fixer beside a *blocked*
    parent with nothing linking them; the refusal reason was recoverable only
    by replaying the guard chain against the event log by hand.
    """
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    other = "integration parked: post-merge gate failed: ruff: exit 1"
    with kb.connect_closing() as conn:
        # The finalizer is parked on ``other``; the fixer owns ``reason``.
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=other, kind="integration")
        _, child_id, fingerprint = _park_with_fixer(
            conn, reason=reason, parent_id=parent_id
        )

        assert kb.complete_task(conn, child_id, summary="fixed something else")

        child_outcomes = _outcomes(conn, child_id)
        resumed = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert resumed == []
    assert len(child_outcomes) == 1
    assert child_outcomes[0]["outcome"] == "not_resumed"
    assert child_outcomes[0]["reason_code"] == "park_fingerprint_mismatch"
    assert child_outcomes[0]["conflict_fingerprint"] == fingerprint
    assert child_outcomes[0]["current_fingerprint"] == kb._conflict_fingerprint(other)


def test_same_episode_stall_escalation_does_not_disarm_the_fixer(kanban_home):
    """The sweep escalates the park it ALSO routed to a fixer — not a hold.

    Live shape (2026-08-04, t_635aded0/t_5b3f6ee7): the no-silent-stall sweep
    appended an ``operator_escalation`` with ``stall_class=integration_parked``
    a few rows AFTER its own ``conflict_fixer_dispatched``. That notification
    permanently disarmed the resume, so the fixer committed its fix, completed
    green, and the chain stayed parked.
    """
    reason = (
        "worker base preparation: clean stale worktree could not rebase onto "
        "main: git rebase main… failed"
    )
    with kb.connect_closing() as conn:
        parent_id, child_id, _ = _park_with_fixer(conn, reason=reason)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": parent_id, "status": "blocked"},
                    "why_now": f"no-silent-stall sweep detected integration_parked: {reason}",
                    "evidence": {"stall_class": "integration_parked", "attempts": 1},
                },
            )

        assert kb.complete_task(conn, child_id, summary="rebase resolved")

        parent = kb.get_task(conn, parent_id)
        outcomes = _outcomes(conn, child_id)

    assert parent.status == "ready"
    assert [o["reason_code"] for o in outcomes] == ["parent_resumed"]


def test_operator_escalation_raised_before_dispatch_still_vetoes(kanban_home):
    """An escalation the fixer inherited is a real hold — refuse, and say so."""
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason, kind="integration")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": parent_id, "status": "blocked"},
                    "why_now": "operator hold",
                    "evidence": {"stall_class": "integration_parked"},
                },
            )
        _, child_id, _ = _park_with_fixer(
            conn, reason=reason, parent_id=parent_id
        )

        assert kb.complete_task(conn, child_id, summary="fix committed")

        parent = kb.get_task(conn, parent_id)
        outcomes = _outcomes(conn, child_id)

    assert parent.status == "blocked"
    assert [o["reason_code"] for o in outcomes] == ["operator_escalation_active"]


def test_last_permitted_attempt_may_still_resume_its_parent(kanban_home):
    """Attempt 2 of 2 fixing the conflict must unpark — not read as exhausted."""
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id, first_child, _ = _park_with_fixer(conn, reason=reason, attempt=1)
        assert kb.block_task(conn, first_child, reason="fixer gave up")
        _, second_child, _ = _park_with_fixer(
            conn, reason=reason, attempt=2, parent_id=parent_id
        )
        assert (
            kb._matching_conflict_fixer_attempts(
                conn, root_id=parent_id, conflict_fingerprint=kb._conflict_fingerprint(reason)
            )
            == kb.CONFLICT_FIXER_MAX_ATTEMPTS
        )

        assert kb.complete_task(conn, second_child, summary="conflict fixed")

        parent = kb.get_task(conn, parent_id)
        outcomes = _outcomes(conn, second_child)

    assert parent.status == "ready"
    assert [o["reason_code"] for o in outcomes] == ["parent_resumed"]


def test_over_budget_attempt_is_reported_as_exhausted(kanban_home):
    """Beyond the budget the refusal stays — now with an auditable reason."""
    reason = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id, first_child, _ = _park_with_fixer(conn, reason=reason, attempt=1)
        assert kb.block_task(conn, first_child, reason="fixer gave up")
        _, second_child, _ = _park_with_fixer(
            conn, reason=reason, attempt=2, parent_id=parent_id
        )
        assert kb.block_task(conn, second_child, reason="fixer gave up")
        _, third_child, _ = _park_with_fixer(
            conn, reason=reason, attempt=3, parent_id=parent_id
        )

        assert kb.complete_task(conn, third_child, summary="claims a fix")

        parent = kb.get_task(conn, parent_id)
        outcomes = _outcomes(conn, third_child)

    assert parent.status == "blocked"
    assert outcomes[0]["reason_code"] == "attempts_exhausted"
    assert outcomes[0]["attempts"] == 3


def test_ordinary_completion_stamps_no_fixer_outcome(kanban_home):
    """The helper runs on every completion — non-fixer cards stay untouched."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ordinary card", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, summary="done")
        assert _outcomes(conn, task_id) == []

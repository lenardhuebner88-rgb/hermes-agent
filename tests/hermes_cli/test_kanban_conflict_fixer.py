"""Regression coverage for bounded conflict-park fixer worker instructions."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_chain_status as chain_status
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


def _spawn_marker(conn, parent_id: str, child_id: str, reason: str) -> None:
    """Record the ``conflict_fixer_for`` back-reference a dispatch would write."""
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


def _repark_integration(conn, parent_id: str, reason: str) -> None:
    """Re-park an already-blocked parent the way an integration sweep does.

    ``_system_park_set_blocked`` accepts ``status = 'blocked'``, so a parked
    chain task can collect a SECOND ``blocked`` event with a different reason
    text while its fixer is still running.
    """
    with kb.write_txn(conn):
        assert kb._system_park_set_blocked(
            conn,
            parent_id,
            kind="integration",
            where_sql="status IN ('running', 'ready', 'blocked')",
        ) == 1
        kb._append_event(
            conn,
            parent_id,
            "blocked",
            kb._system_blocked_event_payload(reason, "integration"),
        )


def test_completed_fixer_resumes_a_parent_reparked_under_a_new_reason(kanban_home):
    """CONFLICT-FIXER-REPARK-CAS: a re-park must not silently strand the chain.

    The parent is parked (reason A), a fixer is dispatched for A, and while it
    runs the parent is re-parked with a different integration reason (B). The
    pre-fix CAS compared A against the LATEST blocked event (B), mismatched,
    and returned ``False`` without any event — the chain stayed blocked until
    a timeout.
    """
    reason_a = "integration parked: merge conflict/failure (aborted): foo.py"
    reason_b = "integration parked: merge conflict/failure (aborted): bar.py"
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason_a, kind="integration")
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        _spawn_marker(conn, parent_id, child_id, reason_a)
        _repark_integration(conn, parent_id, reason_b)

        assert kb.complete_task(conn, child_id, summary="conflict fixed")
        parent = kb.get_task(conn, parent_id)
        resume_events = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert parent is not None
    assert parent.status == "ready"
    assert parent.block_kind is None
    assert [event.payload for event in resume_events] == [
        {
            "child_id": child_id,
            "conflict_fingerprint": kb._conflict_fingerprint(reason_a),
            "reparked_fingerprint": kb._conflict_fingerprint(reason_b),
            "status": "ready",
        }
    ]


def test_late_fixer_completion_does_not_resume_a_closed_episode(kanban_home):
    """Fail-closed boundary: resume → unrelated re-park → late fixer completion.

    Once the episode was closed after dispatch (here: an operator ``unblocked``),
    the park holding the parent now is a DIFFERENT one; the stale fixer must
    leave it alone.
    """
    reason_a = "integration parked: merge conflict/failure (aborted): foo.py"
    reason_b = "integration parked: merge conflict/failure (aborted): bar.py"
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason_a, kind="integration")
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        _spawn_marker(conn, parent_id, child_id, reason_a)
        assert kb.unblock_task(conn, parent_id)
        _repark_integration(conn, parent_id, reason_b)

        assert kb.complete_task(conn, child_id, summary="conflict fixed")
        parent = kb.get_task(conn, parent_id)
        resume_events = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "conflict_fixer_parent_resumed"
        ]

    assert parent is not None
    assert parent.status == "blocked"
    assert resume_events == []


def test_fixer_dispatched_for_a_foreign_park_never_resumes(kanban_home):
    """The dispatch-time park must carry the fixer's fingerprint, not just any park."""
    reason_a = "integration parked: merge conflict/failure (aborted): foo.py"
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked finalizer", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.block_task(conn, parent_id, reason=reason_a, kind="integration")
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": parent_id,
                    "conflict_fingerprint": kb._conflict_fingerprint(
                        "integration parked: a conflict this parent never had"
                    ),
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
    assert parent.status == "blocked"
    assert resume_events == []


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


# ---------------------------------------------------------------------------
# Resume-decline receipts: every non-no-op decline of the resume guard must
# leave exactly one conflict_fixer_resume_declined event with a
# machine-readable reason, attached to the CHILD card (the only task that
# exists on every decline path).
# ---------------------------------------------------------------------------

_PARK_REASON = "integration parked: merge conflict/failure (aborted): foo.py"


def _declined_events(conn, task_id: str):
    return [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == chain_status.CONFLICT_FIXER_RESUME_DECLINED_EVENT
    ]


def _blocked_integration_parent(conn, reason: str = _PARK_REASON) -> str:
    parent_id = kb.create_task(conn, title="parked parent", assignee="coder")
    assert kb.claim_task(conn, parent_id) is not None
    assert kb.block_task(conn, parent_id, reason=reason, kind="integration")
    return parent_id


def _fixer_child_with_marker(
    conn,
    parent_id: str,
    reason: str = _PARK_REASON,
    *,
    fingerprint: str | None = None,
) -> str:
    child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            child_id,
            "conflict_fixer_for",
            {
                "parent_id": parent_id,
                "conflict_fingerprint": (
                    fingerprint
                    if fingerprint is not None
                    else kb._conflict_fingerprint(reason)
                ),
            },
        )
    return child_id


def test_resume_declined_marker_invalid_when_marker_payload_unreadable(kanban_home):
    with kb.connect_closing() as conn:
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'conflict_fixer_for', '{not-json', ?)",
                (child_id, int(time.time())),
            )

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_MARKER_INVALID,
        }
    ]


def test_resume_declined_marker_invalid_when_required_field_empty(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked parent", assignee="coder")
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": parent_id,
                    "root_id": parent_id,
                    "conflict_fingerprint": "",
                },
            )

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_MARKER_INVALID,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_resume_declined_parent_missing(kanban_home):
    missing_parent_id = "t_missing_parent"
    with kb.connect_closing() as conn:
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": missing_parent_id,
                    "conflict_fingerprint": kb._conflict_fingerprint(_PARK_REASON),
                },
            )

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_PARENT_MISSING,
            "parent_id": missing_parent_id,
            "root_id": missing_parent_id,
        }
    ]


def test_resume_declined_parent_not_blocked(kanban_home):
    """An EXISTING parent with the wrong status is not ``parent_missing``."""
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked parent", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None  # running, not blocked
        child_id = _fixer_child_with_marker(conn, parent_id)

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_PARENT_NOT_BLOCKED,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_resume_declined_not_integration_park(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parked parent", assignee="coder")
        assert kb.claim_task(conn, parent_id) is not None
        reason = "waiting on upstream service"
        assert kb.block_task(conn, parent_id, reason=reason, kind="capacity")
        child_id = _fixer_child_with_marker(conn, parent_id, reason)

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_NOT_INTEGRATION_PARK,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_resume_declined_operator_escalation_active(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = _blocked_integration_parent(conn)
        child_id = _fixer_child_with_marker(conn, parent_id)
        with kb.write_txn(conn):
            kb._append_event(conn, parent_id, kb.OPERATOR_ESCALATION_EVENT, {})

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_OPERATOR_ESCALATION_ACTIVE,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_resume_declined_budget_exhausted(kanban_home):
    fingerprint = kb._conflict_fingerprint(_PARK_REASON)
    with kb.connect_closing() as conn:
        parent_id = _blocked_integration_parent(conn)
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": parent_id,
                    "root_id": parent_id,
                    "conflict_fingerprint": fingerprint,
                },
            )
            for attempt in range(1, kb.CONFLICT_FIXER_MAX_ATTEMPTS + 1):
                kb._append_event(
                    conn,
                    parent_id,
                    kb.CONFLICT_FIXER_DISPATCHED_EVENT,
                    {
                        "root_id": parent_id,
                        "child_id": f"t_prior_{attempt}",
                        "conflict_fingerprint": fingerprint,
                    },
                )

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_BUDGET_EXHAUSTED,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_resume_declined_park_ownership_lost(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = _blocked_integration_parent(conn)
        foreign_fingerprint = kb._conflict_fingerprint(
            "integration parked: a conflict this parent never had"
        )
        child_id = _fixer_child_with_marker(
            conn, parent_id, fingerprint=foreign_fingerprint
        )

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_PARK_OWNERSHIP_LOST,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_resume_declined_escalation_is_checked_before_budget(kanban_home):
    """Both declines apply at once — the chain order decides the written reason."""
    fingerprint = kb._conflict_fingerprint(_PARK_REASON)
    with kb.connect_closing() as conn:
        parent_id = _blocked_integration_parent(conn)
        child_id = kb.create_task(conn, title="conflict fixer", assignee="premium")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "conflict_fixer_for",
                {
                    "parent_id": parent_id,
                    "root_id": parent_id,
                    "conflict_fingerprint": fingerprint,
                },
            )
            kb._append_event(conn, parent_id, kb.OPERATOR_ESCALATION_EVENT, {})
            for attempt in range(1, kb.CONFLICT_FIXER_MAX_ATTEMPTS + 1):
                kb._append_event(
                    conn,
                    parent_id,
                    kb.CONFLICT_FIXER_DISPATCHED_EVENT,
                    {
                        "root_id": parent_id,
                        "child_id": f"t_prior_{attempt}",
                        "conflict_fingerprint": fingerprint,
                    },
                )

        assert kb._resume_parent_for_completed_conflict_fixer(conn, child_id) is False
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_OPERATOR_ESCALATION_ACTIVE,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


class _ResumeRacingConnection:
    """Simulate a concurrent winner of the resume CAS.

    Right before the guard's final UPDATE lands, flip the parent out of
    ``blocked`` so the UPDATE matches zero rows — deterministically the state
    a concurrent resume/unblock would leave behind.
    """

    def __init__(self, conn, parent_id: str) -> None:
        self._conn = conn
        self._parent_id = parent_id

    def execute(self, sql, parameters=()):
        if "UPDATE tasks SET status" in sql and "block_recurrences = 0" in sql:
            self._conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?",
                (self._parent_id,),
            )
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_resume_declined_update_race(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = _blocked_integration_parent(conn)
        child_id = _fixer_child_with_marker(conn, parent_id)

        assert (
            kb._resume_parent_for_completed_conflict_fixer(
                _ResumeRacingConnection(conn, parent_id), child_id
            )
            is False
        )
        events = _declined_events(conn, child_id)

    assert [event.payload for event in events] == [
        {
            "child_id": child_id,
            "reason": chain_status.RESUME_DECLINED_UPDATE_RACE,
            "parent_id": parent_id,
            "root_id": parent_id,
        }
    ]


def test_plain_completion_without_fixer_marker_writes_no_declined_event(kanban_home):
    """``marker is None`` is the expected no-op for every non-fixer completion."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="regular task", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None

        assert kb.complete_task(conn, task_id, summary="done")
        declined_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = ?",
            (chain_status.CONFLICT_FIXER_RESUME_DECLINED_EVENT,),
        ).fetchone()[0]

    assert declined_count == 0

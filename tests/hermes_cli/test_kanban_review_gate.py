"""Phase 2 review gate: independent verification before 'done'.

Covers the producer side (``complete_task(review_gate=...)`` →
``_submit_for_review``) and the dependency-gating contract:

* code-bearing worker completions park in ``review`` (not ``done``);
* the gate is opt-in (``review_gate`` defaults False) and config-gated
  (disabled / no verifier profile → direct ``done``, no stall);
* non-code assignees are never gated;
* the verifier's OWN completion (run originated from review) is terminal
  ``done`` — never re-parked (anti-loop);
* children gate on the parent's *verified* ``done``, not on ``review``;
* a REQUEST_CHANGES (``block_task``) leaves the task ``blocked`` and keeps
  children gated;
* the scratch workspace is preserved across the review hop (the verifier
  needs it) and only cleaned on terminal ``done``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def gate_on(monkeypatch):
    """Enable the review gate with coder/premium roles + an existing verifier."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder", "premium"}),
            "verifier_profile": "verifier",
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    return True


# ---------------------------------------------------------------------------
# Producer routing
# ---------------------------------------------------------------------------

def test_code_completion_with_gate_routes_to_review(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl X", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="impl done", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"
        ev = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review'",
            (tid,),
        ).fetchone()
        assert ev is not None


def test_premium_is_code_bearing(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="premium")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="x", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"


def test_default_review_gate_false_goes_done(kanban_home, gate_on):
    """Non-worker callers (default review_gate=False) keep the direct done path."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done")  # no review_gate
        assert kb.get_task(conn, tid).status == "done"


def test_non_code_assignee_not_gated(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="research", assignee="research")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"


def test_gate_disabled_by_default_goes_done(kanban_home):
    """No config + no verifier profile in the isolated home → gate inert."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"


def test_gate_inert_when_verifier_profile_missing(kanban_home, monkeypatch):
    """Enabled gate but missing verifier profile must NOT strand the task."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="x", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# Anti-loop: the verifier's own completion is terminal
# ---------------------------------------------------------------------------

def test_run_originated_from_review_discriminates(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        coder_run = kb.get_task(conn, tid).current_run_id
        assert kb._run_originated_from_review(conn, tid, coder_run) is False
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        claimed = kb.claim_review_task(conn, tid)
        assert kb._run_originated_from_review(
            conn, tid, claimed.current_run_id
        ) is True


def test_verifier_completion_goes_done(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl done", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"
        # Verifier claims the review task and approves via the same worker path.
        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None and claimed.status == "running"
        assert kb.complete_task(
            conn, tid, summary="APPROVED — tests pass", review_gate=True
        )
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# Dependency gating
# ---------------------------------------------------------------------------

def test_children_wait_for_verified_done(kanban_home, gate_on):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="coder")
        child = kb.create_task(
            conn, title="child", parents=[parent], assignee="coder"
        )
        assert kb.get_task(conn, child).status == "todo"
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="impl", review_gate=True)
        # Parent parked in review → child must NOT be promoted.
        assert kb.get_task(conn, parent).status == "review"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"
        # Verifier approves → parent done → child unblocks.
        kb.claim_review_task(conn, parent)
        kb.complete_task(conn, parent, summary="APPROVED", review_gate=True)
        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, child).status == "ready"


def test_reject_blocks_and_keeps_children_gated(kanban_home, gate_on):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="coder")
        child = kb.create_task(
            conn, title="child", parents=[parent], assignee="coder"
        )
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="impl", review_gate=True)
        claimed = kb.claim_review_task(conn, parent)
        assert claimed is not None
        # REQUEST_CHANGES → block.
        kb.block_task(conn, parent, reason="REQUEST_CHANGES: tests fail")
        assert kb.get_task(conn, parent).status == "blocked"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


# ---------------------------------------------------------------------------
# Workspace preservation
# ---------------------------------------------------------------------------

def test_review_does_not_cleanup_workspace(kanban_home, gate_on, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kb, "_cleanup_workspace", lambda conn, tid: calls.append(tid)
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
    assert calls == []  # preserved for the verifier


def test_done_cleans_up_workspace(kanban_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kb, "_cleanup_workspace", lambda conn, tid: calls.append(tid)
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="done")  # gate-off → terminal
    assert calls == [tid]

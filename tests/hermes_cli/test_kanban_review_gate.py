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
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    return True


# ---------------------------------------------------------------------------
# B-T5: effective review tier (explicit column wins; NULL → auto only if opt-in)
# ---------------------------------------------------------------------------

def test_effective_review_tier_explicit_wins_both_ways(kanban_home, monkeypatch):
    """Explicit review_tier is authoritative up AND down; NULL → auto (auto_tier ON)."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        # explicit UPGRADES a trivial task
        t1 = kb.create_task(conn, title="trivial", assignee="coder", review_tier="critical")
        assert kb._effective_review_tier(conn, t1) == "critical"
        # explicit DOWNGRADES an auto-critical task (operator override, both ways)
        t2 = kb.create_task(conn, title="db change",
                            body="run a database migration and deploy",
                            assignee="coder", review_tier="standard")
        assert kb._effective_review_tier(conn, t2) == "standard"
        # NULL + auto_tier ON → auto decides (critical marker in body)
        t3 = kb.create_task(conn, title="db change",
                            body="run a database migration", assignee="coder")
        assert kb._effective_review_tier(conn, t3) == "critical"
        # NULL, no markers → standard
        t4 = kb.create_task(conn, title="tweak copy", body="reword a label", assignee="coder")
        assert kb._effective_review_tier(conn, t4) == "standard"


def test_effective_review_tier_auto_off_is_byte_identical(kanban_home, monkeypatch):
    """auto_tier OFF (default): a NULL-column risky task stays standard = today."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": False},
    )
    with kb.connect() as conn:
        t = kb.create_task(conn, title="db change",
                           body="run a database migration and deploy", assignee="coder")
        assert kb._effective_review_tier(conn, t) == "standard"   # auto OFF → no chain
        # explicit column still wins even with auto OFF
        t2 = kb.create_task(conn, title="x", assignee="coder", review_tier="critical")
        assert kb._effective_review_tier(conn, t2) == "critical"


# ---------------------------------------------------------------------------
# C-T1: operator setter set_task_review_tier (mirror of set_task_model_override)
# ---------------------------------------------------------------------------

def test_set_task_review_tier_roundtrip(kanban_home):
    """Setter mirrors set_task_model_override: set/clear, normalise, validate, event."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="tier setter", assignee="coder")
        # set → column authoritative, effective tier follows
        assert kb.set_task_review_tier(conn, tid, "critical") is True
        assert kb.get_task(conn, tid).review_tier == "critical"
        assert kb._effective_review_tier(conn, tid) == "critical"
        # normalises case + whitespace to the canonical lowercase token
        assert kb.set_task_review_tier(conn, tid, "  Review  ") is True
        assert kb.get_task(conn, tid).review_tier == "review"
        # None clears → NULL (auto-risk decides again)
        assert kb.set_task_review_tier(conn, tid, None) is True
        assert kb.get_task(conn, tid).review_tier is None
        # empty string also clears
        assert kb.set_task_review_tier(conn, tid, "critical") is True
        assert kb.set_task_review_tier(conn, tid, "") is True
        assert kb.get_task(conn, tid).review_tier is None
        # invalid non-empty tier raises — never silently stores garbage
        with pytest.raises(ValueError):
            kb.set_task_review_tier(conn, tid, "bogus")
        # a real set stamps a review_tier_set event
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? AND kind='review_tier_set'",
                (tid,),
            ).fetchall()
        ]
        assert kinds, "expected at least one review_tier_set event"
        # missing task → False, no raise
        assert kb.set_task_review_tier(conn, "t_doesnotexist", "review") is False


# ---------------------------------------------------------------------------
# B-T6: ordered stage list per tier (missing profiles degrade gracefully)
# ---------------------------------------------------------------------------

def test_review_stages_for_tier(monkeypatch):
    cfg = {"verifier_profile": "verifier", "review_profile": "reviewer", "critic_profile": "critic"}
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    assert kb._review_stages_for_tier("standard", cfg) == ["verifier"]
    assert kb._review_stages_for_tier("review", cfg) == ["verifier", "reviewer"]
    assert kb._review_stages_for_tier("critical", cfg) == ["verifier", "reviewer", "critic"]
    # missing critic profile → critical degrades, never strands the task
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: name != "critic")
    assert kb._review_stages_for_tier("critical", cfg) == ["verifier", "reviewer"]
    # unknown tier → single verifier stage (today's behavior)
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    assert kb._review_stages_for_tier("bogus", cfg) == ["verifier"]


# ---------------------------------------------------------------------------
# B-T7: submit stamps the frozen tier + stage 0 + target profile into the event
# ---------------------------------------------------------------------------

def test_submit_for_review_stamps_stage_zero(kanban_home, gate_on):
    import json
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="hard db work",
                             body="database migration", assignee="coder",
                             review_tier="critical")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="impl", review_gate=True)
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'submitted_for_review' ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        p = json.loads(ev["payload"])
        assert p["review_stage"] == 0
        assert p["review_tier"] == "critical"
        assert p["target_profile"] == "verifier"


# ---------------------------------------------------------------------------
# B-T8: dispatch reads the stage profile from the event (not fixed verifier)
# ---------------------------------------------------------------------------

def test_review_chain_target_reads_event(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", body="database migration",
                             assignee="coder", review_tier="critical")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)  # stage 0 → verifier
        cfg = kb._review_gate_config()
        assert kb._review_chain_target(conn, tid, cfg) == "verifier"


# ---------------------------------------------------------------------------
# B-T9: complete_task chain advance (APPROVED intermediate → next stage)
# ---------------------------------------------------------------------------

def test_critical_chain_walks_verifier_reviewer_critic(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", body="database migration",
                             assignee="coder", review_tier="critical")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"          # stage 0 (verifier) pending

        # stage 0: verifier APPROVED → re-park for stage 1 (reviewer)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        kb.complete_task(conn, tid, summary="verifier ok", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"
        assert kb._review_chain_target(conn, tid, kb._review_gate_config()) == "reviewer"

        # stage 1: reviewer APPROVED → re-park for stage 2 (critic)
        kb.claim_review_task(conn, tid, reviewer_profile="reviewer")
        kb.complete_task(conn, tid, summary="reviewer ok", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"
        assert kb._review_chain_target(conn, tid, kb._review_gate_config()) == "critic"

        # stage 2: critic APPROVED → terminal done
        kb.claim_review_task(conn, tid, reviewer_profile="critic")
        kb.complete_task(conn, tid, summary="critic ok", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"


def test_standard_tier_still_single_stage(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="trivial", body="reword label", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        kb.complete_task(conn, tid, summary="verifier ok", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"   # standard → one stage only


# ---------------------------------------------------------------------------
# B-T12: auto-retry renders structured findings (else plaintext fallback)
# ---------------------------------------------------------------------------

def test_auto_retry_feedback_renders_structured_findings(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        kb.block_task(conn, tid, reason="changes needed", reviewer_metadata={
            "verdict": "REQUEST_CHANGES",
            "blocking_findings": ["null deref in foo()", "missing test for bar"]})
        kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
        body = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? "
            "AND author = 'dispatcher' ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()["body"]
        assert "null deref in foo()" in body
        assert "missing test for bar" in body


def test_auto_retry_feedback_plaintext_fallback(kanban_home, gate_on):
    """No structured findings → the historical plaintext-reason path (unchanged)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="y", assignee="coder")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="just stuck")
        kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
        body = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? "
            "AND author = 'dispatcher' ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()["body"]
        assert "Previous block reason" in body
        assert "just stuck" in body


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


# ---------------------------------------------------------------------------
# CLI verb parity (K13): `hermes kanban complete` in worker context must hit
# the same gate as the in-process kanban_complete tool. Regression for the
# 2026-06-10 live finding: a claude-CLI premium worker completed via the CLI
# verb and bypassed the verifier (went straight to 'done').
# ---------------------------------------------------------------------------

def _cli_complete(task_id):
    """Invoke the real CLI handler the claude-CLI lifecycle bridge uses."""
    import argparse

    from hermes_cli import kanban as kanban_cli

    args = argparse.Namespace(
        task_ids=[task_id], summary="impl done", metadata=None, result=None,
    )
    return kanban_cli._cmd_complete(args)


def test_cli_complete_worker_context_routes_to_review(
    kanban_home, gate_on, monkeypatch
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="premium")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    # The task-id-in-env worker contract set by the spawn paths.
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert _cli_complete(tid) == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "review"


def test_cli_complete_operator_context_stays_direct_done(
    kanban_home, gate_on, monkeypatch
):
    """No worker env → operator completion keeps the direct done path."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="premium")
        kb.claim_task(conn, tid)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert _cli_complete(tid) == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"

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

def test_effective_review_tier_floor_explicit_raises_freely(kanban_home, monkeypatch):
    """Auto-floor (2026-06-21 Vision-Pushback, ersetzt 'explizit gewinnt beide Wege'):
    explicit may RAISE freely; a downgrade BELOW the hard-marker heuristic floor snaps
    back up unless a deliberate ack is logged. NULL → heuristic self-classifies."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        # explicit UPGRADES a trivial task (above the standard floor) → wins
        t1 = kb.create_task(conn, title="trivial", assignee="coder", review_tier="critical")
        assert kb._effective_review_tier(conn, t1) == "critical"
        # explicit DOWNGRADE below the critical floor, NO ack → snaps up to the floor
        t2 = kb.create_task(conn, title="db change",
                            body="run a database migration and deploy",
                            assignee="coder", review_tier="standard")
        assert kb._effective_review_tier(conn, t2) == "critical"
        # NULL + auto_tier ON → heuristic decides (critical marker in body)
        t3 = kb.create_task(conn, title="db change",
                            body="run a database migration", assignee="coder")
        assert kb._effective_review_tier(conn, t3) == "critical"
        # NULL, no markers → standard
        t4 = kb.create_task(conn, title="tweak copy", body="reword a label", assignee="coder")
        assert kb._effective_review_tier(conn, t4) == "standard"


def test_effective_review_tier_floor_allows_acked_downgrade(kanban_home, monkeypatch):
    """A logged review_tier_downgrade_ack lets an explicit below-floor value through —
    the deliberate, audit-trailed operator decision."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="db change",
                             body="run a database migration and deploy",
                             assignee="coder", review_tier="standard")
        # without ack: floor holds
        assert kb._effective_review_tier(conn, tid) == "critical"
        # log a deliberate downgrade ack → explicit standard now wins
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "review_tier_downgrade_ack", {"to_tier": "standard"})
        assert kb._effective_review_tier(conn, tid) == "standard"


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


def test_set_tier_below_floor_with_ack_records_event(kanban_home, monkeypatch):
    """acknowledge_downgrade=True logs a review_tier_downgrade_ack so an explicit
    below-floor tier actually takes effect; without it the safety floor holds."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="db change",
                             body="run a database migration and deploy", assignee="coder")
        # plain downgrade (no ack) → floor holds, downgrade has no effect
        assert kb.set_task_review_tier(conn, tid, "standard") is True
        assert kb._effective_review_tier(conn, tid) == "critical"
        # acknowledged downgrade → standard now wins + ack event recorded
        assert kb.set_task_review_tier(conn, tid, "standard", acknowledge_downgrade=True) is True
        assert kb._effective_review_tier(conn, tid) == "standard"
        acks = [e for e in kb.list_events(conn, tid) if e.kind == "review_tier_downgrade_ack"]
        assert acks and acks[-1].payload["to_tier"] == "standard"


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


# ---------------------------------------------------------------------------
# Phase-C-followup (a): Scout-Auto-Insertion bei review_tier:critical.
# Flag kanban.review_gate.auto_scout_on_critical (default OFF = byte-identical).
# Couples the two chokepoints where a task becomes critical:
#   (1) set_task_review_tier(critical)  (2) plan-ingest / decompose critical child.
# ---------------------------------------------------------------------------

@pytest.fixture
def auto_scout_on(monkeypatch):
    """Enable auto_scout_on_critical (the opt-in critical→scout coupling)."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder", "premium"}),
            "verifier_profile": "verifier",
            "auto_tier": False,
            "auto_scout_on_critical": True,
        },
    )
    return True


def _scout_parents(conn, tid):
    """Parent ids of ``tid`` whose task is a scout (assignee=='scout')."""
    out = []
    for pid in kb.parent_ids(conn, tid):
        p = kb.get_task(conn, pid)
        if p is not None and p.assignee == "scout":
            out.append(pid)
    return out


def test_auto_scout_off_is_byte_identical(kanban_home):
    """Default (flag absent/off): setting critical injects NO scout — today's behaviour."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="risky", assignee="coder")
        assert kb.set_task_review_tier(conn, tid, "critical") is True
        assert _scout_parents(conn, tid) == []
        assert kb.get_task(conn, tid).status == "ready"   # not demoted


def test_heuristic_critical_injects_scout_without_explicit_column(kanban_home, monkeypatch):
    """Self-gating: a task the heuristic rates critical (NO explicit review_tier
    column) gets the scout when auto_tier + auto_scout are on. The resolver
    (_effective_review_tier), not the raw column, drives the coupling — and the
    heuristic value is never stamped into the column (Landmine 1)."""
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True,
                 "auto_scout_on_critical": True},
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="run database migration + deploy", assignee="coder")
        assert kb.get_task(conn, tid).review_tier is None          # never stamped
        assert kb._maybe_inject_critical_scout(conn, tid) is not None
        assert kb.scout_predecessor_id(conn, tid) is not None


def test_set_critical_injects_scout_predecessor_when_flag_on(kanban_home, auto_scout_on):
    """Flag on: set_task_review_tier(critical) ensures ONE read-only scout predecessor."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="risky build", assignee="coder")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.set_task_review_tier(conn, tid, "critical") is True
        scouts = _scout_parents(conn, tid)
        assert len(scouts) == 1
        scout = kb.get_task(conn, scouts[0])
        assert scout.assignee == "scout"
        assert kb.parent_ids(conn, scouts[0]) == []          # scout has no parents
        assert kb.get_task(conn, tid).status == "todo"        # demoted ready->todo, waits on scout
        # Atomic dedup: the scout carries a per-task idempotency_key so two
        # concurrent critical setters converge on ONE scout (no race-created 2nd).
        key = conn.execute(
            "SELECT idempotency_key FROM tasks WHERE id=?", (scouts[0],)
        ).fetchone()[0]
        assert key == f"auto-scout:{tid}"


def test_non_critical_tier_does_not_inject_scout(kanban_home, auto_scout_on):
    """Flag on but tier=review: no scout — only critical couples."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="meh", assignee="coder")
        assert kb.set_task_review_tier(conn, tid, "review") is True
        assert _scout_parents(conn, tid) == []


def test_scout_injection_is_deduped(kanban_home, auto_scout_on):
    """Re-setting critical (or clear+re-set) never spawns a second scout."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="risky", assignee="coder")
        kb.set_task_review_tier(conn, tid, "critical")
        kb.set_task_review_tier(conn, tid, "critical")   # idempotent
        assert len(_scout_parents(conn, tid)) == 1
        # clear then re-set: still one scout (dedup is structural, not event-based)
        kb.set_task_review_tier(conn, tid, None)
        kb.set_task_review_tier(conn, tid, "critical")
        assert len(_scout_parents(conn, tid)) == 1


def test_scout_not_injected_for_running_task(kanban_home, auto_scout_on):
    """A task already past pre-run is not retro-fitted — no live-chain bending."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already going", assignee="coder")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        assert kb.set_task_review_tier(conn, tid, "critical") is True
        assert _scout_parents(conn, tid) == []            # too late, skipped


def test_decompose_critical_child_injects_scout_when_flag_on(kanban_home, auto_scout_on):
    """Plan-ingest chokepoint: a decomposed child stamped critical gets a scout predecessor."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="epic", triage=True)
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[
                {"title": "critical slice", "assignee": "coder", "review_tier": "critical"},
                {"title": "trivial slice", "assignee": "coder"},
            ],
        )
        assert kids is not None and len(kids) == 2
        crit, triv = kids
        assert len(_scout_parents(conn, crit)) == 1        # critical child scouted
        assert _scout_parents(conn, triv) == []            # trivial child not


def test_decompose_critical_child_no_scout_when_flag_off(kanban_home):
    """Flag off (default): decompose with a critical child injects no scout."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="epic", triage=True)
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[{"title": "crit", "assignee": "coder", "review_tier": "critical"}],
        )
        assert kids is not None
        assert _scout_parents(conn, kids[0]) == []


def test_decompose_scheduled_held_child_defers_scout(kanban_home, auto_scout_on):
    """Operator-held chain (initial_child_status='scheduled'): no auto-scout before
    release — spawning a dispatchable scout would bypass the operator hold. The
    flow-release path re-couples the scout post-approval."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="held epic", triage=True)
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[{"title": "crit", "assignee": "coder", "review_tier": "critical"}],
            initial_child_status="scheduled",
        )
        assert kids is not None
        assert _scout_parents(conn, kids[0]) == []            # deferred, not bypassed
        assert kb.get_task(conn, kids[0]).status == "scheduled"  # still held


def test_release_freigabe_hold_recouples_scout_for_critical_child(kanban_home, auto_scout_on):
    """Closes the held-chain loop: the decompose-time guard DEFERS (no bypass), and
    release_freigabe_hold RE-COUPLES the scout post-approval — so a held critical
    chain released via the bare operator path still gets its scout, just on RELEASE."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="held epic", triage=True, freigabe="operator")
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[{"title": "crit", "assignee": "coder", "review_tier": "critical"}],
            initial_child_status="scheduled", expected_root_status="triage",
        )
        assert kids is not None
        assert _scout_parents(conn, kids[0]) == []            # deferred while held
        # operator GO via the bare release path (not flow-release)
        assert kb.release_freigabe_hold(conn, root) is True
        assert len(_scout_parents(conn, kids[0])) == 1        # re-coupled on release
        # idempotent: a second release does not spawn a second scout
        kb.release_freigabe_hold(conn, root)
        assert len(_scout_parents(conn, kids[0])) == 1


# ---------------------------------------------------------------------------
# Slice b: batch_active_review_stages — the live review stage per task, read from
# the latest submitted_for_review event (powers the dashboard live-stage pill).
# ---------------------------------------------------------------------------

def test_batch_active_review_stages_latest_event_wins(kanban_home):
    """Returns the target_profile of the LATEST submitted_for_review event; tasks
    without such an event are omitted."""
    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="reviewing", assignee="coder")
        t2 = kb.create_task(conn, title="no review events", assignee="coder")
        with kb.write_txn(conn):
            kb._append_event(conn, t1, "submitted_for_review", {"target_profile": "verifier"})
            kb._append_event(conn, t1, "submitted_for_review", {"target_profile": "reviewer"})
        m = kb.batch_active_review_stages(conn, [t1, t2])
        assert m == {t1: "reviewer"}   # latest event wins; t2 (no event) omitted
        assert kb.batch_active_review_stages(conn, []) == {}

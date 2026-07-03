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

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _write_profile(home: Path, name: str) -> None:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("model: {}\n")


import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb
from hermes_cli import strategist_surface as ss


def _review_efficiency_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "review_efficiency_live_fixtures.json"
    return json.loads(path.read_text(encoding="utf-8"))[name]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ["coder", "premium", "scout"]:
        _write_profile(home, name)
    kb.init_db()
    return home


@pytest.fixture
def gate_on(monkeypatch):
    """Enable the review gate with coder/premium roles + an existing verifier."""
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
# B-T5: effective review tier (explicit column wins; NULL → auto only if opt-in)
# ---------------------------------------------------------------------------


def test_effective_review_tier_floor_explicit_raises_freely(kanban_home, monkeypatch):
    """Auto-floor (2026-06-21 Vision-Pushback, ersetzt 'explizit gewinnt beide Wege'):
    explicit may RAISE freely; a downgrade BELOW the hard-marker heuristic floor snaps
    back up unless a deliberate ack is logged. NULL → heuristic self-classifies."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        # explicit UPGRADES a trivial task (above the standard floor) → wins
        t1 = kb.create_task(
            conn, title="trivial", assignee="coder", review_tier="critical"
        )
        assert kb._effective_review_tier(conn, t1) == "critical"
        # explicit DOWNGRADE below the critical floor, NO ack → snaps up to the floor
        t2 = kb.create_task(
            conn,
            title="db change",
            body="run a database migration and deploy",
            assignee="coder",
            review_tier="standard",
        )
        assert kb._effective_review_tier(conn, t2) == "critical"
        # NULL + auto_tier ON → heuristic decides (critical marker in body)
        t3 = kb.create_task(
            conn, title="db change", body="run a database migration", assignee="coder"
        )
        assert kb._effective_review_tier(conn, t3) == "critical"
        # NULL, no markers → standard
        t4 = kb.create_task(
            conn, title="tweak copy", body="reword a label", assignee="coder"
        )
        assert kb._effective_review_tier(conn, t4) == "standard"


def test_effective_review_tier_floor_allows_acked_downgrade(kanban_home, monkeypatch):
    """A logged review_tier_downgrade_ack lets an explicit below-floor value through —
    the deliberate, audit-trailed operator decision."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="db change",
            body="run a database migration and deploy",
            assignee="coder",
            review_tier="standard",
        )
        # without ack: floor holds
        assert kb._effective_review_tier(conn, tid) == "critical"
        # log a deliberate downgrade ack → explicit standard now wins
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "review_tier_downgrade_ack", {"to_tier": "standard"}
            )
        assert kb._effective_review_tier(conn, tid) == "standard"


def test_effective_review_tier_auto_off_is_byte_identical(kanban_home, monkeypatch):
    """auto_tier OFF (default): a NULL-column risky task stays standard = today."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": False},
    )
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="db change",
            body="run a database migration and deploy",
            assignee="coder",
        )
        assert kb._effective_review_tier(conn, t) == "standard"  # auto OFF → no chain
        # explicit column still wins even with auto OFF
        t2 = kb.create_task(conn, title="x", assignee="coder", review_tier="critical")
        assert kb._effective_review_tier(conn, t2) == "critical"


def test_effective_review_tier_ignores_coder_contract_boilerplate(
    kanban_home, monkeypatch
):
    """The auto-injected coder-contract body (anti-scope: 'no deploy/migration/secret')
    must NOT drive the heuristic — else every bodyless code task would over-classify to
    critical (caught by live dogfood 2026-06-21). Real intent (title/user-spec) decides."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )
    with kb.connect() as conn:
        # bodyless ordinary code task → create_task appends the coder contract whose
        # anti-scope lists 'no deploy/migration/secret' → must STILL resolve standard
        ordinary = kb.create_task(conn, title="reword a button label", assignee="coder")
        assert kb._CODE_TASK_CONTRACT_MARKER in (kb.get_task(conn, ordinary).body or "")
        assert kb._effective_review_tier(conn, ordinary) == "standard"
        # a genuinely risky TITLE still classifies critical despite the same boilerplate
        risky = kb.create_task(
            conn, title="run database migration and deploy", assignee="coder"
        )
        assert kb._effective_review_tier(conn, risky) == "critical"


def test_effective_review_tier_does_not_critical_on_db_path_or_anti_scope(
    kanban_home, monkeypatch
):
    """Live regression for the false-critical cascade: file names and anti-scope
    risk words must not force the expensive verifier→reviewer→critic lane."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )
    with kb.connect() as conn:
        path_only = kb.create_task(
            conn,
            title="fix hermes_cli/kanban_db.py dispatcher edge case",
            assignee="coder",
        )
        assert kb._effective_review_tier(conn, path_only) == "standard"

        anti_scope = kb.create_task(
            conn,
            title="refactor code module",
            body="KEIN Schema-/Migrations-Change an der DB; no deploy, no secret access, no auth changes",
            assignee="coder",
        )
        assert kb._effective_review_tier(conn, anti_scope) == "review"

        drop_in = kb.create_task(
            conn,
            title="Reviewer-SOUL v2 draften",
            body="Vollständiger Drop-in-Draft für ~/.hermes/profiles/reviewer/SOUL.md",
            assignee="coder",
        )
        assert kb._effective_review_tier(conn, drop_in) == "standard"

        auth_enabled_visual = kb.create_task(
            conn,
            title="Review-Wert-Telemetrie end-to-end",
            body="Screenshot über den auth-enabled Visual-Harness",
            assignee="coder",
        )
        assert kb._effective_review_tier(conn, auth_enabled_visual) == "standard"

        real_db = kb.create_task(
            conn,
            title="DB-Migration durchführen",
            body="apply ALTER TABLE",
            assignee="coder",
        )
        assert kb._effective_review_tier(conn, real_db) == "critical"

        real_deploy_after_anti_scope = kb.create_task(
            conn,
            title="no database migration but deploy gateway change",
            assignee="coder",
        )
        assert (
            kb._effective_review_tier(conn, real_deploy_after_anti_scope) == "critical"
        )

        plural_security = kb.create_task(
            conn, title="rotate credentials", assignee="coder"
        )
        assert kb._effective_review_tier(conn, plural_security) == "critical"


def test_effective_review_tier_live_overfire_fixture_stays_noncritical(
    kanban_home, monkeypatch
):
    """Live fixture t_0f85a46e: filename/anti-scope markers must not overfire."""
    fixture = _review_efficiency_fixture("tier_substring_overfire")
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title=fixture["title"],
            body=fixture["body"],
            assignee=fixture["assignee"],
            kind=fixture["kind"],
        )
        assert kb._effective_review_tier(conn, tid) == fixture["expected_effective_tier"]


def test_review_value_scout_read_items_from_live_metadata_fixture(kanban_home):
    """Live fixture run 6033: Scout has read value via checked_files, not findings."""
    fixture = _review_efficiency_fixture("scout_read_value")
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title=fixture["title"],
            body=fixture["body"],
            assignee="scout",
        )
        with kb.write_txn(conn):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, started_at, ended_at, outcome,
                    metadata, input_tokens, output_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    "scout",
                    "done",
                    1782983400,
                    1782983450,
                    "completed",
                    json.dumps(fixture["metadata"]),
                    fixture["input_tokens"],
                    fixture["output_tokens"],
                ),
            )
        rows = {
            row["profile"]: row
            for row in kb.review_value_by_stage(conn, window_start=0)
        }

    scout = rows["scout"]
    assert scout["findings_blocking"] is None
    assert scout["tokens_per_finding"] is None
    assert scout["read_items"] == 1
    assert scout["tokens_per_read_item"] == fixture["input_tokens"]


def test_effective_review_tier_truncates_future_contract_versions(
    kanban_home, monkeypatch
):
    """Forward-compat: the classify-truncation must strip ANY coder-contract version,
    not only the exact current marker string. A future ``v2`` contract whose anti-scope
    still lists 'no deploy/migration/secret' would otherwise slip past a v1-only
    ``str.find`` and re-open the false-green over-classify bug (dogfood 2026-06-21)."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )
    with kb.connect() as conn:
        # Real intent ('reword a label') precedes a *future* contract marker carrying
        # the same risky anti-scope words. Truncation at the marker PREFIX must keep
        # the goal benign → standard; a v1-only match leaves the risky words in → critical.
        body = (
            "reword a label\n\n## Hermes Coder Contract v2\n"
            "ANTI-scope: no deploy, no database migration, no secret access"
        )
        t = kb.create_task(conn, title="ui tweak", body=body, assignee="coder")
        assert kb._effective_review_tier(conn, t) == "standard"


def test_effective_review_tier_logs_when_classify_raises(
    kanban_home, monkeypatch, caplog
):
    """A crash in ``classify_review_tier`` is swallowed to a ``standard`` floor
    (fail-open), but it must be LOGGED — otherwise a config error in the heuristic
    would silently down-gate every task to standard with no operator-visible signal."""
    import hermes_cli.control_plane_gate as cpg

    def boom(_spec):
        raise RuntimeError("classify exploded")

    monkeypatch.setattr(cpg, "classify_review_tier", boom)
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="coder")
        with caplog.at_level("WARNING"):
            assert kb._effective_review_tier(conn, t) == "standard"  # fail-open floor
        assert any(
            "review" in r.message.lower() and "tier" in r.message.lower()
            for r in caplog.records
        ), (
            f"expected a review-tier classify warning, got: {[r.message for r in caplog.records]}"
        )


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
        kb,
        "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "auto_tier": True},
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="db change",
            body="run a database migration and deploy",
            assignee="coder",
        )
        # plain downgrade (no ack) → floor holds, downgrade has no effect
        assert kb.set_task_review_tier(conn, tid, "standard") is True
        assert kb._effective_review_tier(conn, tid) == "critical"
        # acknowledged downgrade → standard now wins + ack event recorded
        assert (
            kb.set_task_review_tier(conn, tid, "standard", acknowledge_downgrade=True)
            is True
        )
        assert kb._effective_review_tier(conn, tid) == "standard"
        acks = [
            e
            for e in kb.list_events(conn, tid)
            if e.kind == "review_tier_downgrade_ack"
        ]
        assert acks and acks[-1].payload["to_tier"] == "standard"


# ---------------------------------------------------------------------------
# B-T6: ordered stage list per tier (missing profiles degrade gracefully)
# ---------------------------------------------------------------------------


def test_review_stages_for_tier(monkeypatch):
    cfg = {
        "verifier_profile": "verifier",
        "review_profile": "reviewer",
        "critic_profile": "critic",
    }
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    assert kb._review_stages_for_tier("standard", cfg) == ["verifier"]
    assert kb._review_stages_for_tier("review", cfg) == ["verifier", "reviewer"]
    assert kb._review_stages_for_tier("critical", cfg) == [
        "verifier",
        "reviewer",
        "critic",
    ]
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
        tid = kb.create_task(
            conn,
            title="hard db work",
            body="database migration",
            assignee="coder",
            review_tier="critical",
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="impl", review_gate=True)
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'submitted_for_review' ORDER BY id DESC LIMIT 1",
            (tid,),
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
        tid = kb.create_task(
            conn,
            title="x",
            body="database migration",
            assignee="coder",
            review_tier="critical",
        )
        kb.claim_task(conn, tid)
        kb.complete_task(
            conn, tid, summary="impl", review_gate=True
        )  # stage 0 → verifier
        cfg = kb._review_gate_config()
        assert kb._review_chain_target(conn, tid, cfg) == "verifier"


# ---------------------------------------------------------------------------
# B-T9: complete_task chain advance (APPROVED intermediate → next stage)
# ---------------------------------------------------------------------------


def test_critical_chain_walks_verifier_reviewer_critic(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="x",
            body="database migration",
            assignee="coder",
            review_tier="critical",
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"  # stage 0 (verifier) pending

        # stage 0: verifier APPROVED → re-park for stage 1 (reviewer)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        kb.complete_task(conn, tid, summary="verifier ok", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"
        assert (
            kb._review_chain_target(conn, tid, kb._review_gate_config()) == "reviewer"
        )

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
        tid = kb.create_task(
            conn, title="trivial", body="reword label", assignee="coder"
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        kb.complete_task(conn, tid, summary="verifier ok", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"  # standard → one stage only


# ---------------------------------------------------------------------------
# B-T12: auto-retry renders structured findings (else plaintext fallback)
# ---------------------------------------------------------------------------


def test_auto_retry_feedback_renders_structured_findings(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        kb.block_task(
            conn,
            tid,
            reason="changes needed",
            reviewer_metadata={
                "verdict": "REQUEST_CHANGES",
                "blocking_findings": ["null deref in foo()", "missing test for bar"],
            },
        )
        kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
        body = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? "
            "AND author = 'dispatcher' ORDER BY id DESC LIMIT 1",
            (tid,),
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
            "AND author = 'dispatcher' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()["body"]
        assert "Previous block reason" in body
        assert "just stuck" in body


# ---------------------------------------------------------------------------
# Producer routing
# ---------------------------------------------------------------------------


def test_code_worker_cannot_block_for_review_required(kanban_home, gate_on):
    """Review-required is not a blocker; code lanes must complete into the gate."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl review handoff", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        ok = kb.block_task(
            conn,
            tid,
            reason="review-required: please verify",
            expected_run_id=run_id,
        )

        assert ok is False
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == run_id
        events = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)
            ).fetchall()
        ]
        assert "review_required_block_rejected" in events
        assert "blocked" not in events

        assert kb.complete_task(
            conn,
            tid,
            summary="implementation ready",
            expected_run_id=run_id,
            review_gate=True,
        )
        assert kb.get_task(conn, tid).status == "review"


def test_code_worker_can_still_block_real_blocker(kanban_home, gate_on):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl needs secret", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        assert (
            kb.block_task(
                conn,
                tid,
                reason="needs credential from operator",
                expected_run_id=run_id,
            )
            is True
        )
        assert kb.get_task(conn, tid).status == "blocked"


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


def test_worker_cannot_bypass_review_gate_with_review_gate_false(kanban_home, gate_on):
    """A worker-owned code run cannot self-transition to done via review_gate=False."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder", kind="code")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        assert kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)

        assert kb.get_task(conn, tid).status == "review"


def test_review_gated_raw_sql_done_update_is_rejected(kanban_home, gate_on):
    """The DB itself rejects direct done writes for review-gated code tasks."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder", kind="code")
        kb.claim_task(conn, tid)

        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))

        assert kb.get_task(conn, tid).status == "running"


def test_review_gated_raw_sql_backstop_uses_canonical_kind_agnostic_gate(
    kanban_home, gate_on
):
    """Backstop protects any canonical code-assignee task, not only kind=code."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder", kind="research")
        kb.claim_task(conn, tid)

        assert kb._review_gate_should_apply(conn, tid, None)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))

        assert kb.get_task(conn, tid).status == "running"


def test_review_gated_raw_sql_backstop_uses_configured_code_roles(
    kanban_home, monkeypatch
):
    """Configured code roles are protected without another hardcoded SQL list."""
    _write_profile(kanban_home, "builder")
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"builder"}),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder", kind="text")
        kb.claim_task(conn, tid)

        assert kb._review_gate_should_apply(conn, tid, None)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))

        assert kb.get_task(conn, tid).status == "running"


def test_review_gated_backstop_migration_refreshes_legacy_trigger(
    kanban_home, monkeypatch
):
    """Legacy boards replace the hardcoded trigger on the next schema pass."""
    _write_profile(kanban_home, "builder")
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"builder"}),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS trg_review_gated_done_terminal_authority;
            CREATE TRIGGER trg_review_gated_done_terminal_authority
            BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'done'
              AND COALESCE(OLD.status, '') != 'done'
              AND COALESCE(NEW.kind, '') = 'code'
              AND COALESCE(NEW.assignee, '') IN ('coder', 'coder-claude', 'premium')
            BEGIN
              SELECT CASE
                WHEN kanban_review_done_authorized() != 1 THEN
                  RAISE(ABORT, 'legacy trigger')
              END;
            END;
            """
        )
        conn.execute("PRAGMA user_version=0")
        kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path().resolve()))

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder", kind="text")
        kb.claim_task(conn, tid)

        assert kb._review_gate_should_apply(conn, tid, None)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))

        assert kb.get_task(conn, tid).status == "running"


def test_review_gated_raw_sql_backstop_allows_non_gated_tasks(kanban_home, gate_on):
    """Direct SQL backstop stays inert when the canonical gate is false."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="research", assignee="scout", kind="code")
        kb.claim_task(conn, tid)

        assert not kb._review_gate_should_apply(conn, tid, None)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))

        assert kb.get_task(conn, tid).status == "done"


def test_review_diff_sentinel_uses_pre_run_commit_baseline(
    kanban_home, gate_on, tmp_path
):
    """Commit-then-complete changes are visible because the baseline is pre-run SHA."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    target = repo / "sentinel.txt"
    target.write_text("before\n")
    subprocess.run(["git", "add", "sentinel.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "before"],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="impl",
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        target.write_text("after\n")
        subprocess.run(["git", "add", "sentinel.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "after"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )

        assert kb.complete_task(
            conn, tid, summary="done", review_gate=True, expected_run_id=run_id
        )
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1",
            (tid, "submitted_for_review"),
        ).fetchone()
        payload = json.loads(ev["payload"])

        assert "sentinel.txt" in payload.get("changed_files", [])
        assert payload.get("diff_baseline") == "pre_run_commit_sha"


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("baseline\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def test_zero_diff_auto_review_tier_downgrades_to_single_verifier(
    kanban_home, tmp_path, monkeypatch
):
    """Live t_92528385 shape: no file edits should not pay reviewer stage by heuristic."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": True,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    repo = tmp_path / "repo-zero-diff"
    _init_repo(repo)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Selbststart-Probe: bestätigen und abschließen, keine Edits",
            body=(
                "Live-Beweis-Probe M1: Diese Kette wurde mit freigabe complete ingested.\n"
                "Deine einzige Aufgabe: bestätige mit einem Satz, dass du gestartet wurdest,\n"
                "und schließe den Task ab. KEINE Datei anfassen, KEIN Commit, keine Analyse."
            ),
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        assert kb.complete_task(
            conn,
            tid,
            summary="Gestartet: keine Datei-Edits, kein Commit, keine Analyse.",
            review_gate=True,
            expected_run_id=run_id,
        )
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1",
            (tid, "submitted_for_review"),
        ).fetchone()
        payload = json.loads(ev["payload"])

        assert payload["review_tier"] == "standard"
        assert payload["target_profile"] == "verifier"
        assert payload["review_tier_adjustment"] == {
            "from": "review",
            "to": "standard",
            "reason": "zero_diff",
        }

        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        assert kb.complete_task(conn, tid, summary="APPROVED", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"
        assert (
            len([
                e for e in kb.list_events(conn, tid) if e.kind == "submitted_for_review"
            ])
            == 1
        )


def test_final_review_completion_writes_review_released_event(kanban_home, gate_on):
    """Live t_92528385 event gap: final review approve gets an explicit release verb."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Selbststart-Probe: bestätigen und abschließen, keine Edits",
            body=(
                "Live-Beweis-Probe M1: Diese Kette wurde mit freigabe complete ingested.\n"
                "Deine einzige Aufgabe: bestätige mit einem Satz, dass du gestartet wurdest,\n"
                "und schließe den Task ab. KEINE Datei anfassen, KEIN Commit, keine Analyse."
            ),
            assignee="coder",
            kind="code",
            review_tier="review",
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="impl", review_gate=True)
        kb.claim_review_task(conn, tid, reviewer_profile="verifier")
        assert kb.complete_task(conn, tid, summary="verifier ok", review_gate=True)
        kb.claim_review_task(conn, tid, reviewer_profile="reviewer")
        assert kb.complete_task(conn, tid, summary="reviewer ok", review_gate=True)

        releases = [e for e in kb.list_events(conn, tid) if e.kind == "review_released"]
        assert len(releases) == 1
        assert releases[0].payload["verdict"] == "APPROVED"
        assert releases[0].payload["review_tier"] == "review"
        assert releases[0].payload["review_stage"] == 1
        assert releases[0].payload["target_profile"] == "reviewer"


def test_verdict_spawn_lane_resolver_error_fails_closed(
    kanban_home, gate_on, tmp_path, monkeypatch
):
    """Reviewer/critic spawn must not fall back to an unconstrained argv."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="reviewer")
        task = kb.get_task(conn, tid)
        assert task is not None

    def boom(*args, **kwargs):
        raise RuntimeError("unknown-runtime")

    monkeypatch.setattr(kb, "_active_lane_entry_for_profile", boom)

    with pytest.raises(RuntimeError, match="unknown-runtime"):
        kb._default_spawn(task, str(tmp_path))


def test_non_code_task_not_review_gated(kanban_home, gate_on):
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
        kb,
        "_review_gate_config",
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
        assert kb._run_originated_from_review(conn, tid, claimed.current_run_id) is True


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


def test_review_originated_request_changes_completion_does_not_go_done(
    kanban_home, gate_on
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl done", review_gate=True)
        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None and claimed.status == "running"

        assert kb.complete_task(
            conn,
            tid,
            summary="needs fixes",
            metadata={"verdict": "REQUEST_CHANGES"},
            review_gate=True,
        )

        assert kb.get_task(conn, tid).status == "blocked"
        row = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        assert row is not None
        assert row["verdict"] == "REQUEST_CHANGES"


def test_non_review_originated_approved_metadata_cannot_fake_review_completion(
    kanban_home, gate_on
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None

        assert kb.complete_task(
            conn,
            tid,
            summary="APPROVED",
            metadata={"verdict": "APPROVED"},
            review_gate=True,
        )

        assert kb.get_task(conn, tid).status == "review"
        row = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        assert row is not None
        assert row["verdict"] is None


# ---------------------------------------------------------------------------
# Dependency gating
# ---------------------------------------------------------------------------


def test_children_wait_for_verified_done(kanban_home, gate_on):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="coder")
        child = kb.create_task(conn, title="child", parents=[parent], assignee="coder")
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
        child = kb.create_task(conn, title="child", parents=[parent], assignee="coder")
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
    monkeypatch.setattr(kb, "_cleanup_workspace", lambda conn, tid: calls.append(tid))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="impl", review_gate=True)
    assert calls == []  # preserved for the verifier


def test_done_cleans_up_workspace(kanban_home, monkeypatch):
    calls = []
    monkeypatch.setattr(kb, "_cleanup_workspace", lambda conn, tid: calls.append(tid))
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
        task_ids=[task_id],
        summary="impl done",
        metadata=None,
        result=None,
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
        kb,
        "_review_gate_config",
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
        assert kb.get_task(conn, tid).status == "ready"  # not demoted


def test_heuristic_critical_injects_scout_without_explicit_column(
    kanban_home, monkeypatch
):
    """Self-gating: a task the heuristic rates critical (NO explicit review_tier
    column) gets the scout when auto_tier + auto_scout are on. The resolver
    (_effective_review_tier), not the raw column, drives the coupling — and the
    heuristic value is never stamped into the column (Landmine 1)."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "auto_scout_on_critical": True,
        },
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="run database migration + deploy", assignee="coder"
        )
        assert kb.get_task(conn, tid).review_tier is None  # never stamped
        assert kb._maybe_inject_critical_scout(conn, tid) is not None
        assert kb.scout_predecessor_id(conn, tid) is not None


def test_set_critical_injects_scout_predecessor_when_flag_on(
    kanban_home, auto_scout_on
):
    """Flag on: set_task_review_tier(critical) ensures ONE read-only scout predecessor."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="risky build", assignee="coder")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.set_task_review_tier(conn, tid, "critical") is True
        scouts = _scout_parents(conn, tid)
        assert len(scouts) == 1
        scout = kb.get_task(conn, scouts[0])
        assert scout.assignee == "scout"
        assert kb.parent_ids(conn, scouts[0]) == []  # scout has no parents
        assert (
            kb.get_task(conn, tid).status == "todo"
        )  # demoted ready->todo, waits on scout
        # Atomic dedup: the scout carries a per-task idempotency_key so two
        # concurrent critical setters converge on ONE scout (no race-created 2nd).
        key = conn.execute(
            "SELECT idempotency_key FROM tasks WHERE id=?", (scouts[0],)
        ).fetchone()[0]
        assert key == f"auto-scout:{tid}"


def test_scout_predecessor_gets_bounded_runtime_cap(kanban_home, monkeypatch):
    """P1-S1: a scout task carries a non-null, bounded max_runtime_seconds so a
    wedged read-only recon is reaped by enforce_max_runtime (which only acts on
    tasks with a non-null cap) — without it a stuck scout silently blocks its
    whole chain forever (auto_scout_on_critical is live)."""
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"verifier_profile": "verifier"}
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="risky build", assignee="coder")
        scout_id = kb.ensure_scout_predecessor(conn, tid)
        assert scout_id is not None
        cap = kb.get_task(conn, scout_id).max_runtime_seconds
        assert cap == kb._SCOUT_MAX_RUNTIME_SECONDS
        assert cap > 0


def test_scout_runtime_cap_respects_config_override(kanban_home, monkeypatch):
    """P1-S1: the scout TTL is tunable via kanban.review_gate.scout_max_runtime_seconds."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {"verifier_profile": "verifier", "scout_max_runtime_seconds": 600},
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        scout_id = kb.ensure_scout_predecessor(conn, tid)
        assert kb.get_task(conn, scout_id).max_runtime_seconds == 600


def test_blocked_scout_escalation_names_the_gated_chain(kanban_home):
    """P1-S2: a settled-blocked scout that gates downstream children produces an
    operator_escalation NAMING the blocked chain (not just a generic block), so a
    wedged read-only scout is actionable (unblock/complete) instead of silently
    deadlocking its chain forever — recompute_ready needs all parents done."""
    import json as _json

    with kb.connect() as conn:
        child = kb.create_task(conn, title="implement the slice", assignee="coder")
        scout_id = kb.ensure_scout_predecessor(conn, child)
        assert scout_id is not None
        assert kb.get_task(conn, child).status == "todo"  # gated on the scout
        # scout wedges → worker/operator blocks it (sticky → never auto-recovers)
        assert kb.block_task(conn, scout_id, reason="recon stuck") is True
        summary = kb.escalate_blocking_scouts_sweep(conn)
        assert scout_id in [e["task_id"] for e in summary["escalated"]]
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1",
            (scout_id, kb.OPERATOR_ESCALATION_EVENT),
        ).fetchone()
        payload = _json.loads(ev["payload"])
        gated_ids = [c["id"] for c in payload.get("blocking_chain", [])]
        assert child in gated_ids
        # the recommended action points the operator at the scout-frees-chain fix
        assert "scout" in payload.get("recommended_human_action", "").lower()
        # idempotent: a second sweep on the same block episode does not re-escalate
        assert kb.escalate_blocking_scouts_sweep(conn)["escalated"] == []


def test_transient_blocked_scout_not_escalated_by_blocking_sweep(kanban_home):
    """P1-S2: only STICKY-blocked scouts (never auto-recover) are surfaced. A scout
    with no sticky-block event is left to the runtime cap + self-healing lane."""
    with kb.connect() as conn:
        child = kb.create_task(conn, title="impl", assignee="coder")
        scout_id = kb.ensure_scout_predecessor(conn, child)
        # raw circuit-breaker style flip (no 'blocked' event) → not sticky
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (scout_id,))
        assert kb.escalate_blocking_scouts_sweep(conn)["escalated"] == []


@pytest.fixture
def _heuristic_critical_cfg(monkeypatch):
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "verifier_profile": "verifier",
            "auto_tier": True,
            "auto_scout_on_critical": True,
            "code_roles": frozenset({"coder", "premium"}),
        },
    )


def test_create_task_auto_scout_injects_for_heuristic_critical(
    kanban_home, _heuristic_critical_cfg
):
    """P1-S3: a standalone heuristic-critical task created with auto_scout=True gets
    the SAME scout predecessor as the decompose/release paths (closes the create-path
    gap; _maybe_inject_critical_scout was never called from create_task)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="run database migration and deploy",
            assignee="coder",
            auto_scout=True,
        )
        assert kb.scout_predecessor_id(conn, tid) is not None


def test_auto_scout_inherits_target_workspace(
    kanban_home, tmp_path, _heuristic_critical_cfg
):
    """A read-only scout must inspect the target repo, not an empty scratch dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="run database migration and deploy",
            assignee="coder",
            auto_scout=True,
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        scout_id = kb.scout_predecessor_id(conn, tid)
        assert scout_id is not None
        scout = kb.get_task(conn, scout_id)
        assert scout.workspace_kind == "dir"
        assert scout.workspace_path == str(repo)


def test_create_task_auto_scout_off_by_default(kanban_home, _heuristic_critical_cfg):
    """P1-S3: default (no auto_scout) is byte-identical — no scout. Decompose and
    internal callers are unaffected; only standalone entry points opt in."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="run database migration and deploy", assignee="coder"
        )
        assert kb.scout_predecessor_id(conn, tid) is None


def test_create_task_auto_scout_defers_for_held_task(
    kanban_home, _heuristic_critical_cfg
):
    """P1-S3: a held (blocked) standalone task does NOT get a scout even with
    auto_scout — held tasks defer their scout to release (no held-scout deadlock)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="run database migration and deploy",
            assignee="coder",
            auto_scout=True,
            initial_status="blocked",
        )
        assert kb.scout_predecessor_id(conn, tid) is None


def test_create_task_auto_scout_noop_for_non_critical(
    kanban_home, _heuristic_critical_cfg
):
    """P1-S3: auto_scout only couples on resolved-critical — a benign task gets none."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="reword a button label", assignee="coder", auto_scout=True
        )
        assert kb.scout_predecessor_id(conn, tid) is None


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
        kb.set_task_review_tier(conn, tid, "critical")  # idempotent
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
        assert _scout_parents(conn, tid) == []  # too late, skipped


def test_decompose_critical_child_injects_scout_when_flag_on(
    kanban_home, auto_scout_on
):
    """Plan-ingest chokepoint: a decomposed child stamped critical gets a scout predecessor."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="epic", triage=True)
        kids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="premium",
            children=[
                {
                    "title": "critical slice",
                    "assignee": "coder",
                    "review_tier": "critical",
                },
                {"title": "trivial slice", "assignee": "coder"},
            ],
        )
        assert kids is not None and len(kids) == 2
        crit, triv = kids
        assert len(_scout_parents(conn, crit)) == 1  # critical child scouted
        assert _scout_parents(conn, triv) == []  # trivial child not


def test_decompose_critical_child_no_scout_when_flag_off(kanban_home):
    """Flag off (default): decompose with a critical child injects no scout."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="epic", triage=True)
        kids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="premium",
            children=[
                {"title": "crit", "assignee": "coder", "review_tier": "critical"}
            ],
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
            conn,
            root,
            root_assignee="premium",
            children=[
                {"title": "crit", "assignee": "coder", "review_tier": "critical"}
            ],
            initial_child_status="scheduled",
        )
        assert kids is not None
        assert _scout_parents(conn, kids[0]) == []  # deferred, not bypassed
        assert kb.get_task(conn, kids[0]).status == "scheduled"  # still held


def test_release_freigabe_hold_recouples_scout_for_critical_child(
    kanban_home, auto_scout_on
):
    """Closes the held-chain loop: the decompose-time guard DEFERS (no bypass), and
    release_freigabe_hold RE-COUPLES the scout post-approval — so a held critical
    chain released via the bare operator path still gets its scout, just on RELEASE."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="held epic", triage=True, freigabe="operator")
        kids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="premium",
            children=[
                {"title": "crit", "assignee": "coder", "review_tier": "critical"}
            ],
            initial_child_status="scheduled",
            expected_root_status="triage",
        )
        assert kids is not None
        assert _scout_parents(conn, kids[0]) == []  # deferred while held
        # operator GO via the bare release path (not flow-release)
        assert kb.release_freigabe_hold(conn, root) is True
        assert len(_scout_parents(conn, kids[0])) == 1  # re-coupled on release
        # idempotent: a second release does not spawn a second scout
        kb.release_freigabe_hold(conn, root)
        assert len(_scout_parents(conn, kids[0])) == 1


def test_complete_freigabe_hold_archives_root_and_children(kanban_home):
    """The third disposition sibling (release=build, dismiss=veto, complete=
    done-elsewhere): closes a held freigabe:operator root whose work was
    ANDERWEITIG erledigt. Root and every still-held child move to archived,
    task_links stay intact, and the root carries a freigabe_completed event
    plus the mandatory rationale comment."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="held epic", triage=True, freigabe="operator")
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[{"title": "crit", "assignee": "coder"}],
            initial_child_status="scheduled", expected_root_status="triage",
        )
        assert kids is not None
        assert kb.complete_freigabe_hold(
            conn, root, author="pytest",
            note="Superseded: operator reviewed directly, chain not needed.",
        ) is True
        assert kb.get_task(conn, root).status == "archived"
        assert kb.get_task(conn, kids[0]).status == "archived"
        # task_links stay intact (decompose link direction: root is the child).
        links = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (root,)
        ).fetchall()
        assert [r["parent_id"] for r in links] == kids
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (root,)
            ).fetchall()
        ]
        assert "freigabe_completed" in kinds
        comments = kb.list_comments(conn, root)
        assert any("Superseded" in c.body for c in comments)


def test_complete_freigabe_hold_requires_author_and_note(kanban_home):
    """anti_scope: this is exclusively operator-/API-triggered — author and
    note are mandatory, unlike the auto-defaulted siblings."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="held epic", triage=True, freigabe="operator")
        kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[{"title": "crit", "assignee": "coder"}],
            initial_child_status="scheduled",
        )
        with pytest.raises(ValueError):
            kb.complete_freigabe_hold(conn, root, author="", note="done elsewhere")
        with pytest.raises(ValueError):
            kb.complete_freigabe_hold(conn, root, author="pytest", note="")
        assert kb.get_task(conn, root).status == "scheduled"


def test_complete_freigabe_hold_noop_on_non_operator_root(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="plain", assignee="coder")
        assert kb.complete_freigabe_hold(
            conn, root, author="pytest", note="n/a",
        ) is False
        assert kb.get_task(conn, root).status != "archived"


def test_complete_freigabe_hold_drops_from_proposals_and_avoids_silent_block_sweep(
    kanban_home,
):
    """done_when #2: the closed root disappears from held_operator_proposals,
    and the silent-block sweep never escalates it — its status leaves
    'scheduled' straight for 'archived' and never passes through 'blocked',
    the sweep's only predicate (:func:`kb.silent_block_task_ids`)."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="held epic", triage=True, freigabe="operator")
        kb.decompose_triage_task(
            conn, root, root_assignee="premium",
            children=[{"title": "crit", "assignee": "coder"}],
            initial_child_status="scheduled",
        )
        assert any(p["id"] == root for p in ss.held_operator_proposals(conn))
        assert kb.complete_freigabe_hold(
            conn, root, author="pytest", note="done elsewhere",
        ) is True
        assert not any(p["id"] == root for p in ss.held_operator_proposals(conn))
        kb.escalate_silent_blocks_sweep(conn)
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (root,)
            ).fetchall()
        ]
        assert kb.OPERATOR_ESCALATION_EVENT not in kinds


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
            kb._append_event(
                conn, t1, "submitted_for_review", {"target_profile": "verifier"}
            )
            kb._append_event(
                conn, t1, "submitted_for_review", {"target_profile": "reviewer"}
            )
        m = kb.batch_active_review_stages(conn, [t1, t2])
        assert m == {t1: "reviewer"}  # latest event wins; t2 (no event) omitted
        assert kb.batch_active_review_stages(conn, []) == {}


# ---------------------------------------------------------------------------
# S2: auto-scout inherits the target task's scope (source of truth) and warns
# against broadening. PlanSpec autoscout-context-integrity-2026-06-22.
# ---------------------------------------------------------------------------

_TARGET_SCOPED_BODY = (
    "Harden the approval-prompt redaction.\n\n"
    "Allowed scope: gateway/run.py, gateway/platforms/api_server.py.\n"
    "Acceptance criteria: redact credentials before the prompt renders.\n"
    "Anti-scope: do NOT touch acp_adapter/permissions.py.\n\n"
    "scope_contract:\n"
    "  version: 2\n"
    "  allowed_paths:\n"
    "    - /home/piet/.hermes/hermes-agent/gateway/run.py\n"
    "  allowed_tools:\n"
    "    - terminal\n"
)


def test_auto_scout_body_inherits_target_scope(kanban_home):
    """The scout body created for a scoped target carries that target's scope
    boundaries (id/title, Allowed scope, Acceptance criteria, Anti-scope,
    scope_contract/allowed paths) — not a generic recon instruction."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="redaction slice",
            assignee="coder",
            body=_TARGET_SCOPED_BODY,
        )
        scout_id = kb.ensure_scout_predecessor(conn, tid)
        assert scout_id is not None
        body = kb.get_task(conn, scout_id).body or ""
    # target identity
    assert tid in body
    assert "redaction slice" in body
    # inherited scope markers from the target body excerpt
    assert "Allowed scope" in body
    assert "Acceptance criteria" in body
    assert "Anti-scope" in body
    assert "scope_contract:" in body
    # explicit allowed-path list pulled out of the body
    assert "/home/piet/.hermes/hermes-agent/gateway/run.py" in body


def test_auto_scout_body_does_not_promote_forbidden_paths_to_allowed(kanban_home):
    """Forbidden path lists must never be relabelled as scout Allowed paths.

    Live worker smoke cards put sensitive paths in ``forbidden_paths`` inside a
    scope_contract. The scout summary used to collect every absolute path in the
    body and print it under "Allowed paths", which inverted the contract.
    """
    body = (
        "scope_contract:\n"
        "  allowed_paths:\n"
        "    - /safe/project/file.py\n"
        "  forbidden_paths:\n"
        "    - /home/piet/.env\n"
        "    - /home/piet/.hermes/config.yaml\n"
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="forbidden path smoke", assignee="coder", body=body
        )
        scout_id = kb.ensure_scout_predecessor(conn, tid)
        scout_body = kb.get_task(conn, scout_id).body or ""
    allowed_line = next(
        line for line in scout_body.splitlines() if line.startswith("Allowed paths")
    )
    forbidden_line = next(
        line for line in scout_body.splitlines() if line.startswith("Forbidden paths")
    )
    assert "/safe/project/file.py" in allowed_line
    assert "/home/piet/.env" not in allowed_line
    assert "/home/piet/.hermes/config.yaml" not in allowed_line
    assert "/home/piet/.env" in forbidden_line
    assert "/home/piet/.hermes/config.yaml" in forbidden_line


def test_auto_scout_body_warns_against_broadening(kanban_home):
    """The scout body must explicitly say the target body/operator directives are
    the source of truth and forbid broadening from title/recent work."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder", body="do the thing")
        scout_id = kb.ensure_scout_predecessor(conn, tid)
        body = kb.get_task(conn, scout_id).body or ""
    assert "Source of Truth" in body
    assert "Recent work" in body  # names the thing NOT to broaden from
    assert "enger statt breiter" in body  # err narrow, not broad


def test_scout_recon_body_handles_empty_target_body():
    """Helper-level: a target with a TRULY empty body still yields the warning
    plus an explicit 'no body — derive strictly' note (never silently
    scopeless). (A real coder task gets the Coder-Contract boilerplate body, so
    this path is exercised at the helper.)"""
    from types import SimpleNamespace

    fake = SimpleNamespace(id="t_empty", title="no-body target", body="")
    body = kb._scout_recon_body([fake])
    assert "t_empty" in body
    assert "Source of Truth" in body
    assert "keinen Body" in body


def test_scout_recon_body_empty_targets_is_warning_only():
    """No resolvable targets → intro + warning only, no target block."""
    body = kb._scout_recon_body([None])
    assert "Source of Truth" in body
    assert "## Ziel-Task" not in body  # no per-target block rendered

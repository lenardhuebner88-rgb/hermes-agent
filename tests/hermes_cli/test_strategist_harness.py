"""Tests for the Vision-Flywheel Phase 2 Strategist harness (I1).

All mocked — no real LLM call, no real usage API, no real ingest side effect
beyond a temp file board. The PlanSpec quality judge is disabled by the autouse
``_disable_spec_judge_by_default`` conftest fixture (HERMES_PLANSPEC_JUDGE=0).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs
from hermes_cli import strategist
from hermes_cli import strategist_surface
from hermes_cli import vision_metrics as vm


@pytest.fixture
def board_home(tmp_path, monkeypatch, all_assignees_spawnable):
    """Isolated temp board + hermes-home, mirroring the G1 surface tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return tmp_path


def _fake_usage(used_percent):
    """Minimal AccountUsageSnapshot-shaped stub with one weekly window."""
    window = SimpleNamespace(window_key="weekly", used_percent=used_percent, label="Current week")
    return SimpleNamespace(windows=(window,), provider="anthropic")


def _patch_budget(monkeypatch, used_percent):
    monkeypatch.setattr(
        "agent.account_usage.fetch_account_usage",
        lambda provider, **kw: _fake_usage(used_percent),
    )


def _seed_ledger(conn, error, *, outcome="crashed"):
    task = kb.create_task(conn, title=f"seed:{error[:20]}", assignee="coder")
    kb.claim_task(conn, task)
    kb._record_task_failure(
        conn, task, error, outcome=outcome, failure_limit=5, release_claim=True, end_run=True
    )
    return task


# --------------------------------------------------------------------------- #
# 1. Budget > 80 % → skip
# --------------------------------------------------------------------------- #
def test_budget_over_threshold_skips(board_home, monkeypatch):
    _patch_budget(monkeypatch, 85.0)
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")  # would otherwise yield a lever

    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir)

    assert result["skipped"] is True
    assert result["used_percent"] == 85.0
    assert "80" in result["reason"]
    assert result["ingested"] == []
    # nothing landed on the surface
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_budget_within_threshold_proceeds(board_home, monkeypatch):
    _patch_budget(monkeypatch, 42.0)
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir)
    assert result["skipped"] is False


# --------------------------------------------------------------------------- #
# 2. Empty ROI landscape → idle (0 specs)
# --------------------------------------------------------------------------- #
def test_empty_landscape_is_idle(board_home, monkeypatch):
    _patch_budget(monkeypatch, 10.0)
    # empty ledger + metrics with no gaps → no levers
    metrics = {"autonomy_pct": 95, "green_gate_streak": 10}
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, metrics=metrics)

    assert result["skipped"] is False
    assert result["idle"] is True
    assert result["candidates"] == 0
    assert result["ingested"] == []
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


# --------------------------------------------------------------------------- #
# 3. Hits → <=5 held, annotated, correct provenance
# --------------------------------------------------------------------------- #
def test_hits_produce_capped_annotated_held_proposals(board_home, monkeypatch):
    _patch_budget(monkeypatch, 30.0)
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")  # transient
        _seed_ledger(conn, "gate failed: tests failed")  # real-bug
        _seed_ledger(conn, "merge conflict in api.ts")  # conflict
        _seed_ledger(conn, "bad-spec: acceptance criteria cannot be met")  # bad-spec
        _seed_ledger(conn, "flaky test passed on retry")  # flaky
    # plus an autonomy gap → the deterministic counter-metric loser
    metrics = {"autonomy_pct": 70, "green_gate_streak": 3}

    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, metrics=metrics)

    assert result["skipped"] is False
    assert result["idle"] is False
    # CAP enforced
    assert len(result["ingested"]) <= strategist.CAP_MAX
    assert len(result["ingested"]) >= 1
    # the blunt autonomy lever was self-gated out, never ingested
    ingested_keys = {item["key"] for item in result["ingested"]}
    assert "AUTON-UPLIFT" not in ingested_keys
    assert any(g["key"] == "AUTON-UPLIFT" for g in result["gated_out"])

    with kb.connect() as conn:
        proposals = strategist_surface.held_operator_proposals(conn)
    assert len(proposals) == len(result["ingested"])
    assert len(proposals) <= strategist.CAP_MAX
    for prop in proposals:
        assert prop["created_by"] == strategist.STRATEGIST_AUTHOR
        # annotation round-tripped through build_root_body → parse_annotation
        assert prop["target_metric"]
        assert prop["roi"]
        assert prop["counter_metric"]
        # chain has a build + review subtask
        assert prop["subtask_count"] == 2


def test_cap_limits_to_five(board_home, monkeypatch):
    _patch_budget(monkeypatch, 30.0)
    # feed six passing drafts through the --drafts-file seam (each grounded, so
    # the draft-path presence-gate passes them all and only CAP trims)
    drafts = [
        {
            "key": f"DRAFT-{i}",
            "title": f"Lever number {i}",
            "lane": "coder-claude",
            "target_metric": f"metric {i} up",
            "roi": "positive",
            "counter_metric": f"guardrail {i} held",
            "grounding": f"git log und grep in hermes_cli/strategist.py belegen Luecke {i}",
            "counter_risk": 0.2,
            "gain_weight": 1.0,
            "cost": 0.3,
            "signal_strength": float(10 - i),  # distinct scores for deterministic ranking
        }
        for i in range(6)
    ]
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, drafts=drafts)
    assert result["survivors"] == 6
    assert len(result["ingested"]) == 5
    with kb.connect() as conn:
        assert len(strategist_surface.held_operator_proposals(conn)) == 5


def test_cap_keeps_higher_rank_score_not_higher_roi_score(board_home, monkeypatch):
    """STRATEGIST-RANKING-S1: propose()'s CAP step must preserve derive_levers'
    rank_score (value-density) order — not re-sort survivors by raw roi_score,
    which would override it. Lever A has a bigger raw roi_score (higher cost,
    higher signal) but a lower rank_score than lever B (cheap, high density)."""
    _patch_budget(monkeypatch, 30.0)
    drafts = [
        {
            "key": "DRAFT-HIGH-ROI-LOW-RANK",
            "title": "High raw ROI, expensive",
            "lane": "coder-claude",
            "target_metric": "metric A up",
            "roi": "positive",
            "counter_metric": "guardrail A held",
            "grounding": "git log und grep in hermes_cli/strategist.py belegen Luecke A",
            "counter_risk": 0.1,
            "gain_weight": 1.0,
            "cost": 2.0,
            "signal_strength": 10.0,  # roi_score = 10*1 - 2 = 8, rank_score = 8/2 = 4
        },
        {
            "key": "DRAFT-LOW-ROI-HIGH-RANK",
            "title": "Lower raw ROI, cheap and value-dense",
            "lane": "coder-claude",
            "target_metric": "metric B up",
            "roi": "positive",
            "counter_metric": "guardrail B held",
            "grounding": "git log und grep in hermes_cli/strategist.py belegen Luecke B",
            "counter_risk": 0.1,
            "gain_weight": 1.0,
            "cost": 0.3,
            "signal_strength": 3.0,  # roi_score = 3*1 - 0.3 = 2.7, rank_score = 2.7/0.3 = 9
        },
    ]
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, drafts=drafts, cap=1)
    assert result["survivors"] == 2
    assert len(result["ingested"]) == 1
    assert result["ingested"][0]["key"] == "DRAFT-LOW-ROI-HIGH-RANK"


# --------------------------------------------------------------------------- #
# 4. Self-gate drops a counter-metric loser
# --------------------------------------------------------------------------- #
def test_self_gate_rejects_counter_metric_loser():
    # autonomy lever carries counter_risk 0.6 > budget 0.5
    loser = strategist._autonomy_lever(gap=20.0)
    verdict = strategist.self_gate(loser)
    assert verdict.passed is False
    assert "Counter-Metrik" in verdict.reason
    # even though its raw ROI score is positive
    assert loser.roi_score > 0


def test_self_gate_accepts_bounded_positive_lever():
    context = {
        "metrics": None,
        "ledger": {"by_class": {kb.HEILER_CLASS_TRANSIENT: 3}, "total": 3, "entries": []},
        "suppressed": set(),
    }
    levers = strategist.derive_levers(context)
    assert len(levers) == 1
    verdict = strategist.self_gate(levers[0])
    assert verdict.passed is True


def test_derive_levers_signal_is_distinct_roots_not_raw_events():
    """LEDGER-BYCLASS-DISTINCT-ROOTS-S1: the lever signal_strength must come
    from the per-class *distinct root* count, not the raw event count, so one
    root that escalates repeatedly cannot over-state its cluster. The raw event
    count stays available in the ledger (recurrence still visible) but does not
    drive the ROI signal."""
    context = {
        "metrics": None,
        "ledger": {
            "by_class": {kb.HEILER_CLASS_TRANSIENT: 9},      # 9 raw events …
            "roots_by_class": {kb.HEILER_CLASS_TRANSIENT: 1},  # … but ONE root
            "total": 9,
            "root_total": 1,
            "entries": [],
        },
        "suppressed": set(),
    }
    levers = strategist.derive_levers(context)
    assert len(levers) == 1
    # signal reflects distinct roots (1.0), not the inflated event count (9.0).
    assert levers[0].signal_strength == 1.0


def test_derive_levers_falls_back_to_event_count_when_roots_absent():
    """Defense-in-depth fallback: a legacy/injected context that predates
    ``roots_by_class`` still derives a lever off the raw ``by_class`` count, so
    the read-path hardening degrades gracefully and never silences the signal."""
    context = {
        "metrics": None,
        "ledger": {"by_class": {kb.HEILER_CLASS_TRANSIENT: 4}, "total": 4, "entries": []},
        "suppressed": set(),
    }
    levers = strategist.derive_levers(context)
    assert len(levers) == 1
    assert levers[0].signal_strength == 4.0


def test_self_gate_rejects_missing_counter_metric():
    lever = strategist.Lever(
        key="X",
        title="t",
        lane="coder-claude",
        target_metric="up",
        roi="hi",
        counter_metric="",
        rationale="r",
        gain_weight=1.0,
        cost=0.1,
        counter_risk=0.1,
    )
    assert strategist.self_gate(lever).passed is False


# --------------------------------------------------------------------------- #
# 4b. COST-AWARENESS-S1: the strategist prioritises $-burn
# --------------------------------------------------------------------------- #
def _cost_ctx(profiles, *, coverage_pct=100.0):
    return {"metrics": {"cost_per_task": {"coverage": {"coverage_pct": coverage_pct}}},
            "cost": {"profiles": profiles},
            "ledger": {"by_class": {}, "total": 0, "entries": []},
            "suppressed": set()}


def test_cost_lever_opens_for_expensive_lane():
    """A lane whose effective burn exceeds the threshold opens a single, self-
    gate-passing cost-efficiency lever; signal scales with burn but is capped."""
    ctx = _cost_ctx([
        {"profile": "coder-claude", "cost_usd": 0.0, "cost_usd_equivalent": 364.0},
        {"profile": "coder", "cost_usd": 0.0, "cost_usd_equivalent": 129.0},
    ])
    levers = strategist.derive_levers(ctx)
    cost_levers = [lv for lv in levers if lv.source == "cost"]
    assert len(cost_levers) == 1  # only the single costliest lane
    lv = cost_levers[0]
    assert lv.key == "COST-EFFICIENCY-CODER-CLAUDE"
    assert "coder-claude" in lv.target_metric
    assert strategist.self_gate(lv).passed is True
    # signal capped so one hot lane can't swamp the cap
    assert lv.signal_strength == pytest.approx(strategist.COST_SIGNAL_CAP)


def test_cost_lever_idle_below_threshold():
    """All lanes below the threshold → no cost lever (idle is correct)."""
    ctx = _cost_ctx([
        {"profile": "verifier", "cost_usd": 0.24, "cost_usd_equivalent": 0.0},
        {"profile": "coder", "cost_usd": 0.0, "cost_usd_equivalent": 1.0},
    ])
    assert [lv for lv in strategist.derive_levers(ctx) if lv.source == "cost"] == []


def test_cost_lever_suppressed_when_cost_coverage_is_low():
    """Sparse metered coverage is not enough grounding for a cost magnitude lever."""
    ctx = _cost_ctx([
        {"profile": "coder-claude", "cost_usd": 0.0, "cost_usd_equivalent": 364.0},
    ], coverage_pct=6.7)
    assert [lv for lv in strategist.derive_levers(ctx) if lv.source == "cost"] == []


def test_cost_lever_skips_synthetic_bucket():
    """The nameless '(ohne profil)' bucket is never proposed as a lane to optimise,
    even if it is the costliest."""
    ctx = _cost_ctx([
        {"profile": "(ohne profil)", "cost_usd": 0.0, "cost_usd_equivalent": 999.0},
        {"profile": "coder", "cost_usd": 0.0, "cost_usd_equivalent": 5.0},
    ])
    assert [lv for lv in strategist.derive_levers(ctx) if lv.source == "cost"] == []


def test_cost_lever_is_suppressed_when_vetoed():
    """A vetoed cost lever key is not re-raised (closed veto loop)."""
    ctx = _cost_ctx([
        {"profile": "coder-claude", "cost_usd": 0.0, "cost_usd_equivalent": 364.0},
    ])
    ctx["suppressed"] = {"COST-EFFICIENCY-CODER-CLAUDE"}
    assert [lv for lv in strategist.derive_levers(ctx) if lv.source == "cost"] == []


def test_cost_lever_round_trips_as_held_proposal(board_home, monkeypatch):
    """End-to-end: an injected cost view drives a held, annotated proposal on the
    G1 surface — the strategist now ships cost-reduction work, not just
    escalation/autonomy levers."""
    _patch_budget(monkeypatch, 30.0)
    cost = {"profiles": [
        {"profile": "coder-claude", "cost_usd": 0.0, "cost_usd_equivalent": 364.0},
    ]}
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir,
                                metrics={"autonomy_pct": 95, "green_gate_streak": 10,
                                         "cost_per_task": {"coverage": {"coverage_pct": 100.0}}},
                                cost=cost)
    assert result["skipped"] is False
    keys = {item["key"] for item in result["ingested"]}
    assert "COST-EFFICIENCY-CODER-CLAUDE" in keys


# --------------------------------------------------------------------------- #
# 5. Reflect reads approved vs vetoed
# --------------------------------------------------------------------------- #
def _make_held_proposal(conn, key, title_suffix):
    root = kb.create_task(
        conn,
        title=f"PlanSpec {key}: {title_suffix}",
        body="held proposal",
        assignee=None,
        created_by=strategist.STRATEGIST_AUTHOR,
    )
    conn.execute(
        "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
        (root,),
    )
    conn.commit()
    return root


def test_reflect_reads_approved_and_vetoed(board_home, tmp_path):
    with kb.connect() as conn:
        approved_root = _make_held_proposal(conn, "HEILER-TRANSIENT", "approved one")
        vetoed_root = _make_held_proposal(conn, "AUTON-UPLIFT", "vetoed one")
        # operator actions
        assert kb.release_freigabe_hold(conn, approved_root, author="operator") is True
        assert kb.dismiss_freigabe_hold(conn, vetoed_root, author="operator") is True
        assert any(event.kind == "archived" for event in kb.list_events(conn, vetoed_root))

    notes_path = tmp_path / "state" / "strategist" / "reflections.jsonl"
    with kb.connect() as conn:
        result = strategist.reflect(conn, since=0, notes_path=notes_path)

    assert result["note"]["approved"] == 1
    assert result["note"]["vetoed"] == 1
    assert result["note"]["vetoed_levers"] == ["AUTON-UPLIFT"]
    assert result["note"]["approved_levers"] == ["HEILER-TRANSIENT"]
    # learning notes written
    assert notes_path.exists()
    record = json.loads(notes_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["vetoed"] == 1
    # vetoed lever merged into the suppression set
    vetoed_set = json.loads((notes_path.parent / "vetoed_levers.json").read_text(encoding="utf-8"))
    assert "AUTON-UPLIFT" in vetoed_set




def test_reflect_records_autoresearch_veto_signal(board_home, tmp_path):
    # Drive the SANCTIONED veto path end-to-end: the reconciler creates the
    # escalation, the real operator veto (kb.veto_operator_escalation) writes the
    # freigabe_vetoed event. No raw event injection — the test exercises the same
    # path the Dashboard/API uses, so it can't go green on an unreachable state.
    from hermes_cli import autoresearch_reconcile as reconcile

    with kb.connect() as conn:
        task_id = reconcile._escalate(
            conn,
            {
                "id": "p1",
                "finding_id": "p1",
                "title": "Autoresearch silent except finding",
                "mode": "code",
                "severity": "high",
                "subsystem": "auth",
                "theme": "silent-except",
                "status": "proposed",
            },
            reason="operator review required",
        )
        assert kb.veto_operator_escalation(conn, task_id, author="operator") is True
        assert any(event.kind == "archived" for event in kb.list_events(conn, task_id))

    notes_path = tmp_path / "state" / "strategist" / "reflections.jsonl"
    with kb.connect() as conn:
        result = strategist.reflect(conn, since=0, notes_path=notes_path)

    assert result["note"]["vetoed"] == 1
    assert result["note"]["vetoed_autoresearch_signals"] == ["silent-except"]
    assert result["note"]["vetoed_levers"] == ["autoresearch:silent-except"]
    assert result["vetoed"][0]["source"] == "autoresearch"
    vetoed_set = json.loads((notes_path.parent / "vetoed_levers.json").read_text(encoding="utf-8"))
    assert "autoresearch:silent-except" in vetoed_set


def test_vetoed_lever_is_suppressed_on_next_propose(board_home, monkeypatch, tmp_path):
    _patch_budget(monkeypatch, 20.0)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "vetoed_levers.json").write_text(json.dumps(["HEILER-TRANSIENT"]), encoding="utf-8")
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")  # transient → would be a lever

    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, notes_dir=notes_dir)
    # suppressed → not proposed
    assert all(item["key"] != "HEILER-TRANSIENT" for item in result["ingested"])


# --------------------------------------------------------------------------- #
# 5b. Reflect rolling window — no calendar-midnight blind gap for late vetoes
# --------------------------------------------------------------------------- #
def test_reflect_rolling_window_catches_previous_evening_veto(board_home, tmp_path):
    """A freigabe_vetoed after last_reflect_run_ts but on the previous calendar
    evening must still count when reflect() is called with no explicit since.

    Calendar-midnight default left a nightly blind window: timer at 20:00, event
    scan open-ended upward, so a veto between 20:00–midnight was seen by no run.
    """
    # Freeze "now" to a deterministic local afternoon so midnight fallback would
    # clearly exclude a previous-evening event if the rolling stamp were ignored.
    now = datetime(2026, 7, 17, 20, 0, 0).timestamp()
    prev_evening_20 = datetime(2026, 7, 16, 20, 0, 0).timestamp()
    prev_evening_21 = datetime(2026, 7, 16, 21, 30, 0).timestamp()

    stamp_path = strategist.default_reflect_last_run_path()
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(
        json.dumps({"last_reflect_run_ts": int(prev_evening_20)}),
        encoding="utf-8",
    )

    with kb.connect() as conn:
        vetoed_root = _make_held_proposal(conn, "LATE-EVENING-VETO", "vetoed after 20:00")
        assert kb.dismiss_freigabe_hold(conn, vetoed_root, author="operator") is True
        # Backdate the freigabe_vetoed event into the previous calendar evening,
        # after the prior reflect stamp but before today's midnight.
        conn.execute(
            "UPDATE task_events SET created_at = ? "
            "WHERE task_id = ? AND kind = 'freigabe_vetoed'",
            (int(prev_evening_21), vetoed_root),
        )
        conn.commit()

    notes_path = tmp_path / "state" / "strategist" / "reflections.jsonl"
    with kb.connect() as conn:
        result = strategist.reflect(conn, notes_path=notes_path, now=now)

    assert result["note"]["vetoed"] >= 1
    assert "LATE-EVENING-VETO" in result["note"]["vetoed_levers"]
    assert result["note"]["since"] == int(prev_evening_20)
    vetoed_set = json.loads((notes_path.parent / "vetoed_levers.json").read_text(encoding="utf-8"))
    assert "LATE-EVENING-VETO" in vetoed_set
    # Successful run advances the stamp to this run's now.
    stamped = json.loads(stamp_path.read_text(encoding="utf-8"))
    assert stamped["last_reflect_run_ts"] == int(now)


def test_reflect_first_run_falls_back_to_midnight_and_writes_stamp(board_home, tmp_path):
    """No reflect_last_run.json → midnight fallback, then stamp is written."""
    now = datetime(2026, 7, 17, 20, 0, 0).timestamp()
    expected_midnight = strategist._local_midnight_epoch(now)
    stamp_path = strategist.default_reflect_last_run_path()
    assert not stamp_path.exists()

    with kb.connect() as conn:
        # Seed a same-day veto so the window is non-empty under midnight fallback.
        vetoed_root = _make_held_proposal(conn, "FIRST-RUN-VETO", "first run")
        assert kb.dismiss_freigabe_hold(conn, vetoed_root, author="operator") is True

    notes_path = tmp_path / "state" / "strategist" / "reflections.jsonl"
    with kb.connect() as conn:
        result = strategist.reflect(conn, notes_path=notes_path, now=now)

    assert result["note"]["since"] == expected_midnight
    assert result["note"]["vetoed"] == 1
    assert stamp_path.exists()
    stamped = json.loads(stamp_path.read_text(encoding="utf-8"))
    assert stamped["last_reflect_run_ts"] == int(now)


# --------------------------------------------------------------------------- #
# Annotation contract: build_root_body renders strategist_meta, surface reads it
# --------------------------------------------------------------------------- #
def test_lever_markdown_round_trips_annotation():
    context = {
        "metrics": None,
        "ledger": {"by_class": {kb.HEILER_CLASS_REAL_BUG: 4}, "total": 4, "entries": []},
        "suppressed": set(),
    }
    lever = strategist.derive_levers(context)[0]
    md = strategist.lever_to_markdown(lever)
    assert "freigabe: operator" in md
    assert "strategist_meta" in md
    # no template residue that the rubric would reject
    assert "TODO" not in md and "<" not in md and "..." not in md


def test_lever_markdown_emits_grounded_scope_and_gate_review_tier():
    lever = strategist.Lever(
        key="GATE-FIX-PYTHON-1",
        title="Fix the strategist gate",
        lane="coder",
        target_metric="gate green",
        roi="high",
        counter_metric="existing tests stay green",
        rationale="repair a persistent gate failure",
        grounding="failure in hermes_cli/strategist.py:1308",
        gain_weight=1.0,
        cost=0.1,
        counter_risk=0.1,
    )

    md = strategist.lever_to_markdown(lever)

    assert 'scope_files:\n        - "hermes_cli/strategist.py"' in md
    assert "review_tier: review" in md


def test_lever_markdown_prepends_scout_when_grounding_has_no_path():
    lever = strategist.Lever(
        key="UNSCOPED-1",
        title="Investigate unknown implementation",
        lane="coder",
        target_metric="unknown failure resolved",
        roi="high",
        counter_metric="existing tests stay green",
        rationale="the current evidence does not identify a file",
        grounding="dashboard observation without a repository path",
        gain_weight=1.0,
        cost=0.1,
        counter_risk=0.1,
    )

    md = strategist.lever_to_markdown(lever)

    assert "id: UNSCOPED-1-SCOUT" in md
    assert "kind: analysis" in md
    assert 'deps: ["UNSCOPED-1-SCOUT"]' in md


# --------------------------------------------------------------------------- #
# STRATEGIST-SELF-GROUNDING-S1 — grounding presence-gate on the DRAFT path only
# --------------------------------------------------------------------------- #
def _grounded_draft(key="GROUNDED-1", grounding="git log zeigt kein vorhandenes Ziel; grep in hermes_cli/strategist.py findet keine Implementierung"):
    """A fully-formed Opus draft carrying a non-empty grounding evidence field."""
    return {
        "key": key,
        "title": f"Hebel {key}",
        "lane": "coder-claude",
        "target_metric": "Kennzahl X anheben",
        "roi": "positiv",
        "counter_metric": "Guardrail Y gehalten",
        "rationale": "Begruendung fuer den Hebel.",
        "grounding": grounding,
        "counter_risk": 0.2,
        "gain_weight": 1.0,
        "cost": 0.3,
        "signal_strength": 1.0,
    }


def test_draft_without_grounding_is_blocked(board_home, monkeypatch):
    """(a) A strategist draft with NO grounding field is deterministically
    blocked from ingest — never reaches the board, surfaced as grounding_blocked."""
    _patch_budget(monkeypatch, 20.0)
    draft = _grounded_draft(key="UNGROUNDED")
    draft.pop("grounding")  # the operator's failure mode: an ungrounded lever

    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, drafts=[draft])

    # blocked: not ingested, recorded with a reason, nothing on the surface
    assert result["ingested"] == []
    assert any(b["key"] == "UNGROUNDED" for b in result["grounding_blocked"])
    blocked = next(b for b in result["grounding_blocked"] if b["key"] == "UNGROUNDED")
    assert "grounding" in blocked["reason"].lower()
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_draft_with_grounding_ingests_and_surfaces(board_home, monkeypatch):
    """(b) A draft WITH a non-empty grounding field is ingested and the evidence
    is visible in the held root body AND on the held_operator_proposals surface."""
    _patch_budget(monkeypatch, 20.0)
    evidence = "git log zeigt kein vorhandenes Ziel; grep in hermes_cli/strategist.py findet keine Implementierung"
    draft = _grounded_draft(key="GROUNDED", grounding=evidence)

    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, drafts=[draft])

    assert result["grounding_blocked"] == []
    assert len(result["ingested"]) == 1
    root_id = result["ingested"][0]["root_task_id"]

    with kb.connect() as conn:
        body = conn.execute("SELECT body FROM tasks WHERE id=?", (root_id,)).fetchone()["body"]
        proposals = strategist_surface.held_operator_proposals(conn)

    # evidence is in the held root body (operator-visible)
    assert evidence in body
    # and surfaced as a parsed field on the proposal surface
    assert len(proposals) == 1
    assert proposals[0]["grounding"] == evidence


def test_general_ingest_path_unaffected_by_grounding_gate(board_home, tmp_path):
    """(c) Non-regression: a Vault/Operator PlanSpec WITHOUT a grounding field
    still ingests via the general planspecs.ingest_planspec path (= hermes plan
    ingest). The presence-gate must NEVER touch the general path."""
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    spec = plans_root / "operator-spec.md"
    spec.write_text(
        "\n".join(
            [
                "---",
                "status: vorgeschlagen",
                "owner: Operator",
                "slice: OP-1",
                "topic: Ein handgeschriebener Operator-Spec",
                "freigabe: operator",
                "live_test_depth: smoke",
                "taskgraph_hints:",
                "  binding: true",
                "  subtasks:",
                "    - id: OP-1-S1",
                "      title: Operator-Hebel bauen (Build plus Test)",
                "      lane: coder-claude",
                "      deps: []",
                "      acceptance_criteria:",
                "        - Etwas Sinnvolles ist gebaut und getestet",
                "      body: Bau den Operator-Hebel.",
                "---",
                "",
                "# OP-1",
                "",
                "Ein Operator-Spec ohne grounding-Feld.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = planspecs.ingest_planspec(spec, board=None, author="operator", plans_root=plans_root)
    assert result.get("root_task_id")
    assert result.get("subtask_count") == 1


# --------------------------------------------------------------------------- #
# GREEN-GATE-AUTOHEAL-LOOP-S1 — red nightly gate → HELD fix-PlanSpec
# --------------------------------------------------------------------------- #
def _gate_records(*nights):
    """Build green-gate fail records: each night is (date, gate, detail)."""
    return [
        {
            "date": date,
            "result": "fail",
            "ts": f"{date}T03:00:00+00:00",
            "first_fail": {"gate": gate, "detail": detail},
        }
        for (date, gate, detail) in nights
    ]


def test_gate_fix_idle_on_single_red_night(board_home):
    """AC-2: a single red night never opens a spec (idle is correct)."""
    records = _gate_records(("2026-06-21", "python", "assert boom"))
    out_dir = board_home / "specs"
    result = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=records)
    assert result["triggered"] is False
    assert result["ingested"] is None
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_gate_fix_dry_run_detects_without_ingest(board_home):
    records = _gate_records(
        ("2026-06-20", "tsc", "type error in src/x"),
        ("2026-06-21", "tsc", "type error in src/x"),
    )
    out_dir = board_home / "specs"
    result = strategist.propose_gate_fix(
        board=None, out_dir=out_dir, gate_records=records, do_ingest=False
    )
    assert result["triggered"] is True
    assert result["ingested"]["dry_run"] is True
    assert not out_dir.exists()  # nothing written on a dry run
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_gate_fix_opens_held_planspec_on_two_red_nights(board_home):
    """AC-1: >=2 consecutive same-cause red nights ingest ONE held, operator-gated
    fix-PlanSpec (build + review) with the autoheal provenance."""
    records = _gate_records(
        ("2026-06-20", "python", "E assert foo == bar in test_x"),
        ("2026-06-21", "python", "E assert foo == bar in test_x"),
    )
    out_dir = board_home / "specs"
    result = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=records)

    assert result["triggered"] is True
    assert result["gate"] == "python"
    assert result["red_nights"] == 2
    ing = result["ingested"]
    assert ing["root_task_id"]
    assert ing["already_ingested"] is False
    assert ing["subtask_count"] == 2  # build + review
    assert ing["freigabe"] == "operator"  # HELD / operator-gated, never auto-deploy

    with kb.connect() as conn:
        proposals = strategist_surface.held_operator_proposals(conn)
    assert len(proposals) == 1
    prop = proposals[0]
    assert prop["created_by"] == strategist.GATE_FIX_AUTHOR
    assert prop["target_metric"]
    assert prop["counter_metric"]
    # the held root is scheduled (not dispatchable) until an operator releases it
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id=?", (ing["root_task_id"],)
        ).fetchone()
    assert row["status"] == "scheduled"
    assert row["freigabe"] == "operator"


def test_gate_fix_is_idempotent_per_cause(board_home):
    """AC-2: re-running while the SAME cause persists (a third red night, with a
    volatile detail tail) dedups to the same chain — no PlanSpec spam."""
    out_dir = board_home / "specs"
    first = _gate_records(
        ("2026-06-20", "python", "boom number 1"),
        ("2026-06-21", "python", "boom number 2"),
    )
    r1 = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=first)

    third = first + [
        {
            "date": "2026-06-22",
            "result": "fail",
            "ts": "2026-06-22T03:00:00+00:00",
            "first_fail": {"gate": "python", "detail": "boom number 3"},
        }
    ]
    r2 = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=third)

    assert r1["ingested"]["already_ingested"] is False
    assert r2["ingested"]["already_ingested"] is True
    assert r2["red_nights"] == 3  # detection still sees the growing streak
    assert r1["key"] == r2["key"]  # stable identity across volatile detail
    assert r1["ingested"]["root_task_id"] == r2["ingested"]["root_task_id"]
    with kb.connect() as conn:
        assert len(strategist_surface.held_operator_proposals(conn)) == 1


def test_gate_fix_distinct_cause_opens_second_chain(board_home):
    """A genuinely different cause is a different chain (not a false dedup, not a
    supersede-conflict)."""
    out_dir = board_home / "specs"
    py = _gate_records(
        ("2026-06-20", "python", "assert alpha"),
        ("2026-06-21", "python", "assert alpha"),
    )
    r1 = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=py)
    build = _gate_records(
        ("2026-06-22", "build", "tsc type error"),
        ("2026-06-23", "build", "tsc type error"),
    )
    r2 = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=build)

    assert r1["key"] != r2["key"]
    assert r1["ingested"]["root_task_id"] != r2["ingested"]["root_task_id"]
    with kb.connect() as conn:
        assert len(strategist_surface.held_operator_proposals(conn)) == 2


def test_persistent_red_triage_is_idempotent_while_window_ramps(board_home):
    """AC-2 (persistent-red path): re-running while the SAME red file set persists
    but the window count ramps (2-of-3 → 3-of-3) must dedup to the same chain —
    NOT a supersede-conflict. Regression guard: the rendered triage spec must not
    interpolate the volatile red_count, or the content hash drifts as the window
    grows and the follow-up night returns an ingest_error instead of the
    documented ``already_ingested``."""
    out_dir = board_home / "specs"
    detail = (
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/tools/test_voice_mode.py  (1 test failed)\n"
    )
    first = _gate_records(
        ("2026-06-20", "python", detail),
        ("2026-06-21", "python", detail),
    )
    r1 = strategist.propose_persistent_red_triage(
        board=None, out_dir=out_dir, gate_records=first
    )

    third = first + [
        {
            "date": "2026-06-22",
            "result": "fail",
            "ts": "2026-06-22T03:00:00+00:00",
            "first_fail": {"gate": "python", "detail": detail},
        }
    ]
    r2 = strategist.propose_persistent_red_triage(
        board=None, out_dir=out_dir, gate_records=third
    )

    assert r1["triggered"] is True
    assert r2["triggered"] is True
    assert r2["red_count"] == 3  # detection still sees the growing window
    assert r1["key"] == r2["key"]  # stable identity across the volatile count
    assert r1["ingested"]["already_ingested"] is False
    assert r2["ingested"]["already_ingested"] is True  # was a conflict pre-fix
    assert r1["ingested"]["root_task_id"] == r2["ingested"]["root_task_id"]
    with kb.connect() as conn:
        assert len(strategist_surface.held_operator_proposals(conn)) == 1


def test_persistent_red_triage_dedups_recent_archived_key_only(board_home):
    """Archived triage keys stay deduped for seven days, without suppressing
    genuinely new fingerprints or permanent re-filing after the cooldown."""
    out_dir = board_home / "specs"

    def records_for(test_file: str):
        detail = (
            "=== 1 files with test failures (1 tests failed) ===\n"
            f"  {test_file}  (1 test failed)\n"
        )
        return _gate_records(
            ("2026-07-30", "python", detail),
            ("2026-07-31", "python", detail),
        )

    first = strategist.propose_persistent_red_triage(
        board=None,
        out_dir=out_dir,
        gate_records=records_for("tests/tools/test_voice_mode.py"),
    )
    first_root = first["ingested"]["root_task_id"]
    with kb.connect() as conn:
        assert kb.archive_task(conn, first_root) is True

    recent_rerun = strategist.propose_persistent_red_triage(
        board=None,
        out_dir=out_dir,
        gate_records=records_for("tests/tools/test_voice_mode.py"),
    )
    assert recent_rerun["ingested"] is None
    assert recent_rerun["skipped_recent_idempotency_key"] is True

    distinct = strategist.propose_persistent_red_triage(
        board=None,
        out_dir=out_dir,
        gate_records=records_for("tests/tools/test_terminal_tool.py"),
    )
    assert distinct["ingested"]["already_ingested"] is False

    with kb.connect() as conn:
        conn.execute(
            "UPDATE task_events SET created_at = created_at - (8 * 24 * 60 * 60) "
            "WHERE task_id = ? AND kind = 'archived'",
            (first_root,),
        )
        conn.commit()

    expired_rerun = strategist.propose_persistent_red_triage(
        board=None,
        out_dir=out_dir,
        gate_records=records_for("tests/tools/test_voice_mode.py"),
    )
    assert expired_rerun["ingested"]["already_ingested"] is False
    assert expired_rerun["ingested"]["root_task_id"] != first_root

    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT t.status FROM tasks AS t "
            "WHERE t.created_by = ? AND NOT EXISTS ("
            "SELECT 1 FROM task_links AS l WHERE l.child_id = t.id)",
            (strategist.GATE_TRIAGE_AUTHOR,),
        ).fetchall()
    assert len(rows) == 3


def test_persistent_red_triage_leaker_head_planspec_is_actionable(board_home):
    """AC-1 (operator layer, LIVE 2026-07-06 shape): a leaker-only head with
    attributed nights behind it must render an ACTIONABLE Triage-PlanSpec — real
    gate + real files listed, never 'Gate unknown' / '(unbekannt)'. The auto-
    opened spec that motivated this task listed no files precisely because the
    head night was leaker-only harness noise."""
    out_dir = board_home / "specs"
    records = [
        {"date": "2026-07-04", "result": "fail", "ts": "2026-07-04T03:00:00+00:00",
         "first_fail": {"gate": "python",
                        "detail": "  tests/hermes_cli/test_a.py  (1 test failed)\n"}},
        {"date": "2026-07-05", "result": "fail", "ts": "2026-07-05T03:00:00+00:00",
         "first_fail": {"gate": "python",
                        "detail": "  tests/hermes_cli/test_b.py  (2 tests failed)\n"}},
        # head night: leaker-only harness noise (no product first_fail cause)
        {"date": "2026-07-06", "result": "fail", "ts": "2026-07-06T03:00:00+00:00",
         "leaker_only": True, "leakers": ["python: tests/flaky.py"]},
    ]
    result = strategist.propose_persistent_red_triage(
        board=None, out_dir=out_dir, gate_records=records, do_ingest=False
    )
    assert result["triggered"] is True
    assert result["gate"] == "python"  # not the 'unknown' sentinel
    # anchored on the most recent attributed night (07-05):
    assert result["red_files"] == ["tests/hermes_cli/test_b.py"]
    assert result["key"].startswith("GATE-TRIAGE-PYTHON-")

    # the human-facing PlanSpec body lists the real file, never "(unbekannt)"
    lever = strategist._persistent_red_triage_lever(
        vm.derive_persistent_red_triage(records)
    )
    assert "(unbekannt)" not in lever.rationale
    assert "tests/hermes_cli/test_b.py" in lever.rationale
    assert "Gate 'python'" in lever.title


# --------------------------------------------------------------------------- #
# GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1 — log-backfill a legacy un-attributed
# predecessor so the live 06-20/06-21 red series self-heals
# --------------------------------------------------------------------------- #
def _unattributed_red(date):
    """A red ledger night that predates the first_fail format (no payload)."""
    return {"date": date, "result": "fail", "ts": f"{date}T03:00:00+00:00"}


_LIVE_HEAD_DETAIL = (
    "Python (run_tests.sh):\n\n"
    "=== 6 files with test failures (9 tests failed) ===\n"
    "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
    "  tests/agent/transports/test_codex_transport.py  (1 test failed)\n"
    "  tests/hermes_cli/test_dashboard_admin_endpoints.py  (3 tests failed)\n"
    "  tests/hermes_cli/test_redact_config_bridge.py  (1 test failed)\n"
    "  tests/hermes_cli/test_startup_plugin_gating.py  (1 test failed)\n"
    "  tests/tools/test_voice_mode.py  (2 tests failed)\n"
    "Volles Log: /x/20260621-052029/python.log"
)
# the 06-20 night's python.log: 5 of the head's 6 failing files (a subset).
_LIVE_PREV_LOG = (
    "=== 5 files with test failures (8 tests failed) ===\n"
    "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
    "  tests/agent/transports/test_codex_transport.py  (1 test failed)\n"
    "  tests/hermes_cli/test_dashboard_admin_endpoints.py  (3 tests failed)\n"
    "  tests/hermes_cli/test_startup_plugin_gating.py  (1 test failed)\n"
    "  tests/tools/test_voice_mode.py  (2 tests failed)\n"
)


def test_gate_fix_heals_live_legacy_predecessor_via_log_backfill(board_home):
    """AC-1 (live 06-20/06-21): the older red night is un-attributed (predates the
    first_fail format) and the head is attributed. Reading the predecessor's gate
    log proves the SAME failing-file signature, so EXACTLY ONE held, operator-gated
    fix-PlanSpec opens instead of staying idle."""
    records = [
        _unattributed_red("2026-06-20"),
        *_gate_records(("2026-06-21", "python", _LIVE_HEAD_DETAIL)),
    ]
    reader = lambda date, fails: _LIVE_PREV_LOG if date == "2026-06-20" else None
    out_dir = board_home / "specs"
    result = strategist.propose_gate_fix(
        board=None, out_dir=out_dir, gate_records=records, night_log_reader=reader
    )
    assert result["triggered"] is True
    assert result["gate"] == "python"
    assert result["red_nights"] == 2
    assert result["dates"] == ["2026-06-21", "2026-06-20"]
    ing = result["ingested"]
    assert ing["root_task_id"]
    assert ing["already_ingested"] is False
    assert ing["freigabe"] == "operator"  # HELD, never auto-deploy
    with kb.connect() as conn:
        assert len(strategist_surface.held_operator_proposals(conn)) == 1


def test_gate_fix_idle_when_predecessor_log_shows_different_cause(board_home):
    """AC-2 counter: the predecessor's log shows a demonstrably DIFFERENT failure
    domain — the two reds are NOT merged into one series, so nothing opens (no
    HELD spam, no false merge of two genuinely different causes)."""
    records = [
        _unattributed_red("2026-06-20"),
        *_gate_records(("2026-06-21", "python", _LIVE_HEAD_DETAIL)),
    ]
    different = (
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/other/test_unrelated.py  (1 test failed)\n"
    )
    reader = lambda date, fails: different if date == "2026-06-20" else None
    out_dir = board_home / "specs"
    result = strategist.propose_gate_fix(
        board=None, out_dir=out_dir, gate_records=records, night_log_reader=reader
    )
    assert result["triggered"] is False
    assert result["ingested"] is None
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_gate_fix_idle_when_predecessor_log_unavailable(board_home):
    """AC-2: with no reader (or a reader that can't find the log) the legacy case
    stays idle — the regression we are fixing must not become a guessed heal."""
    records = [
        _unattributed_red("2026-06-20"),
        *_gate_records(("2026-06-21", "python", _LIVE_HEAD_DETAIL)),
    ]
    out_dir = board_home / "specs"
    # default reader (None) -> pure ledger -> idle (the pre-fix behaviour)
    result = strategist.propose_gate_fix(board=None, out_dir=out_dir, gate_records=records)
    assert result["triggered"] is False
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_gate_fix_chain_is_not_reflected_on(board_home, tmp_path):
    """The autoheal author is distinct from STRATEGIST_AUTHOR, so reflect (which
    scopes to the strategist) never tallies an autoheal hold."""
    records = _gate_records(
        ("2026-06-20", "python", "boom"),
        ("2026-06-21", "python", "boom"),
    )
    strategist.propose_gate_fix(board=None, out_dir=board_home / "specs", gate_records=records)
    notes_path = tmp_path / "state" / "strategist" / "reflections.jsonl"
    with kb.connect() as conn:
        result = strategist.reflect(conn, since=0, notes_path=notes_path)
    assert result["note"]["approved"] == 0
    assert result["note"]["vetoed"] == 0


def test_draft_path_still_applies_vetoed_dedup(board_home, monkeypatch, tmp_path):
    """(d) Non-regression: the vetoed_levers.json dedup still suppresses a draft
    even when that draft carries a grounding field (dedup runs alongside the
    new presence-gate, not instead of it)."""
    _patch_budget(monkeypatch, 20.0)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "vetoed_levers.json").write_text(json.dumps(["VETOED-KEY"]), encoding="utf-8")

    drafts = [
        _grounded_draft(key="VETOED-KEY"),  # suppressed → must not ingest
        _grounded_draft(key="FRESH-KEY"),  # grounded + not vetoed → ingests
    ]
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, drafts=drafts, notes_dir=notes_dir)

    ingested_keys = {item["key"] for item in result["ingested"]}
    assert "VETOED-KEY" not in ingested_keys
    assert "FRESH-KEY" in ingested_keys
    # the vetoed key was dropped by dedup, NOT by the grounding gate (it had grounding)
    assert all(b["key"] != "VETOED-KEY" for b in result["grounding_blocked"])


# --------------------------------------------------------------------------- #
# GATE-FLAKY-RETRY-HONESTY-S1 — flaky (fail->pass) file → HELD de-flake PlanSpec
# --------------------------------------------------------------------------- #
def _flaky_night(date, leakers, *, leaker_only=True):
    rec = {"date": date, "result": "fail", "ts": f"{date}T03:00:00+00:00",
           "leakers": list(leakers)}
    if leaker_only:
        rec["leaker_only"] = True
    return rec


def test_deflake_idle_when_no_flaky_file(board_home):
    records = [{"date": "2026-07-18", "result": "fail", "ts": "2026-07-18T03:00:00+00:00",
               "first_fail": {"gate": "python", "detail": "real regression"}}]
    out_dir = board_home / "specs"
    result = strategist.propose_deflake(board=None, out_dir=out_dir, gate_records=records)
    assert result["triggered"] is False
    assert result["filed"] == []
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_deflake_dry_run_detects_without_ingest(board_home):
    records = [_flaky_night("2026-07-18", ["python: tests/agent/test_delegate.py"])]
    out_dir = board_home / "specs"
    result = strategist.propose_deflake(
        board=None, out_dir=out_dir, gate_records=records, do_ingest=False
    )
    assert result["triggered"] is True
    assert len(result["filed"]) == 1
    assert result["filed"][0]["dry_run"] is True
    assert not out_dir.exists()  # nothing written on a dry run
    with kb.connect() as conn:
        assert strategist_surface.held_operator_proposals(conn) == []


def test_deflake_opens_one_held_planspec_per_flaky_file(board_home):
    """AC-1/AC-2b: each confirmed flake gets exactly one HELD, operator-gated
    de-flake PlanSpec (build + review)."""
    records = [
        _flaky_night("2026-07-10", ["python: tests/agent/test_delegate.py"]),
        _flaky_night("2026-07-12", ["vitest: src/foo.test.ts"]),
    ]
    out_dir = board_home / "specs"
    result = strategist.propose_deflake(board=None, out_dir=out_dir, gate_records=records)

    assert result["triggered"] is True
    assert len(result["filed"]) == 2
    for f in result["filed"]:
        assert f["root_task_id"]
        assert f["already_ingested"] is False

    with kb.connect() as conn:
        proposals = strategist_surface.held_operator_proposals(conn)
    assert len(proposals) == 2
    for prop in proposals:
        assert prop["created_by"] == strategist.GATE_DEFLAKE_AUTHOR
        assert prop["target_metric"]
        assert prop["counter_metric"]
    # each held root is scheduled + operator-gated (never auto-deploy)
    with kb.connect() as conn:
        for f in result["filed"]:
            row = conn.execute(
                "SELECT status, freigabe FROM tasks WHERE id=?", (f["root_task_id"],)
            ).fetchone()
            assert row["status"] == "scheduled"
            assert row["freigabe"] == "operator"


def test_deflake_is_idempotent_per_file(board_home):
    """AC-2b: re-running while the SAME file keeps flaking (more nights, so it now
    counts as recurring) dedups to the same chain — no PlanSpec spam."""
    out_dir = board_home / "specs"
    first = [_flaky_night("2026-07-10", ["python: tests/agent/test_delegate.py"])]
    r1 = strategist.propose_deflake(board=None, out_dir=out_dir, gate_records=first)

    third = first + [
        _flaky_night("2026-07-12", ["python: tests/agent/test_delegate.py"]),
        _flaky_night("2026-07-13", ["python: tests/agent/test_delegate.py"]),
    ]
    r2 = strategist.propose_deflake(board=None, out_dir=out_dir, gate_records=third)

    assert r1["filed"][0]["already_ingested"] is False
    assert r2["filed"] == []
    assert r2["skipped_existing"] == ["tests/agent/test_delegate.py"]
    # now recurring (3 distinct nights) — escalation surfaced on the summary
    assert r2["recurring"] == ["tests/agent/test_delegate.py"]
    with kb.connect() as conn:
        assert len(strategist_surface.held_operator_proposals(conn)) == 1


def test_deflake_filing_drives_counter_metric_to_zero(board_home):
    """AC-2b end-to-end: before filing the counter is non-zero; after
    propose_deflake writes the filed-key set the metric reads 0."""
    records = [_flaky_night("2026-07-18", ["python: tests/agent/test_delegate.py"])]
    filed_path = board_home / ".hermes" / "state" / "deflake-filed.json"

    before = vm._green_gate_metric(records, deflake_filed=vm.read_deflake_filed(filed_path))
    assert before["flake_debt"]["value"] == 1

    out_dir = board_home / "specs"
    strategist.propose_deflake(
        board=None, out_dir=out_dir, gate_records=records, filed_path=filed_path
    )
    after = vm._green_gate_metric(records, deflake_filed=vm.read_deflake_filed(filed_path))
    assert after["flake_debt"]["value"] == 0


def test_run_propose_files_deflake_before_budget_skipped_proposer_and_is_idempotent(
    board_home, monkeypatch
):
    """The deterministic de-flake sibling runs before the budget-gated proposer.

    A later budget skip must not swallow a neutralized fail->pass candidate, and
    a second identical strategist run must not re-ingest that PlanSpec chain.
    """
    records = [_flaky_night("2026-07-18", ["python: tests/agent/test_delegate.py"])]
    out_dir = board_home / "specs"
    monkeypatch.setattr(vm, "read_gate_records", lambda *args, **kwargs: records)

    def budget_skipped_proposer(**kwargs):
        assert vm.read_deflake_filed() == {"GATE-DEFLAKE-PYTHON-785df46f"}
        return {"skipped": "budget", "candidates": 0, "ingested": []}

    monkeypatch.setattr(strategist, "propose", budget_skipped_proposer)
    args = SimpleNamespace(
        board=None,
        out_dir=out_dir,
        drafts_file=None,
        budget_provider=None,
        budget_threshold=None,
        cap=None,
        dry_run=False,
    )

    first = strategist.run_propose(args)
    with kb.connect() as conn:
        first_chain_ids = {task.id for task in kb.list_tasks(conn, limit=20)}
    second = strategist.run_propose(args)

    assert first["skipped"] == "budget"
    assert first["deflake"]["filed"][0]["already_ingested"] is False
    assert second["deflake"]["filed"] == []
    assert second["deflake"]["skipped_existing"] == ["tests/agent/test_delegate.py"]
    with kb.connect() as conn:
        assert {task.id for task in kb.list_tasks(conn, limit=20)} == first_chain_ids


def test_flake_debt_leaf_metric_has_negative_verdict_direction():
    """A falling unfiled-flake count is a measurable improvement."""
    assert strategist._resolve_verdict_direction("green_gate_streak.flake_debt.value") == -1


# --------------------------------------------------------------------------- #
# GATE-RED-NIGHT-EMPTY-EXTRACTION-GUARD-S1 — operator layer: a red gate must
# never file a blind triage card with red_files=[]. Either the raw failing-list
# fallback names the files, or an explicit extraction-anomaly card is opened.
# --------------------------------------------------------------------------- #

# The LIVE 2026-08-02 shape behind the blind GATE-TRIAGE-PYTHON-cd7a0054 card.
_GUARD_DETAIL_ISOLATED = (
    "python (isolated): tests/acp/test_bar.py\n"
    "▶ launching test runner\n"
    "No test files to run\n"
)
_GUARD_DETAIL_NO_PATHS = (
    "python (run_tests.sh):\n"
    "=========================== short test summary info ============================\n"
    "========================= 3 failed, 8 passed in 0.97s ==========================\n"
)


def _guard_records(detail):
    return [
        {"date": "2026-08-01", "result": "fail", "ts": "2026-08-01T03:00:00+00:00",
         "first_fail": {"gate": "python", "detail": detail}},
        {"date": "2026-08-02", "result": "fail", "ts": "2026-08-02T03:00:00+00:00",
         "first_fail": {"gate": "python", "detail": detail}},
    ]


def test_blind_triage_card_is_replaced_by_recovered_file_list(board_home):
    """AC-1: the live blind card's evidence NAMED the failing file; the card now
    lists it instead of '(unbekannt)'."""
    records = _guard_records(_GUARD_DETAIL_ISOLATED)
    result = strategist.propose_persistent_red_triage(
        board=None, out_dir=board_home / "specs", gate_records=records,
        min_reds=2, window=3, do_ingest=False,
    )
    assert result["triggered"] is True
    assert result["red_files_source"] == "raw-failing-list"
    assert result["red_files_effective"] == ["tests/acp/test_bar.py"]

    lever = strategist._persistent_red_triage_lever(
        vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    )
    assert "(unbekannt)" not in lever.rationale
    assert "tests/acp/test_bar.py" in lever.rationale
    assert "EXTRACTION-ANOMALY" not in lever.key


def test_unrecoverable_red_opens_classifiable_extraction_anomaly_card(board_home):
    """AC-1: red FROM test failures with nothing extractable opens an explicit
    extraction-anomaly item — a diagnosable defect, not a blind worker brief."""
    records = _guard_records(_GUARD_DETAIL_NO_PATHS)
    result = strategist.propose_persistent_red_triage(
        board=None, out_dir=board_home / "specs", gate_records=records,
        min_reds=2, window=3, do_ingest=False,
    )
    assert result["triggered"] is True
    assert result["red_files_source"] == "extraction-anomaly"
    assert result["extraction_anomaly"]["kind"] == "extraction-anomaly"
    assert result["key"] == "GATE-TRIAGE-PYTHON-EXTRACTION-ANOMALY-" + hashlib.sha1(
        result["fingerprint"].encode("utf-8")
    ).hexdigest()[:8]

    lever = strategist._persistent_red_triage_lever(
        vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    )
    # the brief must point at the extraction, never at a '(unbekannt)' file list
    assert "(unbekannt)" not in lever.rationale
    assert "Extraktions-Anomalie" in lever.title
    assert "_extract_failing_test_files" in lever.rationale


def test_extraction_anomaly_card_dedups_across_nights(board_home):
    """AC-2 (no alarm noise): the same broken extraction on a further night must
    render the SAME key/body — one chain, not one card per night."""
    out_dir = board_home / "specs"
    base = _guard_records(_GUARD_DETAIL_NO_PATHS)
    later = base + [
        {"date": "2026-08-03", "result": "fail", "ts": "2026-08-03T03:00:00+00:00",
         "first_fail": {"gate": "python",
                        "detail": _GUARD_DETAIL_NO_PATHS + "and again\n"}},
    ]
    a = strategist.propose_persistent_red_triage(
        board=None, out_dir=out_dir, gate_records=base,
        min_reds=2, window=3, do_ingest=False,
    )
    b = strategist.propose_persistent_red_triage(
        board=None, out_dir=out_dir, gate_records=later,
        min_reds=2, window=3, do_ingest=False,
    )
    assert a["key"] == b["key"]
    lever_a = strategist._persistent_red_triage_lever(
        vm.derive_persistent_red_triage(base, min_reds=2, window=3)
    )
    lever_b = strategist._persistent_red_triage_lever(
        vm.derive_persistent_red_triage(later, min_reds=2, window=3)
    )
    assert lever_a.rationale == lever_b.rationale  # byte-stable body -> dedup


def test_non_test_red_still_files_the_normal_triage_card(board_home):
    """AC-2: a tsc red with a legitimately empty file set keeps the ORIGINAL
    behaviour — normal triage card, no anomaly escalation."""
    tsc_detail = (
        "Frontend tsc --noEmit:\n"
        "src/control/Panel.tsx(41,7): error TS2345: bad argument\n"
    )
    records = [
        {"date": "2026-08-01", "result": "fail", "ts": "2026-08-01T03:00:00+00:00",
         "first_fail": {"gate": "tsc", "detail": tsc_detail}},
        {"date": "2026-08-02", "result": "fail", "ts": "2026-08-02T03:00:00+00:00",
         "first_fail": {"gate": "tsc", "detail": tsc_detail}},
    ]
    result = strategist.propose_persistent_red_triage(
        board=None, out_dir=board_home / "specs", gate_records=records,
        min_reds=2, window=3, do_ingest=False,
    )
    assert result["triggered"] is True
    assert result["red_files_source"] == "non-test-red"
    assert "extraction_anomaly" not in result
    assert "EXTRACTION-ANOMALY" not in result["key"]

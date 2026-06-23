"""Tests for the Vision-Flywheel Phase 2 Strategist harness (I1).

All mocked — no real LLM call, no real usage API, no real ingest side effect
beyond a temp file board. The PlanSpec quality judge is disabled by the autouse
``_disable_spec_judge_by_default`` conftest fixture (HERMES_PLANSPEC_JUDGE=0).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs
from hermes_cli import strategist
from hermes_cli import strategist_surface


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
            "grounding": f"git log und grep belegen Luecke {i}",
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
def _cost_ctx(profiles):
    return {"metrics": None, "cost": {"profiles": profiles},
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
                                metrics={"autonomy_pct": 95, "green_gate_streak": 10},
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


# --------------------------------------------------------------------------- #
# STRATEGIST-SELF-GROUNDING-S1 — grounding presence-gate on the DRAFT path only
# --------------------------------------------------------------------------- #
def _grounded_draft(key="GROUNDED-1", grounding="git log zeigt kein vorhandenes Ziel; grep in hermes_cli findet keine Implementierung"):
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
    evidence = "git log zeigt kein vorhandenes Ziel; grep in hermes_cli findet keine Implementierung"
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

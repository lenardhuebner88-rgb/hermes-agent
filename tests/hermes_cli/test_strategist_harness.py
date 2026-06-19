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
from hermes_cli import strategist
from hermes_cli import strategist_surface


@pytest.fixture
def board_home(tmp_path, monkeypatch):
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
    # feed six passing drafts through the --drafts-file seam
    drafts = [
        {
            "key": f"DRAFT-{i}",
            "title": f"Lever number {i}",
            "lane": "coder-claude",
            "target_metric": f"metric {i} up",
            "roi": "positive",
            "counter_metric": f"guardrail {i} held",
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

#!/usr/bin/env python3
"""Tests for Sprint A1: the Autoresearch proposal store + apply-by-id flow.

Covers the One-Click contract: generate persists previewable proposals (store
roundtrip); apply is confirm-gated and reversible (backup → write → eval-gate →
keep or auto-revert); skip closes a proposal; code-mode apply runs the A3
test-suite gate (keep on green, auto-revert on red/crash); the FastAPI routes
wire it all together.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli import autoresearch_proposals as proposals  # noqa: E402
from hermes_cli.autoresearch_view import register_autoresearch_routes  # noqa: E402


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a skills tree + audit dir under tmp."""
    home = tmp_path / "hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(home / "skill-audit"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(home / "skill-audit" / "runner-state"))
    monkeypatch.setattr(proposals, "draft_section", lambda *_args, **_kwargs: {
        "ok": False, "reason": "offline test fallback",
    })
    (home / "config.yaml").write_text("model: MiniMax-M2.7\n", encoding="utf-8")
    return home


def _write_skill(skills_root: Path, name: str, body: str) -> Path:
    d = skills_root / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Generate + store roundtrip
# ---------------------------------------------------------------------------
def test_generate_creates_proposals_for_thin_skill(tmp_home):
    _write_skill(tmp_home / "skills", "alpha", "# Alpha\n\nNo recommended sections here.\n")
    res = proposals.generate_proposals()
    assert res["ok"] is True
    assert res["created_count"] >= 1
    # Store roundtrip: each created id loads back with the full contract.
    for pid in res["created"]:
        p = proposals.load_proposal(pid)
        assert p is not None
        assert p["schema"] == proposals.PROPOSAL_SCHEMA
        assert p["status"] == "proposed"
        assert p["mode"] == "skill"
        assert p["rationale_plain"]
        assert p["diff_before_after"]
        assert p["new_text"]
        assert p["writer"] == "scaffold"


def test_generate_is_idempotent(tmp_home):
    _write_skill(tmp_home / "skills", "beta", "# Beta\n\nThin.\n")
    first = proposals.generate_proposals()
    assert first["created_count"] >= 1
    second = proposals.generate_proposals()
    assert second["created_count"] == 0
    assert second["skipped_existing"] >= first["created_count"]


def test_payload_drops_bulky_fields_and_counts_open(tmp_home):
    _write_skill(tmp_home / "skills", "gamma", "# Gamma\n\nThin.\n")
    proposals.generate_proposals()
    payload = proposals.proposals_payload()
    assert payload["schema"] == "autoresearch-proposals-v1"
    assert payload["open_count"] == payload["count"] >= 1
    card = payload["proposals"][0]
    assert "diff_before_after" in card
    assert "before_text" not in card  # bulky field stays server-side
    assert "after_text" not in card

def _store_minimal_proposal(pid: str, *, status: str = "proposed", last_outcome=None, result=None):
    proposals.save_proposal({
        "id": pid,
        "schema": proposals.PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": "skill",
        "target_path": "/tmp/skill/SKILL.md",
        "section": "Output",
        "eval_label": "Output / Ergebnis",
        "title": pid,
        "rationale_plain": "test",
        "before_text": "",
        "after_text": "",
        "new_text": "",
        "diff_before_after": "",
        "status": status,
        "last_outcome": last_outcome,
        "created_at": "2026-05-29T00:00:00Z",
        "applied_at": None,
        "result": result,
    })


def test_payload_counts_only_actionable_open_and_reports_status_split(tmp_home):
    for i in range(3):
        _store_minimal_proposal(f"fresh-{i}")
    for i in range(4):
        _store_minimal_proposal(f"reverted-{i}", last_outcome="reverted_no_improvement")
    _store_minimal_proposal("testing", status="testing")
    _store_minimal_proposal("applied", status="applied", last_outcome="applied")
    _store_minimal_proposal("skipped", status="skipped")

    payload = proposals.proposals_payload()

    assert payload["open_count"] == 3
    assert payload["reverted_count"] == 4
    assert payload["testing_count"] == 1
    assert payload["applied_count"] == 1
    assert payload["skipped_count"] == 1
    reverted_cards = [p for p in payload["proposals"] if p["id"].startswith("reverted-")]
    assert all(p["last_outcome"] == "reverted_no_improvement" for p in reverted_cards)


def test_backfill_last_outcome_supports_dry_run_backup_and_idempotency(tmp_home):
    _store_minimal_proposal("fresh", result="noch offen")
    _store_minimal_proposal("reverted", result="↩ zurückgerollt — keine Verbesserung: rot")
    _store_minimal_proposal("applied", status="applied", result="✓ übernommen")

    dry = proposals.backfill_last_outcome(dry_run=True)
    assert dry["would_update"] == 2
    assert proposals.load_proposal("reverted").get("last_outcome") is None

    live = proposals.backfill_last_outcome(dry_run=False)
    assert live["updated"] == 2
    assert live["backup_dir"]
    assert Path(live["backup_dir"]).exists()
    assert proposals.load_proposal("fresh").get("last_outcome") is None
    assert proposals.load_proposal("reverted")["last_outcome"] == "reverted_no_improvement"
    assert proposals.load_proposal("applied")["last_outcome"] == "applied"

    again = proposals.backfill_last_outcome(dry_run=False)
    assert again["updated"] == 0

def _store_minimal_proposal(pid: str, *, status: str = "proposed", last_outcome=None, result=None):
    proposals.save_proposal({
        "id": pid,
        "schema": proposals.PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": "skill",
        "target_path": "/tmp/skill/SKILL.md",
        "section": "Output",
        "eval_label": "Output / Ergebnis",
        "title": pid,
        "rationale_plain": "test",
        "before_text": "",
        "after_text": "",
        "new_text": "",
        "diff_before_after": "",
        "status": status,
        "last_outcome": last_outcome,
        "created_at": "2026-05-29T00:00:00Z",
        "applied_at": None,
        "result": result,
    })


def test_payload_counts_only_actionable_open_and_reports_status_split(tmp_home):
    for i in range(3):
        _store_minimal_proposal(f"fresh-{i}")
    for i in range(4):
        _store_minimal_proposal(f"reverted-{i}", last_outcome="reverted_no_improvement")
    _store_minimal_proposal("testing", status="testing")
    _store_minimal_proposal("applied", status="applied", last_outcome="applied")
    _store_minimal_proposal("skipped", status="skipped")

    payload = proposals.proposals_payload()

    assert payload["open_count"] == 3
    assert payload["reverted_count"] == 4
    assert payload["testing_count"] == 1
    assert payload["applied_count"] == 1
    assert payload["skipped_count"] == 1
    reverted_cards = [p for p in payload["proposals"] if p["id"].startswith("reverted-")]
    assert all(p["last_outcome"] == "reverted_no_improvement" for p in reverted_cards)


def test_backfill_last_outcome_supports_dry_run_backup_and_idempotency(tmp_home):
    _store_minimal_proposal("fresh", result="noch offen")
    _store_minimal_proposal("reverted", result="\u21a9 zur\u00fcckgerollt \u2014 keine Verbesserung: rot")
    _store_minimal_proposal("applied", status="applied", result="\u2713 \u00fcbernommen")

    dry = proposals.backfill_last_outcome(dry_run=True)
    assert dry["would_update"] == 2
    assert proposals.load_proposal("reverted").get("last_outcome") is None

    live = proposals.backfill_last_outcome(dry_run=False)
    assert live["updated"] == 2
    assert live["backup_dir"]
    assert Path(live["backup_dir"]).exists()
    assert proposals.load_proposal("fresh").get("last_outcome") is None
    assert proposals.load_proposal("reverted")["last_outcome"] == "reverted_no_improvement"
    assert proposals.load_proposal("applied")["last_outcome"] == "applied"

    again = proposals.backfill_last_outcome(dry_run=False)
    assert again["updated"] == 0


# ---------------------------------------------------------------------------
# Apply: keep, revert, confirm-gate, idempotency
# ---------------------------------------------------------------------------
def test_apply_keeps_and_mutates_with_backup(tmp_home):
    skill = _write_skill(tmp_home / "skills", "delta", "# Delta\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    pid = proposals.generate_proposals()["created"][0]
    res = proposals.apply_proposal(pid, confirm=True)
    assert res["ok"] is True
    assert res["status"] == "applied"
    text = skill.read_text(encoding="utf-8")
    assert text != before
    assert "autoresearch-scaffold" in text
    stored = proposals.load_proposal(pid)
    assert stored["status"] == "applied"
    assert stored["result"].startswith("✓")
    assert Path(stored["backup_dir"]).exists()


def test_generate_uses_minimax_draft_when_valid(tmp_home, monkeypatch):
    skill = _write_skill(tmp_home / "skills", "mu", "# Mu\n\nThin.\n")

    def _draft(_skill, header, _text, **_kwargs):
        return {
            "ok": True,
            "text": (
                f"\n## {header}\n\n"
                "Use this when the Mu skill needs a concrete trigger for operator review.\n"
            ),
            "rationale": "drafted in test",
        }

    monkeypatch.setattr(proposals, "draft_section", _draft)
    pid = proposals.generate_proposals()["created"][0]
    stored = proposals.load_proposal(pid)
    assert stored["new_text"].startswith(f"\n## {stored['section']}\n\n")
    assert stored["writer"] == "minimax"
    assert "concrete trigger" in stored["diff_before_after"]
    res = proposals.apply_proposal(pid, confirm=True)
    assert res["status"] == "applied"
    assert "concrete trigger" in skill.read_text(encoding="utf-8")


def test_generate_falls_back_when_writer_returns_invalid(tmp_home, monkeypatch):
    _write_skill(tmp_home / "skills", "nu", "# Nu\n\nThin.\n")
    monkeypatch.setattr(proposals, "draft_section", lambda *_args, **_kwargs: {
        "ok": False,
        "reason": "missing expected section header",
    })
    pid = proposals.generate_proposals()["created"][0]
    stored = proposals.load_proposal(pid)
    assert stored["writer"] == "scaffold"
    assert "autoresearch-scaffold" in stored["new_text"]


def test_apply_requires_confirm(tmp_home):
    skill = _write_skill(tmp_home / "skills", "epsilon", "# Epsilon\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    pid = proposals.generate_proposals()["created"][0]
    res = proposals.apply_proposal(pid, confirm=False)
    assert res["ok"] is False
    assert "confirm" in res["detail"]
    assert skill.read_text(encoding="utf-8") == before  # untouched


def test_apply_reverts_when_no_improvement(tmp_home):
    """A proposal whose block doesn't resolve its target warning is rolled back,
    the file restored, and the proposal stays open."""
    skill = _write_skill(tmp_home / "skills", "zeta", "# Zeta\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    # Hand-craft a proposal that claims to add 'Output' but its block doesn't.
    bad = {
        "id": "zeta-output",
        "schema": proposals.PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": "zeta",
        "target_path": str(skill),
        "section": "Output",
        "eval_label": "Output / Ergebnis",
        "title": "Abschnitt „Output“ zu zeta hinzufügen",
        "rationale_plain": "test",
        "before_text": before,
        "after_text": before + "\n<!-- placeholder qqq -->\n",
        # NB: must not contain any section needle (would falsely satisfy the gate).
        "new_text": "\n<!-- placeholder qqq -->\n",
        "diff_before_after": "x",
        "status": "proposed",
        "created_at": "2026-05-29T00:00:00Z",
        "applied_at": None,
        "result": None,
    }
    proposals.save_proposal(bad)
    res = proposals.apply_proposal("zeta-output", confirm=True)
    assert res["ok"] is False
    assert res.get("reverted") is True
    assert skill.read_text(encoding="utf-8") == before  # restored from backup
    assert proposals.load_proposal("zeta-output")["status"] == "proposed"  # still open


def test_apply_unknown_id(tmp_home):
    res = proposals.apply_proposal("does-not-exist", confirm=True)
    assert res["ok"] is False
    assert "no such proposal" in res["detail"]


def test_apply_already_applied_is_noop(tmp_home):
    _write_skill(tmp_home / "skills", "eta", "# Eta\n\nThin.\n")
    pid = proposals.generate_proposals()["created"][0]
    assert proposals.apply_proposal(pid, confirm=True)["ok"] is True
    again = proposals.apply_proposal(pid, confirm=True)
    assert again["ok"] is False
    assert again["status"] == "applied"


def test_multiple_section_proposals_same_file_compose(tmp_home):
    """Applying two section-proposals for the same skill must not clobber each
    other (append-to-current, not stale-snapshot)."""
    skill = _write_skill(tmp_home / "skills", "theta", "# Theta\n\nThin.\n")
    created = proposals.generate_proposals()["created"]
    applied = 0
    for pid in created:
        if proposals.apply_proposal(pid, confirm=True)["ok"]:
            applied += 1
    assert applied >= 2
    text = skill.read_text(encoding="utf-8")
    # Both kept sections survive in the final file.
    assert text.count("autoresearch-scaffold") >= 2


# ---------------------------------------------------------------------------
# Skip
# ---------------------------------------------------------------------------
def test_skip_closes_proposal(tmp_home):
    skill = _write_skill(tmp_home / "skills", "iota", "# Iota\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    pid = proposals.generate_proposals()["created"][0]
    res = proposals.skip_proposal(pid)
    assert res["ok"] is True
    assert proposals.load_proposal(pid)["status"] == "skipped"
    assert skill.read_text(encoding="utf-8") == before  # skip never mutates
    # A skipped proposal is no longer actionable.
    assert proposals.apply_proposal(pid, confirm=True)["ok"] is False


# ---------------------------------------------------------------------------
# A3: code-mode test-suite gate + minimal generator
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    """A throwaway repo root so code-proposal apply never touches the real tree."""
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    monkeypatch.setattr(proposals, "_REPO", repo)
    return repo


def test_build_code_proposal_roundtrip(tmp_home, tmp_repo):
    target = tmp_repo / "agent" / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "x = 2\n", title="bump x", rationale="weil")
    assert p["mode"] == "code"
    assert p["status"] == "proposed"
    assert p["after_text"] == "x = 2\n"
    assert p["target"] == "agent/foo.py"
    assert "x = 2" in p["diff_before_after"]
    stored = proposals.load_proposal(p["id"])
    assert stored["after_text"] == "x = 2\n"
    # Bulky before/after stay server-side in the list payload.
    proposals.proposals_payload()  # must not raise with a gate-bearing proposal


def test_code_apply_writes_live_and_marks_testing(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "agent" / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "x = 2\n", title="bump", rationale="r")
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)  # no real worker
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is True
    assert res["status"] == "testing"
    assert target.read_text(encoding="utf-8") == "x = 2\n"  # written live, pending the gate
    stored = proposals.load_proposal(p["id"])
    assert stored["status"] == "testing"
    assert stored["gate"]["phase"] == "running"
    assert stored["gate"]["pid"] == 4242


def test_code_gate_keeps_on_green(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "agent" / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "x = 2\n", title="bump", rationale="r")
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)
    proposals.apply_proposal(p["id"], confirm=True)
    fin = proposals.finalize_code_gate(p["id"], run_suite=lambda log: (0, "==== 3 passed ===="))
    assert fin["ok"] is True
    assert fin["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "x = 2\n"  # kept
    stored = proposals.load_proposal(p["id"])
    assert stored["status"] == "applied"
    assert stored["gate"]["phase"] == "passed"
    assert "passed" in stored["result"]


def test_code_gate_reverts_on_red(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "agent" / "bar.py"
    target.write_text("ok = True\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "ok = False\n", title="break", rationale="r")
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)
    proposals.apply_proposal(p["id"], confirm=True)
    assert target.read_text(encoding="utf-8") == "ok = False\n"  # written before the gate
    fin = proposals.finalize_code_gate(p["id"], run_suite=lambda log: (1, "==== 1 failed ===="))
    assert fin["ok"] is False
    assert fin.get("reverted") is True
    assert target.read_text(encoding="utf-8") == "ok = True\n"  # restored from backup
    stored = proposals.load_proposal(p["id"])
    assert stored["status"] == "proposed"  # reopened for retry/skip
    assert stored["gate"]["phase"] == "failed"


def test_code_apply_outside_repo_refused(tmp_home, tmp_repo):
    outside = tmp_home / "skills" / "x.py"  # under HERMES_HOME, not the repo
    outside.write_text("y = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(outside, "y = 2\n", title="t", rationale="r")
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is False
    assert "inside the repo" in res["detail"]
    assert outside.read_text(encoding="utf-8") == "y = 1\n"  # untouched


def test_code_apply_self_protect_refused(tmp_home, tmp_repo):
    target = tmp_repo / "scripts" / "run_tests.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("echo hi\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "echo pwned\n", title="t", rationale="r")
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is False
    assert "gate" in res["detail"].lower()
    assert target.read_text(encoding="utf-8") == "echo hi\n"  # untouched


def test_testing_gate_crash_is_reconciled(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "agent" / "baz.py"
    target.write_text("a = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "a = 2\n", title="t", rationale="r")
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)
    proposals.apply_proposal(p["id"], confirm=True)
    assert proposals.load_proposal(p["id"])["status"] == "testing"
    # Worker is gone without a verdict → the read path auto-reverts it.
    monkeypatch.setattr(proposals, "_pid_alive", lambda pid: False)
    proposals.list_proposals()
    stored = proposals.load_proposal(p["id"])
    assert stored["status"] == "proposed"
    assert stored["gate"]["phase"] == "crashed"
    assert target.read_text(encoding="utf-8") == "a = 1\n"  # restored


def test_route_code_apply_returns_testing(client, tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "agent" / "route.py"
    target.write_text("v = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "v = 2\n", title="t", rationale="r")
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)
    r = client.post("/autoresearch/apply", json={"id": p["id"], "confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "testing"


# ---------------------------------------------------------------------------
# Route layer (FastAPI)
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_home):
    app = FastAPI()
    register_autoresearch_routes(app)
    return TestClient(app)


def test_routes_generate_list_apply_skip(client, tmp_home):
    _write_skill(tmp_home / "skills", "lambda", "# Lambda\n\nThin.\n")

    gen = client.post("/autoresearch/generate")
    assert gen.status_code == 200
    assert gen.json()["created_count"] >= 1

    lst = client.get("/autoresearch/proposals")
    assert lst.status_code == 200
    body = lst.json()
    assert body["count"] >= 1
    ids = [p["id"] for p in body["proposals"]]

    ap = client.post("/autoresearch/apply", json={"id": ids[0], "confirm": True})
    assert ap.status_code == 200
    assert ap.json()["status"] == "applied"

    if len(ids) > 1:
        sk = client.post("/autoresearch/skip", json={"id": ids[1]})
        assert sk.status_code == 200
        assert sk.json()["status"] == "skipped"


def test_route_apply_unknown_returns_ok_false(client, tmp_home):
    r = client.post("/autoresearch/apply", json={"id": "nope", "confirm": True})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# AR2: relevance ranking (deterministic; cap; criticality; usage; "why first")
# ---------------------------------------------------------------------------
def _cand(skill, label, n_missing=1, cid=None):
    return {
        "skill": skill, "label": label, "n_missing": n_missing,
        "id": cid if cid is not None else f"{skill}-{label}",
    }


def test_rank_candidates_criticality_safety_before_output():
    """At otherwise-equal score, Safety outranks Output (and all get annotated)."""
    ranked = proposals.rank_candidates([
        _cand("s", "Output / Ergebnis"),
        _cand("s", "Safety / Sicherheit"),
    ])
    assert [c["label"] for c in ranked][0] == "Safety / Sicherheit"
    assert all("rank_score" in c and "rank_reason" in c for c in ranked)


def test_rank_candidates_deterministic_regardless_of_input_order_and_cap():
    cands = [_cand(f"skill{i}", "Procedure / Vorgehen", n_missing=2) for i in range(5)]
    r1 = proposals.rank_candidates(cands, limit=3)
    r2 = proposals.rank_candidates(list(reversed(cands)), limit=3)
    assert len(r1) == 3
    assert [c["skill"] for c in r1] == [c["skill"] for c in r2]


def test_rank_candidates_excludes_decided():
    ranked = proposals.rank_candidates(
        [_cand("a", "Safety / Sicherheit", cid="a-safety"),
         _cand("b", "Safety / Sicherheit", cid="b-safety")],
        exclude_ids={"a-safety"},
    )
    assert [c["id"] for c in ranked] == ["b-safety"]


def test_rank_candidates_usage_signal_promotes_frequent_skill():
    cands = [_cand("rare", "Output / Ergebnis"), _cand("hot", "Output / Ergebnis")]
    with_usage = proposals.rank_candidates(cands, usage={"hot": 300.0})
    assert with_usage[0]["skill"] == "hot"
    assert "genutzt" in with_usage[0]["rank_reason"]


def test_rank_reason_explains_completeness():
    ranked = proposals.rank_candidates([_cand("x", "Procedure / Vorgehen", n_missing=1)])
    assert "nur dieser Abschnitt fehlt" in ranked[0]["rank_reason"]


def test_generate_caps_run_and_only_drafts_top_n(tmp_home, monkeypatch):
    skills = tmp_home / "skills"
    for i in range(6):
        _write_skill(skills, f"skill{i}", f"# Skill{i}\n\nthin\n")
    calls = {"n": 0}

    def counting_writer(*_a, **_k):
        calls["n"] += 1
        return {"ok": False, "reason": "test"}

    monkeypatch.setattr(proposals, "draft_section", counting_writer)
    res = proposals.generate_proposals(limit=3)

    assert res["created_count"] <= 3
    # The model writer runs only for the capped Top-N, not for every candidate.
    assert calls["n"] == res["created_count"]
    assert res["candidates_seen"] > res["created_count"]
    # Each drafted card leads with its "why first" and persists the rank fields.
    for pid in res["created"]:
        p = proposals.load_proposal(pid)
        assert p["rationale_plain"].startswith("Priorität:")
        assert p.get("rank_reason")
        assert p.get("rank_score") is not None


def test_generate_payload_orders_proposed_by_rank_score(tmp_home, monkeypatch):
    skills = tmp_home / "skills"
    for i in range(4):
        _write_skill(skills, f"sk{i}", f"# Sk{i}\n\nthin\n")
    monkeypatch.setattr(proposals, "draft_section", lambda *_a, **_k: {"ok": False, "reason": "t"})
    proposals.generate_proposals(limit=5)
    payload = proposals.proposals_payload()
    proposed = [c for c in payload["proposals"] if c["status"] == "proposed"]
    scores = [c.get("rank_score") or 0.0 for c in proposed]
    assert scores == sorted(scores, reverse=True)

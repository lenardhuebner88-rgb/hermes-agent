#!/usr/bin/env python3
"""Tests for Sprint A1: the Autoresearch proposal store + apply-by-id flow.

Covers the One-Click contract: generate persists previewable proposals (store
roundtrip); apply is confirm-gated and reversible (backup → write → eval-gate →
keep or auto-revert); skip closes a proposal; code-mode apply is refused until
the A3 test-suite gate; the FastAPI routes wire it all together.
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
# Code-mode is gated to A3
# ---------------------------------------------------------------------------
def test_code_mode_apply_is_gated(tmp_home):
    skill = _write_skill(tmp_home / "skills", "kappa", "# Kappa\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    code = {
        "id": "kappa-code",
        "schema": proposals.PROPOSAL_SCHEMA,
        "mode": "code",
        "target": "kappa",
        "target_path": str(skill),
        "section": "n/a",
        "eval_label": "Output / Ergebnis",
        "title": "Code change",
        "rationale_plain": "test",
        "before_text": before, "after_text": before, "new_text": "x",
        "diff_before_after": "x", "status": "proposed",
        "created_at": "2026-05-29T00:00:00Z", "applied_at": None, "result": None,
    }
    proposals.save_proposal(code)
    res = proposals.apply_proposal("kappa-code", confirm=True)
    assert res["ok"] is False
    assert res.get("gated")
    assert skill.read_text(encoding="utf-8") == before  # nothing written
    assert proposals.load_proposal("kappa-code")["status"] == "proposed"


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

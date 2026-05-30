#!/usr/bin/env python3
"""Tests for Sprint A1: the Autoresearch proposal store + apply-by-id flow.

Covers the One-Click contract: generate persists previewable proposals (store
roundtrip); apply is confirm-gated and reversible (backup → write → eval-gate →
keep or auto-revert); skip closes a proposal; code-mode apply runs the A3
test-suite gate (keep on green, auto-revert on red/crash); the FastAPI routes
wire it all together.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli import autoresearch_proposals as proposals  # noqa: E402
from hermes_cli import autoresearch_view as view  # noqa: E402
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
    monkeypatch.setattr(proposals.capability_researcher, "research_skills", _fake_research_skills)
    (home / "config.yaml").write_text("model: MiniMax-M2.7\n", encoding="utf-8")
    return home


@pytest.fixture()
def scaffold_enabled(monkeypatch):
    """Opt into the legacy section-scaffold generate path. It is off by default
    now (signed commitment "kein Schein") so these tests must enable it to
    exercise the scaffold writer / generate behaviour they assert."""
    monkeypatch.setattr(proposals._runner(), "_ENABLE_SECTION_SCAFFOLD_DISCOVERY", True)


def _fake_research_skills(skills, *, usage=None, limit=None, **_kwargs):
    skills = list(skills)
    usage = usage or {}
    findings = []
    for skill, text in skills:
        evidence = next((line.strip() for line in text.splitlines() if line.strip()), skill)
        use_count = float(usage.get(skill, 0.0))
        findings.append({
            "skill": skill,
            "category": "unclear_trigger",
            "evidence": evidence,
            "problem": f"`{skill}` has no concrete activation trigger.",
            "fix_hint": "Add a specific when-to-use trigger tied to a real workflow.",
            "rank_score": 6.0 + min(use_count / 50.0, 3.0),
            "rank_reason": f"genutzt ({int(use_count)}x)",
        })
    findings.sort(key=lambda f: (-float(f["rank_score"]), f["skill"]))
    if limit is not None:
        findings = findings[: max(1, int(limit))]
    return {
        "ok": True,
        "findings": findings,
        "skills_seen": len(skills),
        "skills_with_findings": len({f["skill"] for f in findings}),
        "dropped": 0,
        "errors": 0,
    }


def _write_skill(skills_root: Path, name: str, body: str, *, use_count: int = 5) -> Path:
    d = skills_root / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    usage_path = skills_root / ".usage.json"
    try:
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        usage = {}
    usage[name] = {"use_count": use_count}
    usage_path.write_text(json.dumps(usage), encoding="utf-8")
    return path


def _store_scaffold_proposal(
    skills_root: Path,
    name: str,
    body: str,
    *,
    label: str = "When to Use / Wann verwenden",
) -> tuple[Path, str]:
    path = _write_skill(skills_root, name, body)
    runner = proposals._runner()
    proposal = proposals._build_proposal_for_candidate({
        "path": path,
        "skill": name,
        "label": label,
        "n_missing": 1,
        "rank_score": 1.0,
        "rank_reason": "test",
    }, runner)
    proposals.save_proposal(proposal)
    return path, proposal["id"]


# ---------------------------------------------------------------------------
# Generate + store roundtrip
# ---------------------------------------------------------------------------
def test_generate_creates_proposals_for_thin_skill(tmp_home, scaffold_enabled):
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


def test_generate_is_idempotent(tmp_home, scaffold_enabled):
    _write_skill(tmp_home / "skills", "beta", "# Beta\n\nThin.\n")
    first = proposals.generate_proposals()
    assert first["created_count"] >= 1
    second = proposals.generate_proposals()
    assert second["created_count"] == 0
    assert second["skipped_existing"] >= first["created_count"]


def test_payload_drops_bulky_fields_and_counts_open(tmp_home, scaffold_enabled):
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
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "delta", "# Delta\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
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
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "mu", "# Mu\n\nThin.\n")
    stored = proposals.load_proposal(pid)
    assert stored["new_text"].startswith(f"\n## {stored['section']}\n\n")
    assert stored["writer"] == "minimax"
    assert "concrete trigger" in stored["diff_before_after"]
    res = proposals.apply_proposal(pid, confirm=True)
    assert res["status"] == "applied"
    assert "concrete trigger" in skill.read_text(encoding="utf-8")


def test_generate_falls_back_when_writer_returns_invalid(tmp_home, monkeypatch):
    monkeypatch.setattr(proposals, "draft_section", lambda *_args, **_kwargs: {
        "ok": False,
        "reason": "missing expected section header",
    })
    _skill, pid = _store_scaffold_proposal(tmp_home / "skills", "nu", "# Nu\n\nThin.\n")
    stored = proposals.load_proposal(pid)
    assert stored["writer"] == "scaffold"
    assert "autoresearch-scaffold" in stored["new_text"]


def test_apply_requires_confirm(tmp_home):
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "epsilon", "# Epsilon\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
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
    _skill, pid = _store_scaffold_proposal(tmp_home / "skills", "eta", "# Eta\n\nThin.\n")
    assert proposals.apply_proposal(pid, confirm=True)["ok"] is True
    again = proposals.apply_proposal(pid, confirm=True)
    assert again["ok"] is False
    assert again["status"] == "applied"


def test_multiple_section_proposals_same_file_compose(tmp_home):
    """Applying two section-proposals for the same skill must not clobber each
    other (append-to-current, not stale-snapshot)."""
    skill, first = _store_scaffold_proposal(
        tmp_home / "skills", "theta", "# Theta\n\nThin.\n",
        label="When to Use / Wann verwenden",
    )
    _same_skill, second = _store_scaffold_proposal(
        tmp_home / "skills", "theta", "# Theta\n\nThin.\n",
        label="Safety / Sicherheit",
    )
    created = [first, second]
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
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "iota", "# Iota\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
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
    return _RouteClient(app)


class _RouteClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def get(self, path: str):
        return asyncio.run(self._request("GET", path))

    def post(self, path: str, *, json=None):
        return asyncio.run(self._request("POST", path, json=json))

    async def _request(self, method: str, path: str, *, json=None):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            return await ac.request(method, path, json=json)


def test_routes_generate_list_apply_skip(client, tmp_home, scaffold_enabled):
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
    assert ap.json()["ok"] is True
    assert ap.json()["status"] == "applied"

    sk = client.post("/autoresearch/skip", json={"id": ids[0]})
    assert sk.status_code == 200
    assert sk.json()["ok"] is False


def test_route_generate_code_weaknesses_creates_code_proposal(client, tmp_home, monkeypatch):
    target = _REPO / "hermes_cli" / "model_normalize.py"
    old_snippet = (
        '    "trinity": "arcee-ai",\n'
        '    "nemotron": "nvidia",\n'
        '    "llama": "meta-llama",\n'
        '    "step": "stepfun",\n'
        '    "trinity": "arcee-ai",'
    )
    new_snippet = (
        '    "trinity": "arcee-ai",\n'
        '    "nemotron": "nvidia",\n'
        '    "llama": "meta-llama",\n'
        '    "step": "stepfun",'
    )
    assert old_snippet in target.read_text(encoding="utf-8")

    monkeypatch.setattr(proposals, "_iter_code_allowlist_paths", lambda: [target])
    monkeypatch.setattr(proposals, "_call_code_weakness_finder", lambda *_args, **_kwargs: {
        "ok": True,
        "raw": {
            "finding": {
                "category": "dead_logic",
                "title": "Doppelter Vendor-Key",
                "problem": "Der zweite identische Key ist tote Logik und verdeckt die erste Zuordnung.",
                "evidence_quote": '"trinity": "arcee-ai",',
                "old_snippet": old_snippet,
                "new_snippet": new_snippet,
                "fix_hint": "Entferne den doppelten Mapping-Eintrag.",
            }
        },
    })

    gen = client.post("/autoresearch/generate-code-weaknesses")
    assert gen.status_code == 200
    body = gen.json()
    assert body["created_count"] == 1

    stored = proposals.load_proposal(body["created"][0])
    assert stored["mode"] == "code"
    assert stored["proposal_type"] == "code_weakness"
    assert stored["target"] == "hermes_cli/model_normalize.py"
    assert stored["diff_before_after"]


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


def test_generate_caps_run_and_only_drafts_top_n(tmp_home, monkeypatch, scaffold_enabled):
    skills = tmp_home / "skills"
    for i in range(6):
        _write_skill(skills, f"skill{i}", f"# Skill{i}\n\nthin\n")

    res = proposals.generate_proposals(limit=3)

    assert res["created_count"] == 3
    assert res["candidates_seen"] > res["created_count"]
    # Each drafted card leads with its "why first" and persists the rank fields.
    for pid in res["created"]:
        p = proposals.load_proposal(pid)
        assert p["rationale_plain"].startswith("Priorität:")
        assert p.get("rank_reason")
        assert p.get("rank_score") is not None
        assert p["mode"] == "skill"
        assert p["new_text"]


def test_generate_caps_after_excluding_existing_findings(tmp_home, scaffold_enabled):
    skills = tmp_home / "skills"
    for i in range(4):
        _write_skill(skills, f"dupe{i}", f"# Dupe{i}\n\nthin\n")

    first = proposals.generate_proposals(limit=2)
    assert first["created_count"] == 2
    second = proposals.generate_proposals(limit=2)
    assert second["created_count"] == 2
    assert second["skipped_existing"] == 2


def test_proposal_id_ignores_model_problem_prose():
    base = {
        "skill": "stable",
        "category": "unclear_trigger",
        "evidence": "Use this when X happens.",
        "problem": "first phrasing",
    }
    changed = dict(base, problem="second phrasing")
    assert proposals._proposal_id_for_finding(base) == proposals._proposal_id_for_finding(changed)


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


# ---------------------------------------------------------------------------
# Track B: code-finder allowlist enforcement on the apply path
#
# A ``code_weakness`` proposal must only ever touch a file inside
# ``_CODE_ALLOWLIST``. A target outside the allowlist (or in a denied path
# family like tests/migrations/web_dist) is refused at apply time with no
# write and the gate worker is never spawned. An allowlisted target follows
# the normal backup → write → test-suite gate contract: persist on green,
# auto-revert from backup on red.
# ---------------------------------------------------------------------------
def _no_gate(monkeypatch):
    """Make the gate worker fail loudly: a refused apply must never reach it."""
    def _raise(_pid):
        raise AssertionError("_spawn_code_gate must not be reached for a refused apply")
    monkeypatch.setattr(proposals, "_spawn_code_gate", _raise)


def test_code_weakness_apply_outside_allowlist_refused(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "hermes_cli" / "not_listed.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "x = 2\n", title="outside allowlist", rationale="r")
    p["proposal_type"] = "code_weakness"
    proposals.save_proposal(p)
    _no_gate(monkeypatch)
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is False
    assert res["status"] == "proposed"
    assert "allowlist" in res["detail"].lower() or "outside" in res["detail"].lower()
    assert target.read_text(encoding="utf-8") == "x = 1\n"  # no write outside the allowlist


def test_code_weakness_apply_denied_family_refused(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "tests" / "test_x.py"  # tests/ is a denied path family
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "x = 2\n", title="denied family", rationale="r")
    p["proposal_type"] = "code_weakness"
    proposals.save_proposal(p)
    _no_gate(monkeypatch)
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is False
    assert res["status"] == "proposed"
    assert "denied" in res["detail"].lower() or "allowlist" in res["detail"].lower()
    assert target.read_text(encoding="utf-8") == "x = 1\n"  # never written


def test_code_weakness_apply_allowlisted_persists_on_green(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "hermes_cli" / "capability_researcher.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "x = 2\n", title="allowlisted green", rationale="r")
    p["proposal_type"] = "code_weakness"
    proposals.save_proposal(p)
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)  # no real worker
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is True
    assert res["status"] == "testing"
    assert target.read_text(encoding="utf-8") == "x = 2\n"  # written live, pending the gate
    fin = proposals.finalize_code_gate(p["id"], run_suite=lambda log: (0, "==== 1 passed ===="))
    assert fin["ok"] is True
    assert fin["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "x = 2\n"  # persisted on green
    assert proposals.load_proposal(p["id"])["status"] == "applied"


def test_code_weakness_apply_allowlisted_auto_reverts_on_red(tmp_home, tmp_repo, monkeypatch):
    target = tmp_repo / "hermes_cli" / "model_normalize.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("good = 1\n", encoding="utf-8")
    p = proposals.build_code_proposal(target, "good = 0\n", title="allowlisted red", rationale="r")
    p["proposal_type"] = "code_weakness"
    proposals.save_proposal(p)
    monkeypatch.setattr(proposals, "_spawn_code_gate", lambda pid: 4242)
    res = proposals.apply_proposal(p["id"], confirm=True)
    assert res["ok"] is True
    assert res["status"] == "testing"
    assert target.read_text(encoding="utf-8") == "good = 0\n"  # written before the gate
    fin = proposals.finalize_code_gate(p["id"], run_suite=lambda log: (1, "==== 1 failed ===="))
    assert fin["ok"] is False
    assert fin.get("reverted") is True
    assert target.read_text(encoding="utf-8") == "good = 1\n"  # auto-reverted from backup
    assert proposals.load_proposal(p["id"])["status"] == "proposed"  # reopened


# ---------------------------------------------------------------------------
# Track B (real finder origin): allowlist enforcement on the GENERIC code path.
#
# Regression guard for the review blocker: the prior tests bolted on
# ``proposal_type = "code_weakness"`` by hand, which only ever exercised the
# code_weakness sub-branch. These tests instead build a *genuine* finder
# proposal via ``_build_code_weakness_proposal`` — which sets
# ``proposal_type="code_weakness"`` itself, with no manual help — and prove the
# allowlist is enforced on that real path: a non-allowlisted (or denied-family)
# target is refused with NO write and the gate worker is never spawned; an
# allowlisted target persists on green and auto-reverts from backup on red.
# ---------------------------------------------------------------------------
def _no_gate_stub(pid):
    """Stub for _spawn_code_gate that must never be reached on a refused apply."""
    raise AssertionError("_spawn_code_gate should not be called when proposal is refused")


def test_code_finder_proposal_outside_allowlist_refused_no_write(tmp_home, tmp_repo):
    """A real finder proposal on a non-allowlisted path is refused; no write."""
    target = tmp_repo / "hermes_cli" / "not_listed.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")

    finding = {
        "category": "bug_risk",
        "evidence": "x = 1",
        "after_text": "x = 2\n",
        "title": "t",
        "problem": "p",
        "fix_hint": "f",
        "old_snippet": "x = 1",
    }
    p = proposals._build_code_weakness_proposal(target, "x = 1\n", finding)
    proposals.save_proposal(p)

    # The finder builder sets this itself — the test does NOT touch proposal_type.
    assert p["proposal_type"] == "code_weakness"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proposals, "_spawn_code_gate", _no_gate_stub)
        res = proposals.apply_proposal(p["id"], confirm=True)

    assert res["ok"] is False
    assert res["status"] == "proposed"
    detail_lower = res["detail"].lower()
    assert "allowlist" in detail_lower or "outside" in detail_lower, f"detail={res['detail']}"
    assert target.read_text(encoding="utf-8") == "x = 1\n"  # no write outside the allowlist


def test_code_finder_denied_family_refused_no_write(tmp_home, tmp_repo):
    """A real finder proposal targeting a denied family (tests/) is refused; no write."""
    target = tmp_repo / "tests" / "test_x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("y = 10\n", encoding="utf-8")

    finding = {
        "category": "bug_risk",
        "evidence": "y = 10",
        "after_text": "y = 20\n",
        "title": "denied_family_test",
        "problem": "test in denied family",
        "fix_hint": "move out",
        "old_snippet": "y = 10",
    }
    p = proposals._build_code_weakness_proposal(target, "y = 10\n", finding)
    proposals.save_proposal(p)

    assert p["proposal_type"] == "code_weakness"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proposals, "_spawn_code_gate", _no_gate_stub)
        res = proposals.apply_proposal(p["id"], confirm=True)

    assert res["ok"] is False
    assert res["status"] == "proposed"
    detail_lower = res["detail"].lower()
    assert "denied" in detail_lower or "allowlist" in detail_lower, f"detail={res['detail']}"
    assert target.read_text(encoding="utf-8") == "y = 10\n"  # never written


def test_code_finder_allowlisted_persists_on_green(tmp_home, tmp_repo):
    """A real finder proposal on an allowlisted path persists on green."""
    target = tmp_repo / "hermes_cli" / "capability_researcher.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")

    finding = {
        "category": "bug_risk",
        "evidence": "unique_evidence_allowlisted_123",
        "after_text": "x = 2\n",
        "title": "allowlisted_green",
        "problem": "needs fix",
        "fix_hint": "change value",
        "old_snippet": "x = 1",
    }
    p = proposals._build_code_weakness_proposal(target, "x = 1\n", finding)
    proposals.save_proposal(p)
    pid = p["id"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proposals, "_spawn_code_gate", lambda _pid: 4242)  # no real worker
        res = proposals.apply_proposal(pid, confirm=True)

    assert res["ok"] is True
    assert res["status"] == "testing"
    assert target.read_text(encoding="utf-8") == "x = 2\n"  # written live, pending the gate

    fin = proposals.finalize_code_gate(pid, run_suite=lambda log: (0, "==== 1 passed ===="))
    assert fin["ok"] is True
    assert fin["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "x = 2\n"  # persisted on green
    assert proposals.load_proposal(pid)["status"] == "applied"


def test_code_finder_allowlisted_auto_reverts_on_red(tmp_home, tmp_repo):
    """A real finder proposal on an allowlisted path auto-reverts from backup on red."""
    target = tmp_repo / "hermes_cli" / "model_normalize.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("good = 1\n", encoding="utf-8")

    finding = {
        "category": "bug_risk",
        "evidence": "unique_evidence_allowlisted_456",
        "after_text": "good = 0\n",
        "title": "allowlisted_red",
        "problem": "bad change",
        "fix_hint": "revert",
        "old_snippet": "good = 1",
    }
    p = proposals._build_code_weakness_proposal(target, "good = 1\n", finding)
    proposals.save_proposal(p)
    pid = p["id"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proposals, "_spawn_code_gate", lambda _pid: 4242)
        res = proposals.apply_proposal(pid, confirm=True)

    assert res["ok"] is True
    assert res["status"] == "testing"
    assert target.read_text(encoding="utf-8") == "good = 0\n"  # written before the gate

    fin = proposals.finalize_code_gate(pid, run_suite=lambda log: (1, "==== 1 failed ===="))
    assert fin["ok"] is False
    assert fin.get("reverted") is True
    assert target.read_text(encoding="utf-8") == "good = 1\n"  # auto-reverted from backup
    assert proposals.load_proposal(pid)["status"] == "proposed"  # reopened


# ===========================================================================
# Track A (1): usage filter — skills with use_count < 5 are NOT researched,
# >= 5 are. The capability researcher only runs over above-threshold skills.
# ===========================================================================
def test_usage_filter_skips_low_use_count_skills(tmp_home):
    skills_root = tmp_home / "skills"
    # cold: below the _USAGE_MIN_USE_COUNT threshold; hot: at/above it.
    _write_skill(skills_root, "cold", "# Cold\n\nThin.\n", use_count=3)
    _write_skill(skills_root, "hot", "# Hot\n\nThin.\n", use_count=5)
    _write_skill(skills_root, "zero", "# Zero\n\nThin.\n", use_count=0)

    runner = proposals._runner()
    usage = proposals._load_skill_usage_from_root(skills_root)
    skills, path_by_skill, skipped = proposals._skills_for_capability_research(
        [skills_root], runner, usage
    )

    researched = {name for name, _text in skills}
    assert "hot" in researched              # use_count == 5 → researched
    assert "cold" not in researched         # use_count == 3 → skipped
    assert "zero" not in researched         # use_count == 0 → skipped
    assert skipped == 2
    assert "hot" in path_by_skill


def test_usage_filter_exactly_at_threshold_is_included(tmp_home):
    skills_root = tmp_home / "skills"
    _write_skill(skills_root, "edge", "# Edge\n\nThin.\n", use_count=proposals._USAGE_MIN_USE_COUNT)

    runner = proposals._runner()
    usage = proposals._load_skill_usage_from_root(skills_root)
    skills, _paths, skipped = proposals._skills_for_capability_research([skills_root], runner, usage)

    assert {name for name, _ in skills} == {"edge"}
    assert skipped == 0


def test_usage_loader_tolerates_missing_sidecar(tmp_home):
    # No .usage.json present → empty map, never a crash; every skill then reads
    # as below threshold and is skipped.
    skills_root = tmp_home / "skills2"
    skills_root.mkdir(parents=True)
    (skills_root / "lonely").mkdir()
    (skills_root / "lonely" / "SKILL.md").write_text("# Lonely\n", encoding="utf-8")

    usage = proposals._load_skill_usage_from_root(skills_root)
    assert usage == {}


# ===========================================================================
# Track A (2): finding → proposal carries category + verbatim evidence + fix_hint
# (the grounded AR3 fix pipeline, NOT the legacy flat scaffold path).
# ===========================================================================
def _capability_finding(skill, evidence, **over):
    base = {
        "skill": skill,
        "category": "unclear_trigger",
        "evidence": evidence,
        "problem": f"`{skill}` has no concrete activation trigger.",
        "fix_hint": "Add a specific when-to-use trigger tied to a real workflow.",
        "rank_reason": "fehlender Aktivierungs-Trigger",
        "rank_score": 7.0,
    }
    base.update(over)
    return base


def test_finding_proposal_carries_category_evidence_fix_hint(tmp_home, monkeypatch):
    skills_root = tmp_home / "skills"
    body = "# Gamma\n\n## When to Use\n\nUse gamma sometimes.\n"
    path = _write_skill(skills_root, "gamma", body, use_count=9)
    evidence = "Use gamma sometimes."
    finding = _capability_finding("gamma", evidence)

    # Mock the AR3 fix writer: a grounded fix that touches the evidence line.
    after = body.replace("Use gamma sometimes.",
                         "Use gamma when normalising the nightly inventory feed.")
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": True, "text": after, "rationale": "Made trigger concrete.", "reason": None,
    })

    proposal = proposals._build_proposal_for_finding(finding, path)
    assert proposal is not None
    # Verbatim evidence is carried through unchanged.
    assert proposal["evidence"] == evidence
    # Category preserved.
    assert proposal["category"] == "unclear_trigger"
    # fix_hint carried verbatim from the finding.
    assert proposal["fix_hint"] == finding["fix_hint"]
    # This is the grounded fix pipeline, not the legacy flat scaffold.
    assert proposal["proposal_type"] == "capability_research"
    assert proposal["writer"] == "minimax-ar3-fix-writer"
    assert proposal["after_text"] == after
    # No legacy flat-scaffold placeholder marker in the produced text.
    assert "autoresearch-scaffold" not in proposal["after_text"]


def test_finding_proposal_default_fix_hint_when_missing(tmp_home, monkeypatch):
    skills_root = tmp_home / "skills"
    body = "# Theta\n\n## When to Use\n\nUse theta sometimes.\n"
    path = _write_skill(skills_root, "theta", body, use_count=9)
    finding = _capability_finding("theta", "Use theta sometimes.")
    finding.pop("fix_hint")

    after = body.replace("Use theta sometimes.", "Use theta when reconciling the ledger.")
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": True, "text": after, "rationale": "concrete", "reason": None,
    })

    proposal = proposals._build_proposal_for_finding(finding, path)
    assert proposal is not None
    # A non-empty default fix_hint that steers AWAY from generic scaffolding.
    assert proposal["fix_hint"]
    assert "scaffold" in proposal["fix_hint"].lower()


def test_finding_proposal_dropped_when_writer_rejects(tmp_home, monkeypatch):
    skills_root = tmp_home / "skills"
    body = "# Kappa\n\n## When to Use\n\nUse kappa sometimes.\n"
    path = _write_skill(skills_root, "kappa", body, use_count=9)
    finding = _capability_finding("kappa", "Use kappa sometimes.")

    # Writer rejects (hallucinated / dangerous) → no proposal at all.
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": False, "text": None, "rationale": None, "reason": "dangerous execution pattern not allowed",
    })
    assert proposals._build_proposal_for_finding(finding, path) is None


# ===========================================================================
# Track A (5): confirm-batch — only judge-confirmed proposals are written,
# a backup is created, and revert (auto-revert) restores the original.
# A capability_research proposal is read-only at apply; the batch path exercises
# the judge gate + the reversible scaffold/skill apply path.
# ===========================================================================
def _judge_reply(monkeypatch, resolved, no_regression):
    payload = {"resolved": resolved, "no_regression": no_regression, "reason": "judge"}

    class _Msg:
        content = json.dumps(payload)

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    monkeypatch.setattr(view._writer, "_call_llm", lambda **_k: _Resp())


def test_confirm_batch_writes_only_judge_confirmed(tmp_home, monkeypatch):
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "sigma", "# Sigma\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    _judge_reply(monkeypatch, resolved=True, no_regression=True)

    res = view.confirm_batch_proposals([pid], confirm=True)
    assert res["ok"] is True
    item = res["results"][0]
    assert item["status"] == "applied"
    # The skill file was actually written (scaffold block appended).
    assert skill.read_text(encoding="utf-8") != before
    assert proposals.load_proposal(pid)["status"] == "applied"


def test_confirm_batch_skips_when_judge_rejects(tmp_home, monkeypatch):
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "tau", "# Tau\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    _judge_reply(monkeypatch, resolved=True, no_regression=False)  # regression → skip

    res = view.confirm_batch_proposals([pid], confirm=True)
    assert res["ok"] is False
    item = res["results"][0]
    assert item["status"] == "skipped"
    # Nothing written; proposal stays open.
    assert skill.read_text(encoding="utf-8") == before
    assert proposals.load_proposal(pid)["status"] == "proposed"


def test_confirm_batch_creates_backup_and_revert_restores(tmp_home, monkeypatch):
    skill, pid = _store_scaffold_proposal(tmp_home / "skills", "upsilon", "# Upsilon\n\nThin.\n")
    before = skill.read_text(encoding="utf-8")
    _judge_reply(monkeypatch, resolved=True, no_regression=True)

    res = view.confirm_batch_proposals([pid], confirm=True)
    assert res["results"][0]["status"] == "applied"

    applied = proposals.load_proposal(pid)
    backup_dir = Path(applied["backup_dir"])
    assert backup_dir.exists(), "apply must leave a backup directory"
    # The backup holds the pre-apply original verbatim.
    backups = list(backup_dir.rglob("SKILL.md"))
    assert backups, "backup should contain the skill file"
    assert backups[0].read_text(encoding="utf-8") == before

    # Revert by restoring the backup → file matches the original again.
    runner = proposals._runner()
    runner._restore_file(skill, tmp_home / "skills", backup_dir)
    assert skill.read_text(encoding="utf-8") == before


def test_confirm_batch_requires_confirm(tmp_home):
    _skill, pid = _store_scaffold_proposal(tmp_home / "skills", "phi", "# Phi\n\nThin.\n")
    res = view.confirm_batch_proposals([pid], confirm=False)
    assert res["ok"] is False
    assert res["results"][0]["status"] == "skipped"
    assert "confirm" in res["results"][0]["reason"]


def test_confirm_batch_writes_grounded_capability_fix(tmp_home, monkeypatch):
    # A capability_research proposal carries a grounded REPLACEMENT fix. Once the
    # judge confirms it, confirm-batch WRITES it (replace, not append), leaves a
    # backup, and that backup restores the original — Track-A reqs (3) + (5).
    skills_root = tmp_home / "skills"
    body = "# Chi\n\n## When to Use\n\nUse chi sometimes.\n"
    path = _write_skill(skills_root, "chi", body, use_count=9)
    after = body.replace("Use chi sometimes.", "Use chi when closing the books.")
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": True, "text": after, "rationale": "concrete", "reason": None,
    })
    proposal = proposals._build_proposal_for_finding(_capability_finding("chi", "Use chi sometimes."), path)
    proposals.save_proposal(proposal)
    before = path.read_text(encoding="utf-8")

    _judge_reply(monkeypatch, resolved=True, no_regression=True)
    res = view.confirm_batch_proposals([proposal["id"]], confirm=True)

    assert res["results"][0]["status"] == "applied"
    # The grounded fix REPLACED the whole skill (not the scaffold append path).
    assert path.read_text(encoding="utf-8") == after
    # A backup of the pre-apply original exists and restores cleanly (revert).
    applied = proposals.load_proposal(proposal["id"])
    backup_dir = Path(applied["backup_dir"])
    assert backup_dir.exists(), "apply must leave a backup directory"
    runner = proposals._runner()
    runner._restore_file(path, skills_root, backup_dir)
    assert path.read_text(encoding="utf-8") == before


def test_apply_blocked_capability_proposal_stays_readonly(tmp_home, monkeypatch):
    # A detection-only capability_research proposal (explicit apply_blocked_reason,
    # no grounded fix) must still be refused — the read-only guard now keys on the
    # reason field, not the proposal_type.
    skills_root = tmp_home / "skills"
    body = "# Psi\n\n## When to Use\n\nUse psi sometimes.\n"
    path = _write_skill(skills_root, "psi", body, use_count=9)
    after = body.replace("Use psi sometimes.", "Use psi when closing the books.")
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": True, "text": after, "rationale": "concrete", "reason": None,
    })
    proposal = proposals._build_proposal_for_finding(_capability_finding("psi", "Use psi sometimes."), path)
    proposal["apply_blocked_reason"] = "detection-only finding, no grounded fix"
    proposals.save_proposal(proposal)
    before = path.read_text(encoding="utf-8")

    res = proposals.apply_proposal(proposal["id"], confirm=True)
    assert res["ok"] is False
    assert "detection-only" in res["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_single_apply_refuses_unjudged_capability_fix(tmp_home, monkeypatch):
    # A groundable AR3 fix applied via the single-apply path (judged=False) must be
    # refused — the signed gate is Judge + Batch-Confirm, so a direct apply that
    # skips the judge may not write the skill.
    skills_root = tmp_home / "skills"
    body = "# Omega\n\n## When to Use\n\nUse omega sometimes.\n"
    path = _write_skill(skills_root, "omega", body, use_count=9)
    after = body.replace("Use omega sometimes.", "Use omega when closing the books.")
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": True, "text": after, "rationale": "concrete", "reason": None,
    })
    proposal = proposals._build_proposal_for_finding(_capability_finding("omega", "Use omega sometimes."), path)
    proposals.save_proposal(proposal)
    before = path.read_text(encoding="utf-8")

    res = proposals.apply_proposal(proposal["id"], confirm=True)  # judged defaults to False
    assert res["ok"] is False
    assert "Batch-Confirm" in res["detail"]
    assert path.read_text(encoding="utf-8") == before  # file untouched


# ===========================================================================
# Track A (6): Scaffolder-off — the grounded fix pipeline does NOT emit the
# legacy flat "Abschnitt hinzufügen" scaffold proposal. A finding-built proposal
# is a grounded fix (writer != 'scaffold'), and its produced text carries no
# autoresearch-scaffold placeholder marker.
# ===========================================================================
def test_fix_pipeline_emits_no_flat_scaffold_proposal(tmp_home, monkeypatch):
    skills_root = tmp_home / "skills"
    body = "# Psi\n\n## When to Use\n\nUse psi sometimes.\n"
    path = _write_skill(skills_root, "psi", body, use_count=9)
    after = body.replace("Use psi sometimes.", "Use psi when rotating the signing keys.")
    monkeypatch.setattr(proposals, "draft_fix", lambda *_a, **_k: {
        "ok": True, "text": after, "rationale": "concrete", "reason": None,
    })

    proposal = proposals._build_proposal_for_finding(_capability_finding("psi", "Use psi sometimes."), path)
    assert proposal is not None
    # NOT the legacy flat scaffold writer / title.
    assert proposal["writer"] != "scaffold"
    assert "Abschnitt" not in proposal["title"]
    assert proposal["title"].startswith("Skill-Schwäche")
    # No scaffold placeholder block anywhere in the produced text.
    assert "autoresearch-scaffold" not in proposal["after_text"]
    assert "autoresearch-scaffold" not in proposal["new_text"]

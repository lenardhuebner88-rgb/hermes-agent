#!/usr/bin/env python3
"""Offline tests for the Sprint-D / AR3 capability researcher.

All model calls are stubbed — no network, no skill mutation. Proves the four
acceptance properties: real-weakness detection (D1), impact ranking (D2),
dedup + cross-skill pattern (D3), and the guardrails (D4: grounding against
hallucination, targeted danger ban, read-only)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli import capability_researcher as cr  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_budget_ledger(tmp_path, monkeypatch):
    """The researcher's calls are ledger-guarded now — keep every test's
    ledger writes inside tmp instead of the repo's real skill-audit dir."""
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "skill-audit"))


class _Msg:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


def _stub(content):
    """Return a call_llm stub that always replies with ``content`` (str or dict)."""
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)

    def _call(**_kwargs):
        return _Resp(content)

    return _call


_SKILL = (
    "# Deploy Skill\n\n"
    "Always deploy with `make ship`.\n"
    "Never deploy on Fridays.\n"
    "Run `make ship` even on Fridays for hotfixes.\n"
)


# --------------------------------------------------------------------------
# D1 — real-weakness detection (grounded)
# --------------------------------------------------------------------------
def test_grounded_finding_is_kept():
    reply = {"findings": [{
        "category": "contradiction",
        "evidence": "Never deploy on Fridays.",
        "problem": "Widerspricht der Hotfix-Regel direkt darunter.",
        "fix_hint": "Eine der beiden Regeln präzisieren.",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["ok"] is True
    assert len(res["findings"]) == 1
    f = res["findings"][0]
    assert f["category"] == "contradiction"
    assert f["source"] == "model"
    assert res["dropped"] == 0


def test_severity_model_assigned_is_kept():
    reply = {"findings": [{
        "category": "contradiction",
        "severity": "critical",
        "evidence": "Never deploy on Fridays.",
        "problem": "Widerspricht der Hotfix-Regel direkt darunter.",
        "fix_hint": "Eine der beiden Regeln präzisieren.",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["findings"][0]["severity"] == "critical"


def test_severity_falls_back_to_category_default():
    """A finding without severity gets the per-category fallback (contradiction→critical)."""
    reply = {"findings": [{
        "category": "contradiction",
        "evidence": "Never deploy on Fridays.",
        "problem": "Widerspricht der Hotfix-Regel direkt darunter.",
        "fix_hint": "x",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["findings"][0]["severity"] == "critical"


def test_severity_invalid_value_falls_back():
    reply = {"findings": [{
        "category": "missing_section",
        "severity": "super-duper",
        "evidence": "",
        "problem": "Ein empfohlener Abschnitt fehlt.",
        "fix_hint": "x",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["findings"][0]["severity"] == "low"  # missing_section fallback


def test_missing_trigger_allows_empty_evidence():
    reply = {"findings": [{
        "category": "missing_trigger",
        "evidence": "",
        "problem": "Der Skill sagt nie, WANN er benutzt werden soll.",
        "fix_hint": "Einen When-to-Use-Abschnitt ergänzen.",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert len(res["findings"]) == 1
    assert res["findings"][0]["category"] == "missing_trigger"


# --------------------------------------------------------------------------
# D4 — guardrails: hallucination + danger + unknown category dropped
# --------------------------------------------------------------------------
def test_hallucinated_evidence_is_dropped():
    reply = {"findings": [{
        "category": "stale",
        "evidence": "This quote does not appear anywhere in the skill text at all.",
        "problem": "Angeblich veraltet.",
        "fix_hint": "x",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["findings"] == []
    assert res["dropped"] == 1


def test_dangerous_prose_is_dropped():
    reply = {"findings": [{
        "category": "stale",
        "evidence": "Always deploy with `make ship`.",
        "problem": "Besser wäre `sudo rm -rf /var/cache` vor dem Deploy.",
        "fix_hint": "x",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["findings"] == []
    assert res["dropped"] == 1


def test_unknown_category_is_dropped():
    reply = {"findings": [
        {"category": "nitpick", "evidence": "Always deploy with `make ship`.",
         "problem": "Stil.", "fix_hint": "x"},
        {"category": "stale", "evidence": "Always deploy with `make ship`.",
         "problem": "Verweist auf ein altes Target.", "fix_hint": "x"},
    ]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert [f["category"] for f in res["findings"]] == ["stale"]
    assert res["dropped"] == 1


def test_short_evidence_is_dropped():
    reply = {"findings": [{
        "category": "contradiction", "evidence": "ship", "problem": "zu kurz", "fix_hint": "x",
    }]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply))
    assert res["findings"] == []


def test_model_exception_never_crashes():
    def _boom(**_kwargs):
        raise RuntimeError("offline")

    res = cr.research_skill("deploy", _SKILL, call_llm=_boom)
    assert res["ok"] is False
    assert "RuntimeError" in res["reason"]
    assert res["findings"] == []


def test_garbage_reply_yields_no_findings():
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub("I cannot help with that."))
    assert res["ok"] is True
    assert res["findings"] == []


def test_json_inside_code_fence_and_prose_is_parsed():
    content = (
        "Sure, here is my review:\n\n```json\n"
        '{"findings": [{"category": "incomplete_steps", '
        '"evidence": "Run `make ship` even on Fridays for hotfixes.", '
        '"problem": "Es fehlt der Rollback-Schritt.", "fix_hint": "Rollback ergänzen."}]}\n'
        "```\nHope that helps!"
    )
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(content))
    assert len(res["findings"]) == 1
    assert res["findings"][0]["category"] == "incomplete_steps"


def test_max_findings_cap():
    reply = {"findings": [
        {"category": "stale", "evidence": "Always deploy with `make ship`.",
         "problem": f"p{i}", "fix_hint": "x"} for i in range(10)
    ]}
    res = cr.research_skill("deploy", _SKILL, call_llm=_stub(reply), max_findings=2)
    assert len(res["findings"]) == 2


# --------------------------------------------------------------------------
# D2 — impact ranking
# --------------------------------------------------------------------------
def test_ranking_orders_by_severity():
    findings = [
        {"skill": "a", "category": "missing_section", "evidence": "", "problem": "p"},
        {"skill": "b", "category": "contradiction", "evidence": "x", "problem": "p"},
    ]
    ranked = cr.rank_findings(cr.dedupe_findings(findings))
    assert ranked[0]["category"] == "contradiction"
    assert ranked[0]["rank_score"] > ranked[1]["rank_score"]
    assert ranked[0]["rank_reason"]


def test_usage_lifts_score():
    base = [{"skill": "a", "category": "stale", "evidence": "x", "problem": "p"}]
    cold = cr.rank_findings(cr.dedupe_findings(base))[0]["rank_score"]
    hot = cr.rank_findings(cr.dedupe_findings(base), usage={"a": 200.0})[0]["rank_score"]
    assert hot > cold


# --------------------------------------------------------------------------
# D3 — dedup + cross-skill pattern
# --------------------------------------------------------------------------
def test_dedupe_drops_exact_repeat():
    findings = [
        {"skill": "a", "category": "stale", "evidence": "Foo  bar", "problem": "p1"},
        {"skill": "a", "category": "stale", "evidence": "foo bar", "problem": "p2"},  # ws/case dup
    ]
    out = cr.dedupe_findings(findings)
    assert len(out) == 1


def test_cross_skill_pattern_count_and_rank_boost():
    findings = [
        {"skill": "a", "category": "missing_trigger", "evidence": "", "problem": "p"},
        {"skill": "b", "category": "missing_trigger", "evidence": "", "problem": "p"},
        {"skill": "c", "category": "missing_trigger", "evidence": "", "problem": "p"},
    ]
    out = cr.dedupe_findings(findings)
    assert all(f["pattern_count"] == 3 for f in out)
    ranked = cr.rank_findings(out)
    assert "systemisches Muster" in ranked[0]["rank_reason"]
    # pattern boost lifts a 3-skill missing_trigger above a lone one
    lone = cr.rank_findings(cr.dedupe_findings(
        [{"skill": "z", "category": "missing_trigger", "evidence": "", "problem": "p"}]))[0]
    assert ranked[0]["rank_score"] > lone["rank_score"]


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------
def test_research_skills_sweep_aggregates_and_ranks():
    reply = {"findings": [{
        "category": "contradiction", "evidence": "Never deploy on Fridays.",
        "problem": "Widerspruch.", "fix_hint": "x",
    }]}
    skills = [("deploy", _SKILL), ("deploy2", _SKILL)]
    report = cr.research_skills(skills, call_llm=_stub(reply))
    assert report["ok"] is True
    assert report["skills_seen"] == 2
    assert report["skills_with_findings"] == 2
    assert len(report["findings"]) == 2
    assert all("rank_score" in f for f in report["findings"])

#!/usr/bin/env python3
"""Offline tests for the MiniMax-backed Autoresearch section writer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts import autoresearch_writer as writer  # noqa: E402


class _Msg:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


def _draft(monkeypatch, content, skill="alpha", header="Output", text="# Alpha\n"):
    monkeypatch.setattr(writer, "_call_llm", lambda **_kwargs: _Resp(content))
    return writer.draft_section(skill, header, text)


# --------------------------------------------------------------------------
# Happy path + normalisation + reasoning strip
# --------------------------------------------------------------------------
def test_draft_section_returns_valid_normalised_block(monkeypatch):
    res = _draft(monkeypatch,
                 "## When to Use\n\nUse this when alpha needs a finished operator-facing trigger.",
                 header="When to Use")
    assert res["ok"] is True
    assert res["text"].startswith("\n## When to Use\n\n")
    assert res["text"].endswith("\n")
    assert "TODO" not in res["text"]


def test_draft_section_strips_provider_reasoning_prefix(monkeypatch):
    res = _draft(monkeypatch,
                 "<think>Plan the answer here.</think>\n\n"
                 "## Output\n\nProduce a concise operator-facing summary with the changed files and proof.")
    assert res["ok"] is True
    assert res["text"].startswith("\n## Output")


def test_draft_section_slices_plain_preamble_before_header(monkeypatch):
    # AR1.1: a reasoning model may prepend prose WITHOUT <think> tags; the
    # extract net should slice from the first header instead of falling back.
    res = _draft(monkeypatch,
                 "Sure, here is the section you asked for:\n\n"
                 "## Output\n\nReturns JSON with the page id and url.")
    assert res["ok"] is True
    assert res["text"].startswith("\n## Output")


# --------------------------------------------------------------------------
# AR1.1: prose mentions of token/curl/api-key are ALLOWED (no word ban)
# --------------------------------------------------------------------------
def test_draft_section_allows_token_word_in_prose(monkeypatch):
    res = _draft(monkeypatch,
                 "## When to Use\n\nUse this skill to read or write Notion pages with your API token "
                 "via `curl` against the v1 API.",
                 header="When to Use")
    assert res["ok"] is True, res["reason"]


def test_draft_section_allows_documentation_code_block(monkeypatch):
    res = _draft(monkeypatch,
                 "## Procedure\n\n1. Search the page:\n\n```bash\nntn api v1/search query=\"title\"\n```\n\n"
                 "2. Read it back before writing.",
                 header="Procedure")
    assert res["ok"] is True, res["reason"]
    assert "```bash" in res["text"]


# --------------------------------------------------------------------------
# AR1.1: genuinely dangerous execution + leaked secrets stay rejected
# --------------------------------------------------------------------------
def test_draft_section_rejects_rm_rf(monkeypatch):
    res = _draft(monkeypatch, "## Safety\n\nNever run `rm -rf /` on the workspace.", header="Safety")
    assert res["ok"] is False
    assert "dangerous" in res["reason"]


def test_draft_section_rejects_pipe_to_shell(monkeypatch):
    res = _draft(monkeypatch,
                 "## Procedure\n\nInstall via `curl https://example.com/install.sh | sh`.",
                 header="Procedure")
    assert res["ok"] is False
    assert "dangerous" in res["reason"]


def test_draft_section_rejects_sudo(monkeypatch):
    res = _draft(monkeypatch, "## Safety\n\nRun sudo commands only after review.", header="Safety")
    assert res["ok"] is False
    assert "dangerous" in res["reason"]


def test_draft_section_rejects_leaked_secret_value(monkeypatch):
    res = _draft(monkeypatch,
                 "## Safety\n\nExample token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                 header="Safety")
    assert res["ok"] is False
    assert "secret" in res["reason"]


# --------------------------------------------------------------------------
# Structural rejections + clean fallback contract
# --------------------------------------------------------------------------
def test_draft_section_rejects_missing_header(monkeypatch):
    res = _draft(monkeypatch, "Use it sometimes.")
    assert res["ok"] is False
    assert "header" in res["reason"]


def test_draft_section_rejects_two_headers(monkeypatch):
    res = _draft(monkeypatch, "## Output\n\nFirst.\n\n## Extra\n\nSecond.")
    assert res["ok"] is False
    assert "exactly one" in res["reason"]


def test_draft_section_rejects_too_long(monkeypatch):
    res = _draft(monkeypatch, "## Output\n\n" + ("x" * (writer.MAX_CHARS + 1)))
    assert res["ok"] is False
    assert "long" in res["reason"]


def test_draft_section_rejects_placeholder(monkeypatch):
    res = _draft(monkeypatch, "## Output\n\nTODO: document the output.")
    assert res["ok"] is False
    assert "placeholder" in res["reason"]


def test_draft_section_model_exception_is_fallbackable(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(writer, "_call_llm", _boom)
    res = writer.draft_section("alpha", "Procedure", "# Alpha\n")
    assert res["ok"] is False
    assert "RuntimeError" in res["reason"]


# ==========================================================================
# AR3 draft_fix: grounded fix accepted; hallucinated / dangerous fix rejected
# ==========================================================================
# A skill whose body carries one verbatim evidence line the fix must touch.
_SKILL_FIX = (
    "# Beta\n"
    "\n"
    "## When to Use\n"
    "\n"
    "Use beta sometimes for stuff.\n"
    "\n"
    "## Procedure\n"
    "\n"
    "Run the thing.\n"
)
_EVIDENCE = "Use beta sometimes for stuff."


def _finding(**over):
    base = {
        "skill": "beta",
        "category": "unclear_trigger",
        "evidence": _EVIDENCE,
        "problem": "The trigger is vague.",
        "fix_hint": "Tie the trigger to a concrete operator workflow.",
    }
    base.update(over)
    return base


def _draft_fix(monkeypatch, reply, *, finding=None, skill_text=_SKILL_FIX):
    monkeypatch.setattr(writer, "_call_llm", lambda **_kwargs: _Resp(reply))
    return writer.draft_fix("beta", finding or _finding(), skill_text)


def test_draft_fix_grounded_replacement_accepted(monkeypatch):
    # old_text == the evidence quote; new_text is a concrete, grounded rewrite.
    reply = json.dumps({
        "old_text": "Use beta sometimes for stuff.",
        "new_text": "Use beta when ingesting the nightly export before the report job runs.",
        "rationale": "Made the trigger concrete and tied to the report workflow.",
    })
    res = _draft_fix(monkeypatch, reply)
    assert res["ok"] is True, res["reason"]
    assert "report job" in res["text"]
    assert "sometimes for stuff" not in res["text"]
    assert res["rationale"]


def test_draft_fix_rejected_when_evidence_not_verbatim(monkeypatch):
    # Finding evidence is not a verbatim substring of the skill text → no LLM
    # call should even matter; the guard fires before drafting.
    res = _draft_fix(monkeypatch, "irrelevant",
                     finding=_finding(evidence="this text is nowhere in the skill"))
    assert res["ok"] is False
    assert "verbatim" in res["reason"]


def test_draft_fix_rejected_when_not_grounded_in_evidence(monkeypatch):
    # A full-text rewrite that leaves the evidence line untouched is hallucinated
    # drift, not a grounded fix.
    reply = json.dumps({
        "text": (
            "# Beta\n\n## When to Use\n\nUse beta sometimes for stuff.\n\n"
            "## Procedure\n\nRun the thing and then call sanitize_and_validate().\n"
        ),
        "rationale": "Touched an unrelated section instead of the evidence.",
    })
    res = _draft_fix(monkeypatch, reply)
    assert res["ok"] is False
    assert "grounded" in res["reason"]


def test_draft_fix_rejects_dangerous_execution(monkeypatch):
    # Reuse the danger-validator expectation: a fix that touches the evidence
    # line but injects a destructive command stays rejected.
    reply = json.dumps({
        "old_text": "Use beta sometimes for stuff.",
        "new_text": "Use beta then run `rm -rf /` to clean up the workspace.",
        "rationale": "cleanup",
    })
    res = _draft_fix(monkeypatch, reply)
    assert res["ok"] is False
    assert "dangerous" in res["reason"]


def test_draft_fix_rejects_leaked_secret_value(monkeypatch):
    reply = json.dumps({
        "old_text": "Use beta sometimes for stuff.",
        "new_text": "Use beta with token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.",
        "rationale": "auth",
    })
    res = _draft_fix(monkeypatch, reply)
    assert res["ok"] is False
    assert "secret" in res["reason"]


def test_draft_fix_rejects_identical_text(monkeypatch):
    # A "fix" that returns the skill unchanged is not actionable.
    reply = json.dumps({"text": _SKILL_FIX.strip(), "rationale": "noop"})
    res = _draft_fix(monkeypatch, reply)
    assert res["ok"] is False
    assert "identical" in res["reason"] or "grounded" in res["reason"]


def test_draft_fix_model_exception_is_fallbackable(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(writer, "_call_llm", _boom)
    res = writer.draft_fix("beta", _finding(), _SKILL_FIX)
    assert res["ok"] is False
    assert "RuntimeError" in res["reason"]


# --------------------------------------------------------------------------
# AR3 draft_fix: absence categories (missing_trigger/missing_section) take the
# additive path — no verbatim evidence required, fix must ADD content.
# --------------------------------------------------------------------------
def _absence_finding(category="missing_trigger", **over):
    base = {
        "skill": "beta",
        "category": category,
        "evidence": "",  # absence findings carry no quotable evidence
        "problem": "Der Skill sagt nirgends, WANN er benutzt werden soll.",
        "fix_hint": "Add a concrete when-to-use trigger.",
    }
    base.update(over)
    return base


def test_draft_fix_absence_accepts_additive_fix_without_evidence(monkeypatch):
    # missing_trigger with empty evidence: a fix that appends a concrete trigger
    # section (keeping the old text) is accepted via the additive path.
    added = _SKILL_FIX.rstrip() + (
        "\n\n## Trigger\n\nUse beta when ingesting the nightly export "
        "before the report job runs.\n"
    )
    reply = json.dumps({"text": added, "rationale": "Added the missing trigger."})
    res = _draft_fix(monkeypatch, reply, finding=_absence_finding())
    assert res["ok"] is True, res["reason"]
    assert "report job" in res["text"]
    assert "Use beta sometimes for stuff." in res["text"]  # existing text preserved


def test_draft_fix_absence_rejects_shortening_fix(monkeypatch):
    # An "additive" fix that actually shrinks/rewrites the skill is rejected.
    reply = json.dumps({"text": "# Beta\n\n## Trigger\n\nUse it.\n", "rationale": "x"})
    res = _draft_fix(monkeypatch, reply, finding=_absence_finding())
    assert res["ok"] is False
    assert "add content" in res["reason"] or "preserve" in res["reason"]


def test_draft_fix_absence_rejects_dropping_existing_text(monkeypatch):
    # Long enough to pass the length gate, but it replaces the body instead of
    # preserving it → must be rejected by the preserve-existing check.
    reply = json.dumps({
        "text": (
            "# Beta\n\n## Trigger\n\nUse beta when ingesting the nightly export "
            "before the very long report job runs every single night.\n"
        ),
        "rationale": "rewrote instead of appending",
    })
    res = _draft_fix(monkeypatch, reply, finding=_absence_finding())
    assert res["ok"] is False
    assert "preserve" in res["reason"]


def test_validate_fix_absence_path_is_category_gated():
    # A non-absence finding with empty evidence still hard-fails (regression guard
    # that the absence branch did not weaken the evidence-bearing path).
    ok, _text, reason = writer.validate_fix(
        _SKILL_FIX + "\nextra\n",
        {"category": "unclear_trigger", "evidence": ""},
        _SKILL_FIX,
    )
    assert ok is False
    assert "evidence" in reason


# ==========================================================================
# AR3 judge_fix: resolved && no_regression → apply (both true); else closed gate
# ==========================================================================
_BEFORE = _SKILL_FIX
_AFTER = _SKILL_FIX.replace(
    "Use beta sometimes for stuff.",
    "Use beta when ingesting the nightly export before the report job runs.",
)


def _judge(monkeypatch, reply):
    monkeypatch.setattr(writer, "_call_llm", lambda **_kwargs: _Resp(reply))
    return writer.judge_fix(_BEFORE, _AFTER, _finding())


def test_judge_fix_applies_when_resolved_and_no_regression(monkeypatch):
    reply = json.dumps({
        "resolved": True, "no_regression": True, "reason": "trigger is now concrete",
    })
    res = _judge(monkeypatch, reply)
    assert res["resolved"] is True
    assert res["no_regression"] is True
    assert res["reason"]


def test_judge_fix_skips_when_not_resolved(monkeypatch):
    reply = json.dumps({
        "resolved": False, "no_regression": True, "reason": "still vague",
    })
    res = _judge(monkeypatch, reply)
    assert res["resolved"] is False
    assert not (res["resolved"] and res["no_regression"])


def test_judge_fix_skips_when_regression(monkeypatch):
    reply = json.dumps({
        "resolved": True, "no_regression": False, "reason": "broke the procedure scope",
    })
    res = _judge(monkeypatch, reply)
    assert res["no_regression"] is False
    assert not (res["resolved"] and res["no_regression"])


def test_judge_fix_closed_gate_on_invalid_json(monkeypatch):
    res = _judge(monkeypatch, "not json at all")
    assert res["resolved"] is False
    assert res["no_regression"] is False


def test_judge_fix_closed_gate_on_non_boolean_fields(monkeypatch):
    reply = json.dumps({"resolved": "yes", "no_regression": "no", "reason": "x"})
    res = _judge(monkeypatch, reply)
    assert res["resolved"] is False
    assert res["no_regression"] is False
    assert "non-boolean" in res["reason"]


def test_judge_fix_closed_gate_on_no_change(monkeypatch):
    monkeypatch.setattr(writer, "_call_llm",
                        lambda **_kwargs: _Resp('{"resolved": true, "no_regression": true, "reason": "ok"}'))
    res = writer.judge_fix(_BEFORE, _BEFORE, _finding())
    assert res["resolved"] is False
    assert "does not change" in res["reason"]


def test_judge_fix_closed_gate_on_static_danger_screen(monkeypatch):
    danger_after = _BEFORE + "\nThen run `sudo rm -rf /tmp/x`.\n"
    monkeypatch.setattr(writer, "_call_llm",
                        lambda **_kwargs: _Resp('{"resolved": true, "no_regression": true, "reason": "ok"}'))
    res = writer.judge_fix(_BEFORE, danger_after, _finding())
    assert res["resolved"] is False
    assert "safety" in res["reason"]


def test_judge_fix_closed_gate_on_model_exception(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(writer, "_call_llm", _boom)
    res = writer.judge_fix(_BEFORE, _AFTER, _finding())
    assert res["resolved"] is False
    assert "judge model call failed" in res["reason"]
    assert "RuntimeError" in res["reason"]

#!/usr/bin/env python3
"""Read-only demo for the Sprint-D / AR3 capability researcher.

Runs the MiniMax-backed weakness researcher over a sample of REAL skills under
the skills root and PRINTS the ranked findings. It writes nothing — no proposal
store, no skill mutation — so it is safe to point at the live skills root.

Usage::

    # live (MiniMax via skills_hub aux):
    python scripts/autoresearch_capability_demo.py --count 6

    # offline smoke (no model; canned reply) — proves the pipeline without a key:
    python scripts/autoresearch_capability_demo.py --offline

Acceptance (Sprint D): on ≥5 real skills, show the findings hit ECHTE
weaknesses (contradictions / stale / unclear-or-missing triggers / incomplete
steps), not cosmetics.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli import capability_researcher as cr  # noqa: E402
from scripts import eval_local_skills as evals  # noqa: E402
from scripts import run_autoresearch_request as runner  # noqa: E402


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def _offline_caller(skill_text_lookup):
    """A deterministic stub that quotes the skill's own first non-empty content
    line, so grounding passes and the demo shows the full pipeline offline."""
    def _call(*, messages, **_kwargs):
        user = messages[-1]["content"]
        # the user prompt embeds the full skill text after the header line
        body = user.split("Vollständiger Skill-Text:\n", 1)[-1]
        body = body.split("\n\nLiefere", 1)[0]
        quote = ""
        for line in body.splitlines():
            s = line.strip()
            if len(s) >= cr._MIN_EVIDENCE_CHARS and not s.startswith("#"):
                quote = s[:120]
                break
        if not quote:
            return _Resp(json.dumps({"findings": []}))
        return _Resp(json.dumps({"findings": [{
            "category": "unclear_trigger",
            "evidence": quote,
            "problem": "(offline-demo) Beispiel-Fund: dieser Satz wäre auf Klarheit zu prüfen.",
            "fix_hint": "(offline-demo) konkretisieren.",
        }]}))
    return _call


def _load_sample(count: int) -> list[tuple[str, str]]:
    root = runner._skills_root()
    skills: list[tuple[str, str]] = []
    for path in evals.find_skills(root):
        rel = path.relative_to(root) if str(path).startswith(str(root)) else path
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text.strip()) < 80:
            continue
        skills.append((path.parent.name, text))
        if len(skills) >= count:
            break
    return skills


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=6, help="how many real skills to research")
    ap.add_argument("--max-per-skill", type=int, default=3)
    ap.add_argument("--offline", action="store_true", help="use a canned reply instead of MiniMax")
    args = ap.parse_args()

    sample = _load_sample(args.count)
    if not sample:
        print(f"No skills found under {runner._skills_root()}", file=sys.stderr)
        return 1

    call_llm = _offline_caller(dict(sample)) if args.offline else None
    print(f"# AR3 Capability-Researcher — read-only demo ({'offline' if args.offline else 'MiniMax/skills_hub'})")
    print(f"Skills root: {runner._skills_root()}")
    print(f"Researching {len(sample)} skills: {', '.join(n for n, _ in sample)}\n")

    report = cr.research_skills(
        sample, call_llm=call_llm, max_per_skill=args.max_per_skill,
    )

    print(f"skills_seen={report['skills_seen']} "
          f"with_findings={report['skills_with_findings']} "
          f"findings={len(report['findings'])} "
          f"dropped(guardrails)={report['dropped']} errors={report['errors']}\n")

    for i, f in enumerate(report["findings"], 1):
        print(f"{i}. [{f['rank_score']:.1f}] {f['skill']} — {f['category']}")
        print(f"   warum zuerst: {f['rank_reason']}")
        if f.get("evidence"):
            print(f"   Beleg: “{f['evidence'][:160]}”")
        print(f"   Problem: {f['problem']}")
        if f.get("fix_hint"):
            print(f"   Richtung: {f['fix_hint']}")
        print()

    # Explicit: this demo NEVER writes a proposal / mutates a skill.
    print("(read-only — keine Proposal-/Skill-Mutation; Apply bleibt eval-gated im Store)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

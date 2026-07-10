#!/usr/bin/env python3
"""Sprint D / AR3: Autoresearch capability researcher.

Lifts Autoresearch from a *section-drafter* ("a recommended section is missing →
scaffold it") to a *capability researcher* that finds the **real** weaknesses an
operator actually cares about:

* ``contradiction``    — two instructions in the skill disagree
* ``stale``            — an instruction is outdated / references something gone
* ``missing_trigger``  — no "when to use" / activation trigger at all
* ``unclear_trigger``  — a trigger exists but is vague / un-actionable
* ``incomplete_steps`` — the procedure stops mid-way / has an obvious gap
* ``missing_section``  — a recommended section is absent. Lowest-severity AR3
                         category; section-scaffold discovery is no longer the
                         live proposal source.

The semantic categories are detected by the **MiniMax-M2.7** model over the
existing ``skills_hub`` auxiliary slot (``agent.auxiliary_client.call_llm(
task="skills_hub", ...)`` — the very slot AR1's writer uses; an *unknown* task
name would silently fall back to gpt-5.5, so we deliberately reuse ``skills_hub``
and never touch config).

Guardrails carried over from AR1.1 / the apply-gate (Sprint-D D4):

* **Grounded, not hallucinated** — every semantic finding must quote *verbatim*
  text that actually occurs in the skill (whitespace-normalised substring
  check). A finding the model invented is dropped, not shown.
* **Targeted danger ban, not a word ban** — the model's ``problem``/``fix_hint``
  prose is run through the *same* ``_DANGEROUS_RE``/``_SECRET_RE`` the writer
  uses (AR1.1: real ``rm -rf``/``sudo``/pipe-to-shell/leaked-token *values*
  rejected; harmless mentions of "token"/"curl" allowed).
* **Read-only here** — this module only *detects + ranks + dedupes*. It writes
  no proposal, mutates no skill. The eval-gated, path-allowlisted, confirm-gated
  apply path in ``autoresearch_proposals`` is untouched and remains the only
  thing that can change a file.
* **Never crashes a run** — a broken model response, an offline aux client, or a
  malformed skill yields an empty/partial result with a reason, never an
  exception that aborts the sweep.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Reuse the writer's *targeted* danger/secret validators (AR1.1) so the
# capability researcher and the section writer reject exactly the same things.
from scripts.autoresearch_writer import _DANGEROUS_RE, _SECRET_RE  # noqa: E402

# ---------------------------------------------------------------------------
# Categories. Severity weight drives the impact rank; the German label is what
# the operator reads on the card. Ordered most→least costly for an agent system.
# ---------------------------------------------------------------------------
WEAKNESS_CATEGORIES: dict[str, tuple[int, str]] = {
    "contradiction": (5, "widersprüchliche Anweisung"),
    "stale": (4, "veraltete Anweisung"),
    "missing_trigger": (4, "fehlender Aktivierungs-Trigger"),
    "unclear_trigger": (3, "unklarer Aktivierungs-Trigger"),
    "incomplete_steps": (3, "unvollständige Schritte"),
    "missing_section": (2, "empfohlener Abschnitt fehlt"),
}

# Categories that point at *absent* content cannot quote the skill — grounding is
# relaxed for them (you can't quote what isn't there). Everything else MUST quote.
_ABSENCE_CATEGORIES = frozenset({"missing_trigger", "missing_section"})

# Severity scale (critical|high|medium|low) for the frontend grouping/collapse.
# Model-assigned with a per-category fallback. This is a display dimension only —
# the existing rank_findings scorer (severity = category weight) is unchanged.
_SEVERITY_SCALE = frozenset({"critical", "high", "medium", "low"})
_SKILL_CATEGORY_SEVERITY = {
    "contradiction": "critical",
    "stale": "high",
    "missing_trigger": "high",
    "unclear_trigger": "medium",
    "incomplete_steps": "medium",
    "missing_section": "low",
}


def _coerce_skill_severity(value: Any, category: str) -> str:
    sev = str(value or "").strip().lower()
    return sev if sev in _SEVERITY_SCALE else _SKILL_CATEGORY_SEVERITY.get(category, "medium")

# A semantic quote shorter than this matches almost anything → not real evidence.
_MIN_EVIDENCE_CHARS = 12
_MAX_EVIDENCE_CHARS = 240
_MAX_PROBLEM_CHARS = 400
_DEFAULT_MAX_FINDINGS = 4

_RANK_W_SEVERITY = 2.0
_RANK_W_USAGE = 1.0
_RANK_W_PATTERN = 1.5  # a weakness shared across skills is worth fixing first
_RANK_USAGE_FREQUENT = 50.0

_THINK_RE = re.compile(r"<think>.*?</think>", re.I | re.S)
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Model plumbing (isolated so tests stub it without a network)
# ---------------------------------------------------------------------------
def _default_call_llm(**kwargs):
    from agent.auxiliary_client import call_llm
    return call_llm(**kwargs)


def _extract_content(resp) -> str:
    raw = (resp.choices[0].message.content or "").strip()
    return _THINK_RE.sub("", raw).strip()


def _extract_tokens(resp) -> int:
    try:
        usage = getattr(resp, "usage", None)
        total = getattr(usage, "total_tokens", None)
        if total is None and isinstance(usage, dict):
            total = usage.get("total_tokens")
        return int(total or 0)
    except Exception:
        return 0


def _parse_findings_json(content: str) -> list[dict[str, Any]]:
    """Pull the ``findings`` array out of a model reply, tolerating prose/fences
    around the JSON. Returns ``[]`` on anything unparseable — never raises."""
    text = content.strip()
    # Strip a ```json … ``` fence if present.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    # Take the first {...} object so leading/trailing prose can't break json.loads.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    findings = data.get("findings")
    if not isinstance(findings, list):
        return []
    return [f for f in findings if isinstance(f, dict)]


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _prose_is_safe(*parts: str) -> bool:
    """Reject only genuinely dangerous execution / leaked-secret *values* in the
    model's own prose (AR1.1 targeted ban — not a blanket word ban)."""
    blob = "\n".join(p for p in parts if p)
    return not (_DANGEROUS_RE.search(blob) or _SECRET_RE.search(blob))


# ---------------------------------------------------------------------------
# Per-skill research
# ---------------------------------------------------------------------------
_JSON_SHAPE = (
    '{"findings": [{"category": "...", '
    '"severity": "critical|high|medium|low", '
    '"evidence": "<WÖRTLICHES Zitat aus dem Skill-Text, das die Schwäche belegt>", '
    '"problem": "<knapp, deutsch, warum das ein echtes Problem ist>", '
    '"fix_hint": "<kurze Richtung für die Behebung>"}]}'
)

_SYSTEM_PROMPT = (
    "Du bist ein strenger Reviewer für Agenten-Skill-Dokumentation (SKILL.md). "
    "Finde NUR ECHTE, konkrete Schwächen — keine kosmetischen Hinweise, kein Lob, "
    "keine Stiltipps. Erlaubte Kategorien (genau diese Schlüssel):\n"
    "- contradiction: zwei Anweisungen im Skill widersprechen sich\n"
    "- stale: eine Anweisung ist veraltet / verweist auf etwas, das es nicht mehr gibt\n"
    "- missing_trigger: es fehlt JEDE Angabe, WANN der Skill benutzt wird\n"
    "- unclear_trigger: ein Trigger existiert, ist aber vage / nicht handlungsleitend\n"
    "- incomplete_steps: die Schritt-/Prozess-Anleitung bricht ab oder hat eine offensichtliche Lücke\n"
    "- missing_section: ein empfohlener Abschnitt fehlt; nur nutzen, wenn keine konkretere Kategorie passt\n"
    "Antworte mit GENAU EINEM JSON-Objekt, sonst nichts:\n"
    f"{_JSON_SHAPE}\n"
    "Für contradiction/stale/unclear_trigger/incomplete_steps MUSS evidence ein wörtliches "
    "Zitat aus dem Skill sein. Für missing_trigger/missing_section darf evidence leer sein. "
    "Bewerte den Schweregrad (severity) ehrlich: critical = leitet den Agenten aktiv fehl, "
    "high = wichtige Lücke, medium = unklar, low = Nebensache. "
    'Wenn der Skill solide ist, gib {"findings": []} zurück. Erfinde NICHTS. Höchstens 4 Funde.'
)


def research_skill(
    skill_name: str,
    skill_text: str,
    *,
    call_llm: Callable[..., Any] | None = None,
    max_findings: int = _DEFAULT_MAX_FINDINGS,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Ask MiniMax for the real weaknesses of one skill, then keep only the
    grounded, safe, well-formed ones. Returns
    ``{"ok": bool, "findings": [...], "reason": str|None, "dropped": int}``.
    Pure detection — writes nothing."""
    caller = call_llm or _default_call_llm
    user = (
        f"Skill: {skill_name}\n\n"
        "Vollständiger Skill-Text:\n"
        f"{skill_text}\n\n"
        "Liefere das findings-JSON."
    )
    from hermes_cli.autoresearch_budget import BudgetExhausted, guarded_llm_call

    try:
        resp, ledger_entry = guarded_llm_call(
            lane="skill",
            call=caller,
            task="skills_hub",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=900,
            temperature=0.2,
            timeout=timeout,
        )
        tokens = int(ledger_entry.get("total_tokens") or 0) or _extract_tokens(resp)
        usage_source = str(ledger_entry.get("usage_source") or "unknown")
    except BudgetExhausted as exc:
        return {
            "ok": False,
            "findings": [],
            "reason": f"budget exhausted: {exc}",
            "dropped": 0,
            "tokens": 0,
            "usage_source": "measured",
        }
    except Exception as exc:  # offline / provider error → caller falls back gracefully
        return {
            "ok": False,
            "findings": [],
            "reason": f"model call failed: {type(exc).__name__}",
            "dropped": 0,
            "tokens": 0,
            "usage_source": "measured",
        }

    raw_findings = _parse_findings_json(_extract_content(resp))
    norm_skill = _norm(skill_text)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for raw in raw_findings:
        category = str(raw.get("category") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()
        problem = str(raw.get("problem") or "").strip()
        fix_hint = str(raw.get("fix_hint") or "").strip()
        if category not in WEAKNESS_CATEGORIES:
            dropped += 1
            continue
        if not problem or len(problem) > _MAX_PROBLEM_CHARS:
            dropped += 1
            continue
        if len(evidence) > _MAX_EVIDENCE_CHARS:
            evidence = evidence[:_MAX_EVIDENCE_CHARS]
        # Grounding: a non-absence finding must quote the skill verbatim.
        if category not in _ABSENCE_CATEGORIES:
            if len(evidence) < _MIN_EVIDENCE_CHARS or _norm(evidence) not in norm_skill:
                dropped += 1
                continue
        # Targeted safety: the model's own prose may not carry dangerous payloads.
        if not _prose_is_safe(problem, fix_hint, evidence):
            dropped += 1
            continue
        kept.append({
            "skill": skill_name,
            "category": category,
            "severity": _coerce_skill_severity(raw.get("severity"), category),
            "evidence": evidence,
            "problem": problem,
            "fix_hint": fix_hint,
            "source": "model",
        })
        if len(kept) >= max(1, int(max_findings)):
            break
    return {
        "ok": True, "findings": kept, "reason": None, "dropped": dropped,
        "tokens": tokens, "usage_source": usage_source,
    }


# ---------------------------------------------------------------------------
# Dedup + cross-skill patterns (D3)
# ---------------------------------------------------------------------------
def dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact repeats (same skill + category + normalised evidence) and, where
    a category recurs across ≥2 distinct skills, annotate each surviving finding
    with ``pattern_count`` so the ranker can lift a systemic weakness. Order is
    preserved; returned dicts are shallow copies."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for f in findings:
        key = (f.get("skill", ""), f.get("category", ""), _norm(f.get("evidence", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(f))

    # Cross-skill pattern: how many *distinct* skills share each category.
    skills_by_cat: dict[str, set[str]] = {}
    for f in out:
        skills_by_cat.setdefault(f["category"], set()).add(f.get("skill", ""))
    for f in out:
        f["pattern_count"] = len(skills_by_cat.get(f["category"], set()))
    return out


# ---------------------------------------------------------------------------
# Impact ranking (D2) — real score + plain "why first"
# ---------------------------------------------------------------------------
def _rank_reason(finding: dict[str, Any], use_count: float) -> str:
    clauses: list[str] = [WEAKNESS_CATEGORIES[finding["category"]][1]]
    if finding["category"] == "contradiction":
        clauses[0] += " (kann den Agenten aktiv fehlleiten)"
    pattern = int(finding.get("pattern_count") or 1)
    if pattern >= 2:
        clauses.append(f"betrifft {pattern} Skills (systemisches Muster)")
    if use_count >= _RANK_USAGE_FREQUENT:
        clauses.append(f"häufig genutzt ({int(use_count)}×)")
    elif use_count > 0 and pattern < 2:
        clauses.append(f"genutzt ({int(use_count)}×)")
    return "; ".join(clauses[:2])


def rank_findings(
    findings: list[dict[str, Any]],
    *,
    usage: dict[str, float] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Annotate each finding with ``rank_score`` + ``rank_reason`` and return them
    highest-impact first, deterministically. Score = severity + cross-skill
    pattern + optional usage. ``limit`` caps to the Top-N."""
    usage = usage or {}
    ranked: list[dict[str, Any]] = []
    for f in findings:
        annotated = dict(f)
        severity = WEAKNESS_CATEGORIES[f["category"]][0]
        pattern = int(f.get("pattern_count") or 1)
        pattern_w = min(max(0, pattern - 1), 3)
        use_count = float(usage.get(f.get("skill", ""), 0.0))
        usage_w = min(use_count / 50.0, 3.0)
        score = (
            severity * _RANK_W_SEVERITY
            + pattern_w * _RANK_W_PATTERN
            + usage_w * _RANK_W_USAGE
        )
        annotated["rank_score"] = round(score, 4)
        annotated["rank_reason"] = _rank_reason(f, use_count)
        ranked.append(annotated)
    ranked.sort(key=lambda f: (
        -float(f["rank_score"]), f.get("skill", ""), f.get("category", ""),
        _norm(f.get("evidence", "")),
    ))
    if limit is not None:
        ranked = ranked[: max(1, int(limit))]
    return ranked


# ---------------------------------------------------------------------------
# Sweep across skills
# ---------------------------------------------------------------------------
def research_skills(
    skills: Iterable[tuple[str, str]],
    *,
    call_llm: Callable[..., Any] | None = None,
    usage: dict[str, float] | None = None,
    max_per_skill: int = _DEFAULT_MAX_FINDINGS,
    limit: int | None = None,
    on_skill: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the capability researcher over ``(skill_name, skill_text)`` pairs,
    dedupe, rank, and cap. Read-only: returns a report, writes nothing.

    ``on_skill(skill_name)`` is invoked just BEFORE each (slow) per-skill model
    call so a caller can emit a heartbeat and not be falsely reported as crashed
    while a long sweep is in progress. It must never raise.

    Returns ``{"ok", "findings", "skills_seen", "skills_with_findings",
    "dropped", "errors"}``."""
    all_findings: list[dict[str, Any]] = []
    skills_seen = 0
    dropped = 0
    errors = 0
    tokens_total = 0
    estimated_any = False
    budget_reason: str | None = None
    for skill_name, skill_text in skills:
        skills_seen += 1
        if on_skill is not None:
            try:
                on_skill(skill_name)
            except Exception:  # a heartbeat hiccup must never sink the sweep
                pass
        res = research_skill(skill_name, skill_text, call_llm=call_llm, max_findings=max_per_skill)
        tokens_total += int(res.get("tokens") or 0)
        if str(res.get("usage_source") or "") == "estimated":
            estimated_any = True
        if not res.get("ok"):
            reason = str(res.get("reason") or "")
            if reason.startswith("budget exhausted"):
                # The shared daily ledger is spent: stop the sweep instead of
                # burning an "error" per remaining skill.
                budget_reason = reason
                skills_seen -= 1  # this skill was never actually researched
                break
            errors += 1
            continue
        dropped += int(res.get("dropped") or 0)
        all_findings.extend(res.get("findings") or [])

    deduped = dedupe_findings(all_findings)
    ranked = rank_findings(deduped, usage=usage, limit=limit)
    skills_with = len({f.get("skill") for f in ranked})
    return {
        "ok": True,
        "findings": ranked,
        "skills_seen": skills_seen,
        "skills_with_findings": skills_with,
        "dropped": dropped,
        "errors": errors,
        "tokens": tokens_total,
        "usage_source": "estimated" if estimated_any else "measured",
        "reason": budget_reason,
    }

#!/usr/bin/env python3
"""Auxiliary-model section writer for Hermes Autoresearch proposals.

AR1 drafts ONE complete SKILL.md section via the ``skills_hub`` aux task.

AR1.1 made the validation *targeted* instead of a blunt word-ban: real skill
docs legitimately mention ``token``/``curl``/``secret`` in prose and benefit
from a short example code block, so those are allowed. What stays rejected is
genuinely dangerous *execution* (``rm -rf``, ``sudo``, pipe-to-shell,
``dd if=``, raw-disk writes, ``mkfs``) and leaked secret *values*
(``ghp_…``/``sk-…``/``AKIA…``/PRIVATE KEY blocks). Anything that fails
validation returns ``ok=False`` so the caller falls back to the reversible
scaffold — never a crash, never raw passthrough.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

MAX_CHARS = 2200
MAX_LINES = 60
MAX_FIX_CHARS = 80_000

# Absence categories carry no quotable evidence (the thing they flag is missing),
# so they take the additive fix path instead of the verbatim-grounding path.
# Kept in sync with capability_researcher._ABSENCE_CATEGORIES; imported lazily to
# avoid a heavy import at module load, with a literal fallback.
try:  # pragma: no cover - exercised indirectly
    from hermes_cli.capability_researcher import _ABSENCE_CATEGORIES
except Exception:  # pragma: no cover - defensive
    _ABSENCE_CATEGORIES = frozenset({"missing_trigger", "missing_section"})

_HEADER_RE = re.compile(r"^##\s+.+$", re.M)
_FRONTMATTER_LINE_RE = re.compile(r"^---\s*$", re.M)
_PLACEHOLDER_RE = re.compile(r"\b(?:todo|placeholder|lorem ipsum)\b", re.I)
_REASONING_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.I | re.S)
_WS_RE = re.compile(r"\s+")

# Genuinely dangerous *execution* patterns — rejected even inside code blocks.
# (AR1.1: narrowed from a bare-word ban on token/curl/secret/api_key, which
#  falsely rejected normal API/auth skill docs.)
_DANGEROUS_RE = re.compile(
    r"""(?ix)
      \brm\s+-\w*[rf]\w*              # rm -rf / -fr / -r / -f
    | \bsudo\b                        # privilege escalation
    | \bmkfs\b                        # format a filesystem
    | \bdd\s+if=                      # raw disk imaging
    | >\s*/dev/(?:sd|nvme|disk|hd)    # write to a raw disk device
    | \|\s*(?:sudo\s+)?(?:ba)?sh\b    # pipe-to-shell: curl ... | sh
    """,
    re.VERBOSE,
)

# Leaked secret *values* (not the words). Real exfil risk; instructional prose
# does not contain these literal token shapes.
_SECRET_RE = re.compile(
    r"gh[posru]_[A-Za-z0-9]{20,}"             # GitHub tokens
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"          # Slack tokens
    r"|sk-[A-Za-z0-9]{20,}"                   # OpenAI-style keys
    r"|AKIA[0-9A-Z]{16}"                      # AWS access key id
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"    # private key blocks
)


def _call_llm(**kwargs):
    from agent.auxiliary_client import call_llm
    return call_llm(**kwargs)


def _configured_aux_model(task: str = "skills_hub") -> str:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        aux = cfg.get("auxiliary", {}) if isinstance(cfg, dict) else {}
        slot = aux.get(task, {}) if isinstance(aux, dict) else {}
        if isinstance(slot, dict):
            return str(slot.get("model") or "").strip()
    except Exception:
        return ""
    return ""


def _model_label_from_response(resp, *, task: str = "skills_hub") -> str:
    model = str(getattr(resp, "model", "") or "").strip()
    return model or _configured_aux_model(task) or "aux-model"


def _extract_content(resp) -> str:
    raw = (resp.choices[0].message.content or "").strip()
    return _REASONING_RE.sub("", raw).strip()


def _strip_json_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"\A```(?:json|markdown|md)?\s*", "", body, flags=re.I).strip()
        body = re.sub(r"\s*```\s*\Z", "", body).strip()
    return body


def _norm_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip()).lower()


def _parse_fix_reply(content: str, skill_text: str) -> tuple[str | None, str | None, str | None]:
    """Return (after_text, replacement_block, rationale) from a model reply.

    Preferred shape is JSON with either ``text`` (complete new SKILL.md) or an
    ``old_text``/``new_text`` replacement. Plain markdown is treated as a full
    new skill text so tests and provider quirks can stay simple.
    """
    body = _strip_json_fence(content)
    if not body:
        return None, None, None
    data: Any | None = None
    try:
        data = json.loads(body)
    except ValueError:
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(body[start:end + 1])
            except ValueError:
                data = None
    if isinstance(data, dict):
        rationale = str(data.get("rationale") or data.get("reason") or "").strip() or None
        text = data.get("text") or data.get("after_text") or data.get("skill_text")
        if isinstance(text, str) and text.strip():
            return text.strip() + "\n", text.strip() + "\n", rationale
        old_text = data.get("old_text") or data.get("old_snippet")
        new_text = data.get("new_text") or data.get("new_snippet")
        if isinstance(old_text, str) and isinstance(new_text, str):
            old = old_text.strip("\n")
            new = new_text.strip("\n")
            if old and old in skill_text:
                after = skill_text.replace(old, new, 1)
                if skill_text.endswith("\n") and not after.endswith("\n"):
                    after += "\n"
                return after, new, rationale
            return None, None, rationale
    return body.rstrip() + "\n", body.rstrip() + "\n", None


def _evidence_line_indexes(text: str, evidence: str) -> set[int]:
    idx = text.find(evidence)
    if idx < 0:
        return set()
    start = text[:idx].count("\n")
    end = text[:idx + len(evidence)].count("\n")
    return set(range(start, end + 1))


def _fix_touches_evidence(before: str, after: str, evidence: str) -> bool:
    if _norm_ws(evidence) not in _norm_ws(before):
        return False
    evidence_lines = _evidence_line_indexes(before, evidence)
    if not evidence_lines:
        compact = _norm_ws(evidence)
        before_lines = before.splitlines()
        evidence_lines = {
            i for i, line in enumerate(before_lines)
            if compact and (compact in _norm_ws(line) or _norm_ws(line) in compact)
        }
    if not evidence_lines:
        return False
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
        None, before_lines, after_lines
    ).get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:
            if any(idx in evidence_lines for idx in (i1 - 1, i1)):
                return True
            continue
        if any(i1 <= idx < i2 for idx in evidence_lines):
            return True
    return False


def validate_fix(
    text: str,
    finding: dict[str, Any],
    skill_text: str,
) -> tuple[bool, str | None, str | None]:
    body = (text or "").strip()
    category = str(finding.get("category") or "").strip()
    is_absence = category in _ABSENCE_CATEGORIES
    evidence = str(
        finding.get("evidence")
        or finding.get("evidence_quote")
        or ""
    ).strip()
    # Absence findings (missing_trigger/missing_section) describe something that
    # is *not* in the skill, so they carry no quotable evidence. The verbatim
    # grounding check is meaningless there; instead the fix must ADD content.
    # Every other category must still quote the skill verbatim and anchor on it.
    if not is_absence:
        if not evidence:
            return False, None, "missing verbatim evidence quote"
        if _norm_ws(evidence) not in _norm_ws(skill_text):
            return False, None, "evidence quote is not verbatim in skill text"
    if not body:
        return False, None, "empty fix"
    if body == skill_text.strip():
        return False, None, "fix is identical to existing skill text"
    if len(body) > max(MAX_FIX_CHARS, len(skill_text) * 2 + 5000):
        return False, None, "fix too long"
    if "\0" in body:
        return False, None, "fix contains NUL byte"
    if _PLACEHOLDER_RE.search(body):
        return False, None, "placeholder content not allowed"
    if _DANGEROUS_RE.search(body):
        return False, None, "dangerous execution pattern not allowed"
    if _SECRET_RE.search(body):
        return False, None, "leaked secret value not allowed"
    normalised = body.rstrip() + "\n"
    if is_absence:
        # The fix must genuinely ADD the missing trigger/section, not rewrite or
        # shrink the skill: require it to grow and to keep every existing line.
        if len(normalised.strip()) <= len(skill_text.strip()):
            return False, None, "absence fix must add content, not shorten the skill"
        # Per-line containment, NOT one contiguous substring: a real additive fix
        # usually inserts the new section in the MIDDLE (e.g. a trigger near the
        # top, before existing sections), which splits the original text. Require
        # instead that every existing non-blank line still appears somewhere in
        # the fix — this permits insertion anywhere while still catching deletion.
        after_norm = _norm_ws(normalised)
        for line in skill_text.splitlines():
            ln = _norm_ws(line)
            if ln and ln not in after_norm:
                return False, None, "absence fix must preserve the existing skill text"
        return True, normalised, None
    if not _fix_touches_evidence(skill_text, normalised, evidence):
        return False, None, "fix is not grounded in the evidence quote"
    return True, normalised, None


def _parse_judge_reply(content: str) -> dict[str, Any]:
    body = _strip_json_fence(content)
    data: Any | None = None
    try:
        data = json.loads(body)
    except ValueError:
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(body[start:end + 1])
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "judge returned invalid JSON",
        }
    resolved = data.get("resolved")
    no_regression = data.get("no_regression")
    reason = str(data.get("reason") or "").strip()
    if not isinstance(resolved, bool) or not isinstance(no_regression, bool):
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "judge returned non-boolean gate fields",
        }
    if not reason:
        reason = "judge accepted" if resolved and no_regression else "judge rejected without detail"
    return {
        "resolved": resolved,
        "no_regression": no_regression,
        "reason": reason[:800],
    }


def judge_fix(
    before_text: str,
    after_text: str,
    finding: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Ask the aux model to gate a proposed skill fix before applying it.

    Returns ``{resolved: bool, no_regression: bool, reason: str}``. Any model,
    parsing, or malformed-input problem is a closed gate.
    """
    if not isinstance(before_text, str) or not isinstance(after_text, str):
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "before_text/after_text must be strings",
        }
    if not isinstance(finding, dict):
        finding = {}
    if not before_text.strip() or not after_text.strip():
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "empty before_text or after_text",
        }
    if before_text == after_text:
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "proposal does not change the skill text",
        }
    if "\0" in after_text:
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "after_text contains NUL byte",
        }
    if _DANGEROUS_RE.search(after_text) or _SECRET_RE.search(after_text):
        return {
            "resolved": False,
            "no_regression": False,
            "reason": "after_text failed static safety screening",
        }

    system = (
        "Du bist der zweite, unabhaengige Eval-Gate fuer Hermes SKILL.md-Fixes. "
        "Pruefe streng, ob der vorgeschlagene After-Text die genannte Schwaeche "
        "wirklich behebt UND keine Regression in Klarheit, Safety, Scope, "
        "Prerequisites, Tool-Nutzung oder bestehendem Contract einfuehrt. "
        "Antworte ausschliesslich als JSON: "
        "{\"resolved\": true|false, \"no_regression\": true|false, \"reason\": \"kurz\"}. "
        "Setze beide Booleans nur dann auf true, wenn du sicher bist."
    )
    user = (
        "Finding / Schwäche:\n"
        f"{json.dumps(finding, ensure_ascii=False, indent=2)}\n\n"
        "BEFORE SKILL.md:\n"
        f"{before_text}\n\n"
        "AFTER SKILL.md:\n"
        f"{after_text}\n\n"
        "Entscheide das Gate."
    )
    try:
        resp = _call_llm(
            task="skills_hub",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=500,
            temperature=0.0,
            timeout=timeout,
        )
        return _parse_judge_reply(_extract_content(resp))
    except Exception as exc:
        return {
            "resolved": False,
            "no_regression": False,
            "reason": f"judge model call failed: {type(exc).__name__}",
        }


def _slice_from_header(text: str, section_header: str) -> str:
    """Drop non-``<think>`` preamble before the section (extract net).

    Reasoning models may prepend a sentence before the section. Take everything
    from the first ``## {header}`` (or, failing that, the first level-two header);
    leave the text untouched if none is found so validation rejects it and the
    caller falls back to the scaffold.
    """
    body = text.strip()
    idx = body.find(f"## {section_header}")
    if idx < 0:
        m = re.search(r"^##\s", body, re.M)
        idx = m.start() if m else -1
    return body[idx:].strip() if idx > 0 else body


def _normalise_section(text: str, section_header: str) -> str:
    body = text.strip()
    if not body.startswith(f"## {section_header}"):
        return body
    return "\n" + body.rstrip() + "\n"


def validate_section(text: str, section_header: str) -> tuple[bool, str | None, str | None]:
    body = text.strip()
    expected = f"## {section_header}"
    if not body.startswith(expected):
        return False, None, "missing expected section header"
    if len(_HEADER_RE.findall(body)) != 1:
        return False, None, "must contain exactly one level-two section header"
    if len(body) > MAX_CHARS:
        return False, None, "section too long"
    if len(body.splitlines()) > MAX_LINES:
        return False, None, "section has too many lines"
    if _FRONTMATTER_LINE_RE.search(body):
        return False, None, "frontmatter block marker not allowed"
    if _PLACEHOLDER_RE.search(body):
        return False, None, "placeholder content not allowed"
    if _DANGEROUS_RE.search(body):
        return False, None, "dangerous execution pattern not allowed"
    if _SECRET_RE.search(body):
        return False, None, "leaked secret value not allowed"
    return True, _normalise_section(body, section_header), None


def draft_section(skill_name: str, section_header: str, skill_text: str,
                  *, timeout: float = 120.0) -> dict:
    """Ask the aux model to write one complete SKILL.md section."""
    system = (
        "Du schreibst einen fehlenden Abschnitt einer Skill-Doku fertig aus. "
        f"Antworte mit GENAU EINEM Markdown-Abschnitt, der mit `## {section_header}` beginnt. "
        "Konkrete, sofort brauchbare Anleitung fuer DIESEN Skill - kein Platzhalter, kein TODO. "
        "Du darfst HOECHSTENS EIN kurzes Beispiel als Markdown-Codeblock einfuegen, wenn es den "
        "Abschnitt konkreter macht. Keine Befehle, die loeschen/ausfuehren/Rechte aendern "
        "(kein `rm -rf`, kein `sudo`, kein pipe-to-shell wie `curl ... | sh`), keine echten "
        "Secret-/Token-Werte. Beende den letzten Satz vollstaendig. Knapp: hoechstens ~150 Woerter."
    )
    user = (
        f"Skill: {skill_name}\n\n"
        "Vollstaendiger bisheriger Skill-Text:\n"
        f"{skill_text}\n\n"
        f"Schreibe den Abschnitt `## {section_header}`."
    )
    try:
        resp = _call_llm(
            task="skills_hub",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=1000,
            temperature=0.3,
            timeout=timeout,
        )
        content = _slice_from_header(_extract_content(resp), section_header)
        ok, text, reason = validate_section(content, section_header)
        if not ok:
            return {"ok": False, "text": None, "rationale": None, "reason": reason}
        model_label = _model_label_from_response(resp)
        return {
            "ok": True,
            "text": text,
            "rationale": f"{model_label} drafted section via skills_hub aux",
            "reason": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "text": None,
            "rationale": None,
            "reason": f"model call failed: {type(exc).__name__}",
        }


def draft_fix(
    skill_name: str,
    finding: dict[str, Any],
    skill_text: str,
    *,
    timeout: float = 120.0,
) -> dict:
    """Ask the aux model to write a grounded fix for one AR3 finding."""
    category = str(finding.get("category") or "").strip()
    is_absence = category in _ABSENCE_CATEGORIES
    evidence = str(finding.get("evidence") or finding.get("evidence_quote") or "").strip()
    fix_hint = str(finding.get("fix_hint") or "").strip()
    problem = str(finding.get("problem") or "").strip()
    # Absence findings legitimately have no evidence quote (the thing is missing);
    # only enforce the verbatim guard for evidence-bearing categories.
    if not is_absence:
        if not evidence:
            return {"ok": False, "text": None, "rationale": None, "reason": "missing verbatim evidence quote"}
        if _norm_ws(evidence) not in _norm_ws(skill_text):
            return {"ok": False, "text": None, "rationale": None, "reason": "evidence quote is not verbatim in skill text"}
    if is_absence:
        system = (
            "Du ergaenzt eine FEHLENDE Stelle in einer Hermes SKILL.md "
            "(missing_trigger: es fehlt JEDE Angabe, WANN der Skill benutzt wird; "
            "missing_section: ein empfohlener Abschnitt fehlt ganz). Es gibt KEIN "
            "evidence-Zitat, weil das Gesuchte noch nicht existiert. Fuege den "
            "fehlenden Inhalt HINZU, ohne bestehenden Text zu loeschen oder "
            "umzuschreiben. Antworte als JSON mit `text` (vollstaendiger neuer "
            "SKILL.md-Inhalt = bisheriger Text PLUS Ergaenzung) und `rationale`. "
            "Kein Markdown-Fence um JSON. Keine destruktiven Shell-Befehle, kein "
            "`sudo`, kein pipe-to-shell, keine echten Secret-/Token-Werte, keine "
            "Platzhalter oder TODOs."
        )
    else:
        system = (
            "Du reparierst eine konkrete Schwaeche in einer Hermes SKILL.md. "
            "Nutze denselben skills_hub-Slot wie der Abschnittsschreiber. "
            "Der Fix MUSS am angegebenen evidence-Zitat ansetzen; aendere keine "
            "unabhaengigen Stellen. Antworte bevorzugt als JSON mit `text` "
            "(vollstaendiger neuer SKILL.md-Inhalt) und `rationale`. Alternativ "
            "darfst du JSON mit `old_text`, `new_text`, `rationale` liefern, wobei "
            "`old_text` das evidence-Zitat enthalten muss. Kein Markdown-Fence um JSON. "
            "Keine destruktiven Shell-Befehle, kein `sudo`, kein pipe-to-shell, keine "
            "echten Secret-/Token-Werte, keine Platzhalter oder TODOs."
        )
    evidence_line = (
        "Evidence: (keine — die Stelle fehlt; ergaenze sie additiv)\n\n"
        if is_absence
        else f"Evidence verbatim:\n{evidence}\n\n"
    )
    user = (
        f"Skill: {skill_name}\n"
        f"Kategorie: {category}\n"
        f"Problem: {problem}\n"
        f"{evidence_line}"
        f"Fix-Hinweis:\n{fix_hint}\n\n"
        "Vollstaendiger bisheriger Skill-Text:\n"
        f"{skill_text}\n\n"
        "Schreibe den konkreten, grounded Fix."
    )
    try:
        resp = _call_llm(
            task="skills_hub",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=2600,
            temperature=0.2,
            timeout=timeout,
        )
        after_text, _replacement, model_rationale = _parse_fix_reply(_extract_content(resp), skill_text)
        if not isinstance(after_text, str):
            return {"ok": False, "text": None, "rationale": None, "reason": "invalid fix response"}
        ok, text, reason = validate_fix(after_text, finding, skill_text)
        if not ok:
            return {"ok": False, "text": None, "rationale": None, "reason": reason}
        model_label = _model_label_from_response(resp)
        rationale = model_rationale or f"{model_label} drafted grounded AR3 fix via skills_hub aux"
        if _DANGEROUS_RE.search(rationale) or _SECRET_RE.search(rationale):
            return {"ok": False, "text": None, "rationale": None, "reason": "unsafe rationale"}
        return {"ok": True, "text": text, "rationale": rationale, "reason": None}
    except Exception as exc:
        return {
            "ok": False,
            "text": None,
            "rationale": None,
            "reason": f"model call failed: {type(exc).__name__}",
        }

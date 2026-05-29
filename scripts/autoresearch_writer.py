#!/usr/bin/env python3
"""MiniMax-backed section writer for Hermes Autoresearch proposals.

AR1 drafts ONE complete SKILL.md section via MiniMax-M2.7 (``skills_hub`` aux).

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

import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

MAX_CHARS = 2200
MAX_LINES = 60

_HEADER_RE = re.compile(r"^##\s+.+$", re.M)
_FRONTMATTER_LINE_RE = re.compile(r"^---\s*$", re.M)
_PLACEHOLDER_RE = re.compile(r"\b(?:todo|placeholder|lorem ipsum)\b", re.I)
_REASONING_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.I | re.S)

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


def _extract_content(resp) -> str:
    raw = (resp.choices[0].message.content or "").strip()
    return _REASONING_RE.sub("", raw).strip()


def _slice_from_header(text: str, section_header: str) -> str:
    """Drop non-``<think>`` preamble before the section (extract net).

    MiniMax-M2.7 is a reasoning model and may prepend a sentence before the
    section. Take everything from the first ``## {header}`` (or, failing that,
    the first level-two header); leave the text untouched if none is found so
    validation rejects it and the caller falls back to the scaffold.
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
    """Ask MiniMax-M2.7 to write one complete SKILL.md section."""
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
        return {
            "ok": True,
            "text": text,
            "rationale": "MiniMax-M2.7 drafted section via skills_hub aux",
            "reason": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "text": None,
            "rationale": None,
            "reason": f"model call failed: {type(exc).__name__}",
        }

"""S7: Gemeinsame Titel-Destillation für Briefing, Inbox und Kontextpack.

Extrahiert aus dem S6.3-Morgen-Briefing (gateway/pa_watcher.briefing_title),
damit Entscheidungs-Strang und Ereignis-Strang denselben Klartext-Helper
teilen — keine Divergenz zwischen Karte und Inbox-Summary.
"""

from __future__ import annotations

import re

# S6/S7: Suffixe, die aus Titeln entfernt werden (Klartext ohne Status-Rauschen).
_BRIEFING_KIND_SUFFIX = re.compile(
    r"\s*[—–-]\s*(?:"
    r"completed|blocked|gave_up|crashed|timed_out|"
    r"operator_release_required|review_wait_attention|review_unavailable|"
    r"worker_gate_blocked|release_gate_parked|rebase_conflict_returned|"
    r"session_exit|new_receipt|blocked:\S+"
    r")\s*$",
    re.IGNORECASE,
)
_BRIEFING_TASK_PREFIX = re.compile(
    # S7.6-Fix: PlanSpec-Slug-Präfixe („PlanSpec GATE-…-FIX:") ebenfalls
    # strippen — sonst bleibt der laengste, nichtssagende Titelteil stehen
    # (Spiegel von control/jarvis/decisionTitle.ts).
    r"^(?:Task|Gate bei Task|PlanSpec)\s+\S+\s*[:：]\s*",
    re.IGNORECASE,
)
_BRIEFING_TASK_ID = re.compile(r"\bt_[0-9a-f]{6,}\b", re.IGNORECASE)
_BRIEFING_PATH = re.compile(r"(?:/home/\S+|(?:[A-Za-z]:)?(?:/[\w.-]+)+)")

# S7.6: Inbox-Summary-Deckel (Frontend Decision-Cards).
INBOX_SUMMARY_LIMIT = 80
# S8: deterministische PlanSpec-WHY-Deckel und ehrliche Missing-Evidence-Fallbacks.
INBOX_WHY_LIMIT = 320
INBOX_DECLINE_LIMIT = 240
INBOX_WHY_FALLBACK = "Keine Begründung hinterlegt — Rohtitel prüfen."
INBOX_DECLINE_FALLBACK = "Keine Ablehnungsfolge hinterlegt — Rohquelle prüfen."
# S6.3: Briefing-Titel-Deckel (weiterhin 120 für die Karten-Zeilen).
BRIEFING_TITLE_LIMIT = 120


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def distill_title(raw_title: object, *, limit: int = INBOX_SUMMARY_LIMIT) -> str:
    """S7: Klartext-Titel ohne Task-IDs, Beleg-Pfade und Kind-Suffixe.

    ``limit`` steuert die Zeichenkappe (Inbox ≤80, Briefing 120).
    """
    text = str(raw_title or "").strip()
    text = _BRIEFING_TASK_PREFIX.sub("", text)
    text = _BRIEFING_KIND_SUFFIX.sub("", text)
    text = _BRIEFING_TASK_ID.sub("", text)
    text = _BRIEFING_PATH.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" \t-—–:;")
    return _bounded_text(text or "Ereignis", limit)


def briefing_title(raw_title: object) -> str:
    """S6-Kompatibilität: Destillation mit Briefing-Deckel (120 Zeichen)."""
    return distill_title(raw_title, limit=BRIEFING_TITLE_LIMIT)


_SECTION_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_SECTION_LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?"
    r"(ziel|goal|evidenz|evidence|warum|begründung|begruendung|rationale|"
    r"rollback|risiko|risk|anti[- ]?goal|ablehnungsfolge|consequence)"
    r"(?:\*\*)?\s*:\s*(.+)$",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


def _decision_section_kind(label: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9äöüß-]+", " ", label.casefold()).strip()
    if re.search(r"\b(ziel|goal)\b", normalized):
        return "goal"
    if re.search(r"\b(evidenz|evidence)\b", normalized):
        return "evidence"
    if re.search(r"\b(warum|begründung|begruendung|rationale)\b", normalized):
        return "rationale"
    if re.search(r"\brollback\b", normalized):
        return "rollback"
    if re.search(r"\b(risiko|risk)\b", normalized):
        return "risk"
    if re.search(r"\b(anti-?goal|ablehnungsfolge|consequence)\b", normalized):
        return "anti_goal"
    return None


def _clean_decision_prose(value: str) -> str:
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", text).strip()


def _decision_excerpt(parts: list[str], *, sentences: int, limit: int) -> str:
    prose = _clean_decision_prose(" ".join(parts))
    if not prose:
        return ""
    selected = " ".join(_SENTENCE_BOUNDARY_RE.split(prose)[:sentences])
    return _bounded_text(selected, limit)


def distill_decision_why(body: object | None) -> tuple[str, str]:
    """Extract deterministic WHY copy from PlanSpec-style markdown sections.

    Only stored goal/evidence and rollback/risk/anti-goal prose is condensed.
    Missing sections get explicit fallbacks; no rationale is inferred.
    """
    if body is None:
        return INBOX_WHY_FALLBACK, INBOX_DECLINE_FALLBACK
    text = str(body).strip()
    if not text:
        return INBOX_WHY_FALLBACK, INBOX_DECLINE_FALLBACK

    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _SECTION_HEADING_RE.match(stripped)
        if heading:
            current = _decision_section_kind(heading.group(1))
            continue

        labelled = _SECTION_LABEL_RE.match(raw_line)
        if labelled:
            current = _decision_section_kind(labelled.group(1))
            if current is not None:
                sections.setdefault(current, []).append(labelled.group(2))
            continue

        if current is not None and stripped:
            sections.setdefault(current, []).append(stripped)

    why_parts: list[str] = []
    for kind in ("goal", "evidence"):
        excerpt = _decision_excerpt(
            sections.get(kind, []), sentences=1, limit=INBOX_WHY_LIMIT
        )
        if excerpt:
            why_parts.append(excerpt)
    if not why_parts:
        rationale = _decision_excerpt(
            sections.get("rationale", []), sentences=2, limit=INBOX_WHY_LIMIT
        )
        if rationale:
            why_parts.append(rationale)
    why = _decision_excerpt(why_parts, sentences=2, limit=INBOX_WHY_LIMIT)

    decline_parts: list[str] = []
    for kind in ("rollback", "risk", "anti_goal"):
        excerpt = _decision_excerpt(
            sections.get(kind, []), sentences=1, limit=INBOX_DECLINE_LIMIT
        )
        if excerpt:
            decline_parts.append(excerpt)
    consequence = _decision_excerpt(
        decline_parts, sentences=2, limit=INBOX_DECLINE_LIMIT
    )

    return (
        why or INBOX_WHY_FALLBACK,
        consequence or INBOX_DECLINE_FALLBACK,
    )

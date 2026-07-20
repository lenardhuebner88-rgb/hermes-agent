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
    r"^(?:Task|Gate bei Task)\s+\S+\s*[:：]\s*",
    re.IGNORECASE,
)
_BRIEFING_TASK_ID = re.compile(r"\bt_[0-9a-f]{6,}\b", re.IGNORECASE)
_BRIEFING_PATH = re.compile(r"(?:/home/\S+|(?:[A-Za-z]:)?(?:/[\w.-]+)+)")

# S7.6: Inbox-Summary-Deckel (Frontend Decision-Cards).
INBOX_SUMMARY_LIMIT = 80
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

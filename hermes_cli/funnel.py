"""Demand-Funnel-Helfer: Cap, Dedupe-Keys, Alter und Auto-Archiv.

Vorschläge aus den Funnel-Quellen (HermesBar/Familie, Discord ``idee:``,
fo-gap-audit) sind normale Kanban-Tasks in ``triage`` mit einem der
:data:`hermes_cli.kanban_db.FUNNEL_CREATED_BY`-Autoren. Dieses Modul bündelt
die Steuer-Logik, die laut Planspec in den **Code** gehört (nie nur in
Prompts): Cap 15 offene Vorschläge, Auto-Archiv nach 30 Tagen, stabile
Dedupe-Keys. Die fo-brain Cron-Scripts (fo-gap-audit, Sonntags-Digest im
Morgenbrief) importieren es auf dem venv-Python.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import List, Optional

from hermes_cli import kanban_db as kb

FUNNEL_CAP = 15
MAX_AGE_DAYS = 30

_KEY_LIMIT = 120

# --- Spend-Disclosure-Gate --------------------------------------------------
# Erkennt Drafts, die externe bezahlte Modelle/Provider nennen, aber keinen
# expliziten Kosten-Abschnitt enthalten.

_SPEND_SIGNAL_RE = re.compile(
    r"openrouter|benchmark|api[- ]?key|anthropic/|openai/|gpt-5|"
    r"fable-5|claude-fable|deepseek|qwen|minimax|"
    r"provider[- ]?sweep|modellvergleich|model[- ]?comparison",
    re.IGNORECASE,
)

# Markdown-tolerant: matcht auch "**Kosten & Provider:**", "## Kosten:",
# "- Kosten: …" — nicht aber "Kostenlos:" (\b) oder beliebigen Fließtext.
_COST_DISCLOSURE_RE = re.compile(
    r"^[\s>*#-]*(?:\*\*)?\s*(?:Kosten|Budget|Cost)\b[^:\n]{0,40}:",
    re.IGNORECASE | re.MULTILINE,
)


def spend_disclosure_missing(title: str, text: Optional[str]) -> bool:
    """True, wenn der Draft Spend-Signale enthält, aber keinen Kosten-Abschnitt.

    Reine Text-Variante; der Freigabe-Pfad nutzt
    :func:`_spend_disclosure_missing_task`, die zusätzlich alle Kommentare
    des Tasks als Disclosure-Quelle akzeptiert.
    """
    haystack = f"{title or ''}\n{text or ''}"
    if not _SPEND_SIGNAL_RE.search(haystack):
        return False
    return not bool(_COST_DISCLOSURE_RE.search(haystack))


def _spend_disclosure_missing_task(
    conn: sqlite3.Connection,
    task: "kb.Task",
    text: Optional[str],
) -> bool:
    """Spend-Gate auf Task-Ebene: Signal im Titel/kanonischen Draft,
    Disclosure zählt aus Titel, Draft, Task-Body und JEDEM Kommentar.

    Kommentare unter der ``_DRAFT_EXCERPT_MIN``-Schwelle ersetzen den
    kanonischen Draft nicht — eine kurze Zeile ``Kosten: …`` erfüllt damit
    das Gate, ohne die Spec im freigegebenen Kind-Task zu verdrängen.
    """
    if not spend_disclosure_missing(task.title or "", text):
        return False
    if _COST_DISCLOSURE_RE.search(task.body or ""):
        return False
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task.id,),
    ).fetchall()
    return not any(_COST_DISCLOSURE_RE.search(r["body"] or "") for r in rows)


def wish_key(text: str) -> str:
    """Stabiler Dedupe-Key: ``wish:`` + lowercase, Whitespace kollabiert."""
    norm = re.sub(r"\s+", " ", text or "").strip().lower()[:_KEY_LIMIT]
    return f"wish:{norm}"


def open_proposals(conn: sqlite3.Connection) -> List[dict]:
    """Offene Funnel-Vorschläge (status=triage), älteste zuerst."""
    placeholders = ",".join("?" for _ in kb.FUNNEL_CREATED_BY)
    rows = conn.execute(
        "SELECT id, title, created_by, created_at FROM tasks "
        f"WHERE status = 'triage' AND created_by IN ({placeholders}) "
        "ORDER BY created_at ASC",
        kb.FUNNEL_CREATED_BY,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def cap_reached(conn: sqlite3.Connection, *, cap: int = FUNNEL_CAP) -> bool:
    return len(open_proposals(conn)) >= cap


def stale_proposals(
    conn: sqlite3.Connection,
    *,
    max_age_days: int = MAX_AGE_DAYS,
    now: Optional[int] = None,
) -> List[dict]:
    """Offene Vorschläge, die älter als ``max_age_days`` sind."""
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max_age_days * 86400
    return [
        p for p in open_proposals(conn)
        if p["created_at"] is not None and int(p["created_at"]) <= cutoff
    ]


def archive_stale(
    conn: sqlite3.Connection,
    *,
    max_age_days: int = MAX_AGE_DAYS,
    now: Optional[int] = None,
) -> List[dict]:
    """Auto-Archiv: alte offene Vorschläge archivieren, archivierte zurückgeben."""
    archived: List[dict] = []
    for p in stale_proposals(conn, max_age_days=max_age_days, now=now):
        try:
            if kb.archive_task(conn, p["id"]):
                archived.append(p)
        except kb.WaitMutationConflict:
            # The domain guard already wrote a bounded refusal event.  One
            # protected proposal must not abort archival of unrelated stale
            # proposals in this maintenance batch.
            continue
    return archived


# --- Freigabe-Pfad: fertiger Draft → Build-Task (Operator-Klick) -----------

DRAFT_WINDOW_DAYS = 30
_DRAFT_EXCERPT_MIN = 120
_DRAFT_EXCERPT_MAX = 1500
_DRAFT_TEXT_MAX = 12_000
_OPERATOR_EDIT_MAX = 60_000
_BUILD_TITLE_LIMIT = 80

OPERATOR_EDIT_MARKER = "# Operator-edited PlanSpec"
REVISION_REQUEST_MARKER = "Revision angefordert"

APPROVE_BODY_TEMPLATE = (
    "Freigegebener Funnel-Draft — jetzt umsetzen.\n\n"
    "Ursprünglicher Wunsch: „{title}“ ({task_id}, Quelle: {created_by}).\n\n"
    "Draft (letzter Stand aus dem Ursprungs-Task):\n{excerpt}\n\n"
    "Anweisungen: Setze GENAU den freigegebenen Draft um — Referenzen stehen "
    "im Ursprungs-Task (Kommentare) bzw. im dort genannten Backlog-Item. "
    "Gates der Ziel-Lane fahren (Tests/Build); Push/Deploy nur bei grün gemäß "
    "Lane-Governance. Bei unklarem Scope: blocken statt raten."
)

REVISION_BODY_TEMPLATE = (
    "Funnel-Draft zur Überarbeitung.\n\n"
    "Quelle: „{title}“ ({task_id}, created_by={created_by}).\n\n"
    "Aktuelle Plan-Spec / Operator-Fassung:\n{draft_text}\n\n"
    "Operator-Input:\n{operator_note}\n\n"
    "Done-Condition: Überarbeite die Plan-Spec so, dass der Operator-Input "
    "explizit abgedeckt ist. Kommentiere die überarbeitete Plan-Spec als "
    "letzten substantiellen Kommentar und complete den Task. Keine Build-"
    "Umsetzung starten; es geht nur um den revidierten Draft."
)


def draft_text(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    max_chars: int = _DRAFT_TEXT_MAX,
) -> Optional[str]:
    """Jüngster substanzieller Kommentar (= aktueller kanonischer Draft)."""
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for r in rows:
        body = (r["body"] or "").strip()
        if len(body) >= _DRAFT_EXCERPT_MIN and not body.startswith("BLOCKED:"):
            return body[:max(1, int(max_chars))]
    return None


def _draft_excerpt(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Abwärtskompatibler kurzer Ausschnitt des kanonischen Drafts."""
    return draft_text(conn, task_id, max_chars=_DRAFT_EXCERPT_MAX)


def _is_funnel_root(conn: sqlite3.Connection, task_id: str) -> bool:
    """True, wenn der Task keine Eltern hat (echter Funnel-Root).

    Build-Kinder erben ``created_by`` von der Quelle (Wert-Bilanz) — ohne
    diesen Check qualifizierte sich jedes fertige Build-Kind erneut als
    "Draft" und die Kette fraß sich selbst (Umsetzen: Umsetzen: …).
    """
    row = conn.execute(
        "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1", (task_id,),
    ).fetchone()
    return row is None


def _require_funnel_draft(conn: sqlite3.Connection, task_id: str) -> kb.Task:
    """Validate that ``task_id`` is currently actionable in the release queue."""
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} nicht gefunden")
    if (task.created_by or "") not in kb.FUNNEL_CREATED_BY:
        raise ValueError(f"{task_id} ist kein Funnel-Vorschlag (created_by={task.created_by!r})")
    if not _is_funnel_root(conn, task_id):
        raise ValueError(
            f"{task_id} ist kein Funnel-Root (hat Eltern) — Build-Kinder "
            "gehören nicht in die Freigabe-Queue"
        )
    if task.status != "done":
        raise ValueError(f"{task_id} ist nicht fertig (status={task.status}) — erst der fertige Draft wird freigegeben")
    has_child = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? LIMIT 1", (task_id,),
    ).fetchone()
    if has_child:
        raise ValueError(f"{task_id} wurde bereits freigegeben (Build-Kind existiert)")
    return task


def _revision_of(body: Optional[str]) -> Optional[str]:
    if not body:
        return None
    match = re.search(r"Revision von:\s*(t_[A-Za-z0-9]+)", body)
    return match.group(1) if match else None


def draft_dict(conn: sqlite3.Connection, task: kb.Task) -> dict:
    text = draft_text(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "created_by": task.created_by,
        "assignee": task.assignee,
        "completed_at": task.completed_at,
        "draft_excerpt": _draft_excerpt(conn, task.id),
        "draft_text": text,
        "operator_edited": bool(text and text.startswith(OPERATOR_EDIT_MARKER)),
        "revision_of": _revision_of(task.body),
        "spend_alert": _spend_disclosure_missing_task(conn, task, text),
    }


def _format_operator_edit(draft_text: str, operator_note: str = "") -> str:
    body = f"{OPERATOR_EDIT_MARKER}\n\n{draft_text.strip()}"
    note = (operator_note or "").strip()
    if note:
        body += f"\n\n---\nOperator-Input:\n{note}"
    return body


def _validate_operator_draft_text(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("draft_text darf nicht leer sein")
    if len(text) > _OPERATOR_EDIT_MAX:
        raise ValueError(f"draft_text ist zu lang (max {_OPERATOR_EDIT_MAX} Zeichen)")
    return text


def _revision_title(title: str) -> str:
    base = title if (title or "").startswith("Überarbeiten: ") else f"Überarbeiten: {title}"
    if len(base) > _BUILD_TITLE_LIMIT:
        return base[: _BUILD_TITLE_LIMIT - 1].rstrip() + "…"
    return base


def list_drafts(
    conn: sqlite3.Connection,
    *,
    days: int = DRAFT_WINDOW_DAYS,
    now: Optional[int] = None,
) -> List[dict]:
    """Fertige Funnel-Roots ohne Build-Kind — sie warten auf die Freigabe.

    Root heißt: keine Eltern. Damit sind die per :func:`approve_draft`
    erzeugten Build-Kinder (gleiches ``created_by``, irgendwann ``done``)
    hart ausgeschlossen — sonst loopt der Trichter über seine eigene
    Ausgabe. Nach der Freigabe hat der Root ein verlinktes Build-Kind und
    fällt aus dieser Liste; die Kette übernimmt das Flow-Board.
    """
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max(1, int(days)) * 86400
    placeholders = ",".join("?" for _ in kb.FUNNEL_CREATED_BY)
    rows = conn.execute(
        "SELECT * FROM tasks "
        f"WHERE created_by IN ({placeholders}) AND status = 'done' "
        "AND completed_at IS NOT NULL AND completed_at >= ? "
        "AND id NOT IN (SELECT DISTINCT parent_id FROM task_links) "
        "AND id NOT IN (SELECT DISTINCT child_id FROM task_links) "
        "ORDER BY completed_at DESC",
        (*kb.FUNNEL_CREATED_BY, cutoff),
    ).fetchall()
    return [
        draft_dict(conn, kb.Task.from_row(r))
        for r in rows
    ]


def approve_draft(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    assignee_fallback: str = "premium",
) -> str:
    """Freigabe: Build-Task als verlinktes Kind des Draft-Roots anlegen.

    Das Kind erbt ``created_by`` (die Wert-Bilanz zählt die Kette einmal als
    nutzer — der Root wird interior, das Kind der neue Sink) und startet als
    ``ready`` (Parent ist done) — der Dispatcher übernimmt. Raises ValueError
    mit Begründung, wenn der Task kein freigabefähiger Draft ist.
    """
    task = _require_funnel_draft(conn, task_id)

    # Spend-Disclosure-Gate: Draft nennt externe Modelle/Provider → expliziter
    # Kosten-Abschnitt ist Pflicht, bevor die Freigabe greift.
    if _spend_disclosure_missing_task(conn, task, draft_text(conn, task_id)):
        raise ValueError(
            "Draft nennt externe Modelle/Provider, aber keinen Kosten-Abschnitt. "
            "Ergänze am Ursprungs-Task eine kurze Kommentar-Zeile "
            "'Kosten: … — Provider/Modelle: …' (oder bearbeite den Draft) "
            "und gib dann erneut frei."
        )

    # Präfix idempotent halten: tippt die Familie den Wunsch selbst schon als
    # "Umsetzen: …", darf der Build-Titel nicht weiter stapeln.
    title = task.title if (task.title or "").startswith("Umsetzen: ") else f"Umsetzen: {task.title}"
    if len(title) > _BUILD_TITLE_LIMIT:
        title = title[: _BUILD_TITLE_LIMIT - 1].rstrip() + "…"
    excerpt = _draft_excerpt(conn, task_id) or (
        "(kein Draft-Kommentar gefunden — Referenzen im Ursprungs-Task prüfen)"
    )
    new_id = kb.create_task(
        conn,
        title=title,
        body=APPROVE_BODY_TEMPLATE.format(
            title=task.title, task_id=task_id,
            created_by=task.created_by, excerpt=excerpt,
        ),
        created_by=task.created_by,
        assignee=task.assignee or assignee_fallback,
        parents=(task_id,),
        kind="code",
    )
    kb.ensure_code_task_contract_before_pickup(
        conn, new_id, source="funnel.approve_draft",
    )
    return new_id


def dismiss_draft(conn: sqlite3.Connection, task_id: str) -> None:
    """Verwerfen: Draft-Root archivieren (Operator-Entscheid, kein Build).

    Gleiche Gültigkeitsregeln wie :func:`approve_draft` — nur Einträge der
    Freigabe-Queue (fertiger Funnel-Root ohne Build-Kind) sind verwerfbar.
    """
    _require_funnel_draft(conn, task_id)
    kb.preflight_task_removals(
        conn, [task_id], operation="funnel_dismiss_draft"
    )
    if not kb.archive_task(conn, task_id):
        raise RuntimeError(f"Funnel draft {task_id} changed state during dismissal")
    kb.add_comment(conn, task_id, "operator",
                   "Verworfen über die Funnel-Freigabe-Queue — kein Build.")


def save_draft_edit(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    draft_text: str,
    operator_note: str = "",
) -> dict:
    """Persist an operator-edited PlanSpec as the newest canonical draft."""
    task = _require_funnel_draft(conn, task_id)
    text = _validate_operator_draft_text(draft_text)
    kb.add_comment(conn, task_id, "operator", _format_operator_edit(text, operator_note))
    return draft_dict(conn, task)


def request_revision(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    draft_text: str,
    operator_note: str = "",
    assignee_fallback: str = "coder",
) -> str:
    """Archive the current draft and create a new root for re-specification."""
    task = _require_funnel_draft(conn, task_id)
    kb.preflight_task_removals(
        conn, [task_id], operation="funnel_request_revision"
    )
    text = _validate_operator_draft_text(draft_text)
    note = (operator_note or "").strip() or "(kein separater Operator-Input)"
    new_id = kb.create_task(
        conn,
        title=_revision_title(task.title),
        body=(
            f"Revision von: {task_id}\n\n" + REVISION_BODY_TEMPLATE.format(
                title=task.title,
                task_id=task_id,
                created_by=task.created_by,
                draft_text=text,
                operator_note=note,
            )
        ),
        created_by=task.created_by,
        assignee=task.assignee or assignee_fallback,
        parents=(),
    )
    try:
        archived = kb.archive_task(conn, task_id)
    except Exception:
        # Compensate the newly-created, still-unlinked replacement so a late
        # wait conflict cannot leave two live Funnel roots.
        kb.delete_task(conn, new_id)
        raise
    if not archived:
        kb.delete_task(conn, new_id)
        raise RuntimeError(f"Funnel draft {task_id} changed state during revision")
    kb.add_comment(conn, task_id, "operator", f"{REVISION_REQUEST_MARKER} → {new_id}")
    return new_id


def create_wish(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    created_by: str,
    key: Optional[str] = None,
    cap: int = FUNNEL_CAP,
    assignee: Optional[str] = None,
) -> Optional[str]:
    """Funnel-Vorschlag anlegen (triage, dedupe). None, wenn das Cap greift.

    Der Cap-Guard sitzt hier im Code — ein voller Trichter lehnt neue
    Vorschläge ab, statt das Board zu fluten.
    """
    if created_by not in kb.FUNNEL_CREATED_BY:
        raise ValueError(f"created_by must be one of {kb.FUNNEL_CREATED_BY}")
    if cap_reached(conn, cap=cap):
        return None
    return kb.create_task(
        conn,
        title=title,
        body=body,
        created_by=created_by,
        assignee=assignee,
        triage=True,
        idempotency_key=key or wish_key(title),
    )

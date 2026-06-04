#!/usr/bin/env python3
"""Family Organizer tool — Hermes als NL-Brain hinter der HermesBar.

Schreibt über die geschützte Family-Organizer-Hermes-API (``/api/hermes/*``)
in echte Familiendaten: Listen-Items anlegen und Anwesenheit setzen. Das ist
**Tür B** des Brain-Modells (ADR-0004 / backlog 0077): der bestehende
Bearer-Service-Token, die ``X-Request-Id``-Idempotenz und das serverseitige
Audit werden **unverändert** wiederverwendet — diese Tür wird nicht geschwächt.

Write-Actions: ``add_task_item`` (0077), ``set_presence`` (0077),
``create_event`` (Termin, 0081), ``add_birthday`` (Geburtstag, 0087) und
``set_meal_plan`` (Mittagessen, 0088) — die Entitäts-Sub-Slices aus 0078.
Dazu zwei Read-Helfer (Listen, Anwesenheit) zum Auflösen von Listennamen und
zum Beantworten von Fragen. Weiterhin nur **anlegen/setzen** — kein Update/Delete.

Konfiguration (Environment):
  FAMILY_ORGANIZER_BASE_URL  Basis-URL der FO-App (Default: http://127.0.0.1:3000)
  HERMES_SERVICE_TOKEN       Bearer-Token für /api/hermes/* (Pflicht)
"""

import logging
import os
import uuid
from typing import Any, Dict, Optional

import httpx  # noqa: F401 — module-level so tests can patch tools.family_organizer_tool.httpx

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:3000"
_TIMEOUT = 10.0

# Vertrag mit der FO-Hermes-API (src/app/api/hermes/_lib/schemas.ts).
MEMBER_ROLES = ("papa", "mama", "kind_1", "kind_2")
PRESENCE_STATUSES = (
    "homeoffice",
    "office",
    "school",
    "kita",
    "home",
    "away",
    "unknown",
)


def _base_url() -> str:
    return (os.getenv("FAMILY_ORGANIZER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _service_token() -> str:
    return (os.getenv("HERMES_SERVICE_TOKEN") or "").strip()


def _headers(*, write: bool) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_service_token()}",
        "Content-Type": "application/json",
    }
    if write:
        # Idempotenz-Schlüssel; die FO-API verlangt eine kanonische UUID.
        headers["X-Request-Id"] = str(uuid.uuid4())
    return headers


def _request(
    method: str,
    path: str,
    *,
    write: bool,
    json_body: Optional[dict] = None,
) -> Any:
    """Low-level call to the FO Hermes API. Returns parsed JSON or raises."""
    if not _service_token():
        raise RuntimeError("HERMES_SERVICE_TOKEN ist nicht gesetzt")
    url = f"{_base_url()}{path}"
    resp = httpx.request(
        method,
        url,
        headers=_headers(write=write),
        json=json_body,
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        try:
            detail: Any = resp.json()
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(f"FO-API {method} {path} -> {resp.status_code}: {detail}")
    if resp.content:
        return resp.json()
    return None


def _available_list_names() -> list:
    try:
        data = _request("GET", "/api/hermes/lists", write=False)
        return [item.get("name") for item in (data or {}).get("lists", [])]
    except Exception:
        return []


def _resolve_list_id(list_name: str) -> Optional[str]:
    data = _request("GET", "/api/hermes/lists", write=False)
    target = list_name.strip().casefold()
    for item in (data or {}).get("lists", []):
        if str(item.get("name", "")).strip().casefold() == target:
            return item.get("id")
    return None


# ─── Read-Helfer ─────────────────────────────────────────────────────────────

def fo_list_lists() -> str:
    """Liste alle Familienlisten (Name + ID + Item-Anzahl)."""
    try:
        data = _request("GET", "/api/hermes/lists", write=False)
    except Exception as exc:
        return tool_error(str(exc))
    lists = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "kind": item.get("kind"),
            "itemCount": item.get("itemCount"),
        }
        for item in (data or {}).get("lists", [])
    ]
    return tool_result({"lists": lists})


def fo_list_presence(date: str) -> str:
    """Lies die Anwesenheit aller Familienmitglieder für ein Datum (YYYY-MM-DD)."""
    date = (date or "").strip()
    if not date:
        return tool_error("date (YYYY-MM-DD) fehlt")
    try:
        data = _request("GET", f"/api/hermes/presence?date={date}", write=False)
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result(data or {"presence": []})


# ─── Write-Helfer (auditiert, idempotent, token-geschützt) ───────────────────

def fo_create_task(
    list_name: str,
    title: str,
    notes: Optional[str] = None,
    due_date: Optional[str] = None,
) -> str:
    """Lege ein Item in einer Familienliste an (z. B. 'Milch' auf 'Einkauf')."""
    list_name = (list_name or "").strip()
    title = (title or "").strip()
    if not list_name:
        return tool_error("list_name fehlt")
    if not title:
        return tool_error("title fehlt")
    try:
        list_id = _resolve_list_id(list_name)
        if not list_id:
            return tool_error(
                f"Liste '{list_name}' nicht gefunden",
                available_lists=_available_list_names(),
            )
        body: Dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due_date:
            body["dueDate"] = due_date
        data = _request(
            "POST",
            f"/api/hermes/lists/{list_id}/items",
            write=True,
            json_body=body,
        )
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result(
        {"created": True, "list": list_name, "item": (data or {}).get("item", {})}
    )


def fo_set_presence(member_role: str, status: str, date: str) -> str:
    """Setze die Anwesenheit eines Mitglieds für ein Datum.

    member_role: papa | mama | kind_1 | kind_2
    status:      homeoffice | office | school | kita | home | away | unknown
    date:        YYYY-MM-DD
    """
    member_role = (member_role or "").strip().lower()
    status = (status or "").strip().lower()
    date = (date or "").strip()
    if member_role not in MEMBER_ROLES:
        return tool_error(
            f"member_role muss eines von {list(MEMBER_ROLES)} sein", got=member_role
        )
    if status not in PRESENCE_STATUSES:
        return tool_error(
            f"status muss eines von {list(PRESENCE_STATUSES)} sein", got=status
        )
    if not date:
        return tool_error("date (YYYY-MM-DD) fehlt")
    try:
        data = _request(
            "POST",
            "/api/hermes/presence",
            write=True,
            json_body={"date": date, "memberRole": member_role, "status": status},
        )
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result({"updated": True, "presence": (data or {}).get("presence", {})})


def fo_create_event(
    title: str,
    date: str,
    time: Optional[str] = None,
    notes: Optional[str] = None,
    member_role: Optional[str] = None,
) -> str:
    """Lege einen Familientermin an (z. B. 'Zahnarzt Oskar' am 14.06 um 15:30).

    title:       Titel des Termins (Pflicht)
    date:        YYYY-MM-DD (Pflicht) — relative Angaben vorher ausrechnen
    time:        HH:MM (optional); ohne Uhrzeit wird der Termin ganztägig
    notes:       optionale Notiz
    member_role: papa | mama | kind_1 | kind_2 (optional)
    """
    title = (title or "").strip()
    date = (date or "").strip()
    time = (time or "").strip()
    member_role = (member_role or "").strip().lower()
    if not title:
        return tool_error("title fehlt")
    if not date:
        return tool_error("date (YYYY-MM-DD) fehlt")
    if member_role and member_role not in MEMBER_ROLES:
        return tool_error(
            f"member_role muss eines von {list(MEMBER_ROLES)} sein", got=member_role
        )
    try:
        body: Dict[str, Any] = {"title": title, "date": date}
        if time:
            body["time"] = time
        if notes:
            body["notes"] = notes
        if member_role:
            body["memberRole"] = member_role
        data = _request("POST", "/api/hermes/events", write=True, json_body=body)
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result({"created": True, "event": (data or {}).get("event", {})})


def fo_add_birthday(
    name: str,
    date: str,
    notes: Optional[str] = None,
) -> str:
    """Lege einen Geburtstag an (z. B. 'Oma' am 14.06).

    name:  Name der Person, deren Geburtstag eingetragen wird (Pflicht)
    date:  YYYY-MM-DD (Pflicht) — relative/teildatierte Angaben vorher ausrechnen
    notes: optionale Notiz
    """
    name = (name or "").strip()
    date = (date or "").strip()
    if not name:
        return tool_error("name fehlt")
    if not date:
        return tool_error("date (YYYY-MM-DD) fehlt")
    try:
        body: Dict[str, Any] = {"name": name, "date": date}
        if notes:
            body["notes"] = notes
        data = _request("POST", "/api/hermes/birthdays", write=True, json_body=body)
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result({"created": True, "birthday": (data or {}).get("birthday", {})})


def fo_set_meal_plan(
    date: str,
    title: str,
    notes: Optional[str] = None,
) -> str:
    """Trage das Mittagessen für ein Datum ein (z. B. 'Pizza' am Montag).

    date:  YYYY-MM-DD (Pflicht) — relative Angaben vorher ausrechnen
    title: Gericht (Pflicht)
    notes: optionale Notiz

    Upsert pro Datum: ein bestehender Eintrag für denselben Tag wird überschrieben,
    nicht dupliziert.
    """
    date = (date or "").strip()
    title = (title or "").strip()
    if not date:
        return tool_error("date (YYYY-MM-DD) fehlt")
    if not title:
        return tool_error("title fehlt")
    try:
        body: Dict[str, Any] = {"date": date, "title": title}
        if notes:
            body["notes"] = notes
        data = _request("POST", "/api/hermes/meal-plans", write=True, json_body=body)
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result({"updated": True, "mealPlan": (data or {}).get("mealPlan", {})})


def check_family_organizer_requirements() -> bool:
    """Verfügbar, wenn die FO-Hermes-API /health mit unserem Token mit 200 antwortet."""
    if not _service_token():
        return False
    try:
        resp = httpx.get(
            f"{_base_url()}/api/hermes/health",
            headers={"Authorization": f"Bearer {_service_token()}"},
            timeout=5.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ─── Tool-Schemas (OpenAI function-calling) ──────────────────────────────────

_ROLE_HINT = (
    "Familien-Rollen: Papa=papa, Mama=mama, Oskar=kind_1, Fiete=kind_2."
)

FO_CREATE_TASK_SCHEMA = {
    "name": "fo_create_task",
    "description": (
        "Lege ein neues Item in einer Familienliste an (z. B. 'Milch' auf der "
        "Liste 'Einkauf'). Nutze fo_list_lists, wenn du den genauen Listennamen "
        "nicht kennst. Schreibt direkt in die echten Familiendaten."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Name der Zielliste, z. B. 'Einkauf' oder 'Familie'.",
            },
            "title": {"type": "string", "description": "Text des Listen-Items."},
            "notes": {"type": "string", "description": "Optionale Notiz."},
            "due_date": {
                "type": "string",
                "description": "Optionales Fälligkeitsdatum im Format YYYY-MM-DD.",
            },
        },
        "required": ["list_name", "title"],
    },
}

FO_SET_PRESENCE_SCHEMA = {
    "name": "fo_set_presence",
    "description": (
        "Setze die Anwesenheit eines Familienmitglieds für ein Datum. "
        + _ROLE_HINT
        + " Das Datum muss als YYYY-MM-DD übergeben werden — rechne relative "
        "Angaben wie 'morgen' anhand des heutigen Datums aus dem Kontext aus."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "member_role": {
                "type": "string",
                "enum": list(MEMBER_ROLES),
                "description": "Rolle des Mitglieds. " + _ROLE_HINT,
            },
            "status": {
                "type": "string",
                "enum": list(PRESENCE_STATUSES),
                "description": "Anwesenheits-Status.",
            },
            "date": {
                "type": "string",
                "description": "Datum im Format YYYY-MM-DD.",
            },
        },
        "required": ["member_role", "status", "date"],
    },
}

FO_CREATE_EVENT_SCHEMA = {
    "name": "fo_create_event",
    "description": (
        "Lege einen Familientermin im Kalender an (z. B. 'Zahnarzt Oskar' am "
        "14.06. um 15:30). Schreibt direkt in die echten Familiendaten und ist "
        "danach unter /admin/termine und im Küchen-Dashboard sichtbar. " + _ROLE_HINT
        + " Das Datum muss als YYYY-MM-DD übergeben werden — rechne relative "
        "Angaben wie 'morgen' oder 'nächsten Montag' anhand des heutigen Datums "
        "aus dem Kontext aus. Ohne Uhrzeit wird der Termin ganztägig angelegt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Titel des Termins."},
            "date": {
                "type": "string",
                "description": "Datum im Format YYYY-MM-DD.",
            },
            "time": {
                "type": "string",
                "description": "Optionale Uhrzeit im Format HH:MM (ohne -> ganztägig).",
            },
            "notes": {"type": "string", "description": "Optionale Notiz."},
            "member_role": {
                "type": "string",
                "enum": list(MEMBER_ROLES),
                "description": "Optionale Zuordnung zu einem Mitglied. " + _ROLE_HINT,
            },
        },
        "required": ["title", "date"],
    },
}

FO_ADD_BIRTHDAY_SCHEMA = {
    "name": "fo_add_birthday",
    "description": (
        "Lege einen Geburtstag an (z. B. 'Oma' am 14.06.). Schreibt direkt in die "
        "echten Familiendaten und ist danach unter /admin/geburtstage und im "
        "Küchen-Dashboard sichtbar. Das Datum muss als YYYY-MM-DD übergeben werden — "
        "rechne relative oder teildatierte Angaben (z. B. '14.06.') anhand des "
        "heutigen Datums aus dem Kontext aus."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name der Person, deren Geburtstag eingetragen wird.",
            },
            "date": {"type": "string", "description": "Datum im Format YYYY-MM-DD."},
            "notes": {"type": "string", "description": "Optionale Notiz."},
        },
        "required": ["name", "date"],
    },
}

FO_SET_MEAL_PLAN_SCHEMA = {
    "name": "fo_set_meal_plan",
    "description": (
        "Trage das Mittagessen für einen Tag ein (z. B. 'Montag gibt's Pizza'). "
        "Schreibt direkt in die echten Familiendaten und ist danach im Essensplan "
        "und im Küchen-Dashboard sichtbar. Upsert pro Datum: ein bestehender "
        "Eintrag für denselben Tag wird überschrieben (kein Doppel-Eintrag). Das "
        "Datum muss als YYYY-MM-DD übergeben werden — rechne relative Angaben wie "
        "'Montag' anhand des heutigen Datums aus dem Kontext aus."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Datum im Format YYYY-MM-DD."},
            "title": {"type": "string", "description": "Das Gericht, z. B. 'Pizza'."},
            "notes": {"type": "string", "description": "Optionale Notiz."},
        },
        "required": ["date", "title"],
    },
}

FO_LIST_LISTS_SCHEMA = {
    "name": "fo_list_lists",
    "description": "Liste alle Familienlisten mit Namen, ID und Item-Anzahl.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

FO_LIST_PRESENCE_SCHEMA = {
    "name": "fo_list_presence",
    "description": (
        "Lies die Anwesenheit aller Familienmitglieder für ein Datum (YYYY-MM-DD)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Datum im Format YYYY-MM-DD."},
        },
        "required": ["date"],
    },
}


# ─── Registry ────────────────────────────────────────────────────────────────

registry.register(
    name="fo_create_task",
    toolset="family-organizer",
    schema=FO_CREATE_TASK_SCHEMA,
    handler=lambda args, **kw: fo_create_task(
        list_name=args.get("list_name", ""),
        title=args.get("title", ""),
        notes=args.get("notes"),
        due_date=args.get("due_date"),
    ),
    check_fn=check_family_organizer_requirements,
    emoji="📝",
)

registry.register(
    name="fo_set_presence",
    toolset="family-organizer",
    schema=FO_SET_PRESENCE_SCHEMA,
    handler=lambda args, **kw: fo_set_presence(
        member_role=args.get("member_role", ""),
        status=args.get("status", ""),
        date=args.get("date", ""),
    ),
    check_fn=check_family_organizer_requirements,
    emoji="🏠",
)

registry.register(
    name="fo_create_event",
    toolset="family-organizer",
    schema=FO_CREATE_EVENT_SCHEMA,
    handler=lambda args, **kw: fo_create_event(
        title=args.get("title", ""),
        date=args.get("date", ""),
        time=args.get("time"),
        notes=args.get("notes"),
        member_role=args.get("member_role"),
    ),
    check_fn=check_family_organizer_requirements,
    emoji="📅",
)

registry.register(
    name="fo_add_birthday",
    toolset="family-organizer",
    schema=FO_ADD_BIRTHDAY_SCHEMA,
    handler=lambda args, **kw: fo_add_birthday(
        name=args.get("name", ""),
        date=args.get("date", ""),
        notes=args.get("notes"),
    ),
    check_fn=check_family_organizer_requirements,
    emoji="🎂",
)

registry.register(
    name="fo_set_meal_plan",
    toolset="family-organizer",
    schema=FO_SET_MEAL_PLAN_SCHEMA,
    handler=lambda args, **kw: fo_set_meal_plan(
        date=args.get("date", ""),
        title=args.get("title", ""),
        notes=args.get("notes"),
    ),
    check_fn=check_family_organizer_requirements,
    emoji="🍽️",
)

registry.register(
    name="fo_list_lists",
    toolset="family-organizer",
    schema=FO_LIST_LISTS_SCHEMA,
    handler=lambda args, **kw: fo_list_lists(),
    check_fn=check_family_organizer_requirements,
    emoji="📋",
)

registry.register(
    name="fo_list_presence",
    toolset="family-organizer",
    schema=FO_LIST_PRESENCE_SCHEMA,
    handler=lambda args, **kw: fo_list_presence(date=args.get("date", "")),
    check_fn=check_family_organizer_requirements,
    emoji="👀",
)

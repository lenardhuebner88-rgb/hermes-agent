"""P6b — operator-bestätigtes Korrektur-Overlay für die Bibliothek-Herkunft.

Ein separates, versioniertes Overlay ÜBER der deterministischen P6a-Ableitung
(ADR 0001 / ADR 0002). Originale (Cron/Task/Receipt/Deliverable) bleiben
byte-unverändert; eine Korrektur lebt ausschließlich in einem profile-lokalen
JSON-Store unter ``get_hermes_home()/control/`` und überschreibt sichtbar nur
``producer``/``path``/die fünf Ketten-Rollen eines Items — keyed by stabiler
Item-Identifier. Ursprungswert, effektiver Wert, Grund, Zeitpunkt und Historie
bleiben technisch nachvollziehbar (Originalsnapshot + append-only History).

Grenzen (ehrlich dokumentiert, keine Schein-Security):
  * Die Dashboard-Authentisierung liefert im Loopback-Betrieb KEINE harte
    Benutzeridentität (ein gemeinsames Session-Token). Die Mutation hängt darum
    am bestehenden Session-Gate (Route unter ``/api/``, nie in PUBLIC_API_PATHS)
    UND an einer ausdrücklichen Operator-Bestätigung: ``confirm is True``
    fail-closed (Hausmuster ``autoresearch_proposals.apply_proposal`` /
    ``voice_health_track``) plus Pflicht-``reason``. ``actor`` ist fest
    ``"operator"`` = die authentifizierte Dashboard-Session.
  * Kein Agent-/Tool-Automatismus finalisiert eine Korrektur; es gibt genau
    diesen einen bestätigten Schreibpfad.

Concurreny: read-modify-write läuft unter einem exklusiven ``fcntl.flock`` auf
einer separaten Lockdatei; geschrieben wird atomar (mkstemp + fsync +
os.replace + chmod 0600), analog ``library_state``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

_STATE_VERSION = 1
_STORE_FILE = "library_provenance_corrections.json"
_LOCK_FILE = ".library_provenance_corrections.lock"

# Stabiler Item-Identifier: die bekannten Präfixe aus library_view._get_item.
_ITEM_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_:./\-]{1,320}$")
_ITEM_KINDS = ("cron::", "research::", "deliverable::", "receipt::")

# Korrigierbare Oberfläche — NUR diese Felder darf ein Override setzen.
# ``producer`` ist der Operator-Begriff für die Autor-Rolle (Erzeuger == Autor,
# siehe _build_provenance) und wird kanonisch auf ``autor`` abgebildet, damit
# producer/chain.autor nie auseinanderlaufen.
_AGENT_ROLES = ("auftraggeber", "delegation", "autor", "review")
_REF_ROLES = ("ablage",)  # Ablage ist ein technischer Ref, kein Erzeuger.
CANONICAL_FIELDS: tuple[str, ...] = ("path",) + _AGENT_ROLES + _REF_ROLES
_FIELD_ALIASES = {"producer": "autor"}

_MAX_REASON_LEN = 600
_MAX_AGENT_LEN = 160
_MAX_ABLAGE_LEN = 200
ACTOR_OPERATOR = "operator"

try:  # POSIX (Homeserver = Linux); andernfalls degeneriert das Lock zum No-op.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - nicht-POSIX
    _fcntl = None


# ---------------------------------------------------------------------------
# Store-Pfad + atomares, gelocktes Read-Modify-Write
# ---------------------------------------------------------------------------

def storage_path() -> Path:
    """Profile-lokaler JSON-Store — getrennt von allen Quelldokumenten."""
    return get_hermes_home() / "control" / _STORE_FILE


def _lock_path() -> Path:
    return get_hermes_home() / "control" / _LOCK_FILE


def _now() -> int:
    return int(time.time())


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "corrections": {}}


class CorrectionStoreError(RuntimeError):
    """Der persistierte Overlay-Store kann nicht verlustfrei fortgeschrieben werden."""


class _StoreLock:
    """Exklusives flock auf einer separaten Lockdatei (Context Manager).

    Schützt das read-modify-write vor gleichzeitigen Dashboard-/CLI-Schreibern.
    Ohne fcntl (nicht-POSIX) no-op — die atomare os.replace sichert dann
    zumindest Integrität, aber keine Serialisierung."""

    def __init__(self) -> None:
        self._fh: Optional[Any] = None

    def __enter__(self) -> "_StoreLock":
        if _fcntl is None:
            return self
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(lock_path, "a+", encoding="utf-8")
        _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def _read_state(*, for_write: bool = False) -> dict[str, Any]:
    """Lese den Store fail-soft für Anzeigen, aber strikt vor Mutationen.

    Ein unlesbarer oder unbekannter Store darf die Bibliothek-Ansicht nicht
    leeren. Vor einem Schreibvorgang muss derselbe Zustand jedoch vollständig
    interpretierbar sein; andernfalls würde ein vermeintlicher "Repair" die
    übrigen Records samt append-only-Historie überschreiben.
    """
    try:
        raw = json.loads(storage_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError, TypeError) as exc:
        if for_write:
            raise CorrectionStoreError(
                "correction store is unreadable; write refused"
            ) from exc
        return _empty_state()
    if (
        not isinstance(raw, dict)
        or raw.get("version") != _STATE_VERSION
        or not isinstance(raw.get("corrections"), dict)
    ):
        if for_write:
            raise CorrectionStoreError(
                "correction store format/version is unsupported; write refused"
            )
        return _empty_state()
    corrections: dict[str, Any] = {}
    malformed = False
    for item_id, record in raw["corrections"].items():
        if isinstance(item_id, str) and isinstance(record, dict):
            corrections[item_id] = record
        else:
            malformed = True
    if malformed and for_write:
        raise CorrectionStoreError(
            "correction store contains malformed records; write refused"
        )
    return {"version": _STATE_VERSION, "corrections": corrections}


def _write_state(state: dict[str, Any]) -> None:
    path = storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Validierung (rein) — bestehende Alias-/Unknown-Regeln, keine freien Werte
# ---------------------------------------------------------------------------

def validate_item_id(item_id: Any) -> str:
    if not isinstance(item_id, str) or not _ITEM_ID_RE.match(item_id):
        raise ValueError("invalid item id")
    if not item_id.startswith(_ITEM_KINDS):
        raise ValueError("unknown item kind")
    return item_id


def _validate_reason(reason: Any) -> str:
    text = str(reason or "").strip()
    if not text:
        raise ValueError("reason is required")
    if len(text) > _MAX_REASON_LEN:
        raise ValueError("reason is too long")
    return text


def validate_fields(raw: Any) -> dict[str, Optional[str]]:
    """Validiere ein Override-Dict gegen die bestehende P6a-Regelwelt.

    Gibt die kanonische Abbildung ``{feld: wert}`` zurück; ``wert is None``
    bedeutet "diese Korrektur entfernen" (leerer Wert hebt den Override auf).
    Unbekannte Felder, freie Wegtypen oder ein producer/autor-Widerspruch
    werfen ``ValueError`` — es werden keine Werte erfunden."""
    from hermes_cli.library_view import PATH_VALUES, normalize_producer

    if not isinstance(raw, dict):
        raise ValueError("fields must be an object")
    if not raw:
        raise ValueError("fields must not be empty")

    out: dict[str, Optional[str]] = {}
    for key, value in raw.items():
        canonical = _FIELD_ALIASES.get(key, key)
        if canonical not in CANONICAL_FIELDS:
            raise ValueError(f"unknown correction field: {key}")
        if value is not None and not isinstance(value, str):
            raise ValueError(f"correction field {key} must be a string or null")
        text = "" if value is None else value.strip()
        if not text:
            cleaned: Optional[str] = None  # Override entfernen
        elif canonical == "path":
            if text not in PATH_VALUES:
                raise ValueError("path must be one of the fixed Weg values")
            cleaned = text
        elif canonical in _AGENT_ROLES:
            # Alias-/Unknown-Regeln der P6a-Ableitung (niemals Raten).
            if len(text) > _MAX_AGENT_LEN:
                raise ValueError(f"correction field {key} is too long")
            cleaned = normalize_producer(text)
        else:  # ablage — technischer Ref, bereinigt und gedeckelt
            if len(text) > _MAX_ABLAGE_LEN:
                raise ValueError("ablage is too long")
            cleaned = text
        if canonical in out and out[canonical] != cleaned:
            # producer ist ein Alias für autor; widersprüchliche Doppelwerte
            # dürfen nicht von der JSON-Key-Reihenfolge abhängen.
            raise ValueError("producer and autor must agree")
        out[canonical] = cleaned
    return out


# ---------------------------------------------------------------------------
# Reine Apply-/Preview-Mathematik über dem abgeleiteten P6a-Vertrag
# ---------------------------------------------------------------------------

def _snapshot(provenance: dict[str, Any]) -> dict[str, Any]:
    """Unveränderlicher Originalsnapshot des ABGELEITETEN Vertrags (vor jeder
    Korrektur) — Quelle für die Original-Anzeige und das Audit."""
    chain = provenance.get("chain") or {}
    return {
        "producer": provenance.get("producer") or "Unbekannt",
        "path": provenance.get("path") or "Unbekannt",
        "status": provenance.get("status") or "unknown",
        "chain": {role: chain.get(role, "Unbekannt") for role in _all_roles()},
    }


def with_derived(
    record: dict[str, Any], derived_provenance: dict[str, Any],
) -> dict[str, Any]:
    """Response-Kopie eines Audit-Records plus HEUTIGER P6a-Ableitung.

    ``original`` bleibt der unveränderliche Snapshot der ersten Korrektur;
    ``derived`` darf sich mit belastbaren Quellmetadaten ändern und ist die
    Basis für eine exakte UI-Vorschau bzw. das Entfernen eines Overrides.
    Dieser Zusatz wird nicht persistiert.
    """
    out = json.loads(json.dumps(record))
    out["derived"] = _snapshot(derived_provenance)
    return out


def _all_roles() -> tuple[str, ...]:
    from hermes_cli.library_view import _CHAIN_ROLES
    return tuple(_CHAIN_ROLES)


def _unknown_status() -> str:
    from hermes_cli.library_view import STATUS_UNKNOWN
    return STATUS_UNKNOWN


def apply(provenance: dict[str, Any], record: Optional[dict[str, Any]]) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Lege die aktiven Overrides eines Records auf den abgeleiteten Vertrag.

    Rein: liefert ``(effective_provenance, correction_block)``. Ohne Record
    oder ohne aktive Felder bleibt der Vertrag unverändert und der Block ist
    ``None``. Der effektive Vertrag treibt List/Detail/Facetten/Badges; der
    Block hält Original + Audit additiv sichtbar."""
    from hermes_cli.library_view import _provenance_status

    fields = _active_fields(record)
    if not fields:
        return provenance, None

    effective = {
        "producer": provenance.get("producer") or "Unbekannt",
        "path": provenance.get("path") or "Unbekannt",
        "status": provenance.get("status") or _unknown_status(),
        "chain": dict(provenance.get("chain") or {}),
        "refs": list(provenance.get("refs") or []),
    }
    chain = effective["chain"]
    for role in _all_roles():
        if role in fields:
            chain[role] = fields[role]
    if "path" in fields:
        effective["path"] = fields["path"]
    # Erzeuger ist immer die (korrigierte) Autor-Rolle; Status folgt der Kette.
    effective["producer"] = chain.get("autor") or "Unbekannt"
    effective["status"] = _provenance_status(chain)

    original = (record or {}).get("original") or _snapshot(provenance)
    block = {
        "item_id": (record or {}).get("item_id") or "",
        "active": True,
        "fields": dict(fields),
        "original": original,
        "derived": _snapshot(provenance),
        "reason": (record or {}).get("reason") or "",
        "actor": (record or {}).get("actor") or ACTOR_OPERATOR,
        "created_at": int((record or {}).get("created_at") or 0),
        "updated_at": int((record or {}).get("updated_at") or 0),
        "history": list((record or {}).get("history") or []),
    }
    return effective, block


def preview(
    provenance: dict[str, Any],
    fields: dict[str, Any],
    record: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Vorschau der exakten Set-Merge-Semantik ohne Persistenz.

    ``fields`` ist bereits validiert; None entfernt einen Override. Bestehende
    disjunkte Overrides aus ``record`` bleiben wie beim echten gelockten Set
    erhalten. Damit nutzt der Bestätigungsdialog denselben kanonischen Vertrag
    (Aliase, Unknown-Sentinels, Status) wie die spätere Mutation.
    """
    active = _active_fields(record)
    for key, value in fields.items():
        if value is None:
            active.pop(key, None)
        else:
            active[key] = value
    fake_record = {"original": _snapshot(provenance), "fields": active}
    effective, _ = apply(provenance, fake_record)
    return effective


def _active_fields(record: Optional[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(record, dict):
        return {}
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {k: str(v) for k, v in fields.items() if k in CANONICAL_FIELDS and v}


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------

def read(item_id: str) -> Optional[dict[str, Any]]:
    """Voller Record inkl. History (auch zurückgenommen = leere Felder) — für
    Detail/Audit. ``None`` wenn nie korrigiert."""
    validate_item_id(item_id)
    return _read_state()["corrections"].get(item_id)


def load_active() -> dict[str, dict[str, Any]]:
    """Nur Records mit aktiven Feldern — Bulk-Overlay für den List-Pfad."""
    out: dict[str, dict[str, Any]] = {}
    for item_id, record in _read_state()["corrections"].items():
        if _active_fields(record):
            out[item_id] = record
    return out


# ---------------------------------------------------------------------------
# Bestätigte Mutation (fail-closed) + append-only Audit
# ---------------------------------------------------------------------------

def set_correction(
    item_id: str,
    fields: dict[str, Any],
    reason: Any,
    *,
    confirm: bool = False,
    derived_provenance: Optional[dict[str, Any]] = None,
    actor: str = ACTOR_OPERATOR,
) -> dict[str, Any]:
    """Setze/aktualisiere eine Korrektur (Merge pro Feld; leerer Wert entfernt
    das Feld). ``confirm is True`` fail-closed + Pflicht-Grund. Der
    Originalsnapshot wird bei ERSTANLAGE aus dem abgeleiteten Vertrag genommen
    und bleibt danach unverändert."""
    if confirm is not True:
        raise ValueError(
            "set requires confirm=true (the operator 'are you sure' step)"
        )
    item_id = validate_item_id(item_id)
    clean_reason = _validate_reason(reason)
    validated = validate_fields(fields)

    with _StoreLock():
        state = _read_state(for_write=True)
        ts = _now()
        record = state["corrections"].get(item_id)
        if not isinstance(record, dict):
            record = {
                "item_id": item_id,
                "fields": {},
                "original": _snapshot(derived_provenance or {}),
                "created_at": ts,
                "updated_at": ts,
                "actor": actor,
                "reason": clean_reason,
                "history": [],
            }
        active = dict(record.get("fields") or {})
        for key, value in validated.items():
            if value is None:
                active.pop(key, None)
            else:
                active[key] = value
        record["fields"] = active
        record["updated_at"] = ts
        record["actor"] = actor
        record["reason"] = clean_reason
        history = list(record.get("history") or [])
        history.append({
            "at": ts,
            "action": "set",
            "fields": dict(active),
            "reason": clean_reason,
            "actor": actor,
        })
        record["history"] = history
        state["corrections"][item_id] = record
        _write_state(state)
        return json.loads(json.dumps(record))


def revert(
    item_id: str,
    reason: Any,
    *,
    fields: Optional[list[str]] = None,
    confirm: bool = False,
    actor: str = ACTOR_OPERATOR,
) -> Optional[dict[str, Any]]:
    """Nehme eine Korrektur zurück — einzelne Felder (``fields``) oder ganz.
    ``confirm is True`` fail-closed + Pflicht-Grund. Append-only History; der
    Record bleibt (ggf. mit leeren Feldern) für das Audit erhalten."""
    if confirm is not True:
        raise ValueError(
            "revert requires confirm=true (the operator 'are you sure' step)"
        )
    item_id = validate_item_id(item_id)
    clean_reason = _validate_reason(reason)

    with _StoreLock():
        state = _read_state(for_write=True)
        record = state["corrections"].get(item_id)
        if not isinstance(record, dict):
            return None  # nichts zurückzunehmen
        ts = _now()
        active = dict(record.get("fields") or {})
        if fields is not None:
            if not fields:
                raise ValueError("fields must not be empty")
            for key in fields:
                canonical = _FIELD_ALIASES.get(key, key)
                if canonical not in CANONICAL_FIELDS:
                    raise ValueError(f"unknown correction field: {key}")
                active.pop(canonical, None)
        else:
            active = {}
        record["fields"] = active
        record["updated_at"] = ts
        record["actor"] = actor
        record["reason"] = clean_reason
        history = list(record.get("history") or [])
        history.append({
            "at": ts,
            "action": "revert",
            "fields": dict(active),
            "reason": clean_reason,
            "actor": actor,
        })
        record["history"] = history
        state["corrections"][item_id] = record
        _write_state(state)
        return json.loads(json.dumps(record))

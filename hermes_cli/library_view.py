"""Bibliothek (/control, Programm 3 Phase D/E) — der Lesesaal.

Aggregation alles Menschenlesbaren, das Hermes produziert, an
einem Ort. Inhalte bleiben Adapter über Bestehendes; nur Bibliothek-Präferenzen
(gespeicherte Suchen/Themen-Follows) nutzen einen kleinen profile-lokalen Store:

  1. Cron-Digests:   ``<store>/output/<job_id>/<timestamp>.md`` — alle Läufe,
                     Multi-Store (Haupt-Store ``~/.hermes/cron`` UND
                     ``~/.hermes/profiles/*/cron``, Phase E). Als Body wird
                     ausschließlich der ``## Response``-Teil ausgeliefert —
                     Redaction-Disziplin wie cron_observability (Prompts und
                     Scripts bleiben draußen).
  2. Recherchen:     Kanban-Tasks ``tenant=research`` — Frage (Titel/Body) +
                     Antwort (letzter Kommentar, Receipt-Muster).
  3. Deliverables:   Markdown-Deliverables fertiger Tasks aus
                     ``<kanban_home>/reports/by-task/<task_id>/``.

Hausmuster: ``register_library_routes(app)`` nach autoresearch_view/
cron_observability-Vorbild — unter ``/api/`` (erbt das Session-Gate, nie in
PUBLIC_API_PATHS), Blocking-FS via ``asyncio.to_thread``, Pfad-Eskapaden →
400/404 statt 5xx, IDs streng validiert (kein Traversal über ``id``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time as _time
from dataclasses import dataclass, field, replace as _dc_replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, StrictBool

logger = logging.getLogger(__name__)


class SavedSearchCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    query: str = Field(..., min_length=1, max_length=1000)
    topic_tags: list[str] = Field(default_factory=list, max_length=50)
    person_tags: list[str] = Field(default_factory=list, max_length=50)


class SavedSearchUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=160)
    query: Optional[str] = Field(None, min_length=1, max_length=1000)
    topic_tags: Optional[list[str]] = Field(None, max_length=50)
    person_tags: Optional[list[str]] = Field(None, max_length=50)


# P6b — Korrektur-Overlay (operator-bestätigt, ADR 0002). `confirm` steht
# bewusst auf False und muss ausdrücklich `true` sein (fail-closed); `reason`
# ist Pflicht. Es gibt keinen Agent-/Tool-Automatismus auf diesem Pfad.
class CorrectionSetPayload(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=320)
    fields: dict[str, Optional[str]] = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=600)
    confirm: StrictBool = False


class CorrectionPreviewPayload(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=320)
    fields: dict[str, Optional[str]] = Field(..., min_length=1)


class CorrectionRevokePayload(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=320)
    reason: str = Field(..., min_length=1, max_length=600)
    fields: Optional[list[str]] = Field(None, min_length=1, max_length=6)
    confirm: StrictBool = False

# ---------------------------------------------------------------------------
# Kategorien (Grill-Entscheid §7.7: Backend-Dict, Fallback "briefings",
# kein jobs.json-Schema-Touch). Phase E: die WM-/KI-Jobs des research-Stores
# sind explizit gemappt — jeder Job ist eine Serie ("Abo").
# ---------------------------------------------------------------------------

CATEGORIES = ("news", "briefings", "recherchen", "familie", "receipts", "wartung")

# job_id → Kategorie (explizit; gewinnt vor den Namens-Heuristiken).
_JOB_CATEGORY: dict[str, str] = {
    # WM 2026 (research-Profil-Store)
    "342d9529bf9c": "news",   # WM Morgenbrief
    "de387a544da2": "news",   # WM Abendrecap
    "ca21561e299f": "news",   # DFB Newswatch
    "05f12eb3fd8c": "news",   # WM Pre-Kick
    "5d9b9794c8a0": "news",   # WM Postmatch
    # KI-News (research-Profil-Store)
    "5a2a54ac3dae": "news",   # KI Modell-Brief (Morgen)
    "4c88cd4449a6": "news",   # KI Modell Breaking-Watch (Mittag)
    "92adf20dd9bd": "news",   # KI Modell-Brief (Abend)
    # Familie (fo-brain-Profil-Store)
    "e28b8cd87809": "familie",  # Familien-Morgenbrief 06:30
}

_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("news", "news"), ("breaking", "news"), ("wm ", "news"), ("dfb", "news"),
    ("triage", "wartung"), ("audit", "wartung"), ("cleanup", "wartung"),
    ("wartung", "wartung"), ("health", "wartung"), ("heartbeat", "wartung"),
)


def _categorize_job(job_id: str, name: str) -> str:
    explicit = _JOB_CATEGORY.get(job_id)
    if explicit:
        return explicit
    lowered = (name or "").lower()
    for needle, category in _NAME_HINTS:
        if needle in lowered:
            return category
    return "briefings"


# ---------------------------------------------------------------------------
# Cron-Store-Adapter
# ---------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
_OUTPUT_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.md$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_PREVIEW_CHARS = 280
_MAX_BODY_BYTES = 512 * 1024  # ein Digest; Kappung gegen Ausreißer

_STRUCTURED_MODEL_NEWS_KINDS: dict[str, str] = {
    "5a2a54ac3dae": "morgen",
    "4c88cd4449a6": "breaking",
    "92adf20dd9bd": "abend",
}
_FULL_BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*$")
_BOLD_ITEM_TITLE_RE = re.compile(r"^\*\*(.+?)\*\*[: ]*(.*)$")
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _hermes_home() -> Path:
    from hermes_cli.kanban_db import kanban_home
    return kanban_home()


def _cron_stores() -> list[tuple[str, Path]]:
    """Alle Cron-Stores: ``main`` + ``profile:<name>`` (Phase E).

    Store-IDs sind Teil der Item-IDs; die Auflösung läuft NUR über diese
    Liste — nie über Pfade aus dem Request.
    """
    home = _hermes_home()
    stores: list[tuple[str, Path]] = [("main", home / "cron")]
    profiles_root = home / "profiles"
    if profiles_root.is_dir():
        for profile_dir in sorted(profiles_root.iterdir()):
            cron_dir = profile_dir / "cron"
            if cron_dir.is_dir() and _TASK_ID_RE.match(profile_dir.name):
                stores.append((f"profile:{profile_dir.name}", cron_dir))
    return stores


def _resolve_store(store_id: str) -> Optional[Path]:
    for sid, path in _cron_stores():
        if sid == store_id:
            return path
    return None


def _load_jobs_meta(store_dir: Path) -> dict[str, dict]:
    """job_id → {name, schedule_display, prompt, script, enabled} aus jobs.json."""
    try:
        raw = json.loads((store_dir / "jobs.json").read_text(encoding="utf-8"))
        jobs = raw.get("jobs", []) if isinstance(raw, dict) else []
    except (OSError, ValueError):
        return {}
    meta: dict[str, dict] = {}
    for job in jobs:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        schedule = job.get("schedule") or {}
        meta[str(job["id"])] = {
            "name": str(job.get("name") or job["id"]),
            "schedule_display": str(
                schedule.get("display") or schedule.get("expr") or ""
            ) if isinstance(schedule, dict) else "",
            "prompt": str(job.get("prompt") or ""),
            "script": str(job.get("script") or ""),
            "enabled": bool(job.get("enabled", False)),
        }
    return meta


def _extract_response(markdown: str) -> Optional[str]:
    """Nur den ``## Response``-Teil ausliefern (Redaction: Prompt/Script
    bleiben draußen). Der Prompt-Abschnitt kann selbst ##-Headings — auch
    eine wörtliche ``## Response``-Zeile — enthalten; der echte Response-Teil
    ist im Output-Format immer der letzte, darum zählt das LETZTE Vorkommen.
    Lieber einen Response-Anfang verlieren als je Prompt-Text leaken."""
    lines = markdown.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() == "## Response":
            return "\n".join(lines[idx + 1:]).strip()
    return None


def _output_ts(filename: str, fallback: float) -> int:
    try:
        return int(_time.mktime(_time.strptime(filename[:19], "%Y-%m-%d_%H-%M-%S")))
    except ValueError:
        return int(fallback)


def _preview(body: str) -> str:
    flat = " ".join(body.split())
    return flat[:_PREVIEW_CHARS]


def _plain_markdown(value: str) -> str:
    return " ".join(re.sub(r"[*_`]", "", value).split())


def _markdown_section(body: str, title: str) -> str:
    """Return a full-bold cron response section without crossing into the next.

    The KI brief prompt deliberately emits Discord-friendly bold headings
    (``**Quellen**``), not Markdown ``##`` headings.  Parsing that canonical
    response is safer than adding a second producer-side artifact: the fetcher
    scripts run before the agent has verified or written the final report.
    """
    lines = body.splitlines()
    start: Optional[int] = None
    for idx, line in enumerate(lines):
        match = _FULL_BOLD_LINE_RE.match(line.strip())
        if match and match.group(1).strip().casefold() == title.casefold():
            start = idx + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start, len(lines)):
        if _FULL_BOLD_LINE_RE.match(lines[idx].strip()):
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def _markdown_bullets(block: str) -> list[str]:
    bullets: list[str] = []
    current: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if current:
                bullets.append(" ".join(current))
            current = [stripped[2:].strip()]
        elif current and stripped:
            current.append(stripped)
    if current:
        bullets.append(" ".join(current))
    return bullets


def _normalize_url(value: str) -> str:
    return value.strip().rstrip(".,;:!?)]}>'\"")


def _structured_sources(body: str) -> list[dict[str, str]]:
    source_block = _markdown_section(body, "Quellen")
    candidates = _markdown_bullets(source_block)
    if not candidates:
        # Breaking reports put each source URL directly on the news bullet.
        candidates = [line.strip()[2:] for line in body.splitlines() if line.strip().startswith("- ")]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        match = _URL_RE.search(candidate)
        if not match:
            continue
        url = _normalize_url(match.group(0))
        if not url or url in seen:
            continue
        title = candidate[:match.start()].strip().rstrip(" -")
        title = re.sub(r"^\[[^\]]+\]\s*", "", title)
        out.append({"title": _plain_markdown(title) or url, "url": url})
        seen.add(url)
    return out


def _source_for_news(
    title: str,
    summary: str,
    sources: list[dict[str, str]],
) -> Optional[dict[str, str]]:
    inline = _URL_RE.search(summary)
    if inline:
        url = _normalize_url(inline.group(0))
        return next((source for source in sources if source["url"] == url), {"title": title, "url": url})

    ignored = {"modell", "model", "introducing", "the", "and", "with", "jetzt", "new"}
    wanted = {
        word.casefold() for word in _WORD_RE.findall(title)
        if len(word) >= 3 and word.casefold() not in ignored
    }
    ranked: list[tuple[int, dict[str, str]]] = []
    for source in sources:
        source_words = {word.casefold() for word in _WORD_RE.findall(source["title"]) if len(word) >= 3}
        ranked.append((len(wanted & source_words), source))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] > 0 else None


def _parse_structured_model_brief(job_id: str, body: str, ts: int) -> Optional[dict[str, Any]]:
    """Parse the final, already-verified cron response into a UI contract.

    The existing scheduler run record is the Markdown source of truth and the
    flat delivery file remains the Discord representation.  This adapter adds
    no second content truth and never exposes Prompt/Script Output.
    """
    run_kind = _STRUCTURED_MODEL_NEWS_KINDS.get(job_id)
    if run_kind is None:
        return None

    sources = _structured_sources(body)
    important = _markdown_bullets(_markdown_section(body, "Das Wichtigste zuerst"))
    news_bullets = _markdown_bullets(_markdown_section(body, "Neue Modelle & Capabilities"))
    if run_kind == "breaking" and not news_bullets:
        news_bullets = [
            line.strip()[2:]
            for line in body.splitlines()
            if line.strip().startswith("- ") and "WATCHLIST-UPDATE:" not in line
        ]
    if not important and news_bullets:
        important = [news_bullets[0]]

    model_news: list[dict[str, str]] = []
    for bullet in news_bullets:
        title_match = _BOLD_ITEM_TITLE_RE.match(bullet)
        title = _plain_markdown(title_match.group(1)) if title_match else _plain_markdown(bullet[:100])
        summary = _plain_markdown(bullet)
        source = _source_for_news(title, bullet, sources)
        # Source hygiene: an item without a real URL stays in the canonical
        # body but is not promoted to the structured model-news front page.
        if source is None:
            continue
        model_news.append({
            "title": title,
            "summary": summary,
            "source_title": source["title"],
            "source_url": source["url"],
        })

    watchlist_delta = [
        _plain_markdown(line.strip())
        for line in body.splitlines()
        if line.strip().startswith("WATCHLIST-UPDATE:")
    ]
    top_story = _plain_markdown(important[0]) if important else ""
    if not top_story or not sources:
        return None
    return {
        "run_kind": run_kind,
        "generated": datetime.fromtimestamp(ts).astimezone().isoformat(),
        "top_story": top_story,
        "model_news": model_news,
        "sources": sources,
        "watchlist_delta": watchlist_delta,
    }


# Entrauschung 2026-06-11: ``[SILENT]``-Ausgaben sind die Selbstauskunft
# "nichts Neues" (LLM-Pfad schreibt sie trotzdem als Output-File) — kein
# Lesestoff. Check tolerant wie der Delivery-Skip des Schedulers
# (SILENT_MARKER irgendwo im Inhalt, uppercased). Wichtig: Filter NACH dem
# Cache-Read anwenden, nie als Negativ-Eintrag cachen — sonst würden
# bestehende positive Cache-Einträge über den Hit-Pfad weiter ausgeliefert.
_SILENT_MARKER = "[SILENT]"


def _is_silent(item: _Item) -> bool:
    return _SILENT_MARKER in (item.body_md or item.preview or "").upper()


# ---------------------------------------------------------------------------
# P6a — Provenienz (read-only Herkunft-Overlay, ADR 0001)
#
# Beantwortet zwei Laien-Fragen: WER hat das Dokument erzeugt (Erzeuger) und
# über welchen WEG kam es in die Bibliothek? Deterministisch und NUR aus
# Metadaten abgeleitet (source_ref, Store/Profil, Task-Attribution, Receipt-
# Agent) — niemals aus Body-Text, Prompt- oder Script-Inhalten. Mutiert keine
# Quelle; unsichere Fälle bleiben "Unbekannt" (niemals geraten). List- und
# Detailantwort liefern denselben Vertrag (as_dict hängt provenance immer an).
# ---------------------------------------------------------------------------

_UNKNOWN = "Unbekannt"

# Weg — genau einer dieser Werte; technische Untertypen leben nur in den Refs.
# "Manuell" und "Unbekannt" sind explizite Vertragswerte, auch wenn heute kein
# sicherer Adapter sie liefert.
PATH_VALUES = ("Cron", "Task", "Receipt", "Manuell", "Unbekannt")

# Status — deterministisch aus der Anzahl belegter Ketten-Rollen.
STATUS_EVIDENCED = "evidenced"      # vollständig belegt
STATUS_PARTIAL = "partial"          # teilweise belegt
STATUS_UNKNOWN = "unknown"          # unbekannt

# Ketten-Rollen (jede braucht Beleg; Lücke = "Unbekannt").
_CHAIN_ROLES = ("auftraggeber", "delegation", "autor", "review", "ablage")

# Agent-Aliase → stabiler Anzeigename. Konkrete Modelle, Runtime-Varianten, IDs
# und Dateipfade gehören in die technischen Refs, NICHT hierher. Unkuratierte,
# aber konkrete Attributionen (z.B. ein Profilname) werden bereinigt durchgereicht
# (deterministisch, kein Raten); nur leere/fehlende Werte werden "Unbekannt".
_PRODUCER_ALIASES: dict[str, str] = {
    "codex": "Codex",
    "claude": "Claude",
    "claude-code": "Claude",
    "claude_code": "Claude",
    "claude code": "Claude",
    "hermes": "Hermes-System",
    "hermes-system": "Hermes-System",
    "default": "Hermes-System",
    "main": "Hermes-System",
    "system": "Hermes-System",
    "research": "Research",
    "scout": "Scout",
    "kimi": "Kimi",
    "grok": "Grok",
    "qwen": "Qwen",
    "fo-brain": "Familie",
    "fo_brain": "Familie",
    "fo": "Familie",
    "familie": "Familie",
}
_PRODUCER_UNKNOWN_SENTINELS = frozenset({"unknown", "unbekannt", "none", "null", "-"})


def normalize_producer(raw: Any) -> str:
    """Normalize a raw attribution (agent alias / profile / assignee) to a
    stable display name. Empty/missing → "Unbekannt". Known aliases collapse
    onto their canonical name; an unknown but concrete value is passed through
    cleaned (a leading ``profile:`` marker is dropped) — that is still solid
    evidence, just not a curated alias."""
    if raw is None:
        return _UNKNOWN
    cleaned = " ".join(str(raw).split())
    if not cleaned:
        return _UNKNOWN
    key = cleaned.casefold()
    if key in _PRODUCER_UNKNOWN_SENTINELS:
        return _UNKNOWN
    if key in _PRODUCER_ALIASES:
        return _PRODUCER_ALIASES[key]
    if key.startswith("profile:"):
        stripped = cleaned.split(":", 1)[1].strip()
        if not stripped:
            return _UNKNOWN
        stripped_key = stripped.casefold()
        if stripped_key in _PRODUCER_UNKNOWN_SENTINELS:
            return _UNKNOWN
        if stripped_key in _PRODUCER_ALIASES:
            return _PRODUCER_ALIASES[stripped_key]
        return stripped
    return cleaned


def _provenance_status(chain: dict[str, str]) -> str:
    evidenced = sum(1 for value in chain.values() if value != _UNKNOWN)
    if evidenced == 0:
        return STATUS_UNKNOWN
    if evidenced == len(chain):
        return STATUS_EVIDENCED
    return STATUS_PARTIAL


def _unknown_provenance() -> dict[str, Any]:
    return {
        "producer": _UNKNOWN,
        "path": _UNKNOWN,
        "status": STATUS_UNKNOWN,
        "chain": {role: _UNKNOWN for role in _CHAIN_ROLES},
        "refs": [],
    }


def _build_provenance(
    *,
    path: str,
    autor_raw: Any = None,
    auftraggeber_raw: Any = None,
    delegation_raw: Any = None,
    review_raw: Any = None,
    ablage: Optional[str] = None,
    refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Assemble the serializable provenance contract from metadata-only
    evidence. ``producer`` is the actual author / responsible profile (the
    ``autor`` role) — never the commissioner (``auftraggeber``) or reviewer."""
    chain = {
        "auftraggeber": normalize_producer(auftraggeber_raw),
        "delegation": normalize_producer(delegation_raw),
        "autor": normalize_producer(autor_raw),
        "review": normalize_producer(review_raw),
        "ablage": (ablage or _UNKNOWN),
    }
    return {
        "producer": chain["autor"],
        "path": path,
        "status": _provenance_status(chain),
        "chain": chain,
        "refs": [ref for ref in (refs or []) if ref],
    }


def _cron_producer_raw(store_id: str) -> str:
    """Responsible profile of a cron run: the profile store runs it; the main
    store is the default Hermes system itself."""
    if ":" in store_id:
        return store_id.split(":", 1)[1]
    return "main"


@dataclass
class _Item:
    id: str
    category: str
    series_id: str
    series: str
    title: str
    ts: int
    preview: str
    source_ref: str
    series_meta: str = ""
    body_md: Optional[str] = field(default=None, repr=False)
    structured: bool = False
    structured_brief: Optional[dict[str, Any]] = field(default=None, repr=False)
    # P6a: additive, serialisierbare Herkunft. Default = sicherer Unknown-
    # Vertrag, damit jeder Item (auch ein ohne Explizit-Bau konstruierter)
    # einen gültigen Vertrag trägt.
    provenance: dict[str, Any] = field(default_factory=_unknown_provenance, repr=False)
    # P6b: additives Korrektur-Overlay (Original + aktive Felder + Audit), NUR
    # gesetzt wenn eine aktive Operator-Korrektur vorliegt. `provenance` trägt
    # dann bereits den EFFEKTIVEN Wert (treibt Facetten/Filter/Badges); dieser
    # Block hält Ursprung + Historie additiv sichtbar (ADR 0002).
    correction: Optional[dict[str, Any]] = field(default=None, repr=False)

    def as_dict(self, *, with_body: bool) -> dict[str, Any]:
        d = {
            "id": self.id,
            "category": self.category,
            "series_id": self.series_id,
            "series": self.series,
            "title": self.title,
            "ts": self.ts,
            "preview": self.preview,
            "source_ref": self.source_ref,
            "series_meta": self.series_meta,
            # List- UND Detailantwort liefern denselben Provenienz-Vertrag.
            "provenance": self.provenance or _unknown_provenance(),
            # P6b: additiver Korrektur-Block (null ohne aktive Korrektur).
            "correction": self.correction,
        }
        if with_body:
            d["body_md"] = self.body_md or ""
        if self.structured and self.structured_brief is not None:
            d["structured"] = True
            d["structured_brief"] = self.structured_brief
        return d


# Härtung 2026-06-11: Der Haupt-Store hält >100k historische Outputs (542 MB);
# die alle pro Request zu lesen kostete 3-8 s. Zwei Schranken, kein FTS/DB:
#   1. Pro Job nur die NEUESTEN Ausgaben (Dateiname beginnt mit Timestamp →
#      lexikalisch absteigend sortierbar). Ältere Ausgaben sind über die
#      Regal-Ansicht ohnehin nie erreichbar gewesen (Liste war limit-gekappt).
#   2. mtime/size-keyed In-Process-Cache des Parse-Ergebnisses, inklusive
#      Negativ-Einträgen (Datei ohne ##-Response-Teil → None).
_MAX_OUTPUTS_PER_JOB = 40
# path → (mtime_ns, size, meta_fingerprint, geparstes Item mit Body oder None).
# meta_fingerprint enthält sichtbare Serie/Kategorie UND Prompt/Script — so
# invalidiert ein umgebauter Testjob auch bei unverändertem Output-mtime.
_cron_parse_cache: dict[str, tuple[int, int, tuple[str, ...], Optional[_Item]]] = {}
# job_dir → (dir_mtime_ns, newest-N Dateinamen). Ein 30k-Einträge-Verzeichnis
# zu listen+sortieren kostet allein ~0.5 s; das Verzeichnis-mtime ändert sich
# bei jedem Anlegen/Löschen einer Datei und ist darum ein sicherer Schlüssel.
_cron_dir_cache: dict[str, tuple[int, list[str]]] = {}


def _newest_output_names(job_dir: Path) -> list[str]:
    key = str(job_dir)
    try:
        dir_mtime = job_dir.stat().st_mtime_ns
    except OSError:
        _cron_dir_cache.pop(key, None)
        return []
    cached = _cron_dir_cache.get(key)
    if cached is not None and cached[0] == dir_mtime:
        return cached[1]
    names = sorted(
        (e.name for e in job_dir.iterdir() if _OUTPUT_FILE_RE.match(e.name)),
        reverse=True,
    )[:_MAX_OUTPUTS_PER_JOB]
    _cron_dir_cache[key] = (dir_mtime, names)
    return names


def _is_trivial_test_cron_output(name: str, body: str, meta: dict) -> bool:
    """Hide accidental throwaway cron jobs from the Lesesaal.

    The live failure is a job literally named ``w`` with prompt ``echo hi``.
    Keep the predicate exact so real Watch/WM/Wartung reports stay visible.
    """
    name_norm = " ".join(str(name or "").split()).casefold()
    prompt_norm = " ".join(str(meta.get("prompt") or "").split()).casefold()
    script_norm = str(meta.get("script") or "").strip()
    body_norm = " ".join(body.split()).casefold()
    return (
        name_norm == "w"
        and prompt_norm == "echo hi"
        and not script_norm
        and body_norm == "hi"
    )


def _collect_cron_items(*, with_bodies: bool) -> list[_Item]:
    items: list[_Item] = []
    seen_paths: set[str] = set()
    for store_id, store_dir in _cron_stores():
        output_root = store_dir / "output"
        if not output_root.is_dir():
            continue
        jobs_meta = _load_jobs_meta(store_dir)
        for job_dir in output_root.iterdir():
            if not job_dir.is_dir() or not _JOB_ID_RE.match(job_dir.name):
                continue
            meta = jobs_meta.get(job_dir.name, {})
            name = meta.get("name", job_dir.name)
            category = _categorize_job(job_dir.name, name)
            series_meta = meta.get("schedule_display", "")
            prompt = str(meta.get("prompt") or "")
            script = str(meta.get("script") or "")
            profile = store_id.split(":", 1)[1] if ":" in store_id else None
            for fname in _newest_output_names(job_dir):
                md_file = job_dir / fname
                try:
                    stat = md_file.stat()
                except OSError:
                    continue
                cache_key = str(md_file)
                seen_paths.add(cache_key)
                meta_fp = (name, category, series_meta, prompt, script)
                cached = _cron_parse_cache.get(cache_key)
                if (
                    cached is not None
                    and cached[0] == stat.st_mtime_ns
                    and cached[1] == stat.st_size
                    and cached[2] == meta_fp
                ):
                    if cached[3] is not None and not _is_silent(cached[3]):
                        items.append(
                            cached[3] if with_bodies
                            else _dc_replace(cached[3], body_md=None)
                        )
                    continue
                try:
                    raw = md_file.read_text(encoding="utf-8", errors="replace")
                    if len(raw) > _MAX_BODY_BYTES:
                        raw = raw[:_MAX_BODY_BYTES]
                except OSError:
                    continue
                body = _extract_response(raw)
                if not body:
                    _cron_parse_cache[cache_key] = (
                        stat.st_mtime_ns, stat.st_size, meta_fp, None,
                    )
                    continue  # ohne Response-Teil nichts Lesbares
                if _is_trivial_test_cron_output(name, body, meta):
                    _cron_parse_cache[cache_key] = (
                        stat.st_mtime_ns, stat.st_size, meta_fp, None,
                    )
                    continue
                ts = _output_ts(md_file.name, stat.st_mtime)
                structured_brief = _parse_structured_model_brief(job_dir.name, body, ts)
                day = _time.strftime("%d.%m. %H:%M", _time.localtime(ts))
                source_ref = (
                    f"cron:{profile}/{job_dir.name}" if profile
                    else f"cron:{job_dir.name}"
                )
                item = _Item(
                    id=f"cron::{store_id}::{job_dir.name}::{md_file.name}",
                    category=category,
                    series_id=f"{store_id}/{job_dir.name}",
                    series=name,
                    title=f"{name} — Ausgabe {day}",
                    ts=ts,
                    preview=_preview(body),
                    source_ref=source_ref,
                    series_meta=series_meta,
                    body_md=body,
                    structured=structured_brief is not None,
                    structured_brief=structured_brief,
                    provenance=_build_provenance(
                        path="Cron",
                        autor_raw=_cron_producer_raw(store_id),
                        ablage=source_ref,
                        refs=[source_ref, md_file.name],
                    ),
                )
                _cron_parse_cache[cache_key] = (
                    stat.st_mtime_ns, stat.st_size, meta_fp, item,
                )
                if _is_silent(item):
                    continue
                items.append(item if with_bodies else _dc_replace(item, body_md=None))
    # Einträge verschwundener/rotierter Dateien nicht endlos halten.
    for stale in set(_cron_parse_cache) - seen_paths:
        _cron_parse_cache.pop(stale, None)
    return items


def _read_cron_item(store_id: str, job_id: str, filename: str) -> Optional[_Item]:
    if not _JOB_ID_RE.match(job_id) or not _OUTPUT_FILE_RE.match(filename):
        raise ValueError("invalid cron item id")
    store_dir = _resolve_store(store_id)
    if store_dir is None:
        raise ValueError("unknown store")
    output_root = (store_dir / "output").resolve(strict=False)
    target = (output_root / job_id / filename).resolve(strict=False)
    if not str(target).startswith(str(output_root) + "/"):
        raise ValueError("path escape")
    if not target.is_file():
        return None
    raw = target.read_text(encoding="utf-8", errors="replace")[:_MAX_BODY_BYTES]
    body = _extract_response(raw)
    if body is None:
        return None
    jobs_meta = _load_jobs_meta(store_dir)
    meta = jobs_meta.get(job_id, {})
    name = meta.get("name", job_id)
    if _is_trivial_test_cron_output(name, body, meta):
        return None
    ts = _output_ts(filename, target.stat().st_mtime)
    structured_brief = _parse_structured_model_brief(job_id, body, ts)
    profile = store_id.split(":", 1)[1] if ":" in store_id else None
    source_ref = f"cron:{profile}/{job_id}" if profile else f"cron:{job_id}"
    item = _Item(
        id=f"cron::{store_id}::{job_id}::{filename}",
        category=_categorize_job(job_id, name),
        series_id=f"{store_id}/{job_id}",
        series=name,
        title=f"{name} — Ausgabe {_time.strftime('%d.%m. %H:%M', _time.localtime(ts))}",
        ts=ts,
        preview=_preview(body),
        source_ref=source_ref,
        series_meta=meta.get("schedule_display", ""),
        body_md=body,
        structured=structured_brief is not None,
        structured_brief=structured_brief,
        provenance=_build_provenance(
            path="Cron",
            autor_raw=_cron_producer_raw(store_id),
            ablage=source_ref,
            refs=[source_ref, filename],
        ),
    )
    # Consistent with _collect_cron_items: silent self-reports are not readable.
    return None if _is_silent(item) else item


# ---------------------------------------------------------------------------
# Recherchen-Adapter (kanban.db, tenant=research)
# ---------------------------------------------------------------------------

def _collect_research_items(*, with_bodies: bool, limit: int = 200) -> list[_Item]:
    from hermes_cli import kanban_db
    items: list[_Item] = []
    try:
        conn = kanban_db.connect()
    except Exception:
        logger.debug("library: kanban connect failed", exc_info=True)
        return items
    try:
        rows = conn.execute(
            "SELECT id, title, status, created_at, completed_at, assignee, created_by FROM tasks "
            "WHERE tenant = 'research' AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        for row in rows:
            answer = None
            answer_ts = None
            answer_author = None
            comments = kanban_db.list_comments(conn, row["id"])
            if comments:
                answer = comments[-1].body
                answer_ts = comments[-1].created_at
                answer_author = comments[-1].author
            if not answer:
                continue  # Bibliothek zeigt Lesbares; offene Fragen wohnen im Research-Tab
            ts = int(answer_ts or row["completed_at"] or row["created_at"])
            source_ref = f"task:{row['id']}"
            items.append(_Item(
                id=f"research::{row['id']}",
                category="recherchen",
                series_id="research",
                series="Recherchen",
                title=row["title"],
                ts=ts,
                preview=_preview(answer),
                source_ref=source_ref,
                body_md=answer if with_bodies else None,
                provenance=_build_provenance(
                    path="Task",
                    autor_raw=answer_author,
                    auftraggeber_raw=row["created_by"],
                    delegation_raw=row["assignee"],
                    ablage=source_ref,
                    refs=[source_ref],
                ),
            ))
    except Exception:
        logger.debug("library: research adapter failed", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return items


def _read_research_item(task_id: str) -> Optional[_Item]:
    if not _TASK_ID_RE.match(task_id):
        raise ValueError("invalid task id")
    from hermes_cli import kanban_db
    conn = kanban_db.connect()
    try:
        row = conn.execute(
            "SELECT id, title, body, created_at, completed_at, assignee, created_by FROM tasks "
            "WHERE id = ? AND tenant = 'research'",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        comments = kanban_db.list_comments(conn, task_id)
        if not comments:
            return None
        answer = comments[-1].body
        if not answer:
            # Consistent with _collect_research_items: a research task without
            # readable answer has no business appearing in the library.
            return None
        ts = int(comments[-1].created_at)
        question = (row["body"] or "").strip()
        body = (
            f"> **Frage:** {row['title']}\n\n{answer}" if not question
            else f"> **Frage:** {question.splitlines()[0]}\n\n{answer}"
        )
        source_ref = f"task:{task_id}"
        return _Item(
            id=f"research::{task_id}",
            category="recherchen",
            series_id="research",
            series="Recherchen",
            title=row["title"],
            ts=ts,
            preview=_preview(answer),
            source_ref=source_ref,
            body_md=body,
            provenance=_build_provenance(
                path="Task",
                autor_raw=comments[-1].author,
                auftraggeber_raw=row["created_by"],
                delegation_raw=row["assignee"],
                ablage=source_ref,
                refs=[source_ref],
            ),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deliverables-Adapter (reports/by-task, nur Markdown)
# ---------------------------------------------------------------------------

_DELIVERABLE_MAX_PER_TASK = 3
_DELIVERABLE_CATEGORY = "receipts"
_DELIVERABLE_SERIES = "Arbeitsergebnisse"


def _collect_deliverable_items(*, with_bodies: bool, limit_tasks: int = 150) -> list[_Item]:
    items: list[_Item] = []
    reports_root = _hermes_home() / "reports" / "by-task"
    from hermes_cli import kanban_db
    titles: dict[str, str] = {}
    # P6a: task-Attribution (assignee=Autor, created_by=Auftraggeber) — reine
    # Metadaten, kein Body. Einmalig im Bulk geladen (kein N+1 pro Deliverable).
    task_attr: dict[str, tuple[Optional[str], Optional[str]]] = {}
    try:
        conn = kanban_db.connect()
        try:
            for row in conn.execute(
                "SELECT id, title, assignee, created_by FROM tasks "
                "WHERE status IN ('done', 'review')",
            ).fetchall():
                titles[row["id"]] = row["title"]
                task_attr[row["id"]] = (row["assignee"], row["created_by"])
        finally:
            conn.close()
    except Exception:
        logger.debug("library: deliverable title lookup failed", exc_info=True)
    seen_task_ids: set[str] = set()
    if reports_root.is_dir():
        task_dirs = sorted(
            (d for d in reports_root.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime, reverse=True,
        )[:limit_tasks]
        for task_dir in task_dirs:
            if not _TASK_ID_RE.match(task_dir.name):
                continue
            seen_task_ids.add(task_dir.name)
            md_files = sorted(
                task_dir.rglob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:_DELIVERABLE_MAX_PER_TASK]
            for md_file in md_files:
                try:
                    stat = md_file.stat()
                    body = md_file.read_text(encoding="utf-8", errors="replace")[:_MAX_BODY_BYTES]
                except OSError:
                    continue
                if not body.strip():
                    continue
                rel = md_file.relative_to(task_dir).as_posix()
                task_title = titles.get(task_dir.name, task_dir.name)
                suffix = "" if rel == "RESULT.md" else f" · {rel}"
                source_ref = f"task:{task_dir.name}/{rel}"
                assignee, created_by = task_attr.get(task_dir.name, (None, None))
                items.append(_Item(
                    id=f"deliverable::{task_dir.name}::{rel}",
                    category=_DELIVERABLE_CATEGORY,
                    series_id="deliverables",
                    series=_DELIVERABLE_SERIES,
                    title=f"{task_title}{suffix}",
                    ts=int(stat.st_mtime),
                    preview=_preview(body),
                    source_ref=source_ref,
                    body_md=body if with_bodies else None,
                    provenance=_build_provenance(
                        path="Task",
                        autor_raw=assignee,
                        auftraggeber_raw=created_by,
                        delegation_raw=assignee,
                        ablage=source_ref,
                        refs=[source_ref],
                    ),
                ))
    receipt_paths = _receipt_file_paths()
    vault_root = (Path.home() / "vault").resolve()
    try:
        conn = kanban_db.connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT tr.task_id, tr.metadata, t.title, t.completed_at,
                       t.assignee, t.created_by
                  FROM task_runs tr JOIN tasks t ON t.id = tr.task_id
                 WHERE t.status IN ("done", "review")
                   AND tr.metadata IS NOT NULL AND tr.metadata != ""
                   AND tr.metadata LIKE "%artifacts%"
                   AND t.completed_at > strftime("%s", "now") - 86400 * 14
                 ORDER BY t.completed_at DESC LIMIT ?
                """,
                (limit_tasks,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.debug("library: deliverable artifact lookup failed", exc_info=True)
        rows = []
    for row in rows:
        task_id = row["task_id"]
        if task_id in seen_task_ids:
            continue
        try:
            md = json.loads(row["metadata"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        artifacts = md.get("artifacts", []) if isinstance(md, dict) else []
        if not isinstance(artifacts, list):
            continue
        emitted = 0
        seen_names: set[str] = set()
        for art_path in artifacts:
            if emitted >= _DELIVERABLE_MAX_PER_TASK or not isinstance(art_path, str):
                continue
            p_resolved = _validated_artifact_path(art_path, vault_root=vault_root)
            if p_resolved is None:
                continue
            if p_resolved in receipt_paths or not p_resolved.is_file():
                continue
            name = p_resolved.name
            if name in seen_names:
                continue
            try:
                stat = p_resolved.stat()
                body = p_resolved.read_text(encoding="utf-8", errors="replace")[:_MAX_BODY_BYTES]
            except OSError:
                continue
            if not body.strip():
                continue
            task_title = row["title"] or titles.get(task_id, task_id)
            source_ref = f"artifact:{task_id}/{name}"
            items.append(_Item(
                id=f"deliverable::{task_id}::{name}",
                category=_DELIVERABLE_CATEGORY,
                series_id="deliverables",
                series=_DELIVERABLE_SERIES,
                title=f"{task_title} - {name}",
                ts=int(stat.st_mtime),
                preview=_preview(body),
                source_ref=source_ref,
                body_md=body if with_bodies else None,
                provenance=_build_provenance(
                    path="Task",
                    autor_raw=row["assignee"],
                    auftraggeber_raw=row["created_by"],
                    delegation_raw=row["assignee"],
                    ablage=source_ref,
                    refs=[source_ref],
                ),
            ))
            emitted += 1
            seen_names.add(name)
    return items


def _validated_artifact_path(art_path: str, *, vault_root: Path) -> Optional[Path]:
    p = Path(art_path).expanduser()
    if not p.is_absolute() or p.suffix != ".md":
        return None
    try:
        p_resolved = p.resolve()
        p_resolved.relative_to(vault_root)
    except (OSError, ValueError):
        return None
    return p_resolved


def _receipt_file_paths() -> set[Path]:
    receipt_paths: set[Path] = set()
    agents_root = _receipts_root()
    if not agents_root.is_dir():
        return receipt_paths
    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir() or not _TASK_ID_RE.match(agent_dir.name):
            continue
        receipts_dir = agent_dir / "receipts"
        if not receipts_dir.is_dir():
            continue
        receipt_dirs = [receipts_dir]
        receipt_dirs.extend(
            d for d in sorted(receipts_dir.iterdir()) if d.is_dir() and _TASK_ID_RE.match(d.name)
        )
        for receipt_dir in receipt_dirs:
            for fname in _newest_receipt_names(receipt_dir):
                try:
                    receipt_paths.add((receipt_dir / fname).resolve())
                except OSError:
                    continue
    return receipt_paths


def _task_attribution(task_id: str) -> tuple[Optional[str], Optional[str]]:
    """(assignee, created_by) for a task — detail-path Provenienz-Beleg, reine
    Metadaten. Fail-soft: kein DB-Zugriff/kein Task → (None, None)."""
    from hermes_cli import kanban_db
    try:
        conn = kanban_db.connect()
        try:
            row = conn.execute(
                "SELECT assignee, created_by FROM tasks "
                "WHERE id = ? AND status IN ('done', 'review')",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.debug("library: task attribution lookup failed", exc_info=True)
        return None, None
    if row is None:
        return None, None
    return row["assignee"], row["created_by"]


def _read_deliverable_item(task_id: str, rel_path: str) -> Optional[_Item]:
    if not _TASK_ID_RE.match(task_id):
        raise ValueError("invalid task id")
    if not rel_path.endswith(".md"):
        raise ValueError("only markdown deliverables")
    reports_root = (_hermes_home() / "reports" / "by-task").resolve(strict=False)
    target = (reports_root / task_id / rel_path).resolve(strict=False)
    if not str(target).startswith(str(reports_root) + "/"):
        raise ValueError("path escape")
    if not target.is_file():
        return _read_artifact_deliverable_item(task_id, rel_path)
    body = target.read_text(encoding="utf-8", errors="replace")[:_MAX_BODY_BYTES]
    source_ref = f"task:{task_id}/{rel_path}"
    assignee, created_by = _task_attribution(task_id)
    return _Item(
        id=f"deliverable::{task_id}::{rel_path}",
        category=_DELIVERABLE_CATEGORY,
        series_id="deliverables",
        series=_DELIVERABLE_SERIES,
        title=f"{task_id} · {rel_path}",
        ts=int(target.stat().st_mtime),
        preview=_preview(body),
        source_ref=source_ref,
        body_md=body,
        provenance=_build_provenance(
            path="Task",
            autor_raw=assignee,
            auftraggeber_raw=created_by,
            delegation_raw=assignee,
            ablage=source_ref,
            refs=[source_ref],
        ),
    )


def _read_artifact_deliverable_item(task_id: str, name: str) -> Optional[_Item]:
    if "/" in name or "\\" in name:
        return None
    vault_root = (Path.home() / "vault").resolve()
    receipt_paths = _receipt_file_paths()
    from hermes_cli import kanban_db
    try:
        conn = kanban_db.connect()
        try:
            rows = conn.execute(
                """
                SELECT tr.metadata, t.title, t.assignee, t.created_by
                  FROM task_runs tr JOIN tasks t ON t.id = tr.task_id
                 WHERE tr.task_id = ?
                   AND t.status IN ("done", "review")
                   AND tr.metadata IS NOT NULL AND tr.metadata != ""
                   AND tr.metadata LIKE "%artifacts%"
                 ORDER BY tr.id DESC
                """,
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.debug("library: artifact deliverable lookup failed", exc_info=True)
        return None
    for row in rows:
        try:
            md = json.loads(row["metadata"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        artifacts = md.get("artifacts", []) if isinstance(md, dict) else []
        if not isinstance(artifacts, list):
            continue
        for art_path in artifacts:
            if not isinstance(art_path, str):
                continue
            p_resolved = _validated_artifact_path(art_path, vault_root=vault_root)
            if p_resolved is None:
                continue
            if p_resolved in receipt_paths or not p_resolved.is_file():
                continue
            if p_resolved.name != name:
                continue
            try:
                body = p_resolved.read_text(encoding="utf-8", errors="replace")[:_MAX_BODY_BYTES]
                stat = p_resolved.stat()
            except OSError:
                return None
            source_ref = f"artifact:{task_id}/{name}"
            return _Item(
                id=f"deliverable::{task_id}::{name}",
                category=_DELIVERABLE_CATEGORY,
                series_id="deliverables",
                series=_DELIVERABLE_SERIES,
                title=f"{row['title'] or task_id} - {name}",
                ts=int(stat.st_mtime),
                preview=_preview(body),
                source_ref=source_ref,
                body_md=body,
                provenance=_build_provenance(
                    path="Task",
                    autor_raw=row["assignee"],
                    auftraggeber_raw=row["created_by"],
                    delegation_raw=row["assignee"],
                    ablage=source_ref,
                    refs=[source_ref],
                ),
            )
    return None


# ---------------------------------------------------------------------------
# Receipts-Adapter (~/vault/03-Agents/<Agent>/receipts/*.md — read-only
# Quelle, wird NIE beschrieben). Serie = Agent. Der Receipt-Korpus wächst
# in die Tausende → newest-200-Cap flach sowie je Subdir + Dir/Parse-mtime-Cache
# (Cron-Muster 1:1, Latenzfalle-Lehre vom 2026-06-11). Receipts haben kein Prompt/
# Response-Format → Body roh (gekappt), Frontmatter abgetrennt und als
# Meta-Zeile gerendert; fail-soft ohne Frontmatter (Titel = H1 → Dateiname,
# ts = mtime).
# ---------------------------------------------------------------------------

_RECEIPT_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}\.md$")
# Jeder immediate Subdir unter <agent>/receipts/ wird gescannt (nicht nur ein
# festes Allowlist-Paar). Symlinks werden weiterhin übersprungen.
_RECEIPT_SUBDIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._\-]{0,127}$")
_MAX_RECEIPTS_FLAT = 200
_MAX_RECEIPTS_PER_SUBDIR = 200
# path → (mtime_ns, size, geparstes Item oder None).
_receipt_parse_cache: dict[str, tuple[int, int, Optional[_Item]]] = {}
# receipts_dir → ((dir_mtime_ns, subdir_mtimes), newest Dateinamen, mtime-absteigend).
_receipt_dir_cache: dict[str, tuple[tuple[int, tuple[int | None, ...]], list[str]]] = {}

# Mehrere Bibliothek-Regale werden absichtlich gleichzeitig gemountet. Ihre
# Requests teilen sich die mtime-/Parse-Caches oben und in den Cron-Adaptern.
# Ein kalter Dashboard-Start darf diese globalen Dicts deshalb nicht aus
# mehreren ``asyncio.to_thread``-Workern gleichzeitig neu aufbauen: Das kostet
# unter Hostlast ein Vielfaches und die jeweiligen stale-key-Sweeps können sich
# gegenseitig Einträge entfernen. Nach dem ersten Collector-Lauf sind die
# folgenden Aufrufe cache-warm; ein RLock serialisiert nur diesen kurzen,
# gemeinsamen kritischen Abschnitt und bleibt bei verschachtelter Nutzung safe.
_collect_lock = threading.RLock()


def _receipts_root() -> Path:
    return Path.home() / "vault" / "03-Agents"


def _newest_receipt_names(receipts_dir: Path) -> list[str]:
    """Newest receipts per source (flat + alle immediate Subdirs).

    Receipt-Namen sind nicht zeitlich sortierbar. Der Cache hängt am
    Parent-dir-mtime und an den Subdir-mtimes, damit neue Receipts in
    irgendeinem Subdir invalidieren, obwohl der Parent unverändert bleiben
    kann. Eine reine Inhalts-Änderung fängt weiterhin der Parse-Cache.

    Statt eines festen Allowlist-Paars (früher nur ``auto``/``mother``) werden
    jetzt alle immediate Subdirs unter ``<agent>/receipts/`` gescannt.
    Symlinked Dirs und symlinked Dateien werden übersprungen (Security-Policy
    vom 2026-06-11). Leere Subdirs (kein einziges ``.md``) liefern keine
    Einträge, bleiben aber im Cache-Tracking, damit neu hinzugefügte Dateien
    die Invalidierung auslösen.
    """
    key = str(receipts_dir)
    try:
        dir_mtime = receipts_dir.stat().st_mtime_ns
    except OSError:
        _receipt_dir_cache.pop(key, None)
        return []
    # Alle immediate Subdirs sammeln (sortiert für deterministischen Cache-Key).
    subdirs: list[str] = []
    try:
        for e in receipts_dir.iterdir():
            if e.is_symlink() or not e.is_dir():
                continue
            if _RECEIPT_SUBDIR_RE.match(e.name):
                subdirs.append(e.name)
    except OSError:
        subdirs = []
    subdirs.sort()
    subdir_mtimes: list[int | None] = []
    for sub in subdirs:
        subdir = receipts_dir / sub
        try:
            subdir_mtimes.append(subdir.stat().st_mtime_ns)
        except OSError:
            subdir_mtimes.append(None)
    cache_key = (dir_mtime, tuple(subdir_mtimes))
    cached = _receipt_dir_cache.get(key)
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    flat_entries: list[tuple[int, str]] = []
    for e in receipts_dir.iterdir():
        if e.is_symlink() or not _RECEIPT_FILE_RE.match(e.name):
            continue
        try:
            flat_entries.append((e.stat().st_mtime_ns, e.name))
        except OSError:
            continue
    names = [n for _, n in sorted(flat_entries, reverse=True)[:_MAX_RECEIPTS_FLAT]]
    for sub in subdirs:
        subdir = receipts_dir / sub
        if subdir.is_symlink() or not subdir.is_dir():
            continue
        sub_entries: list[tuple[int, str]] = []
        for e in subdir.iterdir():
            if e.is_symlink() or not _RECEIPT_FILE_RE.match(e.name):
                continue
            try:
                sub_entries.append((e.stat().st_mtime_ns, f"{sub}/{e.name}"))
            except OSError:
                continue
        names.extend(n for _, n in sorted(sub_entries, reverse=True)[:_MAX_RECEIPTS_PER_SUBDIR])
    _receipt_dir_cache[key] = (cache_key, names)
    return names


# P6b — Receipt-Delegation aus eigenem Frontmatter (belegbare Unknown-Lücke).
# Explizite Abwesenheits-Marker sind kein Delegationsbeleg und bleiben deshalb
# sichtbar ``Unbekannt``; Body, Prompt und Script werden nie ausgewertet.
_ABSENT_ASSIGNEE = frozenset({"", "none", "null", "unknown", "unbekannt", "-"})


def _receipt_delegation_raw(meta: dict[str, str]) -> Optional[str]:
    """Return an evidenced delegation from the receipt's ``assignee`` key."""
    raw = meta.get("assignee")
    if raw is None:
        return None
    cleaned = " ".join(raw.split())
    if not cleaned or cleaned.lower() in _ABSENT_ASSIGNEE:
        return None
    return cleaned


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Flaches ``key: value``-Frontmatter, fail-soft: ohne öffnendes/
    schließendes ``---`` zählt alles als Body."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    meta: dict[str, str] = {}
    for idx in range(1, len(lines)):
        stripped = lines[idx].strip()
        if stripped == "---":
            return meta, "\n".join(lines[idx + 1:])
        if ":" in stripped and not lines[idx].startswith((" ", "\t")):
            key, _, value = stripped.partition(":")
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return {}, raw


def _parse_receipt_file(
    md_file: Path,
    agent: str,
    stat: Any,
    *,
    receipt_name: str | None = None,
) -> Optional[_Item]:
    try:
        raw = md_file.read_text(encoding="utf-8", errors="replace")
        if len(raw) > _MAX_BODY_BYTES:
            raw = raw[:_MAX_BODY_BYTES]
    except OSError:
        return None
    meta, body = _split_frontmatter(raw)
    body = body.strip()
    if not body:
        return None
    title = next(
        (ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "",
    ) or md_file.stem
    meta_bits = [f"**{k}:** {meta[k]}" for k in ("status", "task", "date") if meta.get(k)]
    name = receipt_name or md_file.name
    source_ref = f"receipt:{agent}/{name}"
    return _Item(
        id=f"receipt::{agent}::{name}",
        category="receipts",
        series_id=f"receipts/{agent}",
        series=agent,
        title=title,
        ts=int(stat.st_mtime),
        preview=_preview(body),
        source_ref=source_ref,
        body_md=f"> {' · '.join(meta_bits)}\n\n{body}" if meta_bits else body,
        provenance=_build_provenance(
            path="Receipt",
            autor_raw=agent,
            delegation_raw=_receipt_delegation_raw(meta),
            ablage=source_ref,
            refs=[source_ref],
        ),
    )


def _collect_receipt_items(*, with_bodies: bool) -> list[_Item]:
    items: list[_Item] = []
    agents_root = _receipts_root()
    if not agents_root.is_dir():
        return items
    seen_paths: set[str] = set()
    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir() or not _TASK_ID_RE.match(agent_dir.name):
            continue
        receipts_dir = agent_dir / "receipts"
        if not receipts_dir.is_dir():
            continue
        for fname in _newest_receipt_names(receipts_dir):
            md_file = receipts_dir / fname
            try:
                stat = md_file.stat()
            except OSError:
                continue
            cache_key = str(md_file)
            seen_paths.add(cache_key)
            cached = _receipt_parse_cache.get(cache_key)
            if (
                cached is not None
                and cached[0] == stat.st_mtime_ns
                and cached[1] == stat.st_size
            ):
                if cached[2] is not None:
                    items.append(
                        cached[2] if with_bodies
                        else _dc_replace(cached[2], body_md=None)
                    )
                continue
            item = _parse_receipt_file(
                md_file,
                agent_dir.name,
                stat,
                receipt_name=fname,
            )
            _receipt_parse_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, item)
            if item is not None:
                items.append(item if with_bodies else _dc_replace(item, body_md=None))
    for stale in set(_receipt_parse_cache) - seen_paths:
        _receipt_parse_cache.pop(stale, None)
    return items


def _valid_receipt_relpath(filename: str) -> Optional[Path]:
    parts = filename.split("/")
    if len(parts) == 1:
        return Path(filename) if _RECEIPT_FILE_RE.match(parts[0]) else None
    if len(parts) == 2 and _RECEIPT_SUBDIR_RE.match(parts[0]) and _RECEIPT_FILE_RE.match(parts[1]):
        return Path(parts[0]) / parts[1]
    return None


def _read_receipt_item(agent: str, filename: str) -> Optional[_Item]:
    rel_path = _valid_receipt_relpath(filename)
    if not _TASK_ID_RE.match(agent) or rel_path is None:
        raise ValueError("invalid receipt id")
    receipts_root = (_receipts_root() / agent / "receipts").resolve(strict=False)
    if len(rel_path.parts) == 2 and (receipts_root / rel_path.parts[0]).is_symlink():
        return None
    target = receipts_root / rel_path
    # The collector deliberately excludes symlinks; the detail path must match
    # that policy so a symlinked receipt cannot be read by naming it directly.
    if target.is_symlink():
        return None
    target = target.resolve(strict=False)
    if not str(target).startswith(str(receipts_root) + "/"):
        raise ValueError("path escape")
    if not target.is_file():
        return None
    return _parse_receipt_file(target, agent, target.stat(), receipt_name=filename)


# ---------------------------------------------------------------------------
# Aggregation + Routen
# ---------------------------------------------------------------------------

def _apply_correction_overlay(items: list[_Item]) -> list[_Item]:
    """P6b — lege das operator-bestätigte Korrektur-Overlay NACH der
    deterministischen P6a-Ableitung an. Mutiert ``item.provenance`` auf den
    EFFEKTIVEN Wert (treibt Facetten/Filter/Badges) und setzt ``item.correction``
    (Original + aktive Felder + Audit). Ein Overlay-Store-Fehler darf die
    Bibliothek nie leeren — fail-soft auf die reine P6a-Ableitung."""
    from hermes_cli import library_corrections
    try:
        active = library_corrections.load_active()
    except Exception:
        logger.debug("library: correction overlay load failed", exc_info=True)
        return items
    if not active:
        return items
    for index, item in enumerate(items):
        record = active.get(item.id)
        if record is None:
            continue
        effective, block = library_corrections.apply(
            item.provenance or _unknown_provenance(), record,
        )
        if block is None:
            continue
        # Collector-Caches halten dieselben _Item-Instanzen über Requests. Das
        # Overlay darf diese abgeleiteten Originale nie mutieren, sonst bleiben
        # entfernte/revertierte Werte bis zu einem Source-mtime-Wechsel hängen.
        items[index] = _dc_replace(
            item,
            provenance=effective,
            correction=block,
        )
    return items


def _collect_all(*, with_bodies: bool) -> list[_Item]:
    with _collect_lock:
        items: list[_Item] = []
        for collector in (
            lambda: _collect_cron_items(with_bodies=with_bodies),
            lambda: _collect_research_items(with_bodies=with_bodies),
            lambda: _collect_deliverable_items(with_bodies=with_bodies),
            lambda: _collect_receipt_items(with_bodies=with_bodies),
        ):
            try:
                items.extend(collector())
            except Exception:
                # Ein kaputter Adapter darf die Bibliothek nie leeren.
                logger.debug("library: adapter failed", exc_info=True)
        # P6b — Overlay nach der Ableitung, VOR Sortierung/Pagination: List- und
        # Detailantwort teilen sich dieselbe Apply-Funktion (Parität).
        _apply_correction_overlay(items)
        items.sort(key=lambda i: -i.ts)
        return items


def _facet_counts(items: list[_Item], key: str) -> list[dict[str, Any]]:
    """Deterministic facet counts over a (pre-pagination) item set: count desc,
    then value asc. ``key`` selects the provenance field ("producer"/"path")."""
    counts: dict[str, int] = {}
    for item in items:
        value = (item.provenance or _unknown_provenance()).get(key, _UNKNOWN)
        counts[value] = counts.get(value, 0) + 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _list_items(
    category: Optional[str],
    q: Optional[str],
    limit: int,
    offset: int = 0,
    producers: Optional[list[str]] = None,
    paths: Optional[list[str]] = None,
) -> dict:
    needs_bodies = bool(q)
    items = _collect_all(with_bodies=needs_bodies)
    if category:
        items = [i for i in items if i.category == category]
    if q:
        needle = q.casefold()
        items = [
            i for i in items
            if needle in i.title.casefold()
            or needle in (i.body_md or "").casefold()
        ]
    # `items` ist jetzt der vollständige, nach Suche/Kategorie gefilterte
    # Bestand (vor Facetten-Filter und vor Pagination).
    producer_set = set(producers) if producers else None
    path_set = set(paths) if paths else None

    def _prov(item: _Item) -> dict[str, Any]:
        return item.provenance or _unknown_provenance()

    # Kontextuelle Facettenzahlen über den VOLLSTÄNDIGEN gefilterten Bestand —
    # Pagination darf sie nicht verfälschen. Innerhalb einer Facette gilt ODER,
    # zwischen Erzeuger und Weg UND: die Erzeuger-Zahlen berücksichtigen den
    # Weg-Filter (und Suche/Kategorie), die Weg-Zahlen den Erzeuger-Filter.
    path_filtered = items if not path_set else [i for i in items if _prov(i)["path"] in path_set]
    producer_filtered = items if not producer_set else [
        i for i in items if _prov(i)["producer"] in producer_set
    ]
    facets = {
        "producer": _facet_counts(path_filtered, "producer"),
        "path": _facet_counts(producer_filtered, "path"),
    }

    filtered = items
    if producer_set:
        filtered = [i for i in filtered if _prov(i)["producer"] in producer_set]
    if path_set:
        filtered = [i for i in filtered if _prov(i)["path"] in path_set]
    total = len(filtered)
    sliced = filtered[offset:offset + limit]
    return {
        "items": [i.as_dict(with_body=False) for i in sliced],
        "count": total,
        "truncated": total > limit,
        "has_more": offset + len(sliced) < total,
        "categories": list(CATEGORIES),
        "facets": facets,
        "now": int(_time.time()),
    }


def _get_item_derived(item_id: str) -> Optional[_Item]:
    """Rohes P6a-Item OHNE Korrektur-Overlay — die deterministische Ableitung.
    Dient dem Detail-Pfad (mit Overlay) UND dem Set-Pfad der Korrektur-Mutation
    (der den unverfälschten Vertrag für den Originalsnapshot braucht)."""
    parts = item_id.split("::")
    if parts[0] == "cron" and len(parts) == 4:
        return _read_cron_item(parts[1], parts[2], parts[3])
    if parts[0] == "research" and len(parts) == 2:
        return _read_research_item(parts[1])
    if parts[0] == "deliverable" and len(parts) == 3:
        return _read_deliverable_item(parts[1], parts[2])
    if parts[0] == "receipt" and len(parts) == 3:
        return _read_receipt_item(parts[1], parts[2])
    raise ValueError("unknown item kind")


def _get_item(item_id: str) -> Optional[_Item]:
    """Detail-Pfad: abgeleitetes Item + Korrektur-Overlay. Derselbe Apply-Schritt
    wie die Liste → identischer effektiver Vertrag (List-/Detail-Parität)."""
    item = _get_item_derived(item_id)
    if item is None:
        return None
    overlaid = [item]
    _apply_correction_overlay(overlaid)
    return overlaid[0]


# ---------------------------------------------------------------------------
# P6b — Korrektur-Overlay: synchroner Orchestrierungskern der Mutation
# (Blocking-FS; die Routen wrappen via asyncio.to_thread). Die Route hängt am
# Session-Gate (/api/, nie PUBLIC_API_PATHS) und verlangt confirm=true + reason;
# actor ist fest "operator" (Loopback = ein Operator, keine harte Identität).
# ---------------------------------------------------------------------------

def _correction_get(item_id: str) -> Optional[dict[str, Any]]:
    from hermes_cli import library_corrections
    record = library_corrections.read(item_id)  # validiert item_id (ValueError)
    if record is None:
        return None
    item = _get_item_derived(item_id)
    if item is None:
        return record
    return library_corrections.with_derived(
        record, item.provenance or _unknown_provenance(),
    )


def _correction_set(
    item_id: str, fields: dict[str, Any], reason: str, confirm: bool,
) -> dict[str, Any]:
    from hermes_cli import library_corrections
    # Abgeleiteten (unkorrigierten) Vertrag holen: validiert die ID strukturell
    # und liefert die Basis für den Originalsnapshot. Existiert das Item nicht,
    # wird keine Korrektur angelegt (kein Overlay ins Leere).
    item = _get_item_derived(item_id)
    if item is None:
        raise LookupError("item not found")
    record = library_corrections.set_correction(
        item_id, fields, reason,
        confirm=confirm,
        derived_provenance=item.provenance or _unknown_provenance(),
    )
    effective, _ = library_corrections.apply(
        item.provenance or _unknown_provenance(), record,
    )
    response_record = library_corrections.with_derived(
        record, item.provenance or _unknown_provenance(),
    )
    return {"correction": response_record, "provenance": effective}


def _correction_preview(item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Mutationsfreie, serverkanonische Vorschau für den Bestätigungsdialog."""
    from hermes_cli import library_corrections
    item = _get_item_derived(item_id)
    if item is None:
        raise LookupError("item not found")
    validated = library_corrections.validate_fields(fields)
    current_record = library_corrections.read(item_id)
    effective = library_corrections.preview(
        item.provenance or _unknown_provenance(), validated, current_record,
    )
    return {"provenance": effective, "fields": validated}


def _correction_revoke(
    item_id: str, reason: str, confirm: bool, fields: Optional[list[str]],
) -> Optional[dict[str, Any]]:
    from hermes_cli import library_corrections
    item = _get_item_derived(item_id)
    record = library_corrections.revert(
        item_id, reason, fields=fields, confirm=confirm,
    )
    if record is None:
        return None
    if item is None:
        return record
    return library_corrections.with_derived(
        record, item.provenance or _unknown_provenance(),
    )


def register_library_routes(app: Any) -> None:
    """Bibliothek-Routen. Unter /api/ → Session-Gate der Middleware;
    bewusst NIE in PUBLIC_API_PATHS."""
    from fastapi import Body, HTTPException, Query

    from hermes_cli import library_corrections, library_knowledge, library_state

    @app.get("/api/library/items")
    async def library_items(  # type: ignore[unused-variable]
        category: Optional[str] = Query(None),
        q: Optional[str] = Query(None, max_length=200),
        limit: int = Query(60, ge=1, le=200),
        offset: int = Query(0, ge=0),
        producer: Optional[list[str]] = Query(None),
        path: Optional[list[str]] = Query(None),
    ):
        if category and category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="unknown category")
        if producer and (
            len(producer) > 50
            or any(not value.strip() or len(value) > 160 for value in producer)
        ):
            raise HTTPException(status_code=400, detail="invalid producer facet")
        if path:
            if len(path) > len(PATH_VALUES):
                raise HTTPException(status_code=400, detail="invalid path facet")
            unknown = [value for value in path if value not in PATH_VALUES]
            if unknown:
                raise HTTPException(status_code=400, detail="unknown path facet")
        return await asyncio.to_thread(
            _list_items, category, q, limit, offset, producer, path,
        )

    @app.get("/api/library/item")
    async def library_item(  # type: ignore[unused-variable]
        id: str = Query(..., max_length=300),
    ):
        try:
            item = await asyncio.to_thread(_get_item, id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
        return item.as_dict(with_body=True)

    # --- P6b — Korrektur-Overlay (operator-bestätigt, ADR 0002) -------------
    @app.get("/api/library/correction")
    async def library_correction_get(  # type: ignore[unused-variable]
        id: str = Query(..., max_length=320),
    ):
        try:
            record = await asyncio.to_thread(_correction_get, id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"correction": record}

    @app.put("/api/library/correction")
    async def library_correction_set(  # type: ignore[unused-variable]
        payload: CorrectionSetPayload = Body(...),
    ):
        try:
            return await asyncio.to_thread(
                _correction_set,
                payload.item_id, payload.fields, payload.reason, payload.confirm,
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except library_corrections.CorrectionStoreError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            # confirm-Gate, Pflicht-Grund und Feld-Validierung.
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/library/correction/preview")
    async def library_correction_preview(  # type: ignore[unused-variable]
        payload: CorrectionPreviewPayload = Body(...),
    ):
        try:
            return await asyncio.to_thread(
                _correction_preview, payload.item_id, payload.fields,
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/library/correction/revoke")
    async def library_correction_revoke(  # type: ignore[unused-variable]
        payload: CorrectionRevokePayload = Body(...),
    ):
        try:
            record = await asyncio.to_thread(
                _correction_revoke,
                payload.item_id, payload.reason, payload.confirm, payload.fields,
            )
        except library_corrections.CorrectionStoreError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"correction": record}

    # --- Wissen/Kanon (Nachschlagewerk) — kuratiertes Referenzwissen ---------
    @app.get("/api/library/knowledge")
    async def library_knowledge_catalog(  # type: ignore[unused-variable]
        q: Optional[str] = Query(None, max_length=200),
    ):
        return await asyncio.to_thread(library_knowledge.list_knowledge, q)

    @app.get("/api/library/knowledge/doc")
    async def library_knowledge_doc(  # type: ignore[unused-variable]
        id: str = Query(..., max_length=300),
    ):
        try:
            doc = await asyncio.to_thread(library_knowledge.read_knowledge_doc, id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if doc is None:
            raise HTTPException(status_code=404, detail="knowledge doc not found")
        return doc

    @app.get("/api/library/saved-searches")
    async def library_saved_searches():  # type: ignore[unused-variable]
        items = await asyncio.to_thread(library_state.list_saved_searches)
        return {"items": items, "count": len(items)}

    @app.post("/api/library/saved-searches")
    async def library_create_saved_search(  # type: ignore[unused-variable]
        payload: SavedSearchCreate = Body(...),
    ):
        try:
            return await asyncio.to_thread(
                library_state.create_saved_search,
                name=payload.name,
                query=payload.query,
                topic_tags=payload.topic_tags,
                person_tags=payload.person_tags,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/api/library/saved-searches/{search_id}")
    async def library_update_saved_search(  # type: ignore[unused-variable]
        search_id: str,
        payload: SavedSearchUpdate = Body(...),
    ):
        try:
            updated = await asyncio.to_thread(
                library_state.update_saved_search,
                search_id,
                name=payload.name,
                query=payload.query,
                topic_tags=payload.topic_tags,
                person_tags=payload.person_tags,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if updated is None:
            raise HTTPException(status_code=404, detail="saved search not found")
        return updated

    @app.delete("/api/library/saved-searches/{search_id}")
    async def library_delete_saved_search(  # type: ignore[unused-variable]
        search_id: str,
    ):
        try:
            deleted = await asyncio.to_thread(library_state.delete_saved_search, search_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"deleted": deleted}

    @app.get("/api/library/topics")
    async def library_topics():  # type: ignore[unused-variable]
        items = await asyncio.to_thread(library_state.list_topics)
        return {"items": items, "count": len(items), "demo_topics": list(library_state.DEMO_TOPICS)}

    @app.post("/api/library/topics/{topic_id}/follow")
    async def library_follow_topic(topic_id: str):  # type: ignore[unused-variable]
        try:
            topic = await asyncio.to_thread(library_state.set_topic_follow, topic_id, True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if topic is None:
            raise HTTPException(status_code=404, detail="topic not found")
        return topic

    @app.delete("/api/library/topics/{topic_id}/follow")
    async def library_unfollow_topic(topic_id: str):  # type: ignore[unused-variable]
        try:
            topic = await asyncio.to_thread(library_state.set_topic_follow, topic_id, False)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if topic is None:
            raise HTTPException(status_code=404, detail="topic not found")
        return topic

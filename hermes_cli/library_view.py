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
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

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

# ---------------------------------------------------------------------------
# Kategorien (Grill-Entscheid §7.7: Backend-Dict, Fallback "briefings",
# kein jobs.json-Schema-Touch). Phase E: die WM-/KI-Jobs des research-Stores
# sind explizit gemappt — jeder Job ist eine Serie ("Abo").
# ---------------------------------------------------------------------------

CATEGORIES = ("news", "briefings", "recherchen", "familie", "arbeit", "receipts", "wartung")

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
    """job_id → {name, schedule_display, enabled} aus jobs.json (fail-soft)."""
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


# Entrauschung 2026-06-11: ``[SILENT]``-Ausgaben sind die Selbstauskunft
# "nichts Neues" (LLM-Pfad schreibt sie trotzdem als Output-File) — kein
# Lesestoff. Check tolerant wie der Delivery-Skip des Schedulers
# (SILENT_MARKER irgendwo im Inhalt, uppercased). Wichtig: Filter NACH dem
# Cache-Read anwenden, nie als Negativ-Eintrag cachen — sonst würden
# bestehende positive Cache-Einträge über den Hit-Pfad weiter ausgeliefert.
_SILENT_MARKER = "[SILENT]"


def _is_silent(item: _Item) -> bool:
    return _SILENT_MARKER in (item.body_md or item.preview or "").upper()


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
        }
        if with_body:
            d["body_md"] = self.body_md or ""
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
# meta_fingerprint = (series, category, series_meta) — invalidiert den Eintrag,
# wenn der Job umbenannt/umkategorisiert wird (Datei-mtime ändert sich da nicht).
_cron_parse_cache: dict[str, tuple[int, int, tuple[str, str, str], Optional[_Item]]] = {}
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


def _collect_cron_items(*, with_bodies: bool) -> list[_Item]:
    from dataclasses import replace as _dc_replace
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
            profile = store_id.split(":", 1)[1] if ":" in store_id else None
            for fname in _newest_output_names(job_dir):
                md_file = job_dir / fname
                try:
                    stat = md_file.stat()
                except OSError:
                    continue
                cache_key = str(md_file)
                seen_paths.add(cache_key)
                meta_fp = (name, category, series_meta)
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
                ts = _output_ts(md_file.name, stat.st_mtime)
                day = _time.strftime("%d.%m. %H:%M", _time.localtime(ts))
                item = _Item(
                    id=f"cron::{store_id}::{job_dir.name}::{md_file.name}",
                    category=category,
                    series_id=f"{store_id}/{job_dir.name}",
                    series=name,
                    title=f"{name} — Ausgabe {day}",
                    ts=ts,
                    preview=_preview(body),
                    source_ref=(
                        f"cron:{profile}/{job_dir.name}" if profile
                        else f"cron:{job_dir.name}"
                    ),
                    series_meta=series_meta,
                    body_md=body,
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
    ts = _output_ts(filename, target.stat().st_mtime)
    profile = store_id.split(":", 1)[1] if ":" in store_id else None
    return _Item(
        id=f"cron::{store_id}::{job_id}::{filename}",
        category=_categorize_job(job_id, name),
        series_id=f"{store_id}/{job_id}",
        series=name,
        title=f"{name} — Ausgabe {_time.strftime('%d.%m. %H:%M', _time.localtime(ts))}",
        ts=ts,
        preview=_preview(body),
        source_ref=f"cron:{profile}/{job_id}" if profile else f"cron:{job_id}",
        series_meta=meta.get("schedule_display", ""),
        body_md=body,
    )


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
            "SELECT id, title, status, created_at, completed_at FROM tasks "
            "WHERE tenant = 'research' AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        for row in rows:
            answer = None
            answer_ts = None
            comments = kanban_db.list_comments(conn, row["id"])
            if comments:
                answer = comments[-1].body
                answer_ts = comments[-1].created_at
            if not answer:
                continue  # Bibliothek zeigt Lesbares; offene Fragen wohnen im Research-Tab
            ts = int(answer_ts or row["completed_at"] or row["created_at"])
            items.append(_Item(
                id=f"research::{row['id']}",
                category="recherchen",
                series_id="research",
                series="Recherchen",
                title=row["title"],
                ts=ts,
                preview=_preview(answer),
                source_ref=f"task:{row['id']}",
                body_md=answer if with_bodies else None,
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
            "SELECT id, title, body, created_at, completed_at FROM tasks "
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
        return _Item(
            id=f"research::{task_id}",
            category="recherchen",
            series_id="research",
            series="Recherchen",
            title=row["title"],
            ts=ts,
            preview=_preview(answer),
            source_ref=f"task:{task_id}",
            body_md=body,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deliverables-Adapter (reports/by-task, nur Markdown)
# ---------------------------------------------------------------------------

_DELIVERABLE_MAX_PER_TASK = 3


def _collect_deliverable_items(*, with_bodies: bool, limit_tasks: int = 150) -> list[_Item]:
    items: list[_Item] = []
    reports_root = _hermes_home() / "reports" / "by-task"
    from hermes_cli import kanban_db
    titles: dict[str, str] = {}
    try:
        conn = kanban_db.connect()
        try:
            for row in conn.execute(
                "SELECT id, title FROM tasks WHERE status IN ('done', 'review')",
            ).fetchall():
                titles[row["id"]] = row["title"]
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
                items.append(_Item(
                    id=f"deliverable::{task_dir.name}::{rel}",
                    category="arbeit",
                    series_id="deliverables",
                    series="Arbeit & Receipts",
                    title=f"{task_title}{suffix}",
                    ts=int(stat.st_mtime),
                    preview=_preview(body),
                    source_ref=f"task:{task_dir.name}/{rel}",
                    body_md=body if with_bodies else None,
                ))
    receipt_paths = _receipt_file_paths()
    vault_root = (Path.home() / "vault").resolve()
    try:
        conn = kanban_db.connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT tr.task_id, tr.metadata, t.title, t.completed_at
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
            items.append(_Item(
                id=f"deliverable::{task_id}::{name}",
                category="arbeit",
                series_id="deliverables",
                series="Arbeit & Receipts",
                title=f"{task_title} - {name}",
                ts=int(stat.st_mtime),
                preview=_preview(body),
                source_ref=f"artifact:{task_id}/{name}",
                body_md=body if with_bodies else None,
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
    return _Item(
        id=f"deliverable::{task_id}::{rel_path}",
        category="arbeit",
        series_id="deliverables",
        series="Arbeit & Receipts",
        title=f"{task_id} · {rel_path}",
        ts=int(target.stat().st_mtime),
        preview=_preview(body),
        source_ref=f"task:{task_id}/{rel_path}",
        body_md=body,
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
                SELECT tr.metadata, t.title
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
            return _Item(
                id=f"deliverable::{task_id}::{name}",
                category="arbeit",
                series_id="deliverables",
                series="Arbeit & Receipts",
                title=f"{row['title'] or task_id} - {name}",
                ts=int(stat.st_mtime),
                preview=_preview(body),
                source_ref=f"artifact:{task_id}/{name}",
                body_md=body,
            )
    return None


# ---------------------------------------------------------------------------
# Receipts-Adapter (~/vault/03-Agents/<Agent>/receipts/*.md — read-only
# Quelle, wird NIE beschrieben). Serie = Agent. Der Hermes-Agent hält >500
# Receipts → newest-40-Cap pro Agent + Dir/Parse-mtime-Cache (Cron-Muster
# 1:1, Latenzfalle-Lehre vom 2026-06-11). Receipts haben kein Prompt/
# Response-Format → Body roh (gekappt), Frontmatter abgetrennt und als
# Meta-Zeile gerendert; fail-soft ohne Frontmatter (Titel = H1 → Dateiname,
# ts = mtime).
# ---------------------------------------------------------------------------

_RECEIPT_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}\.md$")
_RECEIPT_SUBDIRS = ("auto", "mother")
_MAX_RECEIPTS_FLAT = 40
_MAX_RECEIPTS_PER_SUBDIR = 40
_MAX_RECEIPTS_PER_AGENT = _MAX_RECEIPTS_FLAT
# path → (mtime_ns, size, geparstes Item oder None).
_receipt_parse_cache: dict[str, tuple[int, int, Optional[_Item]]] = {}
# receipts_dir → ((dir_mtime_ns, subdir_mtimes), newest Dateinamen, mtime-absteigend).
_receipt_dir_cache: dict[str, tuple[tuple[int, tuple[int | None, ...]], list[str]]] = {}


def _receipts_root() -> Path:
    return Path.home() / "vault" / "03-Agents"


def _newest_receipt_names(receipts_dir: Path) -> list[str]:
    """Newest receipts per source (flat + allowlisted subdirs).

    Receipt-Namen sind nicht zeitlich sortierbar. Der Cache hängt am
    Parent-dir-mtime und an den Subdir-mtimes, damit neue Auto-Receipts in
    ``receipts/auto`` invalidieren, obwohl der Parent unverändert bleiben kann.
    Eine reine Inhalts-Änderung fängt weiterhin der Parse-Cache.
    """
    key = str(receipts_dir)
    try:
        dir_mtime = receipts_dir.stat().st_mtime_ns
    except OSError:
        _receipt_dir_cache.pop(key, None)
        return []
    subdir_mtimes: list[int | None] = []
    for sub in _RECEIPT_SUBDIRS:
        subdir = receipts_dir / sub
        try:
            subdir_mtimes.append(subdir.stat().st_mtime_ns if subdir.is_dir() else None)
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
    for sub in _RECEIPT_SUBDIRS:
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


def _parse_receipt_file(md_file: Path, agent: str, stat: Any) -> Optional[_Item]:
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
    return _Item(
        id=f"receipt::{agent}::{md_file.name}",
        category="receipts",
        series_id=f"receipts/{agent}",
        series=agent,
        title=title,
        ts=int(stat.st_mtime),
        preview=_preview(body),
        source_ref=f"receipt:{agent}/{md_file.name}",
        body_md=f"> {' · '.join(meta_bits)}\n\n{body}" if meta_bits else body,
    )


def _collect_receipt_items(*, with_bodies: bool) -> list[_Item]:
    from dataclasses import replace as _dc_replace
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
            item = _parse_receipt_file(md_file, agent_dir.name, stat)
            _receipt_parse_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, item)
            if item is not None:
                items.append(item if with_bodies else _dc_replace(item, body_md=None))
    for stale in set(_receipt_parse_cache) - seen_paths:
        _receipt_parse_cache.pop(stale, None)
    return items


def _read_receipt_item(agent: str, filename: str) -> Optional[_Item]:
    if not _TASK_ID_RE.match(agent) or not _RECEIPT_FILE_RE.match(filename):
        raise ValueError("invalid receipt id")
    receipts_root = (_receipts_root() / agent / "receipts").resolve(strict=False)
    target = (receipts_root / filename).resolve(strict=False)
    if not str(target).startswith(str(receipts_root) + "/"):
        raise ValueError("path escape")
    if not target.is_file():
        return None
    return _parse_receipt_file(target, agent, target.stat())


# ---------------------------------------------------------------------------
# Aggregation + Routen
# ---------------------------------------------------------------------------

def _collect_all(*, with_bodies: bool) -> list[_Item]:
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
    items.sort(key=lambda i: -i.ts)
    return items


def _list_items(category: Optional[str], q: Optional[str], limit: int) -> dict:
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
    total = len(items)
    sliced = items[:limit]
    return {
        "items": [i.as_dict(with_body=False) for i in sliced],
        "count": total,
        "truncated": total > limit,
        "categories": list(CATEGORIES),
        "now": int(_time.time()),
    }


def _get_item(item_id: str) -> Optional[_Item]:
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


def register_library_routes(app: Any) -> None:
    """Bibliothek-Routen. Unter /api/ → Session-Gate der Middleware;
    bewusst NIE in PUBLIC_API_PATHS."""
    from fastapi import Body, HTTPException, Query

    from hermes_cli import library_knowledge, library_state

    @app.get("/api/library/items")
    async def library_items(  # type: ignore[unused-variable]
        category: Optional[str] = Query(None),
        q: Optional[str] = Query(None, max_length=200),
        limit: int = Query(60, ge=1, le=200),
    ):
        if category and category not in CATEGORIES:
            raise HTTPException(status_code=400, detail="unknown category")
        return await asyncio.to_thread(_list_items, category, q, limit)

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

    # --- Wissen/Kanon (Nachschlagewerk) — kuratiertes Referenzwissen ---------
    @app.get("/api/library/knowledge")
    async def library_knowledge_catalog(  # type: ignore[unused-variable]
        q: Optional[str] = Query(None, max_length=200),
    ):
        return await asyncio.to_thread(library_knowledge.list_knowledge, q)

    @app.get("/api/library/knowledge/doc")
    async def library_knowledge_doc(  # type: ignore[unused-variable]
        id: str = Query(..., max_length=160),
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

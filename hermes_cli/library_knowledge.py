"""Bibliothek → Wissen/Kanon (Programm 3, Nachschlagewerk).

Zweiter, gleichrangiger Bereich der Bibliothek neben dem chronologischen
Lesesaal (``library_view``): ein **kuratiertes, thema-geordnetes Nachschlagewerk**
des dauerhaften Referenzwissens auf dem Homeserver — gedacht für Agenten *und*
den Operator zum Nachschlagen.

Sechs Sammlungen (Regale):
  1. **Kanon** — ``~/vault/00-Canon/*.md`` (geteilte Cross-Agent-Wahrheit:
     Topologie, Konventionen & Gates, Roster, Projekt-Landkarte, Memory).
  2. **Orchestrierung** — ``~/orchestration/CLAUDE.md`` (Verfassung) +
     ``PLAYBOOK.md`` (gesammelte Lehren).
  3. **Claude-Skills** — ``~/.claude/skills/*/SKILL.md`` (was die Agenten
     können; Titel/Kurzbeschreibung aus dem Frontmatter).
  4. **Subagent-Rollen** — ``~/.claude/agents/*.md`` (die Lese-/Bau-/Urteils-
     Rollen).
  5. **LLM-Wiki** — ``~/llm-wiki/wiki/**/*.md`` (agentisch gepflegte Quellen,
     Konzepte, Entitäten, Abfragen und Synthesen).
  6. **Vault Plans** — ``~/vault/03-Agents/*/plans/**/*.md`` (Plan-Dokumente
     mit Frontmatter-Parse für Titel, Status, Tags und Summary).

Sicherheits-Vertrag (wie ``library_view``): read-only, unter ``/api/`` (erbt
das Session-Gate, nie in PUBLIC_API_PATHS), Blocking-FS via ``asyncio.to_thread``.
Die statischen Docs werden über eine **fixe Registry** aufgelöst (Pfad steht im
Code, nie aus dem Request → kein Traversal). Dynamische Docs (Skills/Rollen)
lösen über einen streng validierten Slug + ``startswith``-Escape-Guard auf.
Geschrieben wird **nichts** — keine der Quellen wird je angefasst.
"""

from __future__ import annotations

import logging
import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Eine Datei wird ungekappt selten >70 KB (PLAYBOOK); Schranke gegen Ausreißer.
_MAX_BODY_BYTES = 512 * 1024
_SUMMARY_CHARS = 220
# Slug eines Skills/einer Rolle (= Verzeichnis-/Dateiname). Streng: kein Punkt,
# kein Slash → kann nie aus dem Wurzelverzeichnis ausbrechen.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Relativer Wiki-Pfad unter ~/llm-wiki/wiki. Erlaubt "overview.md" und
# "concepts/foo.md", aber keine Punkte ausser ".md", keine Backslashes, kein
# ".." und keine Request-gelieferte absolute Pfadkomponente.
_LLM_WIKI_REL_RE = re.compile(r"^(?:[a-z0-9][a-z0-9_-]*/)*[a-z0-9][a-z0-9_-]*\.md$")
# Relativer Plan-Pfad unter ~/vault/03-Agents. Erwartet Agent-Verzeichnis +
# plans/ + optionale Unterordner. Agent-Ordner im Vault verwenden teils CamelCase
# und Bindestriche (Hermes, Claude-Code), Pfadsegmente bleiben traversal-sicher.
_VAULT_PLAN_REL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/plans/(?:[A-Za-z0-9][A-Za-z0-9_-]*/)*"
    r"[A-Za-z0-9][A-Za-z0-9_-]*\.md$"
)


# ---------------------------------------------------------------------------
# Wurzeln (alle aus Path.home() abgeleitet → per Test über Path.home monkeypatchbar)
# ---------------------------------------------------------------------------

def _canon_root() -> Path:
    return Path.home() / "vault" / "00-Canon"


def _orchestration_root() -> Path:
    return Path.home() / "orchestration"


def _skills_root() -> Path:
    return Path.home() / ".claude" / "skills"


def _agents_root() -> Path:
    return Path.home() / ".claude" / "agents"


def _llm_wiki_root() -> Path:
    return Path.home() / "llm-wiki" / "wiki"


def _vault_agents_root() -> Path:
    return Path.home() / "vault" / "03-Agents"


# ---------------------------------------------------------------------------
# Sammlungen (Regale) — feste Reihenfolge, Akzentfarbe + Icon-Hinweis fürs UI
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Collection:
    id: str
    title: str
    description: str
    accent: str  # ToneName-kompatibler Akzent fürs Frontend
    icon: str    # lucide-Icon-Name (Frontend mappt)


_COLLECTIONS: tuple[_Collection, ...] = (
    _Collection(
        "kanon",
        "Kanon — Die geteilte Wahrheit",
        "Dauerhafte, agent-übergreifende Fakten. Hermes · Claude · Codex lesen "
        "und pflegen hier — statt jeder in sein eigenes Notizbuch.",
        accent="cyan",
        icon="Landmark",
    ),
    _Collection(
        "orchestrierung",
        "Orchestrierung",
        "Wie Claude als Orchestrator Agenten steuert — die Verfassung und die "
        "gesammelten Lehren aus echten Läufen.",
        accent="violet",
        icon="Workflow",
    ),
    _Collection(
        "skills",
        "Claude-Skills",
        "Was die Agenten von Haus aus können: jede Skill mit Zweck und vollem "
        "Spielbuch.",
        accent="amber",
        icon="Sparkles",
    ),
    _Collection(
        "rollen",
        "Subagent-Rollen",
        "Die spezialisierten Lese-, Bau- und Urteils-Rollen, an die delegiert "
        "wird.",
        accent="emerald",
        icon="Users",
    ),
    _Collection(
        "llm-wiki",
        "LLM-Wiki",
        "Agentisch gepflegtes Wissen aus Quellen, Konzepten, Entitäten, "
        "Abfragen und Synthesen — direkt aus ~/llm-wiki/wiki.",
        accent="indigo",
        icon="Brain",
    ),
    _Collection(
        "vault-plans",
        "Vault Plans",
        "Plan-Dokumente aus ~/vault/03-Agents/*/plans — mit Titel, Status, Tags "
        "und Summary direkt aus dem Frontmatter.",
        accent="rose",
        icon="Newspaper",
    ),
)

_COLLECTION_INDEX: dict[str, _Collection] = {c.id: c for c in _COLLECTIONS}


# ---------------------------------------------------------------------------
# Statische Docs (Kanon + Orchestrierung) — fixe Registry, kein Pfad aus Request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _StaticDoc:
    key: str               # stabiler Schlüssel → Teil der Item-ID (kb::doc::<key>)
    collection: str
    title: str
    summary: str           # kuratierte Kurzbeschreibung (überschreibt Auto-Extrakt)
    rel: tuple[str, ...]   # Pfad relativ zu Path.home() — hartcodiert
    tags: tuple[str, ...] = ()


_STATIC_DOCS: tuple[_StaticDoc, ...] = (
    # --- Kanon ---
    _StaticDoc(
        "canon-index", "kanon", "Canon-Index",
        "Einstieg und Grundregel: was kanonisch hier lebt — und was bewusst nicht.",
        ("vault", "00-Canon", "_index.md"), ("kanon", "einstieg"),
    ),
    _StaticDoc(
        "canon-infra-topology", "kanon", "Infrastruktur & Topologie",
        "Ports, Pfade, Services, Hosts — wie der Homeserver verdrahtet ist.",
        ("vault", "00-Canon", "infra-topology.md"), ("topologie", "ports", "pfade"),
    ),
    _StaticDoc(
        "canon-conventions-gates", "kanon", "Konventionen & Gates",
        "Verbindliche Gates und die Zusammenarbeit: Check-IN → Receipt → Check-OUT.",
        ("vault", "00-Canon", "conventions-gates.md"), ("gates", "konventionen", "provenienz"),
    ),
    _StaticDoc(
        "canon-agent-roster", "kanon", "Agenten-Roster",
        "Wer ist wer: Hermes, Claude, Codex — Rollen, Tokens, Zuständigkeiten.",
        ("vault", "00-Canon", "agent-roster.md"), ("agenten", "rollen"),
    ),
    _StaticDoc(
        "canon-projects-map", "kanon", "Projekt-Landkarte",
        "Die aktiven Ziele und ihre Pfade: Hermes, Family Organizer, Orchestrierung.",
        ("vault", "00-Canon", "projects-map.md"), ("projekte", "pfade"),
    ),
    _StaticDoc(
        "canon-memory-architecture", "kanon", "Memory-Architektur",
        "Wie Erinnerung organisiert ist: Canon vs. Auto-Memory, Scopes, Pflege.",
        ("vault", "00-Canon", "memory-architecture.md"), ("memory", "architektur"),
    ),
    # --- Orchestrierung ---
    _StaticDoc(
        "orch-constitution", "orchestrierung", "Verfassung (Orchestrator)",
        "Die Grundregeln des Orchestrators: triage → delegieren → integrieren → Gate.",
        ("orchestration", "CLAUDE.md"), ("orchestrierung", "regeln"),
    ),
    _StaticDoc(
        "orch-playbook", "orchestrierung", "Playbook — Lehren",
        "Die gesammelten Orchestrierungs-Lessons aus echten Läufen, selbst gepflegt.",
        ("orchestration", "PLAYBOOK.md"), ("lessons", "playbook"),
    ),
    _StaticDoc(
        "orch-loop-engineering", "orchestrierung", "Loop Engineering — Anleitung",
        "Das Paradigma hinter selbstlaufenden Agent-Loops: Anatomie, die fünf "
        "Bausteine, naiv vs. state-of-the-art, Mechanik in unserer Umgebung.",
        ("orchestration", "docs", "LOOP_ENGINEERING.md"), ("loops", "prompting", "agenten"),
    ),
    _StaticDoc(
        "orch-loop-prompts", "orchestrierung", "Loop Engineering — 3 Ops-Loops",
        "Kopierfertige reaktive /loop-Prompts: Backlog-Drain, Fleet-Watchdog und "
        "adversarialer Verify-Gate — fürs Kanban-Fleet und die Qualität.",
        ("orchestration", "docs", "LOOP_ENGINEERING_PROMPTS.md"), ("loops", "ops", "kanban"),
    ),
    _StaticDoc(
        "orch-loop-build-kit", "orchestrierung", "Loop Engineering — Build-Baukasten",
        "Generative Build-Loops: Spec-Vorlage, fünf Muster und drei vollständige "
        "/loop-Prompts für Features, Dashboard-Tabs und Kanban-Weiterentwicklung.",
        ("orchestration", "docs", "LOOP_ENGINEERING_BUILD_KIT.md"), ("loops", "bauen", "dashboard"),
    ),
)

_STATIC_INDEX: dict[str, _StaticDoc] = {d.key: d for d in _STATIC_DOCS}


# ---------------------------------------------------------------------------
# Item-Repräsentation
# ---------------------------------------------------------------------------

@dataclass
class _KbDoc:
    id: str
    collection: str
    title: str
    summary: str
    source_ref: str
    tags: list[str]
    updated_ts: int
    heading_count: int
    body_md: Optional[str] = field(default=None, repr=False)
    extra: dict[str, str] = field(default_factory=dict)

    def as_card(self) -> dict[str, Any]:
        card: dict[str, Any] = {
            "id": self.id,
            "collection": self.collection,
            "title": self.title,
            "summary": self.summary,
            "source_ref": self.source_ref,
            "tags": list(self.tags),
            "updated_ts": self.updated_ts,
            "heading_count": self.heading_count,
        }
        card.update(self.extra)
        return card

    def as_detail(self) -> dict[str, Any]:
        d = self.as_card()
        d["body_md"] = self.body_md or ""
        return d


# ---------------------------------------------------------------------------
# Markdown-Helfer
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


def _read_text(path: Path) -> Optional[str]:
    # Gebündelt lesen: höchstens _MAX_BODY_BYTES (+1 zum Erkennen der Kappung) —
    # nie eine ganze Riesendatei in den Speicher ziehen.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(_MAX_BODY_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_BODY_BYTES:
        raw = raw[:_MAX_BODY_BYTES]
    return raw


def _parse_meta(fm_text: str) -> dict[str, str]:
    """Frontmatter-Block → flaches dict. Primär ``yaml.safe_load`` (korrekt für
    Block-Scalars wie ``description: >-`` über mehrere Zeilen, Quotes, etc.);
    bei Parse-Fehlern ein flacher ``key: value``-Fallback. Werte werden zu
    einzeiligen, whitespace-normalisierten Strings reduziert."""
    try:
        import yaml
        data = yaml.safe_load(fm_text)
        if isinstance(data, dict):
            return {
                str(k).lower(): " ".join(str(v).split())
                for k, v in data.items()
                if not isinstance(v, (list, dict)) and v is not None
            }
    except Exception:
        logger.debug("knowledge: yaml frontmatter parse failed", exc_info=True)
    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        stripped = line.strip()
        if ":" in stripped and not line.startswith((" ", "\t", "-")):
            key, _, value = stripped.partition(":")
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta


def _parse_frontmatter_data(fm_text: str) -> dict[str, Any]:
    """Frontmatter als strukturiertes dict. Für das LLM-Wiki brauchen wir
    neben Strings auch Listen wie ``tags:``; bei YAML-Problemen fällt die
    Funktion auf den flachen Parser zurück."""
    try:
        import yaml
        data = yaml.safe_load(fm_text)
        if isinstance(data, dict):
            return {str(k).lower(): v for k, v in data.items()}
    except Exception:
        logger.debug("knowledge: rich yaml frontmatter parse failed", exc_info=True)
    return _parse_meta(fm_text)


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """``---``-umrahmtes Frontmatter abtrennen → (meta, body). Fail-soft: ohne
    sauberes ``---``-Paar zählt alles als Body."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            meta = _parse_meta("\n".join(lines[1:idx]))
            return meta, "\n".join(lines[idx + 1:]).lstrip("\n")
    return {}, raw  # nie geschlossen → kein Frontmatter


def _split_frontmatter_rich(raw: str) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            meta = _parse_frontmatter_data("\n".join(lines[1:idx]))
            return meta, "\n".join(lines[idx + 1:]).lstrip("\n")
    return {}, raw


def _split_vault_plan_frontmatter(raw: str, *, rel: str) -> tuple[dict[str, Any], str]:
    """``---``-Frontmatter abtrennen. Fail-soft wie ``_split_frontmatter``, aber
    mit Warnung: kaputtes YAML darf den Plan nie aus dem Regal werfen — er
    bleibt gelistet, nur mit leeren Metadaten (Titel fällt dann über
    ``_first_heading``/Dateiname zurück, s. ``_build_vault_plan``)."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    for idx in range(1, len(lines)):
        if lines[idx].strip() != "---":
            continue
        fm_text = "\n".join(lines[1:idx])
        body = "\n".join(lines[idx + 1:]).lstrip("\n")
        import yaml

        try:
            data = yaml.safe_load(fm_text)
        except Exception as exc:
            logger.warning(
                "knowledge: vault plan has malformed frontmatter, listing with empty metadata: %s (%s)",
                rel,
                exc,
            )
            return {}, body
        if data is None:
            meta: dict[str, Any] = {}
        elif isinstance(data, dict):
            meta = {str(k).lower(): v for k, v in data.items()}
        else:
            logger.warning(
                "knowledge: vault plan has malformed frontmatter, listing with empty metadata: "
                "%s (frontmatter is not a mapping)",
                rel,
            )
            return {}, body
        return meta, body
    return {}, raw


def _count_headings(body: str) -> int:
    count = 0
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced and _HEADING_RE.match(line):
            count += 1
    return count


def _summarize(body: str, fallback: str) -> str:
    """Erster echter Absatz als Auto-Kurzbeschreibung (für Skills/Rollen ohne
    kuratierten Text). Headings/Blockquotes/Listen werden übersprungen."""
    fenced = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not line:
            continue
        if line.startswith(("#", ">", "-", "*", "|", "<!--")):
            continue
        flat = " ".join(line.split())
        if len(flat) > _SUMMARY_CHARS:
            flat = flat[: _SUMMARY_CHARS - 1].rstrip() + "…"
        return flat
    return fallback


def _first_sentence(text: str, limit: int = _SUMMARY_CHARS) -> str:
    flat = " ".join(text.split())
    # Bis zum ersten Satzende, sonst hart kappen.
    m = re.search(r"^(.{40,}?[.!?])\s", flat)
    out = m.group(1) if m else flat
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _meta_string(meta: dict[str, Any], key: str) -> str:
    value = meta.get(key)
    if value is None or isinstance(value, (list, dict)):
        return ""
    return " ".join(str(value).split())


def _meta_string_list(meta: dict[str, Any], key: str) -> list[str]:
    value = meta.get(key)
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if item is None or isinstance(item, (list, dict)):
                continue
            text = " ".join(str(item).split())
            if text:
                out.append(text)
        return out
    if value is None or isinstance(value, dict):
        return []
    text = " ".join(str(value).split())
    return [text] if text else []


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            return " ".join(m.group(2).split())
    return ""


def _llm_wiki_type(rel: str, meta: dict[str, Any]) -> str:
    declared = _meta_string(meta, "type").lower()
    if declared:
        return declared
    first = rel.split("/", 1)[0]
    inferred = {
        "concepts": "concept",
        "entities": "entity",
        "queries": "query",
        "sources": "source",
        "models": "model",
        "lint": "lint",
        "overview.md": "overview",
        "synthesis.md": "synthesis",
    }
    return inferred.get(first, "page" if first.endswith(".md") else first)


def _build_llm_wiki(rel: str, *, with_body: bool) -> Optional[_KbDoc]:
    if not _LLM_WIKI_REL_RE.match(rel):
        return None
    root = _llm_wiki_root().resolve(strict=False)
    target = (root / rel).resolve(strict=False)
    if not str(target).startswith(str(root) + "/") or not target.is_file():
        return None
    raw = _read_text(target)
    if raw is None:
        return None
    meta, body = _split_frontmatter_rich(raw)
    wiki_type = _llm_wiki_type(rel, meta)
    title = _meta_string(meta, "title") or _first_heading(body) or Path(rel).stem.replace("-", " ").title()
    summary = (
        _meta_string(meta, "summary")
        or _meta_string(meta, "description")
        or _summarize(body, title)
    )
    tags = ["llm-wiki", f"type:{wiki_type}"]
    for tag in _meta_string_list(meta, "tags"):
        if tag not in tags:
            tags.append(tag)
    try:
        updated = int(target.stat().st_mtime)
    except OSError:
        updated = 0
    return _KbDoc(
        id=f"kb::llm::{rel}",
        collection="llm-wiki",
        title=title,
        summary=summary,
        source_ref=f"llm-wiki/{rel}",
        tags=tags,
        updated_ts=updated,
        heading_count=_count_headings(body),
        body_md=body if with_body else None,
    )


def _build_vault_plan(rel: str, *, with_body: bool) -> Optional[_KbDoc]:
    if not _VAULT_PLAN_REL_RE.match(rel):
        return None
    root = _vault_agents_root().resolve(strict=False)
    target = (root / rel).resolve(strict=False)
    if not str(target).startswith(str(root) + "/") or not target.is_file():
        return None
    raw = _read_text(target)
    if raw is None:
        return None
    meta, body = _split_vault_plan_frontmatter(raw, rel=rel)
    title = _meta_string(meta, "title") or _first_heading(body) or Path(rel).stem.replace("-", " ").title()
    summary = (
        _meta_string(meta, "summary")
        or _meta_string(meta, "description")
        or _summarize(body, title)
    )
    extra = {
        key: value
        for key in ("created", "owner", "type", "status")
        if (value := _meta_string(meta, key))
    }
    plan_type = extra.get("type") or "plan"
    status = extra.get("status", "")
    tags = ["vault-plan", f"type:{plan_type}"]
    if status:
        tags.append(f"status:{status}")
    for tag in _meta_string_list(meta, "tags"):
        if tag not in tags:
            tags.append(tag)
    try:
        updated = int(target.stat().st_mtime)
    except OSError:
        updated = 0
    return _KbDoc(
        id=f"kb::plan::{rel}",
        collection="vault-plans",
        title=title,
        summary=summary,
        source_ref=f"vault/03-Agents/{rel}",
        tags=tags,
        updated_ts=updated,
        heading_count=_count_headings(body),
        body_md=body if with_body else None,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Doc-Aufbau pro Quelle
# ---------------------------------------------------------------------------

def _build_static(doc: _StaticDoc, *, with_body: bool) -> Optional[_KbDoc]:
    path = Path.home().joinpath(*doc.rel)
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        updated = int(path.stat().st_mtime)
    except OSError:
        updated = 0
    return _KbDoc(
        id=f"kb::doc::{doc.key}",
        collection=doc.collection,
        title=doc.title,
        summary=doc.summary,
        source_ref="/".join(doc.rel),
        tags=list(doc.tags),
        updated_ts=updated,
        heading_count=_count_headings(raw),
        body_md=raw if with_body else None,
    )


def _build_skill(slug: str, *, with_body: bool) -> Optional[_KbDoc]:
    if not _SLUG_RE.match(slug):
        return None
    root = _skills_root().resolve(strict=False)
    target = (root / slug / "SKILL.md").resolve(strict=False)
    if not str(target).startswith(str(root) + "/") or not target.is_file():
        return None
    raw = _read_text(target)
    if raw is None:
        return None
    meta, body = _split_frontmatter(raw)
    title = meta.get("name", slug) or slug
    summary = _first_sentence(meta["description"]) if meta.get("description") else _summarize(body, slug)
    try:
        updated = int(target.stat().st_mtime)
    except OSError:
        updated = 0
    return _KbDoc(
        id=f"kb::skill::{slug}",
        collection="skills",
        title=title,
        summary=summary,
        source_ref=f"skills/{slug}",
        tags=["skill"],
        updated_ts=updated,
        heading_count=_count_headings(body),
        body_md=body if with_body else None,
    )


def _build_role(slug: str, *, with_body: bool) -> Optional[_KbDoc]:
    if not _SLUG_RE.match(slug):
        return None
    root = _agents_root().resolve(strict=False)
    target = (root / f"{slug}.md").resolve(strict=False)
    if not str(target).startswith(str(root) + "/") or not target.is_file():
        return None
    raw = _read_text(target)
    if raw is None:
        return None
    meta, body = _split_frontmatter(raw)
    title = meta.get("name", slug) or slug
    summary = _first_sentence(meta["description"]) if meta.get("description") else _summarize(body, slug)
    model = meta.get("model")
    tags = ["rolle"] + ([model] if model else [])
    try:
        updated = int(target.stat().st_mtime)
    except OSError:
        updated = 0
    return _KbDoc(
        id=f"kb::role::{slug}",
        collection="rollen",
        title=title,
        summary=summary,
        source_ref=f"agents/{slug}.md",
        tags=tags,
        updated_ts=updated,
        heading_count=_count_headings(body),
        body_md=body if with_body else None,
    )


def _scan_skill_slugs() -> list[str]:
    root = _skills_root()
    if not root.is_dir():
        return []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    slugs: list[str] = []
    for entry in entries:
        if entry.is_dir() and _SLUG_RE.match(entry.name) and (entry / "SKILL.md").is_file():
            slugs.append(entry.name)
    return sorted(slugs)


def _scan_role_slugs() -> list[str]:
    root = _agents_root()
    if not root.is_dir():
        return []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    slugs: list[str] = []
    for entry in entries:
        if entry.is_file() and entry.suffix == ".md" and _SLUG_RE.match(entry.stem):
            slugs.append(entry.stem)
    return sorted(slugs)


def _scan_llm_wiki_rels() -> list[str]:
    root = _llm_wiki_root()
    if not root.is_dir():
        return []
    try:
        entries = list(root.rglob("*.md"))
    except OSError:
        return []
    rels: list[str] = []
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            rel = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if _LLM_WIKI_REL_RE.match(rel):
            rels.append(rel)

    rank = {
        "overview.md": 0,
        "synthesis.md": 1,
        "concepts": 2,
        "entities": 3,
        "queries": 4,
        "sources": 5,
        "models": 6,
        "lint": 7,
    }

    def sort_key(rel: str) -> tuple[int, str]:
        head = rel.split("/", 1)[0]
        return rank.get(rel, rank.get(head, 9)), rel

    return sorted(rels, key=sort_key)


def _scan_vault_plan_rels() -> list[str]:
    root = _vault_agents_root()
    if not root.is_dir():
        return []
    try:
        entries = list(root.glob("*/plans/**/*.md"))
    except OSError:
        return []
    rels: list[str] = []
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            rel = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if _VAULT_PLAN_REL_RE.match(rel):
            rels.append(rel)
    return sorted(rels, key=str.casefold)


# ---------------------------------------------------------------------------
# Wissens-Puls: jüngste Einträge aus dem cron-gepflegten model-log fürs
# llm-wiki-Regal ("Neu entdeckt: <Modell> · <Datum>").
# ---------------------------------------------------------------------------

# Zeilenformat laut model-log.md selbst: "- YYYY-MM-DD `model-id` (context Xk,
# $Y/$Z per 1M)". Der Klammerteil ist optional (nur der Beleg-Text).
_MODEL_LOG_LINE_RE = re.compile(
    r"^-\s+(\d{4}-\d{2}-\d{2})\s+`([^`]+)`\s*(?:\((.*)\))?\s*$"
)


def _model_log_pulse(limit: int = 3) -> list[dict[str, str]]:
    """Jüngste ``limit`` Discovery-Zeilen aus ``wiki/models/model-log.md``,
    neuestes zuerst (Datei ist append-only, jüngstes steht unten)."""
    path = _llm_wiki_root() / "models" / "model-log.md"
    raw = _read_text(path)
    if raw is None:
        return []
    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        m = _MODEL_LOG_LINE_RE.match(line.strip())
        if not m:
            continue
        date, model, detail = m.group(1), m.group(2), (m.group(3) or "").strip()
        entries.append({"date": date, "model": model, "detail": detail})
    return list(reversed(entries[-limit:]))


def _collect_docs(*, with_bodies: bool) -> list[_KbDoc]:
    docs: list[_KbDoc] = []
    for static in _STATIC_DOCS:
        try:
            built = _build_static(static, with_body=with_bodies)
        except Exception:  # ein kaputtes Doc darf das Regal nie leeren
            logger.debug("knowledge: static doc failed: %s", static.key, exc_info=True)
            built = None
        if built is not None:
            docs.append(built)
    for slug in _scan_skill_slugs():
        try:
            built = _build_skill(slug, with_body=with_bodies)
        except Exception:
            logger.debug("knowledge: skill failed: %s", slug, exc_info=True)
            built = None
        if built is not None:
            docs.append(built)
    for slug in _scan_role_slugs():
        try:
            built = _build_role(slug, with_body=with_bodies)
        except Exception:
            logger.debug("knowledge: role failed: %s", slug, exc_info=True)
            built = None
        if built is not None:
            docs.append(built)
    for rel in _scan_llm_wiki_rels():
        try:
            built = _build_llm_wiki(rel, with_body=with_bodies)
        except Exception:
            logger.debug("knowledge: llm-wiki page failed: %s", rel, exc_info=True)
            built = None
        if built is not None:
            docs.append(built)
    for rel in _scan_vault_plan_rels():
        try:
            built = _build_vault_plan(rel, with_body=with_bodies)
        except Exception:
            logger.debug("knowledge: vault plan failed: %s", rel, exc_info=True)
            built = None
        if built is not None:
            docs.append(built)
    return docs


def _matches(doc: _KbDoc, needle: str) -> bool:
    if needle in doc.title.casefold() or needle in doc.summary.casefold():
        return True
    if any(needle in tag.casefold() for tag in doc.tags):
        return True
    return needle in (doc.body_md or "").casefold()


# ---------------------------------------------------------------------------
# Öffentliche API (von library_view-Routen aufgerufen)
# ---------------------------------------------------------------------------

def list_knowledge(q: Optional[str] = None) -> dict[str, Any]:
    """Karteikatalog: Sammlungen mit ihren Doc-Karten (ohne Body). Bei ``q``
    Volltext-Filter über Titel/Kurzbeschreibung/Tags/Body; leere Sammlungen
    fallen raus."""
    needle = q.strip().casefold() if q and q.strip() else None
    docs = _collect_docs(with_bodies=needle is not None)
    if needle:
        docs = [d for d in docs if _matches(d, needle)]

    by_collection: dict[str, list[_KbDoc]] = {}
    for doc in docs:
        by_collection.setdefault(doc.collection, []).append(doc)

    collections_out: list[dict[str, Any]] = []
    for col in _COLLECTIONS:
        members = by_collection.get(col.id, [])
        if needle and not members:
            continue  # bei aktiver Suche leere Regale ausblenden
        # Statische Docs in Registry-Reihenfolge, dynamische alphabetisch — das
        # garantiert _collect_docs schon (static zuerst, Slugs sortiert).
        entry: dict[str, Any] = {
            "id": col.id,
            "title": col.title,
            "description": col.description,
            "accent": col.accent,
            "icon": col.icon,
            "doc_count": len(members),
            "updated_ts": max((d.updated_ts for d in members), default=0),
            "docs": [d.as_card() for d in members],
        }
        if col.id == "llm-wiki":
            entry["pulse"] = _model_log_pulse()
        collections_out.append(entry)

    return {
        "collections": collections_out,
        "count": len(docs),
        "query": q or "",
        "now": int(_time.time()),
    }


def read_knowledge_doc(doc_id: str) -> Optional[dict[str, Any]]:
    """Einzeldokument mit vollem ``body_md``. ID-Form: ``kb::<kind>::<rest>``.
    Statische Docs lösen über die Registry (kein Traversal), Skills/Rollen über
    validierten Slug. Unbekannte Form → ValueError (→ 400), fehlende Datei →
    None (→ 404)."""
    parts = doc_id.split("::")
    if len(parts) != 3 or parts[0] != "kb":
        raise ValueError("invalid knowledge id")
    kind, rest = parts[1], parts[2]
    if kind == "doc":
        static = _STATIC_INDEX.get(rest)
        if static is None:
            raise ValueError("unknown knowledge doc")
        built = _build_static(static, with_body=True)
    elif kind == "skill":
        if not _SLUG_RE.match(rest):
            raise ValueError("invalid skill slug")
        built = _build_skill(rest, with_body=True)
    elif kind == "role":
        if not _SLUG_RE.match(rest):
            raise ValueError("invalid role slug")
        built = _build_role(rest, with_body=True)
    elif kind == "llm":
        if not _LLM_WIKI_REL_RE.match(rest):
            raise ValueError("invalid llm-wiki path")
        built = _build_llm_wiki(rest, with_body=True)
    elif kind == "plan":
        if not _VAULT_PLAN_REL_RE.match(rest):
            raise ValueError("invalid vault plan path")
        built = _build_vault_plan(rest, with_body=True)
    else:
        raise ValueError("unknown knowledge kind")
    return built.as_detail() if built is not None else None

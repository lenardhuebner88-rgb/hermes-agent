"""Bibliothek (/control) — Modelle: Landscape + gequellte Benchmarks +
Prompting-Guides.

Dritter Bereich neben Lesesaal/Nachschlagewerk/Ergebnisse (Programm 3). Alle
drei Quellen leben ausschliesslich im llm-wiki, geschrieben von den
research-profile Crons:
  - ``wiki/models/model-landscape.md`` — deterministischer Cron-Output
    (``model-return-watch.py``, ``_build_landscape_content``): pro Provider
    eine Markdown-Tabelle mit fixem Kopf/Zellformat, top-5 neueste Modelle.
  - ``wiki/models/data/benchmarks.json`` — S2-Output (``benchmark-sync.py``):
    gequellte Scores je Modell-Id, ``sources``-Registry für Name/URL.
  - ``wiki/prompting/<family>.md`` — S3-Output: 8 kuratierte Guides,
    Frontmatter ``family``/``model_ids``/``updated``/``maturity``/``sources``.

Dieses Modul liest NUR — kein Schreibzugriff, kein Editieren von Wiki-Inhalt
aus dem Dashboard (Anti-Scope S4). Registrierung wie die Geschwister-Regale:
``register_library_models_routes(app)`` unter ``/api/`` (erbt das
Session-Gate der Middleware, nie in ``PUBLIC_API_PATHS``).

Landscape-Parser (S4.2b/S4.3): der Tabellenkopf/die Zellformate sind ein
deterministischer Schreiber (siehe ``model-return-watch.py:416-458`` —
gelesen, nicht geraten). Ein unerwarteter Kopf (umbenannte/verschobene
Spalten) wirft ``LandscapeParseError`` statt leer zu bleiben. Preis-/Datums-
Zellen duerfen laut demselben Schreiber legitim ``"-"`` sein
(``_fmt_price``/``_fmt_date``-Fallback bei fehlendem Wert) — das ist eine
gültige, sparse Zeile, kein Format-Drift, und wird als ``None`` geparst statt
einen Fehler zu werfen. Verdikt (S4.3-Entscheidungspunkt): der Schreiber ist
stabil genug, dass dieser String-Parser NICHT brüchig ist — kein Wechsel auf
ein zusätzliches ``landscape.json`` nötig.

family-Mapping (``FAMILY_PREFIXES``/``family_for_id``) ist eine dritte Kopie
derselben Tabelle aus ``benchmark-sync.py`` (dort bereits ein zweites Mal in
``model-return-watch.py`` dupliziert, "kept consistent", bewusst nicht
importiert, weil beide eigenständige Subprozess-Skripte bleiben sollen).
Hier gilt derselbe Grund nochmal verschärft: ``hermes_cli`` läuft in einem
komplett anderen venv/Prozessraum als die research-profile Scripts — es gibt
keine Paketgrenze, über die man importieren könnte. Bei einer neuen
Familie/Prefix müssen alle drei Tabellen von Hand synchron gehalten werden.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.library_knowledge import (
    _llm_wiki_root,
    _model_log_pulse,
    _read_text,
    _split_frontmatter_rich,
)

logger = logging.getLogger(__name__)


class LandscapeParseError(ValueError):
    """model-landscape.md no longer matches the parser's expectations
    (renamed/reordered columns, unrecognized cell shape) — loud by design
    (S4.2b): a silently empty models list would be the worse failure mode."""


# ---------------------------------------------------------------------------
# family/provider mapping — see module docstring for the duplication rationale.
# ---------------------------------------------------------------------------

_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("anthropic/claude-fable", "claude-fable"),
    ("anthropic/claude-opus", "claude-opus"),
    ("anthropic/claude-sonnet", "claude-sonnet-haiku"),
    ("anthropic/claude-haiku", "claude-sonnet-haiku"),
    ("anthropic/claude-3-haiku", "claude-sonnet-haiku"),
    ("google/gemini", "gemini"),
    ("moonshotai/kimi", "kimi"),
    ("minimax/", "minimax"),
    ("deepseek/", "deepseek"),
)


def family_for_id(model_id: str) -> str:
    """Derive the broad family bucket from a model-id prefix. gpt5-codex
    needs a substring check (OpenAI's codex-branded ids aren't a clean
    prefix set: gpt-5-codex, gpt-5.1-codex, gpt-5.1-codex-max, ...)."""
    low = model_id.lower()
    if low.startswith("openai/") and "codex" in low:
        return "gpt5-codex"
    for prefix, family in _FAMILY_PREFIXES:
        if low.startswith(prefix):
            return family
    return "other"


def provider_for_id(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else "other"


# ---------------------------------------------------------------------------
# Landscape-Parser
# ---------------------------------------------------------------------------

_EXPECTED_HEADER = ("Modell-ID", "Erstellt", "Kontext", "Prompt/Completion pro 1M")
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-+:?\s*\|){4}\s*$")
_PRICE_CELL_RE = re.compile(
    r"^(?:\$([0-9]+(?:\.[0-9]+)?)|-)\s*/\s*(?:\$([0-9]+(?:\.[0-9]+)?)|-)$"
)
_ID_CELL_RE = re.compile(r"^`([^`]+)`$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_UPDATED_STAMP_RE = re.compile(r"^Zuletzt aktualisiert:\s*(.+?)\s*$")
_WATCHLIST_SECTION = "watchlist"


def _split_row_cells(line: str) -> list[str]:
    m = _TABLE_ROW_RE.match(line)
    if not m:
        raise LandscapeParseError(f"malformed table row (missing pipes): {line!r}")
    return [cell.strip() for cell in m.group(1).split("|")]


def _parse_price_cell(cell: str) -> tuple[Optional[float], Optional[float]]:
    m = _PRICE_CELL_RE.match(cell)
    if not m:
        raise LandscapeParseError(f"unrecognized price cell: {cell!r}")
    price_in = float(m.group(1)) if m.group(1) is not None else None
    price_out = float(m.group(2)) if m.group(2) is not None else None
    return price_in, price_out


def parse_landscape(body_md: str) -> list[dict[str, Any]]:
    """Parse model-landscape.md's (frontmatter-stripped) body into flat model
    rows. Sections without a model table (currently only "Watchlist", plain
    bullets) are skipped by name; any section that DOES start a table with an
    unexpected header raises ``LandscapeParseError``."""
    lines = body_md.splitlines()
    models: list[dict[str, Any]] = []
    section: Optional[str] = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(1).strip()
            i += 1
            continue
        if (
            section is not None
            and section.casefold() != _WATCHLIST_SECTION
            and line.startswith("|")
        ):
            header_cells = _split_row_cells(line)
            if tuple(header_cells) != _EXPECTED_HEADER:
                raise LandscapeParseError(
                    f"unexpected landscape table header in section {section!r}: "
                    f"{header_cells!r} (expected {list(_EXPECTED_HEADER)})"
                )
            i += 1
            if i >= n or not _SEPARATOR_RE.match(lines[i]):
                raise LandscapeParseError(
                    f"missing/malformed table separator after header in section {section!r}"
                )
            i += 1
            while i < n and lines[i].startswith("|"):
                cells = _split_row_cells(lines[i])
                if len(cells) != 4:
                    raise LandscapeParseError(
                        f"expected 4 cells in data row, got {len(cells)}: {lines[i]!r}"
                    )
                id_match = _ID_CELL_RE.match(cells[0])
                if not id_match:
                    raise LandscapeParseError(
                        f"model id cell not backtick-wrapped: {cells[0]!r}"
                    )
                model_id = id_match.group(1)
                created = cells[1] if cells[1] != "-" else None
                context = cells[2]
                price_in, price_out = _parse_price_cell(cells[3])
                models.append({
                    "id": model_id,
                    "provider": provider_for_id(model_id),
                    "family": family_for_id(model_id),
                    "context": context,
                    "price_in": price_in,
                    "price_out": price_out,
                    "created": created,
                })
                i += 1
            continue
        i += 1
    return models


def _parse_updated_stamp(body_md: str, frontmatter: dict[str, Any]) -> str:
    for line in reversed(body_md.splitlines()):
        m = _UPDATED_STAMP_RE.match(line.strip())
        if m:
            return m.group(1)
    fm_updated = frontmatter.get("updated")
    return str(fm_updated) if fm_updated else ""


# ---------------------------------------------------------------------------
# Benchmarks (S2 shape)
# ---------------------------------------------------------------------------

def _benchmarks_path() -> Path:
    return _llm_wiki_root() / "models" / "data" / "benchmarks.json"


def _load_benchmarks() -> dict[str, Any]:
    raw = _read_text(_benchmarks_path())
    if raw is None:
        return {"sources": {}, "models": []}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("library_models: benchmarks.json malformed, ignoring", exc_info=True)
        return {"sources": {}, "models": []}
    return data if isinstance(data, dict) else {"sources": {}, "models": []}


def _resolved_scores_by_id(benchmarks: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    sources = benchmarks.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    raw_models = benchmarks.get("models")
    out: dict[str, list[dict[str, Any]]] = {}
    for m in raw_models if isinstance(raw_models, list) else []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str):
            continue
        scores_out: list[dict[str, Any]] = []
        raw_scores = m.get("scores")
        for score in raw_scores if isinstance(raw_scores, list) else []:
            if not isinstance(score, dict):
                continue
            source_id = score.get("source")
            source_meta = sources.get(source_id, {}) if isinstance(source_id, str) else {}
            scores_out.append({
                **score,
                "source_name": source_meta.get("name", source_id),
                "source_url": source_meta.get("url", ""),
            })
        out[mid] = scores_out
    return out


# ---------------------------------------------------------------------------
# Prompting guides (S3 shape)
# ---------------------------------------------------------------------------

_GUIDE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class _Guide:
    family: str
    updated: str
    maturity: str
    title: str
    model_ids: list[str]
    frontmatter: dict[str, Any]
    body_md: str


def _prompting_root() -> Path:
    return _llm_wiki_root() / "prompting"


def _scan_guide_slugs() -> list[str]:
    root = _prompting_root()
    if not root.is_dir():
        return []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return sorted(
        entry.stem for entry in entries
        if entry.is_file() and entry.suffix == ".md" and _GUIDE_SLUG_RE.match(entry.stem)
    )


def _load_guide(slug: str) -> Optional[_Guide]:
    if not _GUIDE_SLUG_RE.match(slug):
        return None
    root = _prompting_root().resolve(strict=False)
    target = (root / f"{slug}.md").resolve(strict=False)
    if not str(target).startswith(str(root) + "/") or not target.is_file():
        return None
    raw = _read_text(target)
    if raw is None:
        return None
    meta, body = _split_frontmatter_rich(raw)
    model_ids_raw = meta.get("model_ids")
    model_ids = [str(x) for x in model_ids_raw] if isinstance(model_ids_raw, list) else []
    return _Guide(
        family=str(meta.get("family") or slug),
        updated=str(meta.get("updated") or ""),
        maturity=str(meta.get("maturity") or ""),
        title=str(meta.get("title") or slug),
        model_ids=model_ids,
        frontmatter=meta,
        body_md=body,
    )


def _load_all_guides() -> list[_Guide]:
    guides: list[_Guide] = []
    for slug in _scan_guide_slugs():
        try:
            g = _load_guide(slug)
        except Exception:
            logger.debug("library_models: guide failed: %s", slug, exc_info=True)
            g = None
        if g is not None:
            guides.append(g)
    return guides


def _resolve_guide_family(model_id: str, family: str, guides: list[_Guide]) -> Optional[str]:
    """S3 resolution rule (S4.2c): frontmatter ``model_ids`` EXACT match
    first (needed e.g. for the GA GPT-5.6 sol/terra/luna ids, whose family
    bucket alone is "other"), then family-prefix fallback."""
    for g in guides:
        if model_id in g.model_ids:
            return g.family
    for g in guides:
        if g.family == family:
            return g.family
    return None


# ---------------------------------------------------------------------------
# Merge + public API
# ---------------------------------------------------------------------------

def build_models_payload() -> dict[str, Any]:
    """Rebuild the full /api/library/models payload from disk on every call.

    No mtime-keyed memoization dict: ``library_knowledge.py`` (the sibling
    Nachschlagewerk regal, same read-only-files pattern) doesn't cache either
    — it just rereads its small source files per request. The three sources
    here (model-landscape.md, benchmarks.json, 8 prompting guides) are
    similarly small (well under 100 KB combined); ``library_view.py``'s
    heavier mtime-cache exists only because that store holds >100k historical
    cron outputs — a scale problem this module doesn't have. Matching the
    lighter, precedent-set pattern instead of adding cache-invalidation
    surface for no measured benefit.
    """
    landscape_path = _llm_wiki_root() / "models" / "model-landscape.md"
    raw = _read_text(landscape_path)
    if raw is None:
        raise LandscapeParseError(f"model-landscape.md not found at {landscape_path}")
    meta, body = _split_frontmatter_rich(raw)
    landscape_models = parse_landscape(body)
    updated = _parse_updated_stamp(body, meta)

    scores_by_id = _resolved_scores_by_id(_load_benchmarks())
    guides = _load_all_guides()

    models_out: list[dict[str, Any]] = []
    for m in landscape_models:
        models_out.append({
            **m,
            "scores": scores_by_id.get(m["id"], []),
            "guide_family": _resolve_guide_family(m["id"], m["family"], guides),
        })

    return {
        "updated": updated,
        "models": models_out,
        "pulse": _model_log_pulse(),
        "guides": [
            {"family": g.family, "updated": g.updated, "maturity": g.maturity, "title": g.title}
            for g in guides
        ],
    }


def get_guide_detail(family: str) -> Optional[dict[str, Any]]:
    if not _GUIDE_SLUG_RE.match(family):
        raise ValueError("invalid guide family")
    g = _load_guide(family)
    if g is None:
        return None
    return {"family": g.family, "frontmatter": g.frontmatter, "body_md": g.body_md}


def register_library_models_routes(app: Any) -> None:
    """Modelle-Routen. Unter /api/ → Session-Gate der Middleware; bewusst NIE
    in PUBLIC_API_PATHS."""
    from fastapi import HTTPException, Query

    @app.get("/api/library/models")
    async def library_models():  # type: ignore[unused-variable]
        try:
            return await asyncio.to_thread(build_models_payload)
        except LandscapeParseError as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/library/models/guide")
    async def library_models_guide(  # type: ignore[unused-variable]
        family: str = Query(..., max_length=64),
    ):
        try:
            detail = await asyncio.to_thread(get_guide_detail, family)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if detail is None:
            raise HTTPException(status_code=404, detail="guide not found")
        return detail

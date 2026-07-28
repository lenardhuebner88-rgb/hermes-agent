"""Modell-Katalog für den Loop-Runner — dieselbe Breite wie der Lanes-Tab.

Bis 2026-07-28 kannte der Loop-Runner nur die handgepflegte Liste in
``loops/models.yaml`` (6 Engines, ~30 Modelle), während der Lanes-Tab den
vollen Provider-Katalog aus ``hermes_cli.inventory`` anbot. Ein Modell, das im
Lanes-Tab wählbar war, ließ sich im Loops-Tab nicht einstellen — und der
fail-closed Membership-Check in ``_validate_effective_phase_catalog`` wies es
zusätzlich beim Start ab.

Dieses Modul ist die EINE Quelle für beide Seiten: Dashboard-Angebot
(``/api/loops/models``) und Runner-Validierung lesen dieselbe Funktion. Liefen
sie auseinander, böte das UI Modelle an, die der Start dann verweigert — genau
der Fehlermodus, den der kuratierte Katalog ursprünglich verhindern sollte.

Der Filter ist die ENGINE: jede Loop-Engine ist ein bestimmtes Abo-CLI, und ein
``claude -p`` kann kein Qwen-Modell fahren. Deshalb wird der Provider-Katalog
pro Engine auf die Provider gemappt, deren Modelle dieses CLI wirklich annimmt.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Engine → Provider-Zuordnung wird ABGELEITET, nicht gepflegt. Zwei Quellen,
# in dieser Reihenfolge:
#
#   1. ``inventory_providers:`` in loops/models.yaml — Daten, kein Code. Wer
#      eine Zuordnung braucht, die der Name nicht hergibt (``codex`` →
#      ``openai-codex``), trägt sie dort ein, ohne Python anzufassen.
#   2. Namensverwandtschaft zum Provider-Slug des Inventorys. Deckt den
#      Normalfall automatisch ab: ``kimi`` fängt ``kimi-coding`` UND ein neu
#      hinzukommendes ``kimi-coding-cn``, ``xai`` fängt ``xai-oauth``,
#      ``neuralwatt`` und ``alibaba-token-plan`` sich selbst.
#
# Damit erscheint ein neu konfigurierter Provider oder ein neues Modell im
# Loops-Tab, sobald Hermes es kennt — ohne Codeänderung und ohne Deploy.
#
# Ein Alias-Sonderfall bleibt bewusst Daten statt Magie: dass ``codex`` den
# Provider ``openai-codex`` meint, ist keine Namensverwandtschaft, sondern
# Produktwissen. Es steht in models.yaml.


def _slug_matches_engine(engine: str, slug: str) -> bool:
    """Namensverwandtschaft Engine ↔ Provider-Slug.

    Bewusst an Segmentgrenzen (``-``) statt an beliebigen Teilstrings: sonst
    finge eine Engine ``ai`` jeden Slug mit ``ai`` darin. ``kimi`` matcht
    ``kimi-coding``; ``xai`` matcht ``xai-oauth``, aber nicht ``xiaomi``.
    """
    engine = (engine or "").strip().lower()
    slug = (slug or "").strip().lower()
    if not engine or not slug:
        return False
    if engine == slug:
        return True
    return slug.startswith(f"{engine}-") or engine.startswith(f"{slug}-")

# Die claude-Engine zieht NICHT aus dem Inventory, sondern aus der SSOT für die
# `claude -p`-Abo-Lane — dieselbe Liste, aus der Lanes-Tab und Fleet→Heute ihre
# Claude-Optionen bauen. Ein Anthropic-API-Modell aus dem Inventory wäre hier
# falsch: `claude --model` akzeptiert nur die CLI-eigenen Ids.
_CLAUDE_CLI_ENGINE = "claude"

# Die hermes-Engine wählt PROFILE, keine Modelle (`hermes -p <profil>`); das
# Profil trägt seine Provider-/Modellbindung selbst. Bleibt kuratiert.
_PROFILE_ENGINES = frozenset({"hermes"})


def _is_image_model(provider: str, model_id: str) -> bool:
    """Bild-/Video-Generator statt Chat-Modell.

    Gleiche Erkennung wie im Lanes-Tab (``plugins/kanban/dashboard/lane_routes.py``
    ``_lane_image_model``): erst models.dev-Ausgabemodalitäten, dann die
    dokumentierten Alibaba-Muster für Provider, die models.dev nicht kennt.
    Die Logik ist hier gespiegelt statt importiert, weil lane_routes als
    Plugin-Fragment in einen eigenen exec()-Namespace geladen wird und nicht
    als Modul importierbar ist.
    """
    model_id = (model_id or "").strip().lower()
    if not model_id:
        return False
    try:
        from agent.models_dev import get_model_info

        info = get_model_info((provider or "").strip().lower(), model_id)
        if info is not None and info.output_modalities:
            outs = {str(m).lower() for m in info.output_modalities}
            return "image" in outs and "text" not in outs
    except Exception:
        pass
    if model_id.startswith("qwen-image"):
        return True
    return model_id.startswith("wan") and "-image" in model_id


def _is_offerable(provider: str, model_id: str) -> bool:
    """Kann dieses Modell eine Loop-Phase fahren?

    Zwei Ausschlüsse, beide aus der Lanes-Verifikation vom 2026-07-24 übernommen:
      - Bild-/Video-Generatoren (s. ``_is_image_model``) — sie können keinen
        Prompt beantworten, ein Loop mit ihnen produziert nur Fehlrunden.
      - ``openai-codex``-Ids auf ``-pro``: der Codex-Endpoint serviert sie nicht
        direkt, ein Aufruf fällt still auf ein anderes Modell zurück. Ein
        Loop, der glaubt auf ``-pro`` zu laufen, liefe damit unbemerkt anders.
    """
    model_lower = (model_id or "").strip().lower()
    if provider == "openai-codex" and model_lower.endswith("-pro"):
        return False
    return not _is_image_model(provider, model_id)


# Der Inventory-Aufbau probt Provider und kostet ~1s. Ein Pack-Listing fragt
# ihn pro Engine und Phase — ungecacht wurde daraus eine messbare Bremse (die
# Testsuite ging von 8s auf 334s hoch). TTL statt lru_cache, damit ein neu
# konfigurierter Provider ohne Prozess-Neustart auftaucht: „dynamisch" heißt
# auch, dass ein Dashboard, das seit Stunden läuft, ihn sieht.
_INVENTORY_TTL_SECONDS = 60.0
_inventory_cache: tuple[float, dict] | None = None


def reset_inventory_cache() -> None:
    """Cache verwerfen (Tests, und nach einer Provider-Änderung)."""
    global _inventory_cache
    _inventory_cache = None


def _inventory_payload() -> dict:
    """Das Provider-Inventory — dieselbe Quelle, aus der der Lanes-Tab baut.

    Fehlschläge sind bewusst nicht fatal: der Runner läuft als eigene
    systemd-Unit und darf nicht sterben, nur weil ein Provider-Probe hakt. Der
    Aufrufer fällt dann auf die kuratierte Liste zurück — kleiner, aber nie leer.
    """
    global _inventory_cache
    now = time.monotonic()
    if _inventory_cache is not None and now - _inventory_cache[0] < _INVENTORY_TTL_SECONDS:
        return _inventory_cache[1]
    payload = _build_inventory_payload()
    _inventory_cache = (now, payload)
    return payload


def _build_inventory_payload() -> dict:
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        return build_models_payload(
            load_picker_context(),
            include_unconfigured=True,
            picker_hints=True,
            capabilities=True,
            max_models=200,
        )
    except Exception:
        log.exception("loops: Inventory-Katalog nicht lesbar — kuratierte Liste gilt")
        return {}


def _inventory_models(engine: str, declared: tuple[str, ...]) -> list[str]:
    """Alle Modelle, die diese Engine laut Inventory fahren kann.

    ``declared`` sind die in models.yaml eingetragenen Provider; zusätzlich
    greift die Namensverwandtschaft. Ein Provider, der zu keiner Engine passt,
    taucht nirgends auf — der Runner soll kein Modell anbieten, für das er
    kein CLI hat.
    """
    wanted = {p.strip().lower() for p in declared if isinstance(p, str) and p.strip()}
    out: list[str] = []
    for row in _inventory_payload().get("providers") or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or row.get("id") or "").strip().lower()
        if not slug or (slug not in wanted and not _slug_matches_engine(engine, slug)):
            continue
        for model in row.get("models") or []:
            if not isinstance(model, str) or not model.strip():
                continue
            model = model.strip()
            if _is_offerable(slug, model):
                out.append(model)
    return out


def _claude_cli_models() -> list[str]:
    try:
        from hermes_cli.claude_cli_model_catalog import load_claude_cli_lane_models

        return [str(item["id"]) for item in load_claude_cli_lane_models() if item.get("id")]
    except Exception:
        log.exception("loops: claude-cli-Katalog nicht lesbar — kuratierte Liste gilt")
        return []


def _hermes_profiles() -> list[str]:
    """Vorhandene Hermes-Profile — die 'Modelle' der hermes-Engine.

    Dynamisch aus dem Profil-Verzeichnis, damit ein neu angelegtes Profil ohne
    Codeänderung im Loops-Tab wählbar ist.
    """
    try:
        from hermes_cli.profiles import list_profiles

        return [p.name for p in list_profiles() if getattr(p, "name", "").strip()]
    except Exception:
        log.exception("loops: Profil-Liste nicht lesbar — kuratierte Liste gilt")
        return []


def models_for_engine(
    engine: str,
    curated: list[str] | None = None,
    inventory_providers: list[str] | None = None,
) -> list[str]:
    """Wählbare Modelle einer Engine: kuratierte Liste ZUERST, dann der Rest.

    Die kuratierten Einträge aus ``models.yaml`` bleiben vorn — sie sind die
    bewährten Default-Routen und sollen im Dropdown oben stehen. Danach folgt,
    was der Provider-Katalog dynamisch hergibt. Dedupliziert, Reihenfolge
    stabil (keine Sortierung: die Katalogreihenfolge ist die Anzeigereihenfolge).
    """
    ordered = [m for m in (curated or []) if isinstance(m, str) and m.strip()]
    if engine in _PROFILE_ENGINES:
        extra = _hermes_profiles()
    elif engine == _CLAUDE_CLI_ENGINE:
        extra = _claude_cli_models()
    else:
        extra = _inventory_models(engine, tuple(inventory_providers or ()))
    return list(dict.fromkeys([*ordered, *extra]))


def catalog_with_dynamic_models(engines: dict) -> dict:
    """models.yaml-Katalog + dynamische Modelle, EINE Quelle für beide Seiten.

    Dashboard-Angebot und Runner-Validierung rufen genau das hier auf. Liefen
    sie auseinander, böte das UI Modelle an, die der Start dann ablehnt.
    """
    out: dict = {}
    for name, entry in (engines or {}).items():
        if not isinstance(entry, dict):
            out[name] = entry
            continue
        out[name] = {
            **entry,
            "models": models_for_engine(
                name,
                entry.get("models") or [],
                entry.get("inventory_providers") or [],
            ),
        }
    return out

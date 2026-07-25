"""Claude-Abo-Modellkatalog — der eine Leser der einen Quelle.

``loops/models.yaml`` → ``engines.claude.models`` ist die Single Source of Truth
für die Modelle, die über die Max-Abo-Lane (``claude -p --model <id>``) laufen.
Der Loop-Runner liest diese Liste direkt (fail-closed Membership-Check in
``loops/runner.py``); der Lanes-Tab und Fleet→Heute brauchen dagegen
Dropdown-Optionen mit Label/Runtime/Gruppe. Dieses Modul ist die Brücke: es
übersetzt die Katalog-IDs in Lane-Optionen, damit ein neues Claude-Modell nur an
EINER Stelle eingetragen werden muss.

Fehlerverhalten ist bewusst fail-soft: kann der Katalog nicht gelesen werden,
liefert der Loader eine leere Liste statt die ``/api/lanes``-Route zu sprengen —
das Frontend fällt dann auf ``FALLBACK_MODELS`` zurück (selbst aus dieser Quelle
generiert). Dass die echte Datei lesbar ist und die erwarteten Modelle trägt,
sichert stattdessen ein Gate-Test ab (``tests/hermes_cli/
test_claude_cli_model_catalog.py``) — fail-soft in der Route, fail-loud im Gate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "loops" / "models.yaml"

#: Engine-Key im Katalog, der die Max-Abo-Modelle trägt.
CATALOG_ENGINE = "claude"

#: UI-Gruppierung der Lane-Dropdowns. Reiner Anzeige-String, kein Katalog-Fakt —
#: darum hier und nicht in der YAML (die trägt Modelle, keine UI-Struktur).
LANE_GROUP = "Claude (Max-Abo)"

#: Worker-Runtime dieser Modelle: `claude -p`, nicht der Hermes-Agent-Loop.
LANE_RUNTIME = "claude-cli"


def _derive_label(model_id: str) -> str:
    """``claude-opus-4-8`` → ``Claude Opus 4.8``.

    Zusammenhängende Ziffern-Segmente werden zur Versionsnummer verklebt
    (``4``+``8`` → ``4.8``), Wort-Segmente bleiben eigene Wörter
    (``claude-opus-5-fast`` → ``Claude Opus 5 Fast``). So braucht die YAML keine
    handgepflegten Labels — ein neues Modell ist eine einzige Zeile.
    """
    parts = [p for p in model_id.split("-") if p]
    if len(parts) < 2 or parts[0].lower() != "claude":
        # Kein erkennbares Claude-Schema: die ID ist ihr eigenes Label, statt
        # sie zu verstümmeln.
        return model_id
    chunks: list[str] = []
    for part in parts[2:]:
        if part.isdigit() and chunks and chunks[-1].replace(".", "").isdigit():
            chunks[-1] = f"{chunks[-1]}.{part}"
        else:
            chunks.append(part if part.isdigit() else part.capitalize())
    return " ".join(["Claude", parts[1].capitalize(), *chunks])


def load_catalog_model_ids(path: Path | None = None) -> list[str]:
    """Die rohen Modell-IDs aus ``engines.claude.models``, Reihenfolge erhalten.

    Leere Liste, wenn der Katalog fehlt oder unbrauchbar ist (siehe Modul-Docstring).
    """
    catalog_path = path or CATALOG_PATH
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Claude-Modellkatalog %s nicht lesbar: %s", catalog_path, exc)
        return []
    engines = raw.get("engines")
    if not isinstance(engines, dict):
        logger.warning("Claude-Modellkatalog %s hat keinen engines-Block", catalog_path)
        return []
    entry = engines.get(CATALOG_ENGINE)
    models = entry.get("models") if isinstance(entry, dict) else None
    if not isinstance(models, list):
        logger.warning(
            "Claude-Modellkatalog %s: engines.%s.models fehlt oder ist keine Liste",
            catalog_path, CATALOG_ENGINE,
        )
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for item in models:
        model_id = str(item).strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            ids.append(model_id)
    return ids


def load_claude_cli_lane_models(path: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Lane-Dropdown-Optionen für die Max-Abo-Modelle.

    Feldform ist die des Lanes-Katalogs (``LaneModelOption`` im Frontend):
    ``id``/``label``/``runtime``/``group``/``provider``/``locked``. ``provider`` ist
    ``None``, weil ``claude -p`` seine Auth aus dem Abo-Login zieht und keinen
    Hermes-Provider-Eintrag durchläuft.
    """
    return tuple(
        {
            "id": model_id,
            "label": _derive_label(model_id),
            "runtime": LANE_RUNTIME,
            "group": LANE_GROUP,
            "provider": None,
            "locked": False,
        }
        for model_id in load_catalog_model_ids(path)
    )

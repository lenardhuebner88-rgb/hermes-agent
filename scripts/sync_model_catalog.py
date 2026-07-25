#!/usr/bin/env python3
"""Generiert die Frontend-Sicht des Claude-Abo-Katalogs aus der einen Quelle.

Quelle:  loops/models.yaml → engines.claude.models
Ziel:    web/src/control/generated/claudeModels.ts

Das Frontend braucht die Claude-Modelle an zwei Stellen offline, also ohne
Backend-Antwort: als ``FALLBACK_MODELS`` im Lanes-Katalog und als Freitext-
Vorschläge im ``ModelPicker``. Statt beide Listen von Hand zu pflegen (und beim
nächsten Modell zu vergessen), werden sie hier erzeugt und eingecheckt.

    python3 scripts/sync_model_catalog.py            # schreiben
    python3 scripts/sync_model_catalog.py --check    # Drift → exit 1 (Gate)

Der ``--check``-Modus ist Teil der Gates: driftet die generierte Datei von der
YAML ab, wird das rot, statt still zu veralten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.claude_cli_model_catalog import (  # noqa: E402
    CATALOG_PATH,
    LANE_GROUP,
    LANE_RUNTIME,
    load_claude_cli_lane_models,
)

TARGET = REPO_ROOT / "web" / "src" / "control" / "generated" / "claudeModels.ts"

HEADER = """// GENERIERT von scripts/sync_model_catalog.py — NICHT von Hand bearbeiten.
// Quelle: loops/models.yaml → engines.claude.models (Single Source of Truth für
// die Claude-Max-Abo-Modelle). Ein neues Modell wird DORT eingetragen; danach
// `python3 scripts/sync_model_catalog.py` laufen lassen. Drift ist gate-rot.

/** Ein Modell der Max-Abo-Lane (`claude -p --model <id>`). */
export interface GeneratedClaudeModel {
  id: string;
  label: string;
  runtime: "%(runtime)s";
  group: string;
  provider: null;
  locked: false;
}

"""


def render(models: tuple[dict[str, object], ...]) -> str:
    """Der TS-Quelltext für den aktuellen Katalogstand."""
    lines = [HEADER % {"runtime": LANE_RUNTIME}]
    lines.append("export const CLAUDE_CLI_MODELS: GeneratedClaudeModel[] = [\n")
    for model in models:
        lines.append(
            f'  {{ id: "{model["id"]}", label: "{model["label"]}", '
            f'runtime: "{LANE_RUNTIME}", group: "{LANE_GROUP}", '
            "provider: null, locked: false },\n"
        )
    lines.append("];\n\n")
    lines.append("/** Nur die IDs — für Freitext-Vorschlagslisten (datalist). */\n")
    lines.append("export const CLAUDE_CLI_MODEL_IDS: string[] = CLAUDE_CLI_MODELS.map(\n")
    lines.append("  (model) => model.id,\n")
    lines.append(");\n")
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="nur prüfen, ob die eingecheckte Datei aktuell ist (exit 1 bei Drift)",
    )
    args = parser.parse_args()

    models = load_claude_cli_lane_models()
    if not models:
        print(f"FEHLER: {CATALOG_PATH} liefert keine Claude-Modelle", file=sys.stderr)
        return 2
    rendered = render(models)

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != rendered:
            print(
                f"FEHLER: {TARGET.relative_to(REPO_ROOT)} ist nicht aktuell.\n"
                "Fix: python3 scripts/sync_model_catalog.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {TARGET.relative_to(REPO_ROOT)} ist aktuell ({len(models)} Modelle)")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"geschrieben: {TARGET.relative_to(REPO_ROOT)} ({len(models)} Modelle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

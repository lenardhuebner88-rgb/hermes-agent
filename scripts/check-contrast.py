#!/usr/bin/env python3
"""check-contrast.py — WCAG-Kontrast-Ratchet fuer die Leitstand-Tokens.

Warum es das gibt: das Frontend-Gate hat bis 2026-07-28 genau EINE Design-Regel
mechanisch erzwungen (keine rohen Hex-Literale, s. check-design-tokens.sh). Ob
die Tokens ueberhaupt lesbar sind, hat nichts geprueft — `--color-ink-3` stand
seit Bestehen bei #757166 und fiel damit auf ALLEN vier Flaechen durch AA
(3,92 / 3,71 / 3,46 / 3,08:1), waehrend es im Bestand unter ganzen Saetzen
stand. Genau diese Fehlerklasse faengt dieses Skript.

Es rechnet die WCAG-2.1-Kontrastformel ueber die Token-Paare aus
web/src/control/theme.css:

  HART (Exit 1) — Tokens, die per Doktrin Text tragen:
      ink / ink-2 / ink-3  (Texthierarchie, DESIGN.md "Text hierarchy")
      data-1..7            (DESIGN.md "Daten-Palette" behauptet AA — hier belegt)
    gegen surface-0..3, Grenze 4,5:1. KEINE Baseline, kein Hochzaehlen:
    der Boden ist absolut. Wer ein Token aendert, muss den Wert halten.

  HINWEIS (Exit 0) — Semantik-Tokens, die gemischt als Signal UND als Text
    auftreten (Status-Trio, Bronze, brand, stamp). Sie zu verschaerfen waere
    eine Doktrin-Entscheidung, keine Fehlerkorrektur, und gehoert dem Operator.
    Sie werden gerechnet und benannt, blockieren aber nicht.

  GUARD (Exit 1) — jedes --color-Token MUSS unten klassifiziert sein. Ein neues
    Token ohne Eintrag laesst das Skript fehlschlagen, damit der Check nicht
    still umgangen wird, indem man einfach ein Token dazulegt.

ROLLEN-EBENE (2026-07-28): theme.css hat einen `@theme inline`-Block mit
semantischen Aliasen (`--color-action: var(--color-bronze)`). Der Parser loest
Aliase auf — sonst waere die neue Ebene fuer diesen Check unsichtbar gewesen und
haette das Gate lautlos umgangen. Ein reiner Alias wird nicht doppelt gerechnet
(sein Ziel steht bereits in der Liste), muss aber klassifiziert sein; setzt
jemand statt der Referenz einen eigenen Wert ein, wird er sofort mitgeprueft.

Aufruf: scripts/check-contrast.py  (kein Argument; Pfade sind repo-relativ)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

THEME = Path(__file__).resolve().parent.parent / "web" / "src" / "control" / "theme.css"

TOKEN_RE = re.compile(r"--color-([a-z0-9-]+)\s*:\s*([^;]+);")
VAR_RE = re.compile(r"^var\(\s*--color-([a-z0-9-]+)\s*\)$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Flaechen, gegen die Text geprueft wird. surface-3 ist laut DESIGN.md reine
# Hover-/Auswahl-Fuellung (nie ruhender Grund) — wird trotzdem hart geprueft,
# weil Text auf einer gehoverten Zeile lesbar bleiben muss.
SURFACES = ("surface-0", "surface-1", "surface-2", "surface-3")

# Tokens, die per Doktrin Text tragen -> harte AA-Grenze.
TEXT_TOKENS = ("ink", "ink-2", "ink-3")
DATA_TOKENS = tuple(f"data-{i}" for i in range(1, 8))

# Gemischt Signal/Text -> nur Hinweis (s. Modul-Docstring).
SEMANTIC_TOKENS = (
    "live",
    "bronze",
    "bronze-hi",
    "brand",
    "status-ok",
    "status-warn",
    "status-alert",
    "lane-prem",
    "stamp",
    "stamp-2",
)

# Rollen aus dem `@theme inline`-Block, die im VORDERGRUND stehen. Heute alles
# Aliase; ihr Ziel wird bereits geprueft. Ersetzt jemand die Referenz durch einen
# eigenen Wert, faellt das Token automatisch in die Hinweis-Pruefung.
ROLE_FOREGROUND_TOKENS = ("action", "action-hi", "focus", "stamped")

# Rollen, die eine FLAECHE benennen — nie Textfarbe, also nie Vordergrund-Pruefung.
ROLE_SURFACE_TOKENS = ("selected",)

# Weder Text noch Flaeche (Haarlinien) -> nicht kontrastpflichtig.
NON_TEXT_TOKENS = ("line", "line-soft")

AA = 4.5


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def parse_raw(path: Path) -> dict[str, str]:
    css = path.read_text(encoding="utf-8")
    return {name: value.strip() for name, value in TOKEN_RE.findall(css)}


def resolve(raw: dict[str, str]) -> tuple[dict[str, str], set[str], list[str]]:
    """Loest `var(--color-x)`-Aliase zu Hexwerten auf.

    Rueckgabe: (aufgeloeste Werte, Menge der Alias-Namen, Fehler).
    """
    resolved: dict[str, str] = {}
    aliases: set[str] = set()
    errors: list[str] = []
    for name, value in raw.items():
        if VAR_RE.match(value):
            aliases.add(name)
        seen: list[str] = []
        current = name
        while True:
            if current in seen:
                errors.append(
                    f"--color-{name}: zirkulaerer Alias ueber {' -> '.join(seen)}"
                )
                break
            seen.append(current)
            val = raw.get(current)
            if val is None:
                errors.append(
                    f"--color-{name}: Alias zeigt auf unbekanntes --color-{current}"
                )
                break
            ref = VAR_RE.match(val)
            if ref:
                current = ref.group(1)
                continue
            if HEX_RE.match(val):
                resolved[name] = val
            else:
                errors.append(
                    f"--color-{name}: Wert '{val}' ist weder #RRGGBB noch var(--color-*) — "
                    "der Kontrast-Check kann ihn nicht rechnen"
                )
            break
    return resolved, aliases, errors


def main() -> int:
    if not THEME.is_file():
        print(f"FAIL: token sheet not found at {THEME}", file=sys.stderr)
        return 1

    raw = parse_raw(THEME)
    if not raw:
        print(f"FAIL: no --color-* tokens parsed from {THEME}", file=sys.stderr)
        return 1

    tokens, aliases, errors = resolve(raw)
    if errors:
        print(f"FAIL: {len(errors)} Token(s) nicht aufloesbar.", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1

    classified = set(
        SURFACES
        + TEXT_TOKENS
        + DATA_TOKENS
        + SEMANTIC_TOKENS
        + ROLE_FOREGROUND_TOKENS
        + ROLE_SURFACE_TOKENS
        + NON_TEXT_TOKENS
    )
    unclassified = sorted(set(raw) - classified)
    if unclassified:
        print(
            "FAIL: unclassified --color-* token(s): "
            + ", ".join(unclassified)
            + "\n  Add each to TEXT_TOKENS / DATA_TOKENS / SEMANTIC_TOKENS /"
            " ROLE_FOREGROUND_TOKENS / ROLE_SURFACE_TOKENS / NON_TEXT_TOKENS /"
            " SURFACES in scripts/check-contrast.py."
            "\n  A token nobody classified is a token nobody checked.",
            file=sys.stderr,
        )
        return 1

    missing_surfaces = [s for s in SURFACES if s not in tokens]
    if missing_surfaces:
        print(
            f"FAIL: surface token(s) missing from theme.css: {', '.join(missing_surfaces)}",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    hints: list[str] = []

    for group, names in (("text", TEXT_TOKENS), ("data", DATA_TOKENS)):
        for name in names:
            if name not in tokens:
                failures.append(
                    f"{group} token --color-{name} is missing from theme.css"
                )
                continue
            for surface in SURFACES:
                ratio = contrast(tokens[name], tokens[surface])
                if ratio < AA:
                    failures.append(
                        f"--color-{name} ({tokens[name]}) on --color-{surface} "
                        f"({tokens[surface]}) = {ratio:.2f}:1, below AA {AA}:1"
                    )

    # Reine Aliase ueberspringen: ihr Ziel steht bereits in der Liste, doppelte
    # Meldung waere Rauschen. Ein Rollen-Token mit EIGENEM Wert wird geprueft.
    hint_names = [
        n for n in SEMANTIC_TOKENS + ROLE_FOREGROUND_TOKENS if n not in aliases
    ]
    for name in hint_names:
        if name not in tokens:
            continue
        weak = [
            (s, contrast(tokens[name], tokens[s]))
            for s in SURFACES
            if contrast(tokens[name], tokens[s]) < AA
        ]
        if weak:
            detail = ", ".join(f"{s} {r:.2f}:1" for s, r in weak)
            hints.append(f"--color-{name} ({tokens[name]}): {detail}")

    if hints:
        print("hint: semantic tokens below AA where they are used as TEXT —")
        print("      signal-only use (LED, chip fill, border) is unaffected:")
        for line in hints:
            print(f"      {line}")

    if failures:
        print(
            f"FAIL: {len(failures)} token pair(s) below WCAG AA ({AA}:1).",
            file=sys.stderr,
        )
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nText and data tokens carry copy — the AA floor is absolute and has no"
            "\nbaseline to ratchet. Fix the token value in web/src/control/theme.css"
            "\nand update the proof table at its definition (see DESIGN.md, Tokens).",
            file=sys.stderr,
        )
        return 1

    checked = (len(TEXT_TOKENS) + len(DATA_TOKENS)) * len(SURFACES)
    print(
        f"contrast OK: {checked} text/data token pairs at or above AA ({AA}:1)"
        f" · {len(aliases)} role alias(es) resolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Vertrag zwischen ``scripts/visual_verify_runner.mjs`` und dem Loop-Runner.

Warum es diese Datei gibt
-------------------------
Der Runner liest die Verifier-Evidenz in ``LoopRunner._validate_visual_evidence_dir``
und verlangt dort je Ergebnis ein ``viewport``-**Dict** mit ganzzahliger ``width``.
Das erzeugende Objektliteral in ``checkOne`` band ``viewport`` ab 2026-07-22
(Commit ``1335f48e51``) ZWEIMAL: einmal als Shorthand auf das Viewport-Objekt und
weiter unten noch einmal auf ``viewport.name``. JavaScript meldet doppelte
Schluessel in einem Objektliteral nicht — der letzte gewinnt. Das Feld war damit
still ein String, die Visual-Attestation schlug jede Nacht fehl, und der einzige
Pack mit Autoland-Erlaubnis landete zwei Wochen lang nichts.

Ein Test gegen eine handgeschriebene Fixture haette das nicht gefunden: die
Fixture bildete das erwartete Schema ab, nicht das erzeugte. Deshalb prueft
dieser Test die erzeugende Quelle selbst — und zwar generisch auf die
Fehlerklasse, nicht nur auf das eine Feld.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "visual_verify_runner.mjs"


def _result_literal() -> str:
    """Das ``return { ... }`` am Ende von ``checkOne`` als Text."""
    source = RUNNER.read_text(encoding="utf-8")
    marker = "async function checkOne("
    start = source.index(marker)
    # Das Ergebnisliteral ist das letzte `return {` innerhalb von checkOne.
    body_end = source.index("\nasync function ", start + len(marker))
    body = source[start:body_end]
    open_at = body.rindex("return {") + len("return ")

    depth = 0
    for offset, char in enumerate(body[open_at:], start=open_at):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[open_at : offset + 1]
    raise AssertionError("Objektliteral in checkOne nicht geschlossen")


def _top_level_keys(literal: str) -> list[str]:
    """Schluessel der obersten Ebene — verschachtelte Objekte bleiben aussen vor."""
    keys: list[str] = []
    depth = 0
    for line in literal.splitlines():
        stripped = line.strip()
        if depth == 1:
            match = re.match(r"^([A-Za-z_$][\w$]*)\s*[:,]", stripped)
            if match:
                keys.append(match.group(1))
        depth += line.count("{") - line.count("}")
    return keys


def test_result_literal_has_no_duplicate_keys() -> None:
    """Doppelte Schluessel sind in JS legal und deshalb hier verboten."""
    keys = _top_level_keys(_result_literal())
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, (
        f"doppelte Schluessel im Ergebnisliteral von checkOne: {duplicates} — "
        "JavaScript nimmt still den letzten. Genau so wurde 'viewport' 2026-07-22 "
        "zum String und die Visual-Attestation zwei Wochen lang unbrauchbar."
    )
    assert keys, "kein Schluessel gefunden — der Parser greift nicht"


def test_viewport_stays_bound_to_the_object() -> None:
    """``viewport`` muss das Objekt sein: der Runner indiziert ``viewport['width']``."""
    literal = _result_literal()
    assert re.search(r"^\s*viewport,\s*$", literal, re.MULTILINE), (
        "Shorthand 'viewport,' fehlt — LoopRunner._validate_visual_evidence_dir "
        "verlangt ein Dict mit 'width' und weist alles andere ab."
    )
    assert not re.search(r"^\s*viewport\s*:", literal, re.MULTILINE), (
        "'viewport' wird erneut zugewiesen und ueberschreibt das Objekt."
    )


@pytest.mark.parametrize("field", ["viewportName", "viewportSize"])
def test_viewport_details_keep_their_own_names(field: str) -> None:
    """Name und Groesse bleiben erhalten — sie duerfen nur nicht ``viewport`` heissen."""
    assert re.search(rf"^\s*{field}\s*:", _result_literal(), re.MULTILINE), (
        f"{field} fehlt im Ergebnisliteral"
    )

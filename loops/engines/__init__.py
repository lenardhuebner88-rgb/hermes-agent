"""Engine-Adapter für den Loop-Runner.

Contract: eine Engine ist ein Callable
    run(model: str, prompt: str, cwd: Path, timeout_s: int,
        effort: str | None = None) -> EngineResult

Engines kapseln je ein Abo-CLI (claude, kimi, codex, …) im Headless-Modus.
Neue Engine = neues Modul mit @register("name", effort_levels=(…)) — nichts
sonst anfassen. ``effort_levels`` deklariert, welche Reasoning-Stufen das CLI
wirklich annimmt; ``validate_effort`` erzwingt das fail-closed, und derselbe
Katalog speist das Dashboard-Control. So können Angebot und Transport nicht
auseinanderlaufen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# 2026-07-02 live gelernt: neben "usage limit"/429 meldet die Claude-CLI auch
# "You've hit your session limit · resets 9:50pm" — ohne diesen Wortlaut lief
# der Planner mit leerem Ergebnis weiter statt zu stoppen.
# "429" nur mit HTTP-/Fehler-Kontext: der Codex-CLI-Footer "tokens used 140,429"
# matchte \b429\b (Komma = Wortgrenze) → Phantom-Stop trotz Tail-Scoping
# (live 2026-07-05, Verifier-FAIL wurde als usage-limit fehlklassifiziert).
USAGE_LIMIT_RE = re.compile(
    r"usage limit|session limit|hit your .{0,12}limit|reached your usage"
    r"|rate.?limit exceeded|(?:http|error|status|code|quota)\D{0,12}429\b",
    re.IGNORECASE,
)


@dataclass
class EngineResult:
    rc: int
    output: str
    usage_limit: bool
    timed_out: bool = False
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    provenance_path: str | None = None


# 2026-07-05 live gelernt: bei einem 69k-Zeilen-Codex-Build-Output tauchten
# im MITTELTEIL sowohl der Agent-eigene Test-String
# `("quota 429 from provider", guarded)` als auch grep-artige Zeilenreferenzen
# wie `tests/hermes_cli/test_kanban_cli.py:429:` auf — der Loop stoppte auf
# einer Phantom-Usage-Limit-Meldung. Echte CLI-Limit-Texte (Claude "You've hit
# your session limit · resets 9:50pm", Codex-Quota-Text) erscheinen am ENDE
# des Prozess-Outputs → Detection auf den Tail begrenzen statt den gesamten
# Output zu durchsuchen.
USAGE_LIMIT_TAIL_CHARS = 4000


def detect_usage_limit(text: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(text[-USAGE_LIMIT_TAIL_CHARS:]))


ENGINES: dict[str, Callable] = {}

# Reasoning-Effort-Stufen, die eine Engine WIRKLICH auf die Leitung bringt —
# je Engine am CLI selbst belegt (2026-07-28), nicht aus dem Gedächtnis:
#   claude  `--effort <l>`                    → hermes_cli.kanban_worker_runtime
#                                               .CLAUDE_CLI_EFFORT_LEVELS (SSOT)
#   codex   `-c model_reasoning_effort=<l>`   → codex-rs/protocol/src/
#                                               openai_models.rs:40-64 (ReasoningEffort)
#   xai     `--reasoning-effort <l>`          → grok CLI selbst: "use one of:
#                                               high, medium, low"
# Leeres Tupel = die Engine transportiert KEINEN Effort. Dann gibt es im
# Dashboard kein Control (dieselbe Konvention wie `reasoning_support: []` im
# Lanes-Tab) und ein gesetzter Effort ist ein FEHLER, kein stiller Drop.
ENGINE_EFFORT_LEVELS: dict[str, tuple[str, ...]] = {}


class EffortUnsupported(ValueError):
    """Angeforderter Effort passt nicht zur Engine — Meldung nennt beide."""


def register(name: str, *, effort_levels: tuple[str, ...] = ()):
    def deco(fn: Callable) -> Callable:
        ENGINES[name] = fn
        ENGINE_EFFORT_LEVELS[name] = tuple(effort_levels)
        return fn

    return deco


def get_engine(name: str) -> Callable:
    try:
        return ENGINES[name]
    except KeyError:
        raise KeyError(
            f"Unbekannte Engine {name!r} — registriert: {sorted(ENGINES)}"
        ) from None


def effort_levels_for(name: str) -> tuple[str, ...]:
    """Transportierbare Effort-Stufen einer Engine ( () = kein Control )."""
    return ENGINE_EFFORT_LEVELS.get(name, ())


def validate_effort(engine: str, effort: str | None) -> str | None:
    """Normalisiert einen angeforderten Effort — fail-closed.

    None/leer = Engine-Default (kein Flag). Ein Effort auf einer Engine ohne
    Transport, oder eine Stufe außerhalb ihres Sets, ist ein harter Fehler:
    ein still verschluckter Effort wäre exakt der Fehlermodus, den diese
    Kette beseitigen soll (Anzeige sagt medium, Prozess läuft xhigh).
    """
    if effort is None:
        return None
    level = str(effort).strip().lower()
    if not level:
        return None
    levels = effort_levels_for(engine)
    if not levels:
        raise EffortUnsupported(
            f"Engine {engine!r} transportiert keinen Reasoning-Effort "
            f"(angefordert: {level!r})"
        )
    if level not in levels:
        raise EffortUnsupported(
            f"Engine {engine!r}: Effort {level!r} unbekannt — erlaubt: "
            f"{', '.join(levels)}"
        )
    return level


# Selbst-Registrierung der mitgelieferten Engines (am Modulende, damit
# register/EngineResult beim Import der Untermodule bereits existieren).
from . import claude_cli  # noqa: E402,F401
from . import kimi_cli  # noqa: E402,F401
from . import codex_cli  # noqa: E402,F401
from . import hermes_profile  # noqa: E402,F401
from . import neuralwatt_cli  # noqa: E402,F401
from . import xai_cli  # noqa: E402,F401
from . import alibaba_token_plan_cli  # noqa: E402,F401

"""Engine-Adapter für den Loop-Runner.

Contract: eine Engine ist ein Callable
    run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult

Engines kapseln je ein Abo-CLI (claude, kimi, codex, …) im Headless-Modus.
Neue Engine = neues Modul mit @register("name") — nichts sonst anfassen.
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


def register(name: str):
    def deco(fn: Callable) -> Callable:
        ENGINES[name] = fn
        return fn

    return deco


def get_engine(name: str) -> Callable:
    try:
        return ENGINES[name]
    except KeyError:
        raise KeyError(
            f"Unbekannte Engine {name!r} — registriert: {sorted(ENGINES)}"
        ) from None


# Selbst-Registrierung der mitgelieferten Engines (am Modulende, damit
# register/EngineResult beim Import der Untermodule bereits existieren).
from . import claude_cli  # noqa: E402,F401
from . import kimi_cli  # noqa: E402,F401
from . import codex_cli  # noqa: E402,F401
from . import hermes_profile  # noqa: E402,F401
from . import neuralwatt_cli  # noqa: E402,F401
from . import xai_cli  # noqa: E402,F401

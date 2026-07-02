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
USAGE_LIMIT_RE = re.compile(
    r"usage limit|session limit|hit your .{0,12}limit|reached your usage"
    r"|rate.?limit exceeded|\b429\b",
    re.IGNORECASE,
)


@dataclass
class EngineResult:
    rc: int
    output: str
    usage_limit: bool
    timed_out: bool = False


def detect_usage_limit(text: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(text))


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

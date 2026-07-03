"""Engine: Hermes-CLI One-Shot mit direkter NeuralWatt-Modell-/Provider-Bindung.

Anders als `hermes_profile` (wo "model" ein Hermes-PROFIL ist) trägt hier der
"model"-Parameter direkt einen NeuralWatt-Modell-Slug; Provider wird explizit
per Flag gepinnt statt über ein Profil aufgelöst.

Belegkette (2026-07-03):
- `-m/--model` und `--provider` sind top-level Flags, die explizit mit `-z`
  paaren (hermes_cli/_parser.py:114-137, Kommentar: "so they can pair with -z
  without needing the `chat` subcommand"). Live seit Tagen auf diesem Host
  genutzt: `hermes -m kimi-k2.7-code --provider neuralwatt -z "<prompt>"`.
- kanban.db ist bewusst NICHT profil-isoliert (kanban_db.py:413-433) →
  HERMES_SANDBOX_MODE=1 (kanban_db.py:558-573) lenkt Kanban-Zugriffe des Laufs
  in eine Sandbox-DB um, damit ein Loop-Builder nie versehentlich aufs Live-
  Board schreibt (identische Begründung wie in `hermes_profile`).
- Kein --cwd/--timeout-Flag in der CLI → cwd/timeout macht dieser Wrapper
  (subprocess), analog zu den übrigen Engines.
- Usage-Limit: generische Erkennung via `detect_usage_limit`
  (engines.USAGE_LIMIT_RE, `\\b429\\b`). NeuralWatt-429 wird laut
  `hermes_profile`-Docstring intern über die Fallback-Chain retried, bevor es
  den Wrapper erreicht — ein durchschlagendes 429 matcht trotzdem die Regex.
- Modell-Liste (`loops/models.yaml`): live geprüft 2026-07-03 via
  GET https://api.neuralwatt.com/v1/models.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

HERMES_BIN = os.environ.get(
    "HERMES_BIN", "/home/piet/.hermes/hermes-agent/venv/bin/hermes"
)


@register("neuralwatt")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    cmd = [HERMES_BIN, "-m", model, "--provider", "neuralwatt", "-z", prompt]
    env = dict(os.environ)
    env["HERMES_SANDBOX_MODE"] = "1"  # Kanban-Writes des Laufs nie aufs Live-Board
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout) + _decode(exc.stderr)
        return EngineResult(
            rc=124, output=out, usage_limit=detect_usage_limit(out), timed_out=True
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    return EngineResult(
        rc=proc.returncode, output=out, usage_limit=detect_usage_limit(out)
    )


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw

"""Engine: Hermes-CLI One-Shot mit Profil-Pinning (NeuralWatt + Codex-Pool via Abo).

Der "model"-Parameter dieser Engine ist ein **Hermes-PROFIL** (nicht ein Modell-Slug):
das Profil trägt die Modell-/Provider-Bindung. Live verifiziert (2026-07-02):
`~/.hermes/profiles/reviewer/config.yaml` → glm-5.2 @ neuralwatt ·
`~/.hermes/profiles/coder/config.yaml` → openai-codex-Pool.

Belegkette (Audit 2026-07-02, Datei:Zeile im Repo):
- One-Shot: `hermes -z "<prompt>"` — hermes_cli/_parser.py:101-113; Dispatch
  main.py:13647-13658; Kern oneshot.py:137/253. stdout = NUR die finale Antwort;
  Exit 0 = Erfolg, 1 = Agent-Fehler (stderr "hermes -z: agent failed: …"), 2 = Usage.
- Profil-Wahl: `-p <name>` setzt HERMES_HOME (main.py:341-531), kompatibel mit -z.
- Tool-Autonomie: run_oneshot setzt HERMES_YOLO_MODE=1 + HERMES_ACCEPT_HOOKS=1 VOR
  dem Agent-Import (oneshot.py:171f) — gilt nur für FRISCHE Subprozesse (approval.py:33
  friert YOLO beim Modul-Import ein). Dieser Adapter startet immer einen frischen
  Subprozess, nie Bibliotheks-Import.
- kanban.db ist bewusst NICHT profil-isoliert (kanban_db.py:413-433) →
  HERMES_SANDBOX_MODE=1 (kanban_db.py:558-573) lenkt Kanban-Zugriffe des Laufs in
  eine Sandbox-DB um, damit ein Loop-Builder nie versehentlich aufs Live-Board schreibt.
- Kein --cwd/--timeout-Flag in der CLI → cwd/timeout macht dieser Wrapper (subprocess).
- Quota-Wortlaut Codex-Pool: "Codex provider quota exhausted (429)…" (auth.py:3735)
  → von engines.USAGE_LIMIT_RE (\\b429\\b) erfasst; NeuralWatt-429 wird intern über
  die Fallback-Chain retried, bevor es den Wrapper erreicht.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

HERMES_BIN = os.environ.get(
    "HERMES_BIN", "/home/piet/.hermes/hermes-agent/venv/bin/hermes"
)


@register("hermes")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    # model = Hermes-Profilname (siehe Modul-Docstring).
    cmd = [HERMES_BIN, "-p", model, "-z", prompt]
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

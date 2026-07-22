"""Engine: Hermes-CLI One-Shot mit direkter Alibaba-Token-Plan-Bindung.

Schmal analog zu ``neuralwatt_cli``: explizit
``hermes -m <model> --provider alibaba-token-plan -z "<prompt>"`` im
Sandbox-Env. Keine Auth-/Credential-Konfiguration hier — Abo-Route muss
bereits greifen.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

HERMES_BIN = os.environ.get(
    "HERMES_BIN", "/home/piet/.hermes/hermes-agent/venv/bin/hermes"
)
PROVIDER = "alibaba-token-plan"


@register("alibaba-token-plan")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    cmd = [HERMES_BIN, "-m", model, "--provider", PROVIDER, "-z", prompt]
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

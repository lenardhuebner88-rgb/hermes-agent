"""Engine: Claude Code CLI headless (Abo, kein API-Key).

Bewiesenes Muster (fable-loop/strategist-cron):
    ~/.local/bin/claude -p --model <id> --permission-mode bypassPermissions "<prompt>"
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path("~/.local/bin/claude").expanduser())
)


@register("claude")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--model",
        model,
        "--permission-mode",
        "bypassPermissions",
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
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

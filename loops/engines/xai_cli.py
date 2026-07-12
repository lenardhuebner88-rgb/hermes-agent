"""Engine: official Grok Build CLI bound to the SuperGrok subscription.

The subscription CLI exposes the product slot ``grok-build`` rather than the
API model id ``grok-4.5``.  xAI documents Grok Build as powered by Grok 4.5, so
the adapter keeps the operator-facing model id and makes that alias explicit at
the transport boundary.  This path does not use an API key or OpenRouter.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

GROK_BIN = os.environ.get("GROK_BIN", "/home/piet/.npm-global/bin/grok")

_CLI_MODEL_ALIASES = {"grok-4.5": "grok-build"}


@register("xai")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    cli_model = _CLI_MODEL_ALIASES.get(model, model)
    cmd = [
        GROK_BIN,
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--always-approve",
        "--model",
        cli_model,
        "--single",
        prompt,
        "--output-format",
        "plain",
    ]
    env = dict(os.environ)
    env["HERMES_SANDBOX_MODE"] = "1"  # a loop builder must never write to the live board
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout) + _decode(exc.stderr)
        return EngineResult(rc=124, output=out, usage_limit=detect_usage_limit(out), timed_out=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return EngineResult(rc=proc.returncode, output=out, usage_limit=detect_usage_limit(out))


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw

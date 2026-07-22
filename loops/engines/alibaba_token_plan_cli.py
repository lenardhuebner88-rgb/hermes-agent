"""Engine: Qwen Code CLI headless via the authenticated Alibaba Token Plan.

The loop catalog keeps the product-facing engine id ``alibaba-token-plan``,
but execution intentionally uses the installed ``qwen`` subscription CLI rather
than a Hermes API-key provider. ``--safe-mode`` disables user context, hooks,
extensions, skills, and MCP servers; ``--sandbox`` keeps tool execution isolated.
Authentication and credential configuration are left untouched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

QWEN_BIN = os.environ.get("QWEN_BIN", "qwen")


@register("alibaba-token-plan")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    cmd = [
        QWEN_BIN,
        "-m",
        model,
        "--safe-mode",
        "--sandbox",
        "--output-format",
        "text",
        "-p",
        prompt,
    ]
    env = dict(os.environ)
    env["HERMES_SANDBOX_MODE"] = "1"
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

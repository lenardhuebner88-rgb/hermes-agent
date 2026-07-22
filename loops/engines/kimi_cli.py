"""Engine: Kimi-Code CLI headless (Coding-Abo, managed OAuth, $0/Token).

Bewiesenes Muster (kimi-loop/start.sh, live seit 2026-06-27):
    ~/.kimi-code/bin/kimi --model kimi-code/kimi-for-coding -p "<prompt>"

Belegzeilen `~/.kimi-code/bin/kimi --help` (2026-07-02 live geprüft):
    -m, --model <model>           LLM model alias to use for this invocation. Defaults to
                                  default_model in config.toml.
    -p, --prompt <prompt>         Run one prompt non-interactively and print the response.
`-p` führt Tools autonom ohne Approval aus. Default-Modell laut
`~/.kimi-code/config.toml`: `default_model = "kimi-code/kimi-for-coding"` (Coding-Abo).

NIEMALS zusätzlich `--yolo`/`--auto` — live bestätigt (2026-07-02):
    $ ~/.kimi-code/bin/kimi -p "test" --yolo
    error: Cannot combine --prompt with --yolo.
    $ ~/.kimi-code/bin/kimi -p "test" --auto
    error: Cannot combine --prompt with --auto.

Usage-Limit: `engines.USAGE_LIMIT_RE` deckt generisches 429/"usage limit" ab. Zusätzlich
meldet die Kimi-CLI eigene Wortlaute (`strings ~/.kimi-code/bin/kimi`, 2026-07-02 live
gegrept — kompiliertes JS-Bundle, keine Doku dafür vorhanden):
    "provider.rate_limit",
    "provider.rate_limit": {
    /provider\\.rate_limit/,
    PROVIDER_RATE_LIMIT: "provider.rate_limit",
    if (code === "rate_limit_exceeded") return new APIProviderRateLimitError(fullMessage);
Deshalb zusätzliche Zusatz-Regex hier statt Änderung an der geteilten
`engines.USAGE_LIMIT_RE`.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

KIMI_BIN = os.environ.get(
    "KIMI_BIN", str(Path("~/.kimi-code/bin/kimi").expanduser())
)
KIMI_DEFAULT_MODEL = "kimi-code/kimi-for-coding"
KIMI_MODEL_ALIASES = {
    # Looptap exposes the short catalog id, while Kimi Code's managed OAuth
    # config requires the fully-qualified alias reported by Kimi Code.
    "k3": "kimi-code/k3",
}

KIMI_LIMIT_RE = re.compile(
    r"provider\.rate_limit|PROVIDER_RATE_LIMIT|rate_limit_exceeded", re.IGNORECASE
)


@register("kimi")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    effective_model = KIMI_MODEL_ALIASES.get(model, model)
    cmd = [
        KIMI_BIN,
        "--model",
        effective_model,
        "-p",
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
            rc=124, output=out, usage_limit=_usage_limit(out), timed_out=True
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    return EngineResult(rc=proc.returncode, output=out, usage_limit=_usage_limit(out))


def _usage_limit(text: str) -> bool:
    return detect_usage_limit(text) or bool(KIMI_LIMIT_RE.search(text))


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw

"""Engine: Qwen Code CLI headless via the authenticated Alibaba Token Plan.

The loop catalog keeps the product-facing engine id ``alibaba-token-plan``,
but execution intentionally uses the installed ``qwen`` subscription CLI rather
than a Hermes API-key provider. ``--safe-mode`` disables user context, hooks,
extensions, skills, and MCP servers; ``--sandbox`` keeps tool execution isolated.
Authentication and credential configuration are left untouched.

Zwei Flags sind Pflicht, damit die Bau-Phase ueberhaupt arbeiten kann — beide
gelernt am Nachtlauf 2026-07-29 (dashboard-experience), der 270s lief und 0
Zeilen schrieb:

* ``-y`` (YOLO): ohne das verweigert die CLI im nicht-interaktiven Modus JEDES
  Tool — auch ``read_file`` — mit "requires user approval but cannot execute in
  non-interactive mode". Die anderen Bau-Engines setzen das Aequivalent
  (``codex --sandbox workspace-write``, ``claude --permission-mode``).
* ``SANDBOX_MOUNTS``: ``--sandbox`` startet einen Docker-Container, der per
  Default nur cwd mountet. Der Builder-Vertrag lebt aber daneben — der Runner
  haelt ``wt = state/"wt"`` und ``status_path = state/"last-status"``. Ohne den
  State-Mount kann der Builder weder Plan noch before_evidence lesen noch
  ``last-status`` schreiben; der Runner meldet dann fail-closed "build-fail: ?".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

QWEN_BIN = os.environ.get("QWEN_BIN", "qwen")


@register("alibaba-token-plan")
def run(
    model: str,
    prompt: str,
    cwd: Path,
    timeout_s: int,
    effort: str | None = None,  # noqa: ARG001 - kein Effort-Transport (s. register)
) -> EngineResult:
    cmd = [
        QWEN_BIN,
        "-m",
        model,
        "--safe-mode",
        "--sandbox",
        "-y",
        "--output-format",
        "text",
        "-p",
        prompt,
    ]
    env = dict(os.environ)
    env["HERMES_SANDBOX_MODE"] = "1"
    _mount_loop_state(env, cwd)
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


def _mount_loop_state(env: dict[str, str], cwd: Path) -> None:
    """Mount den Loop-State-Ordner rw in die Docker-Sandbox.

    Gekoppelt an den Runner-Vertrag ``self.wt = self.state / "wt"``: nur wenn cwd
    wirklich so heisst, ist der Parent der State-Ordner. Sonst fail-soft nichts
    mounten, statt ein fremdes Verzeichnis in den Container zu reichen.
    Der Pfad wird identisch gemappt, weil der Prompt absolute Pfade transportiert.
    """
    if cwd.name != "wt":
        return
    state = cwd.parent
    if not state.is_dir():
        return
    spec = f"{state}:{state}:rw"
    existing = env.get("SANDBOX_MOUNTS", "").strip()
    if spec in existing:
        return
    env["SANDBOX_MOUNTS"] = f"{existing},{spec}" if existing else spec


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw

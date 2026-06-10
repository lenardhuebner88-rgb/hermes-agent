"""Escalation: hand a message to the full Hermes agent and capture its answer.

Uses ``hermes -z/--oneshot`` which "send[s] a single prompt and print[s] ONLY
the final response text to stdout" (hermes_cli/_parser.py) — runs standalone,
no gateway required. This is the expensive path; the pre-filter only reaches
here for messages it classified as ``escalate``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridges.discord_prefilter.config import PrefilterConfig

logger = logging.getLogger("discord_prefilter.escalate")

# Repo root (…/bridges/discord_prefilter/escalate.py → repo root), so that the
# default ``python -m hermes_cli.main`` invocation can import hermes_cli.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def run_hermes_oneshot(message: str, config: "PrefilterConfig") -> str:
    """Run one full Hermes turn for ``message`` and return the final text.

    Raises RuntimeError on failure so the caller can surface a clear error to
    the channel instead of silently swallowing the escalation.
    """
    cmd = [*config.hermes_argv]
    if config.escalate_profile:
        cmd += ["-p", config.escalate_profile]
    cmd += ["-z", message]

    try:
        proc = subprocess.run(  # noqa: S603 -- argv assembled from config
            cmd,
            capture_output=True,
            text=True,
            timeout=config.escalate_timeout_s,
            cwd=str(_REPO_ROOT),
            env=config.hermes_env(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"hermes executable not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"hermes escalation timed out after {config.escalate_timeout_s}s"
        ) from exc

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"hermes exited {proc.returncode}: {tail}")

    answer = (proc.stdout or "").strip()
    if not answer:
        raise RuntimeError("hermes returned an empty response")
    return answer

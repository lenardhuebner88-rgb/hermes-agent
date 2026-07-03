"""Gateway lifecycle guard for cron job creation (#30719).

An agent running inside a gateway can schedule a cron job that calls
``hermes gateway restart`` (or ``launchctl kickstart ai.hermes.gateway``
or ``systemctl restart hermes-gateway``).  When the cron fires, the
gateway dies, the supervisor (launchd KeepAlive / systemd Restart=)
revives it, auto-resume picks up the offending session, and the resumed
turn re-runs the same logic — a SIGTERM-respawn loop every ~10 seconds
until manually broken.

This module rejects cron job specs whose prompt or script contains a
direct shell-level gateway-lifecycle command.  It is enforced at
``cron.jobs.create_job`` so it fires on every job-creation path: the
``hermes cron create`` CLI subcommand AND the agent's ``cronjob`` model
tool (which calls ``create_job`` directly, bypassing the CLI layer).

The pattern is intentionally command-shaped: it anchors on a concrete
command identifier (``hermes gateway``, ``launchctl ... hermes-gateway``,
``systemctl ... hermes-gateway``, ``pkill`` against the gateway) so it
cannot fire on prose.  A cron ``prompt`` is fed to a future LLM, not a
shell, so an over-broad substring match on English ("Kong API gateway
autoscaling and restart behavior") would produce a high false-positive
rate without preventing the actual foot-gun, which requires a real
command shape.

This is a defence-in-depth layer.  ``tools/terminal_tool.py`` already
blocks these commands at *execution* time when ``_HERMES_GATEWAY=1``, and
``hermes gateway stop|restart`` refuse to self-target from inside the
gateway.  Blocking at *creation* time as well means the agent gets an
immediate, informative rejection instead of scheduling a job that will
only fail (silently) when it fires.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
#
# SYNC NOTE: this is the guard for the agent's ``cronjob`` tool path
# (``cron.jobs.create_job``, called directly — no CLI involved). The CLI
# subcommand path (``hermes cron create``/``edit``) is guarded separately by
# ``_GATEWAY_LIFECYCLE_PATTERNS`` in ``hermes_cli/cron.py``, which is the
# more-hardened, canonical source for these command shapes (it is reviewed
# and extended first). When a new gateway-lifecycle command shape is added to
# either guard, mirror it in the other or the two enforcement points diverge
# and one path lets a foot-gun through that the other blocks.
_HERMES_GATEWAY_ACTION = (
    r"\bgateway\b"
    r"(?:\s+(?:-\w|--[\w-]+)(?:[= ]\S+)?)*"
    r"\s+(?:restart|stop)\b"
)
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: `hermes gateway restart|stop` — the canonical foot-gun.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
    # Branch B: `python -m hermes_cli[.main]` invoking a gateway
    # restart/stop — the module-invocation equivalent of Branch A, for cron
    # scripts/prompts that shell to a venv interpreter directly instead of
    # the `hermes` console-script entrypoint. Same restart/stop-only scope
    # as Branch A via `_HERMES_GATEWAY_ACTION`.
    r"|(?:python(?:3(?:\.\d+)?)?\s+-m\s+hermes_cli(?:\.main)?\b(?=[^\n]*" + _HERMES_GATEWAY_ACTION + r"))"
    # Branch C: `python .../hermes_cli/main.py` — the direct-script-path
    # equivalent of Branch B (no `-m`, absolute/relative path to main.py).
    r"|(?:python(?:3(?:\.\d+)?)?\s+\S*hermes_cli/main\.py\b(?=[^\n]*" + _HERMES_GATEWAY_ACTION + r"))"
    # Branch D: launchctl ops on a hermes-gateway label. macOS launchd
    # labels look like `ai.hermes.gateway` / `hermes-gateway`. Requiring the
    # gateway identifier prevents blocking unrelated hermes services (e.g.
    # `launchctl unload ai.hermes.update-checker.plist`).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch E: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch F: pkill/pgrep targeting any hermes process. Broader than the
    # gateway-scoped branches above deliberately (mirrors the CLI guard): a
    # `pkill -f hermes` kills the whole process tree including the gateway,
    # with no gateway-specific token left to anchor on.
    r"|(?:(?:p?kill|pgrep)\s+.*hermes)"
)


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern."""
    if not text:
        return False
    return bool(_GATEWAY_LIFECYCLE_PATTERN.search(text))


def _resolve_script_path(script_path: str) -> Path:
    """Resolve a cron ``script`` value the same way the scheduler does.

    The scheduler (``cron.scheduler``) resolves a bare/relative script path
    under ``<HERMES_HOME>/scripts/`` and only accepts absolute paths as-is.
    We MUST mirror that here so the guard scans the file that will actually
    run — otherwise a job whose script lives at the scheduler's real location
    (``~/.hermes/scripts/restart.sh``) but is passed as the bare name
    ``restart.sh`` would read as a nonexistent relative path and silently
    scan prompt-only content, letting the command through.
    """
    from hermes_constants import get_hermes_home

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        return raw
    return get_hermes_home() / "scripts" / raw


def _read_script_for_scanning(script_path: str) -> str:
    """Read a script file for lifecycle-pattern scanning.

    Decodes with ``errors="replace"`` so binary or non-UTF-8 content does not
    silently bypass the check — a plain text-mode read raises
    ``UnicodeDecodeError`` on such files, and swallowing that error would let
    an attacker hide the command in binary noise.  Returns an empty string
    only when the file cannot be read at all.
    """
    try:
        return _resolve_script_path(script_path).read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def check_gateway_lifecycle(
    prompt: Optional[str],
    script: Optional[str] = None,
) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a
    gateway-lifecycle command pattern.

    ``prompt`` is scanned directly.  ``script``, when supplied, is read from
    disk and concatenated for the scan.  Both are considered together so a
    job cannot slip through by splitting the command across the prompt and
    the script.

    Callers should let the exception propagate when they want the create to
    fail with a ``ValueError``-shaped error (the agent's ``cronjob`` tool
    surfaces this as a tool error; the CLI prints it in red and exits 1).
    """
    combined = prompt or ""
    if script:
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if contains_gateway_lifecycle_command(combined):
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command "
            "(restart/stop/kill). This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )

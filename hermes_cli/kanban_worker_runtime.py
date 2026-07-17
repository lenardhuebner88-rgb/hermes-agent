"""Worker runtime helpers for Hermes Kanban dispatch."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

_log = logging.getLogger(__name__)

CLAUDE_CLI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class WorkerLaunchSpec:
    """Runtime-neutral input for the single worker process lifecycle owner."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Optional[str]
    log_path: Path
    missing_executable_message: str


def claude_worker_bin() -> str:
    """Resolve the ``claude`` CLI binary used for claude-CLI worker spawns."""
    env_bin = os.environ.get("HERMES_CLAUDE_BIN")
    if env_bin:
        return env_bin
    default_path = "/home/piet/.local/bin/claude"
    if os.path.exists(default_path):
        return default_path
    return "claude"


def is_claude_cli_profile(profile_arg: str, hermes_home: Optional[str]) -> bool:
    """True if ``profile_arg`` should be dispatched via the ``claude`` CLI."""
    try:
        allow = os.environ.get("HERMES_CLAUDE_CLI_PROFILES", "")
        for name in allow.split(","):
            if name.strip() == profile_arg:
                return True
        if hermes_home:
            try:
                import yaml
            except Exception:
                return False
            cfg_path = os.path.join(hermes_home, "config.yaml")
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                if isinstance(cfg, dict) and cfg.get("worker_runtime") == "claude-cli":
                    return True
        return False
    except Exception:
        return False


def claude_profile_model(hermes_home: Optional[str]) -> Optional[str]:
    """Return the profile-scoped default claude model, if configured."""
    try:
        if not hermes_home:
            return None
        import yaml
        cfg_path = os.path.join(hermes_home, "config.yaml")
        if not os.path.isfile(cfg_path):
            return None
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if isinstance(cfg, dict):
            model = cfg.get("claude_model")
            if isinstance(model, str) and model.strip():
                return model.strip()
        return None
    except Exception:
        return None


def claude_profile_effort(hermes_home: Optional[str]) -> Optional[str]:
    """Return the profile-scoped default claude-cli ``--effort`` level, if
    configured and valid. Invalid values are logged and treated as absent
    (fail-soft — never blocks the spawn)."""
    try:
        if not hermes_home:
            return None
        import yaml
        cfg_path = os.path.join(hermes_home, "config.yaml")
        if not os.path.isfile(cfg_path):
            return None
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if isinstance(cfg, dict):
            effort = cfg.get("claude_effort")
            if isinstance(effort, str) and effort.strip():
                effort = effort.strip()
                if effort in CLAUDE_CLI_EFFORT_LEVELS:
                    return effort
                _log.warning(
                    "claude_profile_effort: invalid claude_effort=%r in %s "
                    "(must be one of %s) — omitting --effort",
                    effort,
                    cfg_path,
                    CLAUDE_CLI_EFFORT_LEVELS,
                )
        return None
    except Exception:
        return None


def claude_profile_fast_mode(hermes_home: Optional[str]) -> bool:
    """Return whether the profile config requests claude-cli fast mode."""
    try:
        if not hermes_home:
            return False
        import yaml
        cfg_path = os.path.join(hermes_home, "config.yaml")
        if not os.path.isfile(cfg_path):
            return False
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if isinstance(cfg, dict):
            return bool(cfg.get("claude_fast_mode"))
        return False
    except Exception:
        return False


def claude_profile_instructions(
    hermes_home: Optional[str],
    *,
    max_chars: int = 12000,
) -> str:
    """Return profile SOUL instructions for claude-CLI workers, fail-soft."""
    try:
        if not hermes_home:
            return ""
        soul_path = os.path.join(hermes_home, "SOUL.md")
        if not os.path.isfile(soul_path):
            return ""
        with open(soul_path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text:
            return ""
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n[SOUL.md truncated by dispatcher]"
        return text
    except Exception:
        return ""


WORKER_ENV_PREFIXES = ("HERMES_", "TERMINAL_", "LC_", "XDG_")

WORKER_ENV_PASSTHROUGH = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM",
    "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "PWD",
    "VIRTUAL_ENV", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec", "PATHEXT",
})

WORKER_LANE_PROVIDER_KEYS = frozenset({
    "OPENROUTER_API_KEY", "MINIMAX_API_KEY", "MINIMAX_BASE_URL",
    "KIMI_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL",
    "HONCHO_API_KEY",
})

CLAUDE_CLI_ALWAYS_DENIED_TOOLS = ("WebFetch", "WebSearch")
CLAUDE_CLI_VERDICT_READ_ONLY_DENIED_TOOLS = (
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "Agent",
)
CLAUDE_CLI_VERDICT_READ_ONLY_PROFILES = {"reviewer", "critic"}

# AC-1 (Verdict-Cage Phase 2): allowlist fail-closed for verdict lanes.
# Instead of --dangerously-skip-permissions + --disallowedTools (denylist),
# verdict lanes use --allowedTools with a minimal read-only set. This is
# enforced by Claude Code's own permission system (no skip-permissions bypass).
# Bash is intentionally EXCLUDED — verdict lanes must not execute code.
# The guard-dangerous-ops.sh PreToolUse hook remains as belt-and-suspenders.
CLAUDE_CLI_VERDICT_ALLOWLIST = ("Read", "Grep", "Glob")

# AC-1 (Verdict-Cage Phase 2) fail-closed gate. The minimum Claude CLI version
# we trust to enforce ``--allowedTools`` as a HARD cage when the worker is
# spawned WITHOUT ``--dangerously-skip-permissions``. A 2026-06 spike found
# ``--allowedTools`` was silently ignored *under* the skip-permissions bypass;
# verdict lanes drop that bypass, so the normal permission engine applies — but
# only on a CLI new enough to have it. Below this version, or if the version is
# undetectable, we MUST fail-closed (refuse to spawn) rather than fall back to
# the denylist+bypass model, which would hand a verdict worker write/exec tools.
# The floor is intentionally conservative (1.0.0): every shipped Claude Code
# release honors ``--allowedTools`` without the bypass; the gate exists to catch
# a missing/broken binary, not to chase a specific patch level.
VERDICT_ALLOWLIST_MIN_CLAUDE_VERSION: Tuple[int, int, int] = (1, 0, 0)

# AC-1 (Verdict-Cage Phase 2): the ONLY sanctioned way to grant a verdict lane
# read-only Bash. Default EMPTY — verdict lanes get Read,Grep,Glob and nothing
# else. Bash must NEVER be added to CLAUDE_CLI_VERDICT_ALLOWLIST directly; an
# operator who genuinely needs e.g. `git diff` in a review sets
# HERMES_REVIEW_BASH_ALLOWLIST to comma-separated Claude Code tool-permission
# strings (e.g. "Bash(git diff:*),Bash(git log:*)") which are appended verbatim
# to the verdict --allowedTools. Keeping this a separate, explicit, default-empty
# seam keeps the read-only cage auditable.
REVIEW_BASH_ALLOWLIST_ENV = "HERMES_REVIEW_BASH_ALLOWLIST"

# Operator/test override for the enforceability gate. "1"/"true"/"yes"/"on"
# pins enforceable, "0"/"false"/"no"/"off" pins NOT enforceable (fail-closed).
# Lets the operator vouch for a CLI whose ``--version`` we cannot parse, and
# lets tests exercise both branches without a real binary.
VERDICT_ALLOWLIST_ENFORCEABLE_ENV = "HERMES_VERDICT_ALLOWLIST_ENFORCEABLE"

# Memoised per binary path: the dispatcher is long-lived and we do not want a
# ``claude --version`` subprocess on every verdict spawn. Versions only climb,
# so a stale-low cache can only over-restrict (fail-closed) until restart — the
# safe direction for a security floor.
_CLAUDE_CLI_VERSION_CACHE: dict[str, Optional[Tuple[int, int, int]]] = {}


def claude_cli_version(
    claude_bin: str,
    *,
    env: Optional[dict] = None,
) -> Optional[Tuple[int, int, int]]:
    """Best-effort ``(major, minor, patch)`` of the Claude CLI, or None.

    Fail-soft: any probe failure (missing binary, non-zero exit, unparsable
    output, timeout) returns None so the caller can decide. Memoised per
    ``claude_bin`` path.
    """
    if claude_bin in _CLAUDE_CLI_VERSION_CACHE:
        return _CLAUDE_CLI_VERSION_CACHE[claude_bin]
    version: Optional[Tuple[int, int, int]] = None
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [claude_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if proc.returncode == 0:
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout or "")
            if match:
                version = (
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                )
    except Exception:
        version = None
    _CLAUDE_CLI_VERSION_CACHE[claude_bin] = version
    return version


def review_bash_allowlist(*, env: Optional[dict] = None) -> Tuple[str, ...]:
    """Operator-configured read-only Bash allowlist entries for verdict lanes.

    Default empty. Entries are full Claude Code tool-permission strings (e.g.
    ``Bash(git diff:*)``) appended verbatim to the verdict ``--allowedTools``.
    Sourced from ``HERMES_REVIEW_BASH_ALLOWLIST`` (comma-separated).
    """
    source = env if env is not None else os.environ
    raw = source.get(REVIEW_BASH_ALLOWLIST_ENV, "") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def verdict_allowlist_enforceable(
    claude_bin: str,
    *,
    env: Optional[dict] = None,
) -> bool:
    """AC-1 fail-closed gate: is ``--allowedTools`` a hard cage on this CLI?

    Resolution order:
      1. Explicit operator/test override ``HERMES_VERDICT_ALLOWLIST_ENFORCEABLE``.
      2. Otherwise a detectable Claude CLI version >= the known-enforcing floor.
         An undetectable version => not enforceable => fail-closed (no spawn).
    """
    source = env if env is not None else os.environ
    override = source.get(VERDICT_ALLOWLIST_ENFORCEABLE_ENV)
    if override is not None and override.strip() != "":
        return override.strip().lower() in ("1", "true", "yes", "on")
    version = claude_cli_version(claude_bin, env=env)
    if version is None:
        return False
    return version >= VERDICT_ALLOWLIST_MIN_CLAUDE_VERSION


def build_worker_env(
    parent_env,
    *,
    passthrough: frozenset[str] = WORKER_ENV_PASSTHROUGH,
    lane_provider_keys: frozenset[str] = WORKER_LANE_PROVIDER_KEYS,
    prefixes: tuple[str, ...] = WORKER_ENV_PREFIXES,
) -> dict:
    """Allowlisted copy of ``parent_env`` for spawned kanban workers."""
    env: dict = {}
    for key, value in parent_env.items():
        if (
            key in passthrough
            or key in lane_provider_keys
            or key.startswith(prefixes)
        ):
            env[key] = value
    return env


def is_claude_verdict_read_only_lane(
    profile_arg: str,
    lane_entry: Optional[dict],
    *,
    read_only_profiles: set[str] = CLAUDE_CLI_VERDICT_READ_ONLY_PROFILES,
) -> bool:
    """Return whether the active runtime map puts this verdict lane on claude-cli."""
    if not lane_entry:
        return False
    if str(lane_entry.get("worker_runtime") or "").strip().lower() != "claude-cli":
        return False
    return profile_arg in read_only_profiles

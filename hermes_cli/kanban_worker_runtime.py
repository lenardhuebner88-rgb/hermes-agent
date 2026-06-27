"""Worker runtime helpers for Hermes Kanban dispatch."""

from __future__ import annotations

import os
from typing import Optional


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

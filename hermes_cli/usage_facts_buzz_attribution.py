"""Deterministic attribution of CLI usage to managed Buzz agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

BUZZ_AGENT_ORIGIN = "buzz_agent"
BUZZ_SOURCE_ORIGIN_PREFIXES = (
    "claude_code",
    "codex_cli",
    "kimi_cli",
    "qwen_cli",
    "grok_cli",
)
BUZZ_SOURCE_ORIGIN_SQL = (
    "CASE "
    + " ".join(
        f"WHEN origin='{BUZZ_AGENT_ORIGIN}' "
        f"AND run_id LIKE '{source_origin}:%' THEN '{source_origin}'"
        for source_origin in BUZZ_SOURCE_ORIGIN_PREFIXES
    )
    + " ELSE origin END"
)
DEFAULT_BUZZ_AGENT_WORKSPACES_ROOT = Path(
    "/mnt/data/services/buzz-agent-workspaces"
)


def buzz_agent_unit_for_workspace(
    workspace: Any,
    *,
    root: Path | str = DEFAULT_BUZZ_AGENT_WORKSPACES_ROOT,
) -> Optional[str]:
    """Return the Buzz unit for an unambiguous workspace below ``root``.

    Paths are compared lexically.  We deliberately do not resolve symlinks or
    require the logged path to still exist: both would make historical
    attribution depend on mutable filesystem state.  Missing, relative,
    escaping, or root-only paths fail closed.
    """
    if not isinstance(workspace, (str, os.PathLike)):
        return None
    raw_workspace = os.fspath(workspace).strip()
    raw_root = os.fspath(root).strip()
    if not raw_workspace or not raw_root:
        return None
    workspace_path = Path(raw_workspace)
    root_path = Path(raw_root)
    if not workspace_path.is_absolute() or not root_path.is_absolute():
        return None
    if ".." in workspace_path.parts or ".." in root_path.parts:
        return None

    normalized_workspace = Path(os.path.normpath(raw_workspace))
    normalized_root = Path(os.path.normpath(raw_root))
    try:
        relative = normalized_workspace.relative_to(normalized_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    unit = relative.parts[0]
    if unit in {"", ".", ".."}:
        return None
    return unit


def origin_for_workspace(
    default_origin: str,
    workspace: Any,
    *,
    root: Path | str = DEFAULT_BUZZ_AGENT_WORKSPACES_ROOT,
) -> str:
    """Classify a positively identified Buzz workspace, otherwise fail closed."""
    if buzz_agent_unit_for_workspace(workspace, root=root) is not None:
        return BUZZ_AGENT_ORIGIN
    return default_origin


def source_origin_for_run(origin: Any, run_id: Any) -> str:
    """Recover the source-native CLI origin for a Buzz-attributed run.

    The run id is the durable source identity already used by every harvester.
    Unknown or malformed Buzz run ids remain ``buzz_agent`` so downstream
    consumers fail closed instead of guessing a source contract.
    """
    normalized_origin = str(origin or "unknown").strip() or "unknown"
    if normalized_origin != BUZZ_AGENT_ORIGIN:
        return normalized_origin
    normalized_run_id = str(run_id or "")
    for source_origin in BUZZ_SOURCE_ORIGIN_PREFIXES:
        if normalized_run_id.startswith(f"{source_origin}:"):
            return source_origin
    return normalized_origin

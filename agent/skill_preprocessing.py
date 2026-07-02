"""Shared SKILL.md preprocessing helpers."""

import logging
import re
import subprocess
from pathlib import Path

from hermes_cli._subprocess_compat import IS_WINDOWS, windows_hide_flags

logger = logging.getLogger(__name__)

_SKILL_DIR_TOKEN = "${HERMES_SKILL_DIR}"
_SESSION_ID_TOKEN = "${HERMES_SESSION_ID}"
_HERMES_TEMPLATE_PREFIX = "${HERMES_"

# Matches inline shell snippets like:  !`date +%Y-%m-%d`
# Non-greedy, single-line only -- no newlines inside the backticks.
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

# Cap inline-shell output so a runaway command can't blow out the context.
_INLINE_SHELL_MAX_OUTPUT = 4000


def load_skills_config() -> dict:
    """Load the ``skills`` section of config.yaml (best-effort)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        skills_cfg = cfg.get("skills")
        if isinstance(skills_cfg, dict):
            return skills_cfg
    except Exception:
        logger.debug("Could not read skills config", exc_info=True)
    return {}


def substitute_template_vars(
    content: str,
    skill_dir: Path | None,
    session_id: str | None,
) -> str:
    """Replace ${HERMES_SKILL_DIR} / ${HERMES_SESSION_ID} in skill content.

    Only substitutes tokens for which a concrete value is available --
    unresolved tokens are left in place so the author can spot them.
    """
    if not content:
        return content

    if _HERMES_TEMPLATE_PREFIX not in content:
        return content

    skill_dir_str = str(skill_dir) if skill_dir else None
    if skill_dir_str and _SKILL_DIR_TOKEN in content:
        content = content.replace(_SKILL_DIR_TOKEN, skill_dir_str)
    if session_id and _SESSION_ID_TOKEN in content:
        content = content.replace(_SESSION_ID_TOKEN, str(session_id))
    return content


def run_inline_shell(command: str, cwd: Path | None, timeout: int) -> str:
    """Execute a single inline-shell snippet and return its stdout (trimmed).

    Failures return a short ``[inline-shell error: ...]`` marker instead of
    raising, so one bad snippet can't wreck the whole skill message.
    """
    _popen_kwargs = {"creationflags": windows_hide_flags()} if IS_WINDOWS else {}
    try:
        timeout_seconds = max(1, int(timeout))
    except (TypeError, ValueError):
        return f"[inline-shell error: invalid timeout: {timeout}]"

    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        return f"[inline-shell timeout after {timeout_seconds}s: {command}]"
    except FileNotFoundError as exc:
        if cwd and not Path(cwd).exists():
            return f"[inline-shell error: cwd not found: {cwd}]"
        logger.debug("Inline shell executable lookup failed", exc_info=True)
        return "[inline-shell error: bash not found]"
    except RuntimeError as exc:
        # tests/conftest.py installs a live-system guard that blocks real
        # os.kill on out-of-tree PIDs. subprocess.run(timeout=...) may trip
        # that guard while trying to clean up the timed-out shell; treat that
        # as the same timeout outcome instead of surfacing the guard error.
        if "live-system guard: blocked os.kill" in str(exc):
            return f"[inline-shell timeout after {timeout}s: {command}]"
        return f"[inline-shell error: {exc}]"
    except Exception as exc:
        return f"[inline-shell error: {exc}]"

    output = (completed.stdout or "").rstrip("\n")
    if not output and completed.stderr:
        output = completed.stderr.rstrip("\n")
    if len(output) > _INLINE_SHELL_MAX_OUTPUT:
        output = output[:_INLINE_SHELL_MAX_OUTPUT] + "...[truncated]"
    return output


def expand_inline_shell(
    content: str,
    skill_dir: Path | None,
    timeout: int,
) -> str:
    """Replace every !`cmd` snippet in ``content`` with its stdout.

    Runs each snippet with the skill directory as CWD so relative paths in
    the snippet work the way the author expects.
    """
    if "!`" not in content:
        return content

    def _replace(match: re.Match) -> str:
        cmd = match.group(1).strip()
        if not cmd:
            return ""
        return run_inline_shell(cmd, skill_dir, timeout)

    return _INLINE_SHELL_RE.sub(_replace, content)


def preprocess_skill_content(
    content: str,
    skill_dir: Path | None,
    session_id: str | None = None,
    skills_cfg: dict | None = None,
) -> str:
    """Apply configured SKILL.md template and inline-shell preprocessing."""
    if not content:
        return content

    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
    if cfg.get("template_vars", True):
        content = substitute_template_vars(content, skill_dir, session_id)
    if cfg.get("inline_shell", False):
        timeout_value = cfg.get("inline_shell_timeout", 10) or 10
        try:
            timeout = max(1, int(timeout_value))
        except (TypeError, ValueError):
            logger.warning(
                "Invalid skills.inline_shell_timeout=%r; using default timeout",
                timeout_value,
            )
            timeout = 10
        content = expand_inline_shell(content, skill_dir, timeout)
    return content

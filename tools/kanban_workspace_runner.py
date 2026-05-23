"""Safe workspace-local command runner for scoped Kanban workers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error, tool_result

_MAX_TIMEOUT_SECONDS = 60
_DEFAULT_TIMEOUT_SECONDS = 10
_MAX_OUTPUT_BYTES = 100_000
_DEFAULT_OUTPUT_BYTES = 20_000
_TRUNCATION_MARKER = b"\n[truncated]\n"
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
_VALID_WORKSPACE_KINDS = {"scratch", "dir", "worktree"}
_ALLOWED_COMMANDS = {
    "python3": {
        "allowed_extensions": {".py"},
        "max_args": 2,
        "deny_flags": {"-c", "-m"},
    },
    "python": {
        "allowed_extensions": {".py"},
        "max_args": 2,
        "deny_flags": {"-c", "-m"},
    },
    "pytest": {
        "allowed_extensions": {".py"},
        "max_args": 2,
        "deny_flags": set(),
        "path_label": "test",
    },
}
_SECRET_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
)


def _check_requirements() -> bool:
    return bool(os.environ.get("HERMES_KANBAN_TASK"))


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _workspace() -> tuple[Path | None, str | None]:
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None, "HERMES_KANBAN_TASK is required"
    workspace_raw = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if not workspace_raw:
        return None, "HERMES_KANBAN_WORKSPACE is required"
    workspace = Path(workspace_raw).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        return None, f"workspace does not exist: {workspace}"
    kind = (os.environ.get("HERMES_KANBAN_WORKSPACE_KIND") or "scratch").strip() or "scratch"
    if kind not in _VALID_WORKSPACE_KINDS:
        return None, f"unknown workspace kind: {kind}"
    root_raw = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT")
    if root_raw and kind == "scratch":
        root = Path(root_raw).expanduser().resolve()
        try:
            workspace.relative_to(root)
        except ValueError:
            return None, f"workspace is outside configured workspaces root: {workspace}"
    return workspace, None


def _truncate(data: bytes, limit: int) -> tuple[bytes, bool]:
    if len(data) <= limit:
        return data, False
    keep = max(0, limit)
    return data[:keep] + _TRUNCATION_MARKER, True


def _safe_env(workspace: Path) -> dict[str, str]:
    env = {
        "PATH": _SAFE_PATH,
        "HOME": str(workspace),
        "PYTHONNOUSERSITE": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return {
        key: value
        for key, value in env.items()
        if not any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
    }


def _validate_argv(argv: Any, workspace: Path) -> tuple[list[str] | None, str | None]:
    if not isinstance(argv, list):
        return None, "argv must be a list of strings"
    if not argv:
        return None, "argv must not be empty"
    if not all(isinstance(item, str) for item in argv):
        return None, "argv must contain only strings"
    if any("\x00" in item for item in argv):
        return None, "argv must not contain NUL bytes"
    command = Path(argv[0]).name
    policy = _ALLOWED_COMMANDS.get(command)
    if policy is None:
        return None, f"command is not allowlisted: {argv[0]}"
    path_label = policy.get("path_label", "script")
    if len(argv) < 2:
        return None, f"{command} requires a workspace-relative {path_label} path"
    target_arg = argv[1]
    if target_arg in policy["deny_flags"] or target_arg.startswith("-"):
        return None, f"flag is not allowed for {command}: {target_arg}"
    if len(argv) > policy["max_args"]:
        return None, f"too many arguments for {command}"
    target = (workspace / target_arg).resolve()
    try:
        target.relative_to(workspace)
    except ValueError:
        return None, f"{path_label} path is outside workspace: {target_arg}"
    if target.suffix not in policy["allowed_extensions"]:
        return None, f"{path_label} extension is not allowlisted: {target_arg}"
    if not target.exists() or not target.is_file():
        return None, f"{path_label} file not found in workspace: {target_arg}"
    normalized = [command, str(target.relative_to(workspace))]
    return normalized, None


def _execution_argv(display_argv: list[str]) -> list[str]:
    # Execute with Hermes' current Python interpreter instead of resolving
    # python/python3 through worker-controlled PATH.
    if display_argv[0] == "pytest":
        return [sys.executable, "-m", "pytest", display_argv[1]]
    return [sys.executable, display_argv[1]]


def _handle_run_workspace_command(args: dict[str, Any], **_kwargs: Any) -> str:
    workspace, error = _workspace()
    if error:
        return tool_error(error)
    assert workspace is not None

    argv, error = _validate_argv(args.get("argv"), workspace)
    if error:
        return tool_error(error)
    assert argv is not None

    timeout_seconds = _clamp_int(
        args.get("timeout_seconds"),
        default=_DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=_MAX_TIMEOUT_SECONDS,
    )
    max_output_bytes = _clamp_int(
        args.get("max_output_bytes"),
        default=_DEFAULT_OUTPUT_BYTES,
        minimum=1,
        maximum=_MAX_OUTPUT_BYTES,
    )

    display_argv = argv
    run_argv = _execution_argv(display_argv)

    started = time.monotonic()
    timed_out = False
    exit_code: int | None
    stdout = b""
    stderr = b""
    try:
        completed = subprocess.run(  # noqa: S603 - validated allowlist, shell=False
            run_argv,
            cwd=str(workspace),
            env=_safe_env(workspace),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    duration_ms = int((time.monotonic() - started) * 1000)

    stdout, stdout_truncated = _truncate(stdout, max_output_bytes)
    stderr, stderr_truncated = _truncate(stderr, max_output_bytes)
    truncated = stdout_truncated or stderr_truncated

    return tool_result({
        "ok": bool(exit_code == 0 and not timed_out),
        "exit_code": exit_code,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "truncated": truncated,
        "cwd": str(workspace),
        "argv": display_argv,
    })


registry.register(
    name="kanban_run_workspace_command",
    toolset="kanban",
    schema={
        "name": "kanban_run_workspace_command",
        "description": "Run a tiny allowlisted command inside the current Kanban task workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "Must be exactly ['python3', 'relative_file.py'], ['python', 'relative_file.py'], or ['pytest', 'relative_test_file.py'].",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds, clamped to 1-60.",
                    "default": _DEFAULT_TIMEOUT_SECONDS,
                },
                "max_output_bytes": {
                    "type": "integer",
                    "description": "Per-stream output byte cap, clamped to 1-100000.",
                    "default": _DEFAULT_OUTPUT_BYTES,
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
    handler=_handle_run_workspace_command,
    check_fn=_check_requirements,
    description="Run an allowlisted workspace-local command for a Kanban worker.",
    emoji="🧪",
)

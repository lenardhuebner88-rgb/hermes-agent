"""Characterization tests for ``agent.shell_hooks._spawn`` error outcomes.

``_spawn`` is the single place a shell hook subprocess is invoked, and it must
return a structured diagnostic dict (never raise) for every failure mode. This
matters most for ``pre_tool_call`` guardrail hooks: if a misconfigured hook
(missing binary, non-executable file) raised instead of returning
``error=...``, the caller could either crash the tool path or — worse — silently
skip the guardrail and let a dangerous tool run unblocked.

Existing coverage exercises the timeout branch; this file pins the remaining
outcomes: command-not-found, not-executable, unparseable command, empty command,
and the success / non-zero-exit diagnostic shape.
"""
from __future__ import annotations

import os
import sys

from agent.shell_hooks import ShellHookSpec, _spawn


def _spec(command: str, timeout: int = 5) -> ShellHookSpec:
    return ShellHookSpec(event="pre_tool_call", command=command, timeout=timeout)


def test_missing_command_returns_command_not_found():
    result = _spawn(_spec("definitely-not-a-real-command-xyz123"), "")
    assert result["error"] == "command not found"
    assert result["returncode"] is None
    assert result["timed_out"] is False


def test_non_executable_file_returns_not_executable(tmp_path):
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(script, 0o644)  # readable but NOT executable
    result = _spawn(_spec(str(script)), "")
    assert result["error"] == "command not executable"
    assert result["returncode"] is None


def test_unparseable_command_returns_parse_error():
    # An unterminated quote makes shlex.split raise ValueError.
    result = _spawn(_spec('echo "unterminated'), "")
    assert result["error"] is not None
    assert "cannot be parsed" in result["error"]
    assert result["returncode"] is None


def test_empty_command_returns_empty_command_error():
    result = _spawn(_spec("   "), "")
    assert result["error"] == "empty command"
    assert result["returncode"] is None


def test_successful_command_populates_diagnostic_shape():
    result = _spawn(_spec("echo hello"), "")
    assert result["error"] is None
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["timed_out"] is False
    assert isinstance(result["elapsed_seconds"], float)


def test_nonzero_exit_is_reported_via_returncode_not_error():
    cmd = f'{sys.executable} -c "import sys; sys.exit(3)"'
    result = _spawn(_spec(cmd), "")
    assert result["returncode"] == 3
    assert result["error"] is None  # a clean non-zero exit is not an "error" outcome


def test_stdin_is_delivered_to_the_hook():
    cmd = f"{sys.executable} -c \"import sys; print(sys.stdin.read().upper())\""
    result = _spawn(_spec(cmd), "ping")
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "PING"

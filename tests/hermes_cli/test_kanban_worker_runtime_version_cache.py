"""Regression tests for the Claude CLI version probe cache."""

import subprocess
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_worker_runtime


CLAUDE_BIN = "/home/piet/.local/bin/claude"
CLAUDE_VERSION_STDOUT = "2.1.7 (Claude Code)\n"


@pytest.fixture(autouse=True)
def _clear_claude_cli_version_cache():
    kanban_worker_runtime._CLAUDE_CLI_VERSION_CACHE.clear()
    yield
    kanban_worker_runtime._CLAUDE_CLI_VERSION_CACHE.clear()


def test_transient_probe_failure_is_not_cached(monkeypatch):
    worker_env = {"PATH": "/home/piet/.local/bin:/usr/bin"}
    run = Mock(
        side_effect=[
            subprocess.TimeoutExpired([CLAUDE_BIN, "--version"], timeout=10),
            subprocess.CompletedProcess(
                [CLAUDE_BIN, "--version"],
                returncode=0,
                stdout=CLAUDE_VERSION_STDOUT,
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(kanban_worker_runtime.subprocess, "run", run)

    assert kanban_worker_runtime.claude_cli_version(CLAUDE_BIN, env=worker_env) is None
    assert kanban_worker_runtime.claude_cli_version(
        CLAUDE_BIN, env=worker_env
    ) == (2, 1, 7)
    assert run.call_count == 2


def test_successful_probe_remains_cached(monkeypatch):
    worker_env = {"PATH": "/home/piet/.local/bin:/usr/bin"}
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [CLAUDE_BIN, "--version"],
            returncode=0,
            stdout=CLAUDE_VERSION_STDOUT,
            stderr="",
        )
    )
    monkeypatch.setattr(kanban_worker_runtime.subprocess, "run", run)

    assert kanban_worker_runtime.claude_cli_version(
        CLAUDE_BIN, env=worker_env
    ) == (2, 1, 7)
    assert kanban_worker_runtime.claude_cli_version(
        CLAUDE_BIN, env=worker_env
    ) == (2, 1, 7)
    assert run.call_count == 1

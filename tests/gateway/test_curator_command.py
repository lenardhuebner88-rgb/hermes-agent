"""Tests for the gateway `/curator` slash handler.

``/curator`` was advertised to gateway users (registered without
``cli_only``) but had no gateway handler, so it slipped past the
unknown-command guard and fell through to the LLM as literal text. The
handler now runs the shared ``hermes_cli.curator`` module in an isolated
subprocess (so its stdout/stderr/stdin can't race the gateway's
process-global streams) and returns the captured output as a monospace
reply. These tests stub ``subprocess.run`` to verify the subprocess
invocation, argument parsing via ``get_command_args()`` (including the
gateway-canonicalized ``/curator@Bot …`` form), the default-to-status
behavior, output capture + code-fence wrapping, and that a failing curator
exit (usage on stderr) is surfaced rather than crashing the gateway.
"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


def _event(text):
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat1",
        thread_id="th1",
        user_id="u1",
    )
    # Mirror MessageEvent.get_command_args(): everything after the first
    # whitespace-delimited token (the command, which get_command() already
    # canonicalized — stripping any ``@bot`` suffix and case).
    parts = text.split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""
    return SimpleNamespace(text=text, source=source, get_command_args=lambda: args)


def _runner():
    return object.__new__(GatewayRunner)


def _patch_run(monkeypatch, seen, stdout="curator: DISABLED\n  runs:           0\n",
               stderr="", returncode=0):
    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.mark.asyncio
async def test_curator_status_captures_and_wraps(monkeypatch):
    seen = {}
    _patch_run(monkeypatch, seen)

    out = await GatewayRunner._handle_curator_command(_runner(), _event("/curator status"))

    assert seen["cmd"] == [sys.executable, "-m", "hermes_cli.curator", "status"]
    # stdin must be DEVNULL so interactive confirms abort instead of blocking
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    assert out == "```\ncurator: DISABLED\n  runs:           0\n```"


@pytest.mark.asyncio
async def test_curator_defaults_to_status(monkeypatch):
    seen = {}
    _patch_run(monkeypatch, seen)

    await GatewayRunner._handle_curator_command(_runner(), _event("/curator"))

    assert seen["cmd"] == [sys.executable, "-m", "hermes_cli.curator", "status"]


@pytest.mark.asyncio
async def test_curator_passes_subcommand_args(monkeypatch):
    seen = {}
    _patch_run(monkeypatch, seen, stdout="curator: pinned 'foo'\n")

    await GatewayRunner._handle_curator_command(_runner(), _event("/curator pin foo"))

    assert seen["cmd"] == [sys.executable, "-m", "hermes_cli.curator", "pin", "foo"]


@pytest.mark.asyncio
async def test_curator_handles_bot_suffix_and_case(monkeypatch):
    # The dispatcher canonicalizes `/curator@HermesBot status` and routes it
    # here; the handler must read get_command_args() so the subcommand survives
    # instead of leaking the command token into argparse (which would reject it).
    seen = {}
    _patch_run(monkeypatch, seen)

    await GatewayRunner._handle_curator_command(_runner(), _event("/curator@HermesBot status"))

    assert seen["cmd"] == [sys.executable, "-m", "hermes_cli.curator", "status"]


@pytest.mark.asyncio
async def test_curator_surfaces_failed_exit_stderr(monkeypatch):
    seen = {}
    _patch_run(
        monkeypatch,
        seen,
        stdout="",
        stderr="usage: hermes curator [-h] ...\ncurator: error: invalid choice\n",
        returncode=2,
    )

    out = await GatewayRunner._handle_curator_command(_runner(), _event("/curator bogus"))

    assert "usage:" in out
    assert out.startswith("```")

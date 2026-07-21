"""Tests for the gateway `/curator` slash handler.

``/curator`` was advertised to gateway users (registered without
``cli_only``) but had no gateway handler, so it slipped past the
unknown-command guard and fell through to the LLM as literal text. The
handler now delegates to the shared ``hermes_cli.curator.cli_main`` (the
same entry point the classic CLI uses), capturing its stdout/stderr and
returning it as a monospace reply. These tests stub ``cli_main`` to verify
argument parsing, default-to-status, output capture + code-fence wrapping,
and that an argparse ``SystemExit`` is contained instead of crashing the
gateway worker.
"""

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
    return SimpleNamespace(text=text, source=source)


def _runner():
    return object.__new__(GatewayRunner)


@pytest.mark.asyncio
async def test_curator_status_captures_and_wraps(monkeypatch):
    import hermes_cli.curator as curator_mod

    seen = {}

    def fake_cli_main(argv=None):
        seen["argv"] = argv
        print("curator: DISABLED")
        print("  runs:           0")
        return 0

    monkeypatch.setattr(curator_mod, "cli_main", fake_cli_main)

    out = await GatewayRunner._handle_curator_command(_runner(), _event("/curator status"))

    assert seen["argv"] == ["status"]
    assert out == "```\ncurator: DISABLED\n  runs:           0\n```"


@pytest.mark.asyncio
async def test_curator_defaults_to_status(monkeypatch):
    import hermes_cli.curator as curator_mod

    seen = {}

    def fake_cli_main(argv=None):
        seen["argv"] = argv
        print("curator: DISABLED")
        return 0

    monkeypatch.setattr(curator_mod, "cli_main", fake_cli_main)

    await GatewayRunner._handle_curator_command(_runner(), _event("/curator"))

    assert seen["argv"] == ["status"]


@pytest.mark.asyncio
async def test_curator_passes_subcommand_args(monkeypatch):
    import hermes_cli.curator as curator_mod

    seen = {}

    def fake_cli_main(argv=None):
        seen["argv"] = argv
        print("curator: pinned 'foo'")
        return 0

    monkeypatch.setattr(curator_mod, "cli_main", fake_cli_main)

    await GatewayRunner._handle_curator_command(_runner(), _event("/curator pin foo"))

    assert seen["argv"] == ["pin", "foo"]


@pytest.mark.asyncio
async def test_curator_contains_argparse_systemexit(monkeypatch):
    import sys

    import hermes_cli.curator as curator_mod

    def fake_cli_main(argv=None):
        # argparse on a bad subcommand prints usage to stderr then exits 2
        print("usage: hermes curator [-h] ...", file=sys.stderr)
        raise SystemExit(2)

    monkeypatch.setattr(curator_mod, "cli_main", fake_cli_main)

    out = await GatewayRunner._handle_curator_command(_runner(), _event("/curator bogus"))

    # SystemExit is caught; captured stderr usage becomes the reply
    assert "usage:" in out
    assert out.startswith("```")

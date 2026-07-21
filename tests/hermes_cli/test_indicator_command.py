"""Tests for the classic-CLI `/indicator` command (display.tui_status_indicator).

The setting was previously only reachable via the TUI-gateway
``config.set indicator`` RPC; the advertised classic-CLI ``/indicator``
command was registered but had no handler, so typing it printed
``Unknown command``. These assert the CLI handler now validates the style
against the same set as the RPC (ascii | emoji | kaomoji | unicode) and
persists it to config.yaml.
"""

import yaml

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _Stub(CLICommandsMixin):
    def __init__(self):
        pass


def _seed(tmp_path, monkeypatch, value="kaomoji"):
    hh = tmp_path / ".hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text(f"display:\n  tui_status_indicator: {value}\n")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    import cli

    monkeypatch.setattr(cli, "_hermes_home", hh, raising=False)
    return hh


def _read_indicator(hh):
    display = (yaml.safe_load((hh / "config.yaml").read_text()) or {}).get("display", {})
    return display.get("tui_status_indicator")


def test_indicator_sets_and_persists(tmp_path, monkeypatch):
    hh = _seed(tmp_path, monkeypatch, value="kaomoji")
    s = _Stub()
    s._handle_indicator_command("/indicator emoji")
    assert _read_indicator(hh) == "emoji"


def test_indicator_rejects_unknown_style(tmp_path, monkeypatch):
    hh = _seed(tmp_path, monkeypatch, value="kaomoji")
    s = _Stub()
    s._handle_indicator_command("/indicator rainbow")
    # invalid style is not persisted
    assert _read_indicator(hh) == "kaomoji"


def test_indicator_status_does_not_write(tmp_path, monkeypatch):
    hh = _seed(tmp_path, monkeypatch, value="unicode")
    s = _Stub()
    s._handle_indicator_command("/indicator status")
    assert _read_indicator(hh) == "unicode"


def test_indicator_normalizes_case_and_whitespace(tmp_path, monkeypatch):
    hh = _seed(tmp_path, monkeypatch, value="kaomoji")
    s = _Stub()
    s._handle_indicator_command("/indicator  EMOJI ")
    assert _read_indicator(hh) == "emoji"

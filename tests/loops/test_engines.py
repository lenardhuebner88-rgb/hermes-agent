"""Tests für loops.engines — Registry, Usage-Limit-Erkennung, Claude-CLI-Adapter."""

from __future__ import annotations

import subprocess

import pytest

from loops import engines
from loops.engines import claude_cli


def test_registry_contains_claude_and_rejects_unknown():
    assert "claude" in engines.ENGINES
    with pytest.raises(KeyError, match="warpantrieb"):
        engines.get_engine("warpantrieb")


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your session limit · resets 9:50pm (Europe/Berlin)",
        "You have reached your usage limit",
        "API error: 429 Too Many Requests",
        "rate limit exceeded, retry later",
        "You've hit your usage limit",
    ],
)
def test_usage_limit_detected(text):
    assert engines.detect_usage_limit(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Alles gut, 12 Tests grün",
        "limit_order.py angepasst",  # 'limit' allein reicht nicht
        "",
    ],
)
def test_usage_limit_not_overtriggered(text):
    assert engines.detect_usage_limit(text) is False


def test_claude_cli_builds_headless_command(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.run("claude-fable-5", "sag OK", tmp_path, 60)
    assert result.rc == 0 and result.output == "OK" and result.usage_limit is False
    assert seen["cwd"] == str(tmp_path)
    cmd = seen["cmd"]
    assert cmd[0].endswith("claude")
    assert "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-fable-5"
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[-1] == "sag OK"


def test_claude_cli_timeout_maps_to_timed_out(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=b"teil", stderr=b"")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.run("claude-fable-5", "x", tmp_path, 60)
    assert result.timed_out is True and result.rc == 124
    assert "teil" in result.output


def test_claude_cli_flags_usage_limit_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="You've hit your session limit · resets 9:50pm", stderr=""
        )

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.run("claude-sonnet-5", "x", tmp_path, 60)
    assert result.usage_limit is True

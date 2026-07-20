"""Tests for hermes_cli/colors (ANSI color gating + formatting).

should_use_color() consults NO_COLOR, TERM=dumb and sys.stdout.isatty().
We swap the module's own ``os``/``sys`` references for fakes so each case is
deterministic and does not disturb pytest's capture.
"""

from __future__ import annotations

import pytest

from hermes_cli import colors


class _FakeStdout:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _FakeSys:
    def __init__(self, tty: bool):
        self.stdout = _FakeStdout(tty)


class _FakeOs:
    def __init__(self, env: dict):
        self.environ = env


@pytest.fixture
def patch_env(monkeypatch):
    """Return a setter that installs a fake os.environ + sys.stdout.isatty."""

    def _set(env: dict, tty: bool):
        monkeypatch.setattr(colors, "os", _FakeOs(env))
        monkeypatch.setattr(colors, "sys", _FakeSys(tty))

    return _set


class TestShouldUseColor:
    def test_tty_without_no_color_uses_color(self, patch_env):
        patch_env({"TERM": "xterm-256color"}, tty=True)
        assert colors.should_use_color() is True

    def test_no_color_set_disables_even_when_tty(self, patch_env):
        patch_env({"NO_COLOR": "1", "TERM": "xterm"}, tty=True)
        assert colors.should_use_color() is False

    def test_empty_no_color_still_disables(self, patch_env):
        # Per no-color.org, the mere PRESENCE of NO_COLOR disables color.
        patch_env({"NO_COLOR": ""}, tty=True)
        assert colors.should_use_color() is False

    def test_term_dumb_disables(self, patch_env):
        patch_env({"TERM": "dumb"}, tty=True)
        assert colors.should_use_color() is False

    def test_non_tty_disables(self, patch_env):
        patch_env({"TERM": "xterm"}, tty=False)
        assert colors.should_use_color() is False


class TestColor:
    def test_disabled_returns_text_unchanged(self, patch_env):
        patch_env({"NO_COLOR": "1"}, tty=True)
        assert colors.color("hello", colors.Colors.RED) == "hello"

    def test_enabled_wraps_text_with_codes_and_reset(self, patch_env):
        patch_env({"TERM": "xterm"}, tty=True)
        out = colors.color("hi", colors.Colors.RED)
        assert out == "\033[31mhi\033[0m"

    def test_multiple_codes_are_joined_in_order(self, patch_env):
        patch_env({"TERM": "xterm"}, tty=True)
        out = colors.color("hi", colors.Colors.BOLD, colors.Colors.GREEN)
        assert out == "\033[1m\033[32mhi\033[0m"

    def test_no_codes_still_appends_reset_when_enabled(self, patch_env):
        patch_env({"TERM": "xterm"}, tty=True)
        assert colors.color("hi") == "hi\033[0m"


class TestColorConstants:
    def test_reset_and_a_few_colors(self):
        assert colors.Colors.RESET == "\033[0m"
        assert colors.Colors.RED == "\033[31m"
        assert colors.Colors.GREEN == "\033[32m"
        assert colors.Colors.CYAN == "\033[36m"

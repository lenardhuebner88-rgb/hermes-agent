"""Tests for the Mattermost plugin's interactive_setup wizard.

The wizard lazy-imports its CLI helpers from ``hermes_cli.config``
(get_env_value / save_env_value) and ``hermes_cli.cli_output``
(prompt / prompt_yes_no / print_*); we patch those source modules.

Covers the hidden-knob contract: the home channel is a self-configuring
knob that every setup surface hides (dashboard/Desktop cards hide it via
the ``*_HOME_CHANNEL*`` suffix rule), so the wizard must not ask for it —
``/set-home`` sets it later. An existing MATTERMOST_HOME_CHANNEL must
survive reconfiguration untouched. Supersedes the clear-on-blank flow
from PR #58421, which reintroduced the prompt the setup-forms policy
removes.
"""
import hermes_cli.config as config_mod
import hermes_cli.cli_output as cli_output_mod
from plugins.platforms.mattermost.adapter import interactive_setup


# Mattermost prompts: server_url, bot_token (password), allowed_users.
# A fourth prompt (e.g. a reintroduced home-channel question) raises
# StopIteration here, so every test doubles as the asks-only-3-answers
# guard.
_PROMPTS = ["https://mm.example.com", "«redacted:mm-token»", "user1,user2"]


def _patch_setup_io(monkeypatch, prompts, saved, removed, existing):
    prompt_iter = iter(prompts)
    monkeypatch.setattr(config_mod, "get_env_value", lambda key: existing.get(key, ""))
    monkeypatch.setattr(config_mod, "save_env_value", lambda k, v: saved.update({k: v}))

    def _remove(key):
        removed.append(key)
        return existing.pop(key, None) is not None

    monkeypatch.setattr(config_mod, "remove_env_value", _remove)
    monkeypatch.setattr(cli_output_mod, "prompt", lambda *_a, **_kw: next(prompt_iter))
    monkeypatch.setattr(cli_output_mod, "prompt_yes_no", lambda *_a, **_kw: False)
    for name in ("print_header", "print_info", "print_success", "print_warning"):
        monkeypatch.setattr(cli_output_mod, name, lambda *_a, **_kw: None)


class TestMattermostHiddenKnobs:
    """Home channel is hidden on every setup surface — the wizard must not ask."""

    def test_wizard_asks_url_token_allowlist_only(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        saved, removed = {}, []
        _patch_setup_io(monkeypatch, _PROMPTS, saved, removed, existing={})
        interactive_setup()
        assert saved["MATTERMOST_URL"] == "https://mm.example.com"
        assert saved["MATTERMOST_TOKEN"] == "«redacted:mm-token»"
        assert saved["MATTERMOST_ALLOWED_USERS"] == "user1,user2"
        assert "MATTERMOST_HOME_CHANNEL" not in saved
        assert removed == []

    def test_existing_home_channel_survives_reconfigure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        existing = {"MATTERMOST_HOME_CHANNEL": "old-channel-id"}
        saved, removed = {}, []
        _patch_setup_io(monkeypatch, _PROMPTS, saved, removed, existing=existing)
        interactive_setup()
        assert existing["MATTERMOST_HOME_CHANNEL"] == "old-channel-id"
        assert "MATTERMOST_HOME_CHANNEL" not in saved
        assert removed == []

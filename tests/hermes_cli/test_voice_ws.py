import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.voice_ws import DEFAULT_LIVE_MODEL, VoiceWebConfig, voice_web_config


def test_voice_web_config_defaults_off():
    cfg = voice_web_config({})
    assert cfg.enabled is False
    assert cfg.model == "gemini-2.5-flash-native-audio-preview-12-2025"
    assert cfg.language == "de-DE"

    assert DEFAULT_CONFIG["voice_web"] == {
        "enabled": False,
        "model": "gemini-2.5-flash-native-audio-preview-12-2025",
        "language": "de-DE",
    }


@pytest.mark.parametrize("section", ["false", True, ["enabled"], 1])
def test_voice_web_config_rejects_malformed_section(section):
    assert voice_web_config({"voice_web": section}) == VoiceWebConfig()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, False),
        ("true", False),
        ([True], False),
        (None, False),
    ],
)
def test_voice_web_config_only_literal_true_enables(value, expected):
    cfg = voice_web_config({"voice_web": {"enabled": value}})
    assert cfg.enabled is expected


@pytest.mark.parametrize("value", [None, "", "   ", 42, False, []])
def test_voice_web_config_defaults_invalid_model_and_language(value):
    cfg = voice_web_config(
        {"voice_web": {"model": value, "language": value}}
    )
    assert cfg.model == DEFAULT_LIVE_MODEL
    assert cfg.language == "de-DE"


def test_voice_web_config_accepts_nonblank_string_overrides():
    cfg = voice_web_config(
        {"voice_web": {"model": "gemini-live-custom", "language": "de-AT"}}
    )
    assert cfg.model == "gemini-live-custom"
    assert cfg.language == "de-AT"

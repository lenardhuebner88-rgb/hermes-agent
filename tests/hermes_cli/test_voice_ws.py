from hermes_cli.config import DEFAULT_CONFIG


def test_voice_web_config_defaults_off():
    from hermes_cli.voice_ws import voice_web_config

    cfg = voice_web_config({})
    assert cfg.enabled is False
    assert cfg.model == "gemini-2.5-flash-native-audio-preview-12-2025"
    assert cfg.language == "de-DE"

    assert DEFAULT_CONFIG["voice_web"] == {
        "enabled": False,
        "model": "gemini-2.5-flash-native-audio-preview-12-2025",
        "language": "de-DE",
    }

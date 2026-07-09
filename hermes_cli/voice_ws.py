"""Configuration for the standalone voice web surface."""

from dataclasses import dataclass


DEFAULT_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"


@dataclass
class VoiceWebConfig:
    enabled: bool = False
    model: str = DEFAULT_LIVE_MODEL
    language: str = "de-DE"


def voice_web_config(raw: dict) -> VoiceWebConfig:
    """Read the voice web feature flag and Live API defaults."""
    section = raw.get("voice_web") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        section = {}

    model = section.get("model")
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_LIVE_MODEL

    language = section.get("language")
    if not isinstance(language, str) or not language.strip():
        language = "de-DE"

    return VoiceWebConfig(
        enabled=section.get("enabled") is True,
        model=model,
        language=language,
    )

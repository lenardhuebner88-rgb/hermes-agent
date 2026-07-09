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
    section = (raw or {}).get("voice_web") or {}
    return VoiceWebConfig(
        enabled=bool(section.get("enabled", False)),
        model=str(section.get("model", DEFAULT_LIVE_MODEL)),
        language=str(section.get("language", "de-DE")),
    )

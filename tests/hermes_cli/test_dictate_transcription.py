"""Tests for hermes_cli.dictate_transcription — the hint-aware wrapper the
/api/audio/transcribe endpoint calls.

Live bug reproduced 2026-08-06: the old inline code in web_server.py called
``tools.transcription_tools.transcribe_audio(temp_path, language=..., ...)``,
but that function's signature is ``transcribe_audio(file_path, model=None)`` —
no ``language``/``initial_prompt`` kwargs exist there. Every hinted request
raised ``TypeError`` and surfaced as HTTP 502 ("Transcription failed").
"""

import struct
import sys
import types
import wave
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.dictate_transcription import transcribe_with_hints


def _has_faster_whisper() -> bool:
    try:
        return find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


@pytest.fixture(autouse=True)
def _stub_missing_faster_whisper(monkeypatch):
    """tools.transcription_tools imports faster_whisper lazily at module scope."""
    if _has_faster_whisper():
        return
    stub = types.ModuleType("faster_whisper")
    stub.WhisperModel = MagicMock(name="WhisperModel")
    stub.__spec__ = ModuleSpec("faster_whisper", loader=None)
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)


@pytest.fixture
def sample_wav(tmp_path):
    """A minimal valid WAV file (1 second of silence at 16kHz) — the real
    upload format the endpoint hands to the transcriber after decoding the
    client's base64 payload."""
    wav_path = tmp_path / "test.wav"
    n_frames = 16000
    silence = struct.pack(f"<{n_frames}h", *([0] * n_frames))
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(silence)
    return str(wav_path)


class TestNeverCrashesWithHints:
    """The exact live failure: language + initial_prompt used to raise TypeError."""

    def test_language_and_initial_prompt_do_not_raise(self, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = "hallo welt"

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=mock_client), \
             patch(
                 "tools.transcription_tools._load_stt_config",
                 return_value={"provider": "groq"},
             ):
            result = transcribe_with_hints(
                sample_wav, language="de", initial_prompt="Fachbegriffe: Kanban, Diff"
            )

        assert result["success"] is True
        assert result["transcript"] == "hallo welt"

    def test_unsupported_provider_with_hints_falls_back_without_crash(
        self, monkeypatch, sample_wav
    ):
        """A provider whose handler has no language/prompt hook at all (e.g.
        local) must still return a transcript, never raise."""
        fake_result = {"success": True, "transcript": "fallback text", "provider": "local"}
        with patch(
            "tools.transcription_tools._load_stt_config",
            return_value={"provider": "local"},
        ), patch(
            "tools.transcription_tools._get_provider", return_value="local"
        ), patch(
            "tools.transcription_tools.transcribe_audio", return_value=fake_result
        ) as mock_transcribe:
            result = transcribe_with_hints(sample_wav, language="de", initial_prompt="hint")

        assert result == fake_result
        mock_transcribe.assert_called_once_with(sample_wav, model=None)


class TestProviderSignatureClasses:
    """openai/mistral/xai/elevenlabs/local_command accept `language` in their
    private handler signature (verified via AST); groq/local do not."""

    def test_provider_with_language_param_gets_it_forwarded(self, sample_wav):
        with patch(
            "tools.transcription_tools._load_stt_config",
            return_value={"provider": "openai"},
        ), patch(
            "tools.transcription_tools._get_provider", return_value="openai"
        ), patch(
            "tools.transcription_tools._transcribe_openai",
            autospec=True,
            return_value={"success": True, "transcript": "ok", "provider": "openai"},
        ) as mock_openai:
            result = transcribe_with_hints(sample_wav, language="de")

        assert result["success"] is True
        assert mock_openai.call_args.kwargs.get("language") == "de"

    def test_hallucination_on_silence_is_filtered_on_the_hinted_path(self, sample_wav):
        """Speaking into a quiet room must not return invented subtitle credits.

        The unhinted path filters these via transcribe_recording. The hinted
        paths call providers directly, so they skipped the filter — and since
        both clients always send a language, that left the filter effectively
        off for every real dictation. Verified against the live symptom: a
        hinted request returned "Untertitel von …" verbatim where the unhinted
        one returned "".
        """
        with patch(
            "tools.transcription_tools._load_stt_config",
            return_value={"provider": "openai"},
        ), patch(
            "tools.transcription_tools._get_provider", return_value="openai"
        ), patch(
            "tools.transcription_tools._transcribe_openai",
            autospec=True,
            return_value={
                "success": True,
                "transcript": "Untertitel von Stephanie Geiges",
                "provider": "openai",
            },
        ):
            result = transcribe_with_hints(sample_wav, language="de")

        assert result["success"] is True
        assert result["transcript"] == ""
        assert result["filtered"] is True

    def test_renamed_upstream_handler_falls_back_instead_of_raising(self, sample_wav):
        """An upstream rename must not become a 502.

        _HINT_CAPABLE_HANDLERS names private upstream symbols. If a sync
        renames one, a bare getattr would raise AttributeError, which the
        endpoint turns into "Cloud-Transkription fehlgeschlagen" — the exact
        failure mode this whole module was written to remove.
        """
        with patch(
            "tools.transcription_tools._load_stt_config",
            return_value={"provider": "openai"},
        ), patch(
            "tools.transcription_tools._get_provider", return_value="openai"
        ), patch.dict(
            "hermes_cli.dictate_transcription._HINT_CAPABLE_HANDLERS",
            {"openai": "_transcribe_openai_renamed_by_upstream"},
        ), patch(
            "tools.transcription_tools.transcribe_audio",
            autospec=True,
            return_value={"success": True, "transcript": "hallo welt", "provider": "openai"},
        ) as mock_plain:
            result = transcribe_with_hints(sample_wav, language="de")

        assert result["success"] is True
        assert result["transcript"] == "hallo welt"
        assert mock_plain.called, "must fall back to the plain path, not raise"

    def test_provider_without_language_param_still_succeeds(self, monkeypatch, sample_wav):
        """groq's private handler has no language param at all — this must
        route through the dedicated hint-aware Groq call instead, not crash."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        mock_client = MagicMock()
        # Not "ok": the shared hallucination filter treats a bare "ok" as
        # silence noise, so it would be blanked and hide what this test checks.
        mock_client.audio.transcriptions.create.return_value = "hallo welt"

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=mock_client), \
             patch(
                 "tools.transcription_tools._load_stt_config",
                 return_value={"provider": "groq"},
             ):
            result = transcribe_with_hints(sample_wav, language="de")

        assert result["success"] is True
        assert result["transcript"] == "hallo welt"


class TestGroqRequestFormat:
    """Groq is OpenAI-compatible: language/prompt are real multipart fields
    on `client.audio.transcriptions.create()` — asserted against the actual
    openai.OpenAI client call, not a mock of our own code."""

    def test_language_and_prompt_reach_the_groq_request(self, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = "bonjour"

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=mock_client) as mock_openai_cls, \
             patch(
                 "tools.transcription_tools._load_stt_config",
                 return_value={"provider": "groq"},
             ):
            result = transcribe_with_hints(
                sample_wav, language="fr", initial_prompt="Kanban, PlanSpec"
            )

        assert result["transcript"] == "bonjour"
        assert mock_openai_cls.call_args.kwargs["base_url"] == "https://api.groq.com/openai/v1"

        call_kwargs = mock_client.audio.transcriptions.create.call_args.kwargs
        assert call_kwargs["language"] == "fr"
        assert call_kwargs["prompt"] == "Kanban, PlanSpec"

    def test_groq_without_hints_still_omits_them(self, monkeypatch, sample_wav):
        """Sanity check: transcribe_with_hints() only reaches the groq+hints
        path when a hint is present at all (see TestNoHints below for the
        actual no-hints code path)."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        mock_client = MagicMock()
        # Not "ok": the shared hallucination filter treats a bare "ok" as
        # silence noise, so it would be blanked and hide what this test checks.
        mock_client.audio.transcriptions.create.return_value = "hallo welt"

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("openai.OpenAI", return_value=mock_client), \
             patch(
                 "tools.transcription_tools._load_stt_config",
                 return_value={"provider": "groq"},
             ):
            transcribe_with_hints(sample_wav, initial_prompt="only a prompt, no language")

        call_kwargs = mock_client.audio.transcriptions.create.call_args.kwargs
        assert "language" not in call_kwargs
        assert call_kwargs["prompt"] == "only a prompt, no language"


class TestNoHintsUnchanged:
    """Without language/initial_prompt, behavior must be identical to the
    pre-existing transcribe_recording() path (hallucination filtering,
    empty-transcript-as-silence, etc.)."""

    def test_delegates_to_transcribe_recording(self, sample_wav):
        with patch(
            "tools.voice_mode.transcribe_recording",
            return_value={"success": True, "transcript": "unchanged", "filtered": False},
        ) as mock_recording:
            result = transcribe_with_hints(sample_wav)

        assert result == {"success": True, "transcript": "unchanged", "filtered": False}
        mock_recording.assert_called_once_with(sample_wav, model=None)

    def test_blank_hints_are_treated_as_absent(self, sample_wav):
        """The endpoint already strips whitespace-only hints to None before
        calling us, but this module must not depend on that."""
        with patch(
            "tools.voice_mode.transcribe_recording",
            return_value={"success": True, "transcript": "unchanged"},
        ) as mock_recording:
            result = transcribe_with_hints(sample_wav, language="   ", initial_prompt="  ")

        assert result["transcript"] == "unchanged"
        mock_recording.assert_called_once_with(sample_wav, model=None)

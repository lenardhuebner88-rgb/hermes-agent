"""Unit tests for scripts/voice_spar_smoke.py — stage error paths with fakes.

Each stage function is tested against a fake of the underlying real call
(Piper/whisper/claude-lane) it wraps — the SUT here is the smoke script's own
stage/error-reporting logic, not a re-test of Piper/faster-whisper/the
claude CLI themselves (those already have their own real-process tests in
test_voice_spar_session.py). ``main()``'s own real, end-to-end run is
exercised separately as one-off evidence (not part of the automated gate —
it costs real Piper/whisper CPU time and a real claude-CLI turn).
"""

from __future__ import annotations

from pathlib import Path
import wave

import pytest

from scripts.voice_spar_smoke import (
    SmokeError,
    run_llm_stage,
    run_stt_stage,
    run_tts_stage,
)


def _write_minimal_wav(path: Path, *, frames: int = 2205) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x10\x00" * frames)


def test_run_tts_stage_succeeds_with_real_wav_header(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    def fake_synthesize_to_wav(text, *, voice_path, output_path):
        assert text == voice_spar_smoke.TEST_SENTENCE
        _write_minimal_wav(output_path)

    monkeypatch.setattr(voice_spar_smoke, "synthesize_to_wav", fake_synthesize_to_wav)

    output_path = tmp_path / "out.wav"
    run_tts_stage("unused-voice-path", output_path)  # must not raise

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnframes() > 0


def test_run_tts_stage_raises_on_synth_failure(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    def failing_synthesize_to_wav(text, *, voice_path, output_path):
        raise RuntimeError("piper boom")

    monkeypatch.setattr(voice_spar_smoke, "synthesize_to_wav", failing_synthesize_to_wav)

    with pytest.raises(SmokeError) as excinfo:
        run_tts_stage("unused-voice-path", tmp_path / "out.wav")
    assert excinfo.value.stage == "tts"
    assert "piper boom" in excinfo.value.reason


def test_run_tts_stage_raises_when_no_file_produced(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    def noop_synthesize_to_wav(text, *, voice_path, output_path):
        pass  # never writes the file

    monkeypatch.setattr(voice_spar_smoke, "synthesize_to_wav", noop_synthesize_to_wav)

    with pytest.raises(SmokeError) as excinfo:
        run_tts_stage("unused-voice-path", tmp_path / "out.wav")
    assert excinfo.value.stage == "tts"


def test_run_tts_stage_raises_on_invalid_wav_header(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    output_path = tmp_path / "out.wav"

    def fake_synthesize_to_wav(text, *, voice_path, output_path):
        output_path.write_bytes(b"not a real wav file")

    monkeypatch.setattr(voice_spar_smoke, "synthesize_to_wav", fake_synthesize_to_wav)

    with pytest.raises(SmokeError) as excinfo:
        run_tts_stage("unused-voice-path", output_path)
    assert excinfo.value.stage == "tts"


def test_run_stt_stage_returns_transcript_on_good_fuzzy_match(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    def fake_transcribe_wav(wav_path, *, model_size, language=None):
        assert model_size == "small"
        assert language == "de-DE"
        return "Der Server läuft heute Nacht stabil."

    monkeypatch.setattr(voice_spar_smoke, "transcribe_wav", fake_transcribe_wav)

    wav_path = tmp_path / "in.wav"
    _write_minimal_wav(wav_path)
    transcript = run_stt_stage(wav_path, model_size="small", language="de-DE")
    assert transcript == "Der Server läuft heute Nacht stabil."


def test_run_stt_stage_raises_on_transcription_failure(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    def failing_transcribe_wav(wav_path, *, model_size, language=None):
        raise RuntimeError("whisper boom")

    monkeypatch.setattr(voice_spar_smoke, "transcribe_wav", failing_transcribe_wav)

    with pytest.raises(SmokeError) as excinfo:
        run_stt_stage(tmp_path / "in.wav", model_size="small", language="de-DE")
    assert excinfo.value.stage == "stt"
    assert "whisper boom" in excinfo.value.reason


def test_run_stt_stage_raises_when_transcript_does_not_fuzzy_match(monkeypatch, tmp_path):
    from scripts import voice_spar_smoke

    def fake_transcribe_wav(wav_path, *, model_size, language=None):
        return "Ganz etwas anderes."

    monkeypatch.setattr(voice_spar_smoke, "transcribe_wav", fake_transcribe_wav)

    with pytest.raises(SmokeError) as excinfo:
        run_stt_stage(tmp_path / "in.wav", model_size="small", language="de-DE")
    assert excinfo.value.stage == "stt"
    assert "did not fuzzy-match" in excinfo.value.reason


@pytest.mark.asyncio
async def test_run_llm_stage_returns_reply_on_success(monkeypatch):
    from scripts import voice_spar_smoke

    async def fake_call_llm_lane(lane, prompt, *, model, timeout, cwd=None):
        assert lane == "claude"
        assert model == "haiku"
        assert timeout == voice_spar_smoke._LLM_TIMEOUT_SECONDS
        return "Vier."

    monkeypatch.setattr(voice_spar_smoke, "call_llm_lane", fake_call_llm_lane)

    reply = await run_llm_stage("haiku")
    assert reply == "Vier."


@pytest.mark.asyncio
async def test_run_llm_stage_raises_on_llm_lane_error(monkeypatch):
    from scripts import voice_spar_smoke
    from hermes_cli.voice_spar_session import LlmLaneError

    async def failing_call_llm_lane(lane, prompt, *, model, timeout, cwd=None):
        raise LlmLaneError("claude-lane boom")

    monkeypatch.setattr(voice_spar_smoke, "call_llm_lane", failing_call_llm_lane)

    with pytest.raises(SmokeError) as excinfo:
        await run_llm_stage("haiku")
    assert excinfo.value.stage == "llm"
    assert "claude-lane boom" in excinfo.value.reason


@pytest.mark.asyncio
async def test_run_llm_stage_raises_on_empty_reply(monkeypatch):
    from scripts import voice_spar_smoke

    async def empty_call_llm_lane(lane, prompt, *, model, timeout, cwd=None):
        return "   "

    monkeypatch.setattr(voice_spar_smoke, "call_llm_lane", empty_call_llm_lane)

    with pytest.raises(SmokeError) as excinfo:
        await run_llm_stage("haiku")
    assert excinfo.value.stage == "llm"


def test_main_is_importable_and_has_name_main_guard():
    """The script must be importable (no side effects at import time)."""
    from scripts import voice_spar_smoke

    assert callable(voice_spar_smoke.main)
    source = Path(voice_spar_smoke.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source


def test_main_reports_failing_stage_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    from scripts import voice_spar_smoke

    monkeypatch.setattr(
        voice_spar_smoke,
        "load_config",
        lambda: {"voice_web": {"spar": {"whisper_model": "small"}}},
    )

    def failing_synthesize_to_wav(text, *, voice_path, output_path):
        raise RuntimeError("piper boom")

    monkeypatch.setattr(voice_spar_smoke, "synthesize_to_wav", failing_synthesize_to_wav)

    exit_code = voice_spar_smoke.main()

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "stage=tts" in out
    assert "piper boom" in out
    assert "[SILENT]" not in out

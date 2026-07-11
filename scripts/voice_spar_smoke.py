#!/usr/bin/env python3
"""Nightly synthetic Voice-Sparmodus cascade smoke test — no WS/server involved.

Runs the three cascade stages as pure local calls against the real Hermes
config (``load_config()`` / ``voice_web``/``voice_web.spar``), never through
the ``/api/voice/spar`` websocket:

1. Piper synthesizes a German test sentence -> WAV; the WAV header (channel
   count, non-zero frame count) is checked.
2. faster-whisper transcribes that WAV back; a fuzzy match against the test
   sentence's core words proves the round trip actually worked (not just
   "produced some audio").
3. One real ``claude`` subscription-lane turn (haiku, a short prompt, 60s
   timeout) -> a non-empty reply, proving the CLI lane itself is reachable.

Hermes cron convention: on success, prints ``[SILENT] voice-spar-smoke OK
...`` and exits 0 — the ``[SILENT]`` prefix means no Discord delivery for a
routine green run. On ANY failure, prints a concise ``stage: reason`` on
stdout and exits 1 (a cron DOES get notified about that, same convention).

Usage::

    ./venv/bin/python scripts/voice_spar_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import wave
from pathlib import Path

from hermes_cli.config import load_config
from hermes_cli.voice_spar_session import (
    LlmLaneError,
    call_llm_lane,
    synthesize_to_wav,
    transcribe_wav,
)
from hermes_cli.voice_ws import spar_web_config, voice_web_config

TEST_SENTENCE = "Der Server läuft heute Nacht stabil."
# Lowercased core words the STT round trip must recover most of — a fuzzy
# match rather than an exact-string check, since STT casing/punctuation can
# legitimately differ turn to turn.
_CORE_WORDS = ("server", "läuft", "heute", "nacht", "stabil")
_MIN_MATCHED_CORE_WORDS = 3

_LLM_PROMPT = "Antworte in einem kurzen Satz: Wie viel ist 2 plus 2?"
_LLM_TIMEOUT_SECONDS = 60.0
# The smoke test always exercises the claude/haiku lane specifically
# (fastest subscription model, see voice_ws._DEFAULT_SPAR_CLAUDE_MODEL),
# regardless of which lane voice_web.spar.llm_lane is actually configured
# for in production (codex by default) — this proves the claude CLI lane
# itself stays reachable even when it isn't the deployed default.
_DEFAULT_SMOKE_LLM_MODEL = "haiku"


class SmokeError(RuntimeError):
    """One cascade stage failed; carries the stage name + a concise reason."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason


def _check_wav_header(path: Path) -> None:
    """Sanity-check the synthesized WAV: openable, ≥1 channel, non-empty."""
    try:
        with wave.open(str(path), "rb") as wav_file:
            if wav_file.getnchannels() < 1:
                raise SmokeError("tts", f"invalid WAV channel count at {path}")
            if wav_file.getnframes() <= 0:
                raise SmokeError("tts", f"empty WAV (0 frames) at {path}")
    except wave.Error as exc:
        raise SmokeError("tts", f"not a valid WAV file: {exc}") from exc
    except OSError as exc:
        raise SmokeError("tts", f"could not open the synthesized WAV: {exc}") from exc


def run_tts_stage(voice_path: str, output_path: Path) -> None:
    """Stage 1: Piper synthesizes ``TEST_SENTENCE`` to *output_path*."""
    try:
        synthesize_to_wav(TEST_SENTENCE, voice_path=voice_path, output_path=output_path)
    except Exception as exc:
        raise SmokeError("tts", str(exc)) from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise SmokeError("tts", f"Piper produced no (or an empty) WAV at {output_path}")
    _check_wav_header(output_path)


def run_stt_stage(wav_path: Path, *, model_size: str, language: str) -> str:
    """Stage 2: faster-whisper transcribes *wav_path*; fuzzy-matches the test sentence."""
    try:
        transcript = transcribe_wav(str(wav_path), model_size=model_size, language=language)
    except Exception as exc:
        raise SmokeError("stt", str(exc)) from exc
    normalized = transcript.lower()
    matched = [word for word in _CORE_WORDS if word in normalized]
    if len(matched) < _MIN_MATCHED_CORE_WORDS:
        raise SmokeError(
            "stt",
            f"transcript did not fuzzy-match the test sentence: {transcript!r} "
            f"(matched {matched}, need >= {_MIN_MATCHED_CORE_WORDS})",
        )
    return transcript


async def run_llm_stage(model: str) -> str:
    """Stage 3: one real, short claude-lane turn -> a non-empty reply."""
    try:
        reply = await call_llm_lane(
            "claude", _LLM_PROMPT, model=model, timeout=_LLM_TIMEOUT_SECONDS
        )
    except LlmLaneError as exc:
        raise SmokeError("llm", str(exc)) from exc
    if not reply.strip():
        raise SmokeError("llm", "the claude lane returned an empty reply")
    return reply


def main() -> int:
    raw_config = load_config()
    voice_config = voice_web_config(raw_config)
    spar_config = spar_web_config(raw_config)
    llm_model = (
        spar_config.llm_model if spar_config.llm_lane == "claude" else None
    ) or _DEFAULT_SMOKE_LLM_MODEL

    with tempfile.TemporaryDirectory(prefix="voice-spar-smoke-") as tmp_dir:
        wav_path = Path(tmp_dir) / "smoke.wav"
        try:
            run_tts_stage(spar_config.piper_voice_path, wav_path)
            transcript = run_stt_stage(
                wav_path,
                model_size=spar_config.whisper_model,
                language=voice_config.language,
            )
            reply = asyncio.run(run_llm_stage(llm_model))
        except SmokeError as exc:
            print(f"voice-spar-smoke FAILED at stage={exc.stage}: {exc.reason}")
            return 1

    print(
        "[SILENT] voice-spar-smoke OK "
        f"tts=ok stt_transcript={transcript!r} llm_reply={reply[:120]!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

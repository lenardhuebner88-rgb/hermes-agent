"""Standalone voice web routes and the Live-to-cascade bridge."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
import wave
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import psutil

from hermes_cli.config import load_env
from hermes_cli.voice_live_session import (
    DEFAULT_SYSTEM_INSTRUCTION,
    GeminiLiveSession,
    LiveFallbackRequired,
    LiveSessionEnded,
)
from hermes_cli.voice_spar_session import (
    SPAR_SYSTEM_INSTRUCTION,
    LlmLaneError,
    SparLlmLane,
    _load_whisper_model as spar_load_whisper_model,
    create_llm_lane as spar_create_llm_lane,
    run_turn as spar_run_turn,
    synthesize_to_wav as spar_synthesize_to_wav,
    transcribe_wav as spar_transcribe_wav,
)
from hermes_constants import get_hermes_home
from tools.transcription_tools import transcribe_audio
from tools.tts_tool import text_to_speech_tool
from tools.voice_live_tools import FUNCTION_DECLARATIONS, VoiceToolExecutor


DEFAULT_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
VOICE_CLIENT_DIR = Path(__file__).with_name("voice_client")
_HERMES_CLI_ENTRYPOINT = Path(__file__).resolve().parents[1] / "cli.py"
_VOICE_ATTACHMENT_RETENTION_SECONDS = 24 * 60 * 60
_VOICE_ATTACHMENT_CLEANUP_TASKS: set[asyncio.Task[None]] = set()

_ALLOWED_VOICE_ASSETS = {
    "app.js": "application/javascript",
    "icon.svg": "image/svg+xml",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "manifest.json": "application/manifest+json",
    "offline.html": "text/html",
    "sw.js": "application/javascript",
    "worklet.js": "application/javascript",
}
_NO_STORE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}
_SERVICE_WORKER_SCOPE_HEADERS = {"Service-Worker-Allowed": "/voice"}
_MAX_FALLBACK_PCM_BYTES = 16 * 1024 * 1024
_FALLBACK_PREROLL_PCM_BYTES = 60 * 16_000 * 2
_AUDIO_QUEUE_FRAMES = 128
_EVENT_QUEUE_ITEMS = 128
_FALLBACK_END_TIMEOUT_SECONDS = 60.0
_LIVE_END_GRACE_SECONDS = 1.0
_EVENT_DRAIN_TIMEOUT_SECONDS = 10.0
_DELEGATE_TIMEOUT_SECONDS = 120.0
_DELEGATE_LIVE_TIMEOUT_SECONDS = 600.0
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_PROCESS_TERMINATE_GRACE_SECONDS = 1.0
_FALLBACK_CANCEL_TIMEOUT_SECONDS = 7.0
_MAX_TEXT_FRAME_CHARS = 4000
_VIDEO_FRAME_MAGIC = b"\xff\xd8"
_MAX_VIDEO_FRAME_BYTES = 512 * 1024
_VIDEO_FRAME_RATE_LIMIT = 2
_VIDEO_FRAME_RATE_WINDOW_SECONDS = 1.0
_VIDEO_QUEUE_MAXSIZE = 4
_VALID_VIDEO_SOURCES = {"camera", "screen"}
_FFMPEG_TIMEOUT_SECONDS = 60.0
_OUTPUT_PCM_CHUNK_BYTES = 24_000
_RESUMPTION_TTL_SECONDS = 60 * 60
_RESUMPTION_MAX_ENTRIES = 32
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")

_log = logging.getLogger(__name__)

WsAuthReason = Callable[[WebSocket], tuple[str | None, str]]
WsReason = Callable[[WebSocket], str | None]
WsCloseReason = Callable[[str], str]


class ResumptionRegistry:
    """Bound in-memory store for Gemini Live resumption handles.

    A handle must outlive a single websocket so a phone reconnect can resume
    the same Gemini Live conversation instead of starting over. Every
    mutation happens on the event loop thread (nothing else touches this
    registry), so no lock is required today; ``get``/``store`` each perform
    their dict mutation at a single point so a lock could be added there
    trivially if that ever changes.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def get(self, session_id: str) -> str | None:
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        handle, stored_at = entry
        if time.monotonic() - stored_at > _RESUMPTION_TTL_SECONDS:
            del self._entries[session_id]
            return None
        self._entries.move_to_end(session_id)
        return handle

    def store(self, session_id: str, handle: str | None) -> None:
        if handle is None:
            self._entries.pop(session_id, None)
            return
        self._entries[session_id] = (handle, time.monotonic())
        self._entries.move_to_end(session_id)
        while len(self._entries) > _RESUMPTION_MAX_ENTRIES:
            self._entries.popitem(last=False)


_RESUMPTION_REGISTRY = ResumptionRegistry()


_DEFAULT_CONTEXT_TRIGGER_TOKENS = 25_000
_DEFAULT_CONTEXT_TARGET_TOKENS = 10_000
# The documented pre-sprint value (module-level, unconfigurable back then) —
# an operator override may not exceed it, or mandatory compression (the only
# thing keeping a Live session from Google's ~2-minute cap) becomes
# unreachable.
_MAX_CONTEXT_TRIGGER_TOKENS = 100_000
_DEFAULT_SESSION_SOFT_MINUTES = 10.0
_DEFAULT_SESSION_MAX_MINUTES = 15.0
_DEFAULT_SESSION_SOFT_BUDGET_USD = 0.35
_DEFAULT_WATCH_COOLDOWN_SECONDS = 30.0
_DEFAULT_WATCH_MAX_NOTIFICATIONS = 3
_DEFAULT_VIDEO_MODE = "on_demand"
_VALID_VIDEO_MODES = {"stream", "on_demand"}
_DEFAULT_LOOK_MODEL = "gemini-3.1-flash-lite"
_LOOK_CLOSELY_FRAME_WAIT_SECONDS = 3.0
_DETAIL_FRAME_MAX_EDGE = 2048
_DETAIL_FRAME_QUALITY = 0.9

# Proactive memory injection (see _voice_memory_context_block()).
_MEMORY_NOTES_DIR = Path("/home/piet/.memsearch/shared/memory")
_MEMORY_CONTEXT_DAYS = 2
_MEMORY_CONTEXT_CHARS_PER_DAY = 600
_MEMORY_CONTEXT_HEADER = "[Aktueller Arbeitskontext aus Piets letzten Sessions]"

# Spar-Warmup: prespawned persistent claude-lane pool (see prespawn_spar_
# claude_lane()).
_SPAR_LANE_POOL_TTL_SECONDS = 300.0


@dataclass
class VoiceWebConfig:
    enabled: bool = False
    model: str = DEFAULT_LIVE_MODEL
    language: str = "de-DE"
    voice: str = "Puck"
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION
    context_trigger_tokens: int = _DEFAULT_CONTEXT_TRIGGER_TOKENS
    context_target_tokens: int = _DEFAULT_CONTEXT_TARGET_TOKENS
    session_soft_minutes: float = _DEFAULT_SESSION_SOFT_MINUTES
    session_max_minutes: float = _DEFAULT_SESSION_MAX_MINUTES
    session_soft_budget_usd: float | None = _DEFAULT_SESSION_SOFT_BUDGET_USD
    session_hard_budget_usd: float | None = None
    google_search_enabled: bool = False
    watch_cooldown_seconds: float = _DEFAULT_WATCH_COOLDOWN_SECONDS
    watch_max_notifications: int = _DEFAULT_WATCH_MAX_NOTIFICATIONS
    video_mode: str = _DEFAULT_VIDEO_MODE
    look_model: str = _DEFAULT_LOOK_MODEL
    pricing: dict = field(default_factory=dict)
    memory_preload: bool = True


class VoiceRuntimeError(RuntimeError):
    """A safe, structured error suitable for a websocket response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _validated_budget(section: dict, key: str, default: float | None) -> float | None:
    """A budget is a finite, positive float or None; anything else fails safe.

    NaN never fires the hard-budget stop (comparisons against NaN are always
    False); a non-positive or infinite value would end sessions immediately
    or never — both fail safe to ``default`` rather than degrade silently.
    """
    if key not in section:
        return default
    value = section[key]
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        _log.warning("voice_web.%s is invalid (%r); using %r", key, value, default)
        return default
    return float(value)


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

    voice = section.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        voice = "Puck"

    system_instruction = section.get("system_instruction")
    if not isinstance(system_instruction, str):
        system_instruction = DEFAULT_SYSTEM_INSTRUCTION
    else:
        system_instruction = system_instruction.strip()
        if not system_instruction:
            system_instruction = DEFAULT_SYSTEM_INSTRUCTION

    # Context window compression can never be disabled (without it Google
    # caps audio+video Live sessions at ~2 minutes), so an absent/invalid
    # section always yields a usable trigger/target pair.
    context_trigger_tokens = _DEFAULT_CONTEXT_TRIGGER_TOKENS
    context_target_tokens = _DEFAULT_CONTEXT_TARGET_TOKENS
    compression = section.get("context_compression")
    if isinstance(compression, dict) and compression:
        trigger = _positive_int(compression.get("trigger_tokens"))
        target = _positive_int(compression.get("target_tokens"))
        if (
            trigger is not None
            and target is not None
            and target < trigger
            and trigger <= _MAX_CONTEXT_TRIGGER_TOKENS
        ):
            context_trigger_tokens = trigger
            context_target_tokens = target
        else:
            _log.warning(
                "voice_web.context_compression invalid (trigger=%r target=%r); "
                "using %d/%d",
                compression.get("trigger_tokens"),
                compression.get("target_tokens"),
                _DEFAULT_CONTEXT_TRIGGER_TOKENS,
                _DEFAULT_CONTEXT_TARGET_TOKENS,
            )

    session_soft_minutes = _DEFAULT_SESSION_SOFT_MINUTES
    session_max_minutes = _DEFAULT_SESSION_MAX_MINUTES
    soft_minutes = _positive_number(section.get("session_soft_minutes"))
    max_minutes = _positive_number(section.get("session_max_minutes"))
    if (
        soft_minutes is not None
        and max_minutes is not None
        and soft_minutes <= max_minutes
    ):
        session_soft_minutes = soft_minutes
        session_max_minutes = max_minutes
    elif "session_soft_minutes" in section or "session_max_minutes" in section:
        _log.warning(
            "voice_web session duration limits invalid (soft=%r max=%r); "
            "using %s/%s minutes",
            section.get("session_soft_minutes"),
            section.get("session_max_minutes"),
            _DEFAULT_SESSION_SOFT_MINUTES,
            _DEFAULT_SESSION_MAX_MINUTES,
        )

    session_soft_budget_usd = _validated_budget(
        section, "session_soft_budget_usd", _DEFAULT_SESSION_SOFT_BUDGET_USD
    )
    session_hard_budget_usd = _validated_budget(
        section, "session_hard_budget_usd", None
    )

    google_search_enabled = section.get("google_search_enabled") is True

    watch_cooldown_seconds = _DEFAULT_WATCH_COOLDOWN_SECONDS
    watch_max_notifications = _DEFAULT_WATCH_MAX_NOTIFICATIONS
    watch = section.get("watch")
    if isinstance(watch, dict) and watch:
        cooldown = _nonnegative_int(watch.get("cooldown_seconds"))
        max_notifications = _nonnegative_int(watch.get("max_notifications"))
        if cooldown is not None and max_notifications is not None:
            watch_cooldown_seconds = float(cooldown)
            watch_max_notifications = max_notifications
        else:
            _log.warning(
                "voice_web.watch invalid (cooldown_seconds=%r max_notifications=%r); "
                "using %s/%s",
                watch.get("cooldown_seconds"),
                watch.get("max_notifications"),
                _DEFAULT_WATCH_COOLDOWN_SECONDS,
                _DEFAULT_WATCH_MAX_NOTIFICATIONS,
            )

    video_mode = section.get("video_mode")
    if not isinstance(video_mode, str) or video_mode not in _VALID_VIDEO_MODES:
        if "video_mode" in section:
            _log.warning(
                "voice_web.video_mode invalid (%r); using %r",
                section.get("video_mode"),
                _DEFAULT_VIDEO_MODE,
            )
        video_mode = _DEFAULT_VIDEO_MODE

    look_model = section.get("look_model")
    if not isinstance(look_model, str) or not look_model.strip():
        look_model = _DEFAULT_LOOK_MODEL

    pricing = section.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}

    memory_preload = section.get("memory_preload") is not False

    return VoiceWebConfig(
        enabled=section.get("enabled") is True,
        model=model,
        language=language,
        voice=voice,
        system_instruction=system_instruction,
        context_trigger_tokens=context_trigger_tokens,
        context_target_tokens=context_target_tokens,
        session_soft_minutes=session_soft_minutes,
        session_max_minutes=session_max_minutes,
        session_soft_budget_usd=session_soft_budget_usd,
        session_hard_budget_usd=session_hard_budget_usd,
        google_search_enabled=google_search_enabled,
        watch_cooldown_seconds=watch_cooldown_seconds,
        watch_max_notifications=watch_max_notifications,
        video_mode=video_mode,
        look_model=look_model,
        pricing=pricing,
        memory_preload=memory_preload,
    )


_DEFAULT_SPAR_LLM_LANE = "codex"
_VALID_SPAR_LLM_LANES = {"codex", "claude"}
_DEFAULT_SPAR_WHISPER_MODEL = "small"
_DEFAULT_SPAR_MAX_TOOL_HOPS = 2
_DEFAULT_SPAR_LLM_TIMEOUT_SECONDS = 25.0
# The fastest subscription model, chosen so a persistent-child claude-lane
# turn stays inside the walkie-talkie latency budget by default; still
# overridable via voice_web.spar.llm_model.
_DEFAULT_SPAR_CLAUDE_MODEL = "haiku"
_DEFAULT_SPAR_PIPER_VOICE_FILENAME = "de_DE-thorsten-medium.onnx"


@dataclass
class SparWebConfig:
    """Voice-Sparmodus (cascade) settings — see ``voice_spar_session.py``."""

    enabled: bool = True
    llm_lane: str = _DEFAULT_SPAR_LLM_LANE
    llm_model: str | None = None
    whisper_model: str = _DEFAULT_SPAR_WHISPER_MODEL
    piper_voice_path: str = ""
    system_instruction: str = SPAR_SYSTEM_INSTRUCTION
    max_tool_hops: int = _DEFAULT_SPAR_MAX_TOOL_HOPS
    llm_timeout_seconds: float = _DEFAULT_SPAR_LLM_TIMEOUT_SECONDS


def _default_spar_piper_voice_path() -> str:
    return str(
        get_hermes_home() / "voice" / "piper" / _DEFAULT_SPAR_PIPER_VOICE_FILENAME
    )


def spar_web_config(raw: dict) -> SparWebConfig:
    """Read ``voice_web.spar`` — the Sparmodus cascade's own sub-section."""
    voice_section = raw.get("voice_web") if isinstance(raw, dict) else None
    section = (
        voice_section.get("spar")
        if isinstance(voice_section, dict)
        else None
    )
    if not isinstance(section, dict):
        section = {}

    llm_lane = section.get("llm_lane")
    if not isinstance(llm_lane, str) or llm_lane not in _VALID_SPAR_LLM_LANES:
        if "llm_lane" in section:
            _log.warning(
                "voice_web.spar.llm_lane invalid (%r); using %r",
                section.get("llm_lane"),
                _DEFAULT_SPAR_LLM_LANE,
            )
        llm_lane = _DEFAULT_SPAR_LLM_LANE

    llm_model = section.get("llm_model")
    if not isinstance(llm_model, str) or not llm_model.strip():
        llm_model = _DEFAULT_SPAR_CLAUDE_MODEL if llm_lane == "claude" else None

    whisper_model = section.get("whisper_model")
    if not isinstance(whisper_model, str) or not whisper_model.strip():
        whisper_model = _DEFAULT_SPAR_WHISPER_MODEL

    piper_voice_path = section.get("piper_voice_path")
    if not isinstance(piper_voice_path, str) or not piper_voice_path.strip():
        piper_voice_path = _default_spar_piper_voice_path()

    system_instruction = section.get("system_instruction")
    if not isinstance(system_instruction, str) or not system_instruction.strip():
        system_instruction = SPAR_SYSTEM_INSTRUCTION

    max_tool_hops = _nonnegative_int(section.get("max_tool_hops"))
    if max_tool_hops is None:
        if "max_tool_hops" in section:
            _log.warning(
                "voice_web.spar.max_tool_hops invalid (%r); using %d",
                section.get("max_tool_hops"),
                _DEFAULT_SPAR_MAX_TOOL_HOPS,
            )
        max_tool_hops = _DEFAULT_SPAR_MAX_TOOL_HOPS

    llm_timeout_seconds = _positive_number(section.get("llm_timeout_seconds"))
    if llm_timeout_seconds is None:
        if "llm_timeout_seconds" in section:
            _log.warning(
                "voice_web.spar.llm_timeout_seconds invalid (%r); using %s",
                section.get("llm_timeout_seconds"),
                _DEFAULT_SPAR_LLM_TIMEOUT_SECONDS,
            )
        llm_timeout_seconds = _DEFAULT_SPAR_LLM_TIMEOUT_SECONDS

    return SparWebConfig(
        enabled=section.get("enabled") is not False,
        llm_lane=llm_lane,
        llm_model=llm_model,
        whisper_model=whisper_model,
        piper_voice_path=piper_voice_path,
        system_instruction=system_instruction,
        max_tool_hops=max_tool_hops,
        llm_timeout_seconds=llm_timeout_seconds,
    )


def _tail_at_paragraph_boundary(text: str, max_chars: int) -> str:
    """The last ``max_chars`` of *text*, snapped forward past a cut mid-line.

    A hard ``text[-max_chars:]`` slice usually starts mid-sentence; skipping
    to the next newline lands on a paragraph/bullet/header boundary instead
    (a leading ``## ``/``### ``/``- `` line is fine to keep as-is).
    """
    tail = text[-max_chars:]
    newline = tail.find("\n")
    if 0 <= newline < len(tail) - 1:
        tail = tail[newline + 1 :]
    return tail.strip()


def _voice_memory_context_block() -> str:
    """Best-effort "what Piet is working on" excerpt for session-start context.

    Reads the last :data:`_MEMORY_CONTEXT_DAYS` daily memsearch notes
    (``shared/memory/YYYY-MM-DD.md``, one file per day) and takes roughly
    the last :data:`_MEMORY_CONTEXT_CHARS_PER_DAY` characters of each — at a
    paragraph boundary where possible — concatenated oldest-first. No CLI
    call (that would add session-start latency): a direct, best-effort file
    read that returns "" on any error (missing dir, permissions, empty/
    unreadable files), so a memsearch outage never blocks a voice session
    from starting.
    """
    try:
        files = sorted(_MEMORY_NOTES_DIR.glob("*.md"))
    except OSError:
        return ""
    if not files:
        return ""
    chunks: list[str] = []
    for path in files[-_MEMORY_CONTEXT_DAYS:]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        excerpt = _tail_at_paragraph_boundary(text, _MEMORY_CONTEXT_CHARS_PER_DAY)
        if excerpt:
            chunks.append(excerpt)
    if not chunks:
        return ""
    return f"{_MEMORY_CONTEXT_HEADER}\n" + "\n\n".join(chunks)


def resolve_gemini_api_key() -> str:
    """Resolve Gemini credentials server-side without exposing their value."""
    process_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if process_key:
        return process_key
    environment = load_env()
    return str(environment.get("GEMINI_API_KEY") or "").strip()


def _voice_cache_dir() -> Path:
    path = get_hermes_home() / "cache" / "voice-web"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_pcm16_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(pcm)


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    """Retrieve detached task failures after cancellation-safe thread work."""
    try:
        task.result()
    except BaseException:
        pass


async def _run_sync_cancel_safe(function: Callable[..., Any], *args: Any) -> Any:
    """Cancel the await promptly while letting the bounded worker clean up."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_consume_background_task)
        raise


def _transcribe_pcm_sync(pcm: bytes, wav_path: Path) -> dict[str, Any]:
    try:
        _write_pcm16_wav(wav_path, pcm)
        return transcribe_audio(str(wav_path))
    finally:
        wav_path.unlink(missing_ok=True)


async def fallback_transcribe_pcm(pcm: bytes, language: str) -> str:
    """Run Hermes' configured STT adapter against a temporary PCM16 WAV."""
    del language  # The public adapter gets language/provider settings from config.
    wav_path = _voice_cache_dir() / f"input-{uuid.uuid4().hex}.wav"
    result = await _run_sync_cancel_safe(_transcribe_pcm_sync, pcm, wav_path)

    if not isinstance(result, dict) or not result.get("success"):
        detail = result.get("error") if isinstance(result, dict) else None
        raise VoiceRuntimeError(
            "transcription_failed",
            str(detail or "Die Spracheingabe konnte nicht transkribiert werden."),
        )
    transcript = str(result.get("transcript") or "").strip()
    if not transcript:
        raise VoiceRuntimeError(
            "empty_transcript",
            "Die Spracheingabe enthielt keinen erkennbaren Text.",
        )
    return transcript


def _decode_tts_result(raw_result: str) -> Path:
    try:
        result = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VoiceRuntimeError(
            "tts_failed",
            "Die Sprachausgabe lieferte keine gültige Antwort.",
        ) from exc
    if not isinstance(result, dict) or not result.get("success"):
        detail = result.get("error") if isinstance(result, dict) else None
        raise VoiceRuntimeError(
            "tts_failed",
            str(detail or "Die Sprachausgabe ist fehlgeschlagen."),
        )
    file_path = str(result.get("file_path") or "").strip()
    if not file_path:
        raise VoiceRuntimeError(
            "tts_failed",
            "Die Sprachausgabe hat keine Audiodatei erzeugt.",
        )
    return Path(file_path)


def _transcode_to_pcm24k(source_path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VoiceRuntimeError(
            "ffmpeg_unavailable",
            "ffmpeg wird für die Sprachausgabe benötigt.",
        )
    try:
        process = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(source_path),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VoiceRuntimeError(
            "tts_transcode_failed",
            "Die Sprachausgabe konnte nicht in PCM umgewandelt werden.",
        ) from exc
    if process.returncode != 0 or not process.stdout:
        detail = process.stderr.decode("utf-8", "replace").strip()[-500:]
        raise VoiceRuntimeError(
            "tts_transcode_failed",
            detail or "Die Sprachausgabe konnte nicht in PCM umgewandelt werden.",
        )
    return process.stdout


def _unlink_owned_voice_file(path: Path) -> None:
    try:
        path.resolve().relative_to(_voice_cache_dir().resolve())
    except (OSError, ValueError):
        return
    path.unlink(missing_ok=True)


def _synthesize_pcm_sync(text: str, requested_path: Path) -> bytes:
    generated_path = requested_path
    try:
        raw_result = text_to_speech_tool(text, str(requested_path))
        generated_path = _decode_tts_result(raw_result)
        return _transcode_to_pcm24k(generated_path)
    finally:
        _unlink_owned_voice_file(generated_path)
        if generated_path != requested_path:
            _unlink_owned_voice_file(requested_path)


async def fallback_synthesize_pcm(text: str, language: str) -> bytes:
    """Run Hermes' public TTS adapter, then convert its output to PCM16/24k."""
    del language  # Voice/language selection belongs to the existing TTS config.
    requested_path = _voice_cache_dir() / f"output-{uuid.uuid4().hex}.mp3"
    return await _run_sync_cancel_safe(_synthesize_pcm_sync, text, requested_path)


def resolve_hermes_executable() -> str:
    """Resolve Hermes from the active Python environment before consulting PATH."""
    executable_name = "hermes.exe" if os.name == "nt" else "hermes"
    sibling = Path(sys.executable).expanduser().absolute().with_name(executable_name)
    if sibling.is_file() and (os.name == "nt" or os.access(sibling, os.X_OK)):
        return str(sibling)
    discovered = shutil.which(executable_name)
    if discovered:
        return str(Path(discovered).expanduser().absolute())
    raise VoiceRuntimeError(
        "delegation_unavailable",
        "Hermes ist in dieser Laufzeitumgebung nicht verfügbar.",
    )


def _delegation_isolation_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {"start_new_session": True}


def _signal_process_tree(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    """Signal the isolated delegation process group/tree without a shell."""
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        return

    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
        for child in reversed(descendants):
            (child.kill if force else child.terminate)()
    except (OSError, psutil.Error):
        pass
    (process.kill if force else process.terminate)()


def _posix_process_group_exists(pgid: int) -> bool:
    """Probe only the fresh, isolated delegation group created from ``pgid``."""
    if pgid <= 0 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_posix_group_exit(pgid: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _posix_process_group_exists(pgid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _stop_posix_process_group(process: asyncio.subprocess.Process) -> None:
    """Stop every member of the delegation's new session and reap its parent."""
    pgid = process.pid
    if pgid <= 0 or pgid == os.getpgrp():
        raise RuntimeError("refusing to signal a non-isolated process group")

    if _posix_process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    parent_reaped = False
    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=_PROCESS_TERMINATE_GRACE_SECONDS,
        )
        parent_reaped = True
    except TimeoutError:
        pass

    # The direct parent may already be reaped while a TERM-ignoring descendant
    # still owns the isolated group. Probe the group independently before return.
    if _posix_process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if not parent_reaped:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _log.warning("Hermes delegation parent did not exit after group kill")

    if not await _wait_for_posix_group_exit(
        pgid,
        _PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ):
        _log.warning("Hermes delegation process group still exists after kill")


async def _stop_subprocess(process: asyncio.subprocess.Process) -> None:
    """Stop the whole isolated tree, then reap the direct child within bounds."""
    if os.name == "posix":
        await _stop_posix_process_group(process)
        return
    if process.returncode is not None:
        return
    try:
        _signal_process_tree(process, force=False)
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=_PROCESS_TERMINATE_GRACE_SECONDS,
        )
        return
    except TimeoutError:
        if process.returncode is None:
            try:
                _signal_process_tree(process, force=True)
            except (OSError, ProcessLookupError):
                pass
    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _log.warning("Hermes delegation child did not exit after kill")


async def delegate_to_hermes(
    prompt: str,
    *,
    timeout_seconds: float | None = None,
    image: bytes | None = None,
) -> str:
    """Delegate one fallback turn through the supported Hermes CLI surface.

    ``timeout_seconds`` defaults to the cascade budget (``_DELEGATE_TIMEOUT_
    SECONDS``, read dynamically so a test/config override still applies to
    unspecified callers); the Live bridge passes the longer
    ``_DELEGATE_LIVE_TIMEOUT_SECONDS`` explicitly since a NON_BLOCKING
    delegation may legitimately run far longer than one cascade turn.
    """
    effective_timeout = (
        _DELEGATE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    image_path: Path | None = None
    image_cleanup_task: asyncio.Task[None] | None = None
    if image is not None:
        if not image.startswith(_VIDEO_FRAME_MAGIC) or len(image) > _MAX_VIDEO_FRAME_BYTES:
            raise VoiceRuntimeError(
                "delegation_image_invalid",
                "Das geteilte Bild ist ungültig.",
            )
        attachment_dir = get_hermes_home() / "cache" / "voice-web" / "attachments"
        _sweep_voice_attachments(attachment_dir)
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            attachment_dir.chmod(0o700)
            image_path = attachment_dir / f"{uuid.uuid4().hex}.jpg"
            image_path.write_bytes(image)
            image_path.chmod(0o600)
            image_cleanup_task = _schedule_voice_attachment_cleanup(image_path)
        except OSError as exc:
            if image_path is not None:
                image_path.unlink(missing_ok=True)
            raise VoiceRuntimeError(
                "delegation_image_unavailable",
                "Das geteilte Bild konnte nicht für Hermes bereitgestellt werden.",
            ) from exc

    if image_path is None:
        command = [resolve_hermes_executable(), "-z", prompt]
    else:
        command = [
            sys.executable,
            str(_HERMES_CLI_ENTRYPOINT),
            "-q",
            prompt,
            "--image",
            str(image_path),
            "--quiet",
        ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_delegation_isolation_kwargs(),
        )
    except OSError as exc:
        if image_path is not None:
            image_path.unlink(missing_ok=True)
        if image_cleanup_task is not None:
            image_cleanup_task.cancel()
            await asyncio.gather(image_cleanup_task, return_exceptions=True)
        raise VoiceRuntimeError(
            "delegation_unavailable",
            "Hermes konnte nicht gestartet werden.",
        ) from exc

    try:
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.CancelledError:
            await _stop_subprocess(process)
            raise
        except TimeoutError as exc:
            await _stop_subprocess(process)
            raise VoiceRuntimeError(
                "delegation_timeout",
                "Hermes hat nicht rechtzeitig geantwortet.",
            ) from exc
    finally:
        if image_path is not None:
            image_path.unlink(missing_ok=True)
        if image_cleanup_task is not None:
            image_cleanup_task.cancel()
            await asyncio.gather(image_cleanup_task, return_exceptions=True)

    if process.returncode != 0:
        _log.warning(
            "Hermes delegation failed returncode=%s",
            process.returncode,
        )
        raise VoiceRuntimeError(
            "delegation_failed",
            "Hermes konnte die Anfrage nicht bearbeiten.",
        )
    response = stdout.decode("utf-8", "replace").strip()
    if image_path is not None and response.startswith("session_id:"):
        response = response.partition("\n")[2].strip()
    if not response:
        raise VoiceRuntimeError(
            "delegation_empty",
            "Hermes hat keine Antwort geliefert.",
        )
    return response


def _sweep_voice_attachments(
    attachment_dir: Path,
    *,
    now: float | None = None,
) -> None:
    """Delete crash leftovers older than the explicit 24-hour retention bound."""

    if not attachment_dir.is_dir():
        return
    cutoff = (time.time() if now is None else now) - _VOICE_ATTACHMENT_RETENTION_SECONDS
    for path in attachment_dir.glob("*.jpg"):
        try:
            if path.is_file() and path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            _log.warning("Could not remove expired voice attachment %s", path.name)


async def _delete_voice_attachment_after(path: Path, delay: float) -> None:
    """Delete one attachment at its retention deadline while the service runs."""

    try:
        await asyncio.sleep(max(0.0, delay))
        path.unlink(missing_ok=True)
    except asyncio.CancelledError:
        raise
    except OSError:
        _log.warning("Could not remove voice attachment %s at retention deadline", path.name)


def _schedule_voice_attachment_cleanup(
    path: Path,
    *,
    now: float | None = None,
) -> asyncio.Task[None]:
    """Schedule deletion at 24h from mtime and retain the task strongly."""

    current = time.time() if now is None else now
    try:
        remaining = path.stat().st_mtime + _VOICE_ATTACHMENT_RETENTION_SECONDS - current
    except OSError:
        remaining = 0.0
    task = asyncio.create_task(_delete_voice_attachment_after(path, remaining))
    _VOICE_ATTACHMENT_CLEANUP_TASKS.add(task)
    task.add_done_callback(_VOICE_ATTACHMENT_CLEANUP_TASKS.discard)
    return task


@asynccontextmanager
async def _voice_router_lifespan(_app: Any):
    """Re-arm exact retention deadlines for attachments left by a crash.

    Also owns the Spar-Warmup lane pool's shutdown: a prespawned but never-
    consumed claude-lane child must not survive the server process. This is
    a fallback only — the pool's own TTL (``_SPAR_LANE_POOL_TTL_SECONDS``)
    already bounds a leaked child's lifetime even if this lifespan is never
    invoked for some reason.
    """

    attachment_dir = get_hermes_home() / "cache" / "voice-web" / "attachments"
    _sweep_voice_attachments(attachment_dir)
    if attachment_dir.is_dir():
        for path in attachment_dir.glob("*.jpg"):
            if path.is_file():
                _schedule_voice_attachment_cleanup(path)
    try:
        yield
    finally:
        await _discard_spar_lane_pool()


def _html_safe_json(value: str) -> str:
    return (
        json
        .dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _voice_index_response(request: Request, session_token: str) -> HTMLResponse:
    index_path = VOICE_CLIENT_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="Voice client is not built")
    html = index_path.read_text(encoding="utf-8")
    gated = bool(getattr(request.app.state, "auth_required", False))
    bootstrap = f"window.__HERMES_AUTH_REQUIRED__={'true' if gated else 'false'};"
    if not gated:
        bootstrap += (
            f"window.__HERMES_SESSION_TOKEN__={_html_safe_json(session_token)};"
        )
    script = f"<script>{bootstrap}</script>"
    if "</head>" in html:
        html = html.replace("</head>", f"{script}</head>", 1)
    else:
        html = script + html
    return HTMLResponse(html, headers=_NO_STORE_HEADERS)


async def _put_live_audio(
    audio_in: asyncio.Queue[bytes],
    data: bytes,
    fallback_mode: asyncio.Event,
) -> None:
    if fallback_mode.is_set():
        return
    put_task = asyncio.create_task(audio_in.put(data))
    fallback_task = asyncio.create_task(fallback_mode.wait())
    try:
        await asyncio.wait(
            {put_task, fallback_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if fallback_task.done() and not put_task.done():
            put_task.cancel()
    finally:
        for task in (put_task, fallback_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(put_task, fallback_task, return_exceptions=True)


async def _put_live_text(
    text_in: asyncio.Queue[str],
    text: str,
    fallback_mode: asyncio.Event,
) -> bool:
    """Deliver one typed turn to the Live bridge unless fallback wins the race.

    Mirrors :func:`_put_live_audio`'s fallback-aware put, but — unlike a raw
    PCM frame, which is retained in ``fallback_pcm`` either way — a typed
    turn has nowhere else to go, so this reports whether it actually reached
    ``text_in``. The caller must run a turn that didn't make it through the
    fallback single-flight path instead of silently losing it.
    """
    if fallback_mode.is_set():
        return False
    put_task = asyncio.create_task(text_in.put(text))
    fallback_task = asyncio.create_task(fallback_mode.wait())
    try:
        await asyncio.wait(
            {put_task, fallback_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if fallback_task.done() and not put_task.done():
            put_task.cancel()
            return False
        return put_task.done() and not put_task.cancelled()
    finally:
        for task in (put_task, fallback_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(put_task, fallback_task, return_exceptions=True)


async def _put_event(
    events_out: asyncio.Queue[dict[str, Any] | None],
    event: dict[str, Any],
    disconnected: asyncio.Event,
) -> bool:
    if disconnected.is_set():
        return False
    put_task = asyncio.create_task(events_out.put(event))
    disconnect_task = asyncio.create_task(disconnected.wait())
    try:
        await asyncio.wait(
            {put_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task.done() and not put_task.done():
            put_task.cancel()
        return put_task.done() and not put_task.cancelled()
    finally:
        for task in (put_task, disconnect_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(put_task, disconnect_task, return_exceptions=True)


async def _put_event_checkpoint(
    events_out: asyncio.Queue[dict[str, Any] | None],
    event: dict[str, Any],
    disconnected: asyncio.Event,
) -> bool:
    """Queue one event and wait until the sole sender delivered or rejected it."""
    if not await _put_event(events_out, event, disconnected):
        return False
    try:
        await asyncio.wait_for(
            events_out.join(),
            timeout=_EVENT_DRAIN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        disconnected.set()
        return False
    return not disconnected.is_set()


def _discard_queued_response_events(
    events_out: asyncio.Queue[dict[str, Any] | None],
) -> None:
    """Drop queued playback output; the sender's one popped frame may be in flight."""
    retained: list[dict[str, Any] | None] = []
    while True:
        try:
            event = events_out.get_nowait()
        except asyncio.QueueEmpty:
            break
        events_out.task_done()
        should_drop = event is not None and (
            event.get("type") == "audio"
            or (event.get("type") == "transcript" and event.get("role") == "assistant")
            or (
                event.get("type") == "state"
                and event.get("value") in {"speaking", "listening"}
            )
        )
        if not should_drop:
            retained.append(event)
    for event in retained:
        events_out.put_nowait(event)


def _append_fallback_pcm(
    fallback_pcm: bytearray,
    frame: bytes,
    fallback_mode: asyncio.Event,
) -> bool:
    """Retain a rolling Live tail, then enforce the cumulative fallback cap."""
    if len(frame) > _MAX_FALLBACK_PCM_BYTES:
        return False
    if fallback_mode.is_set():
        if len(fallback_pcm) + len(frame) > _MAX_FALLBACK_PCM_BYTES:
            return False
        fallback_pcm.extend(frame)
        return True

    fallback_pcm.extend(frame)
    if len(fallback_pcm) > 2 * _FALLBACK_PREROLL_PCM_BYTES:
        del fallback_pcm[:-_FALLBACK_PREROLL_PCM_BYTES]
    return True


async def _cancel_task_bounded(task: asyncio.Task[Any]) -> None:
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
        return
    task.cancel()
    done, _ = await asyncio.wait(
        {task},
        timeout=_FALLBACK_CANCEL_TIMEOUT_SECONDS,
    )
    if done:
        await asyncio.gather(task, return_exceptions=True)
    else:
        _log.warning("Voice fallback task did not stop within the cleanup bound")
        task.add_done_callback(_consume_background_task)


async def _speak_response(
    response: str,
    config: VoiceWebConfig,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    stop_requested: asyncio.Event,
    *,
    synthesize: Callable[[str, str], "asyncio.Future[bytes]"] | None = None,
) -> None:
    """Emit the assistant transcript, then synthesize and stream its audio.

    Shared tail for both the spoken cascade (:func:`_run_cascade_fallback`)
    and a fallback-mode typed turn (:func:`_run_text_cascade`), so the
    TTS/chunk/stream logic exists exactly once. ``synthesize`` defaults to
    :func:`fallback_synthesize_pcm` (the configured global TTS provider);
    the spar cascade (:func:`_run_spar_cascade`) passes its own local-Piper
    synth instead — same signature (``text, language`` -> PCM16/24k bytes).
    """
    if not await _put_event_checkpoint(
        events_out,
        {"type": "transcript", "role": "assistant", "text": response},
        disconnected,
    ):
        return
    synthesize_fn = synthesize or fallback_synthesize_pcm
    audio = await synthesize_fn(response, config.language)
    if stop_requested.is_set():
        return
    if not audio or len(audio) % 2:
        raise VoiceRuntimeError(
            "invalid_tts_audio",
            "Die Sprachausgabe hat kein gültiges PCM16-Audio erzeugt.",
        )
    if not await _put_event(
        events_out,
        {"type": "state", "value": "speaking"},
        disconnected,
    ):
        return
    for offset in range(0, len(audio), _OUTPUT_PCM_CHUNK_BYTES):
        if stop_requested.is_set():
            return
        if not await _put_event(
            events_out,
            {"type": "audio", "data": audio[offset : offset + _OUTPUT_PCM_CHUNK_BYTES]},
            disconnected,
        ):
            return
    if stop_requested.is_set():
        return
    await _put_event_checkpoint(
        events_out,
        {"type": "state", "value": "listening"},
        disconnected,
    )


async def _run_text_cascade(
    text: str,
    config: VoiceWebConfig,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    stop_requested: asyncio.Event,
) -> None:
    """Run one fallback-mode typed turn: delegate, then speak the reply.

    The caller already emitted the user transcript event before spawning
    this — Gemini never transcribes typed input, so unlike
    :func:`_run_cascade_fallback` there is no STT step here. Runs as a
    detached task (tracked by the caller's ``_FallbackTextTurn``), so a
    failure is reported as an error event here instead of propagating to an
    awaiter — mirroring how the route itself reports a failed
    :func:`_run_controllable_cascade`.
    """
    try:
        if stop_requested.is_set():
            return
        if not await _put_event_checkpoint(
            events_out,
            {"type": "state", "value": "thinking"},
            disconnected,
        ):
            return
        response = await delegate_to_hermes(text)
        if stop_requested.is_set():
            return
        await _speak_response(response, config, events_out, disconnected, stop_requested)
    except VoiceRuntimeError as exc:
        await _put_event(
            events_out,
            {"type": "error", "error": {"code": exc.code, "message": exc.message}},
            disconnected,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Voice text-turn cascade failed")
        await _put_event(
            events_out,
            {
                "type": "error",
                "error": {
                    "code": "fallback_failed",
                    "message": "Der Voice-Fallback ist fehlgeschlagen.",
                },
            },
            disconnected,
        )


@dataclass
class _FallbackTextTurn:
    """Single-flight tracker for a fallback-mode typed turn.

    Fallback mode has no Live API session to run a typed turn through, so
    ``_read_voice_frames`` instead runs it via :func:`_run_text_cascade` as
    a background task — this tracks that task so a second concurrent typed
    turn is rejected (``text_busy``) instead of interleaved, and so an
    ``interrupt`` control frame can stop the in-flight one.
    """

    task: asyncio.Task[None] | None = None
    stop_requested: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


def _validated_text_frame(control: dict[str, Any]) -> str | None:
    """Enforce the ``text`` control-frame contract: non-empty, stripped, ≤4000 chars."""
    raw_text = control.get("text")
    if not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    if not text or len(text) > _MAX_TEXT_FRAME_CHARS:
        return None
    return text


def _decode_video_frame(control: dict[str, Any]) -> bytes | None:
    """Decode+validate a ``video_frame`` control payload; ``None`` means invalid.

    ``source`` is required to be one of the known values (forward-compat for
    future sources is limited to that allowlist, not left wide open) and
    ``data`` must be strict base64 of a JPEG still (checked via its magic
    bytes) — anything else is rejected without ever touching a logger.
    """
    if control.get("source") not in _VALID_VIDEO_SOURCES:
        return None
    raw_data = control.get("data")
    if not isinstance(raw_data, str) or not raw_data:
        return None
    if len(raw_data) > _MAX_VIDEO_FRAME_BYTES * 4 // 3 + 8:
        return None
    try:
        decoded = base64.b64decode(raw_data, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not decoded.startswith(_VIDEO_FRAME_MAGIC):
        return None
    return decoded


def _video_frame_rate_allowed(frame_times: deque[float]) -> bool:
    """Allow at most ``_VIDEO_FRAME_RATE_LIMIT`` frames per rolling second."""
    now = time.monotonic()
    while frame_times and now - frame_times[0] >= _VIDEO_FRAME_RATE_WINDOW_SECONDS:
        frame_times.popleft()
    if len(frame_times) >= _VIDEO_FRAME_RATE_LIMIT:
        return False
    frame_times.append(now)
    return True


def _enqueue_video_frame(video_in: asyncio.Queue[bytes | None], frame: bytes) -> None:
    """Queue one decoded JPEG still, dropping the oldest queued frame if full.

    A stale queued frame is worthless for live vision, so a full queue makes
    room for the newest frame instead of ever blocking or rejecting it.
    """
    while True:
        try:
            video_in.put_nowait(frame)
            return
        except asyncio.QueueFull:
            try:
                video_in.get_nowait()
            except asyncio.QueueEmpty:
                continue


def _enqueue_sharing_stopped(video_in: asyncio.Queue[bytes | None]) -> None:
    """Drop queued stills and put a lifecycle sentinel behind the browser stop."""

    while True:
        try:
            video_in.get_nowait()
        except asyncio.QueueEmpty:
            break
    video_in.put_nowait(None)


class VideoFrameCache:
    """Holds the freshest offered video still without relaying it anywhere.

    Used by ``video_mode: on_demand``: incoming ``video_frame`` control
    messages are cached here in ADDITION to entering the Live session's
    ``video_in`` queue as usual (unconditional on video_mode) — the actual
    gating against inflating the per-turn Gemini prompt with a continuous
    1fps stream happens downstream at the relay
    (``_VideoFrameRelay.forward_to_live``), not by withholding frames here.
    This cache remains for orientation/watch/delegation compatibility.
    ``look_closely`` deliberately bypasses it and uses :class:`FreshFrameBroker`
    so a detail request can never consume a stale 1-fps still.
    """

    def __init__(self) -> None:
        self._frame: bytes | None = None
        self._updated = asyncio.Event()

    def store(self, frame: bytes) -> None:
        self._frame = frame
        self._updated.set()

    def peek(self) -> bytes | None:
        return self._frame

    def clear(self) -> None:
        self._frame = None

    async def wait_for_update(self, timeout: float) -> bytes | None:
        """Wait for the next :meth:`store`, else return whatever is cached."""

        self._updated.clear()
        try:
            await asyncio.wait_for(self._updated.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return self._frame


class FreshFrameBroker:
    """Correlate one bounded, server-requested detail capture at a time.

    The broker intentionally retains no completed frame. Concurrent
    ``look_closely`` calls coalesce onto the same in-flight capture; once the
    waiter completes (or times out), the JPEG is released.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._request_id: str | None = None
        self._future: asyncio.Future[bytes | None] | None = None
        self._waiters = 0

    async def request(
        self,
        events_out: asyncio.Queue[dict[str, Any] | None],
        disconnected: asyncio.Event,
        *,
        timeout: float = _LOOK_CLOSELY_FRAME_WAIT_SECONDS,
    ) -> bytes | None:
        async with self._lock:
            if self._future is not None and not self._future.done():
                future = self._future
            else:
                request_id = uuid.uuid4().hex
                future = asyncio.get_running_loop().create_future()
                self._request_id = request_id
                self._future = future
                await _put_event(
                    events_out,
                    {
                        "type": "detail_frame_request",
                        "request_id": request_id,
                        "max_edge": _DETAIL_FRAME_MAX_EDGE,
                        "quality": _DETAIL_FRAME_QUALITY,
                    },
                    disconnected,
                )
            self._waiters += 1
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            await _put_event(
                events_out,
                {"type": "detail_frame_timeout", "request_id": self._request_id},
                disconnected,
            )
            return None
        finally:
            async with self._lock:
                self._waiters = max(0, self._waiters - 1)
                if self._future is future and (future.done() or self._waiters == 0):
                    self._request_id = None
                    self._future = None

    def submit(self, request_id: object, frame: bytes | None) -> bool:
        if (
            not isinstance(request_id, str)
            or request_id != self._request_id
            or self._future is None
            or self._future.done()
        ):
            return False
        self._future.set_result(frame)
        return True

    def accepts(self, request_id: object) -> bool:
        return (
            isinstance(request_id, str)
            and request_id == self._request_id
            and self._future is not None
            and not self._future.done()
        )

    def cancel(self) -> None:
        future = self._future
        self._request_id = None
        self._future = None
        self._waiters = 0
        if future is not None and not future.done():
            future.set_result(None)

async def _try_start_fallback_text_turn(
    text: str,
    config: VoiceWebConfig,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    text_turn: _FallbackTextTurn,
    *,
    emit_transcript: bool,
) -> None:
    """Single-flight fallback typed turn: reject if busy, else run it.

    ``emit_transcript`` is False only for the live-mode-to-fallback
    fall-through in ``_read_voice_frames`` (fallback flipped mid-``_put_
    live_text`` wait): that caller already put the user transcript event on
    ``events_out`` itself, and that path can never collide with an
    already-running turn since no typed turn could have started while the
    connection was still Live.
    """
    if emit_transcript:
        if text_turn.running:
            await _put_event(
                events_out,
                {
                    "type": "error",
                    "error": {
                        "code": "text_busy",
                        "message": "Eine Anfrage läuft bereits. Bitte warte kurz.",
                    },
                },
                disconnected,
            )
            return
        await _put_event(
            events_out,
            {"type": "transcript", "role": "user", "text": text},
            disconnected,
        )
    stop_requested = asyncio.Event()
    text_turn.stop_requested = stop_requested
    text_turn.task = asyncio.create_task(
        _run_text_cascade(text, config, events_out, disconnected, stop_requested)
    )


async def _read_voice_frames(
    websocket: WebSocket,
    audio_in: asyncio.Queue[bytes],
    fallback_pcm: bytearray,
    events_out: asyncio.Queue[dict[str, Any] | None],
    fallback_mode: asyncio.Event,
    disconnected: asyncio.Event,
    text_in: asyncio.Queue[str],
    video_in: asyncio.Queue[bytes | None],
    config: VoiceWebConfig,
    text_turn: _FallbackTextTurn,
    frame_cache: VideoFrameCache,
    fresh_frames: FreshFrameBroker | None = None,
) -> str:
    video_frame_times: deque[float] = deque()
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                disconnected.set()
                return "disconnect"

            frame = message.get("bytes")
            if frame is not None:
                if len(frame) % 2:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_pcm_frame",
                                "message": "PCM16-Frames müssen eine gerade Bytezahl haben.",
                            },
                        },
                        disconnected,
                    )
                    return "error"
                if not _append_fallback_pcm(fallback_pcm, frame, fallback_mode):
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "audio_too_large",
                                "message": "Die Spracheingabe überschreitet das Größenlimit.",
                            },
                        },
                        disconnected,
                    )
                    return "error"
                await _put_live_audio(audio_in, frame, fallback_mode)
                continue

            text = message.get("text")
            if not isinstance(text, str):
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await _put_event(
                    events_out,
                    {
                        "type": "error",
                        "error": {
                            "code": "invalid_control_frame",
                            "message": "Die Steuerungsnachricht ist kein gültiges JSON.",
                        },
                    },
                    disconnected,
                )
                continue
            control_type = control.get("type") if isinstance(control, dict) else None
            if control_type == "end":
                return "end"
            if control_type == "interrupt":
                # Gemini continues to receive microphone PCM and performs native
                # automatic VAD/barge-in.  The explicit browser control only
                # flushes queued server audio and tells the client to stop local
                # playback; the SDK exposes no supported activity_start hook here.
                if text_turn.running:
                    assert text_turn.stop_requested is not None
                    text_turn.stop_requested.set()
                    await _cancel_task_bounded(text_turn.task)
                _discard_queued_response_events(events_out)
                await _put_event(events_out, {"type": "interrupted"}, disconnected)
                continue
            if control_type == "text":
                validated_text = _validated_text_frame(control)
                if validated_text is None:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_text_frame",
                                "message": (
                                    "Die Textnachricht ist leer oder länger als "
                                    "4000 Zeichen."
                                ),
                            },
                        },
                        disconnected,
                    )
                    continue
                if not fallback_mode.is_set():
                    await _put_event(
                        events_out,
                        {"type": "transcript", "role": "user", "text": validated_text},
                        disconnected,
                    )
                    if await _put_live_text(text_in, validated_text, fallback_mode):
                        continue
                    await _try_start_fallback_text_turn(
                        validated_text,
                        config,
                        events_out,
                        disconnected,
                        text_turn,
                        emit_transcript=False,
                    )
                    continue
                await _try_start_fallback_text_turn(
                    validated_text,
                    config,
                    events_out,
                    disconnected,
                    text_turn,
                    emit_transcript=True,
                )
                continue
            if control_type == "video_frame":
                if fallback_mode.is_set():
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "video_unavailable_fallback",
                                "message": (
                                    "Sehen ist im Fallback-Modus nicht verfügbar."
                                ),
                            },
                        },
                        disconnected,
                    )
                    continue
                # Rate limit FIRST: the base64 decode below is the expensive
                # step, so an over-rate sender must not get to run it (every
                # video_frame message consumes a rate slot, valid or not).
                if not _video_frame_rate_allowed(video_frame_times):
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "video_rate_limited",
                                "message": (
                                    "Zu viele Video-Frames. Bitte langsamer senden."
                                ),
                            },
                        },
                        disconnected,
                    )
                    continue
                decoded_frame = _decode_video_frame(control)
                if decoded_frame is None:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_video_frame",
                                "message": "Das Video-Frame ist ungültig.",
                            },
                        },
                        disconnected,
                    )
                    continue
                if len(decoded_frame) > _MAX_VIDEO_FRAME_BYTES:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "video_frame_too_large",
                                "message": (
                                    "Das Video-Frame überschreitet das Größenlimit."
                                ),
                            },
                        },
                        disconnected,
                    )
                    continue
                # Always cache the freshest still for look_closely AND enqueue
                # it into the Live session's video_in queue (today's
                # behavior, unconditional on video_mode) — gating whether
                # on_demand actually forwards a still into the upstream Live
                # connection happens downstream at the relay
                # (_VideoFrameRelay.forward_to_live), not here.
                frame_cache.store(decoded_frame)
                _enqueue_video_frame(video_in, decoded_frame)
                continue
            if control_type == "detail_frame":
                if fresh_frames is None or not fresh_frames.accepts(control.get("request_id")):
                    continue
                if fallback_mode.is_set():
                    fresh_frames.cancel()
                    continue
                if not _video_frame_rate_allowed(video_frame_times):
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "video_rate_limited",
                                "message": "Zu viele Video-Frames. Bitte langsamer senden.",
                            },
                        },
                        disconnected,
                    )
                    continue
                decoded_frame = _decode_video_frame(control)
                if decoded_frame is None or len(decoded_frame) > _MAX_VIDEO_FRAME_BYTES:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_detail_frame",
                                "message": "Das Detailbild ist ungültig.",
                            },
                        },
                        disconnected,
                    )
                    continue
                if fresh_frames is not None:
                    fresh_frames.submit(control.get("request_id"), decoded_frame)
                continue
            if control_type == "detail_frame_unavailable":
                if fresh_frames is not None and fresh_frames.accepts(control.get("request_id")):
                    fresh_frames.submit(control.get("request_id"), None)
                continue
            if control_type == "sharing_stopped":
                _enqueue_sharing_stopped(video_in)
                frame_cache.clear()
                if fresh_frames is not None:
                    fresh_frames.cancel()
                continue
            await _put_event(
                events_out,
                {
                    "type": "error",
                    "error": {
                        "code": "unknown_control_frame",
                        "message": "Unbekannte Voice-Steuerungsnachricht.",
                    },
                },
                disconnected,
            )
    except WebSocketDisconnect:
        disconnected.set()
        return "disconnect"
    finally:
        if fresh_frames is not None:
            fresh_frames.cancel()


async def _monitor_post_end_controls(
    websocket: WebSocket,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    stop_requested: asyncio.Event,
) -> str:
    """Own websocket.receive after the initial reader returns on ``end``."""
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                disconnected.set()
                stop_requested.set()
                return "disconnect"

            control_type = None
            error_code = "unknown_control_frame"
            error_message = "Unbekannte Voice-Steuerungsnachricht."
            if message.get("bytes") is not None:
                error_code = "audio_after_end"
                error_message = "Nach end werden nur Steuerungsnachrichten akzeptiert."
            else:
                text = message.get("text")
                if isinstance(text, str):
                    try:
                        control = json.loads(text)
                    except json.JSONDecodeError:
                        error_code = "invalid_control_frame"
                        error_message = (
                            "Die Steuerungsnachricht ist kein gültiges JSON."
                        )
                    else:
                        control_type = (
                            control.get("type") if isinstance(control, dict) else None
                        )

            if control_type == "interrupt":
                stop_requested.set()
                _discard_queued_response_events(events_out)
                if await _put_event_checkpoint(
                    events_out,
                    {"type": "interrupted"},
                    disconnected,
                ):
                    return "interrupt"
                return "disconnect"

            if not await _put_event(
                events_out,
                {
                    "type": "error",
                    "error": {"code": error_code, "message": error_message},
                },
                disconnected,
            ):
                stop_requested.set()
                return "disconnect"
    except (OSError, RuntimeError, WebSocketDisconnect):
        disconnected.set()
        stop_requested.set()
        return "disconnect"


async def _send_voice_events(
    websocket: WebSocket,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
) -> None:
    while not disconnected.is_set():
        event = await events_out.get()
        try:
            if event is None:
                return
            if event.get("type") == "audio":
                data = event.get("data")
                if isinstance(data, bytes) and data:
                    await websocket.send_bytes(data)
            else:
                await websocket.send_json(event)
        except (WebSocketDisconnect, OSError, RuntimeError):
            disconnected.set()
            return
        finally:
            events_out.task_done()


async def _run_cascade_fallback(
    fallback_pcm: bytes,
    config: VoiceWebConfig,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    stop_requested: asyncio.Event,
) -> None:
    if stop_requested.is_set():
        return
    if not fallback_pcm:
        raise VoiceRuntimeError("no_audio", "Es wurde kein Audio empfangen.")
    if not await _put_event_checkpoint(
        events_out,
        {"type": "state", "value": "thinking"},
        disconnected,
    ):
        return
    transcript = await fallback_transcribe_pcm(fallback_pcm, config.language)
    if stop_requested.is_set():
        return
    if not await _put_event_checkpoint(
        events_out,
        {"type": "transcript", "text": transcript},
        disconnected,
    ):
        return
    response = await delegate_to_hermes(transcript)
    if stop_requested.is_set():
        return
    await _speak_response(response, config, events_out, disconnected, stop_requested)


async def _run_controllable_cascade(
    websocket: WebSocket,
    fallback_pcm: bytes,
    config: VoiceWebConfig,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
) -> str:
    """Run fallback while a sole post-end receiver remains cancellable."""
    stop_requested = asyncio.Event()
    fallback_task = asyncio.create_task(
        _run_cascade_fallback(
            fallback_pcm,
            config,
            events_out,
            disconnected,
            stop_requested,
        )
    )
    monitor_task = asyncio.create_task(
        _monitor_post_end_controls(
            websocket,
            events_out,
            disconnected,
            stop_requested,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {fallback_task, monitor_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if monitor_task in done:
            outcome = monitor_task.result()
            stop_requested.set()
            await _cancel_task_bounded(fallback_task)
            return outcome
        await fallback_task
        return "complete"
    finally:
        stop_requested.set()
        await _cancel_task_bounded(monitor_task)
        await _cancel_task_bounded(fallback_task)


# =============================================================================
# Voice Sparmodus (cascade, $0 marginal cost) — additive, own websocket route.
# Never touches the Gemini Live bridge above; only ``_speak_response``'s new
# optional ``synthesize`` parameter is shared.
# =============================================================================

_MAX_SPAR_TURN_PCM_BYTES = _MAX_FALLBACK_PCM_BYTES


def _spar_transcribe_pcm_sync(pcm: bytes, wav_path: Path, model_size: str, language: str) -> str:
    try:
        _write_pcm16_wav(wav_path, pcm)
        return spar_transcribe_wav(str(wav_path), model_size=model_size, language=language)
    finally:
        wav_path.unlink(missing_ok=True)


async def _spar_transcribe_pcm(pcm: bytes, model_size: str, language: str) -> str:
    wav_path = _voice_cache_dir() / f"spar-input-{uuid.uuid4().hex}.wav"
    try:
        transcript = await _run_sync_cancel_safe(
            _spar_transcribe_pcm_sync, pcm, wav_path, model_size, language
        )
    except Exception as exc:
        raise VoiceRuntimeError(
            "transcription_failed",
            "Die Spracheingabe konnte nicht transkribiert werden.",
        ) from exc
    transcript = (transcript or "").strip()
    if not transcript:
        raise VoiceRuntimeError(
            "empty_transcript",
            "Die Spracheingabe enthielt keinen erkennbaren Text.",
        )
    return transcript


def _spar_synthesize_pcm_sync(text: str, voice_path: str) -> bytes:
    wav_path = _voice_cache_dir() / f"spar-output-{uuid.uuid4().hex}.wav"
    try:
        spar_synthesize_to_wav(text, voice_path=voice_path, output_path=wav_path)
        return _transcode_to_pcm24k(wav_path)
    finally:
        _unlink_owned_voice_file(wav_path)


async def _spar_synthesize_pcm_factory(
    voice_path: str,
) -> Callable[[str, str], Any]:
    async def _synthesize(text: str, _language: str) -> bytes:
        try:
            return await _run_sync_cancel_safe(_spar_synthesize_pcm_sync, text, voice_path)
        except VoiceRuntimeError:
            raise
        except Exception as exc:
            raise VoiceRuntimeError(
                "tts_failed", "Die Sprachausgabe ist fehlgeschlagen."
            ) from exc

    return _synthesize


async def _read_spar_frames(
    websocket: WebSocket,
    turn_pcm: bytearray,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    frame_cache: VideoFrameCache,
    fresh_frames: FreshFrameBroker | None = None,
) -> str:
    """Read one Sparmodus turn: accumulate PCM until ``turn_end``/``end``.

    No barge-in, no live streaming — the walkie-talkie contract is
    deliberate (see the module docstring in ``voice_spar_session``); this
    loop only ever returns after a full client-decided turn boundary.
    """
    video_frame_times: deque[float] = deque()
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                disconnected.set()
                return "disconnect"

            frame = message.get("bytes")
            if frame is not None:
                if len(frame) % 2:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_pcm_frame",
                                "message": "PCM16-Frames müssen eine gerade Bytezahl haben.",
                            },
                        },
                        disconnected,
                    )
                    return "error"
                if len(turn_pcm) + len(frame) > _MAX_SPAR_TURN_PCM_BYTES:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "audio_too_large",
                                "message": "Die Spracheingabe überschreitet das Größenlimit.",
                            },
                        },
                        disconnected,
                    )
                    return "error"
                turn_pcm.extend(frame)
                continue

            text = message.get("text")
            if not isinstance(text, str):
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await _put_event(
                    events_out,
                    {
                        "type": "error",
                        "error": {
                            "code": "invalid_control_frame",
                            "message": "Die Steuerungsnachricht ist kein gültiges JSON.",
                        },
                    },
                    disconnected,
                )
                continue
            control_type = control.get("type") if isinstance(control, dict) else None
            if control_type == "end":
                return "end"
            if control_type == "turn_end":
                return "turn_end"
            if control_type == "video_frame":
                if not _video_frame_rate_allowed(video_frame_times):
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "video_rate_limited",
                                "message": (
                                    "Zu viele Video-Frames. Bitte langsamer senden."
                                ),
                            },
                        },
                        disconnected,
                    )
                    continue
                decoded_frame = _decode_video_frame(control)
                if decoded_frame is None or len(decoded_frame) > _MAX_VIDEO_FRAME_BYTES:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_video_frame",
                                "message": "Das Video-Frame ist ungültig.",
                            },
                        },
                        disconnected,
                    )
                    continue
                frame_cache.store(decoded_frame)
                continue
            if control_type == "detail_frame":
                if fresh_frames is None or not fresh_frames.accepts(control.get("request_id")):
                    continue
                if not _video_frame_rate_allowed(video_frame_times):
                    continue
                decoded_frame = _decode_video_frame(control)
                if decoded_frame is not None and len(decoded_frame) <= _MAX_VIDEO_FRAME_BYTES:
                    if fresh_frames is not None:
                        fresh_frames.submit(control.get("request_id"), decoded_frame)
                continue
            if control_type == "detail_frame_unavailable":
                if fresh_frames is not None and fresh_frames.accepts(control.get("request_id")):
                    fresh_frames.submit(control.get("request_id"), None)
                continue
            if control_type == "sharing_stopped":
                frame_cache.clear()
                if fresh_frames is not None:
                    fresh_frames.cancel()
                continue
            await _put_event(
                events_out,
                {
                    "type": "error",
                    "error": {
                        "code": "unknown_control_frame",
                        "message": "Unbekannte Voice-Steuerungsnachricht.",
                    },
                },
                disconnected,
            )
    except WebSocketDisconnect:
        disconnected.set()
        return "disconnect"
    finally:
        if fresh_frames is not None:
            fresh_frames.cancel()


@dataclass
class _PooledSparLane:
    """One prespawned, idle claude-lane child waiting for its first session."""

    lane: SparLlmLane
    model: str | None
    system_instruction: str
    created_at: float


_SPAR_LANE_POOL: _PooledSparLane | None = None
# Guards every read-modify-write of _SPAR_LANE_POOL so two concurrent warmup
# calls (or a warmup racing a session start) can never spawn/consume the pool
# entry twice. Nothing here awaits anything that yields back into this same
# critical section, so plain mutual exclusion is enough.
_SPAR_LANE_POOL_LOCK = asyncio.Lock()


def _spar_lane_pool_expired(entry: _PooledSparLane, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    return current - entry.created_at > _SPAR_LANE_POOL_TTL_SECONDS


async def _discard_pooled_lane(entry: _PooledSparLane) -> None:
    try:
        await entry.lane.aclose()
    except Exception:
        _log.warning("Failed to close a prespawned Sparmodus lane", exc_info=True)


def spar_effective_system_instruction(
    spar_config: SparWebConfig, voice_config: VoiceWebConfig
) -> str:
    """The Sparmodus system instruction, with the memory-context suffix if enabled.

    Shared by the real session start (``voice_spar``) and the warmup
    prespawn (:func:`prespawn_spar_claude_lane`) so a pooled lane's spawn-
    time ``--system-prompt`` matches what a real session would build — a
    mismatch just means the pool entry gets discarded as stale, never a
    wrong-context reply (see :func:`_take_pooled_spar_lane`).
    """
    if not voice_config.memory_preload:
        return spar_config.system_instruction
    suffix = _voice_memory_context_block()
    if not suffix:
        return spar_config.system_instruction
    return f"{spar_config.system_instruction}\n\n{suffix}"


async def prespawn_spar_claude_lane(
    spar_config: SparWebConfig, voice_config: VoiceWebConfig
) -> None:
    """Best-effort, idempotent prespawn of one persistent claude-lane child.

    Consumed by the next Sparmodus session start (see
    :func:`_take_pooled_spar_lane`) so the ~5s CLI startup overlaps with this
    warmup call instead of the first turn's latency budget. A no-op unless
    Sparmodus is enabled and configured for the claude lane (the codex lane
    is already stateless-per-turn, nothing to prespawn). The lock plus the
    "already warm" check make concurrent/duplicate warmup calls spawn at
    most one child; an expired entry is retired before a fresh one replaces
    it, so the pool never holds more than one live child at a time.
    """
    global _SPAR_LANE_POOL
    if not spar_config.enabled or spar_config.llm_lane != "claude":
        return
    async with _SPAR_LANE_POOL_LOCK:
        existing = _SPAR_LANE_POOL
        if existing is not None and not _spar_lane_pool_expired(existing):
            return  # already warm
        if existing is not None:
            _SPAR_LANE_POOL = None
            await _discard_pooled_lane(existing)
        system_instruction = spar_effective_system_instruction(spar_config, voice_config)
        lane = spar_create_llm_lane(
            "claude",
            model=spar_config.llm_model,
            timeout=spar_config.llm_timeout_seconds,
            system_instruction=system_instruction,
        )
        try:
            await lane.start()
        except LlmLaneError:
            _log.warning("Sparmodus claude-lane prespawn failed", exc_info=True)
            return
        _SPAR_LANE_POOL = _PooledSparLane(
            lane=lane,
            model=spar_config.llm_model,
            system_instruction=system_instruction,
            created_at=time.monotonic(),
        )


async def _take_pooled_spar_lane(
    spar_config: SparWebConfig, voice_config: VoiceWebConfig
) -> SparLlmLane | None:
    """Consume the pooled lane for a new session if it fits and is still fresh.

    Returns ``None`` (the caller then spawns its own lane as before) when
    there is no pool entry, it expired, or its spawn-time config no longer
    matches this session's — never raises, so a mismatch just forfeits the
    warmup instead of breaking session start.
    """
    global _SPAR_LANE_POOL
    async with _SPAR_LANE_POOL_LOCK:
        entry = _SPAR_LANE_POOL
        if entry is None:
            return None
        _SPAR_LANE_POOL = None
        system_instruction = spar_effective_system_instruction(spar_config, voice_config)
        if (
            _spar_lane_pool_expired(entry)
            or entry.model != spar_config.llm_model
            or entry.system_instruction != system_instruction
        ):
            await _discard_pooled_lane(entry)
            return None
        return entry.lane


async def _discard_spar_lane_pool() -> None:
    """Best-effort pool teardown, called from the router lifespan on shutdown."""
    global _SPAR_LANE_POOL
    async with _SPAR_LANE_POOL_LOCK:
        entry = _SPAR_LANE_POOL
        _SPAR_LANE_POOL = None
    if entry is not None:
        await _discard_pooled_lane(entry)


async def _warm_whisper_model(model_size: str) -> None:
    """Best-effort: load+cache the Sparmodus whisper model off the event loop.

    ``_load_whisper_model`` already caches per size (module-level dict in
    voice_spar_session.py) — this just pays that load cost during warmup
    instead of on the first real turn's STT step.
    """
    try:
        await asyncio.to_thread(spar_load_whisper_model, model_size)
    except Exception:
        _log.warning("Whisper warmup failed for model=%s", model_size, exc_info=True)


async def _run_spar_cascade(
    pcm: bytes,
    spar_config: SparWebConfig,
    voice_config: VoiceWebConfig,
    events_out: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
    executor: VoiceToolExecutor,
    history: list[tuple[str, str]],
    lane: SparLlmLane,
) -> list[tuple[str, str]]:
    """One full Sparmodus turn: STT -> LLM (+tool hops) -> TTS. Returns new history."""
    if not pcm:
        raise VoiceRuntimeError("no_audio", "Es wurde kein Audio empfangen.")
    if not await _put_event_checkpoint(
        events_out,
        {"type": "state", "value": "thinking"},
        disconnected,
    ):
        return history
    transcript = await _spar_transcribe_pcm(
        pcm, spar_config.whisper_model, voice_config.language
    )
    if not await _put_event_checkpoint(
        events_out,
        {"type": "transcript", "role": "user", "text": transcript},
        disconnected,
    ):
        return history
    try:
        reply, history = await spar_run_turn(
            transcript,
            history=history,
            lane=lane,
            executor=executor,
            max_tool_hops=spar_config.max_tool_hops,
        )
    except LlmLaneError as exc:
        raise VoiceRuntimeError("llm_lane_failed", str(exc)) from exc
    synthesize = await _spar_synthesize_pcm_factory(spar_config.piper_voice_path)
    await _speak_response(
        reply,
        voice_config,
        events_out,
        disconnected,
        asyncio.Event(),  # spar has no interrupt/barge-in
        synthesize=synthesize,
    )
    await _put_event(
        events_out,
        {
            "type": "usage_update",
            "mode": "spar",
            "estimated_usd": 0,
            "label": "$0 (Abo)",
            "complete": True,
        },
        disconnected,
    )
    return history


async def _run_live_bridge(
    config: VoiceWebConfig,
    api_key: str,
    audio_in: asyncio.Queue[bytes],
    events_out: asyncio.Queue[dict[str, Any] | None],
    fallback_mode: asyncio.Event,
    disconnected: asyncio.Event,
    session_id: str | None,
    text_in: asyncio.Queue[str] | None = None,
    video_in: asyncio.Queue[bytes | None] | None = None,
    forced_end: asyncio.Event | None = None,
    frame_cache: VideoFrameCache | None = None,
    fresh_frames: FreshFrameBroker | None = None,
) -> bool:
    live_kwargs: dict[str, Any] = {
        "voice": config.voice,
        "system_instruction": config.system_instruction,
        "context_suffix": _voice_memory_context_block() if config.memory_preload else "",
        "context_trigger_tokens": config.context_trigger_tokens,
        "context_target_tokens": config.context_target_tokens,
        "google_search_enabled": config.google_search_enabled,
        "watch_cooldown_seconds": config.watch_cooldown_seconds,
        "watch_max_notifications": config.watch_max_notifications,
        "session_soft_minutes": config.session_soft_minutes,
        "session_max_minutes": config.session_max_minutes,
        "session_soft_budget_usd": config.session_soft_budget_usd,
        "session_hard_budget_usd": config.session_hard_budget_usd,
        "look_model": config.look_model,
        "video_mode": config.video_mode,
        "pricing": config.pricing,
    }
    if session_id:
        live_kwargs["initial_handle"] = _RESUMPTION_REGISTRY.get(session_id)
        live_kwargs["on_handle_update"] = lambda handle: _RESUMPTION_REGISTRY.store(
            session_id, handle
        )
    session = GeminiLiveSession(
        config.model,
        config.language,
        FUNCTION_DECLARATIONS,
        api_key,
        **live_kwargs,
    )

    async def _request_fresh_frame() -> bytes | None:
        """Request one new, correlated high-resolution capture from the client."""
        if fresh_frames is None:
            return None
        return await fresh_frames.request(events_out, disconnected)

    executor = VoiceToolExecutor(
        delegate=lambda prompt: delegate_to_hermes(
            prompt, timeout_seconds=_DELEGATE_LIVE_TIMEOUT_SECONDS
        ),
        delegate_with_image=lambda prompt, image: delegate_to_hermes(
            prompt,
            timeout_seconds=_DELEGATE_LIVE_TIMEOUT_SECONDS,
            image=image,
        ),
        watch_view=getattr(session, "watch_view", None),
        stop_watching=getattr(session, "stop_watching", None),
        request_frame=_request_fresh_frame,
        look_model=config.look_model,
        gemini_api_key=api_key,
        report_look_usage=getattr(session, "record_look_closely_usage", None),
    )
    try:
        await session.run(
            audio_in, events_out, executor, text_in=text_in, video_in=video_in
        )
    except LiveFallbackRequired:
        if fresh_frames is not None:
            fresh_frames.cancel()
        fallback_mode.set()
        await _put_event(
            events_out,
            {"type": "mode", "value": "fallback", "video_mode": config.video_mode},
            disconnected,
        )
        return True
    except LiveSessionEnded:
        # Server-initiated graceful stop (max duration/hard budget): the
        # session already put its own "session_ended" event on the queue.
        # No fallback, and the caller must close the client socket without
        # waiting out the usual client-initiated "end" handshake.
        if forced_end is not None:
            forced_end.set()
        return False
    except asyncio.CancelledError:
        raise
    raise RuntimeError("Gemini Live bridge exited without a fallback signal")


async def _finish_event_sender(
    events_out: asyncio.Queue[dict[str, Any] | None],
    sender_task: asyncio.Task[None],
    disconnected: asyncio.Event,
) -> None:
    if not disconnected.is_set():
        try:
            await asyncio.wait_for(
                events_out.join(),
                timeout=_EVENT_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            disconnected.set()
    if not sender_task.done():
        if disconnected.is_set():
            sender_task.cancel()
        else:
            await events_out.put(None)
    try:
        await asyncio.wait_for(
            asyncio.shield(sender_task),
            timeout=_EVENT_DRAIN_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.CancelledError):
        sender_task.cancel()
    await asyncio.gather(sender_task, return_exceptions=True)


def create_voice_router(
    raw_config: dict,
    *,
    ws_auth_reason: WsAuthReason,
    ws_host_origin_reason: WsReason,
    ws_client_reason: WsReason,
    ws_close_reason: WsCloseReason,
    session_token: str,
) -> APIRouter:
    """Build an optionally empty router with dashboard auth injected."""

    _sweep_voice_attachments(
        get_hermes_home() / "cache" / "voice-web" / "attachments"
    )
    config = voice_web_config(raw_config)
    if not config.enabled:
        return APIRouter(lifespan=_voice_router_lifespan)
    spar_config = spar_web_config(raw_config)
    router = APIRouter(lifespan=_voice_router_lifespan)

    @router.get("/voice")
    async def voice_index(request: Request) -> HTMLResponse:
        return _voice_index_response(request, session_token)

    @router.get("/voice/{asset_name}")
    async def voice_asset(asset_name: str) -> FileResponse:
        media_type = _ALLOWED_VOICE_ASSETS.get(asset_name)
        if media_type is None:
            raise HTTPException(status_code=404, detail="Voice asset not found")
        asset_path = VOICE_CLIENT_DIR / asset_name
        if not asset_path.is_file():
            raise HTTPException(status_code=404, detail="Voice asset not found")
        headers = _NO_STORE_HEADERS
        if asset_name == "sw.js":
            # Legalizes scope "/voice" for a worker script living at
            # /voice/sw.js: a service worker may only control its own
            # directory and below ("/voice/"), which does NOT cover the
            # page URL "/voice" itself. This header widens the allowed
            # scope so the registration with {scope: "/voice"} succeeds.
            headers = {**_NO_STORE_HEADERS, **_SERVICE_WORKER_SCOPE_HEADERS}
        return FileResponse(
            asset_path,
            media_type=media_type,
            headers=headers,
        )

    @router.post("/api/voice/spar/warmup")
    async def voice_spar_warmup() -> JSONResponse:
        """Idempotent, best-effort turn-1-latency warmup for the next Spar session.

        Auth is the same as every other ``/api/`` route in the dashboard app:
        the app-wide session-token/cookie gate (``hermes_cli.web_server``)
        already covers this path (not in ``PUBLIC_API_PATHS``), so no
        additional check is needed here — this route has no WS upgrade, so
        the ``ws_*_reason`` callbacks (websocket-only) don't apply.
        """
        if not spar_config.enabled or spar_config.llm_lane != "claude":
            return JSONResponse({"warmed": False})
        await asyncio.gather(
            _warm_whisper_model(spar_config.whisper_model),
            prespawn_spar_claude_lane(spar_config, config),
            return_exceptions=True,
        )
        return JSONResponse({"warmed": True})

    @router.websocket("/api/voice/live")
    async def voice_live(websocket: WebSocket) -> None:
        auth_reason, _credential = ws_auth_reason(websocket)
        if auth_reason is not None:
            await websocket.close(
                code=4401,
                reason=ws_close_reason(f"auth: {auth_reason}"),
            )
            return
        host_origin_reason = ws_host_origin_reason(websocket)
        if host_origin_reason is not None:
            await websocket.close(
                code=4403,
                reason=ws_close_reason(host_origin_reason),
            )
            return
        client_reason = ws_client_reason(websocket)
        if client_reason is not None:
            await websocket.close(
                code=4408,
                reason=ws_close_reason(client_reason),
            )
            return

        await websocket.accept()
        audio_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_AUDIO_QUEUE_FRAMES)
        events_out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_ITEMS
        )
        text_in: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
        video_in: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_VIDEO_QUEUE_MAXSIZE
        )
        fallback_pcm = bytearray()
        fallback_mode = asyncio.Event()
        disconnected = asyncio.Event()
        text_turn = _FallbackTextTurn()
        frame_cache = VideoFrameCache()
        fresh_frames = FreshFrameBroker()
        sender_task = asyncio.create_task(
            _send_voice_events(websocket, events_out, disconnected)
        )
        reader_task = asyncio.create_task(
            _read_voice_frames(
                websocket,
                audio_in,
                fallback_pcm,
                events_out,
                fallback_mode,
                disconnected,
                text_in,
                video_in,
                config,
                text_turn,
                frame_cache,
                fresh_frames,
            )
        )
        raw_session_id = websocket.query_params.get("session")
        session_id = (
            raw_session_id
            if raw_session_id is not None
            and _SESSION_ID_PATTERN.fullmatch(raw_session_id)
            else None
        )
        api_key = resolve_gemini_api_key()
        live_task: asyncio.Task[bool] | None = None
        forced_end = asyncio.Event()
        if api_key:
            live_task = asyncio.create_task(
                _run_live_bridge(
                    config,
                    api_key,
                    audio_in,
                    events_out,
                    fallback_mode,
                    disconnected,
                    session_id,
                    text_in,
                    video_in,
                    forced_end,
                    frame_cache,
                    fresh_frames,
                )
            )
        else:
            fallback_mode.set()
            await _put_event(
                events_out,
                {"type": "mode", "value": "fallback", "video_mode": config.video_mode},
                disconnected,
            )

        run_fallback = not api_key
        reader_result = "disconnect"
        try:
            if live_task is None:
                reader_result = await reader_task
            else:
                done, _ = await asyncio.wait(
                    {reader_task, live_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if live_task in done:
                    run_fallback = live_task.result()
                    if forced_end.is_set():
                        # Server-initiated stop (max duration/hard budget):
                        # close now, don't wait out the client's own "end".
                        if not reader_task.done():
                            reader_task.cancel()
                        reader_result = "server_closed"
                    elif not reader_task.done():
                        try:
                            reader_result = await asyncio.wait_for(
                                asyncio.shield(reader_task),
                                timeout=_FALLBACK_END_TIMEOUT_SECONDS,
                            )
                        except TimeoutError:
                            reader_result = "error"
                            await _put_event(
                                events_out,
                                {
                                    "type": "error",
                                    "error": {
                                        "code": "fallback_audio_timeout",
                                        "message": (
                                            "Die Fallback-Aufnahme wurde nicht "
                                            "rechtzeitig beendet."
                                        ),
                                    },
                                },
                                disconnected,
                            )
                    else:
                        reader_result = reader_task.result()
                else:
                    reader_result = reader_task.result()
                    if reader_result == "end" and not live_task.done():
                        try:
                            run_fallback = await asyncio.wait_for(
                                asyncio.shield(live_task),
                                timeout=_LIVE_END_GRACE_SECONDS,
                            )
                        except TimeoutError:
                            run_fallback = False
                    elif live_task.done():
                        run_fallback = live_task.result()

            if run_fallback and reader_result == "end" and not disconnected.is_set():
                try:
                    await _run_controllable_cascade(
                        websocket,
                        bytes(fallback_pcm),
                        config,
                        events_out,
                        disconnected,
                    )
                except VoiceRuntimeError as exc:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {"code": exc.code, "message": exc.message},
                        },
                        disconnected,
                    )
                except Exception:
                    _log.exception("Voice cascade fallback failed")
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "fallback_failed",
                                "message": "Der Voice-Fallback ist fehlgeschlagen.",
                            },
                        },
                        disconnected,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.error(
                "Voice Live websocket failed internally type=%s",
                type(exc).__name__,
            )
            await _put_event(
                events_out,
                {
                    "type": "error",
                    "error": {
                        "code": "live_internal_error",
                        "message": "Die Live-Sprachverbindung ist intern fehlgeschlagen.",
                    },
                },
                disconnected,
            )
        finally:
            for task in (reader_task, live_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (reader_task, live_task) if task is not None),
                return_exceptions=True,
            )
            if text_turn.task is not None:
                await _cancel_task_bounded(text_turn.task)
            await _finish_event_sender(events_out, sender_task, disconnected)
            if not disconnected.is_set():
                try:
                    await websocket.close(code=1000)
                except RuntimeError:
                    pass

    @router.websocket("/api/voice/spar")
    async def voice_spar(websocket: WebSocket) -> None:
        auth_reason, _credential = ws_auth_reason(websocket)
        if auth_reason is not None:
            await websocket.close(
                code=4401,
                reason=ws_close_reason(f"auth: {auth_reason}"),
            )
            return
        host_origin_reason = ws_host_origin_reason(websocket)
        if host_origin_reason is not None:
            await websocket.close(
                code=4403,
                reason=ws_close_reason(host_origin_reason),
            )
            return
        client_reason = ws_client_reason(websocket)
        if client_reason is not None:
            await websocket.close(
                code=4408,
                reason=ws_close_reason(client_reason),
            )
            return

        await websocket.accept()
        if not spar_config.enabled:
            await websocket.close(
                code=4404,
                reason=ws_close_reason("Sparmodus ist deaktiviert."),
            )
            return

        events_out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_ITEMS
        )
        disconnected = asyncio.Event()
        frame_cache = VideoFrameCache()
        fresh_frames = FreshFrameBroker()
        sender_task = asyncio.create_task(
            _send_voice_events(websocket, events_out, disconnected)
        )

        async def _request_fresh_frame() -> bytes | None:
            return await fresh_frames.request(events_out, disconnected)

        executor = VoiceToolExecutor(
            delegate=lambda prompt: delegate_to_hermes(
                prompt, timeout_seconds=_DELEGATE_TIMEOUT_SECONDS
            ),
            request_frame=_request_fresh_frame,
            look_model=config.look_model,
            gemini_api_key=resolve_gemini_api_key(),
        )

        # One LLM lane per Sparmodus session, not per turn: for the claude
        # lane this is the persistent CLI child (see PersistentClaudeLane).
        # A warm pooled lane (see prespawn_spar_claude_lane / the /api/voice/
        # spar/warmup route) is consumed here if one fits and is still
        # fresh; otherwise a fresh lane is spawned exactly as before.
        pooled_lane = (
            await _take_pooled_spar_lane(spar_config, config)
            if spar_config.llm_lane == "claude"
            else None
        )
        lane = pooled_lane or spar_create_llm_lane(
            spar_config.llm_lane,
            model=spar_config.llm_model,
            timeout=spar_config.llm_timeout_seconds,
            system_instruction=spar_effective_system_instruction(spar_config, config),
        )

        history: list[tuple[str, str]] = []
        try:
            # start() is called now, before the first turn, so its ~5s CLI
            # startup overlaps with the client's first recording/STT instead
            # of the first reply's latency budget.
            await lane.start()
            while True:
                turn_pcm = bytearray()
                result = await _read_spar_frames(
                    websocket, turn_pcm, events_out, disconnected, frame_cache, fresh_frames
                )
                if result in ("disconnect", "error", "end"):
                    break
                if result != "turn_end":
                    continue
                try:
                    history = await _run_spar_cascade(
                        bytes(turn_pcm),
                        spar_config,
                        config,
                        events_out,
                        disconnected,
                        executor,
                        history,
                        lane,
                    )
                except VoiceRuntimeError as exc:
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {"code": exc.code, "message": exc.message},
                        },
                        disconnected,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("Voice Sparmodus cascade failed")
                    await _put_event(
                        events_out,
                        {
                            "type": "error",
                            "error": {
                                "code": "spar_failed",
                                "message": "Der Sparmodus ist fehlgeschlagen.",
                            },
                        },
                        disconnected,
                    )
        except asyncio.CancelledError:
            raise
        finally:
            await lane.aclose()
            await _finish_event_sender(events_out, sender_task, disconnected)
            if not disconnected.is_set():
                try:
                    await websocket.close(code=1000)
                except RuntimeError:
                    pass

    return router

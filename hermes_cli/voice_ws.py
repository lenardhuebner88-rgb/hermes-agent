"""Standalone voice web routes and the Live-to-cascade bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Any
import uuid
import wave

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
import psutil

from hermes_cli.config import load_env
from hermes_cli.voice_live_session import GeminiLiveSession, LiveFallbackRequired
from hermes_constants import get_hermes_home
from tools.transcription_tools import transcribe_audio
from tools.tts_tool import text_to_speech_tool
from tools.voice_live_tools import FUNCTION_DECLARATIONS, VoiceToolExecutor


DEFAULT_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
VOICE_CLIENT_DIR = Path(__file__).with_name("voice_client")

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
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_PROCESS_TERMINATE_GRACE_SECONDS = 1.0
_FALLBACK_CANCEL_TIMEOUT_SECONDS = 7.0
_FFMPEG_TIMEOUT_SECONDS = 60.0
_OUTPUT_PCM_CHUNK_BYTES = 24_000

_log = logging.getLogger(__name__)

WsAuthReason = Callable[[WebSocket], tuple[str | None, str]]
WsReason = Callable[[WebSocket], str | None]
WsCloseReason = Callable[[str], str]


@dataclass
class VoiceWebConfig:
    enabled: bool = False
    model: str = DEFAULT_LIVE_MODEL
    language: str = "de-DE"


class VoiceRuntimeError(RuntimeError):
    """A safe, structured error suitable for a websocket response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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


async def delegate_to_hermes(prompt: str) -> str:
    """Delegate one fallback turn through the supported Hermes CLI surface."""
    executable = resolve_hermes_executable()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-q",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_delegation_isolation_kwargs(),
        )
    except OSError as exc:
        raise VoiceRuntimeError(
            "delegation_unavailable",
            "Hermes konnte nicht gestartet werden.",
        ) from exc

    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=_DELEGATE_TIMEOUT_SECONDS,
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
    if not response:
        raise VoiceRuntimeError(
            "delegation_empty",
            "Hermes hat keine Antwort geliefert.",
        )
    return response


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


async def _read_voice_frames(
    websocket: WebSocket,
    audio_in: asyncio.Queue[bytes],
    fallback_pcm: bytearray,
    events_out: asyncio.Queue[dict[str, Any] | None],
    fallback_mode: asyncio.Event,
    disconnected: asyncio.Event,
) -> str:
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
                _discard_queued_response_events(events_out)
                await _put_event(events_out, {"type": "interrupted"}, disconnected)
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
    if not await _put_event_checkpoint(
        events_out,
        {"type": "transcript", "role": "assistant", "text": response},
        disconnected,
    ):
        return
    audio = await fallback_synthesize_pcm(response, config.language)
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


async def _run_live_bridge(
    config: VoiceWebConfig,
    api_key: str,
    audio_in: asyncio.Queue[bytes],
    events_out: asyncio.Queue[dict[str, Any] | None],
    fallback_mode: asyncio.Event,
) -> bool:
    session = GeminiLiveSession(
        config.model,
        config.language,
        FUNCTION_DECLARATIONS,
        api_key,
    )
    executor = VoiceToolExecutor(delegate=delegate_to_hermes)
    try:
        await session.run(audio_in, events_out, executor)
    except LiveFallbackRequired:
        fallback_mode.set()
        return True
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
    router = APIRouter()
    config = voice_web_config(raw_config)
    if not config.enabled:
        return router

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
        fallback_pcm = bytearray()
        fallback_mode = asyncio.Event()
        disconnected = asyncio.Event()
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
            )
        )
        api_key = resolve_gemini_api_key()
        live_task: asyncio.Task[bool] | None = None
        if api_key:
            live_task = asyncio.create_task(
                _run_live_bridge(
                    config,
                    api_key,
                    audio_in,
                    events_out,
                    fallback_mode,
                )
            )
        else:
            fallback_mode.set()

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
                    if not reader_task.done():
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
            await _finish_event_sender(events_out, sender_task, disconnected)
            if not disconnected.is_set():
                try:
                    await websocket.close(code=1000)
                except RuntimeError:
                    pass

    return router

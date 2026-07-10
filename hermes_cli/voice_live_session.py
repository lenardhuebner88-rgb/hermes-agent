"""Gemini Live session bridge for the standalone voice assistant."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
from typing import Any, Protocol

from google import genai
from google.genai import errors, types
from websockets.exceptions import WebSocketException

_INPUT_MIME_TYPE = "audio/pcm;rate=16000"
_CONTEXT_TRIGGER_TOKENS = 100_000
_CONTEXT_TARGET_TOKENS = 50_000
_MAX_TOOL_ERROR_CHARS = 500
_SENDER_HANDOFF_SECONDS = 1.0
_MAX_CONCURRENT_NON_BLOCKING_CALLS = 2
_NON_BLOCKING_SESSION_WAIT_SECONDS = 10.0
_NON_BLOCKING_SESSION_POLL_SECONDS = 0.25

DEFAULT_SYSTEM_INSTRUCTION = (
    "Du bist Hermes, Piets persönlicher Sprachassistent auf seinem Homeserver. "
    "Sprich Deutsch, außer Piet wechselt die Sprache. Deine Antworten werden "
    "vorgelesen: antworte in einem bis drei kurzen Sätzen, keine Listen, keine "
    "Markdown-Zeichen. Du hast Werkzeuge: tmux-Terminals (lesen, Befehle "
    "senden), Delegation an den Hermes-Agenten für größere Aufgaben, "
    "Google-Suche für aktuelle Fakten, Discord-Nachrichten an Piet (für "
    "Ergebnisse und Links zum Nachlesen), Kanban-Aufgaben anlegen, den "
    "Hermes-Systemstatus abfragen und Erinnerungen planen, die per Discord "
    "ankommen. Kündige eine Delegation kurz an, bevor du sie startest, und "
    "sprich weiter, während sie läuft; das Ergebnis kommt später automatisch. "
    "Wenn du etwas nicht sicher weißt, sag es ehrlich. Bestätige ausgeführte "
    "Aktionen knapp."
)

_FALLBACK_ERRORS = (
    errors.APIError,
    WebSocketException,
    ConnectionError,
    OSError,
    TimeoutError,
)

_log = logging.getLogger(__name__)


class LiveFallbackRequired(RuntimeError):
    """Signal that the caller should switch this voice session to cascade mode."""


class _ToolExecutor(Protocol):
    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...

    def is_non_blocking(self, name: str) -> bool: ...


class _LiveSession(Protocol):
    async def send_realtime_input(self, *, audio: types.Blob) -> None: ...

    async def send_client_content(
        self, *, turns: types.Content, turn_complete: bool = True
    ) -> None: ...

    async def send_tool_response(
        self, *, function_responses: Sequence[types.FunctionResponse]
    ) -> None: ...

    def receive(self) -> Any: ...


@dataclass(slots=True)
class _PendingAudio:
    data: bytes
    source_queue: asyncio.Queue[bytes]
    source_ack_owed: bool = True


class GeminiLiveSession:
    """Relay PCM and tools with at-least-once, no-local-drop audio resumption."""

    def __init__(
        self,
        model: str,
        language: str,
        tool_declarations: list[dict[str, Any]],
        api_key: str,
        *,
        voice: str = "Puck",
        system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION,
        initial_handle: str | None = None,
        on_handle_update: Callable[[str | None], None] | None = None,
    ) -> None:
        self._model = model
        self._language = language
        self._tool_declarations = list(tool_declarations)
        self._api_key = api_key
        self._voice = voice
        self._system_instruction = system_instruction
        self._resumption_handle: str | None = initial_handle
        self._on_handle_update = on_handle_update
        self._replay_audio: deque[_PendingAudio] = deque()
        self._input_transcript_parts: list[str] = []
        self._output_transcript_parts: list[str] = []
        self._pending_tool_tasks: set[asyncio.Task[None]] = set()
        self._active_session: _LiveSession | None = None

    @staticmethod
    def _ack_source_queue(pending: _PendingAudio) -> None:
        if pending.source_ack_owed:
            pending.source_queue.task_done()
            pending.source_ack_owed = False

    def _enqueue_replay(self, pending: _PendingAudio) -> None:
        """Transfer queue ownership before retaining a frame for replay.

        A send accepted remotely but cancelled before its acknowledgement may be
        replayed, so reconnect delivery is at-least-once rather than exact-once.
        """

        self._ack_source_queue(pending)
        self._replay_audio.appendleft(pending)

    def _connect_config(self) -> types.LiveConnectConfig:
        tools = []
        if self._tool_declarations:
            tools.append(types.Tool(function_declarations=self._tool_declarations))
        tools.append(types.Tool(google_search=types.GoogleSearch()))
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=self._system_instruction,
            speech_config=types.SpeechConfig(
                language_code=self._language,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._voice
                    )
                ),
            ),
            tools=tools,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Developer API supports handles, not transparent index replay.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resumption_handle,
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=_CONTEXT_TRIGGER_TOKENS,
                sliding_window=types.SlidingWindow(
                    target_tokens=_CONTEXT_TARGET_TOKENS
                ),
            ),
        )

    async def _next_audio(
        self,
        audio_in: asyncio.Queue[bytes],
        stop_event: asyncio.Event,
    ) -> _PendingAudio | None:
        if self._replay_audio:
            return self._replay_audio.popleft()

        get_task = asyncio.create_task(audio_in.get())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait(
                {get_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in (get_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, stop_task, return_exceptions=True)
            if get_task.done() and not get_task.cancelled():
                pending = _PendingAudio(get_task.result(), audio_in)
                if stop_event.is_set():
                    self._enqueue_replay(pending)
                    return None
                return pending
            return None
        except asyncio.CancelledError:
            for task in (get_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, stop_task, return_exceptions=True)
            if get_task.done() and not get_task.cancelled():
                self._enqueue_replay(_PendingAudio(get_task.result(), audio_in))
            raise
        finally:
            for task in (get_task, stop_task):
                if not task.done():
                    task.cancel()

    async def _send_audio(
        self,
        session: _LiveSession,
        audio_in: asyncio.Queue[bytes],
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            pending = await self._next_audio(audio_in, stop_event)
            if pending is None:
                return
            if stop_event.is_set():
                self._enqueue_replay(pending)
                return
            if not pending.data:
                self._ack_source_queue(pending)
                continue
            try:
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=pending.data,
                        mime_type=_INPUT_MIME_TYPE,
                    )
                )
            except asyncio.CancelledError:
                self._enqueue_replay(pending)
                raise
            except Exception:
                self._enqueue_replay(pending)
                raise
            else:
                self._ack_source_queue(pending)

    async def _send_text(
        self,
        session: _LiveSession,
        text_in: asyncio.Queue[str],
        stop_event: asyncio.Event,
    ) -> None:
        """Drain typed turns into the live session.

        Unlike audio, a typed turn has no replay queue: reconnects are rare
        and a lost typed turn during one is an acceptable one-off loss (see
        ``run()``'s ``text_in`` docstring), so there is nothing to keep track
        of beyond stopping cleanly — if the connection dies between popping
        an item and sending it, that turn is simply gone.
        """
        while not stop_event.is_set():
            get_task = asyncio.create_task(text_in.get())
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait(
                    {get_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (get_task, stop_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(get_task, stop_task, return_exceptions=True)
            if not (get_task.done() and not get_task.cancelled()):
                return
            if stop_event.is_set():
                return
            text = get_task.result()
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=True,
            )

    async def _execute_tool_calls(
        self,
        session: _LiveSession,
        function_calls: Sequence[types.FunctionCall],
        tool_executor: _ToolExecutor,
    ) -> None:
        responses = []
        for call in function_calls:
            name = call.name or ""
            if tool_executor.is_non_blocking(name):
                if len(self._pending_tool_tasks) >= _MAX_CONCURRENT_NON_BLOCKING_CALLS:
                    responses.append(
                        types.FunctionResponse(
                            id=call.id,
                            name=name,
                            response={
                                "error": {
                                    "code": "non_blocking_cap_reached",
                                    "message": (
                                        "Es laufen bereits zwei Hintergrund-Aufgaben. "
                                        "Bitte warte, bis eine fertig ist."
                                    ),
                                }
                            },
                        )
                    )
                    continue
                task = asyncio.create_task(
                    self._run_non_blocking_call(call, tool_executor)
                )
                self._pending_tool_tasks.add(task)
                task.add_done_callback(self._pending_tool_tasks.discard)
                continue
            try:
                result = await tool_executor.execute(name, dict(call.args or {}))
            except Exception as exc:
                result = {
                    "error": {
                        "code": "tool_execution_failed",
                        "message": str(exc)[:_MAX_TOOL_ERROR_CHARS],
                    }
                }
            responses.append(
                types.FunctionResponse(
                    id=call.id,
                    name=name,
                    response=result,
                )
            )
        if responses:
            await session.send_tool_response(function_responses=responses)

    async def _run_non_blocking_call(
        self,
        call: types.FunctionCall,
        tool_executor: _ToolExecutor,
    ) -> None:
        """Run one NON_BLOCKING tool call and deliver its result out of band.

        Spawned as a detached task by :meth:`_execute_tool_calls` so the
        receive loop keeps processing audio/turns while this runs. The
        response goes through whichever session is live when the call
        finishes — not necessarily the one active when it started, since a
        go-away reconnect may have swapped ``self._active_session`` in the
        meantime.
        """

        name = call.name or ""
        try:
            result = await tool_executor.execute(name, dict(call.args or {}))
        except Exception as exc:
            result = {
                "error": {
                    "code": "tool_execution_failed",
                    "message": str(exc)[:_MAX_TOOL_ERROR_CHARS],
                }
            }
        success = "error" not in result

        active = self._active_session
        if active is None:
            waited = 0.0
            while active is None and waited < _NON_BLOCKING_SESSION_WAIT_SECONDS:
                await asyncio.sleep(_NON_BLOCKING_SESSION_POLL_SECONDS)
                waited += _NON_BLOCKING_SESSION_POLL_SECONDS
                active = self._active_session
            if active is None:
                _log.warning(
                    "Dropping non-blocking tool response for %s: no live session",
                    name,
                )
                return

        try:
            await active.send_tool_response(
                function_responses=[
                    types.FunctionResponse(
                        id=call.id,
                        name=name,
                        response=result,
                        scheduling=(
                            types.FunctionResponseScheduling.INTERRUPT
                            if success
                            else types.FunctionResponseScheduling.WHEN_IDLE
                        ),
                    )
                ]
            )
        except Exception:
            _log.exception(
                "Failed to deliver non-blocking tool response for %s", name
            )

    def _notify_handle_update(self) -> None:
        """Tell the caller about a handle change without risking the bridge."""

        if self._on_handle_update is None:
            return
        try:
            self._on_handle_update(self._resumption_handle)
        except Exception:
            _log.exception("Gemini Live resumption handle callback failed")

    @staticmethod
    async def _emit_transcript_fragment(
        events_out: asyncio.Queue[dict[str, Any]],
        role: str,
        parts: list[str],
        transcription: types.Transcription,
    ) -> list[str]:
        """Accumulate one transcription fragment and emit partial/final events.

        Fragments arrive incrementally across messages, so ``parts`` is the
        caller's per-turn buffer; the returned list replaces it (it is reset
        to empty once ``transcription.finished`` closes the turn).
        """

        fragment = transcription.text
        if fragment:
            parts = [*parts, fragment]
            await events_out.put(
                {
                    "type": "transcript",
                    "role": role,
                    "text": "".join(parts),
                    "partial": True,
                }
            )
        if transcription.finished:
            await events_out.put(
                {
                    "type": "transcript",
                    "role": role,
                    "text": "".join(parts),
                    "partial": False,
                }
            )
            parts = []
        return parts

    async def _handle_message(
        self,
        session: _LiveSession,
        message: types.LiveServerMessage,
        events_out: asyncio.Queue[dict[str, Any]],
        tool_executor: _ToolExecutor,
    ) -> bool:
        """Handle one message and return whether a resumption reconnect is due."""

        update = message.session_resumption_update
        if update:
            if update.resumable is False:
                self._resumption_handle = None
                self._notify_handle_update()
            elif update.resumable and update.new_handle:
                self._resumption_handle = update.new_handle
                self._notify_handle_update()

        data = message.data
        if data:
            await events_out.put({"type": "state", "value": "speaking"})
            await events_out.put({"type": "audio", "data": data})

        tool_call = message.tool_call
        if tool_call and tool_call.function_calls:
            has_blocking_call = any(
                not tool_executor.is_non_blocking(call.name or "")
                for call in tool_call.function_calls
            )
            if has_blocking_call:
                await events_out.put({"type": "state", "value": "thinking"})
            await self._execute_tool_calls(
                session,
                tool_call.function_calls,
                tool_executor,
            )

        content = message.server_content
        if content and content.input_transcription:
            self._input_transcript_parts = await self._emit_transcript_fragment(
                events_out,
                "user",
                self._input_transcript_parts,
                content.input_transcription,
            )
        if content and content.output_transcription:
            self._output_transcript_parts = await self._emit_transcript_fragment(
                events_out,
                "assistant",
                self._output_transcript_parts,
                content.output_transcription,
            )
        # turn_complete/interrupted can close a turn without the output
        # transcription's own `finished` flag ever arriving, so flush
        # whatever is buffered here too — before the state events below,
        # so the client always sees the final transcript first.
        if (
            content
            and (content.interrupted or content.turn_complete)
            and self._output_transcript_parts
        ):
            await events_out.put(
                {
                    "type": "transcript",
                    "role": "assistant",
                    "text": "".join(self._output_transcript_parts),
                    "partial": False,
                }
            )
            self._output_transcript_parts = []

        if content and content.interrupted:
            await events_out.put({"type": "interrupted"})
            await events_out.put({"type": "state", "value": "listening"})
        if content and content.turn_complete:
            await events_out.put({"type": "state", "value": "listening"})

        if message.go_away:
            if self._resumption_handle is None:
                raise ConnectionError(
                    "Gemini Live announced disconnect without a resumption handle"
                )
            return True
        return False

    async def _receive_turns(
        self,
        session: _LiveSession,
        events_out: asyncio.Queue[dict[str, Any]],
        tool_executor: _ToolExecutor,
    ) -> bool:
        """Receive complete turns, re-entering the SDK iterator after each one."""

        while True:
            received_message = False
            async for message in session.receive():
                received_message = True
                if await self._handle_message(
                    session,
                    message,
                    events_out,
                    tool_executor,
                ):
                    return True
            if not received_message:
                raise ConnectionError("Gemini Live receive stream ended unexpectedly")

    @staticmethod
    async def _cancel_tasks(*tasks: asyncio.Task[Any]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_connection(
        self,
        session: _LiveSession,
        audio_in: asyncio.Queue[bytes],
        events_out: asyncio.Queue[dict[str, Any]],
        tool_executor: _ToolExecutor,
        text_in: asyncio.Queue[str] | None,
    ) -> bool:
        stop_sender = asyncio.Event()
        sender = asyncio.create_task(self._send_audio(session, audio_in, stop_sender))
        receiver = asyncio.create_task(
            self._receive_turns(session, events_out, tool_executor)
        )
        text_sender = (
            asyncio.create_task(self._send_text(session, text_in, stop_sender))
            if text_in is not None
            else None
        )
        pending = {sender, receiver} | ({text_sender} if text_sender else set())
        try:
            done, _ = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task.cancelled():
                    raise asyncio.CancelledError
                error = task.exception()
                if error is not None:
                    raise error
            if receiver in done:
                reconnect = receiver.result()
                stop_sender.set()
                try:
                    await asyncio.wait_for(
                        sender,
                        timeout=_SENDER_HANDOFF_SECONDS,
                    )
                except TimeoutError:
                    if sender.done() and not sender.cancelled():
                        error = sender.exception()
                        if error is not None:
                            raise error
                return reconnect
            if text_sender is not None and text_sender in done:
                raise ConnectionError("Gemini Live text sender stopped unexpectedly")
            raise ConnectionError("Gemini Live audio sender stopped unexpectedly")
        finally:
            await self._cancel_tasks(
                *({sender, receiver} | ({text_sender} if text_sender else set()))
            )

    async def run(
        self,
        audio_in: asyncio.Queue[bytes],
        events_out: asyncio.Queue[dict[str, Any]],
        tool_executor: _ToolExecutor,
        text_in: asyncio.Queue[str] | None = None,
    ) -> None:
        """Run until cancelled, or request cascade fallback on a Live API failure.

        ``text_in`` is optional so a caller that never offers typed input
        keeps today's behavior byte-identical: with it ``None`` (the
        default), no text-drain task is ever created and
        ``send_client_content`` is never called.
        """

        self._input_transcript_parts = []
        self._output_transcript_parts = []
        announced_live = False
        try:
            client = genai.Client(api_key=self._api_key)
            while True:
                config = self._connect_config()
                async with client.aio.live.connect(
                    model=self._model,
                    config=config,
                ) as session:
                    self._active_session = session
                    try:
                        if not announced_live:
                            await events_out.put({"type": "mode", "value": "live"})
                            announced_live = True
                        await events_out.put({"type": "state", "value": "listening"})
                        reconnect = await self._run_connection(
                            session,
                            audio_in,
                            events_out,
                            tool_executor,
                            text_in,
                        )
                    finally:
                        self._active_session = None
                if not reconnect:
                    raise ConnectionError("Gemini Live session ended unexpectedly")
        except asyncio.CancelledError:
            raise
        except _FALLBACK_ERRORS as exc:
            raise LiveFallbackRequired("Gemini Live is unavailable") from exc
        finally:
            await self._cancel_tasks(*self._pending_tool_tasks)

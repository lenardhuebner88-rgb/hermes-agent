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
_VOICE_NAME = "Puck"
_MAX_TOOL_ERROR_CHARS = 500
_SENDER_HANDOFF_SECONDS = 1.0

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


class _LiveSession(Protocol):
    async def send_realtime_input(self, *, audio: types.Blob) -> None: ...

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
        initial_handle: str | None = None,
        on_handle_update: Callable[[str | None], None] | None = None,
    ) -> None:
        self._model = model
        self._language = language
        self._tool_declarations = list(tool_declarations)
        self._api_key = api_key
        self._resumption_handle: str | None = initial_handle
        self._on_handle_update = on_handle_update
        self._replay_audio: deque[_PendingAudio] = deque()

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
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=(
                "Du bist Hermes, ein hilfreicher Sprachassistent. "
                "Antworte knapp, natürlich und standardmäßig auf Deutsch."
            ),
            speech_config=types.SpeechConfig(
                language_code=self._language,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_VOICE_NAME
                    )
                ),
            ),
            tools=tools,
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

    async def _execute_tool_calls(
        self,
        session: _LiveSession,
        function_calls: Sequence[types.FunctionCall],
        tool_executor: _ToolExecutor,
    ) -> None:
        responses = []
        for call in function_calls:
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
            responses.append(
                types.FunctionResponse(
                    id=call.id,
                    name=name,
                    response=result,
                )
            )
        if responses:
            await session.send_tool_response(function_responses=responses)

    def _notify_handle_update(self) -> None:
        """Tell the caller about a handle change without risking the bridge."""

        if self._on_handle_update is None:
            return
        try:
            self._on_handle_update(self._resumption_handle)
        except Exception:
            _log.exception("Gemini Live resumption handle callback failed")

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
            await events_out.put({"type": "state", "value": "thinking"})
            await self._execute_tool_calls(
                session,
                tool_call.function_calls,
                tool_executor,
            )

        content = message.server_content
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
    ) -> bool:
        stop_sender = asyncio.Event()
        sender = asyncio.create_task(self._send_audio(session, audio_in, stop_sender))
        receiver = asyncio.create_task(
            self._receive_turns(session, events_out, tool_executor)
        )
        try:
            done, _ = await asyncio.wait(
                {sender, receiver},
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
            raise ConnectionError("Gemini Live audio sender stopped unexpectedly")
        finally:
            await self._cancel_tasks(sender, receiver)

    async def run(
        self,
        audio_in: asyncio.Queue[bytes],
        events_out: asyncio.Queue[dict[str, Any]],
        tool_executor: _ToolExecutor,
    ) -> None:
        """Run until cancelled, or request cascade fallback on a Live API failure."""

        announced_live = False
        try:
            client = genai.Client(api_key=self._api_key)
            while True:
                config = self._connect_config()
                async with client.aio.live.connect(
                    model=self._model,
                    config=config,
                ) as session:
                    if not announced_live:
                        await events_out.put({"type": "mode", "value": "live"})
                        announced_live = True
                    await events_out.put({"type": "state", "value": "listening"})
                    reconnect = await self._run_connection(
                        session,
                        audio_in,
                        events_out,
                        tool_executor,
                    )
                if not reconnect:
                    raise ConnectionError("Gemini Live session ended unexpectedly")
        except asyncio.CancelledError:
            raise
        except _FALLBACK_ERRORS as exc:
            raise LiveFallbackRequired("Gemini Live is unavailable") from exc

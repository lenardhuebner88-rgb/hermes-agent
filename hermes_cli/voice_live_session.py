"""Gemini Live session bridge for the standalone voice assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from google import genai
from google.genai import errors, types
from websockets.exceptions import WebSocketException

_INPUT_MIME_TYPE = "audio/pcm;rate=16000"
_CONTEXT_TRIGGER_TOKENS = 100_000
_CONTEXT_TARGET_TOKENS = 50_000
_VOICE_NAME = "Puck"
_MAX_TOOL_ERROR_CHARS = 500

_FALLBACK_ERRORS = (
    errors.APIError,
    WebSocketException,
    ConnectionError,
    OSError,
    TimeoutError,
    ValueError,
)


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


class GeminiLiveSession:
    """Relay PCM audio and function calls through one resumable Gemini session."""

    def __init__(
        self,
        model: str,
        language: str,
        tool_declarations: list[dict[str, Any]],
        api_key: str,
    ) -> None:
        self._model = model
        self._language = language
        self._tool_declarations = list(tool_declarations)
        self._api_key = api_key
        self._resumption_handle: str | None = None

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
            session_resumption=types.SessionResumptionConfig(
                handle=self._resumption_handle,
                # We reconnect with server handles, but do not buffer/replay
                # client messages from last_consumed_client_message_index.
                transparent=False,
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=_CONTEXT_TRIGGER_TOKENS,
                sliding_window=types.SlidingWindow(
                    target_tokens=_CONTEXT_TARGET_TOKENS
                ),
            ),
        )

    async def _send_audio(
        self,
        session: _LiveSession,
        audio_in: asyncio.Queue[bytes],
    ) -> None:
        while True:
            data = await audio_in.get()
            try:
                if not data:
                    continue
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=_INPUT_MIME_TYPE)
                )
            finally:
                audio_in.task_done()

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

    async def _handle_message(
        self,
        session: _LiveSession,
        message: types.LiveServerMessage,
        events_out: asyncio.Queue[dict[str, Any]],
        tool_executor: _ToolExecutor,
    ) -> bool:
        """Handle one message and return whether a resumption reconnect is due."""

        update = message.session_resumption_update
        if update and update.resumable and update.new_handle:
            self._resumption_handle = update.new_handle

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
        sender = asyncio.create_task(self._send_audio(session, audio_in))
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
                return receiver.result()
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

        try:
            client = genai.Client(api_key=self._api_key)
            while True:
                config = self._connect_config()
                async with client.aio.live.connect(
                    model=self._model,
                    config=config,
                ) as session:
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

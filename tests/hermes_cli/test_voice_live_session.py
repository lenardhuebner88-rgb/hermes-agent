"""Behavior tests for the Gemini Live voice-session wrapper."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors, types


class _FakeConnection:
    def __init__(self, session: _FakeSDKSession | None, error: Exception | None):
        self._session = session
        self._error = error

    async def __aenter__(self) -> _FakeSDKSession:
        if self._error is not None:
            raise self._error
        assert self._session is not None
        return self._session

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeLive:
    def __init__(
        self,
        sessions: list[_FakeSDKSession] | None = None,
        *,
        connect_error: Exception | None = None,
    ):
        self._sessions = list(sessions or [])
        self._connect_error = connect_error
        self.calls: list[tuple[str, types.LiveConnectConfig]] = []
        self.connect_called = asyncio.Event()
        self.second_connect_called = asyncio.Event()

    def connect(self, *, model: str, config: types.LiveConnectConfig):
        self.calls.append((model, config))
        self.connect_called.set()
        if len(self.calls) >= 2:
            self.second_connect_called.set()
        if self._connect_error is not None:
            return _FakeConnection(None, self._connect_error)
        session = self._sessions.pop(0)
        return _FakeConnection(session, None)


class _FakeClient:
    def __init__(self, live: _FakeLive):
        self.aio = SimpleNamespace(live=live)


class _FakeSDKSession:
    def __init__(
        self,
        turns: list[list[types.LiveServerMessage]] | None = None,
        *,
        send_error: Exception | None = None,
        receive_error: Exception | None = None,
    ):
        self._turns = list(turns or [])
        self._send_error = send_error
        self._receive_error = receive_error
        self._never_finish = asyncio.Event()
        self.receive_calls = 0
        self.receive_reentered = asyncio.Event()
        self.audio_sent = asyncio.Event()
        self.realtime_inputs: list[types.Blob] = []
        self.tool_responses: list[list[types.FunctionResponse]] = []

    async def send_realtime_input(self, *, audio: types.Blob) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.realtime_inputs.append(audio)
        self.audio_sent.set()

    async def send_tool_response(
        self, *, function_responses: list[types.FunctionResponse]
    ) -> None:
        self.tool_responses.append(function_responses)

    async def receive(self):
        self.receive_calls += 1
        if self.receive_calls >= 2:
            self.receive_reentered.set()
        if self._receive_error is not None:
            if False:  # pragma: no cover - makes this an async generator
                yield None
            raise self._receive_error
        if self._turns:
            for message in self._turns.pop(0):
                yield message
            return
        await self._never_finish.wait()
        if False:  # pragma: no cover - makes this an async generator
            yield None


class _FakeToolExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"terminals": [{"name": "work", "attached": True}]}


def _audio_message(data: bytes) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                role="model",
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            data=data,
                            mime_type="audio/pcm;rate=24000",
                        )
                    )
                ],
            )
        )
    )


def _tool_call_message() -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[
                types.FunctionCall(id="call-1", name="list_terminals", args={})
            ]
        )
    )


def _turn_complete_message(*, interrupted: bool = False) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            interrupted=interrupted,
            turn_complete=True,
        )
    )


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, live: _FakeLive) -> None:
    from hermes_cli import voice_live_session

    monkeypatch.setattr(
        voice_live_session.genai,
        "Client",
        lambda *, api_key: _FakeClient(live),
    )


@pytest.mark.asyncio
async def test_run_streams_pcm_executes_tools_and_reenters_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    output_audio = b"\x10\x20\x30\x40"
    sdk_session = _FakeSDKSession(
        turns=[
            [
                _audio_message(output_audio),
                _tool_call_message(),
                _turn_complete_message(interrupted=True),
            ]
        ]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)

    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await audio_in.put(b"\x01\x02")
    executor = _FakeToolExecutor()
    wrapper = GeminiLiveSession(
        model="live-model",
        language="de-DE",
        tool_declarations=[{"name": "list_terminals"}],
        api_key="secret",
    )

    task = asyncio.create_task(wrapper.run(audio_in, events_out, executor))
    await asyncio.wait_for(sdk_session.audio_sent.wait(), timeout=1)
    await asyncio.wait_for(sdk_session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())

    assert {"type": "audio", "data": output_audio} in emitted
    assert {"type": "state", "value": "speaking"} in emitted
    assert {"type": "state", "value": "thinking"} in emitted
    assert {"type": "state", "value": "listening"} in emitted
    assert {"type": "interrupted"} in emitted
    assert executor.calls == [("list_terminals", {})]
    response = sdk_session.tool_responses[0][0]
    assert response.id == "call-1"
    assert response.name == "list_terminals"
    assert response.response == {"terminals": [{"name": "work", "attached": True}]}
    assert sdk_session.receive_calls >= 2
    assert sdk_session.realtime_inputs[0].data == b"\x01\x02"
    assert sdk_session.realtime_inputs[0].mime_type == "audio/pcm;rate=16000"

    model, config = live.calls[0]
    assert model == "live-model"
    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.speech_config.language_code == "de-DE"
    assert config.tools[0].function_declarations[0].name == "list_terminals"
    assert config.session_resumption.handle is None
    assert config.session_resumption.transparent is False
    assert config.context_window_compression.trigger_tokens == 100_000
    assert config.context_window_compression.sliding_window.target_tokens == 50_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("connect failed"),
        errors.ClientError(429, {"message": "quota exceeded"}),
    ],
)
async def test_connect_errors_request_fallback(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from hermes_cli.voice_live_session import (
        GeminiLiveSession,
        LiveFallbackRequired,
    )

    live = _FakeLive(connect_error=error)
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    with pytest.raises(LiveFallbackRequired) as caught:
        await wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())

    assert caught.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["send", "receive"])
async def test_stream_errors_request_fallback(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from hermes_cli.voice_live_session import (
        GeminiLiveSession,
        LiveFallbackRequired,
    )

    failure = ConnectionError(f"{path} failed")
    session = _FakeSDKSession(
        send_error=failure if path == "send" else None,
        receive_error=failure if path == "receive" else None,
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    if path == "send":
        await audio_in.put(b"\x01\x02")
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    with pytest.raises(LiveFallbackRequired) as caught:
        await wrapper.run(audio_in, asyncio.Queue(), _FakeToolExecutor())

    assert caught.value.__cause__ is failure


@pytest.mark.asyncio
async def test_go_away_reconnects_with_latest_resumption_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    first = _FakeSDKSession(
        turns=[
            [
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        new_handle="resume-123",
                        resumable=True,
                    ),
                    go_away=types.LiveServerGoAway(time_left="5s"),
                )
            ]
        ]
    )
    second = _FakeSDKSession()
    live = _FakeLive([first, second])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(live.second_connect_called.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert live.calls[0][1].session_resumption.handle is None
    assert live.calls[1][1].session_resumption.handle == "resume-123"


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    session = _FakeSDKSession()
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

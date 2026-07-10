"""Behavior tests for the Gemini Live voice-session wrapper."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from google import genai
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


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self, *, decode: bool = False) -> bytes:
        assert decode is False
        return b'{"setupComplete": {}}'


class _FakeWebSocketConnection:
    def __init__(self, websocket: _FakeWebSocket):
        self._websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self._websocket

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeSDKSession:
    def __init__(
        self,
        turns: list[list[types.LiveServerMessage]] | None = None,
        *,
        send_error: Exception | None = None,
        receive_error: Exception | None = None,
        send_gate: asyncio.Event | None = None,
        expected_sends: int = 1,
        wait_for_send_before_receive: bool = False,
    ):
        self._turns = list(turns or [])
        self._send_error = send_error
        self._receive_error = receive_error
        self._send_gate = send_gate
        self._expected_sends = expected_sends
        self._wait_for_send_before_receive = wait_for_send_before_receive
        self._never_finish = asyncio.Event()
        self.receive_calls = 0
        self.receive_reentered = asyncio.Event()
        self.send_started = asyncio.Event()
        self.audio_sent = asyncio.Event()
        self.expected_audio_sent = asyncio.Event()
        self.realtime_inputs: list[types.Blob] = []
        self.tool_responses: list[list[types.FunctionResponse]] = []
        self.client_contents: list[tuple[types.Content, bool]] = []
        self.client_content_sent = asyncio.Event()

    async def send_client_content(
        self, *, turns: types.Content, turn_complete: bool = True
    ) -> None:
        self.client_contents.append((turns, turn_complete))
        self.client_content_sent.set()

    async def send_realtime_input(self, *, audio: types.Blob) -> None:
        self.send_started.set()
        if self._send_gate is not None:
            await self._send_gate.wait()
        if self._send_error is not None:
            raise self._send_error
        self.realtime_inputs.append(audio)
        self.audio_sent.set()
        if len(self.realtime_inputs) >= self._expected_sends:
            self.expected_audio_sent.set()

    async def send_tool_response(
        self, *, function_responses: list[types.FunctionResponse]
    ) -> None:
        self.tool_responses.append(function_responses)

    async def receive(self):
        self.receive_calls += 1
        if self.receive_calls >= 2:
            self.receive_reentered.set()
        if self._wait_for_send_before_receive and self.receive_calls == 1:
            await self.send_started.wait()
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

    def is_non_blocking(self, name: str) -> bool:
        return False


class _GatedNonBlockingExecutor:
    """Executor whose ``delegate_to_hermes`` calls block on a shared gate.

    Mirrors :class:`tools.voice_live_tools.VoiceToolExecutor`'s NON_BLOCKING
    classification (only ``delegate_to_hermes``) without depending on that
    module — this file tests the session bridge in isolation.
    """

    def __init__(
        self,
        *,
        gate: asyncio.Event,
        result: dict[str, Any] | None = None,
    ):
        self._gate = gate
        self._result = result if result is not None else {"result": "erledigt"}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "delegate_to_hermes":
            await self._gate.wait()
            return self._result
        return {"terminals": []}

    def is_non_blocking(self, name: str) -> bool:
        return name == "delegate_to_hermes"


async def _wait_until(predicate, *, timeout: float = 1.0, interval: float = 0.01) -> None:
    """Poll ``predicate`` until truthy or raise once ``timeout`` elapses."""

    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(interval)


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


def _delegate_tool_call_message(*, call_id: str = "delegate-1") -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[
                types.FunctionCall(
                    id=call_id,
                    name="delegate_to_hermes",
                    args={"prompt": "erledige das"},
                )
            ]
        )
    )


def _delegate_tool_call_message_multi(call_ids: list[str]) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[
                types.FunctionCall(
                    id=call_id, name="delegate_to_hermes", args={"prompt": "erledige das"}
                )
                for call_id in call_ids
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


def _interrupted_message() -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(interrupted=True),
    )


def _input_transcription_message(
    *, text: str | None, finished: bool | None = None
) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=text, finished=finished),
        )
    )


def _output_transcription_message(
    *, text: str | None, finished: bool | None = None
) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text=text, finished=finished),
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
    assert config.session_resumption.transparent is None
    assert config.context_window_compression.trigger_tokens == 100_000
    assert config.context_window_compression.sliding_window.target_tokens == 50_000


@pytest.mark.asyncio
async def test_connect_config_serializes_through_real_developer_api_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.genai.live as sdk_live

    from hermes_cli.voice_live_session import GeminiLiveSession

    websocket = _FakeWebSocket()
    monkeypatch.setattr(
        sdk_live,
        "ws_connect",
        lambda *_args, **_kwargs: _FakeWebSocketConnection(websocket),
    )
    wrapper = GeminiLiveSession("model", "de-DE", [], "unused-api-key")
    wrapper._resumption_handle = "resume-123"
    client = genai.Client(api_key="unused-api-key")
    try:
        async with client.aio.live.connect(
            model="model",
            config=wrapper._connect_config(),
        ):
            pass
    finally:
        client.close()

    assert len(websocket.sent) == 1
    setup = json.loads(websocket.sent[0])["setup"]
    assert setup["sessionResumption"] == {"handle": "resume-123"}


def test_connect_config_defaults_carry_default_voice_and_persona() -> None:
    from hermes_cli.voice_live_session import (
        DEFAULT_SYSTEM_INSTRUCTION,
        GeminiLiveSession,
    )

    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    config = wrapper._connect_config()

    assert (
        config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Puck"
    )
    assert config.system_instruction == DEFAULT_SYSTEM_INSTRUCTION
    assert isinstance(config.input_audio_transcription, types.AudioTranscriptionConfig)
    assert isinstance(config.output_audio_transcription, types.AudioTranscriptionConfig)
    # No function declarations were supplied, so the only tool is search.
    assert len(config.tools) == 1
    assert config.tools[0].google_search is not None


def test_connect_config_carries_configured_voice_persona_and_both_tools() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [{"name": "list_terminals"}],
        "secret",
        voice="Charon",
        system_instruction="Custom persona text.",
    )
    config = wrapper._connect_config()

    assert (
        config.speech_config.voice_config.prebuilt_voice_config.voice_name
        == "Charon"
    )
    assert config.system_instruction == "Custom persona text."
    assert isinstance(config.input_audio_transcription, types.AudioTranscriptionConfig)
    assert isinstance(config.output_audio_transcription, types.AudioTranscriptionConfig)
    assert len(config.tools) == 2
    assert config.tools[0].function_declarations[0].name == "list_terminals"
    assert config.tools[1].google_search is not None


@pytest.mark.asyncio
async def test_input_transcript_streams_partial_fragments_then_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fragments accumulate; a finished-only message flushes the joined text."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    session = _FakeSDKSession(
        turns=[
            [
                _input_transcription_message(text="Hal"),
                _input_transcription_message(text="lo"),
                _input_transcription_message(text=None, finished=True),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())

    transcript_events = [event for event in emitted if event.get("type") == "transcript"]
    assert transcript_events == [
        {"type": "transcript", "role": "user", "text": "Hal", "partial": True},
        {"type": "transcript", "role": "user", "text": "Hallo", "partial": True},
        {"type": "transcript", "role": "user", "text": "Hallo", "partial": False},
    ]
    assert wrapper._input_transcript_parts == []


@pytest.mark.asyncio
async def test_output_transcript_finalizes_before_turn_complete_listening_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    session = _FakeSDKSession(
        turns=[
            [
                _output_transcription_message(text="Hallo Piet"),
                _turn_complete_message(),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())

    final_position = emitted.index(
        {
            "type": "transcript",
            "role": "assistant",
            "text": "Hallo Piet",
            "partial": False,
        }
    )
    assert emitted[final_position + 1] == {"type": "state", "value": "listening"}
    assert wrapper._output_transcript_parts == []


@pytest.mark.asyncio
async def test_output_transcript_finalizes_before_interrupted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    session = _FakeSDKSession(
        turns=[
            [
                _output_transcription_message(text="Halbe Antwort"),
                _interrupted_message(),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())

    final_position = emitted.index(
        {
            "type": "transcript",
            "role": "assistant",
            "text": "Halbe Antwort",
            "partial": False,
        }
    )
    assert emitted[final_position + 1] == {"type": "interrupted"}
    assert wrapper._output_transcript_parts == []


@pytest.mark.asyncio
async def test_transcript_fragment_with_none_text_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    session = _FakeSDKSession(
        turns=[
            [
                _input_transcription_message(text=None),
                _audio_message(b"\x01\x02"),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())

    assert not any(event.get("type") == "transcript" for event in emitted)
    assert wrapper._input_transcript_parts == []


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
async def test_connect_value_error_surfaces_as_programmer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    error = ValueError("invalid live config")
    live = _FakeLive(connect_error=error)
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    with pytest.raises(ValueError, match="invalid live config"):
        await wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())


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
    if path == "send":
        assert audio_in.empty()
        assert [pending.data for pending in wrapper._replay_audio] == [b"\x01\x02"]
        assert all(not pending.source_ack_owed for pending in wrapper._replay_audio)
        await asyncio.wait_for(audio_in.join(), timeout=1)


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
                ),
                types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")),
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
async def test_non_resumable_update_clears_stale_handle_before_go_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import (
        GeminiLiveSession,
        LiveFallbackRequired,
    )

    session = _FakeSDKSession(
        turns=[
            [
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        new_handle="resume-stale",
                        resumable=True,
                    )
                ),
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        resumable=False,
                    )
                ),
                types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    with pytest.raises(LiveFallbackRequired):
        await wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())

    assert len(live.calls) == 1


@pytest.mark.asyncio
async def test_go_away_replays_blocked_frame_first_after_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session
    from hermes_cli.voice_live_session import GeminiLiveSession

    blocked_send = asyncio.Event()
    first = _FakeSDKSession(
        turns=[
            [
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        new_handle="resume-123",
                        resumable=True,
                    )
                ),
                types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")),
            ]
        ],
        send_gate=blocked_send,
        wait_for_send_before_receive=True,
    )
    second = _FakeSDKSession(expected_sends=2)
    live = _FakeLive([first, second])
    _install_fake_client(monkeypatch, live)
    monkeypatch.setattr(
        voice_live_session,
        "_SENDER_HANDOFF_SECONDS",
        0.01,
        raising=False,
    )
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    await audio_in.put(b"first-frame")
    await audio_in.put(b"second-frame")

    task = asyncio.create_task(
        wrapper.run(audio_in, asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(first.send_started.wait(), timeout=1)
    await asyncio.wait_for(second.expected_audio_sent.wait(), timeout=1)
    await asyncio.wait_for(audio_in.join(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert first.realtime_inputs == []
    assert [blob.data for blob in second.realtime_inputs] == [
        b"first-frame",
        b"second-frame",
    ]


@pytest.mark.asyncio
async def test_stop_winner_preserves_get_completed_after_wait_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session
    from hermes_cli.voice_live_session import GeminiLiveSession

    async def stop_only_snapshot(tasks, *, return_when):
        assert return_when is asyncio.FIRST_COMPLETED
        await asyncio.gather(*tasks)
        stop_task = next(task for task in tasks if task.result() is True)
        return {stop_task}, set(tasks) - {stop_task}

    monkeypatch.setattr(voice_live_session.asyncio, "wait", stop_only_snapshot)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    await audio_in.put(b"raced-frame")
    stop_event = asyncio.Event()
    stop_event.set()

    assert await wrapper._next_audio(audio_in, stop_event) is None
    pending = await wrapper._next_audio(audio_in, asyncio.Event())

    assert pending is not None
    assert pending.data == b"raced-frame"
    assert pending.source_ack_owed is False
    await asyncio.wait_for(audio_in.join(), timeout=1)


@pytest.mark.asyncio
async def test_external_cancellation_transfers_blocked_frame_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    blocked_send = asyncio.Event()
    session = _FakeSDKSession(send_gate=blocked_send)
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    await audio_in.put(b"cancelled-frame")

    task = asyncio.create_task(
        wrapper.run(audio_in, asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(session.send_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert audio_in.empty()
    assert [pending.data for pending in wrapper._replay_audio] == [b"cancelled-frame"]
    assert wrapper._replay_audio[0].source_ack_owed is False
    await asyncio.wait_for(audio_in.join(), timeout=1)


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


def test_initial_handle_seeds_resumption_handle() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model", "de-DE", [], "secret", initial_handle="seeded-handle"
    )

    assert wrapper._resumption_handle == "seeded-handle"


@pytest.mark.asyncio
async def test_resumable_update_invokes_on_handle_update_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    first = _FakeSDKSession(
        turns=[
            [
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        new_handle="h2",
                        resumable=True,
                    ),
                ),
                types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")),
            ]
        ]
    )
    second = _FakeSDKSession()
    live = _FakeLive([first, second])
    _install_fake_client(monkeypatch, live)
    updates: list[str | None] = []
    wrapper = GeminiLiveSession(
        "model", "de-DE", [], "secret", on_handle_update=updates.append
    )

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(live.second_connect_called.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert updates == ["h2"]


@pytest.mark.asyncio
async def test_non_resumable_update_invokes_on_handle_update_with_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import (
        GeminiLiveSession,
        LiveFallbackRequired,
    )

    session = _FakeSDKSession(
        turns=[
            [
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        new_handle="resume-stale",
                        resumable=True,
                    )
                ),
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        resumable=False,
                    )
                ),
                types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)
    updates: list[str | None] = []
    wrapper = GeminiLiveSession(
        "model", "de-DE", [], "secret", on_handle_update=updates.append
    )

    with pytest.raises(LiveFallbackRequired):
        await wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())

    assert updates == ["resume-stale", None]


@pytest.mark.asyncio
async def test_handle_update_callback_exception_does_not_break_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    output_audio = b"\x01\x02\x03\x04"
    session = _FakeSDKSession(
        turns=[
            [
                types.LiveServerMessage(
                    session_resumption_update=types.LiveServerSessionResumptionUpdate(
                        new_handle="h1",
                        resumable=True,
                    )
                ),
                _audio_message(output_audio),
            ]
        ]
    )
    live = _FakeLive([session])
    _install_fake_client(monkeypatch, live)

    def failing_callback(_handle: str | None) -> None:
        raise RuntimeError("callback boom")

    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper = GeminiLiveSession(
        "model", "de-DE", [], "secret", on_handle_update=failing_callback
    )

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())

    assert {"type": "audio", "data": output_audio} in emitted
    assert wrapper._resumption_handle == "h1"


@pytest.mark.asyncio
async def test_mode_live_event_emitted_once_across_go_away_reconnect(
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
                ),
                types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")),
            ]
        ]
    )
    second = _FakeSDKSession()
    live = _FakeLive([first, second])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(live.second_connect_called.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    mode_live_events = [
        event for event in emitted if event == {"type": "mode", "value": "live"}
    ]
    assert len(mode_live_events) == 1


@pytest.mark.asyncio
async def test_non_blocking_call_does_not_block_a_later_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated NON_BLOCKING call must not stall the receive loop.

    A later scripted turn (a plain audio message) must arrive on
    ``events_out`` while the delegate call is still gated — proving the
    result was never awaited inline.
    """

    from hermes_cli.voice_live_session import GeminiLiveSession

    gate = asyncio.Event()
    audio_chunk = b"\x11\x22"
    sdk_session = _FakeSDKSession(
        turns=[
            [_delegate_tool_call_message(call_id="delegate-1")],
            [_audio_message(audio_chunk)],
        ]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    executor = _GatedNonBlockingExecutor(gate=gate, result={"result": "erledigt"})
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, executor)
    )

    collected: list[dict[str, Any]] = []
    while True:
        event = await asyncio.wait_for(events_out.get(), timeout=1)
        collected.append(event)
        if event.get("type") == "audio":
            break

    assert collected[-1] == {"type": "audio", "data": audio_chunk}
    # Still gated: the delegate call has not resolved, so no response was
    # sent yet — the audio turn genuinely arrived first, not just first in
    # a pre-buffered queue.
    assert sdk_session.tool_responses == []

    gate.set()
    await _wait_until(lambda: sdk_session.tool_responses)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    while not events_out.empty():
        collected.append(events_out.get_nowait())

    assert len(sdk_session.tool_responses) == 1
    response = sdk_session.tool_responses[0][0]
    assert response.id == "delegate-1"
    assert response.name == "delegate_to_hermes"
    assert response.response == {"result": "erledigt"}
    assert response.scheduling == types.FunctionResponseScheduling.INTERRUPT
    assert {"type": "state", "value": "thinking"} not in collected


@pytest.mark.asyncio
async def test_non_blocking_call_error_result_uses_when_idle_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    gate = asyncio.Event()
    gate.set()
    sdk_session = _FakeSDKSession(
        turns=[[_delegate_tool_call_message(call_id="delegate-err")]]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    executor = _GatedNonBlockingExecutor(
        gate=gate,
        result={"error": {"code": "delegation_failed", "message": "boom"}},
    )
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, executor)
    )
    await _wait_until(lambda: sdk_session.tool_responses)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sdk_session.tool_responses) == 1
    response = sdk_session.tool_responses[0][0]
    assert response.id == "delegate-err"
    assert response.response == {
        "error": {"code": "delegation_failed", "message": "boom"}
    }
    assert response.scheduling == types.FunctionResponseScheduling.WHEN_IDLE


@pytest.mark.asyncio
async def test_non_blocking_cap_rejects_third_concurrent_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    gate = asyncio.Event()  # never set: the first two calls stay in flight
    sdk_session = _FakeSDKSession(
        turns=[[_delegate_tool_call_message_multi(["d1", "d2", "d3"])]]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    executor = _GatedNonBlockingExecutor(gate=gate)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, executor)
    )
    await _wait_until(lambda: sdk_session.tool_responses)
    await _wait_until(lambda: len(wrapper._pending_tool_tasks) == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sdk_session.tool_responses) == 1
    batch = sdk_session.tool_responses[0]
    assert len(batch) == 1
    rejected = batch[0]
    assert rejected.id == "d3"
    assert rejected.response == {
        "error": {
            "code": "non_blocking_cap_reached",
            "message": (
                "Es laufen bereits zwei Hintergrund-Aufgaben. "
                "Bitte warte, bis eine fertig ist."
            ),
        }
    }
    assert rejected.scheduling is None


@pytest.mark.asyncio
async def test_pending_non_blocking_task_cancelled_when_run_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    gate = asyncio.Event()  # never set: the call never resolves on its own
    sdk_session = _FakeSDKSession(
        turns=[[_delegate_tool_call_message(call_id="delegate-1")]]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    executor = _GatedNonBlockingExecutor(gate=gate)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, executor)
    )
    await _wait_until(lambda: wrapper._pending_tool_tasks)
    pending_task = next(iter(wrapper._pending_tool_tasks))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pending_task.cancelled()
    assert wrapper._pending_tool_tasks == set()


@pytest.mark.asyncio
async def test_run_with_text_in_sends_client_content_with_turn_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    text_in: asyncio.Queue[str] = asyncio.Queue()
    await text_in.put("Hallo Hermes")

    task = asyncio.create_task(
        wrapper.run(
            asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor(), text_in=text_in
        )
    )
    await asyncio.wait_for(sdk_session.client_content_sent.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sdk_session.client_contents) == 1
    turns, turn_complete = sdk_session.client_contents[0]
    assert turn_complete is True
    assert turns.role == "user"
    assert len(turns.parts) == 1
    assert turns.parts[0].text == "Hallo Hermes"


@pytest.mark.asyncio
async def test_text_drain_stops_cleanly_and_does_not_keep_draining_after_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked drain task would silently keep calling ``send_client_content``
    on the (now-exited) session forever — assert it does not."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    text_in: asyncio.Queue[str] = asyncio.Queue()
    await text_in.put("erste Nachricht")

    task = asyncio.create_task(
        wrapper.run(
            asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor(), text_in=text_in
        )
    )
    await asyncio.wait_for(sdk_session.client_content_sent.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await text_in.put("sollte nach dem Teardown nicht mehr gesendet werden")
    await asyncio.sleep(0.05)

    assert len(sdk_session.client_contents) == 1


@pytest.mark.asyncio
async def test_run_without_text_in_never_calls_send_client_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    output_audio = b"\x01\x02"
    sdk_session = _FakeSDKSession(turns=[[_audio_message(output_audio)]])
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(sdk_session.receive_reentered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sdk_session.client_contents == []

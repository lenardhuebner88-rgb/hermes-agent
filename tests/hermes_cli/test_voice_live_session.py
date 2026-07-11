"""Behavior tests for the Gemini Live voice-session wrapper."""

from __future__ import annotations

from array import array
import asyncio
from io import BytesIO
import json
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

import pytest
from google import genai
from google.genai import errors, types
from PIL import Image, ImageDraw

FIXTURE_VIDEO = Path(__file__).parent / "fixtures" / "vision_marker.jpg"


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
        self.video_inputs: list[types.Blob] = []
        self.text_inputs: list[str] = []
        self.video_sent = asyncio.Event()
        self.tool_responses: list[list[types.FunctionResponse]] = []
        self.client_contents: list[tuple[types.Content, bool]] = []
        self.client_content_sent = asyncio.Event()
        # Records "audio"/"video"/"text" in the exact order the SDK actually
        # received them, so tests can assert relative ordering (e.g. a video
        # flush landing before the audio/text send that triggered it) instead
        # of only checking each list's final contents.
        self.call_order: list[str] = []

    async def send_client_content(
        self, *, turns: types.Content, turn_complete: bool = True
    ) -> None:
        self.call_order.append("text")
        self.client_contents.append((turns, turn_complete))
        self.client_content_sent.set()

    async def send_realtime_input(
        self,
        *,
        audio: types.Blob | None = None,
        video: types.Blob | None = None,
        text: str | None = None,
    ) -> None:
        self.send_started.set()
        if self._send_gate is not None:
            await self._send_gate.wait()
        if self._send_error is not None:
            raise self._send_error
        if video is not None:
            self.call_order.append("video")
            self.video_inputs.append(video)
            self.video_sent.set()
            return
        if text is not None:
            self.call_order.append("realtime_text")
            self.text_inputs.append(text)
            return
        self.call_order.append("audio")
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


def _loud_pcm_frame() -> bytes:
    """A 3200-byte PCM16 frame at constant amplitude +-2000 (RMS 2000)."""
    return array("h", [2000, -2000] * 800).tobytes()


def _quiet_pcm_frame() -> bytes:
    """A 3200-byte all-zero PCM16 frame (RMS 0)."""
    return bytes(3200)


def _reencode_fixture(*, quality: int) -> bytes:
    output = BytesIO()
    with Image.open(FIXTURE_VIDEO) as image:
        image.save(output, format="JPEG", quality=quality)
    return output.getvalue()


def _changed_fixture() -> bytes:
    output = BytesIO()
    with Image.open(FIXTURE_VIDEO) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image.width // 2, image.height // 2), fill="black")
    image.save(output, format="JPEG", quality=70)
    return output.getvalue()


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
    assert config.context_window_compression.trigger_tokens == 25_000
    assert config.context_window_compression.sliding_window.target_tokens == 10_000


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
    from hermes_cli.voice_live_session import GeminiLiveSession

    # video_mode defaults to "on_demand", so the persona is the on-demand
    # variant (no automatic still, see the dedicated on_demand test below) —
    # this test pins the mode-independent bits (voice, search-tool absence,
    # transcription config).
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    config = wrapper._connect_config()

    assert (
        config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Puck"
    )
    # google_search_enabled defaults False: no search tool, and the default
    # persona must not promise a capability it doesn't have.
    assert "Google-Suche" not in config.system_instruction
    assert isinstance(config.input_audio_transcription, types.AudioTranscriptionConfig)
    assert isinstance(config.output_audio_transcription, types.AudioTranscriptionConfig)
    assert config.tools == []
    # look_closely is a static persona fragment, unconditional (unlike
    # google_search, it is always declared in FUNCTION_DECLARATIONS).
    assert "look_closely" in config.system_instruction
    # recall_memory (long-term memory) is likewise a static, unconditional
    # persona fragment.
    assert "recall_memory" in config.system_instruction


def test_connect_config_google_search_enabled_keeps_tool_and_persona_sentence() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        google_search_enabled=True,
        video_mode="stream",
    )
    config = wrapper._connect_config()

    from hermes_cli.voice_live_session import DEFAULT_SYSTEM_INSTRUCTION

    assert config.system_instruction == DEFAULT_SYSTEM_INSTRUCTION
    assert "Google-Suche" in config.system_instruction
    assert len(config.tools) == 1
    assert config.tools[0].google_search is not None


def test_connect_config_stream_video_mode_keeps_auto_still_promise() -> None:
    """video_mode="stream": the default persona's original wording is untouched."""

    from hermes_cli.voice_live_session import (
        _DEFAULT_SYSTEM_INSTRUCTION_NO_SEARCH,
        GeminiLiveSession,
    )

    wrapper = GeminiLiveSession("model", "de-DE", [], "secret", video_mode="stream")
    config = wrapper._connect_config()

    assert config.system_instruction == _DEFAULT_SYSTEM_INSTRUCTION_NO_SEARCH
    assert "ein aktuelles Standbild" in config.system_instruction
    assert "KEIN automatisches Standbild" not in config.system_instruction
    assert "recall_memory" in config.system_instruction


def test_connect_config_on_demand_video_mode_drops_auto_still_promise() -> None:
    """video_mode="on_demand" (the default): no automatic still is promised.

    Instead the persona is rewritten to point at look_closely for anything
    visual, matching the fact that _VideoFrameRelay.forward_to_live is False
    in this mode — the auto-still sentence would otherwise be a lie.
    """

    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model", "de-DE", [], "secret", video_mode="on_demand"
    )
    config = wrapper._connect_config()

    assert "ein aktuelles Standbild" not in config.system_instruction
    assert "KEIN automatisches Standbild" in config.system_instruction
    assert "look_closely" in config.system_instruction
    assert "rate niemals blind" in config.system_instruction
    assert "recall_memory" in config.system_instruction


def test_connect_config_on_demand_custom_system_instruction_never_rewritten() -> None:
    """A caller-supplied system_instruction is left untouched in on_demand too."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        system_instruction="Custom persona, ein aktuelles Standbild mentioned here.",
        video_mode="on_demand",
    )
    config = wrapper._connect_config()

    assert (
        config.system_instruction
        == "Custom persona, ein aktuelles Standbild mentioned here."
    )


def test_connect_config_context_suffix_appended_after_default_persona() -> None:
    """The suffix lands after every fragment-toggle rewrite, default persona intact."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        context_suffix="[Aktueller Arbeitskontext aus Piets letzten Sessions]\nFoo.",
    )
    config = wrapper._connect_config()

    assert config.system_instruction.endswith(
        "[Aktueller Arbeitskontext aus Piets letzten Sessions]\nFoo."
    )
    # video_mode default (on_demand) fragment-toggle rewrite must still have
    # fired — the suffix is appended AFTER it, not instead of it.
    assert "KEIN automatisches Standbild" in config.system_instruction


def test_connect_config_context_suffix_appended_to_custom_system_instruction() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        system_instruction="Custom persona.",
        context_suffix="Extra context.",
    )
    config = wrapper._connect_config()

    assert config.system_instruction == "Custom persona.\n\nExtra context."


def test_connect_config_empty_context_suffix_leaves_instruction_unchanged() -> None:
    from hermes_cli.voice_live_session import DEFAULT_SYSTEM_INSTRUCTION, GeminiLiveSession

    wrapper = GeminiLiveSession("model", "de-DE", [], "secret", context_suffix="")
    config = wrapper._connect_config()

    assert not config.system_instruction.endswith("\n\n")
    assert "Aktueller Arbeitskontext" not in config.system_instruction
    assert config.system_instruction != DEFAULT_SYSTEM_INSTRUCTION  # on_demand rewrite


def test_connect_config_carries_configured_voice_persona_and_both_tools() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [{"name": "list_terminals"}],
        "secret",
        voice="Charon",
        system_instruction="Custom persona text.",
        google_search_enabled=True,
    )
    config = wrapper._connect_config()

    assert (
        config.speech_config.voice_config.prebuilt_voice_config.voice_name
        == "Charon"
    )
    # Custom system_instruction is the caller's own wording and is never
    # rewritten, even when google_search_enabled changes the tool list.
    assert config.system_instruction == "Custom persona text."
    assert isinstance(config.input_audio_transcription, types.AudioTranscriptionConfig)
    assert isinstance(config.output_audio_transcription, types.AudioTranscriptionConfig)
    assert len(config.tools) == 2
    assert config.tools[0].function_declarations[0].name == "list_terminals"
    assert config.tools[1].google_search is not None


def test_connect_config_custom_system_instruction_never_rewritten_without_search() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        system_instruction="Custom persona text mentions Google-Suche too.",
        google_search_enabled=False,
    )
    config = wrapper._connect_config()

    assert config.system_instruction == "Custom persona text mentions Google-Suche too."
    assert config.tools == []


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
        event
        for event in emitted
        if event.get("type") == "mode" and event.get("value") == "live"
    ]
    assert len(mode_live_events) == 1
    assert mode_live_events[0]["video_mode"] == "on_demand"


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
async def test_silent_non_blocking_result_sends_one_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession

    monkeypatch.setattr(vls, "_NON_BLOCKING_NUDGE_SECONDS", 0.01)
    gate = asyncio.Event()
    gate.set()
    sdk_session = _FakeSDKSession(
        turns=[[_delegate_tool_call_message(call_id="delegate-nudge")]]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")

    task = asyncio.create_task(
        wrapper.run(
            asyncio.Queue(),
            asyncio.Queue(),
            _GatedNonBlockingExecutor(gate=gate),
        )
    )
    await _wait_until(lambda: sdk_session.text_inputs)

    assert len(sdk_session.tool_responses) == 1
    assert sdk_session.text_inputs == [vls._NON_BLOCKING_NUDGE_TEXT]

    # The observation task exits after its one best-effort send; it never
    # repeats while the connection remains quiet.
    await asyncio.sleep(0.03)
    assert sdk_session.text_inputs == [vls._NON_BLOCKING_NUDGE_TEXT]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_output",
    [
        _audio_message(b"\x01\x02"),
        _output_transcription_message(text="Ergebnis", finished=False),
    ],
    ids=["audio", "output-transcript"],
)
async def test_immediate_model_output_suppresses_non_blocking_result_nudge(
    monkeypatch: pytest.MonkeyPatch,
    model_output: types.LiveServerMessage,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession

    monkeypatch.setattr(vls, "_NON_BLOCKING_NUDGE_SECONDS", 0.03)
    gate = asyncio.Event()
    gate.set()
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = sdk_session
    call = types.FunctionCall(
        id="delegate-output",
        name="delegate_to_hermes",
        args={"prompt": "erledige das"},
    )

    await wrapper._run_non_blocking_call(
        call,
        _GatedNonBlockingExecutor(gate=gate),
    )
    await wrapper._handle_message(
        sdk_session,
        model_output,
        asyncio.Queue(),
        _FakeToolExecutor(),
        None,
    )
    await asyncio.sleep(0.06)

    assert len(sdk_session.tool_responses) == 1
    assert sdk_session.text_inputs == []
    assert wrapper._pending_nudge_tasks == set()


@pytest.mark.asyncio
async def test_continuation_of_existing_model_turn_does_not_suppress_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession

    monkeypatch.setattr(vls, "_NON_BLOCKING_NUDGE_SECONDS", 0.03)
    gate = asyncio.Event()
    gate.set()
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = sdk_session
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # A model turn was already speaking when the background result landed.
    await wrapper._handle_message(
        sdk_session,
        _audio_message(b"first chunk"),
        events_out,
        _FakeToolExecutor(),
        None,
    )
    await wrapper._run_non_blocking_call(
        types.FunctionCall(
            id="delegate-mid-turn",
            name="delegate_to_hermes",
            args={"prompt": "erledige das"},
        ),
        _GatedNonBlockingExecutor(gate=gate),
    )

    # More audio from that same turn is not a new output turn and therefore
    # must not hide a delegation result that still needs announcing.
    await wrapper._handle_message(
        sdk_session,
        _audio_message(b"second chunk"),
        events_out,
        _FakeToolExecutor(),
        None,
    )
    await wrapper._handle_message(
        sdk_session,
        _turn_complete_message(),
        events_out,
        _FakeToolExecutor(),
        None,
    )
    await _wait_until(lambda: sdk_session.text_inputs)

    assert sdk_session.text_inputs == [vls._NON_BLOCKING_NUDGE_TEXT]


@pytest.mark.asyncio
async def test_reconnect_suppresses_non_blocking_result_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession

    monkeypatch.setattr(vls, "_NON_BLOCKING_NUDGE_SECONDS", 0.03)
    gate = asyncio.Event()
    gate.set()
    first = _FakeSDKSession()
    replacement = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = first
    call = types.FunctionCall(
        id="delegate-reconnect",
        name="delegate_to_hermes",
        args={"prompt": "erledige das"},
    )

    await wrapper._run_non_blocking_call(
        call,
        _GatedNonBlockingExecutor(gate=gate),
    )
    wrapper._active_session = replacement
    await asyncio.sleep(0.06)

    assert len(first.tool_responses) == 1
    assert first.text_inputs == []
    assert replacement.text_inputs == []
    assert wrapper._pending_nudge_tasks == set()


@pytest.mark.asyncio
async def test_cancelling_run_cancels_pending_non_blocking_result_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession

    monkeypatch.setattr(vls, "_NON_BLOCKING_NUDGE_SECONDS", 60.0)
    gate = asyncio.Event()
    gate.set()
    sdk_session = _FakeSDKSession(
        turns=[[_delegate_tool_call_message(call_id="delegate-cancel-nudge")]]
    )
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    task = asyncio.create_task(
        wrapper.run(
            asyncio.Queue(),
            asyncio.Queue(),
            _GatedNonBlockingExecutor(gate=gate),
        )
    )
    await _wait_until(lambda: wrapper._pending_nudge_tasks)
    nudge_task = next(iter(wrapper._pending_nudge_tasks))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert nudge_task.cancelled()
    assert wrapper._pending_nudge_tasks == set()
    assert sdk_session.text_inputs == []


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


@pytest.mark.asyncio
async def test_run_with_video_in_offers_frame_and_speech_onset_flushes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """video_mode="stream" regression: offering a frame alone never sends it
    upstream (relay-on-activity, see ``_VideoFrameRelay``): only a
    user-activity burst — here loud speech — flushes the held frame, landing
    before the audio frame that triggered it and surfacing a
    ``video_frame_sent`` observability event. Explicit ``video_mode="stream"``
    since the default is "on_demand" (see the on_demand companion test
    below), which never forwards stills into the Live session at all."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret", video_mode="stream")
    video_in: asyncio.Queue[bytes] = asyncio.Queue()
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    frame = FIXTURE_VIDEO.read_bytes()

    task = asyncio.create_task(
        wrapper.run(audio_in, events_out, _FakeToolExecutor(), video_in=video_in)
    )
    # Queued only after connecting so the per-connect drain (see
    # ``_drain_video_queue``) does not discard it before ``_send_video``
    # ever gets to it.
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    await video_in.put(frame)
    await _wait_until(lambda: video_in.empty())
    assert sdk_session.video_inputs == []  # offering alone never sends

    await audio_in.put(_loud_pcm_frame())
    await asyncio.wait_for(sdk_session.expected_audio_sent.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sdk_session.video_inputs) == 1
    blob = sdk_session.video_inputs[0]
    assert blob.data == frame
    assert blob.mime_type == "image/jpeg"
    assert sdk_session.call_order.index("video") < sdk_session.call_order.index("audio")

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    assert {"type": "video_frame_sent"} in emitted


@pytest.mark.asyncio
async def test_run_with_video_in_on_demand_mode_never_flushes_to_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """video_mode="on_demand" (the default): loud speech never flushes a
    still into the Live session — that's the whole point of the mode, only
    look_closely may spend Live prompt budget on an image. peek() (the
    delegate_to_hermes still-frame path) keeps working regardless."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")  # default: on_demand
    video_in: asyncio.Queue[bytes] = asyncio.Queue()
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    frame = FIXTURE_VIDEO.read_bytes()

    task = asyncio.create_task(
        wrapper.run(audio_in, events_out, _FakeToolExecutor(), video_in=video_in)
    )
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    await video_in.put(frame)
    await _wait_until(lambda: video_in.empty())
    assert wrapper._video_relay.peek() == frame

    await audio_in.put(_loud_pcm_frame())
    await asyncio.wait_for(sdk_session.expected_audio_sent.wait(), timeout=1)
    relay = wrapper._video_relay
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sdk_session.video_inputs == []
    # peek() (delegate_to_hermes' still-frame path) is unaffected by the
    # send-suppression above — it never went through flush()/take_for_turn().
    assert relay.peek() == frame

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    assert {"type": "video_frame_sent"} not in emitted


@pytest.mark.asyncio
async def test_video_drain_stops_cleanly_and_does_not_keep_draining_after_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked drain task would silently keep pulling frames off
    ``video_in`` forever after the wrapper is cancelled — assert it does
    not."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    video_in: asyncio.Queue[bytes] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(
            asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor(), video_in=video_in
        )
    )
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    await video_in.put(b"\xff\xd8erstes-bild")
    await _wait_until(lambda: video_in.empty())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await video_in.put(b"\xff\xd8sollte-nicht-mehr-gedraint-werden")
    await asyncio.sleep(0.05)

    assert video_in.qsize() == 1


def test_drain_video_queue_clears_all_queued_frames() -> None:
    """Direct unit test of the mechanism ``run()`` calls at every (re)connect
    iteration: a frame sitting unconsumed in ``video_in`` must not survive
    into whichever session connects next."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    video_in: asyncio.Queue[bytes] = asyncio.Queue()
    video_in.put_nowait(b"\xff\xd8one")
    video_in.put_nowait(b"\xff\xd8two")

    GeminiLiveSession._drain_video_queue(video_in)

    assert video_in.empty()


def test_drain_video_queue_accepts_none() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    GeminiLiveSession._drain_video_queue(None)


@pytest.mark.asyncio
async def test_video_offered_but_unflushed_frame_is_not_replayed_after_go_away_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike audio (see ``test_go_away_replays_blocked_frame_first_after_
    reconnect``), a video frame offered to the relay but never flushed
    before a go-away reconnect lands is simply dropped: ``run()`` clears the
    relay (see ``_VideoFrameRelay.clear``) at the start of every (re)connect
    iteration, so it must never reach the second session. A fresh frame
    offered and flushed after the reconnect must still reach the new session
    normally. Explicit ``video_mode="stream"`` — the default "on_demand"
    never flushes to Live at all (see the dedicated on_demand test)."""

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
    # The gated quiet frame below is cancelled mid-send on the first session
    # and, per the class's at-least-once audio replay contract, is replayed
    # as the *first* frame the second session receives — before the loud
    # frame this test puts on ``audio_in`` after the reconnect. Two expected
    # sends lets the wait below observe the loud replay-successor landing,
    # not just the replayed quiet frame.
    second = _FakeSDKSession(expected_sends=2)
    live = _FakeLive([first, second])
    _install_fake_client(monkeypatch, live)
    monkeypatch.setattr(
        voice_live_session,
        "_SENDER_HANDOFF_SECONDS",
        0.01,
        raising=False,
    )
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret", video_mode="stream")
    video_in: asyncio.Queue[bytes] = asyncio.Queue()
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(audio_in, asyncio.Queue(), _FakeToolExecutor(), video_in=video_in)
    )
    # Queued only after the first connect so ``run()``'s per-iteration drain
    # (see ``_drain_video_queue``) does not remove it before the session
    # even exists — this frame must instead be dropped by the reconnect's
    # relay clear, which is the behavior under test.
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    stale_frame = FIXTURE_VIDEO.read_bytes()
    await video_in.put(stale_frame)
    await _wait_until(lambda: video_in.empty())

    # A quiet audio frame never triggers a flush itself, but its send blocks
    # on the gate — satisfying ``wait_for_send_before_receive`` so the
    # go-away message is only delivered once this send has started,
    # deterministically ordering "frame offered" before "reconnect happens"
    # without racing.
    await audio_in.put(_quiet_pcm_frame())
    await asyncio.wait_for(first.send_started.wait(), timeout=1)
    await asyncio.wait_for(live.second_connect_called.wait(), timeout=1)

    fresh_frame = b"\xff\xd8fresh-frame"
    await video_in.put(fresh_frame)
    await _wait_until(lambda: video_in.empty())
    await audio_in.put(_loud_pcm_frame())
    await asyncio.wait_for(second.expected_audio_sent.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert first.video_inputs == []
    assert [blob.data for blob in second.video_inputs] == [fresh_frame]


@pytest.mark.asyncio
async def test_video_relay_offer_then_flush_sends_once_second_flush_is_noop() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)
    frame = FIXTURE_VIDEO.read_bytes()

    relay.offer(frame)
    sent = await relay.flush(sdk_session)

    assert sent is True
    assert len(sdk_session.video_inputs) == 1
    blob = sdk_session.video_inputs[0]
    assert blob.data == frame
    assert blob.mime_type == "image/jpeg"

    sent_again = await relay.flush(sdk_session)

    assert sent_again is False
    assert len(sdk_session.video_inputs) == 1


def test_watch_detector_ignores_same_and_jpeg_noise_but_detects_marker_change() -> None:
    from hermes_cli.voice_live_session import _jpeg_frame_changed

    original = FIXTURE_VIDEO.read_bytes()

    assert _jpeg_frame_changed(original, original) is False
    assert _jpeg_frame_changed(original, _reencode_fixture(quality=30)) is False
    assert _jpeg_frame_changed(original, _changed_fixture()) is True


def test_watch_requires_current_share_and_stop_is_idempotent() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)

    result = relay.start_watching("Prüfe den Build")

    assert result["error"]["code"] == "watch_requires_sharing"
    assert relay.stop_watching() == {"watching": False, "was_watching": False}


def test_watch_cooldown_and_maximum_three_notifications() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    relay = _VideoFrameRelay(asyncio.Queue())
    original = FIXTURE_VIDEO.read_bytes()
    changed = _changed_fixture()
    relay.offer(original, now=0.0)
    assert relay.start_watching("Prüfe den Build")["watching"] is True

    first = relay.offer(changed, now=1.0)
    assert first is not None
    relay.mark_watch_notification_sent(first, now=1.0)
    cooldown_suppressed = relay.offer(original, now=10.0)
    # The changed state remains stable. Once cooldown expires, the pending
    # event must still fire instead of being lost when its frame became the
    # detector baseline.
    second = relay.offer(original, now=31.0)
    assert second is not None
    relay.mark_watch_notification_sent(second, now=31.0)
    third = relay.offer(changed, now=62.0)
    assert third is not None
    relay.mark_watch_notification_sent(third, now=62.0)
    capped = relay.offer(original, now=93.0)

    assert cooldown_suppressed is None
    assert capped is None


def test_sharing_stop_clears_latest_frame_and_active_watch() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    assert relay.start_watching("Prüfe den Build")["watching"] is True

    relay.sharing_stopped()

    assert relay.peek() is None
    assert relay.stop_watching() == {"watching": False, "was_watching": False}
    assert relay.start_watching("Prüfe weiter")["error"]["code"] == (
        "watch_requires_sharing"
    )


def test_reconnect_clear_rearms_inflight_watch_notification() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    relay = _VideoFrameRelay(asyncio.Queue())
    original = FIXTURE_VIDEO.read_bytes()
    changed = _changed_fixture()
    relay.offer(original, now=0.0)
    relay.start_watching("Prüfe den Build")
    reserved = relay.offer(changed, now=1.0)
    assert reserved is not None

    relay.clear()
    retried = relay.offer(changed, now=2.0)

    assert retried is not None


@pytest.mark.asyncio
async def test_watch_change_sends_exactly_one_current_still_then_realtime_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    monkeypatch.setattr(vls, "_WATCH_IMAGE_SETTLE_SECONDS", 0.0)
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = sdk_session
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("ob der Build fertig ist")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    changed = _changed_fixture()
    await video_in.put(changed)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        wrapper._send_video(sdk_session, relay, video_in, stop_event)
    )

    await _wait_until(lambda: sdk_session.text_inputs)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert sdk_session.call_order == ["video", "realtime_text"]
    assert [blob.data for blob in sdk_session.video_inputs] == [changed]
    assert sdk_session.text_inputs == [
        "[System] Das geteilte Bild hat sich deutlich geändert. "
        "Prüfe: ob der Build fertig ist. Melde dich nur, wenn relevant."
    ]
    assert events_out.get_nowait() == {"type": "watch_notification_sent"}


@pytest.mark.asyncio
async def test_watch_change_on_demand_relay_sends_no_image_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """video_mode="on_demand" (``forward_to_live=False``): a detected change
    must not send a blind still into the Live session, and the notification
    text must say so explicitly and point at look_closely — see Codex-R2
    finding #2 (a notification implying sight while none was sent)."""

    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    monkeypatch.setattr(vls, "_WATCH_IMAGE_SETTLE_SECONDS", 0.0)
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret", video_mode="on_demand")
    wrapper._active_session = sdk_session
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out, forward_to_live=False)
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("ob der Build fertig ist")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    changed = _changed_fixture()
    await video_in.put(changed)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        wrapper._send_video(sdk_session, relay, video_in, stop_event)
    )

    await _wait_until(lambda: sdk_session.text_inputs)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert sdk_session.call_order == ["realtime_text"]  # no "video" call at all
    assert sdk_session.video_inputs == []
    assert len(sdk_session.text_inputs) == 1
    text = sdk_session.text_inputs[0]
    assert "KEIN Bild" in text
    assert "look_closely" in text
    assert "ob der Build fertig ist" in text
    assert relay.images_sent == 0
    assert events_out.get_nowait() == {"type": "watch_notification_sent"}


@pytest.mark.asyncio
async def test_watch_pair_blocks_microphone_interleaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    monkeypatch.setattr(vls, "_WATCH_IMAGE_SETTLE_SECONDS", 0.03)
    monkeypatch.setattr(vls, "_WATCH_USER_QUIET_SECONDS", 0.0)
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = sdk_session
    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("Prüfe den Build")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    stop_event = asyncio.Event()
    await video_in.put(_changed_fixture())

    video_task = asyncio.create_task(
        wrapper._send_video(sdk_session, relay, video_in, stop_event)
    )
    await asyncio.wait_for(sdk_session.video_sent.wait(), timeout=1)
    await audio_in.put(_quiet_pcm_frame())
    audio_task = asyncio.create_task(
        wrapper._send_audio(sdk_session, audio_in, stop_event, relay)
    )

    await asyncio.sleep(0.01)
    assert sdk_session.call_order == ["video"]
    await _wait_until(lambda: sdk_session.text_inputs and sdk_session.realtime_inputs)
    stop_event.set()
    await asyncio.wait_for(video_task, timeout=1)
    await asyncio.wait_for(audio_task, timeout=1)

    assert sdk_session.call_order == ["video", "realtime_text", "audio"]


@pytest.mark.asyncio
async def test_watch_defers_and_retries_if_speech_starts_during_image_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    monkeypatch.setattr(vls, "_WATCH_IMAGE_SETTLE_SECONDS", 0.03)
    monkeypatch.setattr(vls, "_WATCH_USER_QUIET_SECONDS", 0.05)
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = sdk_session
    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("Prüfe den Build")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    stop_event = asyncio.Event()
    changed = _changed_fixture()
    await video_in.put(changed)

    video_task = asyncio.create_task(
        wrapper._send_video(sdk_session, relay, video_in, stop_event)
    )
    await asyncio.wait_for(sdk_session.video_sent.wait(), timeout=1)
    await audio_in.put(_loud_pcm_frame())
    audio_task = asyncio.create_task(
        wrapper._send_audio(sdk_session, audio_in, stop_event, relay)
    )
    await asyncio.wait_for(sdk_session.audio_sent.wait(), timeout=1)

    assert sdk_session.text_inputs == []
    assert sdk_session.call_order == ["video", "audio"]

    # A stable follow-up frame must retry the still-pending notification once
    # the user has gone quiet; the first reservation was not consumed.
    sdk_session.video_sent.clear()
    await video_in.put(changed)
    await _wait_until(lambda: sdk_session.text_inputs)
    stop_event.set()
    await asyncio.wait_for(video_task, timeout=1)
    await asyncio.wait_for(audio_task, timeout=1)

    assert sdk_session.call_order == [
        "video",
        "audio",
        "video",
        "realtime_text",
    ]


@pytest.mark.asyncio
async def test_watch_unchanged_noisy_frame_makes_no_upstream_call() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("Prüfe den Build")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    await video_in.put(_reencode_fixture(quality=30))
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        wrapper._send_video(sdk_session, relay, video_in, stop_event)
    )

    await asyncio.sleep(0.03)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert sdk_session.video_inputs == []
    assert sdk_session.text_inputs == []
    assert sdk_session.call_order == []


@pytest.mark.asyncio
async def test_watch_settle_does_not_send_text_after_session_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    monkeypatch.setattr(vls, "_WATCH_IMAGE_SETTLE_SECONDS", 0.03)
    first = _FakeSDKSession()
    replacement = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = first
    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("Prüfe den Build")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    await video_in.put(_changed_fixture())
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        wrapper._send_video(first, relay, video_in, stop_event)
    )

    await asyncio.wait_for(first.video_sent.wait(), timeout=1)
    wrapper._active_session = replacement
    await asyncio.sleep(0.06)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert len(first.video_inputs) == 1
    assert first.text_inputs == []
    assert replacement.text_inputs == []


@pytest.mark.asyncio
async def test_watch_settle_cancellation_suppresses_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    monkeypatch.setattr(vls, "_WATCH_IMAGE_SETTLE_SECONDS", 60.0)
    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    wrapper._active_session = sdk_session
    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes(), now=0.0)
    relay.start_watching("Prüfe den Build")
    video_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    await video_in.put(_changed_fixture())
    task = asyncio.create_task(
        wrapper._send_video(sdk_session, relay, video_in, asyncio.Event())
    )

    await asyncio.wait_for(sdk_session.video_sent.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sdk_session.video_inputs) == 1
    assert sdk_session.text_inputs == []


@pytest.mark.asyncio
async def test_delegation_peeks_latest_frame_after_conversation_flush() -> None:
    """E2 gets the freshest shared still without consuming the turn relay.

    The speech-onset path commonly flushes the conversational copy before the
    model emits its delegation tool call. The separate latest-frame snapshot
    must therefore remain available to the worker attachment path.
    """
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay
    from tools.voice_live_tools import VOICE_FRAME_ARG

    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    relay = _VideoFrameRelay(asyncio.Queue())
    frame = FIXTURE_VIDEO.read_bytes()
    relay.offer(frame)
    assert await relay.flush(sdk_session) is True

    gate = asyncio.Event()
    executor = _GatedNonBlockingExecutor(gate=gate)
    call = types.FunctionCall(
        id="delegate-with-image",
        name="delegate_to_hermes",
        args={"prompt": "prüfe den sichtbaren Fehler"},
    )

    await wrapper._execute_tool_calls(sdk_session, [call], executor, relay)
    await _wait_until(lambda: executor.calls)

    assert executor.calls[0][0] == "delegate_to_hermes"
    assert executor.calls[0][1]["prompt"] == "prüfe den sichtbaren Fehler"
    assert executor.calls[0][1][VOICE_FRAME_ARG] == frame
    assert relay.peek() == frame

    await wrapper._cancel_tasks(*wrapper._pending_tool_tasks)


@pytest.mark.asyncio
async def test_video_relay_flushes_at_most_once_per_turn() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    relay = _VideoFrameRelay(asyncio.Queue())
    first_frame = b"\xff\xd8first"
    second_frame = b"\xff\xd8second"

    relay.offer(first_frame)
    assert await relay.flush(sdk_session) is True

    relay.offer(second_frame)
    assert await relay.flush(sdk_session) is False
    assert len(sdk_session.video_inputs) == 1

    relay.mark_turn_complete()
    assert await relay.flush(sdk_session) is True
    assert [blob.data for blob in sdk_session.video_inputs] == [
        first_frame,
        second_frame,
    ]


@pytest.mark.asyncio
async def test_video_relay_latest_offer_wins() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    relay = _VideoFrameRelay(asyncio.Queue())

    relay.offer(b"\xff\xd8stale")
    relay.offer(b"\xff\xd8fresh")

    assert await relay.flush(sdk_session) is True
    assert [blob.data for blob in sdk_session.video_inputs] == [b"\xff\xd8fresh"]


@pytest.mark.asyncio
async def test_video_relay_flush_emits_video_frame_sent_event() -> None:
    from hermes_cli.voice_live_session import _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)
    relay.offer(b"\xff\xd8frame")

    assert await relay.flush(sdk_session) is True

    assert events_out.get_nowait() == {"type": "video_frame_sent"}


@pytest.mark.asyncio
async def test_send_audio_quiet_frame_never_flushes_relay() -> None:
    """RMS below ``_SPEECH_RMS_THRESHOLD`` is not a speech-onset trigger, so
    a held frame stays held and only the audio itself reaches the SDK."""

    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    relay = _VideoFrameRelay(asyncio.Queue())
    relay.offer(FIXTURE_VIDEO.read_bytes())
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    await audio_in.put(_quiet_pcm_frame())
    stop_event = asyncio.Event()

    send_task = asyncio.create_task(
        wrapper._send_audio(sdk_session, audio_in, stop_event, relay)
    )
    await asyncio.wait_for(sdk_session.expected_audio_sent.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(send_task, timeout=1)

    assert sdk_session.video_inputs == []
    assert sdk_session.call_order == ["audio"]


@pytest.mark.asyncio
async def test_send_text_embeds_relay_frame_inline_into_the_turn() -> None:
    """Typed turns carry the still as an inline part of the turn itself.

    A realtime video Blob flushed 0 ms before ``send_client_content`` is not
    yet ingested when the turn generates — the model answers blind
    (live-probed 2026-07-10). So no realtime video send may happen here; the
    frame must arrive as ``inline_data`` inside the same Content, before the
    text part, and the relay must emit its observability event.
    """
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    sdk_session = _FakeSDKSession()
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)
    frame = FIXTURE_VIDEO.read_bytes()
    relay.offer(frame)
    text_in: asyncio.Queue[str] = asyncio.Queue()
    await text_in.put("Hallo Hermes")
    stop_event = asyncio.Event()

    send_task = asyncio.create_task(
        wrapper._send_text(sdk_session, text_in, stop_event, relay)
    )
    await asyncio.wait_for(sdk_session.client_content_sent.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(send_task, timeout=1)

    assert sdk_session.call_order == ["text"]
    assert sdk_session.video_inputs == []
    assert len(sdk_session.client_contents) == 1
    parts = sdk_session.client_contents[0][0].parts
    assert len(parts) == 2
    assert parts[0].inline_data is not None
    assert parts[0].inline_data.data == frame
    assert parts[0].inline_data.mime_type == "image/jpeg"
    assert parts[1].text == "Hallo Hermes"
    assert events_out.get_nowait() == {"type": "video_frame_sent"}

    # once-per-turn discipline holds for the inline path too
    relay.offer(frame)
    await text_in.put("Noch eine Frage")
    sdk_session.client_content_sent.clear()
    stop_event.clear()
    send_task = asyncio.create_task(
        wrapper._send_text(sdk_session, text_in, stop_event, relay)
    )
    await asyncio.wait_for(sdk_session.client_content_sent.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(send_task, timeout=1)
    assert len(sdk_session.client_contents[1][0].parts) == 1


def test_decode_video_frame_rejects_oversized_base64_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hardening in ``hermes_cli.voice_ws``: a multi-MB base64 string must
    never even reach ``base64.b64decode`` — this test lives here (not in
    ``tests/hermes_cli/test_voice_ws.py``) per this slice's file scope."""

    from hermes_cli import voice_ws

    decode_calls: list[str] = []
    original_b64decode = voice_ws.base64.b64decode

    def _spy_b64decode(*args: Any, **kwargs: Any) -> bytes:
        decode_calls.append("called")
        return original_b64decode(*args, **kwargs)

    monkeypatch.setattr(voice_ws.base64, "b64decode", _spy_b64decode)
    oversized = "A" * (voice_ws._MAX_VIDEO_FRAME_BYTES * 4 // 3 + 9)

    result = voice_ws._decode_video_frame({"source": "camera", "data": oversized})

    assert result is None
    assert decode_calls == []


@pytest.mark.asyncio
async def test_non_blocking_response_retries_on_a_swapped_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result landing on a dying connection is retried on its successor.

    A 600 s delegation can finish exactly while a go-away reconnect swaps the
    live connection: the first ``send_tool_response`` raises against the
    closing session and the result must NOT be dropped — it belongs to the
    replacement connection the same wrapper is about to run.
    """

    from hermes_cli import voice_live_session as vls
    from hermes_cli.voice_live_session import GeminiLiveSession

    monkeypatch.setattr(vls, "_NON_BLOCKING_SESSION_POLL_SECONDS", 0.01)

    class _ClosingSession:
        def __init__(self) -> None:
            self.calls = 0

        async def send_tool_response(self, *, function_responses):
            self.calls += 1
            raise ConnectionError("connection is closing")

    class _AcceptingSession:
        def __init__(self) -> None:
            self.responses: list[Any] = []

        async def send_tool_response(self, *, function_responses):
            self.responses.append(list(function_responses))

    class _InstantExecutor:
        async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"result": "spät fertig"}

        def is_non_blocking(self, name: str) -> bool:
            return True

    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    closing = _ClosingSession()
    accepting = _AcceptingSession()
    wrapper._active_session = closing

    call = types.FunctionCall(id="delegate-9", name="delegate_to_hermes", args={})
    task = asyncio.create_task(
        wrapper._run_non_blocking_call(call, _InstantExecutor())
    )
    await _wait_until(lambda: closing.calls >= 1)
    wrapper._active_session = accepting  # the go-away reconnect swap
    await asyncio.wait_for(task, timeout=5)

    assert closing.calls == 1
    assert len(accepting.responses) == 1
    delivered = accepting.responses[0][0]
    assert delivered.id == "delegate-9"
    assert delivered.response == {"result": "spät fertig"}
    assert delivered.scheduling == types.FunctionResponseScheduling.INTERRUPT


@pytest.mark.asyncio
async def test_flush_preserves_frame_offered_during_send() -> None:
    """A fresher frame offered while flush's send awaits must survive.

    offer() runs lock-free from the video-sender task; flush must only clear
    the slot if it still holds the frame it just sent (reviewer finding
    2026-07-10), so the next turn flushes the newer still instead of nothing.
    """
    from hermes_cli.voice_live_session import _VideoFrameRelay

    class _BlockingSession:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.sent: list[types.Blob] = []

        async def send_realtime_input(
            self,
            *,
            audio: types.Blob | None = None,
            video: types.Blob | None = None,
        ) -> None:
            if video is not None:
                self.sent.append(video)
            await self.release.wait()

    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)
    stale = b"\xff\xd8stale"
    fresh = b"\xff\xd8fresh"
    relay.offer(stale)

    blocking = _BlockingSession()
    flush_task = asyncio.create_task(relay.flush(blocking))
    for _ in range(5):  # let flush reach the blocked send
        await asyncio.sleep(0)
    relay.offer(fresh)
    blocking.release.set()
    assert await flush_task is True
    assert blocking.sent[0].data == stale

    relay.mark_turn_complete()
    second = _BlockingSession()
    second.release.set()
    assert await relay.flush(second) is True
    assert second.sent[0].data == fresh


@pytest.mark.asyncio
async def test_interrupted_resets_relay_once_per_turn_latch() -> None:
    """A turn ending via interrupted (no turn_complete) frees the next still.

    Barge-in ends turns with only ``interrupted`` set; without the reset the
    following speech burst would flush no frame until some later
    turn_complete arrives (reviewer finding 2026-07-10).
    """
    from hermes_cli.voice_live_session import GeminiLiveSession, _VideoFrameRelay

    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    relay = _VideoFrameRelay(events_out)
    relay.offer(FIXTURE_VIDEO.read_bytes())
    sdk_session = _FakeSDKSession()
    assert await relay.flush(sdk_session) is True
    assert await relay.flush(sdk_session) is False  # latch closed

    handled = await wrapper._handle_message(
        sdk_session,
        _interrupted_message(),
        events_out,
        _FakeToolExecutor(),
        relay,
    )
    assert handled is False

    relay.offer(FIXTURE_VIDEO.read_bytes())
    assert await relay.flush(sdk_session) is True  # latch reopened


# =============================================================================
# Usage metering, cost-guardrail, and watch-config-plumbing tests
# =============================================================================

_PRICING_TABLE = {
    "gemini-3.1-flash-live-preview": {
        "as_of": "2026-07-10",
        "input_per_1m": {"text": 0.75, "audio": 3.00, "image": 1.00},
        "output_per_1m": {"text": 4.50, "audio": 12.00},
    },
}


def _modality_count(modality: types.MediaModality, count: int) -> types.ModalityTokenCount:
    return types.ModalityTokenCount(modality=modality, token_count=count)


def _usage_metadata(
    *,
    prompt_total: int,
    response_total: int,
    prompt_details: list[tuple[types.MediaModality, int]],
    response_details: list[tuple[types.MediaModality, int]],
) -> types.UsageMetadata:
    return types.UsageMetadata(
        prompt_token_count=prompt_total,
        response_token_count=response_total,
        prompt_tokens_details=[_modality_count(m, c) for m, c in prompt_details],
        response_tokens_details=[_modality_count(m, c) for m, c in response_details],
    )


# Live-probed on gemini-3.1-flash-live-preview 2026-07-10 (three turns of one
# real session; see builder brief). Per-modality detail lists undercount the
# prompt totals — the shortfall is real upstream behavior, not a fixture bug.
PROBE_TURNS = [
    _usage_metadata(
        prompt_total=144,
        response_total=19,
        prompt_details=[(types.MediaModality.TEXT, 126)],
        response_details=[(types.MediaModality.AUDIO, 19)],
    ),
    _usage_metadata(
        prompt_total=180,
        response_total=20,
        prompt_details=[
            (types.MediaModality.TEXT, 131),
            (types.MediaModality.AUDIO, 19),
        ],
        response_details=[(types.MediaModality.AUDIO, 20)],
    ),
    _usage_metadata(
        prompt_total=217,
        response_total=23,
        prompt_details=[
            (types.MediaModality.TEXT, 136),
            (types.MediaModality.AUDIO, 39),
        ],
        response_details=[(types.MediaModality.AUDIO, 23)],
    ),
]


def test_usage_meter_probe_fixture_sums_tokens_and_flags_incomplete_estimate() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    for usage in PROBE_TURNS:
        meter.record(usage)

    assert meter.usage_messages == 3
    assert meter.input.total() == 541
    assert meter.output.total() == 62
    assert meter.input.as_dict() == {
        "text": 393,
        "audio": 58,
        "image": 0,
        "unattributed": 90,
    }
    assert meter.output.as_dict() == {
        "text": 0,
        "audio": 62,
        "image": 0,
        "unattributed": 0,
    }
    # Prompt details undercount every turn's total -> honestly flagged.
    assert meter.estimate_incomplete is True
    assert meter.estimated_usd() == pytest.approx(0.00128025)
    assert meter.pricing_as_of == "2026-07-10"


def test_usage_meter_exact_detail_coverage_marks_estimate_complete() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(
        _usage_metadata(
            prompt_total=100,
            response_total=50,
            prompt_details=[(types.MediaModality.TEXT, 100)],
            response_details=[(types.MediaModality.AUDIO, 50)],
        )
    )

    assert meter.estimate_incomplete is False
    expected = 100 / 1_000_000 * 0.75 + 50 / 1_000_000 * 12.00
    assert meter.estimated_usd() == pytest.approx(expected)


def test_usage_meter_unknown_model_has_no_estimate_and_stays_incomplete() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("unknown-model", _PRICING_TABLE)
    meter.record(PROBE_TURNS[0])

    assert meter.estimated_usd() is None
    assert meter.estimate_incomplete is True


def test_usage_meter_none_counts_do_not_crash() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(
        types.UsageMetadata(
            prompt_token_count=None,
            response_token_count=None,
            prompt_tokens_details=None,
            response_tokens_details=None,
        )
    )

    assert meter.usage_messages == 1
    assert meter.input.total() == 0
    assert meter.output.total() == 0
    assert meter.estimated_usd() == 0.0


def test_usage_meter_missing_total_flags_incomplete_estimate() -> None:
    """A usage message whose totals are all ``None`` must not look complete."""
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(
        types.UsageMetadata(
            prompt_token_count=None,
            response_token_count=None,
            prompt_tokens_details=[(types.ModalityTokenCount(
                modality=types.MediaModality.TEXT, token_count=5
            ))],
            response_tokens_details=None,
        )
    )

    assert meter.estimate_incomplete is True


def test_usage_meter_attributed_exceeds_total_flags_incomplete_and_never_subtracts() -> None:
    """Detail lists summing to MORE than the total must not be ignored."""
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(
        _usage_metadata(
            prompt_total=10,
            response_total=0,
            prompt_details=[
                (types.MediaModality.TEXT, 6),
                (types.MediaModality.AUDIO, 8),
            ],
            response_details=[],
        )
    )

    assert meter.estimate_incomplete is True
    # attributed (14) > total (10): both modality counts are kept as-recorded
    # and nothing negative is ever subtracted from unattributed.
    assert meter.input.text == 6
    assert meter.input.audio == 8
    assert meter.input.unattributed == 0


def test_usage_meter_negative_token_counts_are_clamped_and_flag_incomplete() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(
        types.UsageMetadata(
            prompt_token_count=-5,
            response_token_count=-1,
            prompt_tokens_details=[
                types.ModalityTokenCount(
                    modality=types.MediaModality.TEXT, token_count=-3
                )
            ],
            response_tokens_details=None,
        )
    )

    assert meter.estimate_incomplete is True
    assert meter.input.text == 0
    assert meter.input.total() == 0
    assert meter.output.total() == 0


@pytest.mark.parametrize(
    "pricing_entry",
    [
        # Missing output audio rate.
        {
            "as_of": "2026-07-10",
            "input_per_1m": {"text": 0.75, "audio": 3.00, "image": 1.00},
            "output_per_1m": {"text": 4.50},
        },
        # Non-numeric rate.
        {
            "as_of": "2026-07-10",
            "input_per_1m": {"text": 0.75, "audio": 3.00, "image": 1.00},
            "output_per_1m": {"text": 4.50, "audio": "12.00"},
        },
        # Negative rate.
        {
            "as_of": "2026-07-10",
            "input_per_1m": {"text": 0.75, "audio": -3.00, "image": 1.00},
            "output_per_1m": {"text": 4.50, "audio": 12.00},
        },
        # NaN rate.
        {
            "as_of": "2026-07-10",
            "input_per_1m": {"text": 0.75, "audio": 3.00, "image": 1.00},
            "output_per_1m": {"text": 4.50, "audio": float("nan")},
        },
    ],
)
def test_usage_meter_invalid_pricing_schema_is_treated_as_unpriced(pricing_entry) -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    pricing = {"gemini-3.1-flash-live-preview": pricing_entry}
    meter = _UsageMeter("gemini-3.1-flash-live-preview", pricing)
    meter.record(PROBE_TURNS[0])

    assert meter.estimated_usd() is None
    assert meter.estimate_incomplete is True
    assert meter.pricing_as_of is None


def test_usage_meter_complete_valid_pricing_entry_still_prices() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(
        _usage_metadata(
            prompt_total=100,
            response_total=50,
            prompt_details=[(types.MediaModality.TEXT, 100)],
            response_details=[(types.MediaModality.AUDIO, 50)],
        )
    )

    assert meter.estimated_usd() is not None
    assert meter.pricing_as_of == "2026-07-10"


def test_usage_meter_multiple_messages_are_summed_never_deduped() -> None:
    from hermes_cli.voice_live_session import _UsageMeter

    meter = _UsageMeter("gemini-3.1-flash-live-preview", _PRICING_TABLE)
    meter.record(PROBE_TURNS[0])
    meter.record(PROBE_TURNS[0])

    assert meter.usage_messages == 2
    assert meter.input.total() == 144 * 2
    assert meter.output.total() == 19 * 2


@pytest.mark.asyncio
async def test_usage_metadata_message_records_and_emits_usage_update_event() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        pricing=_PRICING_TABLE,
        session_soft_budget_usd=0.35,
    )
    wrapper._session_started_at = time.monotonic() - 5.0
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sdk_session = _FakeSDKSession()

    message = types.LiveServerMessage(usage_metadata=PROBE_TURNS[0])
    handled = await wrapper._handle_message(
        sdk_session, message, events_out, _FakeToolExecutor(), None
    )
    assert handled is False

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    usage_events = [event for event in emitted if event["type"] == "usage_update"]
    assert len(usage_events) == 1
    event = usage_events[0]

    assert event["turns"] == 0
    assert event["usage_messages"] == 1
    assert event["tokens"]["input"] == {
        "text": 126,
        "audio": 0,
        "image": 0,
        "unattributed": 18,
    }
    assert event["tokens"]["output"] == {
        "text": 0,
        "audio": 19,
        "image": 0,
        "unattributed": 0,
    }
    assert event["images_sent"] == 0
    assert event["watch"] == {"candidates": 0, "injections": 0}
    assert event["estimate_incomplete"] is True
    assert isinstance(event["estimated_usd"], float)
    assert event["model"] == "gemini-3.1-flash-live-preview"
    assert event["pricing_as_of"] == "2026-07-10"
    assert event["soft_budget_usd"] == 0.35
    assert event["soft_budget_exceeded"] is False
    assert event["session_seconds"] >= 5.0
    # Never leak transcript text, tool args, or image data onto this event.
    assert "text" not in event
    assert "data" not in event


@pytest.mark.asyncio
async def test_turn_complete_increments_completed_turns_counter() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sdk_session = _FakeSDKSession()

    assert wrapper._completed_turns == 0
    await wrapper._handle_message(
        sdk_session, _turn_complete_message(), events_out, _FakeToolExecutor(), None
    )
    assert wrapper._completed_turns == 1
    await wrapper._handle_message(
        sdk_session, _turn_complete_message(), events_out, _FakeToolExecutor(), None
    )
    assert wrapper._completed_turns == 2


@pytest.mark.asyncio
async def test_run_sets_session_started_at_to_monotonic_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession("model", "de-DE", [], "secret")
    assert wrapper._session_started_at == 0.0

    before = time.monotonic()
    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), asyncio.Queue(), _FakeToolExecutor())
    )
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    after = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert before <= wrapper._session_started_at <= after


@pytest.mark.asyncio
async def test_guardrail_tick_emits_soft_warning_exactly_once() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        session_soft_minutes=10.0,
        session_max_minutes=15.0,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    # Rewind the session start instead of mocking time.monotonic() itself —
    # patching the real clock would also derail asyncio's own scheduling.
    wrapper._session_started_at = time.monotonic() - 10.0 * 60

    await wrapper._guardrail_tick(events_out)

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    warnings = [event for event in emitted if event["type"] == "usage_warning"]
    assert len(warnings) == 1
    assert warnings[0]["reason"] == "soft_minutes"
    assert warnings[0]["minutes"] == pytest.approx(10.0, abs=0.05)

    await wrapper._guardrail_tick(events_out)
    assert events_out.empty()  # not re-warned


@pytest.mark.asyncio
async def test_guardrail_tick_soft_warning_uses_combined_live_and_look_closely_estimate() -> (
    None
):
    """The soft usage_warning must fold in look_closely cost too (see
    ``_combined_estimate``), matching the hard-budget guard — otherwise the
    client sees a soft warning quoting a Live-only estimate that undercounts
    the session's real cost."""

    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
        session_soft_minutes=10.0,
        session_max_minutes=200.0,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic() - 10.0 * 60
    # record_look_closely_usage's own usage_update event needs self._events_out
    # wired up (run() does that); left unset here so events_out only sees
    # what _guardrail_tick puts on it below.
    wrapper.record_look_closely_usage(1_000_000, 0)
    combined_cost, incomplete = wrapper._combined_estimate()
    assert incomplete is False
    assert combined_cost > 0.0

    await wrapper._guardrail_tick(events_out)

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    warnings = [event for event in emitted if event["type"] == "usage_warning"]
    assert len(warnings) == 1
    assert warnings[0]["estimated_usd"] == pytest.approx(combined_cost)
    assert warnings[0]["estimate_incomplete"] is False


@pytest.mark.asyncio
async def test_guardrail_tick_ends_session_at_max_duration() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession, LiveSessionEnded

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        session_soft_minutes=10.0,
        session_max_minutes=15.0,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic() - 15.0 * 60

    with pytest.raises(LiveSessionEnded) as exc_info:
        await wrapper._guardrail_tick(events_out)
    assert exc_info.value.reason == "max_duration"

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    assert {"type": "session_ended", "reason": "max_duration"} in emitted


@pytest.mark.asyncio
async def test_guardrail_tick_hard_budget_fires_only_when_estimate_complete() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession, LiveSessionEnded

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        pricing=_PRICING_TABLE,
        session_soft_minutes=100.0,
        session_max_minutes=200.0,
        session_hard_budget_usd=0.0001,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic()
    wrapper._usage_meter.record(
        _usage_metadata(
            prompt_total=1_000_000,
            response_total=0,
            prompt_details=[(types.MediaModality.TEXT, 1_000_000)],
            response_details=[],
        )
    )
    assert wrapper._usage_meter.estimate_incomplete is False

    with pytest.raises(LiveSessionEnded) as exc_info:
        await wrapper._guardrail_tick(events_out)
    assert exc_info.value.reason == "hard_budget"

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    assert {"type": "session_ended", "reason": "hard_budget"} in emitted


@pytest.mark.asyncio
async def test_guardrail_tick_incomplete_lower_bound_still_hard_stops() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession, LiveSessionEnded

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        pricing=_PRICING_TABLE,
        session_soft_minutes=100.0,
        session_max_minutes=200.0,
        session_hard_budget_usd=0.0001,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic()
    # No per-modality detail list at all -> fully unattributed, incomplete.
    wrapper._usage_meter.record(
        types.UsageMetadata(prompt_token_count=1_000_000, response_token_count=0)
    )
    assert wrapper._usage_meter.estimate_incomplete is True

    with pytest.raises(LiveSessionEnded) as exc_info:
        await wrapper._guardrail_tick(events_out)
    assert exc_info.value.reason == "hard_budget"
    assert events_out.get_nowait() == {"type": "session_ended", "reason": "hard_budget"}


@pytest.mark.asyncio
async def test_guardrail_tick_over_attributed_details_do_not_false_stop() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        pricing=_PRICING_TABLE,
        session_soft_minutes=100.0,
        session_max_minutes=200.0,
        session_hard_budget_usd=0.01,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic()
    wrapper._usage_meter.record(
        _usage_metadata(
            prompt_total=10,
            response_total=0,
            prompt_details=[
                (types.MediaModality.TEXT, 1_000_000),
                (types.MediaModality.AUDIO, 1_000_000),
            ],
            response_details=[],
        )
    )
    assert wrapper._usage_meter.estimate_incomplete is True
    assert wrapper._usage_meter.estimated_usd() > 0.01
    assert wrapper._usage_meter.guaranteed_floor_usd() < 0.01

    await wrapper._guardrail_tick(events_out)
    assert events_out.empty()


@pytest.mark.asyncio
async def test_guardrail_tick_hard_budget_fires_on_combined_live_and_look_closely_cost() -> (
    None
):
    """Live usage alone stays under hard_budget_usd, but Live + look_closely
    together cross it — the guard must use the combined estimate (see
    ``_combined_estimate``), not just the Live half."""

    from hermes_cli.voice_live_session import GeminiLiveSession, LiveSessionEnded

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
        session_soft_minutes=100.0,
        session_max_minutes=200.0,
        session_hard_budget_usd=0.2,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic()
    wrapper._usage_meter.record(
        _usage_metadata(
            prompt_total=100_000,
            response_total=0,
            prompt_details=[(types.MediaModality.TEXT, 100_000)],
            response_details=[],
        )
    )
    live_only_cost = wrapper._usage_meter.estimated_usd()
    assert live_only_cost < 0.2  # under budget on its own

    wrapper.record_look_closely_usage(1_000_000, 0)
    combined_cost, incomplete = wrapper._combined_estimate()
    assert incomplete is False
    assert combined_cost >= 0.2  # only over budget combined

    with pytest.raises(LiveSessionEnded) as exc_info:
        await wrapper._guardrail_tick(events_out)
    assert exc_info.value.reason == "hard_budget"

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    assert {"type": "session_ended", "reason": "hard_budget"} in emitted


@pytest.mark.asyncio
async def test_guardrail_tick_hard_budget_none_never_stops() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        pricing=_PRICING_TABLE,
        session_soft_minutes=100.0,
        session_max_minutes=200.0,
        session_hard_budget_usd=None,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wrapper._session_started_at = time.monotonic()
    wrapper._usage_meter.record(
        _usage_metadata(
            prompt_total=1_000_000,
            response_total=0,
            prompt_details=[(types.MediaModality.TEXT, 1_000_000)],
            response_details=[],
        )
    )

    await wrapper._guardrail_tick(events_out)  # must not raise

    assert events_out.empty()


@pytest.mark.asyncio
async def test_guardrail_tick_soft_warning_is_best_effort_when_events_out_is_full() -> None:
    """The soft usage_warning must also never block the guardrail tick."""
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        session_soft_minutes=10.0,
        session_max_minutes=15.0,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    events_out.put_nowait({"type": "already_there"})
    wrapper._session_started_at = time.monotonic() - 10.0 * 60

    await asyncio.wait_for(wrapper._guardrail_tick(events_out), timeout=1.0)

    assert events_out.qsize() == 1
    assert events_out.get_nowait() == {"type": "already_there"}


@pytest.mark.asyncio
async def test_guardrail_tick_max_duration_raises_even_when_events_out_is_full() -> None:
    """A full/stalled events_out must never block the max-duration stop.

    The terminal event delivery is best-effort only — the raise itself is the
    guarantee.
    """
    from hermes_cli.voice_live_session import GeminiLiveSession, LiveSessionEnded

    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        session_soft_minutes=10.0,
        session_max_minutes=15.0,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    events_out.put_nowait({"type": "already_there"})  # fill the bounded queue
    wrapper._session_started_at = time.monotonic() - 15.0 * 60

    with pytest.raises(LiveSessionEnded) as exc_info:
        await asyncio.wait_for(wrapper._guardrail_tick(events_out), timeout=1.0)
    assert exc_info.value.reason == "max_duration"
    # The pre-existing item is still there; the session_ended event was
    # best-effort-dropped rather than blocking (or erroring).
    assert events_out.qsize() == 1
    assert events_out.get_nowait() == {"type": "already_there"}


@pytest.mark.asyncio
async def test_guardrail_tick_hard_budget_raises_even_when_events_out_is_full() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession, LiveSessionEnded

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        pricing=_PRICING_TABLE,
        session_soft_minutes=100.0,
        session_max_minutes=200.0,
        session_hard_budget_usd=0.0001,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    events_out.put_nowait({"type": "already_there"})
    wrapper._session_started_at = time.monotonic()
    wrapper._usage_meter.record(
        _usage_metadata(
            prompt_total=1_000_000,
            response_total=0,
            prompt_details=[(types.MediaModality.TEXT, 1_000_000)],
            response_details=[],
        )
    )

    with pytest.raises(LiveSessionEnded) as exc_info:
        await asyncio.wait_for(wrapper._guardrail_tick(events_out), timeout=1.0)
    assert exc_info.value.reason == "hard_budget"
    assert events_out.qsize() == 1


@pytest.mark.asyncio
async def test_watch_config_plumbs_custom_cooldown_and_max_into_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        watch_cooldown_seconds=5.0,
        watch_max_notifications=1,
    )
    video_in: asyncio.Queue[bytes] = asyncio.Queue()
    audio_in: asyncio.Queue[bytes] = asyncio.Queue()
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    frame = FIXTURE_VIDEO.read_bytes()

    task = asyncio.create_task(
        wrapper.run(audio_in, events_out, _FakeToolExecutor(), video_in=video_in)
    )
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)
    await video_in.put(frame)
    await _wait_until(lambda: video_in.empty())

    result = wrapper.watch_view("Prüfe den Build")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert result["watching"] is True
    assert result["cooldown_seconds"] == 5.0
    assert result["max_notifications"] == 1


# =============================================================================
# look_closely additive usage accounting (record_look_closely_usage)
# =============================================================================

_LOOK_PRICING_TABLE = {
    **_PRICING_TABLE,
    "gemini-3.1-flash-lite": {
        "as_of": "2026-07-10",
        "input_per_1m": 0.25,
        "output_per_1m": 1.50,
    },
}


def test_record_look_closely_usage_prices_from_flat_look_model_rate() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
    )

    wrapper.record_look_closely_usage(1_000_000, 200_000)

    cost, incomplete = wrapper._look_closely_cost()
    assert incomplete is False
    assert cost == pytest.approx(0.25 + 0.30)
    assert wrapper._extra_usage.calls == 1
    assert wrapper._extra_usage.input_tokens == 1_000_000
    assert wrapper._extra_usage.output_tokens == 200_000


def test_look_closely_cost_zero_calls_never_flags_incomplete() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview", "de-DE", [], "secret", pricing={}
    )
    cost, incomplete = wrapper._look_closely_cost()
    assert cost == 0.0
    assert incomplete is False


def test_look_closely_cost_missing_pricing_entry_flags_incomplete() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing={},
    )
    wrapper.record_look_closely_usage(1000, 100)

    cost, incomplete = wrapper._look_closely_cost()
    assert cost == 0.0
    assert incomplete is True


@pytest.mark.parametrize("bad_rate", [float("nan"), float("inf"), -1.0])
def test_look_closely_cost_nonfinite_or_negative_rate_flags_incomplete(bad_rate) -> None:
    """A NaN/inf/negative rate in the pricing table must never be treated as
    a usable price (which would silently under/over-report cost) — same
    strictness _UsageMeter already applies to Live pricing rows."""
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing={
            "gemini-3.1-flash-lite": {
                "as_of": "2026-07-10",
                "input_per_1m": bad_rate,
                "output_per_1m": 1.50,
            }
        },
    )
    wrapper.record_look_closely_usage(1000, 100)

    cost, incomplete = wrapper._look_closely_cost()
    assert cost == 0.0
    assert incomplete is True


def test_record_look_closely_usage_negative_tokens_are_clamped() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
    )

    wrapper.record_look_closely_usage(-50, -10)

    assert wrapper._extra_usage.input_tokens == 0
    assert wrapper._extra_usage.output_tokens == 0


def test_record_look_closely_usage_folds_into_usage_update_estimated_usd() -> None:
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "unknown-model",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
    )
    # No pricing entry for "unknown-model": the Live half of estimated_usd is
    # None, but a look_closely call must still surface its own additive cost
    # rather than the whole event collapsing to "no cost at all".
    wrapper.record_look_closely_usage(1_000_000, 0)

    event = wrapper._usage_update_event(None)

    assert event["look_closely"] == {"calls": 1, "estimated_usd": pytest.approx(0.25)}
    assert event["estimated_usd"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_record_look_closely_usage_emits_immediate_usage_update_during_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A look_closely call's cost must reach the client right away — not only
    on the next Live usage_metadata arrival, which may never come."""
    from hermes_cli.voice_live_session import GeminiLiveSession

    sdk_session = _FakeSDKSession()
    live = _FakeLive([sdk_session])
    _install_fake_client(monkeypatch, live)
    wrapper = GeminiLiveSession(
        "model",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
    )
    events_out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        wrapper.run(asyncio.Queue(), events_out, _FakeToolExecutor())
    )
    await asyncio.wait_for(live.connect_called.wait(), timeout=1)

    wrapper.record_look_closely_usage(1_000_000, 0)
    # _put_guardrail_event_best_effort uses put_nowait: synchronous, so the
    # event is already queued by the time record_look_closely_usage returns.

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    emitted = []
    while not events_out.empty():
        emitted.append(events_out.get_nowait())
    usage_events = [event for event in emitted if event["type"] == "usage_update"]
    assert usage_events
    assert usage_events[-1]["look_closely"]["calls"] == 1


def test_record_look_closely_usage_before_run_never_raises() -> None:
    """Before ``run()`` sets ``self._events_out``, recording usage must be a
    safe no-op for the event emission (never an AttributeError)."""
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "model", "de-DE", [], "secret", look_model="gemini-3.1-flash-lite"
    )
    wrapper.record_look_closely_usage(100, 10)
    assert wrapper._extra_usage.calls == 1


def test_record_look_closely_usage_incomplete_flags_usage_update_event() -> None:
    """``complete=False`` (missing/partial usage_metadata upstream, see
    tools.voice_live_tools.VoiceToolExecutor) must still count the call and
    the tokens it does have, but flag the combined estimate incomplete —
    never silently report it as a precise number."""
    from hermes_cli.voice_live_session import GeminiLiveSession

    wrapper = GeminiLiveSession(
        "gemini-3.1-flash-live-preview",
        "de-DE",
        [],
        "secret",
        look_model="gemini-3.1-flash-lite",
        pricing=_LOOK_PRICING_TABLE,
    )

    wrapper.record_look_closely_usage(0, 0, complete=False)

    assert wrapper._extra_usage.calls == 1
    assert wrapper._extra_usage.incomplete is True
    event = wrapper._usage_update_event(None)
    assert event["estimate_incomplete"] is True

import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.voice_live_session import LiveFallbackRequired
from hermes_cli.voice_ws import (
    DEFAULT_LIVE_MODEL,
    VoiceWebConfig,
    create_voice_router,
    voice_web_config,
)


FIXTURE = Path(__file__).parent / "fixtures" / "voice_sample_16k.pcm"


def _voice_app(
    *,
    enabled=True,
    auth_reason=(None, "test"),
    host_reason=None,
    client_reason=None,
    auth_required=False,
    session_token="test-session-token",
):
    app = FastAPI()
    app.state.auth_required = auth_required
    app.include_router(
        create_voice_router(
            {"voice_web": {"enabled": enabled}},
            ws_auth_reason=lambda _ws: auth_reason,
            ws_host_origin_reason=lambda _ws: host_reason,
            ws_client_reason=lambda _ws: client_reason,
            ws_close_reason=lambda reason: f"closed:{reason}",
            session_token=session_token,
        )
    )
    return app


def test_voice_web_config_defaults_off():
    cfg = voice_web_config({})
    assert cfg.enabled is False
    assert cfg.model == "gemini-2.5-flash-native-audio-preview-12-2025"
    assert cfg.language == "de-DE"

    assert DEFAULT_CONFIG["voice_web"] == {
        "enabled": False,
        "model": "gemini-2.5-flash-native-audio-preview-12-2025",
        "language": "de-DE",
    }


@pytest.mark.parametrize("section", ["false", True, ["enabled"], 1])
def test_voice_web_config_rejects_malformed_section(section):
    assert voice_web_config({"voice_web": section}) == VoiceWebConfig()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, False),
        ("true", False),
        ([True], False),
        (None, False),
    ],
)
def test_voice_web_config_only_literal_true_enables(value, expected):
    cfg = voice_web_config({"voice_web": {"enabled": value}})
    assert cfg.enabled is expected


@pytest.mark.parametrize("value", [None, "", "   ", 42, False, []])
def test_voice_web_config_defaults_invalid_model_and_language(value):
    cfg = voice_web_config({"voice_web": {"model": value, "language": value}})
    assert cfg.model == DEFAULT_LIVE_MODEL
    assert cfg.language == "de-DE"


def test_voice_web_config_accepts_nonblank_string_overrides():
    cfg = voice_web_config({
        "voice_web": {"model": "gemini-live-custom", "language": "de-AT"}
    })
    assert cfg.model == "gemini-live-custom"
    assert cfg.language == "de-AT"


@pytest.mark.parametrize(
    ("auth_reason", "host_reason", "client_reason", "expected_code"),
    [
        (("no_credential", "none"), None, None, 4401),
        ((None, "ticket"), "host_mismatch", None, 4403),
        ((None, "ticket"), None, "peer_not_loopback", 4408),
    ],
)
def test_voice_websocket_rejects_before_accept(
    auth_reason, host_reason, client_reason, expected_code
):
    client = TestClient(
        _voice_app(
            auth_reason=auth_reason,
            host_reason=host_reason,
            client_reason=client_reason,
        )
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/voice/live"):
            pass

    assert exc.value.code == expected_code
    assert exc.value.reason.startswith("closed:")


def test_voice_index_enabled_injects_loopback_bootstrap_safely(tmp_path, monkeypatch):
    from hermes_cli import voice_ws

    token = '</script><script data-leak="true">'
    (tmp_path / "index.html").write_text(
        "<html><head></head><body>Hermes Voice</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(voice_ws, "VOICE_CLIENT_DIR", tmp_path)
    response = TestClient(_voice_app(session_token=token)).get("/voice")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    assert "window.__HERMES_AUTH_REQUIRED__=false" in response.text
    assert "window.__HERMES_SESSION_TOKEN__=" in response.text
    assert token not in response.text
    assert "\\u003c/script\\u003e" in response.text


def test_voice_index_gated_mode_never_injects_session_token(tmp_path, monkeypatch):
    from hermes_cli import voice_ws

    (tmp_path / "index.html").write_text(
        "<html><head></head><body>Hermes Voice</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(voice_ws, "VOICE_CLIENT_DIR", tmp_path)
    response = TestClient(
        _voice_app(auth_required=True, session_token="must-not-leak")
    ).get("/voice")

    assert response.status_code == 200
    assert "window.__HERMES_AUTH_REQUIRED__=true" in response.text
    assert "__HERMES_SESSION_TOKEN__" not in response.text
    assert "must-not-leak" not in response.text


def test_voice_routes_absent_when_disabled(tmp_path, monkeypatch):
    from hermes_cli import voice_ws

    (tmp_path / "index.html").write_text("voice", encoding="utf-8")
    monkeypatch.setattr(voice_ws, "VOICE_CLIENT_DIR", tmp_path)

    assert TestClient(_voice_app(enabled=False)).get("/voice").status_code == 404


def test_voice_assets_are_explicitly_allowlisted(tmp_path, monkeypatch):
    from hermes_cli import voice_ws

    (tmp_path / "app.js").write_text("voice-app", encoding="utf-8")
    (tmp_path / "private.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(voice_ws, "VOICE_CLIENT_DIR", tmp_path)
    client = TestClient(_voice_app())

    allowed = client.get("/voice/app.js")
    assert allowed.status_code == 200
    assert allowed.text == "voice-app"
    assert allowed.headers["cache-control"].startswith("no-store")
    assert client.get("/voice/private.txt").status_code == 404
    assert client.get("/voice/../private.txt").status_code == 404


def test_live_failure_falls_back_on_same_websocket(monkeypatch):
    from hermes_cli import voice_ws

    fixture = FIXTURE.read_bytes()
    assert len(fixture) > 20_000

    class FakeGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, audio_in, events_out, tool_executor):
            await audio_in.get()
            audio_in.task_done()
            raise LiveFallbackRequired("quota")

    async def fake_transcribe(pcm, language):
        assert pcm == fixture
        assert language == "de-DE"
        return "hallo hermes"

    async def fake_delegate(prompt):
        assert prompt == "hallo hermes"
        return "Hallo Piet"

    async def fake_synthesize(text, language):
        assert text == "Hallo Piet"
        assert language == "de-DE"
        return b"\x01\x00" * 240

    monkeypatch.setenv("GEMINI_API_KEY", "server-only-gemini-key")
    monkeypatch.setattr(voice_ws, "GeminiLiveSession", FakeGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "fallback_transcribe_pcm", fake_transcribe)
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", fake_synthesize)

    transcript = None
    audio = None
    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(fixture)
        ws.send_json({"type": "end"})
        for _ in range(10):
            message = ws.receive()
            if message.get("text"):
                payload = json.loads(message["text"])
                if payload.get("type") == "transcript":
                    transcript = payload
            if message.get("bytes"):
                audio = message["bytes"]
            if transcript is not None and audio is not None:
                break

    assert transcript == {"type": "transcript", "text": "hallo hermes"}
    assert audio


def test_missing_gemini_key_uses_fallback_without_constructing_live(
    monkeypatch,
):
    from hermes_cli import voice_ws

    class UnexpectedGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Live session must not be constructed without a key")

    async def fake_transcribe(_pcm, _language):
        return "hallo"

    async def fake_delegate(_prompt):
        return "antwort"

    async def fake_synthesize(_text, _language):
        return b"\x00\x00"

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "GeminiLiveSession", UnexpectedGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "fallback_transcribe_pcm", fake_transcribe)
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", fake_synthesize)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(b"\x00\x00")
        ws.send_json({"type": "end"})
        messages = [ws.receive() for _ in range(4)]

    assert any(message.get("bytes") for message in messages)


def test_odd_sized_pcm_frame_returns_structured_error(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(b"\x00")
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["error"]["code"] == "invalid_pcm_frame"


@pytest.mark.asyncio
async def test_fallback_transcription_writes_pcm_wav_under_hermes_home(
    tmp_path, monkeypatch
):
    from hermes_cli import voice_ws

    observed = {}

    def fake_transcribe(path):
        audio_path = Path(path)
        observed["path"] = audio_path
        with wave.open(str(audio_path), "rb") as wav_file:
            observed["format"] = (
                wav_file.getnchannels(),
                wav_file.getsampwidth(),
                wav_file.getframerate(),
            )
            observed["pcm"] = wav_file.readframes(wav_file.getnframes())
        return {"success": True, "transcript": "hallo"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(voice_ws, "transcribe_audio", fake_transcribe)

    transcript = await voice_ws.fallback_transcribe_pcm(b"\x01\x00" * 4, "de-DE")

    assert transcript == "hallo"
    assert observed["format"] == (1, 2, 16_000)
    assert observed["pcm"] == b"\x01\x00" * 4
    assert observed["path"].parent == tmp_path / "cache" / "voice-web"
    assert not observed["path"].exists()


@pytest.mark.asyncio
async def test_fallback_tts_uses_public_tool_and_ffmpeg_pcm24k(tmp_path, monkeypatch):
    from hermes_cli import voice_ws

    observed = {}

    def fake_tts(text, output_path):
        observed["text"] = text
        Path(output_path).write_bytes(b"encoded-audio")
        return json.dumps({"success": True, "file_path": output_path})

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"\x02\x00", stderr=b"")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(voice_ws, "text_to_speech_tool", fake_tts)
    monkeypatch.setattr(voice_ws.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_ws.subprocess, "run", fake_run)

    pcm = await voice_ws.fallback_synthesize_pcm("Hallo Piet", "de-DE")

    assert pcm == b"\x02\x00"
    assert observed["text"] == "Hallo Piet"
    assert observed["command"][observed["command"].index("-ar") + 1] == "24000"
    assert observed["command"][observed["command"].index("-ac") + 1] == "1"
    assert list((tmp_path / "cache" / "voice-web").iterdir()) == []


@pytest.mark.asyncio
async def test_delegate_uses_bounded_shell_free_hermes_quiet_subprocess(monkeypatch):
    from hermes_cli import voice_ws

    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"erledigt", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    response = await voice_ws.delegate_to_hermes("prüfe den Status")

    assert response == "erledigt"
    assert observed["args"] == ("hermes", "-q", "prüfe den Status")
    assert observed["kwargs"]["stdout"] is voice_ws.asyncio.subprocess.PIPE
    assert observed["kwargs"]["stderr"] is voice_ws.asyncio.subprocess.PIPE

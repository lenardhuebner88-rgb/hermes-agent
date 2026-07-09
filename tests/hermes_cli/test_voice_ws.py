import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
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

    expected_assets = {
        "app.js": ("voice-app", "javascript"),
        "worklet.js": ("voice-worklet", "javascript"),
        "manifest.json": ('{"name":"Hermes Voice"}', "manifest+json"),
        "icon.svg": ("<svg></svg>", "image/svg+xml"),
        "icon-192.png": ("fake-192-png-bytes", "image/png"),
        "icon-512.png": ("fake-512-png-bytes", "image/png"),
        "icon-maskable-512.png": ("fake-maskable-png-bytes", "image/png"),
        "offline.html": ("<html>offline</html>", "text/html"),
        "sw.js": ("self.skipWaiting();", "javascript"),
    }
    for asset_name, (content, _media_type) in expected_assets.items():
        (tmp_path / asset_name).write_text(content, encoding="utf-8")
    (tmp_path / "private.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(voice_ws, "VOICE_CLIENT_DIR", tmp_path)
    client = TestClient(_voice_app())

    for asset_name, (content, media_type) in expected_assets.items():
        allowed = client.get(f"/voice/{asset_name}")
        assert allowed.status_code == 200
        assert allowed.text == content
        assert media_type in allowed.headers["content-type"]
        assert allowed.headers["cache-control"].startswith("no-store")
    assert client.get("/voice/private.txt").status_code == 404
    assert client.get("/voice/../private.txt").status_code == 404
    assert client.get("/voice/unknown.xyz").status_code == 404


def test_voice_sw_js_asset_sets_service_worker_allowed_header(tmp_path, monkeypatch):
    """sw.js must legalize scope="/voice" via ``Service-Worker-Allowed``.

    Without this header a browser rejects a registration that passes an
    explicit ``scope`` wider than the script's own directory (here that's
    moot since both are "/voice", but the header is the documented opt-in
    browsers require whenever `scope` is passed explicitly at all — see
    app.js' ``register("/voice/sw.js", { scope: "/voice" })``). Sibling
    assets must NOT carry the header — it is sw.js-specific, not folded
    into the shared ``_NO_STORE_HEADERS``.
    """
    from hermes_cli import voice_ws

    (tmp_path / "sw.js").write_text("self.skipWaiting();", encoding="utf-8")
    (tmp_path / "icon.svg").write_text("<svg></svg>", encoding="utf-8")
    monkeypatch.setattr(voice_ws, "VOICE_CLIENT_DIR", tmp_path)
    client = TestClient(_voice_app())

    response = client.get("/voice/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["service-worker-allowed"] == "/voice"
    assert response.headers["cache-control"].startswith("no-store")

    sibling = client.get("/voice/icon.svg")
    assert sibling.status_code == 200
    assert "service-worker-allowed" not in sibling.headers


def test_voice_pwa_manifest_contract():
    """REAL-ARTIFACT: exercises the actual on-disk manifest.json and icon
    files (no monkeypatched VOICE_CLIENT_DIR), so a manifest/icon drift that
    only shows up against the real repo files can't hide behind a fixture.
    """
    from hermes_cli import voice_ws

    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    manifest_path = client_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["name"]
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/voice"
    assert manifest["scope"] == "/voice"
    assert manifest["theme_color"] == "#071310"

    icons = manifest["icons"]
    assert any(
        icon["sizes"] == "192x192" and icon["type"] == "image/png" for icon in icons
    )
    assert any(
        icon["sizes"] == "512x512"
        and icon["type"] == "image/png"
        and icon["purpose"] == "any"
        for icon in icons
    )
    assert any(icon["purpose"] == "maskable" for icon in icons)

    for icon in icons:
        icon_path = client_dir / icon["src"].removeprefix("/voice/")
        assert icon_path.is_file(), (
            f"manifest icon src {icon['src']!r} has no file on disk"
        )
        if icon_path.suffix == ".png":
            assert icon_path.read_bytes().startswith(b"\x89PNG"), (
                f"{icon_path} does not start with the PNG magic bytes"
            )

    for asset_name in voice_ws._ALLOWED_VOICE_ASSETS:
        assert (client_dir / asset_name).is_file(), (
            f"_ALLOWED_VOICE_ASSETS entry {asset_name!r} has no file on disk"
        )


def test_voice_sw_js_guards_api_paths_and_references_offline_fallback():
    """REAL-ARTIFACT sanity check on sw.js's actual text: a cheap tripwire
    that the "/api/*" fetch guard and the offline fallback wiring survive
    future edits without standing up a full ServiceWorker test harness
    (neither jsdom nor plain Node implement the SW/Cache APIs).
    """
    sw_path = Path(__file__).parents[2] / "hermes_cli" / "voice_client" / "sw.js"
    script = sw_path.read_text(encoding="utf-8")

    assert "hermes-voice-v" in script
    assert "/voice/offline.html" in script
    assert 'startsWith("/api/")' in script


def test_voice_client_uses_single_use_ticket_without_long_lived_ws_token():
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    script = (client_dir / "app.js").read_text(encoding="utf-8")
    document = (client_dir / "index.html").read_text(encoding="utf-8")

    assert 'fetch("/api/auth/ws-ticket"' in script
    assert 'credentials: "same-origin"' in script
    assert 'headers.set("X-Hermes-Session-Token", loopbackToken)' in script
    assert 'websocketUrl.searchParams.set("ticket", ticket)' in script
    assert 'searchParams.set("token"' not in script
    assert "?token=" not in script
    assert '<script src="/voice/app.js" defer></script>' in document
    assert '<link rel="manifest" href="/voice/manifest.json"' in document
    assert 'href="/voice/icon.svg"' in document
    # C1 (PWA installability): app.js registers the service worker with an
    # explicit "/voice" scope, guarded so a registration failure never
    # breaks the app itself.
    assert 'if ("serviceWorker" in navigator)' in script
    assert (
        'navigator.serviceWorker.register("/voice/sw.js", { scope: "/voice" })'
        in script
    )
    assert ".catch(() => {})" in script


def test_voice_client_barge_in_tracks_audible_playback_not_server_state():
    script_path = Path(__file__).parents[2] / "hermes_cli" / "voice_client" / "app.js"
    script = script_path.read_text(encoding="utf-8")

    # The server may announce `listening` after enqueueing all PCM while the
    # WebAudio queue remains audible. Barge-in must therefore key off tracked
    # local sources, and a non-speaking state must not reset its three-frame
    # detector until those sources have drained.
    assert "session.playbackSources.size > 0 &&" in script
    assert "session.playbackSources.size === 0\n  )" in script
    assert 'session.voiceState === "speaking" &&' not in script


def test_voice_client_mic_frames_are_safe_before_websocket_assignment():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the standalone voice client harness")

    repo_root = Path(__file__).parents[2]
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("hermes_cli/voice_client/app.js", "utf8");
const element = {
  textContent: "",
  disabled: false,
  hidden: false,
  children: [],
  addEventListener() {},
  setAttribute() {},
  append() {},
};
const context = {
  AbortController,
  ArrayBuffer,
  DataView,
  Headers,
  URL,
  WebSocket: { OPEN: 1, CONNECTING: 0 },
  console: { info() {} },
  document: {
    body: { dataset: {} },
    querySelector() { return element; },
  },
  navigator: {},
  performance: { now() { return 60; } },
  window: {
    __HERMES_SESSION_TOKEN__: undefined,
    addEventListener() {},
    clearTimeout,
    setTimeout,
  },
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`
  const startupSession = {
    microphoneStopped: false,
    websocket: null,
    playbackSources: new Set(),
    drainRequested: false,
    voiceState: "connecting",
    bargeTriggered: false,
    loudChunks: 0,
    bargeStartedAt: null,
  };
  activeSession = startupSession;
  handleMicFrame(startupSession, { rms: 0.01, pcm: new ArrayBuffer(640) });

  const sourceNode = { onended: null, stop() {}, disconnect() {} };
  const bargeSession = {
    microphoneStopped: false,
    websocket: null,
    playbackSources: new Set([sourceNode]),
    playbackCursor: 0,
    audioContext: { currentTime: 0 },
    drainRequested: false,
    voiceState: "speaking",
    suppressIncomingAudio: false,
    bargeTriggered: false,
    loudChunks: 2,
    bargeStartedAt: 0,
  };
  activeSession = bargeSession;
  handleMicFrame(bargeSession, { rms: 1, pcm: new ArrayBuffer(640) });
  if (!bargeSession.bargeTriggered) {
    throw new Error("barge-in was not triggered by audible local playback");
  }
`, context);
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(fixture)
        ws.send_json({"type": "end"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        transcript = ws.receive_json()
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "Hallo Piet",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        audio = ws.receive_bytes()
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        assert ws.receive()["type"] == "websocket.close"

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
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {"type": "transcript", "text": "hallo"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "antwort",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        assert ws.receive_bytes()
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        assert ws.receive()["type"] == "websocket.close"


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


class _FakeVoiceInput:
    def __init__(self, messages, before_receive=None):
        self._messages = iter(messages)
        self._before_receive = before_receive
        self._index = 0

    async def receive(self):
        if self._before_receive is not None:
            self._before_receive(self._index)
        self._index += 1
        return next(self._messages)


async def _read_test_voice_frames(websocket, fallback_mode):
    from hermes_cli import voice_ws

    audio_in = asyncio.Queue(maxsize=32)
    fallback_pcm = bytearray()
    events_out = asyncio.Queue()
    disconnected = asyncio.Event()
    result = await voice_ws._read_voice_frames(
        websocket,
        audio_in,
        fallback_pcm,
        events_out,
        fallback_mode,
        disconnected,
    )
    return result, fallback_pcm, events_out


@pytest.mark.asyncio
async def test_healthy_live_audio_keeps_recent_bounded_fallback_preroll(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MAX_FALLBACK_PCM_BYTES", 12)
    monkeypatch.setattr(voice_ws, "_FALLBACK_PREROLL_PCM_BYTES", 4)
    frames = [bytes([value, 0]) * 2 for value in range(1, 6)]
    messages = [{"bytes": frame} for frame in frames]
    messages.append({"text": json.dumps({"type": "end"})})

    result, fallback_pcm, events_out = await _read_test_voice_frames(
        _FakeVoiceInput(messages),
        asyncio.Event(),
    )

    assert result == "end"
    assert bytes(fallback_pcm) == frames[-1]
    assert len(fallback_pcm) <= 2 * voice_ws._FALLBACK_PREROLL_PCM_BYTES
    assert events_out.empty()


@pytest.mark.asyncio
async def test_live_failure_keeps_preroll_then_counts_new_fallback_audio(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MAX_FALLBACK_PCM_BYTES", 12)
    monkeypatch.setattr(voice_ws, "_FALLBACK_PREROLL_PCM_BYTES", 4)
    fallback_mode = asyncio.Event()
    frames = [bytes([value, 0]) * 2 for value in range(1, 6)]
    messages = [{"bytes": frame} for frame in frames]
    messages.append({"text": json.dumps({"type": "end"})})

    def enter_fallback(index):
        if index == 3:
            fallback_mode.set()

    result, fallback_pcm, events_out = await _read_test_voice_frames(
        _FakeVoiceInput(messages, enter_fallback),
        fallback_mode,
    )

    assert result == "end"
    assert bytes(fallback_pcm) == b"".join(frames[2:])
    assert events_out.empty()


@pytest.mark.asyncio
async def test_missing_key_fallback_keeps_full_audio_up_to_hard_cap(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MAX_FALLBACK_PCM_BYTES", 12)
    monkeypatch.setattr(voice_ws, "_FALLBACK_PREROLL_PCM_BYTES", 4)
    fallback_mode = asyncio.Event()
    fallback_mode.set()
    frames = [bytes([value, 0]) * 2 for value in range(1, 4)]
    messages = [{"bytes": frame} for frame in frames]
    messages.append({"text": json.dumps({"type": "end"})})

    result, fallback_pcm, events_out = await _read_test_voice_frames(
        _FakeVoiceInput(messages),
        fallback_mode,
    )

    assert result == "end"
    assert bytes(fallback_pcm) == b"".join(frames)
    assert events_out.empty()


@pytest.mark.asyncio
async def test_fallback_audio_exceeding_hard_cap_returns_structured_error(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MAX_FALLBACK_PCM_BYTES", 12)
    monkeypatch.setattr(voice_ws, "_FALLBACK_PREROLL_PCM_BYTES", 4)
    fallback_mode = asyncio.Event()
    fallback_mode.set()
    frame = b"\x01\x00" * 2
    messages = [{"bytes": frame} for _ in range(4)]

    result, fallback_pcm, events_out = await _read_test_voice_frames(
        _FakeVoiceInput(messages),
        fallback_mode,
    )

    assert result == "error"
    assert bytes(fallback_pcm) == frame * 3
    assert events_out.get_nowait()["error"]["code"] == "audio_too_large"


@pytest.mark.asyncio
async def test_single_oversize_live_frame_still_hits_hard_cap(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MAX_FALLBACK_PCM_BYTES", 12)
    monkeypatch.setattr(voice_ws, "_FALLBACK_PREROLL_PCM_BYTES", 4)

    result, fallback_pcm, events_out = await _read_test_voice_frames(
        _FakeVoiceInput([{"bytes": b"\x01\x00" * 7}]),
        asyncio.Event(),
    )

    assert result == "error"
    assert fallback_pcm == b""
    assert events_out.get_nowait()["error"]["code"] == "audio_too_large"


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

    expected_executable = str(Path(sys.executable).absolute().with_name("hermes"))
    assert Path(expected_executable).is_file()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    response = await voice_ws.delegate_to_hermes("prüfe den Status")

    assert response == "erledigt"
    assert observed["args"] == (
        expected_executable,
        "-q",
        "prüfe den Status",
    )
    assert observed["kwargs"]["stdout"] is voice_ws.asyncio.subprocess.PIPE
    assert observed["kwargs"]["stderr"] is voice_ws.asyncio.subprocess.PIPE
    if os.name == "posix":
        assert observed["kwargs"]["start_new_session"] is True


def test_disabled_web_server_does_not_import_voice_runtime(tmp_path):
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(tmp_path)
    code = """
import sys
from fastapi.testclient import TestClient
from hermes_cli import web_server

for module_name in (
    "hermes_cli.voice_ws",
    "hermes_cli.voice_live_session",
    "google.genai",
    "tools.transcription_tools",
    "tools.tts_tool",
):
    assert module_name not in sys.modules, module_name
assert web_server._voice_web_enabled({}) is False
assert web_server._voice_web_enabled({"voice_web": {"enabled": 1}}) is False
assert web_server._voice_web_enabled({"voice_web": {"enabled": True}}) is True
route_paths = {getattr(route, "path", "") for route in web_server.app.routes}
assert "/voice" not in route_paths
assert "/api/voice/live" not in route_paths
assert TestClient(web_server.app).get("/voice").status_code == 404
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_enabled_web_server_imports_and_registers_voice_router(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "voice_web:\n  enabled: true\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(tmp_path)
    code = """
import sys
from hermes_cli import web_server

assert "hermes_cli.voice_ws" in sys.modules
route_paths = {getattr(route, "path", "") for route in web_server.app.routes}
assert "/voice" in route_paths
assert "/api/voice/live" in route_paths
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_google_api_key_alias_does_not_enable_gemini_live(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-alias-only")
    monkeypatch.setattr(
        voice_ws,
        "load_env",
        lambda: {"GOOGLE_API_KEY": "google-dotenv-alias-only"},
    )

    assert voice_ws.resolve_gemini_api_key() == ""


def test_blank_process_gemini_key_falls_back_to_dotenv(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setattr(
        voice_ws,
        "load_env",
        lambda: {"GEMINI_API_KEY": "dotenv-gemini-key"},
    )

    assert voice_ws.resolve_gemini_api_key() == "dotenv-gemini-key"


def test_unexpected_live_error_is_safe_and_never_cascades(monkeypatch):
    from hermes_cli import voice_ws

    secret = "server-only-gemini-key"
    fallback_calls = []

    class BrokenGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, audio_in, events_out, tool_executor):
            raise ValueError(f"programmer failure containing {secret}")

    async def unexpected_fallback(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        raise AssertionError("unexpected live errors must not cascade")

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: secret)
    monkeypatch.setattr(voice_ws, "GeminiLiveSession", BrokenGeminiLiveSession)
    monkeypatch.setattr(
        voice_ws,
        "fallback_transcribe_pcm",
        unexpected_fallback,
    )
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", unexpected_fallback)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        payload = ws.receive_json()

    assert payload == {
        "type": "error",
        "error": {
            "code": "live_internal_error",
            "message": "Die Live-Sprachverbindung ist intern fehlgeschlagen.",
        },
    }
    assert secret not in json.dumps(payload)
    assert fallback_calls == []


def test_interrupt_flushes_playback_semantics_and_live_keeps_receiving(
    monkeypatch,
):
    from hermes_cli import voice_ws

    received_frames = []

    class ContinuingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, audio_in, events_out, tool_executor):
            for _ in range(2):
                frame = await audio_in.get()
                received_frames.append(frame)
                audio_in.task_done()
            await events_out.put({"type": "state", "value": "listening"})
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(
        voice_ws,
        "GeminiLiveSession",
        ContinuingGeminiLiveSession,
    )
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(b"\x01\x00")
        ws.send_json({"type": "interrupt"})
        assert ws.receive_json() == {"type": "interrupted"}
        ws.send_bytes(b"\x02\x00")
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    assert received_frames == [b"\x01\x00", b"\x02\x00"]


@pytest.mark.asyncio
async def test_delegate_cancellation_terminates_and_reaps_child(monkeypatch):
    from hermes_cli import voice_ws

    class CancellableProcess:
        def __init__(self):
            self.pid = 424_201
            self.returncode = None
            self.communicate_started = voice_ws.asyncio.Event()
            self.terminated = False
            self.reaped = False

        async def communicate(self):
            self.communicate_started.set()
            await voice_ws.asyncio.Event().wait()

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            raise AssertionError("cooperative terminate should not need kill")

        async def wait(self):
            self.reaped = True
            return self.returncode

    process = CancellableProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    tree_signals = []
    group_alive = True

    def fake_killpg(pid, sig):
        nonlocal group_alive
        if sig == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        tree_signals.append((pid, sig))
        if sig == voice_ws.signal.SIGTERM:
            process.terminated = True
            process.returncode = -sig
            group_alive = False

    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(voice_ws.os, "killpg", fake_killpg)
    task = voice_ws.asyncio.create_task(voice_ws.delegate_to_hermes("langlaufend"))
    await voice_ws.asyncio.wait_for(process.communicate_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(voice_ws.asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.reaped is True
    assert tree_signals == [(process.pid, voice_ws.signal.SIGTERM)]


@pytest.mark.asyncio
async def test_delegate_timeout_escalates_to_kill_and_reaps_child(monkeypatch):
    from hermes_cli import voice_ws

    class StubbornProcess:
        def __init__(self):
            self.pid = 424_202
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.reaped = False

        async def communicate(self):
            await voice_ws.asyncio.Event().wait()

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if not self.killed:
                await voice_ws.asyncio.Event().wait()
            self.reaped = True
            return self.returncode

    process = StubbornProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    tree_signals = []
    group_alive = True

    def fake_killpg(pid, sig):
        nonlocal group_alive
        if sig == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        tree_signals.append((pid, sig))
        if sig == voice_ws.signal.SIGTERM:
            process.terminated = True
        if sig == voice_ws.signal.SIGKILL:
            process.killed = True
            process.returncode = -sig
            group_alive = False

    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(voice_ws.os, "killpg", fake_killpg)
    monkeypatch.setattr(voice_ws, "_DELEGATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(voice_ws, "_PROCESS_TERMINATE_GRACE_SECONDS", 0.01)

    with pytest.raises(voice_ws.VoiceRuntimeError) as exc:
        await voice_ws.delegate_to_hermes("langlaufend")

    assert exc.value.code == "delegation_timeout"
    assert process.terminated is True
    assert process.killed is True
    assert process.reaped is True
    assert tree_signals == [
        (process.pid, voice_ws.signal.SIGTERM),
        (process.pid, voice_ws.signal.SIGKILL),
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
@pytest.mark.asyncio
async def test_stop_subprocess_kills_term_ignoring_descendant_process_group():
    from hermes_cli import voice_ws

    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(300)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.PIPE, text=True); "
        "assert child.stdout.readline().strip() == 'ready'; "
        "print(child.pid, flush=True); time.sleep(300)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    child_pid = None
    try:
        assert process.stdout is not None
        child_pid = int(
            (
                await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=3,
                )
            ).decode()
        )
        await voice_ws._stop_subprocess(process)

        assert process.returncode is not None
        for _ in range(100):
            if not voice_ws._posix_process_group_exists(
                process.pid
            ) and not voice_ws.psutil.pid_exists(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not voice_ws._posix_process_group_exists(process.pid)
        assert not voice_ws.psutil.pid_exists(child_pid)
    finally:
        try:
            os.killpg(process.pid, voice_ws.signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=2)
        if child_pid is not None and voice_ws.psutil.pid_exists(child_pid):
            try:
                voice_ws.psutil.Process(child_pid).kill()
            except voice_ws.psutil.Error:
                pass


@pytest.mark.asyncio
async def test_interrupt_audio_discard_preserves_queue_accounting():
    from hermes_cli import voice_ws

    events = asyncio.Queue()
    events.put_nowait({"type": "state", "value": "speaking"})
    events.put_nowait({"type": "audio", "data": b"old"})
    events.put_nowait({"type": "state", "value": "listening"})

    voice_ws._discard_queued_response_events(events)

    retained = []
    while not events.empty():
        retained.append(events.get_nowait())
        events.task_done()
    await asyncio.wait_for(events.join(), timeout=1)
    assert retained == []


def test_post_end_interrupt_cancels_and_reaps_blocked_delegate(monkeypatch):
    from hermes_cli import voice_ws

    class BlockingProcess:
        pid = 424_203

        def __init__(self):
            self.returncode = None
            self.started = threading.Event()
            self.reaped = threading.Event()

        async def communicate(self):
            self.started.set()
            await asyncio.Event().wait()

        async def wait(self):
            self.reaped.set()
            return self.returncode

    process = BlockingProcess()
    synthesis_calls = []
    group_alive = True

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    def fake_killpg(pid, sig):
        nonlocal group_alive
        assert pid == process.pid
        if sig == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        process.returncode = -sig
        group_alive = False

    async def fake_transcribe(_pcm, _language):
        return "hallo"

    async def unexpected_synthesis(*args, **kwargs):
        synthesis_calls.append((args, kwargs))
        return b"\x00\x00"

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "fallback_transcribe_pcm", fake_transcribe)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", unexpected_synthesis)
    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(voice_ws.os, "killpg", fake_killpg)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(b"\x01\x00")
        ws.send_json({"type": "end"})
        assert process.started.wait(timeout=2)
        ws.send_json({"type": "interrupt"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {"type": "transcript", "text": "hallo"}
        assert ws.receive_json() == {"type": "interrupted"}
        assert ws.receive()["type"] == "websocket.close"

    assert process.reaped.wait(timeout=1)
    assert synthesis_calls == []


def test_post_end_interrupt_drops_queued_short_tts_response(monkeypatch):
    from hermes_cli import voice_ws

    interrupt_processed = threading.Event()
    original_discard = voice_ws._discard_queued_response_events

    def tracking_discard(events_out):
        original_discard(events_out)
        interrupt_processed.set()

    async def controlled_sender(websocket, events_out, disconnected):
        while not disconnected.is_set():
            event = await events_out.get()
            try:
                if event is None:
                    return
                if event == {"type": "state", "value": "speaking"}:
                    await websocket.send_json(event)
                    while not interrupt_processed.is_set():
                        await asyncio.sleep(0.001)
                elif event.get("type") == "audio":
                    await websocket.send_bytes(event["data"])
                else:
                    await websocket.send_json(event)
            finally:
                events_out.task_done()

    async def fake_transcribe(_pcm, _language):
        return "hallo"

    async def fake_delegate(_prompt):
        return "antwort"

    async def fake_synthesize(_text, _language):
        return b"\x01\x00" * 20

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "fallback_transcribe_pcm", fake_transcribe)
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", fake_synthesize)
    monkeypatch.setattr(voice_ws, "_send_voice_events", controlled_sender)
    monkeypatch.setattr(
        voice_ws,
        "_discard_queued_response_events",
        tracking_discard,
    )

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(b"\x01\x00")
        ws.send_json({"type": "end"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {"type": "transcript", "text": "hallo"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "antwort",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        ws.send_json({"type": "interrupt"})
        assert ws.receive_json() == {"type": "interrupted"}
        assert ws.receive()["type"] == "websocket.close"

    assert interrupt_processed.is_set()


def test_delegate_stderr_never_reaches_websocket(monkeypatch, caplog):
    from hermes_cli import voice_ws

    secret = "SECRET_FROM_HERMES_STDERR"
    synthesis_calls = []

    class FailingProcess:
        pid = 424_204
        returncode = 17

        async def communicate(self):
            return b"", secret.encode()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FailingProcess()

    async def fake_transcribe(_pcm, _language):
        return "hallo"

    async def unexpected_synthesis(*args, **kwargs):
        synthesis_calls.append((args, kwargs))
        return b"\x00\x00"

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "fallback_transcribe_pcm", fake_transcribe)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", unexpected_synthesis)
    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        ws.send_bytes(b"\x01\x00")
        ws.send_json({"type": "end"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {"type": "transcript", "text": "hallo"}
        payload = ws.receive_json()
        assert ws.receive()["type"] == "websocket.close"

    assert payload == {
        "type": "error",
        "error": {
            "code": "delegation_failed",
            "message": "Hermes konnte die Anfrage nicht bearbeiten.",
        },
    }
    assert secret not in json.dumps(payload)
    assert secret not in caplog.text
    assert synthesis_calls == []


@pytest.mark.asyncio
async def test_cancelled_stt_thread_finishes_before_its_temp_file_is_removed(
    tmp_path, monkeypatch
):
    from hermes_cli import voice_ws

    started = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    observed_path = []

    def blocked_transcribe(path):
        observed_path.append(Path(path))
        started.set()
        release.wait(timeout=2)
        worker_done.set()
        return {"success": True, "transcript": "ignored after cancellation"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(voice_ws, "transcribe_audio", blocked_transcribe)
    task = asyncio.create_task(
        voice_ws.fallback_transcribe_pcm(b"\x01\x00" * 4, "de-DE")
    )
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed_path[0].exists()

    release.set()
    assert await asyncio.to_thread(worker_done.wait, 1)
    for _ in range(50):
        if not observed_path[0].exists():
            break
        await asyncio.sleep(0.01)
    assert not observed_path[0].exists()

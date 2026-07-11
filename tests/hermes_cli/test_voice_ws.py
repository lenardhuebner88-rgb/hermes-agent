import asyncio
import base64
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
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.voice_live_session import DEFAULT_SYSTEM_INSTRUCTION, LiveFallbackRequired
from hermes_cli.voice_ws import (
    DEFAULT_LIVE_MODEL,
    VoiceWebConfig,
    create_voice_router,
    voice_web_config,
)


FIXTURE = Path(__file__).parent / "fixtures" / "voice_sample_16k.pcm"
FIXTURE_VIDEO = Path(__file__).parent / "fixtures" / "vision_marker.jpg"


def _video_frame_message(data: bytes, *, source: str | None = "camera") -> dict:
    payload = {"type": "video_frame", "data": base64.b64encode(data).decode("ascii")}
    if source is not None:
        payload["source"] = source
    return {"text": json.dumps(payload)}


def _voice_app(
    *,
    enabled=True,
    auth_reason=(None, "test"),
    host_reason=None,
    client_reason=None,
    auth_required=False,
    session_token="test-session-token",
    extra_voice_web=None,
):
    app = FastAPI()
    app.state.auth_required = auth_required
    voice_web_section = {"enabled": enabled, **(extra_voice_web or {})}
    app.include_router(
        create_voice_router(
            {"voice_web": voice_web_section},
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
    assert cfg.voice == "Puck"
    assert cfg.system_instruction == DEFAULT_SYSTEM_INSTRUCTION

    voice_web_defaults = DEFAULT_CONFIG["voice_web"]
    assert voice_web_defaults["enabled"] is False
    assert voice_web_defaults["model"] == "gemini-2.5-flash-native-audio-preview-12-2025"
    assert voice_web_defaults["language"] == "de-DE"
    assert voice_web_defaults["voice"] == "Puck"
    assert voice_web_defaults["context_compression"] == {
        "trigger_tokens": 25000,
        "target_tokens": 10000,
    }
    assert voice_web_defaults["session_soft_minutes"] == 10
    assert voice_web_defaults["session_max_minutes"] == 15
    assert voice_web_defaults["session_soft_budget_usd"] == 0.35
    assert voice_web_defaults["session_hard_budget_usd"] is None
    assert voice_web_defaults["google_search_enabled"] is False
    assert voice_web_defaults["watch"] == {
        "cooldown_seconds": 30,
        "max_notifications": 3,
    }
    assert "gemini-3.1-flash-live-preview" in voice_web_defaults["pricing"]


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


@pytest.mark.parametrize("value", [None, "", "   ", 42, False, []])
def test_voice_web_config_defaults_invalid_voice_and_system_instruction(value):
    cfg = voice_web_config(
        {"voice_web": {"voice": value, "system_instruction": value}}
    )
    assert cfg.voice == "Puck"
    assert cfg.system_instruction == DEFAULT_SYSTEM_INSTRUCTION


def test_voice_web_config_accepts_voice_and_system_instruction_overrides():
    cfg = voice_web_config({
        "voice_web": {
            # Voice follows the same coercion pattern as model/language: a
            # non-blank string is stored verbatim, not stripped.
            "voice": " Charon ",
            "system_instruction": "  Custom persona text.  ",
        }
    })
    assert cfg.voice == " Charon "
    assert cfg.system_instruction == "Custom persona text."


@pytest.mark.parametrize(
    "compression",
    [
        {"trigger_tokens": 10000, "target_tokens": 10000},  # target must be < trigger
        {"trigger_tokens": 10000, "target_tokens": 20000},  # target > trigger
        {"trigger_tokens": 0, "target_tokens": 100},  # zero fails _positive_int
        {"trigger_tokens": "25000", "target_tokens": "10000"},  # strings
        {"trigger_tokens": 25000},  # missing target
        {},
        "not-a-dict",
    ],
)
def test_voice_web_config_invalid_compression_falls_back_to_defaults(compression):
    cfg = voice_web_config({"voice_web": {"context_compression": compression}})
    assert cfg.context_trigger_tokens == 25000
    assert cfg.context_target_tokens == 10000


def test_voice_web_config_valid_compression_is_passed_through():
    cfg = voice_web_config(
        {
            "voice_web": {
                "context_compression": {"trigger_tokens": 40000, "target_tokens": 5000}
            }
        }
    )
    assert cfg.context_trigger_tokens == 40000
    assert cfg.context_target_tokens == 5000


def test_voice_web_config_compression_trigger_above_100k_falls_back_to_defaults():
    """A huge trigger makes mandatory compression unreachable -> reject it."""
    cfg = voice_web_config(
        {
            "voice_web": {
                "context_compression": {
                    "trigger_tokens": 10**9,
                    "target_tokens": 10000,
                }
            }
        }
    )
    assert cfg.context_trigger_tokens == 25000
    assert cfg.context_target_tokens == 10000


def test_voice_web_config_compression_trigger_at_100k_is_accepted():
    cfg = voice_web_config(
        {
            "voice_web": {
                "context_compression": {
                    "trigger_tokens": 100_000,
                    "target_tokens": 10000,
                }
            }
        }
    )
    assert cfg.context_trigger_tokens == 100_000
    assert cfg.context_target_tokens == 10000


def test_voice_web_config_compression_trigger_above_100k_by_one_falls_back():
    cfg = voice_web_config(
        {
            "voice_web": {
                "context_compression": {
                    "trigger_tokens": 100_001,
                    "target_tokens": 10000,
                }
            }
        }
    )
    assert cfg.context_trigger_tokens == 25000
    assert cfg.context_target_tokens == 10000


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("session_soft_budget_usd", 1.5, 1.5),
        ("session_soft_budget_usd", None, None),
        ("session_hard_budget_usd", 2, 2.0),
        ("session_hard_budget_usd", None, None),
    ],
)
def test_voice_web_config_budget_accepts_float_or_none(key, value, expected):
    cfg = voice_web_config({"voice_web": {key: value}})
    assert getattr(cfg, key) == expected


@pytest.mark.parametrize("key", ["session_soft_budget_usd", "session_hard_budget_usd"])
@pytest.mark.parametrize("value", ["1.5", True, [1.5], "not-a-number"])
def test_voice_web_config_budget_invalid_shape_falls_back_to_default(key, value):
    default = 0.35 if key == "session_soft_budget_usd" else None
    cfg = voice_web_config({"voice_web": {key: value}})
    assert getattr(cfg, key) == default


@pytest.mark.parametrize("key", ["session_soft_budget_usd", "session_hard_budget_usd"])
@pytest.mark.parametrize("value", [float("nan"), -1, float("inf"), 0])
def test_voice_web_config_budget_nonfinite_or_nonpositive_falls_back_to_default(
    key, value
):
    """NaN never fires the hard-budget stop silently; negative/zero/inf ends
    a session immediately or never — all fail safe to the default instead."""
    default = 0.35 if key == "session_soft_budget_usd" else None
    cfg = voice_web_config({"voice_web": {key: value}})
    assert getattr(cfg, key) == default


@pytest.mark.parametrize("key", ["session_soft_budget_usd", "session_hard_budget_usd"])
def test_voice_web_config_budget_positive_fraction_is_accepted(key):
    cfg = voice_web_config({"voice_web": {key: 0.5}})
    assert getattr(cfg, key) == 0.5


@pytest.mark.parametrize(
    "watch",
    [
        {"cooldown_seconds": -1, "max_notifications": 3},
        {"cooldown_seconds": 30, "max_notifications": -1},
        {"cooldown_seconds": "30", "max_notifications": 3},
        {},
        "not-a-dict",
    ],
)
def test_voice_web_config_invalid_watch_falls_back_to_defaults(watch):
    cfg = voice_web_config({"voice_web": {"watch": watch}})
    assert cfg.watch_cooldown_seconds == 30.0
    assert cfg.watch_max_notifications == 3


def test_voice_web_config_valid_watch_is_passed_through():
    cfg = voice_web_config(
        {"voice_web": {"watch": {"cooldown_seconds": 5, "max_notifications": 1}}}
    )
    assert cfg.watch_cooldown_seconds == 5.0
    assert cfg.watch_max_notifications == 1


def test_voice_web_config_video_mode_defaults_to_on_demand():
    cfg = voice_web_config({})
    assert cfg.video_mode == "on_demand"
    assert DEFAULT_CONFIG["voice_web"]["video_mode"] == "on_demand"
    assert DEFAULT_CONFIG["voice_web"]["look_model"] == "gemini-3.1-flash-lite"
    assert "gemini-3.1-flash-lite" in DEFAULT_CONFIG["voice_web"]["pricing"]


def test_voice_web_config_video_mode_accepts_stream():
    cfg = voice_web_config({"voice_web": {"video_mode": "stream"}})
    assert cfg.video_mode == "stream"


@pytest.mark.parametrize("value", ["invalid", "", 1, None, ["stream"]])
def test_voice_web_config_video_mode_invalid_falls_back_to_on_demand(value):
    cfg = voice_web_config({"voice_web": {"video_mode": value}})
    assert cfg.video_mode == "on_demand"


@pytest.mark.parametrize("value", [None, "", "   ", 42, False, []])
def test_voice_web_config_look_model_falls_back_when_invalid(value):
    cfg = voice_web_config({"voice_web": {"look_model": value}})
    assert cfg.look_model == "gemini-3.1-flash-lite"


def test_voice_web_config_look_model_accepts_override():
    cfg = voice_web_config({"voice_web": {"look_model": "gemini-custom-lite"}})
    assert cfg.look_model == "gemini-custom-lite"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", False),  # only `is True` counts; truthy strings don't
        (1, False),
        (None, False),
        ([True], False),
    ],
)
def test_voice_web_config_google_search_enabled_only_literal_true(value, expected):
    cfg = voice_web_config({"voice_web": {"google_search_enabled": value}})
    assert cfg.google_search_enabled is expected


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
    app.js' ``register("/voice/sw.js", { scope: "/voice/" })``). Sibling
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
        'navigator.serviceWorker.register("/voice/sw.js", { scope: "/voice/" })'
        in script
    )
    assert ".catch(() => {})" in script


def test_voice_client_renders_partial_transcripts_with_fallback_compat():
    """C3: live captions stream via ``upsertTranscript`` and stay compatible
    with the cascade fallback, which never sends a "partial" field at all.
    """
    script_path = Path(__file__).parents[2] / "hermes_cli" / "voice_client" / "app.js"
    script = script_path.read_text(encoding="utf-8")

    assert "function upsertTranscript(session, role, text, partial)" in script
    assert "session.pendingTranscript" in script
    assert "upsertTranscript(" in script
    # A missing "partial" field (the cascade fallback's shape) must coerce to
    # `false` via strict equality, so those transcripts still render
    # immediately and completely, exactly as before this slice.
    assert "message.partial === true" in script


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
    addEventListener() {},
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

        def watch_view(self, instruction):
            return {"watching": True, "instruction": instruction}

        def stop_watching(self):
            return {"watching": False, "was_watching": True}

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
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
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
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
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
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
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["error"]["code"] == "invalid_pcm_frame"


def test_resumption_registry_store_get_roundtrip():
    from hermes_cli import voice_ws

    registry = voice_ws.ResumptionRegistry()
    registry.store("session-a", "handle-1")

    assert registry.get("session-a") == "handle-1"


def test_resumption_registry_store_none_clears_entry():
    from hermes_cli import voice_ws

    registry = voice_ws.ResumptionRegistry()
    registry.store("session-a", "handle-1")
    registry.store("session-a", None)

    assert registry.get("session-a") is None


def test_resumption_registry_ttl_expiry(monkeypatch):
    from hermes_cli import voice_ws

    registry = voice_ws.ResumptionRegistry()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(voice_ws.time, "monotonic", lambda: clock["now"])

    registry.store("session-a", "handle-1")
    clock["now"] += voice_ws._RESUMPTION_TTL_SECONDS + 1

    assert registry.get("session-a") is None


def test_resumption_registry_lru_eviction_at_cap():
    from hermes_cli import voice_ws

    registry = voice_ws.ResumptionRegistry()
    for index in range(voice_ws._RESUMPTION_MAX_ENTRIES + 1):
        registry.store(f"session-{index}", f"handle-{index}")

    assert registry.get("session-0") is None
    assert registry.get("session-1") == "handle-1"
    last_index = voice_ws._RESUMPTION_MAX_ENTRIES
    assert registry.get(f"session-{last_index}") == f"handle-{last_index}"


def test_resumption_registry_get_refreshes_recency():
    from hermes_cli import voice_ws

    registry = voice_ws.ResumptionRegistry()
    registry.store("oldest", "handle-oldest")
    for index in range(1, voice_ws._RESUMPTION_MAX_ENTRIES):
        registry.store(f"session-{index}", f"handle-{index}")
    registry.get("oldest")  # bump "oldest" back to most-recently-used

    registry.store("newcomer", "handle-newcomer")

    assert registry.get("oldest") == "handle-oldest"
    assert registry.get("session-1") is None


def test_live_route_seeds_and_updates_resumption_handle_for_valid_session(
    monkeypatch,
):
    from hermes_cli import voice_ws

    fresh_registry = voice_ws.ResumptionRegistry()
    fresh_registry.store("a1b2c3d4-x", "handle-1")
    monkeypatch.setattr(voice_ws, "_RESUMPTION_REGISTRY", fresh_registry)
    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    captured = {}
    constructed = threading.Event()

    class CapturingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            constructed.set()

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)

    with TestClient(_voice_app()).websocket_connect(
        "/api/voice/live?session=a1b2c3d4-x"
    ) as ws:
        assert constructed.wait(timeout=2)
        assert captured["kwargs"]["initial_handle"] == "handle-1"
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    on_handle_update = captured["kwargs"]["on_handle_update"]
    on_handle_update("handle-2")
    assert fresh_registry.get("a1b2c3d4-x") == "handle-2"

    on_handle_update(None)
    assert fresh_registry.get("a1b2c3d4-x") is None


@pytest.mark.parametrize("raw_session", ["short", "bad_underscore_chars"])
def test_live_route_ignores_malformed_session_param(monkeypatch, raw_session):
    from hermes_cli import voice_ws

    fresh_registry = voice_ws.ResumptionRegistry()
    monkeypatch.setattr(voice_ws, "_RESUMPTION_REGISTRY", fresh_registry)
    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    captured = {}
    constructed = threading.Event()

    class CapturingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            constructed.set()

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)

    with TestClient(_voice_app()).websocket_connect(
        f"/api/voice/live?session={raw_session}"
    ) as ws:
        assert constructed.wait(timeout=2)
        assert captured["kwargs"].get("initial_handle") is None
        assert "on_handle_update" not in captured["kwargs"]
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    assert not fresh_registry._entries


def test_live_route_passes_configured_voice_and_persona_to_session(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    captured = {}
    constructed = threading.Event()

    class CapturingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            constructed.set()

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)

    app = _voice_app(
        extra_voice_web={
            "voice": "Charon",
            "system_instruction": "Custom persona text.",
        }
    )
    with TestClient(app).websocket_connect("/api/voice/live") as ws:
        assert constructed.wait(timeout=2)
        assert captured["kwargs"]["voice"] == "Charon"
        assert captured["kwargs"]["system_instruction"] == "Custom persona text."
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"


def test_live_session_mode_live_event_precedes_listening_state(monkeypatch):
    from hermes_cli import voice_ws

    class RealisticGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            pass

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            await events_out.put({"type": "mode", "value": "live"})
            await events_out.put({"type": "state", "value": "listening"})
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(voice_ws, "GeminiLiveSession", RealisticGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        assert ws.receive_json() == {"type": "mode", "value": "live"}
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"


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


async def _read_test_voice_frames(
    websocket, fallback_mode, *, video_mode="stream", frame_cache=None
):
    """Defaults to ``video_mode="stream"`` so every pre-existing caller keeps
    exercising today's video_in-relay behavior unchanged (see
    ``test_video_frame_on_demand_*`` for the on_demand cache path)."""
    from hermes_cli import voice_ws

    audio_in = asyncio.Queue(maxsize=32)
    fallback_pcm = bytearray()
    events_out = asyncio.Queue()
    disconnected = asyncio.Event()
    text_in = asyncio.Queue(maxsize=8)
    video_in = asyncio.Queue(maxsize=voice_ws._VIDEO_QUEUE_MAXSIZE)
    config = voice_ws.VoiceWebConfig(video_mode=video_mode)
    text_turn = voice_ws._FallbackTextTurn()
    if frame_cache is None:
        frame_cache = voice_ws.VideoFrameCache()
    result = await voice_ws._read_voice_frames(
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
    )
    return result, fallback_pcm, events_out, video_in


@pytest.mark.asyncio
async def test_healthy_live_audio_keeps_recent_bounded_fallback_preroll(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MAX_FALLBACK_PCM_BYTES", 12)
    monkeypatch.setattr(voice_ws, "_FALLBACK_PREROLL_PCM_BYTES", 4)
    frames = [bytes([value, 0]) * 2 for value in range(1, 6)]
    messages = [{"bytes": frame} for frame in frames]
    messages.append({"text": json.dumps({"type": "end"})})

    result, fallback_pcm, events_out, _video_in = await _read_test_voice_frames(
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

    result, fallback_pcm, events_out, _video_in = await _read_test_voice_frames(
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

    result, fallback_pcm, events_out, _video_in = await _read_test_voice_frames(
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

    result, fallback_pcm, events_out, _video_in = await _read_test_voice_frames(
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

    result, fallback_pcm, events_out, _video_in = await _read_test_voice_frames(
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
        "-z",
        "prüfe den Status",
    )
    assert observed["kwargs"]["stdout"] is voice_ws.asyncio.subprocess.PIPE
    assert observed["kwargs"]["stderr"] is voice_ws.asyncio.subprocess.PIPE
    if os.name == "posix":
        assert observed["kwargs"]["start_new_session"] is True


@pytest.mark.asyncio
async def test_delegate_image_is_private_cli_attachment_and_deleted(monkeypatch, tmp_path):
    from hermes_cli import voice_ws

    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            image_path = Path(observed["args"][observed["args"].index("--image") + 1])
            observed["image_path"] = image_path
            observed["image_bytes"] = image_path.read_bytes()
            observed["image_mode"] = image_path.stat().st_mode & 0o777
            observed["directory_mode"] = image_path.parent.stat().st_mode & 0o777
            return "session_id: voice-test\nBild geprüft".encode(), b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        observed["args"] = args
        return FakeProcess()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        voice_ws.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    frame = FIXTURE_VIDEO.read_bytes()

    response = await voice_ws.delegate_to_hermes(
        "prüfe den sichtbaren Fehler",
        image=frame,
    )

    assert response == "Bild geprüft"
    assert observed["args"] == (
        sys.executable,
        str(voice_ws._HERMES_CLI_ENTRYPOINT),
        "-q",
        "prüfe den sichtbaren Fehler",
        "--image",
        str(observed["image_path"]),
        "--quiet",
    )
    assert observed["image_bytes"] == frame
    assert observed["image_mode"] == 0o600
    assert observed["directory_mode"] == 0o700
    assert not observed["image_path"].exists()


def test_voice_attachment_sweep_removes_only_expired_jpegs(tmp_path):
    from hermes_cli import voice_ws

    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir()
    expired = attachment_dir / "expired.jpg"
    fresh = attachment_dir / "fresh.jpg"
    unrelated = attachment_dir / "keep.txt"
    for path in (expired, fresh, unrelated):
        path.write_bytes(b"data")
    now = 2_000_000.0
    os.utime(
        expired,
        (now - voice_ws._VOICE_ATTACHMENT_RETENTION_SECONDS - 1,) * 2,
    )
    os.utime(fresh, (now - 1,) * 2)
    os.utime(unrelated, (now - voice_ws._VOICE_ATTACHMENT_RETENTION_SECONDS - 1,) * 2)

    voice_ws._sweep_voice_attachments(attachment_dir, now=now)

    assert not expired.exists()
    assert fresh.exists()
    assert unrelated.exists()


@pytest.mark.asyncio
async def test_voice_attachment_scheduled_cleanup_enforces_deadline(
    monkeypatch, tmp_path
):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_VOICE_ATTACHMENT_RETENTION_SECONDS", 0.01)
    attachment = tmp_path / "crash-leftover.jpg"
    attachment.write_bytes(b"jpeg")

    task = voice_ws._schedule_voice_attachment_cleanup(attachment)
    await asyncio.wait_for(task, timeout=1)

    assert not attachment.exists()
    assert task not in voice_ws._VOICE_ATTACHMENT_CLEANUP_TASKS


def test_delegate_entrypoints_expose_required_live_cli_contracts():
    from hermes_cli import voice_ws

    oneshot_help = subprocess.run(
        [voice_ws.resolve_hermes_executable(), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    image_help = subprocess.run(
        [sys.executable, str(voice_ws._HERMES_CLI_ENTRYPOINT), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert oneshot_help.returncode == 0
    assert "-z" in oneshot_help.stdout
    assert image_help.returncode == 0
    image_help_text = image_help.stdout + image_help.stderr
    assert "--image" in image_help_text
    assert "--quiet" in image_help_text


@pytest.mark.asyncio
async def test_delegate_image_write_failure_removes_partial_attachment(
    monkeypatch, tmp_path
):
    from hermes_cli import voice_ws

    original_write_bytes = Path.write_bytes

    def partial_write_then_fail(path, data):
        original_write_bytes(path, data)
        raise OSError("disk failure after partial write")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "write_bytes", partial_write_then_fail)

    with pytest.raises(voice_ws.VoiceRuntimeError) as error:
        await voice_ws.delegate_to_hermes(
            "prüfe den sichtbaren Fehler",
            image=FIXTURE_VIDEO.read_bytes(),
        )

    assert error.value.code == "delegation_image_unavailable"
    attachments = tmp_path / "cache" / "voice-web" / "attachments"
    assert list(attachments.glob("*.jpg")) == []


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

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
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

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
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
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
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
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
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
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
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


@pytest.mark.asyncio
async def test_delegate_to_hermes_defaults_to_the_cascade_timeout(monkeypatch):
    """Omitting ``timeout_seconds`` must still resolve the current cascade
    budget dynamically (not a value frozen at import time), so a config/test
    override of ``_DELEGATE_TIMEOUT_SECONDS`` keeps applying to unspecified
    callers — this is exactly what
    ``test_delegate_timeout_escalates_to_kill_and_reaps_child`` above relies
    on when it patches the module constant without passing the kwarg."""
    from hermes_cli import voice_ws

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"erledigt", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    captured = {}

    async def fake_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(
        voice_ws.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(voice_ws.asyncio, "wait_for", fake_wait_for)

    response = await voice_ws.delegate_to_hermes("status?")

    assert response == "erledigt"
    assert captured["timeout"] == voice_ws._DELEGATE_TIMEOUT_SECONDS == 120.0


@pytest.mark.asyncio
async def test_run_live_bridge_delegate_uses_the_live_timeout(monkeypatch):
    """The Live bridge's executor must delegate with the 600s Live budget,
    not the 120s cascade default, since a NON_BLOCKING delegation may
    legitimately outlast one cascade turn."""
    from hermes_cli import voice_ws

    class FakeGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            pass

        def watch_view(self, instruction):
            return {"watching": True, "instruction": instruction}

        def stop_watching(self):
            return {"watching": False, "was_watching": True}

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            raise voice_ws.LiveFallbackRequired("done")

    captured_executor = {}

    class SpyExecutor:
        def __init__(
            self,
            *,
            delegate,
            delegate_with_image,
            watch_view,
            stop_watching,
            **_extra,
        ):
            captured_executor["delegate"] = delegate
            captured_executor["delegate_with_image"] = delegate_with_image
            captured_executor["watch_view"] = watch_view
            captured_executor["stop_watching"] = stop_watching

    recorded = {}

    async def fake_delegate(prompt, *, timeout_seconds=None, image=None):
        recorded["prompt"] = prompt
        recorded["timeout_seconds"] = timeout_seconds
        recorded["image"] = image
        return "ok"

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", FakeGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "VoiceToolExecutor", SpyExecutor)
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)

    config = VoiceWebConfig(enabled=True)
    await voice_ws._run_live_bridge(
        config,
        "api-key",
        asyncio.Queue(),
        asyncio.Queue(),
        asyncio.Event(),
        asyncio.Event(),
        None,
    )

    result = await captured_executor["delegate"]("mach das")
    image_result = await captured_executor["delegate_with_image"](
        "mach das mit Bild", b"\xff\xd8frame\xff\xd9"
    )
    watch_result = captured_executor["watch_view"]("Prüfe den Build")
    stop_result = captured_executor["stop_watching"]()

    assert result == "ok"
    assert image_result == "ok"
    assert recorded["prompt"] == "mach das mit Bild"
    assert recorded["timeout_seconds"] == voice_ws._DELEGATE_LIVE_TIMEOUT_SECONDS == 600.0
    assert recorded["image"] == b"\xff\xd8frame\xff\xd9"
    assert watch_result == {"watching": True, "instruction": "Prüfe den Build"}
    assert stop_result == {"watching": False, "was_watching": True}


def test_live_mode_text_frame_emits_transcript_and_reaches_session_text_in(
    monkeypatch,
):
    """C5: a typed turn in Live mode gets its own (non-partial) transcript
    event and is handed to the Live bridge's ``text_in`` queue verbatim —
    Gemini never transcribes typed input, so the server is the only source
    of that transcript event."""
    from hermes_cli import voice_ws

    captured = {}
    constructed = threading.Event()

    class CapturingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            constructed.set()

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            captured["text_in"] = text_in
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        assert constructed.wait(timeout=2)
        ws.send_json({"type": "text", "text": "Wie spät ist es?"})
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "Wie spät ist es?",
        }
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    text_in = captured["text_in"]
    assert text_in.get_nowait() == "Wie spät ist es?"


def test_fallback_text_frame_runs_full_cascade_to_audio_and_listening(monkeypatch):
    """(a) A typed turn while in fallback mode reuses the cascade pieces
    minus STT: transcript(user) -> thinking -> transcript(assistant) ->
    speaking -> >=1 audio frame -> listening."""
    from hermes_cli import voice_ws

    async def fake_delegate(prompt):
        assert prompt == "Wie spät ist es?"
        return "Es ist 14 Uhr."

    async def fake_synthesize(text, language):
        assert text == "Es ist 14 Uhr."
        assert language == "de-DE"
        return b"\x01\x00" * 240

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", fake_synthesize)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
        ws.send_json({"type": "text", "text": "Wie spät ist es?"})
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "Wie spät ist es?",
        }
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "Es ist 14 Uhr.",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        audio = ws.receive_bytes()
        assert ws.receive_json() == {"type": "state", "value": "listening"}

    assert audio


def test_fallback_second_text_frame_while_first_turn_running_gets_text_busy(
    monkeypatch,
):
    """(b) A second typed turn arriving while the first is still in flight
    (gated open on delegate_to_hermes) is rejected as text_busy; the first
    turn still completes normally afterward."""
    from hermes_cli import voice_ws

    delegate_started = threading.Event()
    release_delegate = threading.Event()

    async def gated_delegate(prompt):
        delegate_started.set()
        await asyncio.to_thread(release_delegate.wait)
        return f"Antwort auf: {prompt}"

    async def fake_synthesize(text, language):
        return b"\x01\x00" * 4

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", gated_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", fake_synthesize)

    try:
        with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
            assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
            ws.send_json({"type": "text", "text": "erste Nachricht"})
            assert ws.receive_json() == {
                "type": "transcript",
                "role": "user",
                "text": "erste Nachricht",
            }
            assert ws.receive_json() == {"type": "state", "value": "thinking"}
            assert delegate_started.wait(timeout=2)

            ws.send_json({"type": "text", "text": "zweite Nachricht"})
            assert ws.receive_json() == {
                "type": "error",
                "error": {
                    "code": "text_busy",
                    "message": "Eine Anfrage läuft bereits. Bitte warte kurz.",
                },
            }

            release_delegate.set()
            assert ws.receive_json() == {
                "type": "transcript",
                "role": "assistant",
                "text": "Antwort auf: erste Nachricht",
            }
            assert ws.receive_json() == {"type": "state", "value": "speaking"}
            assert ws.receive_bytes()
            assert ws.receive_json() == {"type": "state", "value": "listening"}
    finally:
        release_delegate.set()


def test_interrupt_stops_running_text_turn_before_audio(monkeypatch):
    """(c) An interrupt while a typed turn is gated open inside TTS stops
    it: the interrupted event arrives and no audio is ever produced."""
    from hermes_cli import voice_ws

    tts_started = threading.Event()

    async def fake_delegate(prompt):
        return "Antwort"

    async def hanging_synthesize(text, language):
        tts_started.set()
        # A plain, never-set asyncio.Event responds to task cancellation
        # immediately (unlike a threading.Event awaited via asyncio.to_thread,
        # whose executor thread cannot be interrupted once running) — this
        # mirrors how the real fallback_synthesize_pcm resolves promptly on
        # cancellation via asyncio.shield (see _run_sync_cancel_safe).
        await asyncio.Event().wait()
        return b"\x00\x00"  # unreachable: nothing ever sets that event

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", hanging_synthesize)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }
        ws.send_json({"type": "text", "text": "spiel etwas ab"})
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "spiel etwas ab",
        }
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "Antwort",
        }
        assert tts_started.wait(timeout=2)

        ws.send_json({"type": "interrupt"})
        assert ws.receive_json() == {"type": "interrupted"}


def test_invalid_text_frames_return_structured_error_and_session_keeps_working(
    monkeypatch,
):
    """(d) empty, whitespace-only, over-length, and non-string ``text``
    fields are all rejected as invalid_text_frame without breaking the
    session — a valid frame afterward still runs the full cascade."""
    from hermes_cli import voice_ws

    async def fake_delegate(prompt):
        return f"Antwort: {prompt}"

    async def fake_synthesize(text, language):
        return b"\x01\x00" * 4

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "")
    monkeypatch.setattr(voice_ws, "delegate_to_hermes", fake_delegate)
    monkeypatch.setattr(voice_ws, "fallback_synthesize_pcm", fake_synthesize)

    with TestClient(_voice_app()).websocket_connect("/api/voice/live") as ws:
        assert ws.receive_json() == {
            "type": "mode",
            "value": "fallback",
            "video_mode": "on_demand",
        }

        for invalid_text in ("", "   ", "x" * 4001, 12345):
            ws.send_json({"type": "text", "text": invalid_text})
            payload = ws.receive_json()
            assert payload["type"] == "error"
            assert payload["error"]["code"] == "invalid_text_frame"

        ws.send_json({"type": "text", "text": "funktioniert das noch?"})
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "funktioniert das noch?",
        }
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "Antwort: funktioniert das noch?",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        assert ws.receive_bytes()
        assert ws.receive_json() == {"type": "state", "value": "listening"}


def test_live_route_video_frame_reaches_session_video_in_byte_exact(monkeypatch):
    """Real client wire format: ``{"type":"video_frame","data":"<base64-
    jpeg>","source":"camera"}`` must arrive byte-exact in the Live bridge's
    ``video_in`` queue, decoded from a real JPEG fixture (live-verified
    against the Gemini API). Regression pin for ``video_mode: stream`` —
    today's default is ``on_demand`` (see the on_demand cache tests below),
    so this test opts explicitly into the pre-2026-07-10 relay behavior."""
    from hermes_cli import voice_ws

    fixture_bytes = FIXTURE_VIDEO.read_bytes()
    captured = {}
    constructed = threading.Event()

    class CapturingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            constructed.set()

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            captured["video_in"] = video_in
            await asyncio.Event().wait()

    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")
    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "_LIVE_END_GRACE_SECONDS", 0.01)

    app = _voice_app(extra_voice_web={"video_mode": "stream"})
    with TestClient(app).websocket_connect("/api/voice/live") as ws:
        assert constructed.wait(timeout=2)
        ws.send_json(
            {
                "type": "video_frame",
                "data": base64.b64encode(fixture_bytes).decode("ascii"),
                "source": "camera",
            }
        )
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    video_in = captured["video_in"]
    assert video_in.get_nowait() == fixture_bytes


@pytest.mark.asyncio
async def test_video_frame_oversize_rejected_then_valid_frame_still_lands():
    from hermes_cli import voice_ws

    oversized = voice_ws._VIDEO_FRAME_MAGIC + b"\x00" * voice_ws._MAX_VIDEO_FRAME_BYTES
    valid = FIXTURE_VIDEO.read_bytes()
    messages = [
        _video_frame_message(oversized),
        _video_frame_message(valid),
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages), asyncio.Event()
    )

    assert result == "end"
    error_event = events_out.get_nowait()
    assert error_event["type"] == "error"
    assert error_event["error"]["code"] == "video_frame_too_large"
    assert events_out.empty()
    assert video_in.get_nowait() == valid


@pytest.mark.asyncio
async def test_video_frame_rate_limit_drops_third_rapid_frame():
    valid = FIXTURE_VIDEO.read_bytes()
    messages = [
        _video_frame_message(valid),
        _video_frame_message(valid),
        _video_frame_message(valid),
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages), asyncio.Event()
    )

    assert result == "end"
    error_event = events_out.get_nowait()
    assert error_event["type"] == "error"
    assert error_event["error"]["code"] == "video_rate_limited"
    assert events_out.empty()
    landed = []
    while not video_in.empty():
        landed.append(video_in.get_nowait())
    assert landed == [valid, valid]


@pytest.mark.asyncio
async def test_video_frame_fallback_mode_returns_advisory_and_queues_nothing():
    valid = FIXTURE_VIDEO.read_bytes()
    fallback_mode = asyncio.Event()
    fallback_mode.set()
    messages = [
        _video_frame_message(valid),
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages), fallback_mode
    )

    assert result == "end"
    assert events_out.get_nowait() == {
        "type": "error",
        "error": {
            "code": "video_unavailable_fallback",
            "message": "Sehen ist im Fallback-Modus nicht verfügbar.",
        },
    }
    assert events_out.empty()
    assert video_in.empty()


@pytest.mark.asyncio
async def test_video_frame_invalid_base64_and_non_jpeg_return_invalid_video_frame():
    messages = [
        {
            "text": json.dumps(
                {
                    "type": "video_frame",
                    "data": "not-base64!!",
                    "source": "camera",
                }
            )
        },
        {
            "text": json.dumps(
                {
                    "type": "video_frame",
                    "data": base64.b64encode(b"not a jpeg at all").decode("ascii"),
                    "source": "camera",
                }
            )
        },
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages), asyncio.Event()
    )

    assert result == "end"
    for _ in range(2):
        error_event = events_out.get_nowait()
        assert error_event["type"] == "error"
        assert error_event["error"]["code"] == "invalid_video_frame"
    assert events_out.empty()
    assert video_in.empty()


@pytest.mark.asyncio
async def test_video_frame_missing_or_invalid_source_returns_invalid_video_frame():
    valid = FIXTURE_VIDEO.read_bytes()
    messages = [
        _video_frame_message(valid, source=None),
        _video_frame_message(valid, source="microphone"),
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages), asyncio.Event()
    )

    assert result == "end"
    for _ in range(2):
        error_event = events_out.get_nowait()
        assert error_event["type"] == "error"
        assert error_event["error"]["code"] == "invalid_video_frame"
    assert events_out.empty()
    assert video_in.empty()


def test_enqueue_video_frame_drops_oldest_when_queue_is_full():
    from hermes_cli import voice_ws

    video_in = asyncio.Queue(maxsize=voice_ws._VIDEO_QUEUE_MAXSIZE)
    frames = [f"frame-{index}".encode() for index in range(5)]
    for frame in frames:
        voice_ws._enqueue_video_frame(video_in, frame)

    remaining = []
    while not video_in.empty():
        remaining.append(video_in.get_nowait())

    assert remaining == frames[1:]


def test_enqueue_sharing_stopped_drops_stale_frames_and_routes_sentinel():
    from hermes_cli import voice_ws

    video_in = asyncio.Queue(maxsize=voice_ws._VIDEO_QUEUE_MAXSIZE)
    voice_ws._enqueue_video_frame(video_in, b"stale-1")
    voice_ws._enqueue_video_frame(video_in, b"stale-2")

    voice_ws._enqueue_sharing_stopped(video_in)

    assert video_in.get_nowait() is None
    assert video_in.empty()


@pytest.mark.asyncio
async def test_video_frame_on_demand_mode_caches_and_still_enqueues_to_video_in():
    """Real client wire format through ``_read_voice_frames``: enqueueing
    into ``video_in`` is unconditional on video_mode (see
    ``_read_voice_frames``'s docstring comment) — gating whether on_demand
    actually forwards a still into the upstream Live connection happens
    downstream at ``_VideoFrameRelay.forward_to_live``, not here. The real
    JPEG fixture lands byte-exact in both the queue and the frame cache."""
    from hermes_cli import voice_ws

    real_jpeg = FIXTURE_VIDEO.read_bytes()
    frame_cache = voice_ws.VideoFrameCache()
    messages = [
        _video_frame_message(real_jpeg),
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages),
        asyncio.Event(),
        video_mode="on_demand",
        frame_cache=frame_cache,
    )

    assert result == "end"
    assert events_out.empty()
    assert video_in.get_nowait() == real_jpeg
    assert frame_cache.peek() == real_jpeg


@pytest.mark.asyncio
async def test_video_frame_stream_mode_still_relays_to_video_in_regression():
    """Regression: video_mode="stream" reproduces pre-2026-07-10 behavior —
    the frame lands in video_in exactly as before."""
    from hermes_cli import voice_ws

    real_jpeg = FIXTURE_VIDEO.read_bytes()
    frame_cache = voice_ws.VideoFrameCache()
    messages = [
        _video_frame_message(real_jpeg),
        {"text": json.dumps({"type": "end"})},
    ]

    result, _fallback_pcm, events_out, video_in = await _read_test_voice_frames(
        _FakeVoiceInput(messages),
        asyncio.Event(),
        video_mode="stream",
        frame_cache=frame_cache,
    )

    assert result == "end"
    assert events_out.empty()
    assert video_in.get_nowait() == real_jpeg
    # Stream mode also caches, so look_closely keeps working regardless of mode.
    assert frame_cache.peek() == real_jpeg


@pytest.mark.asyncio
async def test_video_frame_sharing_stopped_clears_the_frame_cache():
    from hermes_cli import voice_ws

    real_jpeg = FIXTURE_VIDEO.read_bytes()
    frame_cache = voice_ws.VideoFrameCache()
    messages = [
        _video_frame_message(real_jpeg),
        {"text": json.dumps({"type": "sharing_stopped"})},
        {"text": json.dumps({"type": "end"})},
    ]

    await _read_test_voice_frames(
        _FakeVoiceInput(messages),
        asyncio.Event(),
        video_mode="on_demand",
        frame_cache=frame_cache,
    )

    assert frame_cache.peek() is None


@pytest.mark.asyncio
async def test_video_frame_cache_wait_for_update_returns_fresh_frame():
    from hermes_cli import voice_ws

    cache = voice_ws.VideoFrameCache()

    async def store_soon():
        await asyncio.sleep(0.01)
        cache.store(b"\xff\xd8fresh\xff\xd9")

    task = asyncio.create_task(store_soon())
    result = await cache.wait_for_update(timeout=1.0)
    await task

    assert result == b"\xff\xd8fresh\xff\xd9"


@pytest.mark.asyncio
async def test_video_frame_cache_wait_for_update_falls_back_to_stale_cache_on_timeout():
    from hermes_cli import voice_ws

    cache = voice_ws.VideoFrameCache()
    cache.store(b"\xff\xd8stale\xff\xd9")

    result = await cache.wait_for_update(timeout=0.01)

    assert result == b"\xff\xd8stale\xff\xd9"


@pytest.mark.asyncio
async def test_video_frame_cache_wait_for_update_returns_none_when_never_offered():
    from hermes_cli import voice_ws

    cache = voice_ws.VideoFrameCache()

    result = await cache.wait_for_update(timeout=0.01)

    assert result is None


@pytest.mark.asyncio
async def test_run_live_bridge_wires_look_closely_request_frame_and_usage(
    monkeypatch,
):
    """request_frame is served directly from frame_cache — no client
    round-trip event — since ``_read_voice_frames`` always populates the
    cache regardless of video_mode."""
    from hermes_cli import voice_ws
    from tools.voice_live_tools import VoiceToolExecutor

    real_jpeg = FIXTURE_VIDEO.read_bytes()
    captured_executor_kwargs = {}

    class RecordingGeminiLiveSession:
        def __init__(self, *args, **kwargs):
            self.usage_reports = []

        def record_look_closely_usage(self, input_tokens, output_tokens, complete=True):
            self.usage_reports.append((input_tokens, output_tokens, complete))

        async def run(
            self, audio_in, events_out, tool_executor, text_in=None, video_in=None
        ):
            await asyncio.Event().wait()

    def spy_executor(**kwargs):
        captured_executor_kwargs.update(kwargs)
        return VoiceToolExecutor(**kwargs)

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", RecordingGeminiLiveSession)
    monkeypatch.setattr(voice_ws, "VoiceToolExecutor", spy_executor)

    audio_in = asyncio.Queue()
    events_out = asyncio.Queue(maxsize=8)
    disconnected = asyncio.Event()
    frame_cache = voice_ws.VideoFrameCache()
    frame_cache.store(real_jpeg)
    config = voice_ws.VoiceWebConfig(video_mode="on_demand")

    bridge_task = asyncio.create_task(
        voice_ws._run_live_bridge(
            config,
            "server-key",
            audio_in,
            events_out,
            asyncio.Event(),
            disconnected,
            None,
            None,
            None,
            None,
            frame_cache,
        )
    )
    await asyncio.sleep(0.01)

    request_frame = captured_executor_kwargs["request_frame"]
    frame = await request_frame()
    assert frame == real_jpeg
    # No client round-trip: request_frame() reads the cache directly, so no
    # "request_frame" (or any other) event is enqueued.
    assert events_out.empty()

    captured_executor_kwargs["report_look_usage"](120, 14, True)
    assert captured_executor_kwargs["gemini_api_key"] == "server-key"
    assert captured_executor_kwargs["look_model"] == config.look_model

    bridge_task.cancel()
    await asyncio.gather(bridge_task, return_exceptions=True)


def test_voice_client_composer_form_present_with_max_length():
    """C5 client tripwire: the typed-input composer exists with the
    protocol's 4000-char cap wired into the input itself."""
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    document = (client_dir / "index.html").read_text(encoding="utf-8")

    assert '<form id="composer">' in document
    assert 'id="composer-input"' in document
    assert 'maxlength="4000"' in document
    assert 'id="composer-submit"' in document


def test_voice_client_install_chip_and_text_send_and_no_session_hint():
    """C5 client tripwire: beforeinstallprompt handling, the typed-turn
    websocket send, and the exact no-session hint string all exist."""
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    script = (client_dir / "app.js").read_text(encoding="utf-8")

    assert 'window.addEventListener("beforeinstallprompt"' in script
    assert "event.preventDefault();" in script
    assert "stashedInstallPrompt" in script
    assert '"appinstalled"' in script
    assert (
        'session.websocket.send(JSON.stringify({ type: "text", text }));'
        in script
    )
    # Typed turns must gate mic PCM (SDK forbids interleaving realtime input
    # with client-content turns) and must be refused during the end-drain.
    assert "muteMicUntilResponse" in script
    assert "activeSession.drainRequested" in script
    assert "Starte zuerst eine Sitzung, dann kannst du auch schreiben." in script


def test_voice_client_native_app_shell_prevents_horizontal_overflow():
    """The mobile shell keeps every control in a bounded grid instead of
    wrapping all header actions into a row wider than an Android WebView."""
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    document = (client_dir / "index.html").read_text(encoding="utf-8")

    assert "overflow-x: clip;" in document
    assert "min-width: 0;" in document
    assert 'class="app-header"' in document
    assert 'class="sharing-actions"' in document
    assert 'id="voice-trigger"' in document
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in document
    assert "#mode-badge[hidden]" in document
    assert 'placeholder="Nachricht an Hermes …"' in document
    assert "Sitzung starten" in document

    script = (client_dir / "app.js").read_text(encoding="utf-8")
    assert 'voiceTriggerElement?.addEventListener("click"' in script


def test_voice_app_icon_uses_native_shell_palette():
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    icon = (client_dir / "icon.svg").read_text(encoding="utf-8")

    assert "#0d100f" in icon
    assert "#7bd0c0" in icon
    assert "#dda05f" in icon
    assert 'id="ring"' in icon


def test_voice_client_video_sharing_capture_pipeline_tripwires():
    """"Sehen" client tripwire: camera/screen capture and the wire format
    sent over the voice websocket. Checks source-level, like the other
    client tripwires in this file — no jsdom/browser harness exists for the
    capture pipeline (canvas/MediaStream aren't available under Node)."""
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    script = (client_dir / "app.js").read_text(encoding="utf-8")

    # Feature detection: getDisplayMedia is missing on many Android-Chrome
    # versions, so the "Bildschirm" chip must be disabled, not left to throw.
    assert "typeof navigator.mediaDevices?.getDisplayMedia" in script
    assert "screenChipElement.disabled = true" in script
    assert "screenShareHintElement.hidden = false" in script
    document = (client_dir / "index.html").read_text(encoding="utf-8")
    assert 'id="screen-share-hint" class="share-hint" role="status" hidden' in document
    assert "Chrome auf Android noch nicht unterstützt" in document
    # Camera capture uses the rear/environment-facing camera by default.
    assert 'facingMode: "environment"' in script
    assert "navigator.mediaDevices.getUserMedia({" in script
    assert "navigator.mediaDevices.getDisplayMedia({ video: true })" in script

    # Downscale bound: longest edge stays within the server's decode limits.
    assert "longestEdge > 1024" in script
    # JPEG encode at the agreed quality.
    assert "canvas.toBlob(" in script
    assert '"image/jpeg", 0.7' in script

    # 1 fps capture cadence.
    assert "window.setInterval(" in script
    assert "}, 1000);" in script

    # The capture tick must skip (send nothing) while the WS isn't open, the
    # session is in fallback mode, or a typed turn's mic-gate is active —
    # the same guard `handleMicFrame` uses for PCM.
    assert "function canSendVideoFrame(session)" in script
    assert "hasOpenWebSocket(session)" in script
    assert 'session.mode !== "fallback"' in script
    assert "!session.muteMicUntilResponse" in script
    assert "canSendVideoFrame(activeSession)" in script

    # Wire format matches the server's `video_frame` control-frame contract.
    assert 'type: "video_frame"' in script
    assert "source: sharingSource" in script
    assert 'type: "sharing_stopped"' in script


def test_voice_client_video_sharing_autostop_and_indicator_tripwires():
    """"Sehen" client tripwire: auto-stop paths and the visible sharing
    state (red pulsing indicator + mini live preview) in the markup."""
    client_dir = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
    script = (client_dir / "app.js").read_text(encoding="utf-8")
    document = (client_dir / "index.html").read_text(encoding="utf-8")

    assert "function stopSharing()" in script

    # pagehide must stop sharing (not just the mic session).
    assert (
        'window.addEventListener("pagehide", () => {\n  stopSharing();' in script
    )

    # The browser's native "Stop sharing" UI (or a yanked camera) fires
    # "ended" on the track, not a client-driven event.
    assert 'track.addEventListener("ended", () => {' in script

    # The server's fallback advisory must stop sharing, not just toast it.
    assert '"video_unavailable_fallback"' in script
    assert (
        'message.error.code === "video_unavailable_fallback"' in script
    )

    # Review fixes 2026-07-10: a superseded in-flight start must stop its
    # late stream (double-click camera-leak blocker), and a live->fallback
    # mode event must stop sharing directly (the capture tick's own fallback
    # gate prevents the server advisory from ever firing).
    assert "sharingStartGeneration" in script
    assert "generation !== sharingStartGeneration" in script
    assert 'message.value === "fallback"' in script

    # Visible sharing state: red pulsing "teilt" indicator + mini preview,
    # both hidden by default and toggled by stopSharing()/startSharing().
    assert 'id="sharing-indicator" class="sharing-indicator" hidden' in document
    assert 'class="sharing-dot"' in document
    assert ">teilt<" in document
    assert 'id="sharing-preview"' in document
    assert 'class="sharing-preview"' in document
    assert "sharingIndicatorElement.hidden = true" in script
    assert "sharingPreviewElement.hidden = true" in script


# =============================================================================
# Voice Sparmodus (cascade) — additive, own websocket route
# =============================================================================


def test_spar_web_config_defaults():
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({})
    assert cfg.enabled is True
    assert cfg.llm_lane == "codex"
    assert cfg.llm_model is None
    assert cfg.whisper_model == "small"
    assert cfg.piper_voice_path.endswith("de_DE-thorsten-medium.onnx")
    assert cfg.max_tool_hops == 2
    assert cfg.llm_timeout_seconds == 25.0


def test_spar_web_config_enabled_only_false_disables():
    from hermes_cli.voice_ws import spar_web_config

    assert spar_web_config({"voice_web": {"spar": {"enabled": False}}}).enabled is False
    # Anything other than the literal False keeps it enabled (fail-open — a
    # malformed 'enabled' must not silently kill the $0 lane).
    assert spar_web_config({"voice_web": {"spar": {"enabled": "false"}}}).enabled is True


@pytest.mark.parametrize("value", ["openrouter", "gemini", "", 1, None])
def test_spar_web_config_invalid_llm_lane_falls_back_to_codex(value):
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"llm_lane": value}}})
    assert cfg.llm_lane == "codex"


def test_spar_web_config_accepts_claude_lane():
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"llm_lane": "claude"}}})
    assert cfg.llm_lane == "claude"


def test_spar_web_config_claude_lane_defaults_model_to_haiku():
    from hermes_cli.voice_ws import spar_web_config

    # The fastest subscription model, so the persistent-child claude-lane
    # stays inside the walkie-talkie latency budget without an explicit
    # voice_web.spar.llm_model override.
    cfg = spar_web_config({"voice_web": {"spar": {"llm_lane": "claude"}}})
    assert cfg.llm_model == "haiku"


def test_spar_web_config_claude_lane_llm_model_override_wins():
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config(
        {"voice_web": {"spar": {"llm_lane": "claude", "llm_model": "sonnet"}}}
    )
    assert cfg.llm_model == "sonnet"


@pytest.mark.parametrize("value", ["", "   ", 42, False, []])
def test_spar_web_config_invalid_whisper_model_falls_back(value):
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"whisper_model": value}}})
    assert cfg.whisper_model == "small"


def test_spar_web_config_accepts_whisper_model_override():
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"whisper_model": "tiny"}}})
    assert cfg.whisper_model == "tiny"


def test_spar_web_config_accepts_piper_voice_path_override(tmp_path):
    from hermes_cli.voice_ws import spar_web_config

    override = str(tmp_path / "custom-voice.onnx")
    cfg = spar_web_config({"voice_web": {"spar": {"piper_voice_path": override}}})
    assert cfg.piper_voice_path == override


@pytest.mark.parametrize("value", [-1, "two", None, 3.5])
def test_spar_web_config_invalid_max_tool_hops_falls_back(value):
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"max_tool_hops": value}}})
    assert cfg.max_tool_hops == 2


def test_spar_web_config_accepts_max_tool_hops_zero():
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"max_tool_hops": 0}}})
    assert cfg.max_tool_hops == 0


@pytest.mark.parametrize("value", [0, -5, "slow", None, float("nan")])
def test_spar_web_config_invalid_llm_timeout_falls_back(value):
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config({"voice_web": {"spar": {"llm_timeout_seconds": value}}})
    assert cfg.llm_timeout_seconds == 25.0


def test_spar_web_config_accepts_llm_model_and_system_instruction_overrides():
    from hermes_cli.voice_ws import spar_web_config

    cfg = spar_web_config(
        {
            "voice_web": {
                "spar": {
                    "llm_model": "gpt-5.4-mini",
                    "system_instruction": "Custom persona.",
                }
            }
        }
    )
    assert cfg.llm_model == "gpt-5.4-mini"
    assert cfg.system_instruction == "Custom persona."


def _fake_spar_synthesize_to_wav(text, *, voice_path, output_path):
    # A minimal-but-real PCM16 WAV — exercises the real ffmpeg transcode
    # path in voice_ws (_transcode_to_pcm24k) instead of faking that too.
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x10\x00" * 2205)  # 0.1s of tone-ish PCM


def test_spar_turn_runs_stt_llm_tool_tts_over_one_websocket(monkeypatch):
    from hermes_cli import voice_ws

    fixture = FIXTURE.read_bytes()
    assert len(fixture) > 20_000

    tool_calls = []

    def fake_transcribe_wav(wav_path, *, model_size, language):
        assert model_size == "small"
        assert language == "de-DE"
        with wave.open(wav_path, "rb") as wav_file:
            assert wav_file.getframerate() == 16_000
            assert wav_file.getnchannels() == 1
        return "starte den terminal befehl"

    llm_replies = iter(
        [
            'TOOL: send_to_terminal {"session": "work", "command": "ls"}',
            "Erledigt, ich habe ls ausgeführt.",
        ]
    )

    async def fake_call_llm_lane(lane, prompt, *, model, timeout, cwd=None):
        assert lane == "codex"
        return next(llm_replies)

    async def fake_tool_execute(name, args):
        tool_calls.append((name, args))
        return {"ok": True}

    monkeypatch.setattr(voice_ws, "spar_transcribe_wav", fake_transcribe_wav)
    monkeypatch.setattr(voice_ws, "spar_synthesize_to_wav", _fake_spar_synthesize_to_wav)
    monkeypatch.setattr(
        "hermes_cli.voice_spar_session.call_llm_lane", fake_call_llm_lane
    )
    monkeypatch.setattr(
        voice_ws.VoiceToolExecutor, "execute", lambda self, name, args: fake_tool_execute(name, args)
    )

    app = _voice_app(extra_voice_web={"spar": {}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        ws.send_bytes(fixture)
        ws.send_json({"type": "turn_end"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "starte den terminal befehl",
        }
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "Erledigt, ich habe ls ausgeführt.",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        audio = ws.receive_bytes()
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        usage = ws.receive_json()
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    assert audio
    assert usage == {
        "type": "usage_update",
        "mode": "spar",
        "estimated_usd": 0,
        "label": "$0 (Abo)",
        "complete": True,
    }
    assert tool_calls == [("send_to_terminal", {"session": "work", "command": "ls"})]


def test_spar_turn_without_tool_call_speaks_direct_reply(monkeypatch):
    from hermes_cli import voice_ws

    fixture = FIXTURE.read_bytes()

    def fake_transcribe_wav(wav_path, *, model_size, language):
        return "wie spät ist es"

    async def fake_call_llm_lane(lane, prompt, *, model, timeout, cwd=None):
        return "Ich habe keine Uhr, aber frag gern nochmal."

    monkeypatch.setattr(voice_ws, "spar_transcribe_wav", fake_transcribe_wav)
    monkeypatch.setattr(voice_ws, "spar_synthesize_to_wav", _fake_spar_synthesize_to_wav)
    monkeypatch.setattr(
        "hermes_cli.voice_spar_session.call_llm_lane", fake_call_llm_lane
    )

    app = _voice_app(extra_voice_web={"spar": {}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        ws.send_bytes(fixture)
        ws.send_json({"type": "turn_end"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "wie spät ist es",
        }
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "Ich habe keine Uhr, aber frag gern nochmal.",
        }
        assert ws.receive_json() == {"type": "state", "value": "speaking"}
        assert ws.receive_bytes()
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        assert ws.receive_json()["type"] == "usage_update"
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"


def test_spar_no_audio_reports_structured_error(monkeypatch):
    from hermes_cli import voice_ws

    called = False

    def unexpected_transcribe(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not transcribe an empty turn")

    monkeypatch.setattr(voice_ws, "spar_transcribe_wav", unexpected_transcribe)

    app = _voice_app(extra_voice_web={"spar": {}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        ws.send_json({"type": "turn_end"})
        assert ws.receive_json() == {
            "type": "error",
            "error": {"code": "no_audio", "message": "Es wurde kein Audio empfangen."},
        }
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"
    assert called is False


def test_spar_disabled_closes_connection():
    app = _voice_app(extra_voice_web={"spar": {"enabled": False}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        event = ws.receive()
        assert event["type"] == "websocket.close"
        assert event["code"] == 4404


def test_spar_odd_sized_pcm_frame_returns_structured_error():
    app = _voice_app(extra_voice_web={"spar": {}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        ws.send_bytes(b"\x00")
        assert ws.receive_json() == {
            "type": "error",
            "error": {
                "code": "invalid_pcm_frame",
                "message": "PCM16-Frames müssen eine gerade Bytezahl haben.",
            },
        }
        assert ws.receive()["type"] == "websocket.close"


def test_spar_video_frame_is_cached_for_look_closely(monkeypatch):
    """A video_frame control message is cached (not relayed) — look_closely
    consumes it via the same VideoFrameCache the Live bridge uses."""
    from hermes_cli import voice_ws

    frame_bytes = FIXTURE_VIDEO.read_bytes()
    stored: list[bytes] = []
    real_store = voice_ws.VideoFrameCache.store

    def spy_store(self, frame):
        stored.append(frame)
        return real_store(self, frame)

    monkeypatch.setattr(voice_ws.VideoFrameCache, "store", spy_store)

    app = _voice_app(extra_voice_web={"spar": {}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        ws.send_json(
            {
                "type": "video_frame",
                "source": "camera",
                "data": base64.b64encode(frame_bytes).decode("ascii"),
            }
        )
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"

    assert stored == [frame_bytes]


def test_spar_llm_lane_failure_reports_structured_error(monkeypatch):
    from hermes_cli import voice_ws
    from hermes_cli.voice_spar_session import LlmLaneError

    fixture = FIXTURE.read_bytes()

    def fake_transcribe_wav(wav_path, *, model_size, language):
        return "hallo"

    async def failing_call_llm_lane(lane, prompt, *, model, timeout, cwd=None):
        raise LlmLaneError("Codex-Lane-Fehler (exit 1): boom")

    monkeypatch.setattr(voice_ws, "spar_transcribe_wav", fake_transcribe_wav)
    monkeypatch.setattr(
        "hermes_cli.voice_spar_session.call_llm_lane", failing_call_llm_lane
    )

    app = _voice_app(extra_voice_web={"spar": {}})
    with TestClient(app).websocket_connect("/api/voice/spar") as ws:
        ws.send_bytes(fixture)
        ws.send_json({"type": "turn_end"})
        assert ws.receive_json() == {"type": "state", "value": "thinking"}
        assert ws.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "hallo",
        }
        error_event = ws.receive_json()
        assert error_event["type"] == "error"
        assert error_event["error"]["code"] == "llm_lane_failed"
        ws.send_json({"type": "end"})
        assert ws.receive()["type"] == "websocket.close"


# =============================================================================
# Proactive memory injection (voice_web.memory_preload)
# =============================================================================

_MEMSEARCH_FIXTURE_1 = FIXTURE.parent / "memsearch_daily_sample_1.md"
_MEMSEARCH_FIXTURE_2 = FIXTURE.parent / "memsearch_daily_sample_2.md"


def test_voice_web_config_memory_preload_defaults_true():
    from hermes_cli.voice_ws import voice_web_config

    assert voice_web_config({}).memory_preload is True


def test_voice_web_config_memory_preload_false_disables():
    from hermes_cli.voice_ws import voice_web_config

    cfg = voice_web_config({"voice_web": {"memory_preload": False}})
    assert cfg.memory_preload is False
    # Fail-open like the other boolean flags in this module: only the
    # literal False disables it.
    cfg2 = voice_web_config({"voice_web": {"memory_preload": "false"}})
    assert cfg2.memory_preload is True


def _seed_real_memsearch_daily_notes(memory_dir):
    """Copy 2 REAL harvested daily memsearch notes (not synthetic fixtures).

    These are verbatim tails of Piet's actual shared/memory/*.md files
    (2026-07-10/11), captured once as test fixtures — proves the excerpt
    logic against the real Markdown shape (## Session/### HH:MM headers,
    HTML transcript comments, "- " bullets), not a hand-written stand-in.
    """
    (memory_dir / "2026-07-10.md").write_text(
        _MEMSEARCH_FIXTURE_1.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (memory_dir / "2026-07-11.md").write_text(
        _MEMSEARCH_FIXTURE_2.read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_voice_memory_context_block_uses_real_daily_note_format(monkeypatch, tmp_path):
    from hermes_cli import voice_ws

    _seed_real_memsearch_daily_notes(tmp_path)
    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)

    block = voice_ws._voice_memory_context_block()

    assert block.startswith(voice_ws._MEMORY_CONTEXT_HEADER)
    # Both real daily excerpts are present, oldest first.
    day1_text = _MEMSEARCH_FIXTURE_1.read_text(encoding="utf-8").strip()
    day2_text = _MEMSEARCH_FIXTURE_2.read_text(encoding="utf-8").strip()
    assert day1_text[-80:] in block
    assert day2_text[-80:] in block
    assert block.index(day1_text[-40:]) < block.index(day2_text[-40:])
    # Never starts mid-sentence: snapped to the next newline.
    for chunk in block.split("\n\n")[1:]:
        assert not chunk.startswith(" ")


def test_voice_memory_context_block_only_last_two_days(monkeypatch, tmp_path):
    from hermes_cli import voice_ws

    (tmp_path / "2026-07-01.md").write_text("Zu alt, darf nicht auftauchen.", encoding="utf-8")
    _seed_real_memsearch_daily_notes(tmp_path)
    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)

    block = voice_ws._voice_memory_context_block()

    assert "Zu alt" not in block


def test_voice_memory_context_block_missing_dir_returns_empty(monkeypatch, tmp_path):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path / "does-not-exist")

    assert voice_ws._voice_memory_context_block() == ""


def test_voice_memory_context_block_empty_dir_returns_empty(monkeypatch, tmp_path):
    from hermes_cli import voice_ws

    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)

    assert voice_ws._voice_memory_context_block() == ""


def test_voice_memory_context_block_unreadable_file_is_skipped_not_raised(
    monkeypatch, tmp_path
):
    from hermes_cli import voice_ws

    bad = tmp_path / "2026-07-11.md"
    bad.write_text("some content", encoding="utf-8")
    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)

    real_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == bad:
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    assert voice_ws._voice_memory_context_block() == ""


@pytest.mark.asyncio
async def test_run_live_bridge_passes_memory_context_suffix(monkeypatch, tmp_path):
    from hermes_cli import voice_ws

    _seed_real_memsearch_daily_notes(tmp_path)
    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)
    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")

    captured_kwargs = {}

    class CapturingGeminiLiveSession:
        def __init__(self, model, language, tools, api_key, **kwargs):
            captured_kwargs.update(kwargs)

        async def run(self, *args, **kwargs):
            raise voice_ws.LiveFallbackRequired("done")

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)

    config = voice_ws.VoiceWebConfig(enabled=True, memory_preload=True)
    await voice_ws._run_live_bridge(
        config,
        "server-key",
        asyncio.Queue(),
        asyncio.Queue(),
        asyncio.Event(),
        asyncio.Event(),
        None,
    )

    assert captured_kwargs["context_suffix"].startswith(voice_ws._MEMORY_CONTEXT_HEADER)


@pytest.mark.asyncio
async def test_run_live_bridge_memory_preload_false_passes_empty_suffix(
    monkeypatch, tmp_path
):
    from hermes_cli import voice_ws

    _seed_real_memsearch_daily_notes(tmp_path)
    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)
    monkeypatch.setattr(voice_ws, "resolve_gemini_api_key", lambda: "server-key")

    captured_kwargs = {}

    class CapturingGeminiLiveSession:
        def __init__(self, model, language, tools, api_key, **kwargs):
            captured_kwargs.update(kwargs)

        async def run(self, *args, **kwargs):
            raise voice_ws.LiveFallbackRequired("done")

    monkeypatch.setattr(voice_ws, "GeminiLiveSession", CapturingGeminiLiveSession)

    config = voice_ws.VoiceWebConfig(enabled=True, memory_preload=False)
    await voice_ws._run_live_bridge(
        config,
        "server-key",
        asyncio.Queue(),
        asyncio.Queue(),
        asyncio.Event(),
        asyncio.Event(),
        None,
    )

    assert captured_kwargs["context_suffix"] == ""


def test_spar_effective_system_instruction_appends_memory_suffix(monkeypatch, tmp_path):
    from hermes_cli import voice_ws
    from hermes_cli.voice_spar_session import SPAR_SYSTEM_INSTRUCTION

    _seed_real_memsearch_daily_notes(tmp_path)
    monkeypatch.setattr(voice_ws, "_MEMORY_NOTES_DIR", tmp_path)

    spar_config = voice_ws.SparWebConfig(system_instruction=SPAR_SYSTEM_INSTRUCTION)
    voice_config = voice_ws.VoiceWebConfig(memory_preload=True)

    effective = voice_ws.spar_effective_system_instruction(spar_config, voice_config)

    assert effective.startswith(SPAR_SYSTEM_INSTRUCTION)
    assert voice_ws._MEMORY_CONTEXT_HEADER in effective


def test_spar_effective_system_instruction_memory_preload_false_is_unchanged():
    from hermes_cli import voice_ws
    from hermes_cli.voice_spar_session import SPAR_SYSTEM_INSTRUCTION

    spar_config = voice_ws.SparWebConfig(system_instruction=SPAR_SYSTEM_INSTRUCTION)
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)

    effective = voice_ws.spar_effective_system_instruction(spar_config, voice_config)

    assert effective == SPAR_SYSTEM_INSTRUCTION


# =============================================================================
# Spar-Warmup: turn-1-latency prespawn pool (/api/voice/spar/warmup)
# =============================================================================

FAKE_CLAUDE_STREAM_CLI = Path(__file__).parent / "fixtures" / "fake_claude_stream_cli.py"


@pytest_asyncio.fixture
async def spar_lane_pool_cleanup():
    """Guarantee no leaked lane child survives a failed assertion mid-test.

    Async (not a plain ``asyncio.run()`` in a sync fixture): the pooled
    lane's subprocess transport is bound to whichever event loop spawned it,
    so tearing it down must happen on that SAME loop — a fresh
    ``asyncio.run()`` loop can't touch it (a real, empirically hit failure
    mode here: "Future attached to a different loop"). Used only by the
    plain async-unit tests below, which run their whole body (including
    fixture teardown) on pytest-asyncio's one test loop; the TestClient-based
    tests instead rely on the router's own lifespan shutdown hook (see
    ``_voice_router_lifespan``), which runs on the portal loop that actually
    spawned the child.
    """
    yield
    from hermes_cli import voice_ws

    if voice_ws._SPAR_LANE_POOL is not None:
        await voice_ws._discard_spar_lane_pool()


@pytest.mark.asyncio
async def test_prespawn_spar_claude_lane_warms_pool(monkeypatch, spar_lane_pool_cleanup):
    from hermes_cli import voice_ws
    from hermes_cli.voice_spar_session import PersistentClaudeLane

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    spar_config = voice_ws.SparWebConfig(llm_lane="claude", llm_timeout_seconds=5.0)
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)

    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)

    assert voice_ws._SPAR_LANE_POOL is not None
    assert isinstance(voice_ws._SPAR_LANE_POOL.lane, PersistentClaudeLane)
    assert voice_ws._SPAR_LANE_POOL.lane._process is not None
    assert voice_ws._SPAR_LANE_POOL.lane._process.returncode is None


@pytest.mark.asyncio
async def test_prespawn_spar_claude_lane_noop_for_codex_lane(spar_lane_pool_cleanup):
    from hermes_cli import voice_ws

    spar_config = voice_ws.SparWebConfig(llm_lane="codex")
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)

    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)

    assert voice_ws._SPAR_LANE_POOL is None


@pytest.mark.asyncio
async def test_prespawn_spar_claude_lane_noop_when_spar_disabled(spar_lane_pool_cleanup):
    from hermes_cli import voice_ws

    spar_config = voice_ws.SparWebConfig(enabled=False, llm_lane="claude")
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)

    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)

    assert voice_ws._SPAR_LANE_POOL is None


@pytest.mark.asyncio
async def test_prespawn_spar_claude_lane_double_call_spawns_one_child(
    monkeypatch, spar_lane_pool_cleanup
):
    from hermes_cli import voice_ws

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    spar_config = voice_ws.SparWebConfig(llm_lane="claude", llm_timeout_seconds=5.0)
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)

    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)
    first_pid = voice_ws._SPAR_LANE_POOL.lane._process.pid

    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)
    second_pid = voice_ws._SPAR_LANE_POOL.lane._process.pid

    assert first_pid == second_pid  # no second child spawned


@pytest.mark.asyncio
async def test_take_pooled_spar_lane_consumes_and_clears_pool(
    monkeypatch, spar_lane_pool_cleanup
):
    from hermes_cli import voice_ws
    from hermes_cli.voice_spar_session import PersistentClaudeLane

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    spar_config = voice_ws.SparWebConfig(llm_lane="claude", llm_timeout_seconds=5.0)
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)
    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)

    lane = await voice_ws._take_pooled_spar_lane(spar_config, voice_config)

    assert isinstance(lane, PersistentClaudeLane)
    assert voice_ws._SPAR_LANE_POOL is None
    try:
        reply = await lane.turn("hallo", history=[])
        assert reply == "HALLO"
    finally:
        await lane.aclose()


@pytest.mark.asyncio
async def test_take_pooled_spar_lane_none_when_pool_empty():
    from hermes_cli import voice_ws

    spar_config = voice_ws.SparWebConfig(llm_lane="claude")
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)

    assert await voice_ws._take_pooled_spar_lane(spar_config, voice_config) is None


@pytest.mark.asyncio
async def test_take_pooled_spar_lane_expired_entry_discarded(
    monkeypatch, spar_lane_pool_cleanup
):
    from hermes_cli import voice_ws

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    spar_config = voice_ws.SparWebConfig(llm_lane="claude", llm_timeout_seconds=5.0)
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)
    await voice_ws.prespawn_spar_claude_lane(spar_config, voice_config)
    stale_process = voice_ws._SPAR_LANE_POOL.lane._process
    voice_ws._SPAR_LANE_POOL.created_at -= voice_ws._SPAR_LANE_POOL_TTL_SECONDS + 1

    lane = await voice_ws._take_pooled_spar_lane(spar_config, voice_config)

    assert lane is None
    assert voice_ws._SPAR_LANE_POOL is None
    await asyncio.wait_for(stale_process.wait(), timeout=5.0)
    assert stale_process.returncode is not None  # the expired child was terminated


@pytest.mark.asyncio
async def test_take_pooled_spar_lane_model_mismatch_discarded(
    monkeypatch, spar_lane_pool_cleanup
):
    from hermes_cli import voice_ws

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    warm_config = voice_ws.SparWebConfig(
        llm_lane="claude", llm_model="haiku", llm_timeout_seconds=5.0
    )
    voice_config = voice_ws.VoiceWebConfig(memory_preload=False)
    await voice_ws.prespawn_spar_claude_lane(warm_config, voice_config)

    mismatched_config = voice_ws.SparWebConfig(
        llm_lane="claude", llm_model="sonnet", llm_timeout_seconds=5.0
    )
    lane = await voice_ws._take_pooled_spar_lane(mismatched_config, voice_config)

    assert lane is None
    assert voice_ws._SPAR_LANE_POOL is None


def test_voice_spar_warmup_route_prespawns_claude_lane(monkeypatch):
    from hermes_cli import voice_ws

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    app = _voice_app(
        extra_voice_web={
            "memory_preload": False,
            "spar": {"llm_lane": "claude", "llm_timeout_seconds": 5.0},
        }
    )
    # `with TestClient(app) as client:` keeps the whole call on ONE portal/
    # event loop (like a real deployed server's single persistent loop) and
    # runs the router's own lifespan shutdown on exit (see
    # ``_voice_router_lifespan``), which discards any pooled lane cleanly —
    # no separate teardown fixture needed here.
    with TestClient(app) as client:
        response = client.post("/api/voice/spar/warmup")
        assert response.status_code == 200
        assert response.json() == {"warmed": True}
        assert voice_ws._SPAR_LANE_POOL is not None
    assert voice_ws._SPAR_LANE_POOL is None


def test_voice_spar_warmup_route_noop_for_codex_lane():
    from hermes_cli import voice_ws

    app = _voice_app(extra_voice_web={"spar": {"llm_lane": "codex"}})
    response = TestClient(app).post("/api/voice/spar/warmup")

    assert response.status_code == 200
    assert response.json() == {"warmed": False}
    assert voice_ws._SPAR_LANE_POOL is None


def test_voice_spar_websocket_reuses_pooled_lane_without_recreating(monkeypatch):
    """The websocket handler consumes the warm pool entry instead of spawning fresh."""
    from hermes_cli import voice_ws

    monkeypatch.setenv("HERMES_CLAUDE_BIN", str(FAKE_CLAUDE_STREAM_CLI))
    monkeypatch.setattr(voice_ws, "spar_transcribe_wav", lambda *a, **k: "hallo")
    monkeypatch.setattr(voice_ws, "spar_synthesize_to_wav", _fake_spar_synthesize_to_wav)

    app = _voice_app(
        extra_voice_web={
            "memory_preload": False,
            "spar": {"llm_lane": "claude", "llm_timeout_seconds": 5.0},
        }
    )
    # A real deployed server runs its whole lifetime on ONE event loop; a
    # bare `client.post(...)` outside `with TestClient(app) as client:` opens
    # and tears down its own portal/loop per call, which would spawn the
    # pooled lane's subprocess on a loop the later websocket_connect() call
    # (a separate portal) can't touch. Sharing one `with` block mirrors the
    # real single-loop deployment.
    with TestClient(app) as client:
        warm = client.post("/api/voice/spar/warmup")
        assert warm.json() == {"warmed": True}
        pooled_pid = voice_ws._SPAR_LANE_POOL.lane._process.pid

        def _fail_create_llm_lane(*_args, **_kwargs):
            raise AssertionError(
                "a warm pooled lane should have been reused instead of spawning fresh"
            )

        monkeypatch.setattr(voice_ws, "spar_create_llm_lane", _fail_create_llm_lane)

        fixture = FIXTURE.read_bytes()
        with client.websocket_connect("/api/voice/spar") as ws:
            ws.send_bytes(fixture)
            ws.send_json({"type": "turn_end"})
            assert ws.receive_json() == {"type": "state", "value": "thinking"}
            transcript = ws.receive_json()
            assert transcript == {"type": "transcript", "role": "user", "text": "hallo"}
            reply = ws.receive_json()
            assert reply["type"] == "transcript" and reply["role"] == "assistant"
            assert reply["text"].upper() == reply["text"]  # fake CLI echoes uppercased
            assert ws.receive_json() == {"type": "state", "value": "speaking"}
            assert ws.receive_bytes()
            assert ws.receive_json() == {"type": "state", "value": "listening"}
            ws.receive_json()  # usage_update
            ws.send_json({"type": "end"})
            assert ws.receive()["type"] == "websocket.close"

    assert voice_ws._SPAR_LANE_POOL is None  # consumed, not re-pooled
    assert pooled_pid  # sanity: a real child pid was captured before consumption

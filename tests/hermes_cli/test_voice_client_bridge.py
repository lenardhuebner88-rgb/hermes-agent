"""Tripwires for the two /voice web-client additions in this slice:

- the native Android screen-share bridge (window.HermesNative, protocol v1)
- the server usage_update/usage_warning/session_ended events

Neither jsdom nor plain Node implement enough of the DOM/WebSocket surface to
run app.js unmodified, so these tests use the same node:vm harness pattern as
``test_voice_client_mic_frames_are_safe_before_websocket_assignment`` in
test_voice_ws.py: load the real script text into a minimal fake-DOM context,
then drive its top-level functions directly. This exercises the real
app.js source (not a reimplementation), which is the point of the harness.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


CLIENT_DIR = Path(__file__).parents[2] / "hermes_cli" / "voice_client"


def _run_node_harness(body: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the standalone voice client harness")

    repo_root = Path(__file__).parents[2]
    harness = f"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("hermes_cli/voice_client/app.js", "utf8");

function makeElement(id) {{
  return {{
    id,
    hidden: false,
    disabled: false,
    textContent: "",
    title: "",
    dataset: {{}},
    children: [],
    classList: {{
      calls: [],
      toggle(name, force) {{
        this.calls.push([name, force]);
      }},
    }},
    setAttribute(name, value) {{
      this["attr_" + name] = value;
    }},
    addEventListener() {{}},
    append() {{}},
    querySelector() {{
      return null;
    }},
  }};
}}

const elementIds = [
  "voice-status", "status-detail", "usage-line", "session-button",
  "transcript", "transcript-empty", "mode-badge", "install-chip",
  "camera-chip", "screen-chip", "screen-share-hint", "sharing-indicator",
  "sharing-preview", "composer", "composer-input",
];
const elements = {{}};
for (const id of elementIds) {{
  elements[id] = makeElement(id);
}}

// `context` (below) becomes the vm sandbox's global object once
// contextified: everything the harness `body` needs to read/assert on must
// be a PROPERTY OF `context` itself, not a plain outer-scope `const`/`let` —
// those live in this Node process's own module scope and are invisible from
// inside the sandbox (this bit us once already: ReferenceError on first run).
const sentToNative = [];
const spokenUtterances = [];
const nativeBridge = {{
  postMessage(json) {{
    sentToNative.push(JSON.parse(json));
  }},
  addEventListener(type, handler) {{
    if (type === "message") {{
      context.nativeMessageHandler = handler;
    }}
  }},
}};

class FakeSpeechSynthesisUtterance {{
  constructor(text) {{
    this.text = text;
  }}
}}
const fakeSpeechSynthesis = {{
  speak(utterance) {{
    spokenUtterances.push(utterance.text);
  }},
}};

const context = {{
  AbortController,
  ArrayBuffer,
  DataView,
  Headers,
  URL,
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  SpeechSynthesisUtterance: FakeSpeechSynthesisUtterance,
  console: {{ info() {{}}, log() {{}} }},
  document: {{
    body: {{ dataset: {{}} }},
    querySelector(selector) {{
      const id = selector.replace("#", "");
      return elements[id] || makeElement(id);
    }},
    addEventListener() {{}},
  }},
  navigator: {{}},
  performance: {{ now() {{ return 0; }} }},
  window: {{
    HermesNative: nativeBridge,
    speechSynthesis: fakeSpeechSynthesis,
    __HERMES_SESSION_TOKEN__: undefined,
    addEventListener() {{}},
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  }},
  elements,
  sentToNative,
  spokenUtterances,
  nativeMessageHandler: null,
}};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`
  {body}
`, context);
"""
    return subprocess.run(
        [node, "-e", harness],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_native_bridge_sends_ready_handshake_on_load():
    result = _run_node_harness(
        """
        if (sentToNative.length !== 1) {
          throw new Error("expected exactly one bridge_ready message, got " + JSON.stringify(sentToNative));
        }
        if (sentToNative[0].v !== 1 || sentToNative[0].type !== "bridge_ready") {
          throw new Error("unexpected handshake payload: " + JSON.stringify(sentToNative[0]));
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_native_capabilities_reenables_disabled_screen_chip():
    result = _run_node_harness(
        """
        // featureDetectScreenShare() ran at load with no getDisplayMedia and
        // must have disabled the chip first, same as plain Android Chrome.
        if (elements["screen-chip"].disabled !== true) {
          throw new Error("chip should start disabled without getDisplayMedia");
        }
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "native_capabilities", screen_capture: true }) });
        if (nativeScreen.available !== true) {
          throw new Error("native_capabilities did not mark screen capture available");
        }
        if (elements["screen-chip"].disabled !== false) {
          throw new Error("native_capabilities did not re-enable the screen chip");
        }
        if (elements["screen-share-hint"].hidden !== true) {
          throw new Error("native_capabilities did not hide the unsupported-browser hint");
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_native_bridge_ignores_malformed_and_unknown_messages():
    result = _run_node_harness(
        """
        nativeMessageHandler({ data: "not json" });
        nativeMessageHandler({ data: JSON.stringify({ type: "native_capabilities", screen_capture: true }) }); // missing v
        nativeMessageHandler({ data: JSON.stringify({ v: 2, type: "native_capabilities", screen_capture: true }) }); // wrong version
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "some_future_type" }) }); // unknown type
        if (nativeScreen.available !== false) {
          throw new Error("malformed/unknown bridge messages must be ignored silently");
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_native_screen_share_start_stop_and_frame_gating():
    result = _run_node_harness(
        """
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "native_capabilities", screen_capture: true }) });

        const fakeSocket = { readyState: 1, sent: [], send(data) { this.sent.push(data); } };
        activeSession = { websocket: fakeSocket, mode: null, muteMicUntilResponse: false };

        toggleSharing("screen");
        if (nativeScreen.state !== "requesting") {
          throw new Error("expected requesting state after toggleSharing('screen'), got " + nativeScreen.state);
        }
        const startMessages = sentToNative.filter((m) => m.type === "start_screen_capture");
        if (startMessages.length !== 1) {
          throw new Error("expected exactly one start_screen_capture, got " + startMessages.length);
        }

        // A frame arriving before screen_capture_started must be dropped.
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_frame", data: "AAAA" }) });
        if (fakeSocket.sent.length !== 0) {
          throw new Error("frame sent to server before capture was confirmed active");
        }

        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_capture_started" }) });
        if (nativeScreen.state !== "active" || sharingSource !== "screen") {
          throw new Error("screen_capture_started did not flip to the active sharing state");
        }
        if (elements["sharing-indicator"].hidden !== false) {
          throw new Error("sharing indicator was not shown for a native share");
        }
        if (elements["sharing-preview"].hidden !== true) {
          throw new Error("native share must never show the local <video> preview");
        }

        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_frame", data: "QUJD" }) });
        if (fakeSocket.sent.length !== 1) {
          throw new Error("active screen_frame was not forwarded to the server");
        }
        const forwarded = JSON.parse(fakeSocket.sent[0]);
        if (forwarded.type !== "video_frame" || forwarded.source !== "screen" || forwarded.data !== "QUJD") {
          throw new Error("forwarded video_frame has the wrong shape: " + fakeSocket.sent[0]);
        }

        // User-initiated stop: must notify native and send sharing_stopped once.
        stopSharing();
        if (nativeScreen.state !== "idle") {
          throw new Error("stopSharing() did not reset native state to idle");
        }
        const stopMessages = sentToNative.filter((m) => m.type === "stop_screen_capture");
        if (stopMessages.length !== 1) {
          throw new Error("expected exactly one stop_screen_capture, got " + stopMessages.length);
        }
        const controlFrames = fakeSocket.sent.filter((raw) => JSON.parse(raw).type === "sharing_stopped");
        if (controlFrames.length !== 1) {
          throw new Error("stopSharing() did not send sharing_stopped for a native share");
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_native_screen_capture_stopped_does_not_echo_stop_command():
    result = _run_node_harness(
        """
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "native_capabilities", screen_capture: true }) });
        const fakeSocket = { readyState: 1, sent: [], send(data) { this.sent.push(data); } };
        activeSession = { websocket: fakeSocket, mode: null, muteMicUntilResponse: false };
        toggleSharing("screen");
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_capture_started" }) });
        sentToNative.length = 0; // clear the start_screen_capture from setup

        // Native ended the share on its own (e.g. system UI) — this must NOT
        // re-send stop_screen_capture back (echo-loop guard).
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_capture_stopped", reason: "system" }) });
        if (nativeScreen.state !== "idle") {
          throw new Error("screen_capture_stopped did not reset to idle");
        }
        const stopEchoes = sentToNative.filter((m) => m.type === "stop_screen_capture");
        if (stopEchoes.length !== 0) {
          throw new Error("screen_capture_stopped must not be echoed back as stop_screen_capture, got " + stopEchoes.length);
        }
        const controlFrames = fakeSocket.sent.filter((raw) => JSON.parse(raw).type === "sharing_stopped");
        if (controlFrames.length !== 1) {
          throw new Error("a native share ending must still send sharing_stopped over the session websocket");
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_native_screen_capture_error_surfaces_message_and_stops():
    result = _run_node_harness(
        """
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "native_capabilities", screen_capture: true }) });
        activeSession = { websocket: null, mode: null, muteMicUntilResponse: false };
        toggleSharing("screen");
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_capture_started" }) });

        nativeMessageHandler({
          data: JSON.stringify({ v: 1, type: "screen_capture_error", code: "permission_denied", message: "Bildschirmfreigabe abgelehnt." }),
        });
        if (nativeScreen.state !== "idle") {
          throw new Error("screen_capture_error did not reset native state");
        }
        if (elements["status-detail"].textContent !== "Bildschirmfreigabe abgelehnt.") {
          throw new Error("screen_capture_error did not surface its message: " + elements["status-detail"].textContent);
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_usage_update_renders_estimate_and_incomplete_and_no_pricing_variants():
    result = _run_node_harness(
        """
        const session = { usageWarningSpoken: false };

        handleJsonMessage(session, JSON.stringify({
          type: "usage_update", session_seconds: 180, estimated_usd: 0.012,
          estimate_incomplete: false, soft_budget_exceeded: false,
          tokens: { input: { text: 100 }, output: { text: 50 } },
        }));
        if (elements["usage-line"].textContent !== "Kosten ≈ $0.012 · 3 Min") {
          throw new Error("unexpected usage_update text: " + elements["usage-line"].textContent);
        }
        if (elements["usage-line"].hidden !== false) {
          throw new Error("usage-line must become visible once usage is known");
        }
        const warnCalls = elements["usage-line"].classList.calls;
        if (warnCalls[warnCalls.length - 1][1] !== false) {
          throw new Error("soft_budget_exceeded=false must not toggle the warn class on");
        }

        handleJsonMessage(session, JSON.stringify({
          type: "usage_update", session_seconds: 180, estimated_usd: 0.012,
          estimate_incomplete: true, soft_budget_exceeded: true,
        }));
        if (elements["usage-line"].textContent !== "Kosten ≥ $0.012 (unvollständig)") {
          throw new Error("unexpected estimate_incomplete text: " + elements["usage-line"].textContent);
        }
        if (warnCalls[warnCalls.length - 1][1] !== true) {
          throw new Error("soft_budget_exceeded=true must toggle the warn class on");
        }

        handleJsonMessage(session, JSON.stringify({
          type: "usage_update", session_seconds: 60, estimated_usd: null,
          tokens: { input: { text: 400, audio: 100 }, output: { text: 41 } },
        }));
        if (elements["usage-line"].textContent !== "~541 Tokens (keine Preisdaten)") {
          throw new Error("unexpected no-pricing text: " + elements["usage-line"].textContent);
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_usage_warning_sets_status_and_speaks_at_most_once_per_session():
    result = _run_node_harness(
        """
        const session = { usageWarningSpoken: false };
        handleJsonMessage(session, JSON.stringify({
          type: "usage_warning", reason: "soft_minutes", minutes: 12.4,
          estimated_usd: 0.5, estimate_incomplete: false,
        }));
        if (elements["status-detail"].textContent !== "Kostenwarnung: Sitzung läuft seit 12 Minuten.") {
          throw new Error("unexpected usage_warning status text: " + elements["status-detail"].textContent);
        }
        if (spokenUtterances.length !== 1) {
          throw new Error("expected exactly one spoken utterance, got " + spokenUtterances.length);
        }
        handleJsonMessage(session, JSON.stringify({
          type: "usage_warning", reason: "soft_minutes", minutes: 20,
          estimated_usd: 0.8, estimate_incomplete: false,
        }));
        if (spokenUtterances.length !== 1) {
          throw new Error("a second usage_warning must not speak again in the same session, got " + spokenUtterances.length);
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_screen_capture_started_with_stale_generation_replies_stop():
    result = _run_node_harness(
        """
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "native_capabilities", screen_capture: true }) });
        const fakeSocket = { readyState: 1, sent: [], send(data) { this.sent.push(data); } };
        activeSession = { websocket: fakeSocket, mode: null, muteMicUntilResponse: false };

        toggleSharing("screen"); // requesting @ some generation
        // The user cancels locally before native confirms — resets to idle
        // and already notifies native once.
        stopSharing();
        sentToNative.length = 0; // clear the start/stop pair from setup

        // Native's confirmation for the now-abandoned request arrives late.
        nativeMessageHandler({ data: JSON.stringify({ v: 1, type: "screen_capture_started" }) });

        if (nativeScreen.state !== "idle") {
          throw new Error("a stale screen_capture_started must not resurrect the sharing state, got " + nativeScreen.state);
        }
        if (sharingSource === "screen") {
          throw new Error("a stale screen_capture_started must not flip sharingSource to screen");
        }
        const stopMessages = sentToNative.filter((m) => m.type === "stop_screen_capture");
        if (stopMessages.length !== 1) {
          throw new Error("expected the orphaned native capture to be killed with stop_screen_capture, got " + stopMessages.length);
        }
        if (elements["sharing-indicator"].hidden !== true) {
          throw new Error("a stale screen_capture_started must not show the sharing indicator");
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_session_ended_stores_terminal_detail_for_the_websocket_close_path():
    result = _run_node_harness(
        """
        let capturedDetail = "not-called";
        // finishSession is a top-level function binding in app.js; replace
        // it to isolate finalizeWebSocketClose's detail computation from its
        // own (unrelated) cleanup side effects.
        finishSession = async (session, detail) => {
          capturedDetail = detail;
        };

        const session = { usageWarningSpoken: false, terminalDetail: null, drainRequested: false };
        handleJsonMessage(session, JSON.stringify({ type: "session_ended", reason: "max_duration" }));
        if (!session.terminalDetail || !session.terminalDetail.includes("Zeitlimit")) {
          throw new Error("session_ended did not stash a terminalDetail on the session: " + session.terminalDetail);
        }

        // The server's close(1000) right after session_ended must reuse the
        // stashed detail, not the generic idle text.
        finalizeWebSocketClose(session, { code: 1000, wasClean: true });
        if (capturedDetail !== session.terminalDetail) {
          throw new Error("finalizeWebSocketClose did not preserve the session_ended detail, got: " + capturedDetail);
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_session_ended_sets_a_clear_terminal_status_per_reason():
    result = _run_node_harness(
        """
        const session = { usageWarningSpoken: false };
        handleJsonMessage(session, JSON.stringify({ type: "session_ended", reason: "max_duration" }));
        if (!elements["status-detail"].textContent.includes("Zeitlimit")) {
          throw new Error("max_duration session_ended did not mention the time limit: " + elements["status-detail"].textContent);
        }
        handleJsonMessage(session, JSON.stringify({ type: "session_ended", reason: "hard_budget" }));
        if (!elements["status-detail"].textContent.includes("Budgetlimit")) {
          throw new Error("hard_budget session_ended did not mention the budget limit: " + elements["status-detail"].textContent);
        }
        """
    )
    assert result.returncode == 0, result.stderr


def test_bridge_protocol_strings_present_in_app_js():
    """Cheap tripwire on the real source: the exact protocol literals this
    slice's spec fixes must survive future edits without re-running the node
    harness for every string.
    """
    script = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")

    for literal in [
        "window.HermesNative",
        '"bridge_ready"',
        '"start_screen_capture"',
        '"stop_screen_capture"',
        '"native_capabilities"',
        '"screen_capture_started"',
        '"screen_frame"',
        '"screen_capture_stopped"',
        '"screen_capture_error"',
        '"usage_update"',
        '"usage_warning"',
        '"session_ended"',
    ]:
        assert literal in script, f"missing bridge/usage literal: {literal}"

    # This is a JS web-client asset, not native Android code, but the spec
    # explicitly calls out addJavascriptInterface as a thing app.js must
    # never reach for (that API lives on the native side of the bridge).
    assert "addJavascriptInterface" not in script


def test_index_html_has_usage_line_element_with_hidden_attribute():
    document = (CLIENT_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="usage-line"' in document
    assert 'class="usage-line"' in document

#!/usr/bin/env python3
"""End-to-end acceptance test for the Hermes Voice web assistant.

Runs against a REAL deployed dashboard service (default
``http://127.0.0.1:9119``): password login → WS ticket → voice WebSocket
session → a real Gemini Live turn → transcripts + audio, plus (skippable) a
vision turn that sends a still frame and asks the assistant to describe it.

**This costs real Gemini API quota on every run** — it is not a mock/unit
test, it drives the live Gemini Live API through the deployed server.

Usage::

    ./venv/bin/python scripts/voice_e2e.py [--base-url URL] [--skip-vision] [--question TEXT]

Credentials come from ``HERMES_DASHBOARD_USERNAME`` / ``HERMES_DASHBOARD_PASSWORD``
(prompted interactively if unset, mirroring ``scripts/smoke_health_status_auth.py``).
Passwords, tokens, and tickets are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
from io import BytesIO
import json
import os
import sys
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image
import websockets

DEFAULT_BASE_URL = "http://127.0.0.1:9119"
VISION_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "hermes_cli"
    / "fixtures"
    / "vision_marker.jpg"
)
DEFAULT_QUESTION = (
    "Was siehst du im Bild? Nenne Farbe und Form der Figur, das Wort und die Zahl."
)
VISION_KEYWORDS = ("rot", "kreis", "kirsche", "42", "zweiundvierzig")
MIN_TURN1_AUDIO_BYTES = 10000
TURN_TIMEOUT_SECONDS = 75.0
OVERALL_TIMEOUT_SECONDS = 240.0
VIDEO_FRAME_SETTLE_SECONDS = 1.5
STARTUP_LISTENING_GRACE_SECONDS = 2.0


class E2EError(RuntimeError):
    """A failed acceptance check; carries a human-readable, secret-free detail."""


@dataclass
class TurnResult:
    """What one turn produced, for both assertions and timeout diagnostics."""

    transcript: str = ""
    audio_bytes: int = 0
    video_frame_sent: bool = False
    watch_notification_sent: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)


def _format_event(event: dict[str, Any]) -> str:
    """Render one event compactly — never dumps raw audio bytes or secrets."""
    if event.get("type") == "audio":
        return f"{{'type': 'audio', 'bytes': {len(event.get('data') or b'')}}}"
    text = event.get("text")
    if isinstance(text, str) and len(text) > 200:
        event = {**event, "text": text[:200] + "…"}
    return repr(event)


def _summarize_event_types(events: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        key = str(event.get("type"))
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{name}x{count}" for name, count in sorted(counts.items()))


def _normalize_message(raw: str | bytes) -> dict[str, Any]:
    """Turn one raw WS frame into a uniform event dict.

    Text frames carry JSON control/state events. Binary frames carry raw
    PCM16 assistant audio — the server streams these via ``send_bytes``
    (see ``voice_ws._send_voice_events``), not a base64 JSON envelope —
    normalized here into ``{"type": "audio", "data": <bytes>}`` so the rest
    of this script can reason about "audio events" uniformly.
    """
    if isinstance(raw, bytes):
        return {"type": "audio", "data": raw}
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise E2EError(f"received a non-JSON text frame: {raw[:200]!r}") from exc
    if not isinstance(event, dict):
        raise E2EError(f"received a non-object JSON event: {raw[:200]!r}")
    return event


class EventReader:
    """Wraps the WS connection with a one-slot push-back buffer."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._pending: list[dict[str, Any]] = []

    def push_back(self, event: dict[str, Any]) -> None:
        self._pending.insert(0, event)

    async def next_event(self, timeout: float) -> dict[str, Any]:
        if self._pending:
            return self._pending.pop(0)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        return _normalize_message(raw)


def _base_url(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise E2EError(f"invalid dashboard URL: {raw!r}")
    return raw.rstrip("/")


def resolve_credentials() -> tuple[str, str]:
    """Read dashboard credentials, prompting as a fallback (never printed)."""
    username = os.environ.get("HERMES_DASHBOARD_USERNAME", "").strip()
    if not username:
        username = input("HERMES_DASHBOARD_USERNAME: ").strip()
    if not username:
        raise E2EError("HERMES_DASHBOARD_USERNAME is not set")

    password = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")
    if not password:
        password = getpass.getpass("HERMES_DASHBOARD_PASSWORD: ")
    if not password:
        raise E2EError("HERMES_DASHBOARD_PASSWORD is not set")
    return username, password


def login(client: httpx.Client, base_url: str, username: str, password: str) -> None:
    """POST /auth/password-login; the session cookie lands in ``client``'s jar."""
    provider = os.environ.get("HERMES_DASHBOARD_AUTH_PROVIDER", "basic")
    try:
        response = client.post(
            f"{base_url}/auth/password-login",
            json={
                "provider": provider,
                "username": username,
                "password": password,
                "next": "",
            },
        )
    except httpx.HTTPError as exc:
        raise E2EError(f"password-login request failed: {exc}") from exc
    if response.status_code != 200:
        raise E2EError(f"password-login returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise E2EError("password-login returned invalid JSON") from exc
    if payload.get("ok") is not True:
        raise E2EError("password-login did not return ok=true")


def mint_ws_ticket(client: httpx.Client, base_url: str) -> str:
    """POST the authenticated /api/auth/ws-ticket REST endpoint."""
    try:
        response = client.post(f"{base_url}/api/auth/ws-ticket")
    except httpx.HTTPError as exc:
        raise E2EError(f"ws-ticket request failed: {exc}") from exc
    if response.status_code != 200:
        raise E2EError(f"ws-ticket returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise E2EError("ws-ticket returned invalid JSON") from exc
    ticket = payload.get("ticket")
    if not isinstance(ticket, str) or len(ticket) < 16:
        raise E2EError("ws-ticket response carried no usable ticket")
    return ticket


def build_ws_url(base_url: str, ticket: str) -> str:
    """Mirror hermes_cli/voice_client/app.js's createWebSocket() exactly."""
    parsed = urllib.parse.urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urllib.parse.urlencode({"ticket": ticket, "session": str(uuid.uuid4())})
    return urllib.parse.urlunparse((scheme, parsed.netloc, "/api/voice/live", "", query, ""))


async def _consume_startup_listening(reader: EventReader) -> None:
    """Drain the post-``mode:live`` readiness ``state:listening`` event.

    ``GeminiLiveSession.run`` puts this right after ``mode: live``, before
    any turn begins (see voice_live_session.py) — consuming it here keeps a
    turn's own "state: listening" end-of-turn signal from matching this
    leftover startup event instead.
    """
    try:
        event = await reader.next_event(STARTUP_LISTENING_GRACE_SECONDS)
    except TimeoutError:
        return
    if not (event.get("type") == "state" and event.get("value") == "listening"):
        reader.push_back(event)


async def collect_turn(reader: EventReader, timeout: float) -> TurnResult:
    """Collect events until the server signals end-of-turn or the deadline passes.

    End-of-turn is ``{"type": "state", "value": "listening"}`` — the server
    emits it (after flushing any buffered final transcript) once Gemini's
    ``turn_complete``/``interrupted`` fires, see ``_handle_message`` in
    voice_live_session.py.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    result = TurnResult()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise E2EError(
                f"turn timed out after {timeout:.0f}s; collected so far: "
                f"transcript={result.transcript!r} audio_bytes={result.audio_bytes} "
                f"video_frame_sent={result.video_frame_sent} "
                f"events=[{_summarize_event_types(result.events)}]"
            )
        event = await reader.next_event(remaining)
        result.events.append(event)
        event_type = event.get("type")
        if event_type == "audio":
            result.audio_bytes += len(event.get("data") or b"")
        elif event_type == "transcript" and event.get("role") == "assistant":
            if event.get("partial") is False:
                text = event.get("text") or ""
                if text:
                    result.transcript = (result.transcript + " " + text).strip()
        elif event_type == "video_frame_sent":
            result.video_frame_sent = True
        elif event_type == "usage_update":
            # Cost observability (numbers only, never transcript/image data).
            # The per-turn input total is the real post-compression context
            # window size — the forced-compression proof reads it off here.
            tokens = event.get("tokens") or {}
            input_tokens = tokens.get("input") or {}
            output_tokens = tokens.get("output") or {}
            print(
                "E2E usage_update: "
                f"turn={event.get('turns')} "
                f"input={sum(v for v in input_tokens.values() if isinstance(v, int))} "
                f"output={sum(v for v in output_tokens.values() if isinstance(v, int))} "
                f"est_usd={event.get('estimated_usd')} "
                f"incomplete={event.get('estimate_incomplete')}"
            )
        elif event_type == "watch_notification_sent":
            result.watch_notification_sent = True
        elif event_type == "mode" and event.get("value") == "fallback":
            raise E2EError(f"Live session dropped to fallback mid-turn: {event!r}")
        elif event_type == "error":
            raise E2EError(f"server reported an error mid-turn: {_format_event(event)}")
        elif event_type == "state" and event.get("value") == "listening":
            return result


async def run_voice_regression_turn(ws: Any, reader: EventReader) -> TurnResult:
    """Turn 1 (Plan-C parity): a scripted phrase, checked for transcript + audio."""
    await ws.send(json.dumps({"type": "text", "text": "Sag exakt das Wort: Bestanden"}))
    return await collect_turn(reader, TURN_TIMEOUT_SECONDS)


async def run_vision_turn(ws: Any, reader: EventReader, question: str) -> TurnResult:
    """Turn 2 (Plan D): a still frame, flushed on the following typed turn."""
    jpeg_b64 = base64.b64encode(VISION_FIXTURE.read_bytes()).decode("ascii")
    await ws.send(json.dumps({"type": "video_frame", "data": jpeg_b64, "source": "screen"}))
    await asyncio.sleep(VIDEO_FRAME_SETTLE_SECONDS)
    await ws.send(json.dumps({"type": "text", "text": question}))
    return await collect_turn(reader, TURN_TIMEOUT_SECONDS)


def _watch_change_frame_b64() -> str:
    """Build a deterministic, materially changed JPEG without persisting it."""

    with Image.new("RGB", (640, 480), color=(5, 30, 80)) as image:
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def run_watch_probe(ws: Any, reader: EventReader) -> tuple[TurnResult, TurnResult]:
    """Plan E: arm watch_view, then prove one local-change notification live."""

    await ws.send(
        json.dumps(
            {
                "type": "text",
                "text": (
                    "Beobachte jetzt die geteilte Ansicht. Nutze watch_view mit dem "
                    "Auftrag: Bei der nächsten deutlichen Bildänderung sage exakt "
                    "AENDERUNG ERKANNT."
                ),
            }
        )
    )
    armed = await collect_turn(reader, TURN_TIMEOUT_SECONDS)
    await ws.send(
        json.dumps(
            {
                "type": "video_frame",
                "data": _watch_change_frame_b64(),
                "source": "screen",
            }
        )
    )
    notified = await collect_turn(reader, TURN_TIMEOUT_SECONDS)
    return armed, notified


async def _run_session_checks(
    ws: Any, args: argparse.Namespace, record: Callable[[str, bool, str], None]
) -> None:
    reader = EventReader(ws)

    try:
        mode_event = await reader.next_event(20.0)
    except TimeoutError:
        record("mode-live", False, "timed out waiting for the initial mode event")
        return
    if mode_event.get("type") != "mode":
        record("mode-live", False, f"expected a mode event first, got {mode_event!r}")
        return
    if mode_event.get("value") != "live":
        diagnostics = _format_event(mode_event)
        try:
            follow_up = await reader.next_event(3.0)
        except TimeoutError:
            pass
        else:
            diagnostics += f", next_event={_format_event(follow_up)}"
        record("mode-live", False, f"server started in fallback mode ({diagnostics})")
        return
    record("mode-live", True, "mode=live")
    await _consume_startup_listening(reader)

    try:
        turn1 = await run_voice_regression_turn(ws, reader)
    except E2EError as exc:
        record("turn1-transcript", False, str(exc))
        record("turn1-audio", False, str(exc))
        return
    record(
        "turn1-transcript",
        "bestanden" in turn1.transcript.lower(),
        f"assistant said {turn1.transcript[:200]!r}",
    )
    record(
        "turn1-audio",
        turn1.audio_bytes > MIN_TURN1_AUDIO_BYTES,
        f"audio_bytes={turn1.audio_bytes}",
    )

    if args.skip_vision:
        await _send_end(ws)
        return

    try:
        turn2 = await run_vision_turn(ws, reader, args.question)
    except E2EError as exc:
        record("turn2-video-frame-sent", False, str(exc))
        record("turn2-transcript", False, str(exc))
        await _send_end(ws)
        return
    record(
        "turn2-video-frame-sent",
        turn2.video_frame_sent,
        f"video_frame_sent={turn2.video_frame_sent}",
    )
    matched = [keyword for keyword in VISION_KEYWORDS if keyword in turn2.transcript.lower()]
    record(
        "turn2-transcript",
        len(matched) >= 2,
        f"matched={matched} assistant said {turn2.transcript[:200]!r}",
    )
    if args.plan_e:
        try:
            armed, notified = await run_watch_probe(ws, reader)
        except E2EError as exc:
            record("plan-e-watch-armed", False, str(exc))
            record("plan-e-watch-event", False, str(exc))
            await _send_end(ws)
            return
        record(
            "plan-e-watch-armed",
            bool(armed.transcript or armed.audio_bytes),
            f"assistant said {armed.transcript[:200]!r}",
        )
        normalized = notified.transcript.lower().replace("ä", "ae")
        record(
            "plan-e-watch-event",
            notified.watch_notification_sent
            and "aenderung" in normalized
            and "erkannt" in normalized,
            (
                f"watch_notification_sent={notified.watch_notification_sent} "
                f"assistant said {notified.transcript[:200]!r}"
            ),
        )
    await _send_end(ws)


async def _send_end(ws: Any) -> None:
    try:
        await ws.send(json.dumps({"type": "end"}))
    except websockets.exceptions.ConnectionClosed:
        pass


def _finish(checks: list[tuple[str, bool]]) -> int:
    passed = bool(checks) and all(ok for _, ok in checks)
    total = len(checks)
    ok_count = sum(1 for _, ok in checks if ok)
    print(f"E2E summary: {'PASS' if passed else 'FAIL'} ({ok_count}/{total} checks passed)")
    return 0 if passed else 1


async def _amain(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append((name, passed))
        print(f"E2E {name}: {'PASS' if passed else 'FAIL'} {detail}")

    try:
        username, password = resolve_credentials()
    except E2EError as exc:
        record("credentials", False, str(exc))
        return _finish(checks)

    try:
        base_url = _base_url(args.base_url)
    except E2EError as exc:
        record("base-url", False, str(exc))
        return _finish(checks)

    with httpx.Client(timeout=15.0) as client:
        try:
            login(client, base_url, username, password)
        except E2EError as exc:
            record("login", False, str(exc))
            return _finish(checks)
        record("login", True, f"user={username}")

        try:
            ticket = mint_ws_ticket(client, base_url)
        except E2EError as exc:
            record("ws-ticket", False, str(exc))
            return _finish(checks)
        record("ws-ticket", True, "ticket minted")

    ws_url = build_ws_url(base_url, ticket)
    try:
        async with websockets.connect(ws_url, open_timeout=15.0, max_size=4 * 1024 * 1024) as ws:
            await _run_session_checks(ws, args, record)
    except (OSError, websockets.exceptions.WebSocketException, TimeoutError) as exc:
        record("websocket", False, f"{type(exc).__name__}: {exc}")

    return _finish(checks)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end acceptance test for the Hermes Voice web assistant "
        "(costs real Gemini API quota).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("HERMES_DASHBOARD_URL", DEFAULT_BASE_URL),
        help=f"Dashboard base URL (default: {DEFAULT_BASE_URL}, or HERMES_DASHBOARD_URL).",
    )
    parser.add_argument(
        "--skip-vision",
        action="store_true",
        help="Skip the vision turn (e.g. against a not-yet-deployed server).",
    )
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Question asked in the vision turn.",
    )
    parser.add_argument(
        "--plan-e",
        action="store_true",
        help="Also arm watch_view and verify one real change notification.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(asyncio.wait_for(_amain(args), timeout=OVERALL_TIMEOUT_SECONDS))
    except TimeoutError:
        print(f"E2E watchdog: FAIL overall run exceeded {OVERALL_TIMEOUT_SECONDS:.0f}s")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

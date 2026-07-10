#!/usr/bin/env python3
"""Fire one scheduled voice reminder: read its payload, post it to Discord.

Invoked by ``systemd-run --user --on-active=<N>min`` from
``tools.voice_live_tools``'s ``schedule_reminder`` tool. Runs standalone, long
after the voice session (and process) that scheduled it is gone, so it takes
the payload path as its only argument instead of any in-process state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_constants import get_hermes_home


def _reminders_dir() -> Path:
    return get_hermes_home() / "cache" / "voice-web" / "reminders"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: voice_reminder_fire.py <payload.json>", file=sys.stderr)
        return 2
    payload_path = Path(argv[0]).resolve()

    try:
        payload_path.relative_to(_reminders_dir().resolve())
    except ValueError:
        print(
            f"refusing payload outside the reminders directory: {payload_path}",
            file=sys.stderr,
        )
        return 2

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read reminder payload {payload_path}: {exc}", file=sys.stderr)
        return 1

    text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
    if not text:
        print(f"reminder payload has no text: {payload_path}", file=sys.stderr)
        return 1

    from tools.send_message_tool import send_message_tool

    message = f"⏰ Erinnerung: {text}"
    raw_result = send_message_tool({"target": "discord", "message": message})
    try:
        result = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError):
        result = None

    if not isinstance(result, dict) or not result.get("success"):
        detail = result.get("error") if isinstance(result, dict) else raw_result
        print(f"reminder delivery failed: {detail}", file=sys.stderr)
        return 1

    payload_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

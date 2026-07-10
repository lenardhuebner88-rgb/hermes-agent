#!/usr/bin/env python3
"""Fake ``claude -p --input-format stream-json --output-format stream-json`` child.

Used by ``test_voice_spar_session.py`` to exercise ``PersistentClaudeLane``
against a real subprocess with the empirically verified stream-json line
format, without spawning the real (slow, billed) claude CLI. One line in ->
one ``result`` event out, matching what a real ``claude -p`` turn emits:
``{"type": "result", "subtype": "success", "is_error": false, "result": "..."}``.

Special inputs (checked against the ``content[0].text`` field) drive test
scenarios:
- text starting with ``CRASH`` -> exit immediately with no output (simulates
  a dead child mid-turn).
- text starting with ``HANG`` -> sleep past any sane test timeout without
  ever emitting a result (simulates a stuck child).
- anything else -> one noise line (ignored by a real reader, same as the
  live CLI's system/assistant events) then a real result line echoing the
  input, uppercased, so tests can assert exactly what was sent.
"""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        envelope = json.loads(raw_line)
        text = envelope["message"]["content"][0]["text"]
        if text.startswith("CRASH"):
            return 1
        if text.startswith("HANG"):
            time.sleep(120)
            continue
        print(json.dumps({"type": "system", "subtype": "hook_started"}), flush=True)
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": text.upper(),
                    "session_id": "fake-session",
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

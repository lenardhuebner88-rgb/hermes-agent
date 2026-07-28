#!/usr/bin/env python3
"""Tiny reusable Discord poster for watchdog scripts.

Reads the bot token from ``$DISCORD_BOT_TOKEN`` or ``~/.hermes/.env`` and
posts a message to a channel. Mentions are always suppressed.

Usage:
    echo "msg" | discord-notify.py --channel <id> --stdin
    discord-notify.py --channel <id> --content "msg"

Exits 0 on success and 1 on failure so callers can decide whether delivery is
fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


ENV_PATH = os.path.expanduser("~/.hermes/.env")


def read_token() -> str | None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        return token.strip()
    try:
        with open(ENV_PATH, encoding="utf-8") as env_file:
            for line in env_file:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def post_discord(channel: str, content: str) -> str | None:
    token = read_token()
    if not token:
        print("[discord-notify] no DISCORD_BOT_TOKEN", file=sys.stderr)
        return None

    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=json.dumps(
            {"content": content[:1990], "allowed_mentions": {"parse": []}}
        ).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://hermes.local, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response).get("id")
    except urllib.error.HTTPError as exc:
        print(
            f"[discord-notify] HTTP {exc.code}: {exc.read().decode()[:200]}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        print(f"[discord-notify] {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--content", default=None)
    parser.add_argument("--stdin", action="store_true", help="read content from stdin")
    args = parser.parse_args()

    if args.stdin:
        content = sys.stdin.read()
    elif args.content is not None:
        content = args.content
    else:
        print("[discord-notify] need --content or --stdin", file=sys.stderr)
        return 1

    content = content.strip()
    if not content:
        print("[discord-notify] empty content — nothing to post", file=sys.stderr)
        return 1

    message_id = post_discord(args.channel, content)
    return 0 if message_id else 1


if __name__ == "__main__":
    sys.exit(main())

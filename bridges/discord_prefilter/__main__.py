"""CLI entrypoint for the Discord pre-filter bridge.

Usage:
    python -m bridges.discord_prefilter                 # run the bot
    python -m bridges.discord_prefilter --triage "..."  # one-shot triage (no Discord)
    python -m bridges.discord_prefilter --escalate "..."# one-shot escalation (no Discord)

Run it with the Hermes venv interpreter so discord.py and hermes_cli resolve, e.g.:
    /home/piet/.hermes/hermes-agent/.venv/bin/python -m bridges.discord_prefilter
"""

from __future__ import annotations

import argparse
import logging
import sys

from bridges.discord_prefilter.config import PrefilterConfig


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discord_prefilter")
    parser.add_argument("--env-file", default=None, help="Path to the bridge env file")
    parser.add_argument("--triage", metavar="TEXT", default=None,
                        help="Run one triage on TEXT and print the decision, then exit")
    parser.add_argument("--escalate", metavar="TEXT", default=None,
                        help="Run one Hermes escalation on TEXT and print the answer, then exit")
    args = parser.parse_args(argv)

    _setup_logging()
    config = PrefilterConfig.load(args.env_file)

    if args.triage is not None:
        from bridges.discord_prefilter.triage import run_triage
        d = run_triage(args.triage, config)
        print(f"bucket={d.bucket.value} source={d.source}")
        if d.reply:
            print(f"reply: {d.reply}")
        return 0

    if args.escalate is not None:
        from bridges.discord_prefilter.escalate import run_hermes_oneshot
        print(run_hermes_oneshot(args.escalate, config))
        return 0

    from bridges.discord_prefilter.bot import run
    run(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

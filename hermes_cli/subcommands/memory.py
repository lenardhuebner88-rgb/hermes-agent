"""``hermes memory`` subcommand parser.

Extracted from ``hermes_cli/main.py:main()`` (god-file Phase 2 follow-up).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_memory_parser(subparsers, *, cmd_memory: Callable) -> None:
    """Attach the ``memory`` subcommand to ``subparsers``."""
    memory_parser = subparsers.add_parser(
        "memory",
        help="Configure external memory provider",
        description=(
            "Set up and manage external memory provider plugins.\n\n"
            "Available providers: honcho, openviking, mem0, hindsight,\n"
            "holographic, retaindb, byterover.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active."
        ),
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration"
    )
    _setup_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker",
    )
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    _digest_parser = memory_sub.add_parser(
        "digest",
        help="Weekly decision extract from completion receipts",
        description=(
            "Receipt-first decision digest: scan completed kanban runs over a\n"
            "window and surface the decisions agents made, open operator\n"
            "follow-ups, and superseded items — each linked to its source task.\n"
            "Reads the canonical completion metadata (decisions[],\n"
            "operator_followup, supersedes[]); nothing is written."
        ),
    )
    _digest_parser.add_argument(
        "--since",
        default="7d",
        help="Window back from now: e.g. 7d, 24h, 2w, 30m (default: 7d)",
    )
    _digest_parser.add_argument(
        "--profile",
        default=None,
        help="Restrict to runs from this profile/lane (e.g. coder); 'all' = no filter",
    )
    _digest_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of decisions shown (newest first)",
    )
    _digest_parser.add_argument(
        "--board",
        default=None,
        help="Kanban board slug to read (default: current board)",
    )
    _digest_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the digest as JSON instead of markdown",
    )
    _reset_parser = memory_sub.add_parser(
        "reset",
        help="Erase all built-in memory (MEMORY.md and USER.md)",
    )
    _reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    _reset_parser.add_argument(
        "--target",
        choices=["all", "memory", "user"],
        default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'",
    )
    memory_parser.set_defaults(func=cmd_memory)

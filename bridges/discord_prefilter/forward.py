"""Build the hand-off message the pre-filter posts into the Orchestrator channel.

Phase-2 escalation mode "orchestrator": instead of running a throwaway agent,
the pre-filter forwards a real task to the live Hub Orchestrator (which owns the
Kanban board) by posting into its channel with an @mention — additive, it feeds
the existing pipeline rather than competing with it.

Pure string-building, kept separate from discord.py so it is unit-testable.
"""

from __future__ import annotations

from typing import Optional


def build_forward_message(
    content: str,
    author_name: str,
    source_channel: Optional[str],
    mention_id: Optional[str],
) -> str:
    """Compose the forwarded task line for the Orchestrator channel.

    The leading ``<@id>`` mention is REQUIRED for the Orchestrator to pick it up
    when its policy is ``DISCORD_ALLOW_BOTS=mentions``. Provenance is included so
    a human reading #hermes-oc sees where the task came from.
    """
    prefix = f"<@{mention_id}> " if mention_id else ""
    src = f"#{source_channel}" if source_channel else "Vorfilter"
    author = author_name or "jemand"
    return (
        f"{prefix}[Weitergeleitet aus {src} von {author} · Fable-5-Vorfilter]\n"
        f"{content.strip()}"
    )

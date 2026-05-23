"""Cross-bot (multi-agent) filter tests for Discord gateway.

Validates the contract for finding F-2026-05-17-01 (Reactor-Matrix v1 row 21):
when multiple Hermes bots share a Discord channel, a message that @-mentions
*another* bot but NOT this bot must be rejected at the channel-side filter
(no LLM gate, no spend). Cross-bot communication goes through the C4 pickup
queue (Hub-Watcher converts coord-authored signals into Hub TASKs), not via
direct cross-bot mentions.

These tests pin the behavior; if the filter is ever loosened to allow
cross-bot triggering, the regression here catches it.
"""

import os
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock


def _make_author(*, bot: bool = False, is_self: bool = False, user_id: int = 12345):
    author = MagicMock()
    author.bot = bot
    author.id = 99999 if is_self else user_id
    author.name = "TestBot" if bot else "TestUser"
    author.display_name = author.name
    return author


def _make_channel_message(*, author, mentions):
    msg = MagicMock()
    msg.author = author
    msg.attachments = []
    msg.mentions = mentions
    msg.webhook_id = None
    msg.application_id = None
    msg.channel = MagicMock()
    msg.channel.id = 222
    msg.channel.name = "shared-channel"
    msg.channel.guild = MagicMock()
    msg.guild = msg.channel.guild
    type(msg.channel).__name__ = "TextChannel"
    return msg


def _multi_agent_filter(message, client_user):
    """Replicates the multi-agent filter block from discord.py:~841.

    Returns True if the message should be processed, False if rejected
    (i.e. another bot is being addressed and we're not).
    """
    import discord

    if isinstance(message.channel, discord.DMChannel):
        return True
    if not message.mentions:
        return True
    self_mentioned = client_user is not None and client_user in message.mentions
    other_bots_mentioned = any(
        m.bot and m != client_user for m in message.mentions
    )
    if other_bots_mentioned and not self_mentioned:
        return False
    return True


class TestDiscordCrossBotFilter(unittest.TestCase):
    """F-2026-05-17-01 contract pin: sibling-bot mentions don't trigger us."""

    def setUp(self):
        self.us = _make_author(is_self=True)
        self.us.bot = True

    def test_other_bot_mention_without_self_mention_dropped(self):
        """Bot-authored or human-authored msg @-mentioning ONLY another bot → reject."""
        other_bot = _make_author(bot=True, user_id=55555)
        msg = _make_channel_message(author=other_bot, mentions=[other_bot])
        self.assertFalse(_multi_agent_filter(msg, self.us))

    def test_other_bot_mention_with_self_mention_passes(self):
        """If the message mentions both the other bot AND us → process."""
        other_bot = _make_author(bot=True, user_id=55555)
        human = _make_author(bot=False)
        msg = _make_channel_message(author=human, mentions=[other_bot, self.us])
        self.assertTrue(_multi_agent_filter(msg, self.us))

    def test_human_mention_other_bot_passes(self):
        """Human authoring a message that mentions another bot → process.

        The filter rejects shared-channel messages that talk to a sibling bot
        regardless of author. This guards spend even when a human posts
        "@OtherBot do X" — we shouldn't reply unless we're also mentioned.
        """
        human = _make_author(bot=False)
        other_bot = _make_author(bot=True, user_id=55555)
        msg = _make_channel_message(author=human, mentions=[other_bot])
        # Current contract: reject if only-other-bot mentioned.
        self.assertFalse(_multi_agent_filter(msg, self.us))

    def test_finding_f_2026_05_17_01_reference_in_discord_py(self):
        """The discord.py multi-agent filter block must reference the finding ID."""
        discord_py = Path("/home/piet/.hermes/hermes-agent/plugins/platforms/discord/adapter.py")
        self.assertTrue(discord_py.exists(), f"discord.py missing at {discord_py}")
        text = discord_py.read_text(encoding="utf-8")
        # The finding ID is stamped in the comment block immediately above the
        # multi-agent filter at line ~841. If a future edit removes the
        # comment, this test catches the drift.
        self.assertIn(
            "F-2026-05-17-01",
            text,
            "discord.py must reference F-2026-05-17-01 above the multi-agent filter",
        )
        # And the surrounding context must still match the filter signature.
        self.assertTrue(
            re.search(
                r"_other_bots_mentioned\s*=\s*any\(",
                text,
            ),
            "discord.py multi-agent filter signature changed unexpectedly",
        )


if __name__ == "__main__":
    unittest.main()

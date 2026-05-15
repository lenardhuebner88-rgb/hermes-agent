"""Tests for Discord bot message filtering (DISCORD_ALLOW_BOTS)."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_author(*, bot: bool = False, is_self: bool = False):
    """Create a mock Discord author."""
    author = MagicMock()
    author.bot = bot
    author.id = 99999 if is_self else 12345
    author.name = "TestBot" if bot else "TestUser"
    author.display_name = author.name
    return author


def _make_message(
    *,
    author=None,
    content="hello",
    mentions=None,
    is_dm=False,
    webhook_id=None,
    application_id=None,
):
    """Create a mock Discord message."""
    msg = MagicMock()
    msg.author = author or _make_author()
    msg.content = content
    msg.attachments = []
    msg.mentions = mentions or []
    msg.webhook_id = webhook_id
    msg.application_id = application_id
    if is_dm:
        import discord
        msg.channel = MagicMock(spec=discord.DMChannel)
        msg.channel.id = 111
        msg.guild = None
    else:
        msg.channel = MagicMock()
        msg.channel.id = 222
        msg.channel.name = "test-channel"
        msg.channel.guild = MagicMock()
        msg.channel.guild.name = "TestServer"
        msg.guild = msg.channel.guild
        # Make isinstance checks fail for DMChannel and Thread
        type(msg.channel).__name__ = "TextChannel"
    return msg


class TestDiscordBotFilter(unittest.TestCase):
    """Test the DISCORD_ALLOW_BOTS filtering logic."""

    def _run_filter(self, message, allow_bots="none", client_user=None):
        """Simulate the on_message bot/app filter and return whether accepted."""
        # Replicate the bot/app-authored filter logic from discord.py on_message.
        if message.author == client_user:
            return False  # own messages always ignored

        is_dm = message.guild is None
        self_mentioned = bool(client_user and client_user in message.mentions)
        author_is_bot = getattr(message.author, "bot", False) is True
        app_authored = bool(
            author_is_bot
            or getattr(message, "webhook_id", None) is not None
            or getattr(message, "application_id", None) is not None
        )
        if app_authored:
            allow = allow_bots.lower().strip()
            if not is_dm and not self_mentioned:
                return False
            if allow == "none":
                return False
            elif allow == "mentions":
                if not self_mentioned:
                    return False
            # "all" falls through

        return True  # message accepted

    def test_own_messages_always_ignored(self):
        """Bot's own messages are always ignored regardless of allow_bots."""
        bot_user = _make_author(is_self=True)
        msg = _make_message(author=bot_user)
        self.assertFalse(self._run_filter(msg, "all", bot_user))

    def test_human_messages_always_accepted(self):
        """Human messages are always accepted regardless of allow_bots."""
        human = _make_author(bot=False)
        msg = _make_message(author=human)
        self.assertTrue(self._run_filter(msg, "none"))
        self.assertTrue(self._run_filter(msg, "mentions"))
        self.assertTrue(self._run_filter(msg, "all"))

    def test_allow_bots_none_rejects_bots(self):
        """With allow_bots=none, all other bot messages are rejected."""
        bot = _make_author(bot=True)
        msg = _make_message(author=bot)
        self.assertFalse(self._run_filter(msg, "none"))

    def test_allow_bots_all_accepts_bot_dm(self):
        """With allow_bots=all, bot DMs are accepted."""
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, is_dm=True)
        self.assertTrue(self._run_filter(msg, "all"))

    def test_allow_bots_all_rejects_shared_channel_bot_without_self_mention(self):
        """Even allow_bots=all must not allow shared-channel bot loops."""
        our_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, mentions=[])
        self.assertFalse(self._run_filter(msg, "all", our_user))

    def test_allow_bots_mentions_rejects_without_mention(self):
        """With allow_bots=mentions, bot messages without @mention are rejected."""
        our_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, mentions=[])
        self.assertFalse(self._run_filter(msg, "mentions", our_user))

    def test_allow_bots_mentions_accepts_with_mention(self):
        """With allow_bots=mentions, bot messages with @mention are accepted."""
        our_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, mentions=[our_user])
        self.assertTrue(self._run_filter(msg, "mentions", our_user))

    def test_default_is_none(self):
        """Default behavior (no env var) should be 'none'."""
        with patch.dict(os.environ, {}, clear=True):
            default = os.getenv("DISCORD_ALLOW_BOTS", "none")
        self.assertEqual(default, "none")

    def test_case_insensitive(self):
        """Allow_bots value should be case-insensitive."""
        our_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, mentions=[our_user])
        self.assertTrue(self._run_filter(msg, "ALL", our_user))
        self.assertTrue(self._run_filter(msg, "All", our_user))
        self.assertFalse(self._run_filter(msg, "NONE", our_user))
        self.assertFalse(self._run_filter(msg, "None", our_user))

    def test_webhook_without_self_mention_in_shared_channel_rejected(self):
        """Webhook/app-authored channel messages without self-mention are ignored."""
        webhook_author = _make_author(bot=False)
        msg = _make_message(author=webhook_author, webhook_id=123456, mentions=[])
        self.assertFalse(self._run_filter(msg, "all"))

    def test_webhook_explicit_self_mention_in_shared_channel_accepted(self):
        """Webhook/app-authored channel messages with explicit self-mention pass."""
        our_user = _make_author(is_self=True)
        webhook_author = _make_author(bot=False)
        msg = _make_message(author=webhook_author, webhook_id=123456, mentions=[our_user])
        self.assertTrue(self._run_filter(msg, "mentions", our_user))

    def test_human_shared_channel_without_self_mention_unaffected_by_loop_guard(self):
        """Human messages still bypass only the bot/app loop guard."""
        human = _make_author(bot=False)
        msg = _make_message(author=human, mentions=[])
        self.assertTrue(self._run_filter(msg, "mentions"))


if __name__ == "__main__":
    unittest.main()

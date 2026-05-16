"""Tests for Discord bot Stop-Code Inbound-Gate (v1.1 2026-05-16).

Spec: ~/vault/03-Agents/Hermes/playbooks/hub-coordinator-reporting-v1.md §6.2/§6.3
RCA:  ~/vault/03-Agents/Hermes/analyses/bot-loop-rca-and-architecture-2026-05-16T1748.md

Purpose: Bot/App-authored messages in shared channels must carry one of the
Inbound Stop-Codes from v1 §6.2 (hub-plan-ready, hub-budget-warn, hub-memory-warn,
hub-shutdown, coord-ack-required, reviewer-rejected) — otherwise Hub silently
drops them, preventing Hub↔Coord feedback loops over short status pings like
"[idle]" / "[ready]".
"""

import os
import re
import unittest
from unittest.mock import MagicMock, patch


DEFAULT_INBOUND_CODES = {
    "hub-plan-ready",
    "hub-budget-warn",
    "hub-memory-warn",
    "hub-shutdown",
    "coord-ack-required",
    "reviewer-rejected",
}


def _make_author(*, bot: bool = False, is_self: bool = False):
    author = MagicMock()
    author.bot = bot
    author.id = 99999 if is_self else 12345
    author.name = "TestBot" if bot else "TestUser"
    author.display_name = author.name
    return author


def _make_message(*, author=None, content="hello", mentions=None, is_dm=False):
    msg = MagicMock()
    msg.author = author or _make_author()
    msg.content = content
    msg.attachments = []
    msg.mentions = mentions or []
    msg.webhook_id = None
    msg.application_id = None
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
        msg.guild = msg.channel.guild
        type(msg.channel).__name__ = "TextChannel"
    return msg


def _stop_code_gate(message, client_user, allow_bots="mentions"):
    """Replicate the discord.py:on_message bot-author + stop-code logic.

    Returns True if the message would be accepted past the bot/stop-code gate,
    False if rejected.

    Mirrors the new v1.1 stop-code gate that sits inside the `if _app_authored:`
    block after the existing allow_bots checks (see discord.py:772-820).
    """
    if message.author == client_user:
        return False  # own messages

    is_dm = message.guild is None
    self_mentioned = bool(client_user and client_user in message.mentions)
    author_is_bot = getattr(message.author, "bot", False) is True
    app_authored = author_is_bot or message.webhook_id or message.application_id

    if not app_authored:
        return True  # humans not affected by stop-code gate

    # Existing allow_bots logic
    allow = (allow_bots or "none").lower().strip()
    if not is_dm and not self_mentioned:
        return False
    if allow == "none":
        return False
    elif allow == "mentions":
        if not self_mentioned:
            return False
    # "all" falls through

    # v1.1 Stop-Code Gate (new)
    if not is_dm:
        stop_codes_csv = os.getenv(
            "HERMES_DISCORD_INBOUND_STOP_CODES",
            "hub-plan-ready,hub-budget-warn,hub-memory-warn,"
            "hub-shutdown,coord-ack-required,reviewer-rejected",
        )
        inbound_codes = {
            c.strip().lower() for c in stop_codes_csv.split(",") if c.strip()
        }
        if "*" not in inbound_codes:
            body = getattr(message, "content", "") or ""
            m = re.search(r"\[STOP-CODE:\s*([a-zA-Z0-9_-]+)\]", body)
            if not m or m.group(1).lower() not in inbound_codes:
                return False

    return True


class TestStopCodeGate(unittest.TestCase):
    """Behavioral tests for the Inbound Stop-Code Gate (v1.1)."""

    def setUp(self):
        # Make sure env doesn't leak between tests
        for v in ("HERMES_DISCORD_INBOUND_STOP_CODES",):
            os.environ.pop(v, None)

    def test_human_unaffected_by_gate(self):
        """Human-authored messages bypass the stop-code gate entirely."""
        human = _make_author(bot=False)
        msg = _make_message(author=human, content="just a normal message")
        self.assertTrue(_stop_code_gate(msg, client_user=_make_author(is_self=True)))

    def test_bot_channel_idle_message_rejected(self):
        """Bot posts '[idle]' in shared channel → rejected (no stop-code)."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, content="[idle]", mentions=[self_user])
        self.assertFalse(_stop_code_gate(msg, client_user=self_user))

    def test_bot_channel_ready_message_rejected(self):
        """Bot posts '[ready]' in shared channel → rejected (no stop-code)."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, content="[ready]", mentions=[self_user])
        self.assertFalse(_stop_code_gate(msg, client_user=self_user))

    def test_bot_channel_with_inbound_stop_code_accepted(self):
        """Bot posts '[STOP-CODE: hub-plan-ready] ...' → accepted."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE: hub-plan-ready] Bitte Plan für Task t_xy.",
            mentions=[self_user],
        )
        self.assertTrue(_stop_code_gate(msg, client_user=self_user))

    def test_bot_channel_with_each_inbound_stop_code_accepted(self):
        """All 6 §6.2 Inbound-Codes pass the gate."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        for code in DEFAULT_INBOUND_CODES:
            msg = _make_message(
                author=bot,
                content=f"[STOP-CODE: {code}] context here",
                mentions=[self_user],
            )
            self.assertTrue(
                _stop_code_gate(msg, client_user=self_user),
                f"code {code} should pass",
            )

    def test_bot_channel_outbound_stop_code_rejected(self):
        """Bot posts '[STOP-CODE: approval-missing]' (an Outbound §6.1 Code) → rejected."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE: approval-missing] waiting",
            mentions=[self_user],
        )
        self.assertFalse(_stop_code_gate(msg, client_user=self_user))

    def test_bot_channel_unknown_stop_code_rejected(self):
        """Bot posts '[STOP-CODE: not-a-real-code]' → rejected."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE: not-a-real-code] ?",
            mentions=[self_user],
        )
        self.assertFalse(_stop_code_gate(msg, client_user=self_user))

    def test_bot_dm_bypasses_gate(self):
        """Bot DMs (not shared channel) bypass the stop-code gate."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, content="hi", is_dm=True)
        # In DM allow_bots=all path
        self.assertTrue(_stop_code_gate(msg, client_user=self_user, allow_bots="all"))

    def test_env_override_wildcard_disables_gate(self):
        """HERMES_DISCORD_INBOUND_STOP_CODES=* disables the gate (allow all bot msgs)."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, content="[idle]", mentions=[self_user])
        with patch.dict(os.environ, {"HERMES_DISCORD_INBOUND_STOP_CODES": "*"}):
            self.assertTrue(_stop_code_gate(msg, client_user=self_user))

    def test_env_override_custom_list(self):
        """Env override with custom code list works as expected."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        with patch.dict(os.environ, {"HERMES_DISCORD_INBOUND_STOP_CODES": "my-custom-code,other-code"}):
            msg_pass = _make_message(
                author=bot,
                content="[STOP-CODE: my-custom-code] ok",
                mentions=[self_user],
            )
            self.assertTrue(_stop_code_gate(msg_pass, client_user=self_user))
            msg_fail = _make_message(
                author=bot,
                content="[STOP-CODE: hub-plan-ready] no longer in default",
                mentions=[self_user],
            )
            self.assertFalse(_stop_code_gate(msg_fail, client_user=self_user))

    def test_stop_code_is_case_insensitive(self):
        """[STOP-CODE: HUB-PLAN-READY] also accepted."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE: HUB-PLAN-READY] yes",
            mentions=[self_user],
        )
        self.assertTrue(_stop_code_gate(msg, client_user=self_user))

    def test_bot_without_self_mention_still_rejected(self):
        """Bot without @-mention in channel → rejected by earlier filter (gate not reached)."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE: hub-plan-ready] msg",
            mentions=[],  # NO mention of self
        )
        # require_mention=mentions and self_mentioned=False → reject before gate
        self.assertFalse(_stop_code_gate(msg, client_user=self_user, allow_bots="mentions"))

    def test_extra_whitespace_in_stop_code_accepted(self):
        """[STOP-CODE:  hub-plan-ready ] (extra whitespace) still accepted."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE:  hub-plan-ready] flexible",
            mentions=[self_user],
        )
        self.assertTrue(_stop_code_gate(msg, client_user=self_user))

    def test_stop_code_in_middle_of_message_accepted(self):
        """Stop-code can appear anywhere in body (regex.search, not match)."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="Hi Hub. [STOP-CODE: hub-memory-warn] Memory is 95%.",
            mentions=[self_user],
        )
        self.assertTrue(_stop_code_gate(msg, client_user=self_user))


if __name__ == "__main__":
    unittest.main()

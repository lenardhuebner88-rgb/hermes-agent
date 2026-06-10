"""discord.py wiring for the pre-filter bridge.

A standalone bot (its own token) that listens in exactly ONE channel, triages
each message on the Max subscription, and either answers trivially itself,
ignores noise, or escalates real work to the full Hermes agent.

Blocking subprocess work (``claude -p`` triage, ``hermes -z`` escalation) is
offloaded to a thread executor so the Discord event loop never stalls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

import discord

from bridges.discord_prefilter.config import PrefilterConfig
from bridges.discord_prefilter.escalate import run_hermes_oneshot
from bridges.discord_prefilter.forward import build_forward_message
from bridges.discord_prefilter.triage import Bucket, run_triage

logger = logging.getLogger("discord_prefilter.bot")

_DISCORD_LIMIT = 1990  # leave headroom under the hard 2000-char cap


def _chunk(text: str, limit: int = _DISCORD_LIMIT) -> List[str]:
    """Split text into Discord-sized chunks on line boundaries where possible."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single very long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


async def _send(channel: discord.abc.Messageable, text: str) -> None:
    for chunk in _chunk(text):
        await channel.send(chunk)


def build_client(config: PrefilterConfig) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:  # noqa: D401 - discord.py callback
        esc = (
            f"{config.escalate_mode}→{config.orchestrator_channel_id}"
            if config.escalate_enabled else "off"
        )
        logger.info(
            "pre-filter online as %s | locked to channel %s | model=%s | escalate=%s",
            client.user, config.channel_id, config.model, esc,
        )

    @client.event
    async def on_message(message: discord.Message) -> None:  # noqa: D401
        # Hard channel lock — the pilot only ever acts in one channel.
        if message.channel.id != config.channel_id:
            return
        # Never react to ourselves.
        if client.user is not None and message.author.id == client.user.id:
            return
        # Skip other bots unless explicitly allowed.
        if message.author.bot and not config.allow_bots:
            return
        # Only normal messages / replies (no pins, joins, system notices).
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        content = (message.content or "").strip()
        if not content:
            return

        loop = asyncio.get_running_loop()
        try:
            decision = await loop.run_in_executor(None, run_triage, content, config)
        except Exception:  # triage is fail-open, but never let the loop die
            logger.exception("triage crashed; ignoring message")
            return

        logger.info("triage: bucket=%s source=%s text=%r",
                    decision.bucket.value, decision.source, content[:120])

        if decision.bucket is Bucket.NOISE:
            if config.react_on_noise:
                try:
                    await message.add_reaction(config.react_on_noise)
                except discord.DiscordException:
                    pass
            return

        if decision.bucket is Bucket.TRIVIAL:
            await _send(message.channel, decision.reply or "")
            return

        # --- ESCALATE ---
        if not config.escalate_enabled:
            await message.reply(config.escalate_placeholder)
            return

        # Mode "orchestrator" (default): forward the task to the live Hub
        # Orchestrator's channel with an @mention so the existing Kanban
        # pipeline handles it. Additive — does not run a competing agent.
        if config.escalate_mode == "orchestrator":
            text = build_forward_message(
                content,
                message.author.display_name,
                getattr(message.channel, "name", None),
                config.orchestrator_mention_id,
            )
            try:
                channel = client.get_channel(config.orchestrator_channel_id)
                if channel is None:
                    channel = await client.fetch_channel(config.orchestrator_channel_id)
                await channel.send(
                    text, allowed_mentions=discord.AllowedMentions(users=True)
                )
            except Exception as exc:  # surface, don't swallow
                logger.exception("forward to orchestrator failed")
                await message.reply(f"⚠️ Weiterleitung an den Orchestrator fehlgeschlagen: {exc}")
                return
            await message.reply(config.escalate_forward_ack)
            return

        # Mode "oneshot": run a standalone full Hermes turn and relay the answer.
        async with message.channel.typing():
            try:
                answer = await loop.run_in_executor(
                    None, run_hermes_oneshot, content, config
                )
            except Exception as exc:  # surface, don't swallow
                logger.exception("escalation failed")
                await message.reply(f"⚠️ Eskalation an Hermes fehlgeschlagen: {exc}")
                return
        await _send(message.channel, answer)

    return client


def run(config: PrefilterConfig) -> None:
    client = build_client(config)
    client.run(config.discord_token, log_handler=None)

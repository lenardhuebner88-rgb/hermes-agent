"""Regression tests for the user allowlist gate (security hardening S3).

The pre-filter bridge used to gate only on ``channel_id`` — anyone who could
post in the pilot channel reached the triage model and, on escalate, the full
Hermes worker pipeline. The allowlist closes that door: only configured
Discord user ids are triaged; with no allowlist configured the bot fails
CLOSED (and logs why) instead of silently serving everyone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from bridges.discord_prefilter import bot as bot_mod
from bridges.discord_prefilter.config import PrefilterConfig


ALLOWED_ID = 207500364776341504
STRANGER_ID = 999999999999999999
CHANNEL_ID = 1486480074491559966


def make_config(**overrides) -> PrefilterConfig:
    kwargs = dict(
        discord_token="x",
        channel_id=CHANNEL_ID,
        model="fable 5",
        allowed_user_ids=frozenset({ALLOWED_ID}),
    )
    kwargs.update(overrides)
    return PrefilterConfig(**kwargs)


def make_message(author_id: int, *, bot: bool = False, content: str = "deploy the fix"):
    return SimpleNamespace(
        channel=SimpleNamespace(id=CHANNEL_ID, name="reviewer"),
        author=SimpleNamespace(id=author_id, bot=bot, display_name="someone"),
        type=discord.MessageType.default,
        content=content,
    )


def run_on_message(config: PrefilterConfig, message, monkeypatch):
    """Drive the registered on_message handler; capture triage calls."""
    calls: list[str] = []

    def fake_run_triage(content, cfg):
        calls.append(content)
        return SimpleNamespace(
            bucket=bot_mod.Bucket.NOISE, source="test", reply=None
        )

    monkeypatch.setattr(bot_mod, "run_triage", fake_run_triage)
    client = bot_mod.build_client(config)
    asyncio.run(client.on_message(message))
    return calls


def test_allowed_user_reaches_triage(monkeypatch):
    calls = run_on_message(make_config(), make_message(ALLOWED_ID), monkeypatch)
    assert calls, "allowlisted user must reach triage"


def test_stranger_is_ignored(monkeypatch):
    calls = run_on_message(make_config(), make_message(STRANGER_ID), monkeypatch)
    assert not calls, "non-allowlisted user must never reach triage"


def test_empty_allowlist_fails_closed(monkeypatch):
    cfg = make_config(allowed_user_ids=frozenset())
    calls = run_on_message(cfg, make_message(ALLOWED_ID), monkeypatch)
    assert not calls, "empty allowlist must fail closed, not open"


def test_allowlisted_bot_still_needs_allow_bots(monkeypatch):
    cfg = make_config()
    calls = run_on_message(
        cfg, make_message(ALLOWED_ID, bot=True), monkeypatch
    )
    assert not calls, "bot authors stay gated by allow_bots"


# --- config loader ----------------------------------------------------------

def _load_config(monkeypatch, tmp_path, **env):
    for key in (
        "PREFILTER_ALLOWED_USERS",
        "DISCORD_ALLOWED_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PREFILTER_DISCORD_TOKEN", "t")
    monkeypatch.setenv("PREFILTER_CHANNEL_ID", str(CHANNEL_ID))
    monkeypatch.setenv("PREFILTER_MODEL", "fable 5")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return PrefilterConfig.load(env_file=str(tmp_path / "missing.env"))


def test_load_parses_prefilter_allowed_users(monkeypatch, tmp_path):
    cfg = _load_config(
        monkeypatch, tmp_path,
        PREFILTER_ALLOWED_USERS=f"{ALLOWED_ID}, 42",
    )
    assert cfg.allowed_user_ids == frozenset({ALLOWED_ID, 42})


def test_load_falls_back_to_discord_allowed_users(monkeypatch, tmp_path):
    cfg = _load_config(
        monkeypatch, tmp_path,
        DISCORD_ALLOWED_USERS=str(ALLOWED_ID),
    )
    assert cfg.allowed_user_ids == frozenset({ALLOWED_ID})


def test_load_without_allowlist_is_empty(monkeypatch, tmp_path):
    cfg = _load_config(monkeypatch, tmp_path)
    assert cfg.allowed_user_ids == frozenset()

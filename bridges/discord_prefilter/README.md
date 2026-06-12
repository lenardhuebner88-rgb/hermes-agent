# Discord Pre-Filter Bridge

A standalone Discord bot that sits **in front of** the main Hermes responder as a
cheap **pre-filter**. It runs in **one dedicated channel**, triages every message
with a small model on the **Max subscription** (`claude -p`, **no API call**), and:

- **trivial** → answers itself, for free, on Max;
- **escalate** → hands the message to the full Hermes agent (`hermes -z`) and relays the answer;
- **noise** → ignores it (optionally reacts).

It is a separate process that talks to Hermes only through public CLIs (`claude`,
`hermes`) — it never imports gateway internals, so it stays robust against churn in
the agent core. The live Hermes bot/channel is **not touched**.

## Why this exists

Today the Hermes Discord bot answers *every* message with the configured default
(`gpt-5.5` → a paid API call). This bridge catches the bulk of traffic for free on
the Max subscription and only escalates real work.

## No-API guarantee

The triage call deliberately **strips `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`**
from the `claude` subprocess env (`config.claude_env()`), forcing the `claude` CLI
onto its **subscription OAuth** login. The pre-filter therefore cannot make a paid
API call even if a key is present in the environment.

## Prerequisites (Discord portal)

1. Reactivate the old **Kanbanops** bot (it currently exists but is offline).
2. Enable **MESSAGE CONTENT INTENT** for it (Developer Portal → Bot → Privileged
   Gateway Intents) — required to read message text.
3. Invite the bot to your server and into the **dedicated pilot channel** only.
4. Note that channel's numeric **channel id** (right-click → Copy ID, Developer Mode on).

## Configuration

Create `~/.hermes/bridges/discord_prefilter.env` (its own namespace — **not** the
main bot's `DISCORD_BOT_TOKEN`):

```ini
PREFILTER_DISCORD_TOKEN=<the reactivated Kanbanops bot token>
PREFILTER_CHANNEL_ID=<numeric id of the pilot channel>
PREFILTER_MODEL=<claude -p --model alias for "fable 5">

# Phase 1: leave escalation OFF (triage-only). Flip to on for Phase 2.
PREFILTER_ESCALATE=off
# PREFILTER_ESCALATE_PROFILE=default
# PREFILTER_NOISE_REACTION=👀
# PREFILTER_NOISE_PATTERNS=^\s*ping\s*$|||^\s*test\s*$
# PREFILTER_CLAUDE_BIN=/home/piet/.local/bin/claude
# PREFILTER_TRIAGE_TIMEOUT_S=45
# PREFILTER_ESCALATE_TIMEOUT_S=600
```

## Running

Use the Hermes **venv** interpreter so `discord.py` and `hermes_cli` resolve:

```bash
/home/piet/.hermes/hermes-agent/.venv/bin/python -m bridges.discord_prefilter
```

### Self-test (no Discord connection)

```bash
# Exercises the Max path (claude -p). Proves triage works + no API call.
.venv/bin/python -m bridges.discord_prefilter --triage "läuft alles?"

# Exercises escalation (hermes -z). Needs PREFILTER_ESCALATE not required here.
.venv/bin/python -m bridges.discord_prefilter --escalate "kurzer test"
```

## Rollout phases

1. **Phase 0 — setup:** portal steps above; confirm the `PREFILTER_MODEL` alias.
2. **Phase 1 — triage-only:** `PREFILTER_ESCALATE=off`. Bot answers `trivial`,
   posts a placeholder on `escalate`, ignores `noise`. Validates buckets at zero
   default cost.
3. **Phase 2 — escalation on:** `PREFILTER_ESCALATE=on`. `hermes -z` relays real
   answers into the channel.
4. **Later (out of scope here):** widen to the real Hermes channel.

## Triage contract

The model is asked to return only `{"bucket": "trivial|escalate|noise", "reply": ...}`.
Parsing is **fail-open**: any malformed / missing / ambiguous output becomes
`escalate`, so a real request is never silently dropped (see `triage.py`).

**Inert by design:** the triage call uses `claude -p --system-prompt <classifier>`
(a *full* system-prompt replacement — `--append-system-prompt` gets overridden by
Claude Code's coding-assistant identity) together with `--tools ""` (no tools at
all). So the pre-filter agent can only classify; it cannot run commands, edit
files, or start performing the task it is reading.

**Latency:** a real `claude -p` cold call is ~7–15 s per message (it is a full
Claude Code process, not a chat-completion endpoint). The noise heuristic answers
obvious chatter with **zero** model spawn; only non-noise messages pay the model
round-trip. Fine for a pilot; revisit if throughput matters.

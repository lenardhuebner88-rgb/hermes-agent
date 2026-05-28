# Daily Research Radar Discord Post

`scripts/daily_research_post.py` is a deterministic V1 daily research job for the Hermes/OpenClaw research channel. It fetches prioritized RSS/Atom sources, filters and deduplicates entries, ranks the strongest 3-5 system-relevant signals, formats a German Discord post, and can either print the post for Hermes cron delivery or send it directly via the existing `send_message` gateway infrastructure.

Default target:

- Discord channel: `1491150772224659649`
- Suggested schedule: `0 7 * * *` (daily morning run; scheduler timezone is the host/runtime timezone)

## Local dry run

From the Hermes Agent repository:

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
python scripts/daily_research_post.py --verbose
```

The command prints the Discord-ready message to stdout and does not send anything.

## Direct send path

Use this when a normal systemd timer or another external scheduler should send the post itself:

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
python scripts/daily_research_post.py --send --verbose
```

`--send` calls `tools.send_message_tool.send_message_tool` with target `discord:<channel-id>`, so it reuses the configured Hermes gateway/posting stack. Do not combine `--send` with a Hermes cron `--deliver discord:...` delivery for the same run, or the channel can receive duplicates.

## Hermes cron delivery path

Use this when Hermes cron should own delivery. The script prints only the curated message; cron delivers stdout to Discord:

```bash
hermes cron create "0 7 * * *" \
  --name "Daily Research Radar" \
  --script daily_research_post.py \
  --no-agent \
  --deliver discord:1491150772224659649
```

Cron script paths are resolved under `~/.hermes/scripts/`, so deploy this repository script there (or use a tiny wrapper in `~/.hermes/scripts/daily_research_post.py` that imports/runs `~/.hermes/hermes-agent/scripts/daily_research_post.py`). This implementation task does not mutate the live cron board/job config.

## Optional JSON config

Set `HERMES_DAILY_RESEARCH_CONFIG=/path/to/daily-research.json` or pass `--config`:

```json
{
  "channel_id": "1491150772224659649",
  "schedule": "0 7 * * *",
  "max_items": 5,
  "lookback_hours": 72,
  "sources": [
    {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml", "priority": "P1"},
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml", "priority": "P1"},
    {"name": "GitHub Changelog", "url": "https://github.blog/changelog/feed/", "priority": "P2"}
  ]
}
```

Environment overrides:

- `HERMES_DAILY_RESEARCH_CHANNEL_ID`
- `HERMES_DAILY_RESEARCH_SCHEDULE`
- `HERMES_DAILY_RESEARCH_MAX_ITEMS`
- `HERMES_DAILY_RESEARCH_LOOKBACK_HOURS`

## Selection logic

V1 is deterministic and auditable:

1. Fetch enabled feeds with bounded timeouts; a failing feed logs a warning and is skipped.
2. Deduplicate by normalized URL, then normalized title.
3. Score by source priority (P1/P2/P3), freshness, and relevance keywords around agents, MCP/tools, evals, memory/context, model routing/inference, security/hardening, and operations.
4. Keep up to 5 entries above the signal threshold.
5. Add a concise "Was bringt uns das im System?" impact line using keyword-based heuristics.
6. If no strong signal remains, produce a low-signal fallback message instead of fabricating items.

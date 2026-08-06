# disk-cleanup

Auto-tracks and cleans up ephemeral files created during Hermes Agent
sessions — test scripts, temp outputs, cron logs, stale chrome profiles.
Scoped strictly to `$HERMES_HOME` and `/tmp/hermes-*`.

Originally contributed by [@LVT382009](https://github.com/LVT382009) as a
skill in PR #12212.  Ported to the plugin system so the behaviour runs
automatically via `post_tool_call` and `on_session_end` hooks — the agent
never needs to remember to call a tool.

## How it works

| Hook | Behaviour |
|---|---|
| `post_tool_call` | When `write_file` / `terminal` / `patch` touches a path inside `HERMES_HOME` or `/tmp/hermes-*`, `guess_category()` decides whether to track it silently (see below). |
| `on_session_end` | If any test files were auto-tracked during this turn, run `quick` cleanup (no prompts). |

Auto-categorisation is **either** by name **or** by location — the two rules are
independent, and only the name rules produce the `test` category:

| Rule | Category |
|---|---|
| Name starts with `test_` or `tmp_` | `test` |
| Name ends with `.test.py`, `.test.js`, `.test.ts`, `.test.md` (exactly these four — `.test.tsx` / `.test.json` are *not* matched) | `test` |
| Anything under `$HERMES_HOME/cache/`, regardless of name | `temp` |
| Anything under `$HERMES_HOME/cron/output/` or `cronjobs/output/`, regardless of name | `cron-output` |
| Everything else (incl. the excluded top-level dirs below) | not tracked |

Deletion rules (same as the original PR):

| Category | Threshold | Confirmation |
|---|---|---|
| `test` | every session end | Never |
| `temp` | >7 days since tracked | Never |
| `cron-output` | >14 days since tracked | Never |
| empty dirs under HERMES_HOME | always | Never |
| `research` | >30 days, beyond 10 newest | Always (deep only) |
| `chrome-profile` | >14 days since tracked | Always (deep only) |
| files >500 MB | never auto | Always (deep only) |

## Slash command

```
/disk-cleanup status                     # breakdown + top-10 largest
/disk-cleanup dry-run                    # preview without deleting
/disk-cleanup quick                      # run safe cleanup now
/disk-cleanup deep                       # quick + list items needing prompt
/disk-cleanup track <path> <category>    # manual tracking
/disk-cleanup forget <path>              # stop tracking
```

## Safety

- `is_safe_path()` rejects anything outside `HERMES_HOME` or `/tmp/hermes-*`
- Windows mounts (`/mnt/c` etc.) are rejected
- The state directory `$HERMES_HOME/disk-cleanup/` is itself excluded
- `$HERMES_HOME/logs/`, `memories/`, `sessions/`, `skills/`, `plugins/`,
  and config files are never tracked
- Backup/restore is scoped to `tracked.json` — the plugin never touches
  agent logs
- Atomic writes: `.tmp` → backup → rename

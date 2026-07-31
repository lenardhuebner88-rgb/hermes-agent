# usage-facts harvest — operator wiring

Two harvesters feed `origin` classes into
`/mnt/data/hermes-observability/usage_facts.db` that no live hook writes:

- `scripts/harvest_claude_code_usage.py` — Claude Code transcripts below
  `~/.claude/projects` (`origin=claude_code`). High-water mark in
  `<db>.claude_code_hwm.json`.
- `scripts/harvest_foreign_lanes.py` — Codex, Kimi, Grok and Qwen CLI logs
  (`origin=codex_cli|kimi_cli|grok_cli|qwen_cli`). Per-source fingerprints in
  `foreign_lane_harvest_state.json`.

The remaining origins (`hermes_agent`, `hermes_aux`) are written live by the
`observability/board_facts` plugin and need no harvest.

## Scheduling

`hermes-usage-harvest.timer` runs `hermes-usage-harvest.service` every 15
minutes (`OnCalendar=*:0/15`, `Persistent=true` so a reboot catches up). Both
harvesters run as two sequential `ExecStart` lines in one `oneshot` unit —
never in parallel, because both write the same SQLite file.

Install (units live in `scripts/systemd/`, copied not symlinked, per the
convention of the other Hermes units):

    cp scripts/systemd/hermes-usage-harvest.{service,timer} ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now hermes-usage-harvest.timer

Canon `2026-07-31-metrik-ssot-register` rule 4 binds this to a timer: capture
never sits in the session or worker critical path, so there is deliberately
**no** Stop-hook. Measured on 2026-07-31, that costs nothing worth optimising —
a run with no new source data takes about a second.

## Measured cost

| case | wall time |
|---|---|
| cold run, ~2 900 transcripts + 4-day foreign backlog | 49 s |
| Claude Code alone, 14 h backlog | 42 s |
| Claude Code alone, nothing new | 0.7 s |
| foreign lanes alone, nothing new | 0.1 s |

Both are idempotent: a second run writes 0 rows, and `(origin, run_id)` stayed
unique across a probe run on a DB copy.

## Verifying it works

`captured_at` carries the **source** timestamp — when the session or call
happened — not when the harvest ran. A lane that nobody used for 13 hours
therefore has a 13-hour-old newest row, and that is correct, not a backlog.
So do not check freshness against the clock; check the harvest against its
source:

    systemctl --user list-timers hermes-usage-harvest.timer
    journalctl --user -u hermes-usage-harvest.service -o cat --since -1h

The per-origin JSON in the journal is the real check: `errors: 0`, and
`scanned == extracted + skipped_*`. To confirm a specific lane is complete,
compare the newest session in its source tree against the rows in
`run_usage_facts` — presence, not timestamp.

Note that Codex writes the session **start** into `captured_at`, so a long
session that ended recently still sorts by its old start time. Ordering
`codex_cli` rows by `captured_at` does not give you the most recently active
session.

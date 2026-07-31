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

**`captured_at` is write time, not event time — never filter a report by it.**
An earlier version of this file claimed the opposite; it was wrong, and the
error is load-bearing enough to spell out. Measured 2026-07-31 against the live
DB:

| origin | rows | distinct `captured_at` seconds |
|---|---|---|
| `claude_code` | 106 026 | **76** |
| `hermes_agent` | 1 661 | 1 238 |
| `qwen_cli` | 1 637 | 1 557 |
| `codex_cli` | 1 255 | 1 248 |
| `grok_cli` | 901 | 896 |
| `kimi_cli` | 171 | 171 |

The live-writing origins land near 1:1 because writing and happening coincide
for them. `claude_code` does not: the harvester reads transcripts *later*, so
106 026 rows collapse onto the 76 seconds its ticks were running. The mechanism
is `_refresh_run_aggregates` in `hermes_cli/usage_facts_db.py`, whose last
statement stamps `captured_at=utc_now_iso()` unconditionally after every call —
overwriting the source timestamp the harvester correctly passed one statement
earlier. `_upsert_run_facts` fills the same default when the value is absent.

The practical consequence: a "what did today cost" query filtered on
`captured_at` reports weeks of transcript history as today. On 2026-07-31 that
would have been ~101 M input tokens attributed to a single day.

Until the event-time columns land, the honest time source per origin is:
transcript `timestamp` for `claude_code`, `task_runs.started_at` (epoch int, in
`kanban.db`) for anything correlated to a Kanban run, and the run directory for
the foreign lanes.

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
session — a second reason not to treat the column as an event clock.

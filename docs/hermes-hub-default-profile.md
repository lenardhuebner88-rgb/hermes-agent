# Hermes HUB/DEFAULT Profile

The HUB/DEFAULT profile is the durable messaging surface that ships
Discord, Telegram, Slack and other chat-gateway events.  Its job is to
stay online, reply fast, and route work out — not to host CPU-heavy
test/build/typecheck loops.

This document captures the rules, runtime checks, and tuning knobs that
landed with `feat: HUB/DEFAULT profile policy` (branch
`hermes-hub-default-profile-optimization`, May 2026).

## Identity

A gateway is bound to the HUB profile iff its `HERMES_HOME` resolves to
the default Hermes root (typically `~/.hermes`).  Two locations are
explicitly **not** the HUB:

* `<root>/profiles/<name>` — named work profiles such as `coder`,
  `reviewer`, `research`.
* `<root>/worktrees/<name>` — isolated git worktrees used for
  parallel branch work.

The classifier lives in [`gateway/profile_policy.py`](../gateway/profile_policy.py)
under `is_default_hermes_profile_home()` and is import-light enough to
be called from any gateway path.

## Rules at the HUB

* **No heavy local workloads** — the terminal tool refuses
  `pytest`, `ruff`, `mypy`, `pyright`, `tox`, `nox`, `coverage`, and
  the `uv`/`pdm`/`hatch run pytest` variants when run inside a
  `HERMES_GATEWAY_SESSION` with `env_type="local"`.  Run those in a
  named profile or a worktree instead.
* **No Minimax fallback** — Minimax provider entries are stripped from
  `fallback_providers` and the legacy `fallback_model` at the HUB.
  The runtime warns once at gateway startup if the config contains
  Minimax entries that will be filtered.  See `MINIMAX_PROVIDER_NAMES`
  and `MINIMAX_MODEL_MARKERS` in `gateway/profile_policy.py` for the
  exact match list.
* **Quiet Discord defaults** — `tool_progress="new"`,
  `tool_preview_length=80`.  Tier-High remains the default for named
  profiles and worktrees.  Explicit operator config in
  `display.platforms.discord.*` or `display.<key>` still wins.
* **Token pressure is observed, not acted on** — every agent turn
  persists `token_usage` to `gateway_state.json` with a classification
  (`ok` / `watch` / `critical`).  The classification is informational;
  `model.default` is never altered.

## Recommended HUB Config Shape

These values are advisory — the code does not enforce them.  They
match the rules above and keep the Discord channel quiet enough for
operator-grade signals to land cleanly.

```yaml
display:
  platforms:
    discord:
      tool_progress: new           # auto-applied by gateway when at HUB
      tool_preview_length: 80      # auto-applied by gateway when at HUB
      interim_assistant_messages: true

# Move heavy fallbacks to named profiles:
#   ~/.hermes/profiles/coder/config.yaml
#   ~/.hermes/profiles/research/config.yaml
fallback_providers: []             # or omit entirely at HUB
```

> **Notes**
>
> * `provider_routing.ignore` is an **OpenRouter pass-through** option;
>   it does NOT block Minimax when Minimax is wired as its own
>   provider entry.  HUB Minimax suppression runs exclusively through
>   `gateway.profile_policy.filter_default_gateway_fallbacks`.
> * Telegram is out of scope for this sprint.  Its default is already
>   `tool_progress="new"` (`gateway/display_config.py:84`); Slack is
>   `"off"`.  Discord was the only loud default.

## Operational Check

```bash
hermes gateway status
```

On a HUB instance running the new code, expect:

* `✓ discord: online (last heartbeat 35s ago, latency 750ms, lag watch)`
  — populated from `DiscordAdapter.runtime_health()` snapshots written
  by `_write_runtime_status_safe`.
* `Token pressure: <ok|watch|critical> <pct>% of context on <model>`
  — populated once per agent turn from `gateway/run.py`.
* A single `WARNING profile_policy: ... [default-profile-minimax-fallback-filtered]`
  line in `gateway.log` if the live `config.yaml` contains Minimax
  fallback entries.  The warning is emitted once per gateway start.

Named profiles and worktrees keep the historical output shape.

## Thresholds

All thresholds are module-level constants in
[`gateway/profile_policy.py`](../gateway/profile_policy.py) and can be
overridden via environment variable.  Operators do not need to edit
code to retune the gateway.

| Constant                       | Default | Env override                       |
|--------------------------------|---------|------------------------------------|
| `PRESSURE_WATCH_PCT`           | 65      | `HERMES_PRESSURE_WATCH_PCT`        |
| `PRESSURE_CRITICAL_PCT`        | 85      | `HERMES_PRESSURE_CRITICAL_PCT`     |
| `PRESSURE_FLOOR_TOKENS`        | 20 000  | `HERMES_PRESSURE_FLOOR_TOKENS`     |
| `DISCORD_LAG_WATCH_MS`         | 500     | `HERMES_DISCORD_LAG_WATCH_MS`      |
| `DISCORD_LAG_CRITICAL_MS`      | 1000    | `HERMES_DISCORD_LAG_CRITICAL_MS`   |

Absolute prompt counts below `PRESSURE_FLOOR_TOKENS` always classify as
`"ok"`, even when the relative percentage would otherwise trip
`watch` or `critical`.  This prevents tiny dev / fixture contexts from
producing alarming log noise.

## Rollback

The branch is implemented as a worktree under
`~/.hermes/worktrees/hub-default-profile-opt`.  To roll back the entire
sprint without disturbing `main`:

```bash
cd ~/.hermes/hermes-agent
git worktree remove ../worktrees/hub-default-profile-opt --force
git branch -D hermes-hub-default-profile-optimization
git stash pop   # restore the Codex TTFB watchdog WIP from Phase 0.2
```

For a partial rollback, revert the offending phase's single commit and
leave the rest in place — each phase is a self-contained commit.

The new fields in `gateway_state.json` (`token_usage`, per-platform
`health`) are additive.  External consumers that read the file
read-only ignore unknown fields, so no schema migration is required.

# Hermes Agent - Startup Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

**Never give up on the right solution.**

This file is intentionally short enough to load in every agent startup context. Detailed, slower-moving reference material lives in [`docs/agent-dev-guide.md`](docs/agent-dev-guide.md). When in doubt, inspect the live code and prefer the detailed guide over memory.

## Vault: Collaboration & Provenance (shared memory hub)

This host's Vault (`/home/piet/vault`) is the shared work + memory hub for Hermes, Claude Code, Codex, and Kimi. Durable cross-agent facts live once in `00-Canon/`; `10-KB/` is frozen legacy/OpenClaw compiler output and is not authoritative.

Around any edit-risk work:

1. **Check-IN before the first edit** — run `python3 /home/piet/vault/_agents/_shared/scripts/coordination-open-sessions.py`. If an open `touching:` overlaps your planned paths, stop and coordinate. Otherwise create a session note in `/home/piet/vault/_agents/_coordination/` with `agent`, `started`, `ended: null`, `task`, and `touching:`.
2. **Work** — re-run the overlap check before further concrete writes when scope changes.
3. **Receipt** — write a receipt/report under `/home/piet/vault/03-Agents/<your-agent>/receipts/` naming task IDs, changes, evidence, and status. Use your actual agent identity (`Hermes/`, `Claude-Code/`, `Codex/`, `Kimi/`).
4. **Check-OUT** — set `ended:` in your coordination note.

Canonical rules: `/home/piet/vault/00-Canon/conventions-gates.md` and `/home/piet/vault/00-Canon/planspec-taskgraph.md`.

## What Hermes Is

Hermes is a personal AI agent that runs the same core across CLI, messaging gateway, TUI, desktop app, scheduled jobs, memory/skills, tools, terminal, and browser.

Two properties shape almost every design decision:

- **Per-conversation prompt caching is sacred.** Do not mutate past context, swap toolsets, or rebuild the system prompt mid-conversation except through supported compression paths.
- **The core is a narrow waist; capability lives at the edges.** New capability should usually be CLI + skill, service-gated tool, plugin, or MCP server — not a new core model tool.

## Contribution Intent

Wanted:

- Fix real bugs with evidence: reproduce against current code, identify the exact behavior/line, and fix the bug class rather than a single symptom.
- Expand reach at the edges: platforms, channels, providers, models, dashboard/TUI/desktop features are welcome when integrated into existing setup/config UX.
- Refactor god-files into focused modules when the refactor itself is the declared task.
- Preserve prompt caching, strict role alternation, stable system prompts, and contributor credit.
- Use behavior/invariant tests instead of snapshots of expected-to-change data.

Avoid:

- Speculative hooks or extension points without a concrete consumer.
- New `HERMES_*` env vars for non-secret config. `.env` is for credentials only; behavior belongs in `config.yaml`.
- New core tools when terminal/file/CLI/skill/plugin/MCP can solve the need.
- Lazy-reading pagination on instructional tools that agents must read fully.
- Mitigations that destroy the feature they are supposed to secure.
- Outbound telemetry, usage attribution, or third-party identifiers without explicit opt-in gating.
- Plugins modifying core files. Expand generic plugin surfaces instead.
- Third-party products (observability backends, vendor SaaS connectors, analytics dashboards) integrated into the core tree. Ship as a standalone plugin repo instead.

Before calling something a bug, verify both the premise and the original intent with live code/history. Intentional omissions can be load-bearing.

## The Footprint Ladder

Choose the least permanent surface that solves the problem:

1. Extend existing code.
2. Add a CLI command + skill.
3. Add a service-gated tool (`check_fn`) so it has zero footprint when unavailable.
4. Build a plugin.
5. Add an MCP server/catalog entry.
6. Add a new core tool only as a last resort for broadly fundamental capabilities unreachable otherwise.

When multiple PRs integrate the same category, design a shared ABC/orchestrator and plugin surface rather than merging one-off integrations.

## Development Environment

```bash
source .venv/bin/activate   # or: source venv/bin/activate
scripts/run_tests.sh <target>
scripts/run-affected.sh
```

`scripts/run_tests.sh` probes `.venv`, `venv`, then `$HOME/.hermes/hermes-agent/venv` and enforces CI-like environment isolation. Prefer targeted gates; the full suite is a nightly/pre-release concern, not an interactive default.

## Project Map

File counts shift; inspect the filesystem for the canonical view. Load-bearing entry points:

```text
run_agent.py          # AIAgent and core conversation loop
model_tools.py        # tool discovery/dispatch, plugin pre/post hooks
toolsets.py           # toolset definitions, _HERMES_CORE_TOOLS
cli.py                # classic CLI orchestrator
hermes_state.py       # SessionDB + FTS5 search
hermes_constants.py   # get_hermes_home(), display_hermes_home()
hermes_logging.py     # profile-aware logs
agent/                # provider adapters, memory, caching, compression, etc.
hermes_cli/           # CLI subcommands, setup wizard, plugins loader, skin engine
tools/                # built-in tools and terminal backends
gateway/              # messaging gateway + platform adapters
plugins/              # general, memory, model-provider, kanban, observability, etc.
skills/               # built-in skills
ui-tui/               # Ink TUI
apps/desktop/         # Electron desktop app
tui_gateway/          # JSON-RPC backend for TUI/desktop
acp_adapter/          # ACP server
cron/                 # scheduler
scripts/              # test/release/support scripts
website/              # docs site
tests/                # pytest suite
```

See `ARCHITECTURE.md` for generated backend/dashboard/Kanban lifecycle maps.

## File Dependency Chain

```text
tools/registry.py
  ↑
tools/*.py
  ↑
model_tools.py
  ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

## AIAgent and Loop Basics

`AIAgent` lives in `run_agent.py`; read the real signature before editing because it has many parameters. Common knobs include provider/model/API mode, toolsets, memory/context/session settings, credential pool, callbacks, budget, and platform metadata.

The core loop is synchronous: call the model, dispatch tool calls through `handle_function_call()`, append tool results, track iteration/budget, and return final assistant content when no tool calls remain. Preserve OpenAI-style role alternation and do not inject synthetic user messages mid-loop.

## CLI / Slash Commands

`HermesCLI.process_command()` in `cli.py` dispatches slash commands. The canonical registry is `hermes_cli/commands.py`; CLI help, gateway known commands, Telegram menu, Slack routing, and autocomplete derive from it.

To add a slash command:

1. Add a `CommandDef` to `COMMAND_REGISTRY`.
2. Add CLI handling in `HermesCLI.process_command()`.
3. If available in messaging, add gateway handling in `gateway/run.py`.
4. Persist settings through config helpers, not ad-hoc files/env vars.

Gateway commands that must work while an agent is active must bypass both the base adapter pending-message guard and the gateway runner guard.

## TUI, Dashboard, and Desktop

- `hermes --tui` runs Ink over stdio JSON-RPC to `tui_gateway`. Extend Ink for the main terminal chat experience.
- `hermes dashboard` embeds the real TUI through `hermes_cli/pty_bridge.py` and `/api/pty`; do not rebuild the primary transcript/composer in React.
- `apps/desktop/` is a separate Electron + React + nanostore chat surface talking to `tui_gateway`. Desktop slash-command curation belongs in `apps/desktop/src/lib/desktop-slash-commands.ts`; skill and quick commands must remain discoverable.

## Tools and Toolsets

Before adding a tool, apply the Footprint Ladder. Core tools require both:

1. A `tools/<name>.py` module that registers with `tools.registry.registry` and returns JSON strings.
2. Exposure through `toolsets.py` (`_HERMES_CORE_TOOLS` or a deliberate toolset).

Use `get_hermes_home()` for persistent state and `display_hermes_home()` in user-facing schema/help text. Do not hardcode cross-tool references in schemas; if dynamic cross-references are needed, add them in `get_tool_definitions()`.

Agent-level tools such as todo/memory are intercepted in `run_agent.py` before normal model-tool dispatch.

## Configuration and Dependencies

Configuration:

- Add non-secret settings to `DEFAULT_CONFIG` in `hermes_cli/config.py`.
- Bump `_config_version` only for migrations/transforms, not for simple new keys handled by deep-merge.
- `.env` metadata in `OPTIONAL_ENV_VARS` is for credentials/secrets only.
- Know the loader: CLI (`cli.py::load_cli_config()`), subcommands/setup (`hermes_cli/config.py::load_config()`), gateway direct YAML load (`gateway/run.py` / `gateway/config.py`).

Dependencies:

- PyPI deps need upper bounds (`>=floor,<next_major`; pre-1.0 uses a conservative minor ceiling).
- Git deps/actions pin to commit SHAs; CI-only pip deps use exact pins.
- Run lockfile updates when dependency pins change.

## Plugins, Skills, Curator, Cron, Kanban

Plugins:

- General plugins are discovered by `hermes_cli/plugins.py`; `discover_plugins()` is normally triggered through `model_tools.py` import.
- Memory providers live behind `agent/memory_provider.py` / `agent/memory_manager.py`; new memory backends should be external plugins, not new in-tree provider directories.
- Model providers are plugins under `plugins/model-providers/` with lazy discovery in `providers/__init__.py`.

Skills:

- Skills are procedural memory, not a logbook. Put reusable workflows in skills, not `AGENTS.md`.
- Skill commands are injected as user messages to preserve prompt caching.
- Do not add lazy pagination to skill-loading semantics.

Curator/Cron/Kanban:

- Curator maintains skills in the background; keep critical operational skills pinned when appropriate.
- Cron/scheduled jobs should use the existing scheduler/CLI surfaces.
- Kanban workers must use `kanban_*` tools for board state. Helper scripts that touch Kanban writes must set `HERMES_SANDBOX_MODE=1` unless the live-board write is explicitly audited through the tool call.

## Profiles and Paths

Hermes profiles are independent HERMES_HOME roots. `_apply_profile_override()` in `hermes_cli/main.py` sets `HERMES_HOME` before profile-aware modules import.

Rules:

- Use `get_hermes_home()` for state paths.
- Use `display_hermes_home()` for user-facing output.
- Avoid hardcoded `~/.hermes` / `Path.home() / ".hermes"` except for intentionally HOME-anchored profile-root operations.
- Tests that mock `Path.home()` for profiles should also set `HERMES_HOME`.
- Gateway adapters using unique credentials should acquire/release scoped token locks.

## TypeScript Style

- Prefer small nanostores for shared state; colocate atoms with feature owners.
- Keep route roots thin; avoid monolithic hooks.
- Prefer interfaces for public props/object shapes.
- Extend React primitive props when wrapping components.
- Use table-driven mappings for ids/routes/views.
- Make async UI handler intent explicit: `onClick={() => void save()}`.
- If a callback is pure side effect, use terse void form: `onState={st => void setGatewayState(st)}`.

## Testing Rules

Use the narrowest meaningful gate first:

```bash
scripts/run_tests.sh tests/path_or_test.py::test_name
scripts/run-affected.sh
```

`tests/conftest.py` and the wrapper isolate credentials, HOME/HERMES_HOME, timezone, locale, and subprocess state. Avoid direct `pytest` unless debugging a narrow issue and you understand the isolation tradeoff.

Frontend gate: `scripts/gate-frontend.sh` (lint:control → `tsc -b --noEmit` → vitest → build). Two proven traps it exists to prevent: (1) bare `tsc --noEmit`/`npm run typecheck` are no-ops here — `web/` is a solution config with `files: []`, only `tsc -b` type-checks (masked a real type drift 2026-06-16); (2) hand-rolled pipe chains swallow exit codes — `npx vitest run | tail` reported green on 3 failing tests 2026-07-01 (without pipefail the exit code is tail's). Use `--skip-build` when `web_dist` must not be overwritten (build writes straight into the served assets; check `git status` for foreign dirty `web/` state first).

Do not write change-detector tests for expected-to-change data (model lists, config version literals, enumeration counts). Prefer behavior and invariants: plumbing works, migrations converge to the current version, catalogs have required metadata, mutually-exclusive sets do not overlap.

## Important Pitfalls

- Never `git reset --hard origin/main` in this fork. `origin` is upstream; `main` tracks Piet's fork. Use merge-based sync workflows.
- Do not introduce new `simple_term_menu` usage; prefer `hermes_cli/curses_ui.py`.
- Do not use ANSI erase-to-EOL (`\033[K`) in spinner/display code under prompt_toolkit; use space padding.
- `_last_resolved_tool_names` in `model_tools.py` is process-global and saved/restored around delegate subagent runs.
- Tests must not write to a real `~/.hermes/`.
- When wiring unused/dead code into a live path, E2E test the actual resolution chain with real imports and temp `HERMES_HOME`.
- Squash-merging stale branches can silently revert unrelated fixes; inspect merge diffs for unexpected deletions.

## Source Code Reference

Dependency source is cached via `opensrc`. Use it for internals rather than guessing. Resolve the installed/pinned version from the project before comparing source behavior, and prefer repo/lockfile-aware commands. More examples and package-specific notes are in [`docs/agent-dev-guide.md`](docs/agent-dev-guide.md#source-code-reference).

## More Detail

The full pre-split guide, including detailed command examples and subsystem-specific notes, is in [`docs/agent-dev-guide.md`](docs/agent-dev-guide.md). Keep this startup file compact; move durable deep reference material to that document or to focused skills/docs.

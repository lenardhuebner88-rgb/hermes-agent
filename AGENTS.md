# Hermes Agent — startup development guide

Instructions for assistants working on the `hermes-agent` codebase. Detailed,
slower-moving implementation guidance lives in
[`docs/agent-dev-guide.md`](docs/agent-dev-guide.md); inspect live code and that
guide when a subsystem is in scope. Never give up on the right solution.

## Start and coordinate

- Read the global vision and rules plus this file once per new session/repo.
- Before edit-risk work, use the global `codex-vault-coordination` skill. A
  broad claim overlap is only a signal; stop for concrete same-file work or an
  inseparable subsystem, and record why disjoint-file work may proceed.
- Preserve foreign dirty files. This live checkout is shared and uncommitted
  files can leak into integration gates.
- For isolated or multi-step implementation, use `hermes-worktree-startup`.
- Finish write work with a Codex receipt and proper Check-OUT under the Vault
  convention. Do not commit or push unless the active request explicitly asks.

Canonical collaboration and PlanSpec rules:

- `/home/piet/vault/00-Canon/conventions-gates.md`
- `/home/piet/vault/00-Canon/planspec-taskgraph.md`

## Product architecture

Hermes is one personal agent core exposed through CLI, messaging gateway, TUI,
desktop, scheduled jobs, memory/skills, tools, terminal, browser, and dashboard.
Two properties are load-bearing:

- Per-conversation prompt caching is sacred. Preserve past context, stable
  system prompts, supported compression, and strict role alternation.
- The core is a narrow waist. Put new capability at the least permanent edge
  that solves the real need.

Use this footprint ladder:

1. Extend existing code.
2. Add a CLI command plus skill.
3. Add a service-gated tool with zero footprint when unavailable.
4. Build a plugin.
5. Add an MCP server/catalog entry.
6. Add a core model tool only when the capability is broadly fundamental and
   unreachable through the earlier layers.

Verify both current behavior and original intent before calling an omission a
bug. Avoid speculative extension points, feature-destroying mitigations,
outbound telemetry without opt-in, vendor SaaS in core, and plugins that modify
core files.

## Load-bearing map

```text
run_agent.py          AIAgent and synchronous conversation loop
model_tools.py        tool discovery/dispatch and plugin hooks
toolsets.py           toolset definitions and core exposure
cli.py                classic CLI and slash-command dispatch
hermes_state.py       SessionDB and FTS5
hermes_constants.py   profile-aware persistent paths
hermes_cli/           CLI commands, config, plugins, skins
tools/                built-in tools and terminal backends
gateway/              messaging platforms and routing
plugins/              generic, memory, provider, kanban, observability edges
ui-tui/               Ink terminal UI
apps/desktop/         Electron/React desktop chat surface
tui_gateway/          JSON-RPC backend for TUI/desktop
web/                  dashboard/control frontend
tests/                pytest suite
```

Dependency chain: `tools/registry.py` ← `tools/*.py` ← `model_tools.py` ←
`run_agent.py`/`cli.py`/runners. Trace the real callers before changing shared
symbols. Read `AIAgent`'s actual signature before editing it.

## Integration contracts

- Slash commands originate in `hermes_cli/commands.py`; wire CLI handling in
  `HermesCLI.process_command()` and gateway handling when messaging supports it.
- `hermes --tui` is Ink over stdio JSON-RPC. `hermes dashboard` embeds the real
  TUI through `hermes_cli/pty_bridge.py` and `/api/pty`; do not rebuild that
  transcript/composer in React. Desktop is a separate Electron surface.
- New core tools require registry registration and deliberate exposure through
  `toolsets.py`. Prefer CLI/skill/plugin/MCP first.
- Add non-secret behavior to `DEFAULT_CONFIG` in `hermes_cli/config.py`; reserve
  `.env` metadata for credentials. Know the separate CLI/setup/gateway loaders.
- Use `get_hermes_home()` for persistent paths and `display_hermes_home()` in
  user-facing schemas. Apply profile overrides before profile-aware imports.
- PyPI dependencies need bounded ranges; Git dependencies/actions pin SHAs.
- Memory and model providers belong behind existing interfaces or external
  plugins, not new in-tree provider silos.
- Kanban workers use `kanban_*` tools. Helper scripts that write board state set
  `HERMES_SANDBOX_MODE=1` unless the tool call itself is the audited write.

## Skills and UI workflows

- Skills are procedural memory, not logs. Keep startup rules short and move
  reusable workflows into repo skills.
- For Design Board work, use the repo-local `design-board` router before any
  card/mockup/pin/promotion action.
- For Hermes visual verification, use `hermes-ui-preview` and its canonical
  task-specific references.
- A deprecated-system skill must self-gate as read-only legacy reference; never
  silently present decommissioned behavior as live.

## Verification

Use `hermes-gates` to select and preserve the correct gate. Typical entrypoints:

```bash
source .venv/bin/activate   # or source venv/bin/activate
scripts/run_tests.sh <target>
scripts/run-affected.sh
scripts/gate-frontend.sh
```

Run targeted/affected checks interactively; the full suite is the nightly path.
Use repository wrappers rather than bare pytest. Frontend proof requires
`tsc -b` through the gate; do not trust bare `tsc --noEmit`, no-op typecheck
scripts, or pipe chains that swallow the producer's exit status. Prefer behavior
and invariants over snapshots or counts of expected-to-change catalogs.

## Hard pitfalls

- Never `git reset --hard origin/main`; `origin` is upstream and `main` tracks
  Piet's fork.
- If auto-release rollback leaves the live checkout detached, triage and restore
  `main` before any build. Never build on the detached state.
- Tests must not write to a real `~/.hermes/`. Mocked profiles must set
  `HERMES_HOME` as well as `Path.home()`.
- Do not add `simple_term_menu`; use `hermes_cli/curses_ui.py`.
- Do not use ANSI erase-to-EOL under prompt_toolkit; pad with spaces.
- `_last_resolved_tool_names` is process-global and must be saved/restored around
  delegate subagents.
- Wiring dead code into a live path requires an end-to-end resolution test with
  real imports and temporary `HERMES_HOME`.
- Inspect merge diffs for silent deletion/reversion when integrating stale work.
- Treat load-sensitive `waitFor` flakes as scoped test-timeout problems only
  after reproducing in the relevant loaded gate.

Use `opensrc` from the project for dependency internals at the installed version.
More examples and subsystem detail remain in `docs/agent-dev-guide.md`.

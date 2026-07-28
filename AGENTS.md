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
scripts/run_tests.sh <target>
scripts/run-affected.sh
scripts/gate-frontend.sh
```

### Python test environment

- Canonical outside Nix: `.venv/bin/python`, with locked `all` + `dev` + `messaging`.
- Bootstrap/repair: `uv sync --locked --extra all --extra dev --extra messaging`.
- `venv/` is the managed release runtime; never add dev deps or run tests there.
- A naked agent shell needs neither activation nor Nix; call the wrappers above.
- `no test Python` means: run the exact `uv sync` command printed by the wrapper.
- Never use `python3 -m pytest`; system Python creates optional-dependency phantom errors.
- In the Nix devShell, `HERMES_PYTHON` is the locked fallback when `.venv` is absent.
- Interpreter selection lives once, in `scripts/lib/select_test_python.sh`. Both
  `run_tests.sh` and `collect_check.sh` source it. Do not re-inline it — the two inline
  copies drifted on 2026-07-25 while a comment claimed they matched.

### Test runtime — what actually costs time

Measured against the 2026-07-25 nightly (`~/.hermes/logs/green-gate/*/python.log`, which
records per-file timings — read it before optimising anything):

- The nightly runs **`-j 12` with a 300 s per-file timeout**. The 12 does not come from
  `run_tests.sh` (which sets `HERMES_TEST_WORKERS:-8` because gate runs share the box) — it
  comes from `HERMES_TEST_WORKERS=12` exported in `~/.bashrc` and
  `~/.config/environment.d/50-hermes-limits.conf`. Under that contention a file runs roughly
  5× slower than standalone, which is how a 85 s file hits a 300 s limit.
- **A file over the timeout is SIGKILLed and contributes zero tests** while the summary still
  reads as one failing file. `tests/plugins/test_kanban_dashboard_plugin.py` loses 307 tests
  that way. Check for that class before trusting a green-ish nightly.
- `tests/hermes_cli/conftest.py` builds the kanban DB **once per module** and copies the file
  per test. Do not reintroduce a per-test `kb.init_db()`; it cost 63 % of the runtime of the
  35 files that use the shared `kanban_home` fixture. Note the 35: of the 123 files that
  mention `kanban_home`, 88 define their own override and are untouched by that conftest.

Run targeted/affected checks interactively; the full suite is the nightly path.
Use repository wrappers rather than bare pytest. In worktrees NEVER bare
`python`/`pytest` — there is no venv there; the wrappers resolve the
interpreter (this is the #1 first-stumble for new workers). Frontend proof requires
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
- **New fork code never goes into an upstream-owned file.** Put it in a
  fork-owned module and call it from one line. `hermes_cli/kanban_db.py` is the
  cautionary tale: ~29k fork lines interleaved into upstream's ~9.8k forced the
  2026-07-24 sync to resolve it as *ours*, which silently discarded 12 upstream
  commits while keeping upstream's tests — 42 tests red on arrival. Before
  touching that file, upstream syncs, or the refactor:
  **`docs/refactor/UPSTREAM-STRATEGY.md`**.
- Treat load-sensitive `waitFor` flakes as scoped test-timeout problems only
  after reproducing in the relevant loaded gate.
- **A lane-scope park names files; verify them before believing it.** Five
  consecutive parks on 2026-07-27/28 were false positives that named paths the
  card never touched. A card's real contribution is
  `git diff --name-only $(git merge-base main <branch>)..<branch>`, and
  `branch_name` for a chain slice is the *chain* branch (`kanban/<root>`), not
  `kanban/<task-id>` — guessing it yields an empty diff and a wrong conclusion.
  Four distinct causes existed; all are fixed as of 2026-07-28. **Read the code
  path in the wrong order and you will fix the wrong one — measure this first:**
  `_lane_scope_review_snapshot_diff_spec(conn, task_id, repo_root)`. When it
  returns non-`None`, a review snapshot pins the diff and the *entire* per-task
  attribution is skipped — receipts, orphaned-basis branch, merge-base
  subtraction all become dead code for that completion. For a chain slice the
  snapshot candidate is the shared chain **tip**, so the slice is charged with
  its siblings' commits. That single mechanism produced seven consecutive false
  parks on one card while three earlier fixes looked correct and never ran.
  Never answer a park with another lane-scope fixer before measuring; the second
  bounce means diagnose, not retry.
- **`done` + `MERGED_GREEN` does not prove the code is on `main`.** Verified
  2026-07-28: a card carried `INTEGRATOR_VERIFIED` with a green gate while its
  four files were absent from `main` — the stamp named the *sibling's* branch and
  file list, because `chain_root_id` walks `task_links` upward and resolved a
  foreign, already-finished card as the chain root. Confirm with
  `git merge-base --is-ancestor <card branch> main` (and `git patch-id --stable`
  if a rebase may have reminted it). A `done` card is never revisited by the
  board, so unlanded work there is silent and permanent.

Use `opensrc` from the project for dependency internals at the installed version.
More examples and subsystem detail remain in `docs/agent-dev-guide.md`.

## Code map

Re-measured 2026-07-25 — this supersedes the 2026-07-24 guidance.

- **Architecture / "what connects X→Y"** → `graphify query|path` (`--budget`, `--context call`
  to narrow). `tests/` is excluded from the graph as of 2026-07-25; the reason and the numbers
  are in `.graphifyignore`. Builders never rebuild; Maintainer/timer only.
- **Callers / blast radius / "which tests cover X?"** → `codegraph query|node|explore`. Its old
  >1 MiB blind spot is fixed by a local patch (details in `CLAUDE.md`), so `kanban_db.py` and
  `gateway/run.py` are now indexed correctly. If a CodeGraph update reverts the patch, the
  symptom returns silently — see `CLAUDE.md` before trusting a result on those two files.
- **Never carry a line number over from a doc, a plan, or an earlier answer.** Resolve it when
  you need it. Stale-but-plausible line numbers are the recurring failure mode here: CodeGraph
  served `dispatch_once` 15,427 lines off its real location, and this repo's own docs have
  carried spine line numbers that no longer resolve.

## Working in `hermes_cli/kanban_db.py`

39,727 lines. You cannot read it, and you should not try. Read these three first, in order:

1. **`docs/kanban/LIFECYCLE.md`** — the map: state diagram, transition table, dispatch path
   (tick → worker), landing path, decision order inside `complete_task`, traps. Its 144 symbol
   anchors are mechanically verified by `scripts/check_kanban_lifecycle_anchors.py` (exit 0),
   so its line numbers are the one set in this repo you may trust — and re-run that checker
   after any change that moves symbols.
2. **`docs/refactor/ownership.kanban_db.md`** — answers the question that comes *before* any
   edit: does this symbol belong to the fork or to upstream? Editing an upstream-owned body is
   what created the merge problem in the first place; put new fork code in a fork-owned module
   and call it from one line.
   **Do not quote counts from that file, and do not trust a single tool's count.** Measured
   2026-07-25, three sources disagree about the same file: the doc itself says 733 fork-only
   (it predates the sync), `scripts/refactor/split_module.py --ownership` says 737, and
   `scripts/refactor/upstream_divergence.py` says 740 — they classify symbols differently, and
   none is simply wrong. Re-run the tool that matches your question and cite *which* one, or
   the next reader inherits a number that looks authoritative and matches nothing.
3. **`docs/refactor/UPSTREAM-STRATEGY.md`** — why the file is like this, what was already tried,
   and the two dead ends not to re-run. The modularization plan under `docs/refactor/` is a
   **parked design record, not a work order**; Task 7 is blocked after two reverted attempts.

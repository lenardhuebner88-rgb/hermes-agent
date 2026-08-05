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
- PlanSpec code slices need grounded `scope_files`; missing Strategist grounding
  inserts a scout dependency. Structured acceptance criteria may carry `route`
  for the concrete review/UI location. Intake renders the scope contract into
  the task body; it does not populate `tasks.scope_contract`.
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
- Canonical single-file / targeted run (extra pytest flags pass through; no `--` separator):
  `scripts/run_tests.sh <testpfad> -q -p no:cacheprovider`
- Never pass pytest `--timeout` — `pytest-timeout` is not installed, so the flag aborts with
  `unrecognized arguments` before any test runs. Per-file caps live in the runner (default 300 s,
  SIGKILL of the process tree); tune via `HERMES_TEST_FILE_TIMEOUT=<seconds>` or `--file-timeout`.
- Collection sweep is a different script: `scripts/collect_check.sh -q tests/ 2>&1 | tail -3`
  (want `0 errors`). It runs `pytest --co` in one process. Do not route `--co` through
  `run_tests.sh` — that fans collection out to ~one process per test file.

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

- **`android/` has a gate now — use it, do not rebuild one.**
  `scripts/gate-android.sh <app> [--ui]` where `<app>` is `deck` (default),
  `dictate` or `voice`: Android Lint + JVM unit tests, and `--ui` adds
  instrumented Compose tests. It pipes nothing, the exit code is the truth.
  `--ui` *refuses* for an app with no instrumented tests rather than booting an
  emulator and measuring nothing. Three things it encodes that cost a session
  each to find: `/dev/kvm` needs `sg kvm -c` from any shell older than the group
  change (Piet *is* in `kvm`; only stale sessions are not); `adb devices` keeps
  listing an emulator that is still tearing down, so a device only counts once
  `sys.boot_completed` is `1`; and `local.properties` is git-ignored, so a fresh
  worktree needs it written before Gradle runs.
- **The emulator belongs to `scripts/android-emulator.sh`, one AVD and one port
  per app** (deck 5554, dictate 5560, voice 5562). Do not pick an AVD off the
  floor: until 2026-08-05 the gate took `emulator -list-avds | head -1`, exactly
  one AVD existed, and the deck gate therefore booted the *dictate* AVD — the
  same one the dictate scripts boot. Two boots of one AVD is not two emulators;
  the second loses, and it reads as a flake.
- **Shipping an APK means `assembleRelease`, never `assembleDebug`** — and
  `scripts/release-deck-apk.sh` is the only way to do it. The debug build is
  43.4 MB with `ui-tooling` and `application-debuggable`; release is 34.9 MB.
  A session compared the two, ruled out its own diff with a control build, and
  concluded the *build environment* had drifted. It had not. The script signs
  with the Android debug keystore (`285a89ae…` — any other key makes updates
  refuse to install over the shipped builds) and aborts on a foreign
  certificate, on `debuggable`, or when the APK's internal version disagrees
  with the one being built.
- **`pgrep -c <name>` never matches a name longer than 15 characters.**
  `pgrep -c qemu-system-x86_64` returns 0 with exit 1 whether or not an emulator
  is running — a session used exactly that to "prove" it had stopped one. `-f`
  matches but also matches your own command line. And **`ps` is no better here:
  measured on 2026-08-05, `ps -eo args | rg adb` finds nothing while
  `pgrep -af "adb -L tcp"` returns the running adb server.** Whatever probe you
  use, first show it finding a process you know is running; an unfalsifiable
  zero is not a measurement.
- **`journalctl -g` greps the rendered line, ANSI escapes and all.** Rust
  `tracing` colours output even under systemd, so `acp::tool: tool_call` does not
  exist as a literal and matching it returns zero lines with exit 0 —
  indistinguishable from an idle service. Anchor patterns inside the uncoloured
  message body, never across the `target: ` boundary. For `--user` units the
  agent unit is `_SYSTEMD_USER_UNIT`; `_SYSTEMD_UNIT` is `user@1000.service` on
  every line and silently merges all units into one bucket.
- **A RED probe that does not bite indicts the test, not the probe.** A guard in
  `ThreadActivity` looked covered; the test's event was discarded one line
  earlier, so it asserted nothing. If breaking the code leaves tests green, fix
  the test before trusting the suite.
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
- **`scripts/run-affected.sh HEAD` against a CLEAN worktree runs zero tests and
  exits 0** ("no applicable Python production paths"). The worker-gate stamp then
  carries only `exit_codes: [0]` / `passed: True` — bit-identical to a run over a
  thousand tests. After committing, gate with `HEAD~1` and confirm with
  `git diff --name-only HEAD~1 HEAD` that the expected files are in the diff.
- **`hermes kanban list --status blocked` does not show PlanSpec chains.** They
  run under `tenant: planspec`, so the list reports "no matching tasks" while a
  slice is blocked. Check chains with `hermes kanban show <task_id>`, never by
  the list alone.
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
  Four distinct causes existed by 2026-07-28; the 2026-08-02 guards additionally
  hardened fixer-scope inheritance, stale review-base clamping, zero-test gates,
  and merge-receipt attribution. **Read the code path in the wrong order and you
  will fix the wrong one — measure this first:**
  `_lane_scope_review_snapshot_diff_spec(conn, task_id, repo_root)`. When it
  returns non-`None`, a review snapshot pins the upper-bound diff. Task-local
  receipts may narrow that bound only when attributable single-parent commits
  yield paths; merge commits and empty/unusable receipts mean *unknown*, so the
  full snapshot remains. For a chain slice the snapshot candidate is the shared
  chain **tip**, so explicit `scope_files` plus the bounded fixer allowlist are
  still required to distinguish the slice from sibling commits. The old
  snapshot-short-circuit produced seven consecutive false parks while three
  earlier fixes looked correct and never ran.
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

- `ruff` here does **not** catch dead imports — `pyproject.toml` selects a narrow
  rule set and `F401` is off. After removing a function, `rg` for its imports
  yourself; `All checks passed!` will happily keep an orphan `import` alive.
- Never render a measured value through a formatter that floors below its own
  unit. `derive.ts::fmtDur` does `Math.floor(sec)`, so a measured 250 ms TTFT
  read as `0s` — and TTFT is essentially always sub-second, so the column showed
  zero permanently. Milliseconds go through `fmtMillis` (`StatistikView.tsx`).
- `z.coerce.number().catch(0)` on a cost or token field is a bug, not a default.
  When a read path fails the backend returns an empty `summary` and the schema
  fills every missing field with `0`; "0 Context Tokens" then reads as "no
  activity" instead of "not readable". Canon
  `00-Canon/decisions/2026-07-27-kosten-ssot-im-lesepfad.md` rule 3: unknown
  stays unknown, never `0`. Same for `?? 0` / `.catch(() => 0)` at render sites.
- A legacy/fallback shape must not assert what the old payload never carried.
  `ScorecardView::legacyQuality` set numerator = denominator for review coverage,
  producing a permanent 100 % with a green check where the real figure is ~29 %.
  Fallbacks fire exactly in the deploy-skew window (new `web_dist`, service not
  yet restarted), so nobody sees them in tests.
- `expect(markup).toContain("<word>")` is tautological when the word also appears
  in unconditional copy. A thin-sample test passed on `"dünn"` while the panel
  footer renders "dünne Nenner bleiben sichtbar" — it stayed green with the
  guarded logic deleted. Assert the marker in context plus a negative case.

- The root `.gitignore` rule `data/` is **not path-anchored** (a trailing slash with no
  other slash matches at any depth), so it swallows every directory named `data`
  anywhere in the tree. It kept the Kotlin package `net/hermes/deck/data/` out of four
  commits while the build stayed green locally — the files were on disk. `git status`
  and `git add -A` say nothing. After adding a new subproject, check once with
  `git ls-files <dir> | wc -l` against `ls`, or `git check-ignore -v <file>`, which
  names the offending rule and line. Fix from a deeper, fork-owned `.gitignore`.
- **The dashboard answers 401 for every unauthenticated route, including ones that do
  not exist.** A 401 is therefore no proof that a route is registered — only a logged-in
  call separates 200 from 404. Verify new endpoints through a real login (see
  `scripts/smoke_health_status_auth.py`), and keep a nonexistent path in the same probe
  as the control.

- **Counting through a pipe silently truncates here.** Measured 2026-08-05 in this repo:
  `grep -v … | wc -l` returned **28** where the answer was **1871**, and
  `git log --oneline <range> -- <path> | wc -l` returned **50** where `git rev-list --count`
  returned **829**. Both wrong answers look plausible — no error, no empty output. Redirect to
  a file first and count with `awk`/`wc -l < file`, or use a counting subcommand
  (`git rev-list --count`). Never build a measurement out of a long pipe.
- **`git merge-tree` conflict lines are not one format.** `CONFLICT (content): Merge conflict in
  <path>` ends with the path, but `CONFLICT (modify/delete): … Version X of <path> left in tree.`
  ends with `tree.` — a generic `awk '{print $NF}'` extractor invents a file called `tree.` and
  loses the real one. The *count* of CONFLICT lines stays right, so the error hides in the
  mapping, not the total. Parse per conflict type.
- **For the `android/` apps, `uiautomator dump` is for coordinates, never for a verdict on
  layout.** The dump carries a card's *full* semantic text even where it renders clamped, it
  lists nodes below the fold, and taken too early it shows the state before recomposition —
  on 2026-08-05 three design findings in a row were wrong for exactly these reasons and
  `adb exec-out screencap -p` corrected every one. Judge appearance from the picture. The
  same run also shows why a control probe has to be able to *separate* the hypotheses: a
  search was accepted as filtering because, with a single matching task, filtered and
  unfiltered looked identical — it had never filtered at all.

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
   (tick → worker), landing path, decision order inside `complete_task`, traps. Its 145 symbol
   and banner anchors are mechanically verified by
   `scripts/check_kanban_lifecycle_anchors.py` (exit 0), making it the repo's mechanically
   checked source map — re-run that checker after renaming or removing a top-level symbol or
   exact banner text.
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

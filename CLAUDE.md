# Hermes Agent — Claude Code entry

`AGENTS.md` = the 7 key pitfalls; full depth (architecture + all known pitfalls): `docs/agent-dev-guide.md` (large — read on demand).

## Live checkout (critical)
- Several agent sessions edit this directory in **parallel**. ALWAYS `git status --short` first; leave foreign uncommitted/untracked work untouched.
- `origin` = NousResearch upstream → **NEVER push** there. Push only to `piet-fork`, fast-forward only, never `--force`.

## Upstream merge capability
- Before touching `hermes_cli/kanban_db.py`, an upstream sync, or the refactor: **`docs/refactor/UPSTREAM-STRATEGY.md`** (goal, metrics, two dead ends, next step).
- **Standing rule:** new fork code goes in a fork-owned module, never an upstream-owned file. Resolving a file as *ours* takes upstream's **tests** without its **implementation** — re-measure after every sync with `git diff --stat <old-merge-base> origin/main -- <file>`; `merge-tree` reporting 0 is an artefact, not a pass.

## Worktree sessions (phone/remote)
- Remote sessions spawn in `.claude/worktrees/bridge-cse_*` (branch `worktree-bridge-…`, forked from local HEAD). Finished work returns to the live branch via merge — no direct edits to the live checkout.
- Worktrees get an exclusive `node_modules` tree under `HERMES_WORKTREE_DEPS_ROOT` (never the live checkout's) → run `scripts/gate-frontend.sh` first, it provisions deps into that tree on first call; then iterate via the hoisted root binaries `<wt>/node_modules/.bin/{tsc,vitest}`. **Never** `cd <wt>/web && npm ci` by hand — npm removes the dedicated symlink before reifying and breaks the isolation (the resulting gate error names the recovery) — and **never** `npx tsc/vitest` in a worktree (stub trap `ENOWORKSPACES`). NEVER gate a worktree diff in the live checkout (foreign sessions keep it dirty). Details: skill `hermes-dashboard-dev`.

## Dashboard (primary build target)
- `/control` SPA (FastAPI + React/TS), port **9119** (loopback), reachable via Tailscale Serve `:9443`.
- Binding PlanSpecs (`taskgraph_hints`, `freigabe`, `live_test_depth`) are defined in `/home/piet/vault/00-Canon/planspec-taskgraph.md`; dashboard hub and `hermes plan ingest <planspec.md>` must use that schema.
- Restart: `systemctl --user restart hermes-dashboard.service` (run via systemd, never by hand).
- Deploy: `scripts/deploy_dashboard.sh` — standing grant only on *truly* green gates (with `CONFIRMED=1`), otherwise not. Truth = API payload, not screenshot (the SPA injects its token via `window.__HERMES_SESSION_TOKEN__`; bare loopback curl = 401).
- Auth smoke after a gated deploy: `HERMES_DASHBOARD_URL=https://… HERMES_DASHBOARD_USERNAME=… HERMES_DASHBOARD_PASSWORD=… scripts/smoke_health_status_auth.py --no-prompt` (login cookie → `/api/health-status`; the script logs no passwords, tokens, or cookies).
- Design language is binding → `web/src/control/DESIGN.md` (tokens in `web/src/control/theme.css`, gate-enforced ratchet in `scripts/gate-frontend.sh`).

## Gates (before deploy/push)
- Frontend: `scripts/gate-frontend.sh` (lint:control → `tsc -b --noEmit` → vitest → build). It pipes nothing — the exit code is the truth; never gate freehand with `| tail` (without pipefail that swallows the exit code). `--skip-build` when `web_dist` must not be overwritten (e.g. foreign dirty `web/` state). lint:control = eslint over fork-own code (`src/control` + `vite.config.ts` + `e2e`) — do NOT "clean up" upstream files like `src/App.tsx`; the verifier judges those diff-relative.
- Android (`android/{hermes-deck,hermes-dictate,hermes-voice}`): `scripts/gate-android.sh <app> [--ui]` — Lint + JVM unit tests; `--ui` adds instrumented Compose tests and refuses for an app that has none. The emulator belongs to `scripts/android-emulator.sh` (one AVD + one port **per app**), never to an ad-hoc `emulator -avd` call. Shipping is `scripts/release-deck-apk.sh` only — **release, not debug**, signed with the debug keystore `285a89ae…`; publishing on a green gate is a standing grant. The SDK and the AVDs live on the second disk (`~/Android/Sdk` is a bind mount on `/dev/sda1`); storage is not the constraint, but Gradle's outputs land on `/` — `./gradlew clean` after a build. Project definition: Canon `projects-map.md` → *Android-Apps*; the decisions behind it: `00-Canon/decisions/2026-08-05-android-projektstruktur.md`. Traps: `AGENTS.md` → *Hard pitfalls*.
- Python: `scripts/run_tests.sh` (per-file timeout via `run_tests_parallel.py`, `HERMES_TEST_FILE_TIMEOUT`/`--file-timeout`) + `ruff`.
- **Test scope:** targeted by default — `scripts/run-affected.sh` while building/verifying; before deploy/push one collection sweep (`pytest --co -q tests/`) + affected tests; the **full** suite runs only nightly (`green-gate-heartbeat`). Rule: AGENTS.md → *Test scope* / Canon `conventions-gates.md`. Do NOT have worker and verifier both run the full suite.

## Skills
- `hermes-dashboard-dev` — build tabs/tiles/endpoints (the *what*).
- `hermes-fork-sync` — sync, branches, merge conflicts, dirty `git status` (the *git/state*).

## Dependency source (opensrc)
Read a dependency's internals instead of guessing: `rg "x" $(opensrc path <pkg>)` / `cat $(opensrc path pypi:<pkg>)/…` — real repo source at the version tag, cached globally in `~/.opensrc/` (works without `web/node_modules` in worktrees). Full block: `AGENTS.md`.

## Code map — which navigator actually works here
Overrides the global "CodeGraph first" rule **for this repo**. Re-measured 2026-07-25; both entries below changed that day, so trust this block over anything older.

> **Never copy a line number out of this file, a plan, or a doc into your reasoning.** Both
> navigators and every doc in this repo have been caught serving line numbers that are
> thousands of lines stale while looking perfectly plausible. Resolve the location at the
> moment you need it — `codegraph query <symbol>`, or `ast.parse` + `lineno`. The anchor
> checker (`scripts/check_kanban_lifecycle_anchors.py`) is the only mechanically verified
> source map here: it resolves top-level symbols through the AST and module banner texts by
> exact match; line numbers everywhere else drift silently.

- **`codegraph query|node|explore` now covers the whole repo, including the two big files.**
  Until 2026-07-25 it skipped every file >1 MiB, which silently excluded `hermes_cli/kanban_db.py`
  and `gateway/run.py` — and worse, it kept serving *stale* entries for them from an older,
  smaller revision: `dispatch_once` was reported at `kanban_db.py:14137` when it really sat at
  L29564, 15,427 lines off, with plausible-looking code at the wrong line. That is fixed by a
  **local patch** to `MAX_FILE_SIZE` in `~/.codegraph/versions/v1.5.0/lib/dist/extraction/index.js:113`,
  now `Number(process.env.CODEGRAPH_MAX_FILE_SIZE) || 4 * 1024 * 1024`. Verified after
  `codegraph index` (6,532 files, 172,902 nodes, 21.4 s, no heap trouble): all four kanban spine
  symbols plus `gateway/run.py:24834` match the AST exactly.
  **The patch does not survive a CodeGraph update — re-apply it and re-run `codegraph index`,
  same discipline as the memsearch minipatch.** Backup: `index.js.bak-20260725-maxfilesize`.
  Rollback is `CODEGRAPH_MAX_FILE_SIZE=1048576` or restoring the backup.
- **`graphify query|path`** for "how does X flow / what connects X→Y". Wide queries truncate —
  narrow with `--budget` or `--context call`. Canon: `vault/00-Canon/graphify-playbook.md`.
  **`tests/` is excluded from the graph since 2026-07-25** (see `.graphifyignore` for the
  measurement: tests were 54% of all nodes and 97% of the answers to kanban-symbol queries).
  So graphify answers architecture questions; it can no longer answer "which tests cover X?" —
  use codegraph for that. No worker rebuilds.
- **Use `rg`, not `grep -r`/`find`.** Worktrees are git-excluded, so `rg` sees 1 hit where `grep -r` sees 33 copies.
- Tools live in `/usr/local/bin` (symlinks): `codegraph`, `graphify`, `qmd`, `hermes`. Bare names resolve; the harness shell does **not** read `~/.bashrc`/`~/.profile`, so never rely on those for PATH.

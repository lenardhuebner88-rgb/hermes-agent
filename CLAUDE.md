# Hermes Agent — Claude Code entry

`AGENTS.md` = the 7 key pitfalls; full depth (architecture + all known pitfalls): `docs/agent-dev-guide.md` (large — read on demand).

## Live checkout (critical)
- Several agent sessions edit this directory in **parallel**. ALWAYS `git status --short` first; leave foreign uncommitted/untracked work untouched.
- `origin` = NousResearch upstream → **NEVER push** there. Push only to `piet-fork`, fast-forward only, never `--force`.

## Worktree sessions (phone/remote)
- Remote sessions spawn in `.claude/worktrees/bridge-cse_*` (branch `worktree-bridge-…`, forked from local HEAD). Finished work returns to the live branch via merge — no direct edits to the live checkout.
- Worktrees start **without** `web/node_modules` → `cd <wt>/web && npm ci` (safe; only symlink/install against the LIVE web is forbidden), then run gates via the hoisted root binaries `<wt>/node_modules/.bin/{tsc,vitest}` — **never** `npx tsc/vitest` in a worktree (stub trap `ENOWORKSPACES`). NEVER gate a worktree diff in the live checkout (foreign sessions keep it dirty). Details: skill `hermes-dashboard-dev`.

## Dashboard (primary build target)
- `/control` SPA (FastAPI + React/TS), port **9119** (loopback), reachable via Tailscale Serve `:9443`.
- Binding PlanSpecs (`taskgraph_hints`, `freigabe`, `live_test_depth`) are defined in `/home/piet/vault/00-Canon/planspec-taskgraph.md`; dashboard hub and `hermes plan ingest <planspec.md>` must use that schema.
- Restart: `systemctl --user restart hermes-dashboard.service` (run via systemd, never by hand).
- Deploy: `scripts/deploy_dashboard.sh` — standing grant only on *truly* green gates (with `CONFIRMED=1`), otherwise not. Truth = API payload, not screenshot (the SPA injects its token via `window.__HERMES_SESSION_TOKEN__`; bare loopback curl = 401).
- Auth smoke after a gated deploy: `HERMES_DASHBOARD_URL=https://… HERMES_DASHBOARD_USERNAME=… HERMES_DASHBOARD_PASSWORD=… scripts/smoke_health_status_auth.py --no-prompt` (login cookie → `/api/health-status`; the script logs no passwords, tokens, or cookies).
- Design language is binding → `web/src/control/DESIGN.md` (tokens in `web/src/control/theme.css`, gate-enforced ratchet in `scripts/gate-frontend.sh`).

## Gates (before deploy/push)
- Frontend: `scripts/gate-frontend.sh` (lint:control → `tsc -b --noEmit` → vitest → build). It pipes nothing — the exit code is the truth; never gate freehand with `| tail` (without pipefail that swallows the exit code). `--skip-build` when `web_dist` must not be overwritten (e.g. foreign dirty `web/` state). lint:control = eslint over fork-own code (`src/control` + `vite.config.ts` + `e2e`) — do NOT "clean up" upstream files like `src/App.tsx`; the verifier judges those diff-relative.
- Python: `scripts/run_tests.sh` (per-file timeout via `run_tests_parallel.py`, `HERMES_TEST_FILE_TIMEOUT`/`--file-timeout`) + `ruff`.
- **Test scope:** targeted by default — `scripts/run-affected.sh` while building/verifying; before deploy/push one collection sweep (`pytest --co -q tests/`) + affected tests; the **full** suite runs only nightly (`green-gate-heartbeat`). Rule: AGENTS.md → *Test scope* / Canon `conventions-gates.md`. Do NOT have worker and verifier both run the full suite.

## Skills
- `hermes-dashboard-dev` — build tabs/tiles/endpoints (the *what*).
- `hermes-fork-sync` — sync, branches, merge conflicts, dirty `git status` (the *git/state*).

## Dependency source (opensrc)
Read a dependency's internals instead of guessing: `rg "x" $(opensrc path <pkg>)` / `cat $(opensrc path pypi:<pkg>)/…` — real repo source at the version tag, cached globally in `~/.opensrc/` (works without `web/node_modules` in worktrees). Full block: `AGENTS.md`.

## Code map (graphify)
Prefer `graphify query|path` for architecture/callers when `graphify-out/` exists. Canon: `vault/00-Canon/graphify-playbook.md`. No worker rebuilds.

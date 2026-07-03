# Hermes Agent — Arbeits-Einstieg für Claude Code

Schlanker Auto-Load-Einstieg. Tiefe Architektur + die 9 Known Pitfalls stehen in
`AGENTS.md` (groß — nur bei Bedarf lesen).

## Live-Checkout (kritisch)
- Mehrere Agent-Sessions editieren dieses Verzeichnis **parallel**. IMMER zuerst
  `git status --short`; fremde uncommittete/untracked Arbeit unangetastet lassen.
- `origin` = NousResearch-Upstream → **NIE pushen**. Push nur auf `piet-fork`,
  nur fast-forward, nie `--force`.

## Worktree-Sessions (Handy/Remote)
- Remote-Sessions spawnen in `.claude/worktrees/bridge-cse_*` (Branch `worktree-bridge-…`,
  abgezweigt von lokalem HEAD). Fertige Arbeit per Merge zurück auf den Live-Branch —
  kein Direkt-Edit am Live-Checkout.
- Worktrees haben anfangs **kein** `web/node_modules` → im Worktree `cd <wt>/web && npm ci`
  (sicher; verboten ist nur Symlink/Install gegen das LIVE-web), dann Gates über die
  gehoisteten Root-Binaries `<wt>/node_modules/.bin/{tsc,vitest}` fahren — **nie** `npx
  tsc/vitest` im Worktree (Stub-Trap `ENOWORKSPACES`). NIE einen Worktree-Diff im Live-
  Checkout gaten (fremde Sessions halten ihn dirty). Details: Skill `hermes-dashboard-dev`.

## Dashboard (Haupt-Bauziel)
- `/control`-SPA (FastAPI + React/TS), Port **9119** (loopback), via Tailscale Serve
  `:9443` erreichbar.
- Binding PlanSpecs (`taskgraph_hints`, `freigabe`, `live_test_depth`) sind in
  `/home/piet/vault/00-Canon/planspec-taskgraph.md` definiert; Dashboard-Hub und
  `hermes plan ingest <planspec.md>` müssen dieses Schema nutzen.
- Neustart: `systemctl --user restart hermes-dashboard.service` (über systemd betreiben,
  nicht von Hand).
- Deploy: `scripts/deploy_dashboard.sh` — Standing Grant bei *wirklich* grünen Gates
  (mit `CONFIRMED=1`), sonst nicht. Wahrheit = API-Payload, nicht Screenshot (die SPA
  injiziert ihr Token via `window.__HERMES_SESSION_TOKEN__`; bare Loopback-curl = 401).
- Auth-Smoke nach gated Deploy: `HERMES_DASHBOARD_URL=https://… HERMES_DASHBOARD_USERNAME=… HERMES_DASHBOARD_PASSWORD=… scripts/smoke_health_status_auth.py --no-prompt`
  prüft Login-Cookie → `/api/health-status`; Script loggt keine Passwörter, Tokens oder Cookies.

## Gates (vor Deploy/Push)
- Frontend: `scripts/gate-frontend.sh` (lint:control → tsc -b --noEmit → vitest → build; pipet
  nichts, Exit-Code ist die Wahrheit — nie freihändig mit `| tail` gaten, das schluckt ohne
  pipefail den Exit-Code. `--skip-build`, wenn `web_dist` nicht überschrieben werden darf,
  z. B. bei fremdem dirty `web/`-Stand.)
  (lint:control = eslint über fork-eigenen Code `src/control` + `vite.config.ts` + `e2e` —
  Upstream-Dateien wie `src/App.tsx` NICHT mit-aufräumen, dort urteilt der Verifier diff-relativ)
- Python: `scripts/run_tests.sh` (Per-Datei-Timeout via `run_tests_parallel.py`,
  `HERMES_TEST_FILE_TIMEOUT`/`--file-timeout`) + `ruff`
- **Testumfang:** targeted by default — `scripts/run-affected.sh` beim
  Bauen/Verifizieren; vor Deploy/Push einmal Collection-Sweep (`pytest --co -q tests/`) + betroffene
  Tests; die **komplette** Suite läuft nur nachts (`green-gate-heartbeat`). Regel: AGENTS.md → *Test
  scope* / Canon `conventions-gates.md`. NICHT Worker und Verifier beide die Vollsuite fahren lassen.

## Skills
- `hermes-dashboard-dev` — Tabs/Kacheln/Endpoints bauen (das *Was*).
- `hermes-fork-sync` — Sync, Branches, Merge-Konflikte, dirty `git status` (das *Git/State*).

## Dependency-Source lesen (opensrc)
- Internals einer Dependency lesen statt raten: `rg "x" $(opensrc path <pkg>)` /
  `cat $(opensrc path pypi:<pkg>)/...` — echte Repo-Source am Versions-Tag, global in
  `~/.opensrc/` (funktioniert auch ohne `web/node_modules` im Worktree). Voller Block in `AGENTS.md`.

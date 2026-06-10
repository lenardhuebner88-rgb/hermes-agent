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
- Worktrees haben **kein** `web/node_modules` → Frontend-Gates/Builds im Live-Checkout
  (`~/.hermes/hermes-agent/web`) über `.bin/` laufen lassen, nicht via npx im Worktree.

## Dashboard (Haupt-Bauziel)
- `/control`-SPA (FastAPI + React/TS), Port **9119** (loopback), via Tailscale Serve
  `:9443` erreichbar.
- Neustart: `systemctl --user restart hermes-dashboard.service` (über systemd betreiben,
  nicht von Hand).
- Deploy: `scripts/deploy_dashboard.sh` — Standing Grant bei *wirklich* grünen Gates
  (mit `CONFIRMED=1`), sonst nicht. Wahrheit = API-Payload, nicht Screenshot (die SPA
  injiziert ihr Token via `window.__HERMES_SESSION_TOKEN__`; bare Loopback-curl = 401).

## Gates (vor Deploy/Push)
- Frontend: `cd web && npx tsc --noEmit && npx vitest run && npm run build`
- Python: `scripts/run_tests.sh` (mit pytest-timeout) + `ruff`

## Skills
- `hermes-dashboard-dev` — Tabs/Kacheln/Endpoints bauen (das *Was*).
- `hermes-fork-sync` — Sync, Branches, Merge-Konflikte, dirty `git status` (das *Git/State*).

## Dependency-Source lesen (opensrc)
- Internals einer Dependency lesen statt raten: `rg "x" $(opensrc path <pkg>)` /
  `cat $(opensrc path pypi:<pkg>)/...` — echte Repo-Source am Versions-Tag, global in
  `~/.opensrc/` (funktioniert auch ohne `web/node_modules` im Worktree). Voller Block in `AGENTS.md`.

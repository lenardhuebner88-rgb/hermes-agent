---
name: hermes-dashboard-dev
description: 'Primary build target - das /control SPA (FastAPI + React/TS, Port 9119) in ~/.hermes/hermes-agent. Nutze fuer Tabs/Kacheln/Panels/Views bauen, aendern, debuggen; FastAPI-Endpoints ans Frontend haengen; Polling/Refresh; stale/leere Panels fixen. Deutsch zaehlt ("bau einen Tab ins /control panel", "neue Kachel", "zeig die Worker im Dashboard"). NICHT fuer standalone Dashboards, die FO-App :3000 (family-organizer-dev), Backend ohne UI oder generische React/Vite-Fragen.'
---

# Hermes Control dashboard — architecture & how to add a slice

The operator builds **only the dashboard** (everything else runs via Hermes) and is **not a strong
coder** — keep changes minimal, follow the existing patterns exactly, explain in plain language. The
dashboard is part of a fork that syncs with NousResearch upstream, so **match house style** to avoid
merge pain. (OpenClaw was shut down 2026-06-01 — it is no longer a tab/data source; ignore any older
OpenClaw/Mission-Control references.)

## Where things live

```
hermes_cli/web_server.py          FastAPI app, SPA mount, plugin route loader (port 9119)
hermes_cli/autoresearch_view.py   register_autoresearch_routes(app) — example backend slice
web/src/control/
  ControlPage.tsx                 route → view wiring (activeFromPath + tabPath record)
  components/ControlShell.tsx     ControlTab type (the tab union) + the tab nav chrome
  views/                          ONE component per tab (OverviewView, HermesFleet, AutoresearchView, …)
  hooks/useControlData.ts         data layer: usePolling + fetchJSON + parseOrThrow
  lib/schemas.ts                  zod schemas + parseOrThrow (runtime validation)
  lib/types.ts                    TypeScript types
  lib/derive.ts, tones.ts, tokens.ts   pure helpers + design tokens
  i18n/de.ts                      German UI labels
  *.test.ts                       colocated vitest unit tests for lib logic
```

**Current tabs** (`ControlTab` union in `ControlShell.tsx` — check there, the list grows):
`overview, inbox, pulse, workstreams, agentTerminals, flow, ketten, statistik, autoresearch,
backlog, orchestrator, crons, lanes, pressure, ops, research, bibliothek, schmiede, stratege,
loops` — `inbox` is the `/control` landing. Mobile bottom-nav is the separate `mobileTabs`
filter + a 6-column grid in `ControlShell.tsx`; adding a tab there needs a short `mobileLabel`.

Backend endpoints are reached two ways: registered directly (e.g. `register_autoresearch_routes(app)`
→ `/api/autoresearch/status`) or via a **plugin** exposing a FastAPI `router`, auto-mounted under
`/api/plugins/<plugin-name>` by `_mount_plugin_api_routes()`. Existing control data comes from
`/api/autoresearch/*`, `/api/plugins/kanban/workers/active`, `/api/plugins/kanban/runs/<id>/inspect`.

## The data layer pattern (copy this, don't invent)

Every view gets its data from a hook in `useControlData.ts` built on `usePolling`:
```ts
export function useThing() {
  return usePolling<ThingResponse>(
    async () => parseOrThrow(ThingResponseSchema, await fetchJSON<unknown>("/api/.../thing"), "thing"),
    5000,   // poll interval ms
  );
}
```
Three load-bearing details:
- **`parseOrThrow(Schema, data, label)`** validates the backend response against a zod schema
  (`lib/schemas.ts`) at runtime → a backend shape change fails loudly with a labelled error instead of
  corrupting the UI.
- **`fetchJSON`** injects the session token + handles loopback vs gated auth. For an endpoint whose
  **401 is expected** (loopback auth probe), pass `{ skipStaleTokenReload: true }` as the 3rd arg or
  the SPA reload-loops. (This is the recurring `web/src/lib/api.ts` upstream-merge conflict — keep our
  `skipStaleTokenReload`/`opts` naming.)
- **`usePolling` pauses when `document.hidden`** — by design. A background/MCP tab stops polling, so a
  panel can look empty/stale though the endpoint is fine. Don't "fix" it; foreground the tab to test.
  See `[[feedback_hermes_control_dashboard_audit]]`.

## Adding a new slice/tab — the 7 steps

(The current K-series tabs each followed exactly this; copy a recent one as the template.)

1. **Backend route** returning JSON — inside a `register_*_routes` function or a plugin `router`. For a
   proxy keep it **read-only** and defensive (timeout, error→structured JSON, never expose write paths).
2. **zod schema + type** — `ThingResponseSchema` in `lib/schemas.ts` + the type in `lib/types.ts`.
3. **Data hook** — `useThing()` in `hooks/useControlData.ts` using the `usePolling`+`parseOrThrow` pattern.
4. **View component** — `views/ThingView.tsx`. Reuse `components/atoms.tsx`, tones, tokens; don't add a
   new style system.
5. **Tab wiring in `ControlPage.tsx`** — add to `activeFromPath`, the `tabPath` record, and a
   `<Route path="thing" element={<ThingView .../>} />`.
6. **Tab type + nav** — add the value to `ControlTab` in `ControlShell.tsx` so the nav renders it.
7. **i18n** — add the label(s) to `i18n/de.ts`.

Then a colocated `*.test.ts` for any non-trivial `lib/` logic (vitest).

## Build & verify (the sprint gates)

**Where are you?** The commands differ — getting this wrong costs an hour (proven 3× in one night):

**A. Live checkout (`~/.hermes/hermes-agent`), tree clean:**
```bash
scripts/gate-frontend.sh                 # ONE call: lint:control → tsc -b → vitest → build.
                                         # Exists because freehand `vitest | tail` swallowed a red
                                         # exit (2026-07-01). --skip-build if web_dist must survive.
```
Or the steps by hand:
```bash
cd web
npm run lint:control
../node_modules/.bin/tsc -b --noEmit --force   # binaries are HOISTED to the repo-root node_modules;
                                               # --force: stale .tsbuildinfo yields false-green (belegt 2026-06-16/21)
../node_modules/.bin/vitest run          # ALWAYS the full suite — tests pin cross-file source
npm run build                            # only when web_dist may be overwritten (deploy path)
```

**B. Bridge worktree (`.claude/worktrees/bridge-*`), no node_modules yet:**
```bash
cd <worktree>/web && npm ci              # IN the worktree — safe and standard.
                                         # (The forbidden thing is symlinking/installing
                                         #  against the LIVE web/node_modules — orch-iso trap.)
npm run lint:control
../node_modules/.bin/tsc -b --noEmit --force   # npm ci hoists to the WORKTREE root node_modules
../node_modules/.bin/vitest run          # full suite, same reason
```
Python gates in a worktree (no venv there — use the live venv, it is named `venv`, NOT `.venv`):
```bash
cd <worktree>
PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest <tests> -q
# PYTHONPATH wins over the editable install → tests import the WORKTREE copy
# (verify with: python -c 'import hermes_cli; print(hermes_cli.__file__)')
/home/piet/.hermes/hermes-agent/venv/bin/ruff check <files>
```
- **`npx` is a stub trap in worktrees**: `npx tsc` yields `ENOWORKSPACES` or a wrong global
  tsc that "typechecks" nothing. Never `npx tsc`/`npx vitest` here — use the `.bin` paths.
- New devDependency in `web/package.json`? After `npm ci` in the worktree it lands in the
  **worktree root** `node_modules` (workspace hoisting) — gates just work; no live install.
- **Never gate a worktree diff inside the live checkout** (foreign sessions keep it dirty;
  results would mix your diff with their WIP). Gate where the code is.

A browser is **not** needed to land a slice. Visual check (chromium-shot Snap-trap, auth-empty
caveat, Tailscale/phone, restart-via-Hermes): **`references/visual-check.md`**.

## Guardrails
- Match the existing patterns (polling, parseOrThrow, tab wiring) — a slice that skips validation or
  rolls its own fetch is inconsistent and merge-fragile.
- `web/src/lib/api.ts` is **shared with upstream and conflict-prone** — touch it minimally, keep our naming.
- One-operator tool: don't add auth/access-control layers the operator didn't ask for (`[[feedback_1man_no_overbuild]]`).
- Don't commit generated artifacts (`.hermes/` is gitignored). To sync/push dashboard work cleanly, use `[[hermes-fork-sync]]`.

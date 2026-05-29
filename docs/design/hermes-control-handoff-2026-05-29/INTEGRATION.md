# Hermes Control — Integration in die echte React-App

> Begleitdokument zum Design-Handoff (`README.md` + `react-scaffold/`). Hält fest,
> **wie** das Handoff-Design in die bestehende `web/`-App eingebaut wird, welche
> Entscheidungen gelockt sind und wo der Handoff-Stack vom echten Stack abweicht.
> Status: **Design-Phase, noch nicht gebaut** (2026-05-29).

## Build-Modus (gelockt)
Claude/Codex bauen **nur das Dashboard**. Betrieb + Autoresearch-Inhalt (A2 MiniMax-
Schreiber) laufen über Hermes. Siehe Memory `project_unified_dashboard_arch`.

## Gelockte Entscheidungen
- **Architektur „eine App, drei Stufen"** (A+B+C): A = luftiges Bottom-Tab-Layout
  (mobil-Default), B = dichtere Rail/Cockpit-Ansicht (auto ab `lg` **oder** Pin),
  C = ⌘K-Command-Palette additiv darüber. Eine Codebasis, kein Drei-Apps-Bau.
- **Integration = gekapselter Bereich** unter einer eigenen Route (`/control`).
  Eigene Shell (Bottom-Tab/Rail/⌘K) NUR in diesem Bereich. Die bestehende
  Sidebar-App (Sessions/Models/Logs/Cron/…) bleibt unangetastet als Fallback.
  Sekundär-Nav (Sessions/Kanban/Modelle/Logs/Cron/Skills/Config) verlinkt auf die
  BESTEHENDEN Seiten — kein Doppelbau. Voll reversibel.
- **Dichte:** auto ab `lg` + Pin (localStorage). Entspricht `useDensity` aus dem Scaffold.
- **Settings-Persistenz:** localStorage (Ein-Betreiber).
- **code-mode-Vorschläge:** anzeigen mit Warnfarben-Badge + echtem Diff, **Apply
  gesperrt** mit Hinweis „kommt mit Test-Suite-Gate (A3)". Skip bleibt möglich.
- **vitest** wird nachgezogen (für `derive.test.ts`, 90-s-Stuck-Schwelle etc.).

## Klärpunkt-Abgleich (die 4 markierten offenen Punkte des Handoffs)
| Punkt | Auflösung |
|---|---|
| **Diff-Format** | A1-Backend liefert **Unified-Diff-String** in `diff_before_after`. Scaffold-`diff.ts/toDiffLines()` frisst String *oder* Array → Konvertierung an der fetch-Grenze. **Kein Backend-Change.** |
| **Code-Gate-Feld** | A1-Apply antwortet bei `mode:'code'` mit `{ok:false, gated:"test-suite (A3)"}`. UI sperrt Apply + zeigt den Hinweis. Sobald A3 das Gate baut, liefert das Backend „Tests grün". |
| **Dichte** | auto ab `lg` + Pin (siehe oben). |
| **Settings-Persistenz** | localStorage (siehe oben). |

## Stack-Abweichung Handoff ↔ echte App
Der Handoff nimmt **Next.js 15 / SWR / shadcn-Radix / framer-motion** an. Die echte
App `web/` ist **Vite 7 + react-router 7 + `@nous-research/ui` 0.16 + `motion` v12**.
Konsequente Anpassungen:

| Handoff nimmt an | echte `web/` | Anpassung beim Bau |
|---|---|---|
| Next App-Router | **Vite + react-router 7** | Routen via `BUILTIN_ROUTES_CORE`/`BUILTIN_NAV_REST` in `web/src/App.tsx`; eigene `/control`-Sub-Routen |
| SWR (`useHermesData.ts`) | keins | eigener Poll-Hook über **`fetchJSON`** aus `web/src/lib/api.ts` (Base-Path + Session-Token-Header!) statt nacktem `fetch` |
| shadcn/Radix | **`@nous-research/ui`** | nous-UI (`Button`, `Segmented`, `Badge`, `Switch`, `Select`…), Import `@nous-research/ui/ui/components/<x>`. ⌘K-Palette selbst bauen (nous hat keine) |
| framer-motion | **`motion`** (=framer v12, vorhanden) | `motion`-Import; Animations-Warnung des Handoffs beachten (kein opacity-Gate) |
| eigene globale Tokens (`tokens.css`) | nous-Theme-System (teal, `web/src/themes/`) | Hermes-Control-Tokens **gescopet** unter `[data-control]`/Wrapper, damit der Rest der App nicht umgefärbt wird; **NICHT** die globalen nous-Theme-Variablen überschreiben |
| `npx msw init` + Storybook | — | MSW optional für isolierten UI-Bau; Primär gegen das echte Backend (läuft lokal auf 9119) |

## Was 1:1 aus dem Scaffold übernommen wird (framework-neutral)
`lib/derive.ts` (+ `derive.test.ts`), `lib/diff.ts`, `lib/tones.ts`, `lib/tokens.ts`,
`lib/keymap.ts`, `lib/types.ts` (leicht an A1-Payload angepasst: Diff = String),
`i18n/de.ts`, `hooks/useDensity.ts`. Diese kommen nahezu copy-paste nach
`web/src/control/…`.

## Was neu/angepasst gebaut wird
- `hooks/useControlData.ts` — Poll-Hooks über `fetchJSON` + zod-Validierung (ersetzt
  das SWR-`useHermesData.ts`).
- Atome/Karten/Shells/Views als nous-UI-Komponenten (Props-Verträge aus
  `react-scaffold/src/components/contracts.ts`).
- `<CommandPalette>` (⌘K) selbst gebaut.
- Backend-Erweiterung später: `/api/openclaw/agents` Read-only-Proxy (Sprint B-Teil),
  Worker-Routen existieren bereits (`/api/plugins/kanban/workers/active`,
  `/runs/{id}/inspect`).

## Andock-Punkte in `web/`
- Routing/Nav: `web/src/App.tsx` (`BUILTIN_ROUTES_CORE`, `BUILTIN_NAV_REST`, `buildRoutes`).
- Entry: `web/src/main.tsx` (BrowserRouter + Provider).
- API-Grenze: `web/src/lib/api.ts` (`fetchJSON`, `HERMES_BASE_PATH`, Session-Token).
- Theme: `web/src/themes/` (nous-Registry; Control-Tokens gescopet daneben).
- i18n: `web/src/i18n/` (für den Sidebar-Eintrag „Control").

## Sprint-Schnitt (gelockt 2026-05-29)
B0 Fundament → **B1 Autoresearch (zuerst)** → B2 Hermes-Worker → B4 Übersicht →
B5 Dichte/⌘K. **B3 OpenClaw-Worker zurückgestellt** (braucht Read-only-Proxy →
eigener Folge-Slice). Voller Schnitt + DoD je Slice: Vault-Plan
`03-Agents/Hermes/plans/unified-dashboard-sprint-b-plan-2026-05-29.md`.

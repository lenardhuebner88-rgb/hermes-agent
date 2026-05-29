# Hermes Control — React-Scaffold

Sauberes, typsicheres Fundament für die React-Umsetzung. **Kein UI** — bewusst.
Hier liegt die *unstrittige* Schicht (Verträge, Logik, Daten, Mocks, Hooks), damit die
eigentliche Komponenten-Arbeit auf festem Boden steht und exakt unserer Logik folgt.

> Bezug zur Empfehlung („eine App, drei Stufen"): **A** (luftig) ist die Basis, **B**
> (kompakt/Cockpit) ist dieselbe App in höherer Dichte (`useDensity`), **C** (⌘K) ist die
> Command-Palette als Beschleuniger. Dieses Scaffold ist dichte- und richtungsneutral —
> es bedient alle drei.

## Dateibaum

```
src/
├── lib/
│   ├── types.ts        Domänen-Typen + Enums (API-Verträge 1:1)
│   ├── schemas.ts      zod-Schemas + parseOrThrow() für die fetch-Grenze
│   ├── derive.ts       REINE Logik: workerHealth, buildOverview, fmtAge/Dur/MB/Clock
│   ├── derive.test.ts  vitest — pinnt Schwellen (90s stuck) & Formatierung
│   ├── tones.ts        Ton→Klassen ("border/20 bg/10 text-200"), Labels, Agentenfarben
│   ├── tokens.ts       rohe Token-Werte für JS (LED-Glow, recharts, inline-style)
│   ├── diff.ts         Unified-Diff → Zeilenmodell (+ Zeilennummern für B)
│   └── keymap.ts       Tastatur-/A11y-Karte (⌘K, J/K, A/S)
├── data/
│   └── fixtures.ts     echte Dummy-Daten als typisiertes ESM (Quelle: Prototyp)
├── mocks/
│   ├── handlers.ts     MSW v2 — alle Endpunkte, apply/skip mutieren In-Memory
│   ├── browser.ts      setupWorker (Dev)
│   └── sse.ts          SSE-Simulator (Heartbeat „atmet") + Prod-Skizze
├── hooks/
│   ├── useDensity.ts   A↔B-Stufe: gespeicherte Präferenz > Breakpoint-Default
│   └── useHermesData.ts SWR-Hooks + optimistisches Apply/Skip/AllApply
├── components/
│   └── contracts.ts    Prop-Interfaces der geteilten Bausteine (keine Impl.)
├── i18n/
│   └── de.ts           deutscher String-Katalog (Operator-Tonalität)
└── styles/
    ├── theme.css       Tailwind-4-Einstieg (@import tailwindcss + tokens + @theme)
    └── tokens.css      vollständige Token-Schicht (CSS-Variablen, Dark-Default)
```

## Verdrahtung (Reihenfolge)

1. **Styles:** in `app/globals.css` ganz oben `@import "../styles/theme.css";`. `<html>` startet
   ohne `data-theme` (Dark-Default); Light = `data-theme="light"`.
2. **Mocks (Dev):** einmalig `npx msw init public/ --save`, dann beim App-Start
   `if (dev) { (await import('@/mocks/browser')).startMocks(); }`.
3. **Daten:** Views nutzen `useHermesWorkers()`, `useOpenClawAgents()`,
   `useAutoresearchStatus()`, `useProposals()`. Alle validieren per zod.
4. **Ableitungen:** in den Karten `workerHealth(w, now)` / `buildOverview(...)` aufrufen —
   **nie** Status-Logik in der Komponente neu erfinden.
5. **Dichte:** `const { density, setDensity } = useDensity();` → `<ShellAiry>` vs.
   `<ShellCompact>`; Views bekommen `density` via Props/Context.
6. **Command-Palette:** global mounten, `⌘K` aus `KEYMAP.global.openCommandPalette`.

## Was bewusst NICHT hier ist
- Die React-Komponenten selbst (Karten, Shells, Palette) — die gehören in eure Codebasis
  mit euren shadcn/Radix-Mustern. `components/contracts.ts` gibt die Props vor; die HTML-
  Prototypen im Eltern-Ordner (`../richtung-*.html`) sind die visuelle Referenz.
- Echte SSE-/Auth-Anbindung (Tailscale) — Endpunkt-Skizze steht in `mocks/sse.ts`.

## Offene Punkte (vor dem Backend-Anschluss klären)
- **Diff-Format** der echten API: fertiges Zeilen-Array oder roher Unified-Diff?
  (`diff.ts` kann beides — `toDiffLines()`.)
- **Code-Gate:** Welches Feld/Status signalisiert „Test-Suite grün" für `mode:'code'`?
- **Dichte:** automatisch nach Breakpoint, manueller Tweak, oder beides? (Hook kann beides.)
- **Settings-Persistenz:** localStorage (aktuell) oder serverseitig pro Betreiber?

Siehe die ausführliche Spezifikation in `../README.md` (Screens, Tokens, Interaktionen,
Verhalten) und die HTML-Referenzen + Screenshots im Eltern-Ordner.

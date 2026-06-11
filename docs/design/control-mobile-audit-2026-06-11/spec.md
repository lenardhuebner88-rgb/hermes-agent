# Control-Dashboard — Mobile-/Design-Audit & Umsetzungs-Spec

**Datum:** 2026-06-11 · **Autor:** Claude (Orchestrator-Session, Audit) · **Status:** M1+M2+M3 umgesetzt
(2026-06-11, Audit-Session selbst; M1-Probe 8/8 + M3-Probe 13/13 PASS, Gates grün; M3 = Variante A
nach Piets Go). **Offen: M4–M6.** Gotcha für Folge-Slices: Portale an `document.body` brauchen einen
`<div data-control className="contents">`-Wrapper — außerhalb des `[data-control]`-Scopes lösen die
`--hc-*`-Tokens nicht auf, und direkt am Element setzt `[data-control]` min-height/background.
**Anlass:** Piet konnte auf dem Handy im „Neues Epic“-Sheet den Submit-Button nicht erreichen (Screenshot 17:08). Daraus wurde ein voller Mobile-/Design-Audit des `/control`-Dashboards.

Diese Spec ist die Arbeitsgrundlage für eine **frische Umsetzungs-Session**. Sie enthält alle Funde
(live verifiziert + Code-Audit), das Top-Nav-Redesign und einen Slice-Plan mit Akzeptanzkriterien.

---

## Methodik / Reproduktion

- **Live-Probe** mit Playwright (Pixel-5-Profil, 393×727 Visual-Viewport) gegen `http://127.0.0.1:9119`:
  `probe.mjs` in diesem Ordner. Ausführen aus einem Checkout mit Playwright in `node_modules`
  (z. B. `cd ~/projects/family-organizer && cp <hier>/probe.mjs .p.mjs && node .p.mjs && rm .p.mjs`).
  Screenshots landen unter `/tmp/hc-probe-*.png`.
- **Statische Mobile-Shots:** `~/bin/chromium-shot --screenshot=/tmp/x.png --window-size=390,844 --virtual-time-budget=12000 <url>`.
- **Code-Audit** über `web/src/control/` (Stacking-Contexts, Overlays, Touch-Targets, Viewport-Einheiten).
- Konsole/Netzwerk über alle Haupt-Routen: **keine Fehler** (loopback). Kein horizontaler Page-Overflow.

---

## Funde

### P0 — Blocker (täglicher Mobile-Flow kaputt)

**F1 · Hero-Stacking-Trap: Sheets im Flow-Board sind unbedienbar.**
`.hc-hero` hat `isolation: isolate` + `overflow: hidden` (`styles/control-tokens.css:237-249`). Die Sheets
`EpicCreate.tsx:37` und `FlowCapture.tsx:101` (`fixed inset-0 z-50`) werden im Hero-`action`-Slot gerendert
(`views/FlowView.tsx:~1119`) und sind damit im Hero-Stacking-Context **gefangen**: ihr `z-50` zählt nur
innerhalb des Heros. Folgen, live bewiesen (probe.mjs):
- Submit-Buttons beider Sheets liegen **hinter der Bottom-Nav** (`elementFromPoint` über dem Button trifft `BUTTON.hc-tab`; Nav: `ControlShell.tsx:108`, `z-40`).
- **Alles, was im DOM nach dem Hero kommt, malt über das offene Sheet** (Projekt-Chip-Badges scheinen „durch“ das Capture-Sheet, der Glocken-FAB schwebt darüber).
- Nur die FlowView-Instanzen sind betroffen; die CommandHome-Instanz (`CommandHome.tsx:186`) sitzt am View-Root und ist sauber.
**Fix:** Gemeinsamer `<Overlay>`-Portal-Wrapper (`createPortal(document.body)`), beide Sheets (und der Capture-FAB, s. F3) darüber rendern. Es gibt **kein** `createPortal` im control/-Baum — der Wrapper ist die strukturelle Absicherung gegen jede künftige Wiederholung (jeder neue `transform`/`backdrop-blur`/`isolation`-Ancestor reproduziert den Bug sonst).

**F2 · Capture-Sheet + Android-Tastatur: Submit unerreichbar, Sheet nicht scrollbar.**
`FlowCapture.tsx:78` autofokussiert den Titel-Input; das Sheet (`items-end`, Inhalt ≈ 540 px hoch) hat
**kein `max-h` / `overflow-y-auto`**. Chrome ≥108 resized bei offener Tastatur nur den Visual-Viewport →
das layout-fixierte Bottom-Sheet bleibt hinter der Tastatur, Submit (`:135`) unerreichbar. `EpicCreate.tsx:19/37` dito.
**Fix:** Im Overlay-Wrapper bzw. Sheet: `max-h-[85dvh] overflow-y-auto overscroll-contain` +
`pb-[env(safe-area-inset-bottom,0px)]`; Autofokus auf Touch-Geräten weglassen (oder an `visualViewport` koppeln).

**F3 · Glocken-FAB und „Aufgabe erfassen“-FAB überlappen sich — Quick-Add fängt Taps nicht.**
`NotificationBridge.tsx:82` (`fixed bottom-[calc(5.5rem+safe)] right-3 z-40`, 40×40) liegt **auf** dem mobilen
Capture-FAB `FlowCapture.tsx:168` (`fixed bottom-20 right-4 z-40`, 56×56). Live bewiesen: Playwright-Klick auf
den FAB wird von der Glocke abgefangen („intercepts pointer events“). Die Glocke malt zudem über offene Sheets.
**Fix:** Glocke auf Mobile versetzen (z. B. `bottom-[calc(9.5rem+safe)]`) **oder** in den Header ziehen; FAB
safe-area-fest machen (`bottom-[calc(5rem+env(safe-area-inset-bottom,0px))]`); beide über den Overlay-Portal-Pfad bzw. z-Hygiene gegen Sheets klären (Glocke gehört UNTER offene Modals).

### P1 — Major

**F4 · Kein Body-Scroll-Lock bei keinem Overlay.** Kein Overlay (Sheets, Drawer, Palette, Mehr-Menü)
sperrt `document.body` — auf Touch scrollt der Hintergrund unterm Sheet mit.
**Fix:** im Overlay-Wrapper `useEffect` → `document.body.style.overflow = "hidden"` + `overscroll-contain`.

**F5 · „Mehr“-Menü: ~700 px hoch, ohne max-h — und jeder Scroll-Versuch schließt es.**
`ControlShell.tsx:190`: 15 Einträge (9 moreTabs + 6 secondaryNav) ohne `max-h`/`overflow-y-auto`;
`useDismissibleMenu` (`:168-170`) schließt auf jedes `pointerdown` außerhalb → wer zu „Logs/Skills/Konfig“
scrollen will, schließt das Menü. **Das ist die erlebte „Hakeligkeit“.** (Outside-Dismiss, Escape und
Close-after-Nav existieren und funktionieren — die Höhe ist das Problem.) Fix: siehe **Top-Nav-Redesign** unten.

**F6 · Touch-Targets systematisch unter 44 px.** Live gemessen auf Start: „Öffnen:“-Links der
Decision-Rows **15×15 px** (!), Filter-Chips 25 px hoch, „Flow öffnen“/„Statistik öffnen“ 15 px hoch.
Im Code: Close-Buttons `h-8 w-8`/`h-9 w-9` (`EpicCreate.tsx:47`, `FlowCapture.tsx:111`,
`BacklogDetailDrawer.tsx:143`, `FoDetailDrawer.tsx:100`, `FlowView.tsx:1118`, `ControlsBar.tsx:131/139`,
`BacklogSections.tsx:49/52`), Epic-Trigger mobil icon-only ~34 px. Es gibt einen fertigen 44-px-Helper
`.hc-hit` (`control-tokens.css:209`), der nicht genutzt wird.
**Fix:** `.hc-hit`/`min-h-11 min-w-11` flächig auf interaktive Elemente; „Öffnen“-Links als ganze Zeile tappbar machen.

**F7 · `.hc-prose` ist noch Dark-Theme: weiße Headings auf weißem Papier.**
`control-tokens.css:503` (`h1..h4 { color:#fff }`), `:547` (`th { color:#fff }`), `code`-Chip `rgba(255,255,255,.08)`
(`:528`), `pre` `rgba(0,0,0,.4)` (`:533`) — Research-/Bibliothek-Markdown (`ProseMarkdown.tsx:14`) ist auf den
hellen Panels teils unsichtbar. Der Daylight-Compat-Layer (`:401-490`) remappt nur Utility-Klassen, nicht dieses Roh-CSS.
**Fix:** `.hc-prose` auf `var(--hc-text)`/Token-Farben umstellen.

### P2 — Minor (Politur, gebündelt umsetzbar)

- **F8** CommandPalette `pt-[12vh]`/`max-h-[60vh]` → `dvh`; Input `text-sm` → `text-base` (iOS-Fokus-Zoom). (`CommandPalette.tsx:228/232/235`)
- **F9** EpicCreate-Textarea `text-sm` → `text-base` (iOS-Zoom). (`EpicCreate.tsx:63`)
- **F10** ⌘K-Button im Header ist auf Touch nutzlos → `< sm` ausblenden oder als „Suche“-Icon. (`ControlShell.tsx:104/159`)
- **F11** Gepinnte Compact-Density gilt auch hochkant auf schmalem Viewport (72-px-Rail, keine Bottom-Nav) → unter `lg` hart auf airy clampen. (`useDensity.ts`, `ControlShell.tsx:119-128`)
- **F12** Load-bearing Infos nur in `title=`-Tooltips (113 Stellen; z. B. Health-Details `ControlShell.tsx:234-236`, Confirm-Begründung `panels.tsx:253`) — auf Touch unsichtbar → als sichtbare Hilfszeile rendern, wo entscheidungsrelevant.
- **F13** Tabellen-Skeletons clippen auf Phones (Grid min ≈ 540 px in `overflow-hidden`): `OrchestratorQueueTable.tsx:115-126`, `FoBacklogQueueTable.tsx:115-124` → `overflow-x-auto` wie die echte Tabelle.
- **F14** Sub-12-px-Schriften als Flächenmuster (128 Stellen `text-[10px]`/`text-[11px]`/`0.6–0.72rem`; ModeOption-Hints `FlowCapture.tsx:36`) → bedeutungstragende Hints auf ≥12 px, 10 px nur für Deko-Eyebrows.
- **F15** Statistik mobil: KPI „Kosten heute“ bricht hässlich um (`$ 0.00 · ≈ $ 22.15` über drei Zeilen), Sektionstitel „Wert-Bilanz · 7 Tage“ zerfällt → Wrap-Verhalten der KPI-Pods prüfen.
- **F16** Backlog-Tab mobil: Status-Kacheln (Now/Next Ready/Blocked/Unowned/Stale/High Risk) stapeln als volle Farbbänder → riesiger Scrollweg vor dem Inhalt → mobil 2- oder 3-spaltiges Kompakt-Grid.
- **F17** Flow mobil: Projekt-Filter ist eine Chip-Wand (9+ Chips übereinander) → Top-N + „mehr“-Auf­klapper oder horizontaler Scroll-Strip mit Affordance.
- **F18** Statistik/Backlog haben horizontale Scroll-Container ohne sichtbare Affordance (Elemente enden bei right≈605–611 px) → Fade-Edge oder Scroll-Hinweis.

### Geprüft, sauber (keine Aktion)

Drawers (`BacklogDetailDrawer`, `FoDetailDrawer`: View-Root, z-50 über Nav, korrektes inneres Scrolling,
Esc + Fokus-Rückgabe) · CommandPalette-Mechanik (Fokus-Trap, Esc, Backdrop, am ControlPage-Root) ·
RouteTransition/Framer (kein persistenter Containing-Block) · Bottom-Nav (safe-area, kein Breakpoint-Loch,
60-px-Targets) · SelectionActionBar (bewusst unter Nav) · Mehr-Menü-Dismiss-Mechanik · OfflineStaleBanner ·
Konsole/Netzwerk fehlerfrei · kein horizontaler Page-Overflow auf allen Haupt-Routen.

---

## Top-Nav-Redesign (Headerzeile + „Mehr“)

Der Header trägt mobil aktuell: Eyebrow + Titel, ⌘K-Button, „Mehr“-`<details>`-Dropdown, StatusDots —
dazu unten die 4-Tab-Bottom-Nav. Probleme: nutzloser ⌘K auf Touch (F10), 700-px-Dropdown mit
Scroll-schließt-Menü (F5), kein Backdrop/keine Animation, zwei konkurrierende Nav-Orte.

**Empfehlung — Variante A („Mehr“ wird mobiles Bottom-Sheet):**
- `< lg`: „Mehr“ wandert aus dem Header **als 5. Tab in die Bottom-Nav** (Standard-Mobile-Pattern:
  Start · Flow · Stats · Bibliothek · Mehr). Tap öffnet ein **Bottom-Sheet über den Overlay-Wrapper aus F1**
  (Portal, Backdrop, `max-h-[80dvh] overflow-y-auto`, Scroll-Lock): zwei gruppierte Sektionen
  („Control-Ansichten“ = moreTabs, „System“ = secondaryNav), Einträge `min-h-11`, volle Breite.
  Damit erbt das Menü automatisch alle Fixes (Scrollbarkeit, kein Scroll-Dismiss, sauberes Stacking) und
  fühlt sich an wie die übrigen Sheets — ein Pattern statt drei.
- Header mobil wird ruhig: nur Eyebrow + Titel + StatusDots. ⌘K-Button nur `≥ sm`.
- `≥ lg` (Desktop): bestehendes `<details>`-Dropdown bleibt, bekommt aber `max-h-[70dvh] overflow-y-auto`
  (Laptop-Fenster können klein sein) — minimal-invasiv, kein Redesign nötig.

**Variante B (kleiner Eingriff, falls A zu groß):** Dropdown behalten, nur `max-h-[70dvh] overflow-y-auto`
aufs Panel — behebt F5 mechanisch, lässt aber Touch-Ergonomie (Position oben rechts, kleine Targets,
kein Backdrop) unverändert. Nur als Fallback.

---

## Umsetzungsplan (Slices für die Bau-Session)

Reihenfolge so gewählt, dass M1 die Infrastruktur (Overlay-Wrapper) liefert, von der M2/M3 zehren.

| Slice | Inhalt | Dateien (Kern) | Funde |
|---|---|---|---|
| **M1** | `<Overlay>`-Portal-Wrapper (createPortal→body, Backdrop, Esc, Scroll-Lock, `max-h dvh` + inneres Scrollen, safe-area) bauen; EpicCreate- + FlowCapture-Sheet darauf umstellen; Autofokus touch-aware; `text-base`-Inputs | neu `components/Overlay.tsx`; `EpicCreate.tsx`; `FlowCapture.tsx` | F1 F2 F4 F9 |
| **M2** | FAB/Glocke entflechten + safe-area; Glocke unter Modals bzw. in Header (mobil) | `NotificationBridge.tsx`; `FlowCapture.tsx:168` | F3 |
| **M3** | Top-Nav Variante A: „Mehr“ als 5. Bottom-Tab + Bottom-Sheet (Overlay-Wrapper); Header mobil schlank, ⌘K ≥sm; Desktop-Dropdown max-h | `ControlShell.tsx`; `i18n/de.ts` | F5 F10 |
| **M4** | Touch-Target-Pass: `.hc-hit` auf Close-Buttons, „Öffnen“-Row-Links, Chips; Decision-Row ganz tappbar | `EpicCreate.tsx`, `FlowCapture.tsx`, beide Drawer, `ControlsBar.tsx`, `BacklogSections.tsx`, Decision-Row-Komponente | F6 |
| **M5** | `.hc-prose`-Daylight-Fix (Headings/th/code/pre auf Tokens) | `styles/control-tokens.css:495-560` | F7 |
| **M6** | Politur-Bündel: dvh-Palette, Density-Clamp, Skeleton-overflow, Statistik-KPI-Wrap, Backlog-Kacheln kompakt, Chip-Wand, title-Tooltips, Sub-12px-Hints, Scroll-Affordance | diverse (siehe F8–F18) | F8 F11–F18 |

M1–M3 sind der eigentliche Schmerz (P0 + erlebte Hakeligkeit); M4–M6 können einzeln und später laufen.

**Akzeptanz pro Slice (Gates):**
1. `cd web && npx tsc --noEmit && npx vitest run && npm run build` grün.
2. `probe.mjs` (dieser Ordner) — nach M1 müssen gelten:
   `elementFromPoint` über beiden Submit-Buttons trifft **den Button selbst**; Capture-Sheet intern scrollbar;
   Hintergrund scrollt bei offenem Sheet nicht. Nach M2: regulärer (nicht-force) Playwright-Klick auf den
   Capture-FAB öffnet das Sheet. Nach M3: Mehr-Menü auf Pixel-5-Profil vollständig erreichbar (alle 15 Einträge tappbar).
3. Kurzer Telefon-Gegencheck durch Piet (Tailscale) für M1–M3.

**Risiken / Hinweise für die Bau-Session:**
- `~/.hermes/hermes-agent` ist ein **live geteilter Fork-Checkout** (Auto-Committer läuft, mehrere Sessions):
  vor Arbeit Vault-Check-IN (`touching: web/src/control`), `git status` prüfen, Sync über das
  `hermes-fork-sync`-Skill — kein blinder Push.
- `web/src/lib/api.ts` nicht anfassen (upstream-konfliktträchtig); alle Änderungen liegen in `web/src/control/`.
- Upstream-Merge-Hygiene: neue Datei (`Overlay.tsx`) + chirurgische Edits bevorzugen, House-Style halten.
- `.hc-hero` selbst **nicht** ändern (`isolation` trägt den Deko-Effekt) — der Portal-Weg ist der Fix.
- Visuelle Referenzen: `/tmp/hc-probe-*.png`, `/tmp/hc-mobile-*.png` (regenerierbar via Methodik oben).

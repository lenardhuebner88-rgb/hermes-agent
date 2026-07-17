# Plan: Autoresearch-Tab → menschenlesbare Entscheidungsoberfläche

**Autor:** Claude (Plan) · **Umsetzung:** Codex-Terminal-Session · **Datum:** 2026-07-15
**Zielrepo:** `~/.hermes/hermes-agent` · **Tab:** `/control` → Autoresearch (`web/src/control/views/AutoresearchView.tsx`)

## Kontext & Auftrag

Der Autoresearch-Tab ist für den Operator (Nicht-Ingenieur, Piet) heute **nicht als
Entscheidungsoberfläche nutzbar**. Die oberste Ebene (Hero „X geprüfte Verbesserung
entscheiden", Nutzen/Risiko/Empfohlen-Framing) ist gut — aber sobald man eine Entscheidung
wirklich treffen will, kippt es. Ziel dieses Passes: der Tab wird eine **Ja/Nein-Triage-Oberfläche
pro Vorschlag**. Piet entscheidet pro Karte „annehmen/ablehnen" auf Basis von Klartext; aller
technischer Kram wandert hinter „Für Technik ausklappen".

Dies ist ein **UI-Usability-Pass** (Items 1–4). Backend-Logik, Outcome-Verifikation und der
Enforcement-Pfad werden **nicht** angefasst (siehe „Nicht in Scope").

## Ist-Zustand (live verifiziert, 2026-07-15, Screenshots unter `.claude/worktrees/…/ui-verify-autoresearch/`)

- **Blocker 1 — Haupt-Knopf ist tot.** Der primäre CTA („Top-Karte öffnen" / „Entscheidungen
  prüfen") tut nachweislich nichts: kein Scroll, kein Aufklappen, kein Request. `scrollTop`
  bleibt 0, `aria-expanded` bleibt `false`.
- **Blocker 2 — Detail-Ebene = Ingenieurs-Wand.** Beim Öffnen einer Karte: rohe Dateipfade als
  Überschrift, CLI-Kommandos (`scripts/run-affected.sh …`), Code-Bezeichner (`` `toolsets.py` ``),
  Deutsch/Englisch gemischt (Risiko/Empfehlung teils englisch), und ein „Fix-Diff +2/−2", der in
  Wirklichkeit ~14 Zeilen inkl. fremdem Skill-Markdown rendert.
- **Blocker 3 — Lärm.** Seite ist ~9 Bildschirmhöhen lang; „Erweitert" ist **aufgeklappt** und
  frisst ~die Hälfte. Dazu ein Desktop-Layout-Bug (Statustext als 1-Wort-pro-Zeile-Streifen).
- Konsole sauber (0 Meldungen). Mobile (390px) hat den Layout-Bug **nicht** (stackt korrekt).

## Getroffene Design-Entscheidungen (verbindlich, mit Operator abgestimmt)

- **Kern-Entscheidung des Tabs:** pro Vorschlag „annehmen/ablehnen".
- **Sichtbarer Karten-Inhalt (Default), alles andere hinter „Für Technik ausklappen":**
  1. **Was es ist** — 1 Satz Klartext, kein Jargon, kein Pfad.
  2. **Warum es dir etwas bringt** — konkreter Nutzen.
  3. **Empfehlung + Grund** — „annehmen"/„ablehnen" + Halbsatz.
  4. **Aufwand, Kosten & Risiko grob** — klein/mittel/groß, geschätzte Kosten, ein Downside-Satz.
- **Endziel gestuft:** jetzt nur berichten/entscheiden; Enforcement später (eigene Spec, s.u.).

## Scope — Items 1–4

### Item 1 — Haupt-Knopf reparieren (höchste Priorität)

**Root Cause (im Code bestätigt):** In `AutoresearchView.tsx`:
```
focusProposal(id) → document.getElementById(`autoresearch-proposal-${id}`)?.scrollIntoView(...)
```
Solange die Ziel-Karte hinter dem „1 Einzelkarte anzeigen"-Toggle in `ProposalQueue.tsx`
**eingeklappt** ist, ist `#autoresearch-proposal-<id>` **nicht im DOM** → `getElementById` gibt
`null` → `?.` schluckt den Aufruf → No-op. Betroffen: `focusProposal`, `selectOrFocusTopProposal`,
der Deep-Link-`useEffect` (Zeilen ~334–349, ~373–380) und der Hero-CTA `runPrimaryRecommendation`
(→ `scrollTo`/`focusProposal`).

**Aufgabe:** Der Primär-CTA (und `focusProposal`) muss die Zielkarte **erst aufklappen/rendern**
und **dann** hinscrollen. Konkret: den Collapse-/Expand-Zustand der `ProposalQueue` (der
„Einzelkarte anzeigen"-Toggle) an den Focus-Pfad koppeln — z.B. Focus setzt `expanded=true` für
die Karte bzw. öffnet die Einzelkarten-Ansicht, danach scrollt ein Effect auf das dann existierende
Element. Kein reines `scrollIntoView` mehr auf potenziell nicht-gerendertes Element.

**Akzeptanz:** Klick auf den Hero-Primär-CTA klappt die Top-Karte auf **und** scrollt sichtbar
dorthin; `aria-expanded` der Karte wird `true`; ein neuer Regressionstest deckt „Focus auf
eingeklappte Karte → Karte wird geöffnet" ab. Live per Screenshot belegt (Desktop + Handy).

### Item 2 — Detail-Ebene auf Klartext entrümpeln

**Dateien:** `ProposalQueue.tsx` (Kartendetail + „Fix-Diff"-Block + „Entscheidungshilfe"),
`OutcomePanel.tsx` (Messbeleg-Karten), `web/src/control/i18n/de.ts`.

**Aufgabe:**
- Default-Ansicht einer Karte zeigt **nur** die vier abgestimmten Felder (Was / Warum /
  Empfehlung+Grund / Aufwand+Kosten+Risiko) mit den beiden Buttons **[annehmen] [ablehnen]**.
- Alles Technische — Rohdiff, CLI-Kommandos (`Prüfung: scripts/…`), Dateipfade, Code-Bezeichner,
  `contract_id`/SHA/`Probevertrag`/`Gegenmetriken`/`Schwelle`/`Fenster`/`Evidence` — wandert
  hinter eine Disclosure **„Für Technik ausklappen"** (Default zu).
- **Fix-Diff-Wall zähmen:** Der „+2/−2"-Diff darf nicht ~14 Zeilen inkl. fremdem Kontext zeigen.
  Entweder nur die tatsächlich geänderten Zeilen rendern (kein unrelated Skill-Markdown) oder den
  Diff komplett in die Technik-Disclosure verschieben. Das „+X/−Y"-Label muss zur gezeigten Menge
  passen.
- **DE/EN-Mischung:** Labels durchgängig Deutsch. Achtung: die *Inhalte* von „Risiko"/„Empfehlung"
  kommen teils modell-generiert auf Englisch aus den Proposal-Daten — das ist ein
  Generator-Prompt-Thema, **nicht** in diesem UI-Pass zu lösen. Hier nur: deutsche **Labels**,
  und wenn ein Feld leer/`—` ist, einen menschlichen Platzhaltersatz statt `„Kein vorregistrierter
  Claim"` / `„Schwelle keine · Gegenmetriken keine"`. (Generator-Prompt → Follow-up notieren.)

**Akzeptanz:** In der Default-Kartenansicht ist **kein** roher Pfad/CLI/Diff/SHA sichtbar; die vier
Klartext-Felder + beide Buttons sind da; „Für Technik ausklappen" enthält den Rest. Screenshot-Beleg.

### Item 3 — Lärm runter

**Dateien:** `AdvancedSection.tsx` (bzw. dessen Einbindung in `AutoresearchView.tsx`/`panels.tsx`),
`OutcomePanel.tsx`.

**Aufgabe:**
- „Erweitert" (Lane-Modelle, Subsystem-Audit, Test-Foundry) **standardmäßig eingeklappt** — auch
  wenn der „Speziallauf braucht Aufmerksamkeit"-Banner aktiv ist (der darf highlighten, aber nicht
  auto-expandieren).
- Reihenfolge: **Entscheidungen zuerst.** Der Jargon-lastige Messbeleg-Block (`OutcomePanel`
  „Messbelege") wird zusammenklappbar (Default zu), solange es keine `contract_verified`-Fälle gibt.
- Ziel: sichtbare Seitenlänge im Default-Zustand deutlich unter den heutigen ~9 Viewports.

**Akzeptanz:** Beim Laden ist „Erweitert" zu; die Entscheidungs-Queue steht ohne langes Scrollen im
Blick. Screenshot-Beleg Desktop + Handy.

### Item 4 — Desktop-Layout-Bug fixen

**Symptom (gemessen):** Zwei Status-Absätze kollabieren auf Desktop (1440px) zu ~58–63px breiten
Spalten (1–2 Wörter/Zeile): „Keine sichtbare Karte ist für Sammel-Übernehmen freigegeben."
(Panel „Auswahlwirkung") und „Nächster Schritt: Jetzt erst die Karten prüfen…" (Panel „Letzter
Lauf"). Tritt bei 390px **nicht** auf. Ursache: Text-Spalte in einer Flex-Row ohne `min-w-0`/
Flex-Basis neben fixer Stat-Box-Gruppe.

**Dateien:** vermutlich `panels.tsx` (die beiden Panels). Codex lokalisiert exakt.

**Aufgabe:** `min-w-0` (bzw. korrektes Flex-Basis/`flex-1`) auf die betroffene Text-Spalte, sodass
sie die volle Breite nutzt wie die korrekt rendernde Schwester-Instanz (624px).

**Akzeptanz:** Beide Absätze rendern auf Desktop als normal breite Absätze; Regressionstest oder
Screenshot-Beleg.

## Nicht in Scope (bewusst ausgelassen)

- Backend/Outcome-Verifikations-Logik, Migrationen, Gateway.
- Generator-Prompt-Fix für englische Risiko/Empfehlungs-Texte → **Follow-up** (separat notieren).
- Der Shadow-**Warte-Status-Streifen** („läuft/letzter Lauf/worauf es wartet") → **Item 5, später**
  (Design steht schon: Ableitung aus `deriveMetrics` + `/api/n/status` + `/api/n/runs`).
- **Enforcement (Ziel 2)** → eigene Spec, wenn der erste natürliche `contract_verified`-Fall da ist.
  Verbindliche Eckpunkte für diese spätere Spec: Kill-Switch-Marker analog Shadow; Promotion
  **manuell** + frischer Cross-Family-Review; **nie** Auto-Promote; erst ab n≥3 `contract_verified`
  derselben Klasse; `calibration_eligible=false` bleibt bis dahin.

## Bau-Randbedingungen für die Codex-Session (verbindlich)

- **Worktree-Hygiene:** Der Live-Checkout wird parallel editiert. `git status --short` zuerst;
  fremde uncommittete/untrackte Arbeit unangetastet lassen. Arbeit bevorzugt in einem eigenen
  Worktree; dort `cd <wt>/web && npm ci`, dann Gates über die **gehisteten Root-Binaries**
  (`<wt>/node_modules/.bin/…`) — **nie** `npx tsc/vitest` im Worktree. Nie den Worktree-Diff im
  Live-Checkout gaten.
- **Design-Sprache ist bindend:** `web/src/control/DESIGN.md`, Tokens in
  `web/src/control/theme.css`; keine Ad-hoc-Farben/Abstände. Ratchet in `scripts/gate-frontend.sh`.
- **Gates (müssen grün sein):** `scripts/gate-frontend.sh` (lint:control → `tsc -b --noEmit` →
  vitest → build). Exit-Code ist die Wahrheit; **nicht** freihand mit `| tail` gaten.
- **Tests:** ≥1 Test gegen **echtes Proposal-Datenformat** (nicht getürkt) — mindestens für Item 1
  (Focus öffnet eingeklappte Karte) und Item 2 (Default-Ansicht zeigt keinen Rohdiff/Pfad).
  Bestehende `AutoresearchView.test.tsx` / `OutcomePanel.test.tsx` grün halten/erweitern.
- **Abnahme:** Screenshot Desktop 1440 + Handy 390 pro Item-relevanter Ansicht; Konsole 0 Fehler.
- **Kein Remote-Push** (`origin` = NousResearch, verboten). Kein Deploy ohne Operator-Go. Lokaler
  Commit ok; Landung/Deploy entscheidet Piet.

## Definition of Done

Items 1–4 umgesetzt, Gates grün, Tests grün (inkl. neuer Regressionstests), Screenshot-Abnahme
Desktop+Handy vorhanden, Konsole sauber. Ein Nicht-Ingenieur kann: Tab öffnen → Top-Entscheidung
per funktionierendem Knopf öffnen → in Klartext Was/Warum/Empfehlung/Aufwand+Risiko lesen →
[annehmen]/[ablehnen] klicken, ohne je Rohdiff/Pfad/CLI sehen zu müssen.

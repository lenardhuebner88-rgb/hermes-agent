# BRIEF S2 — /lanes Greenfield Frontend (Builder: Qwen 3.8 Max Preview)

**Arbeitsort NUR:** `/home/piet/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform` (Branch `claude/lanes-model-platform`). Vor Start `git status --short` (parallele Sessions — fremde Änderungen stehen lassen). **KEIN push, KEIN deploy, KEIN Service-Restart, KEIN origin, KEIN --force.** `git commit` auf den Branch ist erlaubt (Phase-Commits erwünscht).

## Ziel
Ersetze `web/src/control/views/LanesView.tsx` durch ein **greenfield** Redesign: der /lanes-Tab wird zur Modell-Plattform. Baue exakt gegen das verbindliche Mockup `docs/design/lanes-plattform-mockup.html` (4 Artboards — öffne/lese es, es ist die visuelle Wahrheit) und den Plan `docs/plans/2026-07-24-lanes-model-platform.md`. Design-Sprache „Bronze auf Graphit" ist BINDEND (`web/src/control/DESIGN.md` + `theme.css`).

## Was du lesen MUSST (in dieser Reihenfolge)
1. `docs/design/lanes-plattform-mockup.html` — das verbindliche Design (alle 4 Artboards + Zustands-Vokabular).
2. `web/src/control/views/lanes/api.ts` — EXISTIERENDER API-Client + pure Helper. BEHALTEN und ERWEITERN (nicht löschen): `editorRows`, `applyChoice`, `profilesFromEditorRows`, `laneEntryWarnings`, `choiceFromEntry`/`entryFromChoice`/provider-aware Varianten, Eskalations-Logik, `FALLBACK_MODELS`, `modelLabel`, `providerOptions`, `modelsForProvider`. Nur NEUE Typen/Funktionen ergänzen.
3. `web/src/control/DESIGN.md` + `web/src/control/components/leitstand/README.md` — Primitive + Doktrin.
4. `web/src/control/views/LoopsView.test.tsx` (Zeilen 1–60) — Vitest-Muster (render + screen + waitFor; Mock des Hooks/Fetch mit REALEN Fixtures, KEINE vi.mock-Tautologie).
5. Reale Fixture: `web/src/control/views/lanes/__fixtures__/lanes-live.json` (captured live GET /lanes: 3 Lanes, 10 Profile, 55 Modelle, 7 Gruppen).

## Vertrag (gepinnt — der Backend-Slice S1 liefert diese Felder; frontend-seitig ALLE NEUEN FELDER OPTIONAL typisieren, damit alte Payloads + die Fixture ohne die Felder weiter kompilieren/gerendert werden)
`LaneCatalogProfile` += `reasoning_effort?: string|null`, `reasoning_support?: string[]`
`LaneModelOption` += `authenticated?: boolean`, `configured?: boolean`, `price_in_per_mtok_usd?: number|null`, `price_out_per_mtok_usd?: number|null`, `context_window?: number|null`, `reasoning_support?: string[]`, `probe?: {status: string; duration_ms?: number; error_class?: string|null; reason?: string|null; at?: number}|null`, `sinnvoll?: boolean`, `used_in_profiles?: boolean`, `admitted?: boolean`
Neue Client-Funktionen in api.ts:
`runModelProbe(input:{provider:string; model:string; profile?:string; timeoutSeconds?:number}): Promise<ModelProbeResult>` → `POST /api/plugins/kanban/lanes/model-probe`
`runCatalogProbe(input:{models:{provider:string;model:string}[]; profile?:string|null; timeoutSeconds?:number; limit?:number}): Promise<{results:ModelProbeResult[]; truncated:boolean}>` → `POST /api/plugins/kanban/lanes/catalog-probe`
`ModelProbeResult = {provider; model; profile?; status: "ok"|"fallback"|"auth_error"|"quota_or_rate_limit"|"timeout"|"config_error"|"error"|"skipped"; duration_ms?; observed_provider?; observed_model?; error_class?; reason?; at?}`
Persist-Payload: `LanePersistProfileEntry` um `reasoning_effort?: string|null` erweitern; `profilesFromEditorRows` muss `reasoning_effort` mitschicken wenn die Row einen nicht-Default-Wert hat (null/"Standard" = weglassen).

## Layout / Komponenten (exakt wie Mockup; neue Komponenten als eigene Dateien unter `web/src/control/views/lanes/`)
- **LanesView.tsx** (neu): Puls-Leiste kommt von der Shell (nicht selbst bauen). Dann: `LaneBar` (Karten pro Lane; aktive = surface-2 + `inset 3px 0 0 var(--bronze)`-Äquivalent via Token-Klasse + Bronze-LED + „Aktiv"-Eyebrow; Klick = `activateLane` mit Inline-Bestätigung; Ghost-Card „Neue Lane"). Dann 2-Pane (≥840px): links 62% `ProfileMatrix`, rechts 38% Subtabs `Rauch`|`Kompass`. Tablet (600–839): Panes gestapelt. Mobil (<600): Lane-Pills horizontal-scroll, Profile als Cards, Rauch/Kompass via `DrawerShell`, sticky Save 48px.
- **ProfileMatrix**: Tabelle, Spalten Profil (RoleChip-Stil: provider-dot + name + description) · Modell (`ModelSelect`, gefiltert, group headers mit provider-dot) · Reasoning (`ReasoningControl`) · Fallback (Count, Edit via Drawer) · Probe (mono-Latenz + LED aus row/model.probe) · Override-Badge (Lane ≠ Profil-Default). Footer `SaveBar` ([Verwerfen][Speichern + aktivieren] bronze primary, dirty-gesteuert) + Hint „wirkt ab nächstem Spawn".
- **ReasoningControl**: Segment-Buttons aus `row.reasoning_support` (Werte z.B. [Std,min,low,med,high] wobei „Std" immer = Default/leer). Auswahl → bronze `.on`. Wenn `reasoning_support` fehlt/leer → `.dis` (disabled) + Hint „Modell hat keinen Reasoning-Knopf" (siehe Mockup coder/qwen). Wert in EditorRow-Stage halten, in Persist mitschicken.
- **ModelSelect**: Default-Filter „sinnvoll & erreichbar" = `m.sinnvoll !== false` UND (kein probe ODER probe.status nicht in {auth_error,timeout,error,config_error}); Toggle „Alle (N)" zeigt Rest grau gruppiert. Gruppierung nach provider mit stabilem data-dot. Unbekannte/fehlende Felder fail-soft anzeigen (nie crashen).
- **Rauch-Panel** (`SmokePanel.tsx`): KPI-Tiles (erreichtbar X/Y = count probe.status ok|fallback / gesamt sinnvoll; p50 Latenz mono; blockiert = count auth|timeout|error) via `KpiTile`. Primary CTA „Katalog messen · N sinnvolle Modelle" → `runCatalogProbe` mit der sinnvoll-Menge (limit 8). Ergebnis-Feed als `ListRow` mit `StatusChip`/LED + mono-Latenz + Kosten. Per-Row-Probe-Button (Blitz) in der Matrix triggert `runModelProbe` und patched die Row. Nach Batch: `loadLanes()` neu (probe-Cache kommt im GET zurück). Kein Dauer-Polling nötig.
- **Kompass-Panel** (`Compass.tsx` + **`fit.ts`** pure Logik, unit-getestet): `SubtabChips` der Rollen (coder/reviewer/critic/verifier/research/scout/premium). `fit.ts` exportiert `scoreModelForRole(model, role, probes)` → `{score:number(0-100), reasons:string[]}` und `rankModelsForRole(...)`. Rollen-Anforderungsprofil als statische Tabelle in fit.ts (coding/reasoning/speed/cost_sensitivity/context Gewichte pro Rolle). Score aus: probe-Latenz (schneller=besser, fehlend=neutral), Preis (billiger=besser je cost_sensitivity), reasoning_support (Match zum Rollen-Wunsch), authenticated/sinnvoll (Pflicht, sonst Score 0), Kontext. UI: Top-5 `fit-row` (Rank, provider-dot, model mono, „● aktuell"-Bronze-Marker wenn = row-Modell, `MeterBar`/meter mit ink-2-Füllstand — **NIE bronze** — Score mono, Grund-Chips mono, Button „Übernehmen" = staged die Wahl in die Matrix-Row via applyChoice-kompatiblen Eintrag; aktuelle Row = „Übernehmen ✓"). **Bench**: Auswahl 2–4 Modelle → `runCatalogProbe` genau diese → 2–4 `cmp-card` nebeneinander (Status-Chip + Latenz + Kosten + Reasoning mono) + „Bench mit Auswahl wiederholen".
- **Provider→data-Farbe** (stabile Map in `fit.ts` oder `providerColors.ts`): openai-codex→data-3, alibaba-token-plan→data-1, neuralwatt→data-2, moonshotai/kimi→data-4, claude-cli/anthropic→data-5, nous→data-6, openrouter→data-7, default→data-6. Dots als Token-Klassen (keine Roh-Hex!).

## Bindende Design-Doktrin (VERBATIM einhalten — Ratchet-Gate `npm run check:tokens` zählt Roh-Hex in .tsx/.ts und FAILT bei Zuwachs)
- **Keine Roh-Hex / kein `[#...]`/`rgb()` in .tsx/.ts.** Jede Farbe = Token-Utility (`bg-surface-1/2/3`, `text-ink/ink-2/ink-3`, `border-line/line-soft`, `text-live`/`text-bronze`/`bg-live`, `text-status-ok/warn/alert`, `bg-data-1..7`/`text-data-1..7` falls vorhanden, sonst `var(--color-data-N)` NUR in einer .css-Datei). Wenn ein data-dot-Token als Utility fehlt: eine kleine `lanes.css` (vom Route-Chunk geladen, gescopet unter einer Wrapper-Klasse) mit `.pdot.p-x{background:var(--color-data-N)}` — CSS wird vom Ratchet NICHT gescannt, aber Regel: nur `var(--color-*)`.
- Bronze (`--color-live/bronze`) NUR für Interaktives/Live (aktive Lane, Primary-CTA, gewähltes Segment, Focus-Ring, aktive Subtab-Underline, „aktuell"-Marker). NIE als Status-Chip, NIE als Meter-Füllstand.
- Status-Trio (ok/warn/alert) NUR semantisch, IMMER LED+Label, nie Farbe allein.
- Mono (`font-data`/mono-Utility) NUR für Daten (Latenz, $, Modell-IDs, Counts, Scores, Timestamps). Eyebrows/Mastheads = display/Archivo.
- 3 Surface-Tiefen: 0 Canvas, 1 Panel, 2 Card, 3 Hover/Selected-only.
- Chips nie Navigation. Radius panel=10 / card=7. Touch mobil ≥44px.
- Motion 120–160ms ease-out, `@media (prefers-reduced-motion: reduce)` killt sie. Keine Ambient-Animation.
- Empty-States (Doktrin): Situation → Bewertung → Aktion, ink-2/3, KEIN ok-Grün auf neutral, kein grünes Häkchen (siehe Mockup Artboard 4 „Noch keine Messungen").
- Importiere Shared-Primitive aus `components/leitstand`: `SectionHeader`, `KpiTile`, `SubtabChips`, `DrawerShell`, `ListRow`, `StatusChip`; `MeterBar`/`Led` aus `components/atoms` wenn vorhanden. Erfinde nichts neu, was es schon gibt.

## Strings / Isolation (KRITISCH — Parallel-Session-Schutz)
- Alle UI-Strings als lokales `const t = {...}` IM View-Ordner (wie heute). **NIEMALS** `web/src/control/i18n/de.ts`, `lib/types.ts`, `lib/schemas.ts` anfassen. **NIEMALS** `ControlShell.tsx`/`ControlPage.tsx` (Tab-Verdrahtung existiert bereits). **NIEMALS** geteilte Hooks in `hooks/useControlData.ts` ändern. Nur `views/LanesView.tsx` + `views/lanes/**`.

## Tests (colocated, Vitest, gegen REALE Fixture — keine Tautologie; Canon-Falle: Bug/Verhalten VOR dem Test gegen echte Daten prüfen, nie `vi.fn()` als Erwartung UND SUT)
- `fit.test.ts`: `scoreModelForRole`/`rankModelsForRole` deterministisch für geseedete Modelle; coder-Ranking bevorzugt coding+günstig+schnell; Modell ohne authenticated → score 0; reasons-Array enthält Latenz/Preis-Token wo Daten da sind.
- `lanes.helpers.test.ts` (neue pure Filter/Choice-Helper): `filterSinnvoll(models)` reduziert die 55er-Fixture auf <55 (nous/unzugelassen raus wenn sinnvoll-Feld das markiert; fail-soft wenn Feld fehlt → curated-Heuristik source∈{claude-cli}||authenticated); Roundtrip Choice↔Entry MIT reasoning_effort; `profilesFromEditorRows` schickt reasoning_effort nur bei Nicht-Default.
- `LanesView.render.test.tsx`: render mit einem Mock des `loadLanes`/Fetch, der die REALE Fixture (`__fixtures__/lanes-live.json`) zurückgibt; asserte: Lane-Bar rendert 3 Karten + aktive Markierung; Matrix rendert eine Row pro Profil; Reasoning-Control ist für eine Row ohne reasoning_support (bzw. qwen/alibaba ohne Support) DISABLED mit Hint-Text; Rauch-Subtab + Kompass-Subtab vorhanden; 0 ungefangene Console-Errors (watchPage-Pattern). **Kein** `vi.mock` derselben Funktion als Erwartung.

## Gates (verbatim — Exit-Code ist die Wahrheit, NICHT durch `| tail` füttern; Binaries sind hoisted im Worktree-Root node_modules, npm ci ist bereits gelaufen)
Pro Phase mindestens `cd web && npm run lint:control && ../node_modules/.bin/tsc -b --noEmit --force && ../node_modules/.bin/vitest run <betroffene Dateien>`.
Am Ende (Phase D) VOLL: `cd /home/piet/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform && bash scripts/gate-frontend.sh` (lint:control → tsc -b --force → vitest FULL → build). `--skip-build` NICHT setzen (worktree web/dist darf überschrieben werden). Wenn der Token-Ratchet (`check:tokens`) wegen neuer Roh-Hex FAILT: ersetze durch Token-Utilities/lanes.css und baseline NICHT manuell erhöhen.

## Phasen + Commits (damit Teilfortschritt erhalten bleibt — nach jeder grünen Phase committen)
- **Phase A (Kern):** LaneBar + ProfileMatrix + ReasoningControl + ModelSelect(gefiltert) + SaveBar/Persist inkl. reasoning_effort. Helper + render-test. Gate (lint+tsc+vitest betroffen) grün. Commit msg: `lanes: greenfield matrix + reasoning + filtered model select`.
- **Phase B (Rauch):** SmokePanel + runModelProbe/runCatalogProbe + KPI + Feed + Per-Row-Probe. Tests. Gate grün. Commit: `lanes: smoke panel + model/catalog probes`.
- **Phase C (Kompass):** fit.ts + Compass + Bench + adopt. fit.test + helper.test. Gate grün. Commit: `lanes: compass fit-ranking + bench`.
- **Phase D (Politur+Full-Gate):** mobile/drawer, empty states, reduced-motion, i18n-lokal komplett; `scripts/gate-frontend.sh` komplett grün. Commit: `lanes: polish + full green gate`.

## Anti-Scope (NICHT tun)
- Kein Backend/Python. Keine Shared-Files (s.o.). Keine neuen npm-Dependencies (bewege dich mit vorhandenem motion/react nur falls in package.json, sonst CSS-Transitions). Kein Refactor von api.ts-Bestandsfunktionen. Kein Auth/Permission-Layer. Kein Edit von `web/src/lib/api.ts`.

## Done-when (dein Report)
1. `scripts/gate-frontend.sh` exit 0 — Output VERBATIM (zumindest die Zusammenfassungszeilen + Exit-Code).
2. Liste der Vitest-Tests die laufen + grün.
3. Diff-Summary (Dateien + LOC) und die 4 Commit-SHAs auf dem Branch.
4. Keine Roh-Hex in deinen .tsx/.ts (Ratchet grün ist der Beweis).
5. Kurznotiz: welche Mockup-Entscheidung du bei Unklarheit wie umgesetzt hast.
Screenshots sind NICHT deine Aufgabe (visuelle Abnahme macht der Orchestrator separat).

Bei Ambiguität: Mockup + Plan schlagen nach; im Zweifel fail-soft + lokal begrenzt; NIEMALS Shared-Files anfassen. Wenn eine Phase partout nicht grün wird: Teil-Commit des grünen Stands + konkrete Fehlerzeile im Report; NICHT stundenlang am selben Punkt drehen.

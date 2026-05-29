# Handoff: Hermes Control — vereinheitlichtes Steuer-Dashboard

> **Ziel dieses Pakets:** Ein Entwickler (mit Claude Code) baut das Dashboard in einer
> echten React-Codebasis **genau nach der empfohlenen Architektur**: **Richtung A als
> Fundament**, **Richtung-B-Dichte als ausklappbare Stufe** und die **⌘K-Command-Palette
> aus Richtung C als Beschleuniger** darüber.

---

## 1. Überblick

Hermes Control ist **ein** Steuer-Dashboard, das langfristig drei verteilte Tools ablöst.
Es überwacht und steuert zwei Agenten-Flotten (**Hermes-Worker** und **OpenClaw-Agenten**)
und fährt einen **Autoresearch-One-Click-Flow**, der Verbesserungen an Skills (Markdown)
und an Code vorschlägt.

- **Genau ein Nutzer** (technisch versierter Betreiber, will Klartext statt Jargon). **Kein
  Enterprise-Chrome** (kein Org-Switcher, kein Billing, keine Team-Verwaltung).
- **~70 % Zugriff vom Handy** (über Tailscale) → **mobile-first ist Pflicht**; Desktop ist
  die größere Variante desselben Layouts.
- **Dark-Mode als Default**, heller Modus optional. **UI-Sprache: Deutsch.**
- Reaktionszeit-Anspruch: Übersicht muss „Ist alles gesund?" in **≈3 Sekunden** beantworten.

## 2. Über die Design-Dateien

Die HTML-Dateien in diesem Bundle sind **Design-Referenzen** (Prototypen, die Aussehen und
Verhalten zeigen) — **kein** produktiver Code zum 1:1-Kopieren. Aufgabe ist, diese Designs
in der **bestehenden Umgebung der Ziel-Codebasis** nachzubauen (siehe Tech-Stack unten) und
dabei deren etablierte Muster/Bibliotheken zu nutzen. Die Prototypen sind in Vanilla-JS +
Tailwind-CDN gebaut, weil sie eigenständig im Browser laufen sollen — die Produktion ist
React.

Es gibt **drei Richtungen**. Sie sind bewusst als Vergleich gebaut. **Gebaut werden soll
nicht eine davon allein, sondern die vereinheitlichte Architektur aus Abschnitt 4.**

## 3. Fidelity

**High-Fidelity.** Farben, Typografie, Abstände, Radien, Stati und Interaktionen sind final
und folgen dem **Mission-Control-Design-System** (Token-Datei `tokens.css` liegt bei). Bitte
pixelgenau mit den Codebase-eigenen Bibliotheken nachbauen.

## 4. Empfohlene Ziel-Architektur — „eine App, drei Stufen"

Das ist der Kern des Auftrags. Nicht drei Apps, sondern **ein** responsives Dashboard:

| Stufe | Quelle | Wann aktiv | Was sie liefert |
|---|---|---|---|
| **Basis-Layout** | **Richtung A** | immer (mobile-first) | Bottom-Tab-Bar, luftige runde Cards, großer „Gesund?"-Hero. Default auf allen Größen. |
| **Dichte-Stufe** | **Richtung B** | `lg+` automatisch **oder** per Dichte-Tweak „Kompakt" | Linke Icon-Rail statt Bottom-Tabs, Status-LEDs, Monospace-Metriken, CPU/RAM-Bars, System-Matrix, Terminal-Log. |
| **Command-Palette** | **Richtung C** | überall per `⌘K` / `Ctrl+K` (optionales Overlay) | Spotlight-Navigation „springe zu / tu dies", Tastatur-Beschleuniger. Ersetzt **nicht** die sichtbare Navigation. |

**Begründung (für den Entwickler, damit Entscheidungen nachvollziehbar sind):**
- A ist im Kern mobile-first (Daumen-Navigation, große Ziele, „in 3 s lesbar") → geringste
  Reibung für den 70-%-Handy-Alltag. Architektur skaliert sauber zur Desktop-Variante.
- B ist am 27″-Schirm und bei Incidents überlegen (alles sichtbar ohne Tippen). Da A und B
  **dasselbe Datenmodell und dieselben Komponenten** teilen, ist B eine **Render-Variante**
  derselben Daten, kein zweites Produkt.
- C (⌘K) ist zu wertvoll, um sie wegzulassen, aber als **Accelerator** über A/B — nicht als
  alleinige Navigation (höchste Lernkurve, auf dem Handy am wenigsten intuitiv).

**Konkrete Umsetzungsidee in React:**
- Ein `useDensity()`-Hook (Werte `'airy' | 'compact'`), initialisiert aus `localStorage` und
  einem `matchMedia('(min-width: 1024px)')`-Default. Steuert, ob `<ShellAiry>` (Bottom-Tabs)
  oder `<ShellCompact>` (linke Rail) gerendert wird.
- Tab-Inhalte (`OverviewView`, `HermesFleet`, `OpenClawFleet`, `AutoresearchView`) sind
  **dichteunabhängig** und bekommen die Dichte per Context. Cards rendern in beiden Stufen
  dieselben Felder, nur mit anderem Padding/anderer Typo-Größe.
- `<CommandPalette>` global (Radix Dialog + cmdk o. ä.), getriggert per `⌘K`. Quelle der
  Befehle: Tabs, Sekundär-Nav, Aktionen (Verbesserungen holen, Alle übernehmen), plus
  durchsuchbare Worker/Agenten.

## 5. Tech-Stack der Ziel-Codebasis (aus dem Design-System)

Next.js 15.5 (App Router) · React 19 · **Tailwind 4 mit CSS-Variablen-Theming** · shadcn/ui
auf Radix · TypeScript · **SWR + SSE** für Live-Daten · **framer-motion** · recharts ·
**lucide-react** · sonner (Toasts) · `@hello-pangea/dnd`.

> ⚠️ **Animations-Hinweis (wichtig, aus dem Prototyping gelernt):** Inhalt **nie** hinter
> einer `opacity: 0`-Einblende-Animation verstecken, deren Endzustand erst durch eine
> laufende Timeline erreicht wird. Wenn die Timeline pausiert (Tab im Hintergrund,
> Screenshot-Renderer), bleibt der Inhalt unsichtbar. Mit framer-motion: `initial`-Werte so
> wählen, dass der Inhalt **ohne** abgeschlossene Animation lesbar ist (z. B. nur `y`-Offset,
> nicht `opacity`), oder `prefers-reduced-motion` sauber bedienen. Genauso: Overlays
> (Drawer/Sheet/Palette) per `display`/Mount toggeln, nicht nur per Opacity-Transition.

## 6. Informations-Architektur (fix)

**Haupt-Tabs (Reihenfolge fix):**
1. **Übersicht** — `/overview` — „Ist alles gesund?" auf einen Blick.
2. **Hermes-Worker** — `/hermes` — eigene Worker-Prozess-Flotte.
3. **OpenClaw-Worker** — `/openclaw` — zweite Agenten-Flotte, gleiche Optik.
4. **Autoresearch** — `/autoresearch` — One-Click-Verbesserungs-Flow (Herzstück).

**Sekundär-Nav** (dezent, Drawer / „Mehr"): Sessions, Kanban-Board, Modelle, Logs, Cron,
Skills, Konfiguration.

---

## 7. Echtes Datenmodell (Feldnamen & Enums 1:1 übernehmen)

> Diese Verträge sind verbindlich — Feldnamen, Status-Werte und Endpunkte exakt so verwenden.
> Im Prototyp sind sie in `hermes-data.js` mit realistischen Dummy-Daten + Helfern abgebildet
> (gute Vorlage für Typen + Ableitungslogik wie `workerHealth`).

### 7.1 Hermes-Worker — `GET /api/plugins/kanban/workers/active`
Antwort: `{ workers: Worker[], count, checked_at }`. Ein Worker = ein laufender Prozess:
```
run_id, task_id, task_title, task_status, task_assignee, profile, worker_pid,
started_at (epoch s), claim_lock, claim_expires (epoch s), last_heartbeat_at (epoch s),
max_runtime_seconds
```
- **profile** (Rolle): `default | admin | coder | devpower | dispatcher | kanbanops | planner | research | critic`
- **task_status**: `triage | todo | scheduled | ready | running | blocked | review | done | archived`
- **run_status**: `running | done | blocked | crashed | timed_out | failed | released`
- **run_outcome**: `completed | blocked | crashed | timed_out | spawn_failed | gave_up | reclaimed | iteration_budget_exhausted`
- Live-Prozessdetail `GET /runs/{run_id}/inspect`: `cpu_percent, memory_info.rss, num_threads, num_fds, status, create_time, cmdline, alive (bool)`
- **Ableitungen für die Card:** Laufzeit `= now − started_at`; Heartbeat-Alter `= now − last_heartbeat_at`;
  Rest-Zeit `= max_runtime_seconds − Laufzeit`.
- **Gesundheits-Logik (`workerHealth`)** — Reihenfolge der Prüfung:
  1. `run_status ∈ {timed_out, crashed}` **oder** `!inspect.alive` → **Offline** (Ton: zinc, LED grau)
  2. `run_status === 'blocked'` → **Blockiert** (Ton: red, LED rot pulsierend)
  3. `Heartbeat-Alter > 90 s` **oder** `claim_expires < now` → **Stuck** (Ton: amber, LED amber pulsierend)
  4. sonst → **Läuft** (Ton: cyan, LED grün „live")

### 7.2 OpenClaw-Agenten — `GET /api/openclaw/agents`
Antwort: `{ agents: AgentLive[], updatedAt }`.
```ts
AgentLive {
  id, name, emoji, status, model, lastActive,
  tasks: { queued: Task[], active: Task[], review: Task[], recentDone: Task[] },
  stuckSignal: boolean, activityPulse: number,
  fleetHealth: { currentTask, heartbeat, throughput, currentTool, lastOutput },
  roleLabel, roleSummary, escalationNote
}
Task { id, title, priority: 'high'|'med'|'low', progressPercent }
```
- **AgentStatus**: `active | monitoring | ready | idle | offline`
- **Echte Flotte** (id → emoji → Rolle): `main` 🦅 Orchestrator · `sre-expert` 🔧 SRE/Infra ·
  `frontend-guru` 🎨 UI · `efficiency-auditor` 🔍 Kosten/Audit · `spark` 🪄 Relief · `james` 🔬 Research
- **Visuelle Logik:** gesund (`active`/`monitoring`/`ready`) vs. `idle` vs. `offline` vs.
  **stuck** (`stuckSignal === true`) muss **sofort** unterscheidbar sein. `stuckSignal`
  überschreibt den Status optisch (amber + Eskalations-Callout aus `escalationNote`).
  Queue-Verteilung = Anzahl in `queued/active/review/recentDone`.

### 7.3 Autoresearch
**Status** `GET /autoresearch/status` (Schema `autoresearch-runner-status-v1`):
```
state: 'idle'|'running'|'stopping'|'crashed', pid, request_id, iteration, max,
last_step, last_eval, route_status ('configured' u.a.), heartbeat_age_s,
heartbeat_fresh (bool), last_receipt, last_run, note
```
**Vorschläge** `GET /autoresearch/proposals` → `Proposal[]`:
```
id, target (Skill-Name oder Code-Pfad), section, new_text, rationale_plain,
diff_before_after, mode: 'skill'|'code', status: 'proposed'|'applied'|'skipped'
```
- **Aktionen:** `POST /autoresearch/apply {id}` · `POST /autoresearch/skip {id}`
- Im Prototyp ist `diff_before_after` als Zeilen-Array `{ type: 'ctx'|'add'|'del', text }`
  modelliert → robustes, konsistentes Diff-Rendering (grün/rot). In Produktion ggf. echten
  unified-diff parsen, aber dieselbe Zeilen-Klassifikation rendern.

---

## 8. Screens / Views (Aufbau & Verhalten)

### 8.1 Übersicht (`/overview`) — „Ist alles gesund?"
**Zweck:** In ≈3 s erkennen, ob etwas Eingriff braucht.
**Pflichtinhalte:**
- **Gesundheits-Hero** ganz oben: eine große Aussage. Gesund → ruhiger Smaragd-Ton („Alles
  läuft ruhig"); Probleme → Amber-Ton mit Anzahl („N Signale/Dinge brauchen Aufmerksamkeit").
- **KPI-Zeile / Tiles:** Hermes laufen `running/total`, OpenClaw `aktiv/total`, **offene
  Vorschläge** (`proposals.status==='proposed'`), **Warnungen** (Summe stuck/blocked/offline
  + `stuckSignal`/offline-Agenten). Jede Kachel ist anklickbar → springt zum passenden Tab.
- **„Braucht Aufmerksamkeit"-Liste:** alle problematischen Worker/Agenten, jeweils mit
  LED/Emoji + Klartext-Grund, tippbar → Zielort.
- **Autoresearch-Teaser:** Loop-Status + „N Verbesserungen warten" + Sprung zu Tab 4.

### 8.2 Hermes-Worker (`/hermes`)
Liste/Raster aus **Worker-Cards**. Problematische zuerst sortieren (stuck/blocked > offline >
gesund). Card zeigt: Profil-Chip, `task_title`, Status-Pill + LED, Laufzeit, Heartbeat-Alter
(rot/amber wenn alt), Rest-Zeit (amber wenn ≤0), PID, **CPU- und RAM-Balken**, sowie bei
Problem einen Ton-gewaschenen Callout (`block_reason` bzw. „Heartbeat X alt · claim_expires
überschritten"). Aktionen: `Inspect`/`Details`, kontextuelle Primäraktion (`Dispatch` /
`Anstoßen` / `Lock lösen` / `Neu starten`).

### 8.3 OpenClaw-Worker (`/openclaw`)
**Agent-Cards**, gleiche Optik. Emoji-Avatar im Agent-Farbton (siehe Tokens), Name,
Status-Pill + LED, `roleLabel/roleSummary`, **aktuelle Aufgabe** (`fleetHealth.currentTask`),
Heartbeat, Throughput, Tool, Modell. Bei `stuckSignal`/offline: amber-Rand +
`escalationNote`-Callout. **Queue-Verteilung** als 4 Zähler: Wartet/Aktiv/Review/Fertig.

### 8.4 Autoresearch (`/autoresearch`) — Herzstück, hier am meisten investieren
- **Oben — Status-Zeile:** `state` + `heartbeat_fresh` (+ iter `iteration/max`, hb-Alter,
  route). Darunter **„Dein nächster Schritt"** in einfacher Sprache. Großer Primär-Button
  **„Verbesserungen holen"**. Global **„Alle übernehmen"**, wenn >1 offen.
- **Kern — Vorschlags-Cards.** Pro Card:
  - **Klartext-Titel** (z. B. „Fügt Abschnitt ‚Output' zum Skill ‚findmy' hinzu")
  - **„Warum"** aus `rationale_plain` (1–2 einfache Sätze)
  - **echter Vorher/Nachher-Diff** aus `diff_before_after` — grün (add) / rot (del),
    scrollbar, **auf dem Handy einklappbar** (in B mit Zeilennummern, in A/C ohne)
  - **Badge `mode`:** `skill` = normaler Ton (violett); `code` = **Warnfarbe (amber),
    höhere Stufe**, Hinweis „wird erst nach grüner Test-Suite scharf geschaltet"
  - **Buttons:** „Übernehmen" (Primär) / „Überspringen"
  - **Nach Apply** → `status='applied'`: Card wandert in **„Erledigt"** mit Ergebnis
    („✓ übernommen — Skill: eval grün" bzw. „Code: Tests grün")
- **Unten — Aktivitäts-Log** in Klartext (Zeitstempel + Ereignis, Ton-codiert).

---

## 9. Interaktionen, State & Verhalten
- **Tab-Wechsel:** Bottom-Tab (A) bzw. Rail (B) bzw. Segmented-Underline (C). Aktiver Tab in
  Akzentfarbe. Badge mit Anzahl offener Vorschläge auf dem Autoresearch-Tab.
- **Apply/Skip (lokaler State im Prototyp; in Produktion optimistisch + SWR-Revalidate):**
  `applied`/`skipped`-Mengen; nach Apply rückt die Card in „Erledigt".
- **„Alle übernehmen":** alle offenen (nicht übersprungenen) Vorschläge übernehmen.
- **Command-Palette (C):** `⌘K`/`Ctrl+K` öffnet/schließt; Filtern per Eingabe; ↑/↓ + ↵;
  Gruppen Navigation / Mehr / Aktionen / durchsuchbare Worker & Agenten. **Overlay per Mount/
  `display` toggeln** (nicht nur Opacity).
- **Tastatur (C):** in Listen `J/K` bewegen, `↵` Detail; im Autoresearch `A` übernehmen /
  `S` überspringen (jeweils oberster offener Vorschlag).
- **Live-Daten:** SSE/SWR; relative Zeiten in Kurzform (`3s`,`4m`,`2h`,`4d`). Zahlen immer
  **tabular-nums**.
- **Animationen:** subtil, diagnostisch, 150–250 ms; Status-LEDs pulsieren (live 2 s, warn
  3 s, error 1 s); `prefers-reduced-motion` respektieren. (Siehe Warnung in Abschnitt 5.)
- **Tweaks (im Prototyp pro Richtung):** Akzentfarbe, Dichte (luftig/kompakt = A↔B-Stufe!),
  Hell/Dunkel, Heartbeat-Puls an/aus (B), Tastatur-Hinweise an/aus (C). In Produktion als
  persistente User-Settings (localStorage) sinnvoll.

## 10. Design-Tokens
Vollständig in **`tokens.css`** (CSS-Variablen, dieselbe Quelle wie das Mission-Control-
Design-System — direkt in die Tailwind-4-`@theme`/Variablen-Schicht übernehmen). Kernwerte:
- **Flächen (zinc):** `--bg #0d0d0f` (mit zwei violetten Radial-Glows oben + vertikalem
  Verlauf — **nie flaches Schwarz**), `--panel #111`, `--panel-2 #141414`,
  `--panel-card #161b22`, `--border #1e1e1e`, `--border-strong #2a2a2a`.
- **Text:** `--text #f0f0f0`, `--text-soft #6b7280`, `--text-dim #374151`.
- **Akzent (einzige Marke): Violett `#7c3aed`** (`--primary`/`--accent`/`--ring`),
  Akzent-Text `#c4b5fd`, Wash `rgba(124,58,237,.15)`, Glow `0 0 12px rgba(124,58,237,.45)`.
  **Amber ist KEINE Markenfarbe — nur Status-Ton** (stalled/monitoring/review).
- **Status-Töne** (immer `border /20 + bg /10 + text-200|300`): emerald `#22c55e` ok/done ·
  cyan `#22d3ee` running · sky `#38bdf8` ready/waiting · indigo `#818cf8` picked · amber
  `#f59e0b` stalled/monitoring · rose `#f43f5e` incident/failed · red `#ef4444` blocked ·
  zinc `#52525b` idle/archive.
- **Agenten-Töne:** main/Atlas teal `#14b8a6` · sre-expert/Forge orange `#f97316` ·
  james emerald `#10b981` · efficiency-auditor/Lens yellow `#eab308` · frontend-guru/Pixel
  fuchsia `#d946ef` · spark pink `#ec4899` · Operator blau `#3b82f6`.
- **Typo:** Geist Sans (Body/Heading) + Geist Mono (Code/Zahlen), Features `tnum`+`ss01`
  immer an. Fluid-`clamp()`-Skala (16 px Body-Floor). Eyebrows: 10 px UPPERCASE, Tracking
  `0.18–0.26em`.
- **Radien:** sm 6 · md 8 · lg 10 · xl 14 · 2xl 18 · card 20 · pill 999. **Cards nie eckig.**
- **Hit-Targets:** mobil **nie < 44 px**; Bottom-Tab-Zellen ≥ 60 px. Safe-Area-Insets nutzen.
- **Schatten:** sparsam; Card `0 4px 24px rgba(0,0,0,.35)`, Hover `0 8px 32px rgba(0,0,0,.5)`
  + `translateY(-2px)`. Marken-Glow violett, Warn-Glow amber **nur** auf amber-Status.
- **Signatur-Stil:** Hairline-Borders + transluzente Farbwäschen statt voller Füllungen.

## 11. Assets
- `logo.svg` — Mission-/Hermes-Control-Mark (Settings-Gear, violett auf violett-Wash). In
  Produktion durch das eigene Marken-SVG ersetzen, falls vorhanden.
- **Icons:** lucide-react (Stroke 1.5, 16–18 px, `currentColor`). Emoji **nur** als
  Agenten-Marker, **nie** in Body-Copy.
- Keine Produktfotos/Illustrationen.

## 12. Dateien in diesem Bundle
- `richtung-a-ruhig-klar.html` — **Basis-Layout** (Bottom-Tab, luftig). Haupt-Referenz.
- `richtung-b-cockpit.html` — **Dichte-Stufe** (Rail + Top-Tabs, kompakt, LEDs, Terminal-Log).
- `richtung-c-command.html` — **Command-Palette** + minimalistische Variante.
- `Hermes-Control-Vergleich.html` — Vergleichsseite mit Begründung & Empfehlung.
- `hermes-data.js` — echtes Datenmodell als Dummy-Daten + Helfer (`workerHealth`, `overview`,
  `fmtAge`, `fmtDur`, Ton-Maps). **Gute Vorlage für TS-Typen + Ableitungslogik.**
- `tokens.css` — vollständige Design-Token-Schicht (CSS-Variablen).
- `logo.svg` — Marken-Mark.

## 12a. Screenshots (Ordner `screenshots/`)
Hochauflösende Referenz-Aufnahmen (mobile Breite, Dark-Default):
- **Richtung A:** `01-richtung-a.png` Übersicht · `02-richtung-a.png` Autoresearch (Diffs
  aufgeklappt) · `03-richtung-a.png` Hermes-Worker · `04-richtung-a.png` OpenClaw.
- **Richtung B:** `01-richtung-b.png` Übersicht (KPI + System-Matrix) · `02-richtung-b.png`
  Autoresearch (Terminal-Diff) · `03-richtung-b.png` Hermes-Worker (blocked/stuck/offline
  sichtbar) · `04-richtung-b.png` OpenClaw.
- **Richtung C:** `01-richtung-c.png` Übersicht · `02-richtung-c.png` Autoresearch ·
  `03-richtung-c.png` ⌘K-Command-Palette · `04-richtung-c.png` Hermes-Worker.

## 12b. React-Scaffold (`react-scaffold/`) — typsichere Übergabe-Schicht
Sauberer, sofort nutzbarer TS-Unterbau, der **exakt unserer Logik** folgt — damit die
Komponenten-Arbeit auf festem Boden steht (Details: `react-scaffold/README.md`):
- `src/lib/types.ts` + `schemas.ts` — Verträge als TS-Types **und** zod (Validierung an der
  fetch-Grenze).
- `src/lib/derive.ts` (+ `derive.test.ts`) — **reine, getestete** Logik: `workerHealth`
  (Schwelle 90 s / `claim_expires`), `buildOverview`, `fmtAge/Dur/MB/Clock`. Eine Quelle der
  Wahrheit statt drei HTML-Dateien.
- `src/lib/tones.ts` + `tokens.ts` + `styles/theme.css` — Token-Brücke zu Tailwind 4.
- `src/lib/diff.ts` — Unified-Diff → unser Zeilenmodell (rendert wie der Prototyp).
- `src/data/fixtures.ts` — echte Demo-Daten als typisiertes ESM (aus dem Prototyp portiert).
- `src/mocks/` — **MSW**-Handler (alle Endpunkte, apply/skip mutieren) + **SSE-Simulator** →
  Frontend ohne Backend baubar.
- `src/hooks/` — `useDensity` (die **A↔B-Stufe**), SWR-Daten-Hooks + optimistisches Apply/Skip.
- `src/components/contracts.ts` — Prop-Interfaces der geteilten Bausteine (A & B teilen sie).
- `src/i18n/de.ts` — deutscher String-Katalog (Operator-Tonalität).
- `src/lib/keymap.ts` — Tastatur-/A11y-Karte (⌘K, J/K, A/S).
- `package.deps.json` — erwartete Abhängigkeiten + Setup-Skripte.

> Implementierungs-Reihenfolge-Empfehlung: (1) Tokens + Shell A, (2) Datenmodell-Typen +
> SWR/SSE-Hooks, (3) die vier Views dichteunabhängig, (4) Dichte-Stufe B (Rail + kompakte
> Cards), (5) ⌘K-Command-Palette. Autoresearch-Diff-Cards zuerst hochwertig bauen — das ist
> der visuelle Schwerpunkt.

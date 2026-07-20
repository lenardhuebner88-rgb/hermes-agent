# Jarvis Greenfield-Redesign — „JARVIS OS" (2026-07-20)

Piet-Vorgabe (wortlaut): „greenfield dashboard – vollkommene Freiheit – bring mir ein
echtes Jarvis Gefühl". Auslöser (4 Mobile-Screenshots): Der Chat wird von
„Jarvis-Wächter"-Systemkarten geflutet (rohe Task-IDs, volle Receipt-Pfade,
Dopplungen gave_up → review_wait_attention → completed desselben Tasks), die
eigentliche Konversation geht unter. „Too much — ich kann damit nichts anfangen.
Wo muss ich Abschlüsse sehen?"

## Leitidee

**Der Assistent ist die Mitte. Die Maschine tritt in die Peripherie.**

Ein Bildschirm, drei Ebenen:

1. **Der Orb (Identität).** Lebendiges Emblem über dem Gespräch, Zustände:
   `idle` (ruhiger Puls) · `listening` (Mic) · `thinking` (Turn läuft) ·
   `speaking` (TTS) · `error`. Engine-Wahl bleibt am Orb (bestehender Switcher).
2. **Das Gespräch (Zentrum).** Nur Mensch ↔ Assistent. KEINE Wächter-Karten
   mehr als Chat-Bubbles. PlanSpec-Draft-Cards bleiben (Teil des Gesprächs).
3. **Die Peripherie (Maschinenraum, dezent).** Eine schlanke Zeile über dem
   Gespräch: Tages-Zähler + letzter deduplizierter Status
   („✓ 3 · ⚠ 1 · 👁 1 — zuletzt: ✓ Jarvis Mobile: APK paketieren, 14:33").
   Tap → bestehender Aktivitaet-Drawer (volles Log bleibt erreichbar, S3.10).

**Abschlüsse sieht Piet ab jetzt:** (a) in der Peripherie-Zeile (immer
sichtbar), (b) im Aktivitaet-Drawer (vollständig). Nicht mehr im Gespräch.

## Architektur-Entscheidungen

- **Presentation-Layer only.** `gateway/pa_watcher.py` und `pa_chat.py` bleiben
  unberührt (ehrliche Rohdaten). Wächter-Nachrichten sind an
  `engine === "pa-watcher"` erkennbar (`PaChatMessage.engine`, bereits im
  API-Typ) — kein Text-Parsing nötig für die Filterung, nur für die Zeilen.
- **Kein Rewrite der Verdrahtung.** `JarvisChat` behält alle Hooks
  (usePaChat/useMicRecorder/useSpeechPlayback/useLiveShare/Planspec/Inbox) und
  rendert neu: Orb-Header + PeripheryStrip + gefilterte Konversation + Composer.
  `JarvisShellView` = Ambience (Graph, gedimmt) + HUD-Toggle + Drawer.
- **Mock-Panels (Brain/Filter/KI-LAGE/Sparks, S1-Statik):** hinter HUD-Toggle,
  Default **aus** (localStorage `hermes.jarvis.hud`), Mobile gar nicht. Der
  Mock-Tag aus der S4-Härtung bleibt am Code für den HUD-Modus.
- **Mobile-first**, Desktop gleiche reduzierte Default-Ansicht.

## Neue Module (web/src/control/jarvis/)

### `watcherDigest.ts` (pure, + colocated Test)
```ts
export type WatcherState =
  | "completed" | "attention" | "blocked" | "gave_up" | "receipt" | "session" | "info";
export interface WatcherEvent {
  taskId: string | null;        // aus /t_[0-9a-f]{8}/
  state: WatcherState;          // aus "— completed" / "blocked:…" / "review_wait_attention"
                                // / "gave_up" / "Neues Receipt:" / "Agenten-Session beendet:" / sonst info
  title: string;                // Zeile ohne "- " und ohne "(Beleg: …)"
  ts: number;                   // Message-ts
}
export function parseWatcherEvents(messages: PaChatMessage[]): WatcherEvent[];
  // nur engine==="pa-watcher", Zeilen ab "- ", Headerzeile ("Jarvis-Wächter: …") überspringen
export interface WatcherDigest {
  latest: WatcherEvent[];       // latest-state pro taskId (sonst pro Titel), max N, neueste zuerst
  completedToday: number; attentionOpen: number; blockedOpen: number;
  lastEvent: WatcherEvent | null;
}
export function digestWatcherEvents(messages: PaChatMessage[], opts?: { max?: number }): WatcherDigest;
```
Dedupe-Regel: gleiche taskId (oder gleicher Titel) → nur das NEUESTE Event
behalten (gave_up/review_wait_attention verschwinden, sobald completed da ist).
Zähler über die deduplizierte Menge; attentionOpen = letzter Stand
review_wait_attention ohne späteres completed/blocked.

### `JarvisOrb.tsx` (+ Test)
Props: `state: "idle"|"listening"|"thinking"|"speaking"|"error"`, `engineLabel`,
`onEngineClick`. Reine CSS-Animation (Ringe/Core, `.jv-orb--<state>`), keine neue
Dependency. Labelzeile darunter: Engine-Switcher (bestehende Komponente).

### `PeripheryStrip.tsx` (+ Test)
Props: `digest: WatcherDigest`, `onOpenLog: () => void`. Eine Zeile:
`✓ {completedToday} · 👁 {attentionOpen} · ⚠ {blockedOpen}` + `lastEvent`-Kurzform
(Titel auf ~60 Zeichen gekürzt, Uhrzeit). `role="button"`, aria-label i18n.
Leerzustand: Strip rendert nichts.

## Umbau bestehende Dateien

- **`JarvisChat.tsx`:**
  - `const watcherMessages = messages.filter(m => m.engine === "pa-watcher")`,
    `const conversation = messages.filter(m => m.engine !== "pa-watcher")`.
    Liste rendert nur `conversation` (Bubbles/Draft-Cards wie bisher).
  - Über der Liste: `<JarvisOrb state=…>` (state aus
    `mic.status==="recording"→listening`, `activeTurn→thinking`,
    Speech-`playing`→speaking, `composerError/messagesError→error`, sonst idle;
    Priorität: error > listening > thinking > speaking > idle) + EngineSwitcher.
  - Darunter `<PeripheryStrip digest=… onOpenLog={öffne Aktivitaet-Drawer}>`.
    Der Drawer lebt in JarvisShellView — Strip-Callback via neuem kleinen
    Custom-Event `jarvis:open-aktivitaet` (window.dispatchEvent) statt
    Prop-Bohrung durch die Shell; Shell hört bereits auf URL-Param, ergänzt
    Listener. Einfachere Variante bevorzugt falls vorhanden.
  - Composer-Hint kürzen + CSS-Ellipsis (Screenshot: „guten Morge" abgeschnitten).
- **`JarvisShellView.tsx`:** Mobile (`@media (max-width: 900px)`): Panels
  `.jv-brainpanel/.jv-filter/.jv-news/.jv-sys` nicht rendern bzw. `display:none`
  + HUD-Toggle (kleiner Button am Rand, localStorage `hermes.jarvis.hud`,
  Default aus) für Desktop. Graph-Ambience bleibt, stärker dimmen (opacity).
- **`jarvis.css`:** Orb-Stile/Keyframes, `.jv-periphery`, `.jv-hudtoggle`,
  Panel-Hiding, Composer-Ellipsis.
- **`i18n/de.ts`:** neue Keys (periphery*, orb*, hudToggle).

## Nicht-Ziele
Kein Backend, kein neuer Endpoint, keine neuen Dependencies, kein
VAD/Streaming, kein Design-Token-Systemwechsel. Bestehende Drawer/Panels/
Live-Share/Planspec bleiben funktional.

## Akzeptanz
- Mobile-Screenshot-Befund behoben: Konversation ohne Wächter-Flut; Abschlüsse
  in Peripherie sichtbar; gleiche Daten im Aktivitaet-Drawer.
- `vitest run src/control/jarvis` grün (neue Tests: digest-Dedupe/Zähler,
  Orb-State-Mapping, Strip-Render/Leerzustand, Chat-Filterung).
- `tsc -b --noEmit` exit 0, `npm run lint:control` auf geänderten Dateien sauber.

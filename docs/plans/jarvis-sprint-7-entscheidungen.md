# Jarvis Sprint 7 — „Entscheidungen auf einen Blick" (2026-07-20)

Vorgänger: Sprint 6 (`jarvis-sprint-6-stimme-alltag.md`, abgeschlossen & live).
Piets Ziel für S7: **im Jarvis/Projekte-Tab schnell entscheiden können, ohne in
jede strategen-generierte PlanSpec im Detail zu schauen** — plus Backlog
durchziehen. Builder: dieselben drei, Zuordnung nach S6-Stärken (kimi-Wahl).

## Stränge-Modell (wie sich alles ergänzt)

1. **Ereignis-Strang** (beobachten): Wächter → Morgen-Briefing (S6.3) →
   Peripherie-Zähler. *Was ist passiert?*
2. **Entscheidungs-Strang** (handeln): Inbox → Wartet-dezent → Peripherie-Badge
   (S6.2) → Tap-Actions. *Was braucht mich?* — **S7.6 macht ihn
   entscheidungstauglich.** Dieselbe Titel-Destillation wie im Briefing (ein
   gemeinsamer Helper, keine Divergenz).
3. **Gesprächs-Strang** (fragen): Chat + Stimme + Kontextpack — **S7.1 gibt ihm
   Gedächtnis** (Tagebuch/memsearch) und Entscheidungs-Kontext („3 Freigaben
   warten"), damit Jarvis über beide anderen Stränge Auskunft geben kann.

## Datenlage (verifiziert 2026-07-20, data-first)
`/api/pa/inbox` (build_inbox, pa_chat.py:1144) liefert pro Item nur:
type (question/pa_action/held_task/freigabe_gate), id, title (roh, z.B.
„PlanSpec GATE-GREEN-KANBAN-LIFECYCLE-REGRESSION-FIX: Green-Gate-Ursachenfix:
die live-reproduzierten …"), status/freigabe, block_radius, ts (+category/
action_payload bei pa_action). **Keine** Kurzfassung, kein Risiko/Tier, keine
Kettengröße — genau Piets Pain. `briefing_title()` in gateway/pa_watcher.py
macht bereits Titel-Destillation (S6.3) — wird S7 gemeinsamer Micro-Helper.

## Tasks & Builder

### Grok 4.5 (Backend-Stärke) — Worktree codex-jarvis-s7-grok
- **S7.1 Kontext-Tiefe:** `build_context_pack` (pa_chat.py:714) bekommt eine
  geboundete `memory`-Sektion (Tagebuch/memsearch — S3.9-Bestand untersuchen,
  `grep -rn tagebuch hermes_cli/ plugins/ cron/`) plus `pending_decisions`
  (Anzahl + Kurztitel der freigabe_gates/held_tasks aus build_inbox-Quelle).
  Hartes 14k-Zeichen-Budget respektieren (CONTEXT_PACK_MAX_CHARS), Quelle
  isoliert-fehlertolerant wie die anderen Sektionen. Tests.
- **S7.3 Proaktiv II (pa_watcher.py):** Abend-Rückblick (eine Karte 21:00,
  Fenster seit 07:30, gleiche Kuratierung wie Morgen-Briefing) +
  Inbox-Aging-Eskalation: 👁-Items älter als 24h erzeugen ein Warning-Event
  (bestehender Signifikanz-Pfad, Dedupe via fingerprint). Tests nach Muster.
- **S7.6-Backend:** Titel-Destillation als Micro-Modul `hermes_cli/pa_titles.py`
  (aus pa_watcher.briefing_title extrahiert; pa_watcher importiert von dort —
  kein Zirkel: pa_watcher importiert bereits pa_chat), und `build_inbox`
  ergänzt `summary` (destillierter Titel, ≤80 Z.) pro Item. Tests.

### Qwen 3.8 Preview (Frontend) — Worktree codex-jarvis-s7-qwen
- **S7.6-Frontend Decision-Cards:** InboxPanel + WartetPanel rendern
  entscheidungs-first: Zeile 1 = `summary` (Fallback: clientseitige
  Destillation wenn Feld fehlt — kleiner ts-Helper `decisionTitle.ts`),
  Badges: 🔑 freigabe=operator, Typ, Alter („seit 3d" aus ts), block_radius>0
  als „blockiert N"; Roh-Titel + Details hinter Expand (bestehendes details-
  Idiom). Keine neuen API-Felder anfassen außer `summary` im api.ts-Typ
  (optional field). Tests: Badge-Logik, Fallback-Destillation, Alter.
- **S7.5 Asset-Fix:** `api.paAssetUrl` token-sicher (Session-Token als
  Query-Param wie andere fetchJSON-Flows — Muster in api.ts prüfen), damit
  `<img>` ohne Cookie keine „Bild nicht mehr verfügbar"-Chips zeigt. Test.
- Dateien: InboxPanel.tsx, WartetPanel.tsx (+Tests), api.ts (minimal!),
  i18n/de.ts, jarvis.css (Card-Abschnitt). NICHT: JarvisChat.tsx (GPT),
  pa_chat.py/pa_watcher.py (Grok).

### GPT 5.6 (eng gescoped — S6-Limit beachten!) — Worktree codex-jarvis-s7-gpt
- **S7.2 Voice UX II:** a) PTT-Auto-Send-Option: Toggle (localStorage
  `hermes.jarvis.ptt_autosend`, Muster Vorlese-Toggle) — nach erfolgreicher
  Transkription direkt senden statt nur in den Input; Default AUS.
  b) Barge-in: Mic-Start stoppt laufendes Vorlesen (useSpeechPlayback.stop
  existiert). Dateien: useMicRecorder.ts, useSpeechPlayback.ts, JarvisChat.tsx
  (NUR Mic/Speak-Verdrahtung), i18n/de.ts, Tests. NICHT: InboxPanel,
  WartetPanel, api.ts, Backend.
- **Prozess-Vorgabe (aus S6 gelernt):** Gates FRÜH und nach JEDEM Teilschritt
  fahren; bei Limit-Risiko lieber kleinere Commits im Arbeitsbaum und Summary
  zwischenschreiben.

### kimi (parallel): S7.4 Erreichbarkeit
PWA-Install-Führung + Push-Kanal-Status prüfen (S3.2-Bestand), ggf. kleiner
Fix direkt; Ergebnis ins Receipt.

## Prozess
Wie S6: Builder-Specs pro Worktree, kein Commit durch Builder, kimi reviewed
jeden Diff + fährt Gates unabhängig nach, Integration sequentiell, Deploy +
Live-Verifikation + Discord-Report. Datei-Partitionierung strikt einhalten
(siehe NICHT-Listen) — sonst Merge-Reibung.

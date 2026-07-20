# Jarvis — Follow-ups & Sprint-8-Backlog (2026-07-20)

Nach Sprint 6+7 (beide live). Diese Punkte sind bewusst **noch nicht gebaut** —
Dokumentation für Follow-ups, priorisiert nach Risiko pro Aufwand.
Quellen: Schwachstellen-Analyse + Receipts
`2026-07-20-jarvis-s6-orchestration-receipt.md`,
`2026-07-20-jarvis-s7-orchestration-receipt.md`.

## 🔴 Prio 0 — Risiko, sofort

### F1 pa.db-Backup-Timer (Jarvis-Gedächtnis)
- **Befund (verifiziert 2026-07-20):** Backups laufen für Vault
  (vault-autosync), memsearch, FO-Supabase, Health-Track — **nicht** für
  `~/.hermes/pa/pa.db` (Turns, Feed, Inbox-Historie, Journal-Verweise).
  Disk bei 86 %. rclone-Remotes (`onedrive:`, `gdrive:`) existieren.
- **Maßnahme:** systemd-user-Timer (Muster memsearch-backup.timer): täglich
  sqlite-Backup von pa.db (+ pa/journal-Dateien) → rclone-Remote, Retention
  14 Tage, Discord-Alarm nur bei Rot. Aufwand ~30 Min.
- **Owner:** kimi (Haupt-Agent, systemd+Secrets-Nähe). Kein Builder nötig.

### F2 Builder-Sandboxing / Env-Scrubbing
- **Befund:** Headless-Builder (grok/qwen/hermes CLIs) liefen S6/S7 mit
  `bypassPermissions`/yolo und vollem Home-Zugriff — inkl. lesbarem
  `~/.hermes/.env` (36 Provider-Keys). Bei Kanban-Workern existiert
  Key-Stripping (`_spawn_claude_worker`), bei Inline-Buildern nicht.
  Prompt-Injection über Repo/Web-Inhalt wäre ein Exfil-Vektor.
- **Maßnahme:** Builder-Spawn-Wrapper (scripts/): Env-Scrubbing
  (Provider-Keys raus, außer der Builder-eigene), optional `--sandbox`
  (qwen) / sandbox-exec; Konvention ins Orchestrierungs-Playbook.
- **Owner:** Grok 4.5 (Backend/Infra-Stärke) oder kimi.

## 🟡 Prio 1 — Haltung vor neuen Features

### F3 Daily-Driver-Tag (bewusster Soak)
- Kein Bauen, nur Benutzen: Morgen-Briefing 07:30 + Abend-Karte 20:55
  bewerten (ist der Report „sinnvoll und gut"?), Diktat via Groq, Qwen-
  Engine im Alltag, PTT-Auto-Send ausprobieren. Friktion sammeln → erst
  dann Sprint 8 final zuschneiden.
- **Owner:** Piet + kimi-Protokoll.

### F4 Mobile-Oberflächen konsolidieren
- **Befund:** Drei Surfaces: PWA (Jarvis-Tab), `android/hermes-voice`,
  `android/hermes-dictate`. Unklar, welche „die Jarvis-App" ist.
- **Maßnahme:** Entscheidungsvorlage (kurz): PWA als die eine Oberfläche,
  native Apps als Zusatz (Voice/Dictate als Input-Brücken) oder umgekehrt —
  mit Migrationspfad. Erst analysieren (was können die nativen, was die
  PWA nicht?), dann Piet-Entscheid.
- **Owner:** kimi Analyse → Piet Go.

## 🟢 Prio 2 — Tiefe

### F5 Engine-Qualitäts- & Kosten-Feedback
- Vier Engines (sol/claude/kimi/qwen), keine Antwort auf „welche antwortet
  gut / was kostet sie". €100-Token-Plan ohne Verbrauchssicht.
- **Maßnahme:** Turn-Log um Bewertung (👍/👎 pro Bubble) + Kosten-Schätzer
  pro Engine (Token-Preise aus .firecrawl/*) in der Peripherie/Statistik.
- **Owner:** Qwen 3.8 (Frontend) + Grok (Backend-Turn-Log).

### F6 Decision-WHY (Entscheidungs-Begründung)
- S7.6 zeigt WAS (destillierter Titel), nicht WARUM (Ziel des PlanSpecs,
  Konsequenz bei Ablehnung). Daten liegen in den PlanSpec-Dateien (Vault).
- **Maßnahme:** `build_inbox` um `goal` (1 Zeile aus PlanSpec-Frontmatter/
  Body) erweitern; Card-Expand zeigt Ziel + „bei Ablehnung". Data-first:
  Frontmatter-Felder vorher verifizieren.
- **Owner:** Grok (Backend) + Qwen (Card-Expand).

### F7 Kalender-/Personen-Kontext
- Kontextpack ist faktisch (Kanban/Receipts), nicht persönlich. FO hat
  Kalenderdaten. „Was steht heute an" kann Jarvis nicht.
- **Maßnahme:** FO-Kalender read-only ins Kontextpack (gebundet, heute+
  morgen), später Personen-Gedächtnis. Schnittstelle zuerst prüfen
  (FO-API/DB, kein neuer Silo — bestehende Interfaces).
- **Owner:** Grok, nach F3-Soak.

### F8 Circuit-Breaker im Jarvis-Tab
- Auto-Release scharf, Autonomie wächst; Kill-Switches verteilt
  (Config-Zeilen, systemctl). Kein Ein-Knopf-Halt im Tab.
- **Maßnahme:** Orb/Peripherie: „Alles anhalten" (setzt bekannte
  Kill-Switches atomar: release.autonomous=false, Strategen-Timer stop,
  Kanban-Dispatcher pause) + Status-Anzeige der Schalter. Read-First:
  welche Schalter existieren (Config, Timer, Flags).
- **Owner:** Grok (Backend-Endpoint) + Qwen (UI).

## 🔵 Prio 2 — Strategen-/Entscheidungs-Pipeline (Befund 2026-07-20, Live-
Analyse der 4 wartenden freigabe_gates; Empfehlung an Piet: t_c35430a7
freigeben, t_e28ab9bb nach Gusto, beide GATE-TRIAGE-Duplikate ablehnen)

### F9 Duplikat-Erkennung auf Themen-Ebene
- **Befund (live):** Zwei identische GATE-TRIAGE-PYTHON-PlanSpecs held
  (`t_79001b8a` dc5ed369, 0,7d + `t_3d396ebd` a57e7ba8, 1,7d). Die
  Fingerprint-Dedup greift nicht, weil sich das rote Test-Datei-Set ändert
  → neuer Fingerprint → neue Spec. Ironie: die neuere Spec verlangt selbst
  „Duplicate-PlanSpec-Rate muss 0 bleiben".
- **Maßnahme:** Dedup auf Thema/Slice-Präfix (z.B. `GATE-TRIAGE-PYTHON`)
  statt nur Content-Fingerprint; bei wechselnder Ursache bestehende HELD-
  Spec aktualisieren statt neue zu öffnen.
- **Owner:** Grok (strategist/triage-check Pipeline).

### F10 Supersede-/Withdraw-Logik für Strategen-Vorschläge
- **Befund:** Der Stratege zieht überholte eigene HELD-Vorschläge nicht
  zurück. Die ältere Triage-Spec war durch die neuere (und durch die
  konkrete gate-green-Spec) faktisch ersetzt, blieb aber 1,7d in der Inbox.
- **Maßnahme:** Beim Propose prüfen: existiert eine HELD-Spec zum selben
  Thema? → alte als `status: ersetzt` markieren (aus Inbox nehmen) oder
  in-place aktualisieren. Nie zwei HELD zum selben Thema.
- **Owner:** Grok.

### F11 Entscheidungs-Ranking in der Inbox
- **Befund:** Alle 4 Items standen gleichwertig da (Sortierung nur
  block_radius + ts). Die Kern-Vertrauens-Entscheidung (green-gate streak=0,
  22 Fail-Nächte) war von Duplikat-Rauschen nicht unterscheidbar.
- **Maßnahme:** Ranking-Signal: `strategist_meta.roi`/`target_metric` in
  Score übersetzen (Kern-Signal betroffen > Verbesserung > Meta-Arbeit),
  Inbox sortiert/badged danach. Hängt an F6 (WHY auf der Karte).
- **Owner:** Grok (Score) + Qwen (Badge/Sortierung).

### F12 Evidence-Frische auf Decision-Cards
- **Befund:** Die gate-green-Spec behauptete 3 rote Tests (Grounding
  20.07.); Nachmessung: 2 noch rot, 1 zwischenzeitlich grün. Karten zeigen
  weder Alter der Beweislage noch deren Halbwertszeit.
- **Maßnahme:** Card zeigt „Beleg vom <datum>" + Verfallshinweis ab N
  Tagen („Evidence älter als 3d — vor Freigabe neu prüfen?"). Optional:
  Re-Verify-Button (führt die Grounding-Kommandos erneut aus).
- **Owner:** Grok (Backend-Feld) + Qwen (Anzeige).

## Erledigt heute (Referenz, nicht mehr offen)
S6: Groq-STT live, Morgen-Briefing, Integrator-Härtung, Panels live,
Frame-Age, Voice-Pipeline-E2E, Tap-Actions, Datums-Trenner.
S7: Decision-Cards (summary+Badges), Asset-Token-Fix, Gedächtnis im
Kontextpack, Abend-Briefing + Inbox-Aging, PTT-Auto-Send + Barge-in,
PWA-Theme, Push-Verifikation, PlanSpec-Slug-Fix, Graph-Flake-Härtung.

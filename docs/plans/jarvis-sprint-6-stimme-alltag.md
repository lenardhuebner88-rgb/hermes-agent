# Jarvis Sprint 6 — „Stimme & Alltag" (2026-07-20)

Vorgänger: Sprint-5-Vorbereitung (`jarvis-sprint-5-vorbereitung.md`) + Redesign
(`jarvis-redesign.md`). Piets Zielbild: **„ein echter Jarvis — mobiler Assistent,
desktop optimal, erster Ansprechpartner für alles."** Builder dieses Sprints:
**Grok 4.5** und **GPT 5.6** (Piet-Entscheidung 2026-07-20, ersetzt Codex-Zuordnung
aus dem Sprint-5-Dok).

## Stand nach Integration (kimi, 20.07.)
- `codex/jarvis-s4-haertung` (10 Bugfixes + JARVIS-OS-Redesign) und
  `codex/jarvis-s5-qwen` (Qwen-Engine qwen3.7-plus, opt-in) sind auf `main`
  gemergt, Gates grün, deployed.
- Sprint-5-Task A (Qwen-Engine) damit **erledigt**. Offen aus Sprint 5: B (Panels
  live), C (UX-Feinschliff), D (Integrator-Härtung), E (STT/TTS-Config).

## Reihenfolge-Änderung gegenüber Sprint-5-Plan (kimi-Empfehlung, Piet ok)
Integration zuerst (✅), dann **Stimme** (der mobile Gamechanger), dann B/C.
Zusätzlich zwei neue Tasks aus dem „echter Jarvis"-Zielbild: Tap-Actions und
Wächter-Briefing.

## Vorlauf (kein Builder-Task — Credentials/Piet-Gate)
- **S6.0 STT/TTS-Config** (kimi + Piet): `stt.provider: groq` + TTS-Voice in
  `~/.hermes/config.yaml` setzen (Groq-Key-Lage prüfen, ~/.hermes/.env). Danach
  Live-Proof: eine Diktat-Runde + ein Vorlesen auf Piets Phone. Sprint-5-Task E.

## Tasks

### S6.1 Voice-Pipeline-Verifikation — **GPT 5.6**
Scope: `hermes_cli/web_server.py` (/api/audio/transcribe, /api/audio/speak),
`tools/tts_tool.py`, `tests/**`.
Inhalt: E2E-Tests der Audio-Endpunkte mit Mock-Providern (Erfolg, Provider-Ausfall,
Timeout, Oversize-Upload); Provider-Ketten-Fallbacks; nach S6.0 Live-Verifikation
der Groq-Verkabelung. Akzeptanz: Tests grün, Fehler landen sichtbar im Redesign-
Composer (nie still), ruff+affected grün.

### S6.2 Tap-Actions mobil — **GPT 5.6**
Scope: `web/src/control/jarvis/` (InboxPanel, JarvisChat), `jarvis.css`.
Inhalt: pa_action-Approvals als One-Thumb-Cards im Redesign (große
Approve/Reject-Targets ≥48px, safe-area, Expand für Reason/Payload); Inbox-Badge
in der Peripherie-Zeile. Akzeptanz: Approval komplett mobil mit einer Hand
bedienbar; vitest+tsc grün; visueller Shot 390px im Receipt.

### S6.3 Wächter 2.0 — Morgen-Briefing — **Grok 4.5**
Scope: `gateway/pa_watcher.py`, `tests/gateway/test_pa_watcher*`, optional
Frontend-Karte.
Inhalt: Statt N Einzel-Bundles eine verdichtete Briefing-Karte (Fenster
konfigurierbar, Default: über Nacht seit 21:00 → Zustellung 07:30, Quiet-Hours
respektieren): Abschlüsse ✓, Blocker ⚠, wartet-auf-dich 👁, max 8 Zeilen, dedup
pro Task. Einzel-Bundles tagsüber bleiben (Rate-Limit existiert). Akzeptanz:
Tests für Fenster/Dedupe/Quiet-Hours; Piet sieht morgens EINE Karte.

### S6.4 Panels live (Sprint-5-Task B) — **Grok 4.5**
Scope: `web/src/control/jarvis/` (ShellView, mockContent), ggf. `web_server.py`.
Inhalt: KI-LAGE-Panel an `GET /api/pa/feed`; Filter/Top-Hubs aus dem bereits
gepollten Graph ableiten; Sparks aus System-Stats. Danach entfallen die Mock-Tags
für diese Panels (HUD-Modus wird echt). Akzeptanz: parseOrThrow/zod-Muster der
Control-Datenschicht; vitest+tsc grün.

### S6.5 UX-Feinschliff (Sprint-5-Task C) — **Grok 4.5**
Scope: `web/src/control/jarvis/`.
Inhalt: Frame-Age-Indikator am Live-Share; effektive Engine des nächsten Turns am
Composer; Datum bei älteren Bubbles; Focus-Trap im Inbox-Drawer. Akzeptanz:
vitest+tsc grün, keine Regression im Redesign.

### S6.6 Integrator-Härtung (Sprint-5-Task D) — **Grok 4.5**
Scope: `scripts/`, `hermes_cli/kanban_worktrees.py`, Merge-Gate.
Inhalt: ENOSPC-Ursache im Merge-Gate (Disk-Budget/Worktree-Hygiene) beheben,
Vitest-Timeout-Flakes unter Last isolieren — killt den Revert/Re-land-Churn
(t_1ccb0734 ×4). Akzeptanz: Gate läuft unter Last stabil; Churn-Historie im
Receipt referenziert.

## Prozess pro Task
Worktree-Disziplin (canonical root `/home/piet/.hermes/worktrees/`), Koordinations-
Check-IN vor erstem Write, nur affected Tests (`scripts/run-affected.sh`), Frontend-
Gates über Live-`node_modules/.bin`, keine Vollsuite (Nachtlauf). Worker pushen/
deployen nicht — Übergabe an kimi zur Integration. Builder-Lanes: Grok-Tasks über
die grok-Lane (grok 4.5), GPT-Tasks über die sol-Lane (gpt-5.6-sol).

## Offene Piet-Entscheidungen
1. S6.0-Vorlauf: Groq-Key vorhanden/freigegeben? TTS-Voice-Wahl.
2. Qwen bleibt opt-in (beschlossen) — nach einer Woche Praxis: Default-Kandidat?
3. Morgen-Briefing 07:30 ok? Push dazu aufs Phone (Kanal existiert, S3.2)?

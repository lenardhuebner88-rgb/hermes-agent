# Jarvis Sprint 5 — Vorbereitung & Härtung (2026-07-20)

Status: Vorbereitung abgeschlossen, Härtungs-Patch in Arbeit (Worktree
`codex-jarvis-s4-haertung`). Erstellt von kimi (Review + Analyse), Bau-Tasks für
Codex und Grok 4.5 spezifiziert unten.

---

## 1. Sprint-Review: Was S3/S4 geliefert hat

Landung auf `main`, 15 Dateien / 147 Vitest-Tests im Jarvis-Bereich **grün** (Stand 2026-07-20).

- **Live-Screenshare (dominant, 6 der letzten 15 Commits):** echter kontinuierlicher
  `getDisplayMedia`-Share ersetzt Einzel-Frame-Picker (t_842e4dcc), Mobile-Image-Fallback
  (t_1ccb0734), Cleanup-Race-Fix für verwaiste Backend-Sessions (t_339054d6), nativer
  Android-MediaProjection-Bridge `window.HermesNative` (t_bd5c02e3), Fail-closed-Härtung
  „kein grüner Share ohne nutzbaren Frame" (7d35d2496).
- **S3.6:** PTT-Mic → `/api/audio/transcribe` in den Composer (kein Auto-Send) +
  Vorlese-Toggle → `/api/audio/speak` (persistiert in localStorage).
- **S3.7 / S3.3-FE:** Screenshare-Frame → PA-Vision-Turn; `/plan <idee>` PlanSpec-Draft-Cards
  → Propose in Inbox.
- **S3.1/S3.2/S4 (Backend):** `gateway/pa_watcher.py` Feed, Web Push (S24-Proof),
  Reminders als gated action.

**Prozess-Befund:** t_1ccb0734 und t_842e4dcc brauchten je 3–4 Revert/Re-land-Zyklen —
Ursache war die **Integrations-Umgebung** (ENOSPC im Merge-Gate, Vitest-Timeouts), nicht
der Feature-Code. Das ist Sprint-5-Task D.

**Architektur-Befund (gesund):** Engine-Switching ist real verdrahtet, nicht half-wired:
`JarvisChat → usePaChat → POST /api/pa/message {engine?, model?} → turn_id → Poll
GET /api/pa/turns/{id}`. Roster aus `ENGINE_REGISTRY` in `hermes_cli/pa_chat.py:70-82`
(sol/claude/kimi), jeder Turn = stateless One-Shot-Subprocess. Das Roster-Design ist die
vorgesehene Erweiterungsstelle für Qwen (s. §3).

## 2. Härtungs-Patch (läuft, Worktree `codex-jarvis-s4-haertung`)

Gefixt werden (Review-Befund, priorisiert):

| # | Befund | Datei |
|---|---|---|
| 1 | Unlabelter Mock-Content als „live" HUD (Brain-Stats, KI-LAGE-Fake-News, Filter, Top-Hubs, Sparks) — Graph-Fallback ist korrekt gelabelt, Nachbarn nicht | `JarvisShellView.tsx`, `mockContent.ts` |
| 2 | `messagesError` aus `usePaChat` wird nie gerendert — stiller History-Ausfall | `JarvisChat.tsx` |
| 3 | `speakError` (TTS) wird nie gerendert | `JarvisChat.tsx`, `useSpeechPlayback.ts` |
| 4 | Native Live-Share kann ewig in „starting" hängen (kein Watchdog) | `useLiveShare.ts` |
| 5 | Ein einzelner Frame-Upload-Fehler killt den ganzen Share → Budget von 3 aufeinanderfolgenden Fehlern | `useLiveShare.ts` |
| 6 | Mic-Doppelklick während Permission-Dialog leakt Stream/Recorder | `useMicRecorder.ts` |
| 7 | Blob-URL-Leak im finalize-Early-Return | `usePaChat.ts` |
| 8 | Tote Exports (nur verifiziert ungenutzte) | `graphMock.ts`, `engineSelection.ts`, u.a. |
| 9 | Engine-Wahl geht bei Reload verloren → localStorage (wie Speak-Toggle) | `engineSelection.ts` |
| 10 | `alt=""` auf inhaltstragenden Anhang-Thumbnails | `JarvisChat.tsx` |

Explizit **nicht** im Patch: Live-Verdrahtung der Mock-Panels, VAD/Streaming-Audio,
STT/TTS-Provider-Config (Piet-Gates laut Receipt).

## 3. Qwen als Jarvis-Sprachmodul — Bewertung

**Kurz: Ja, tragfähige Alternative. Empfehlung: `qwen3.7-plus` als 4. Engine.**

### Katalog-Stand (heute)
- Terminal-Agent-Katalog: `qwen` in `_AGENT_KINDS` (`hermes_cli/agent_terminals.py:52`),
  Binary-Resolution vorhanden. Frontend-Spiegel `web/src/lib/api.ts:456`.
- Modell-Katalog (`hermes_cli/models.py`): OpenRouter/Nous: `qwen3.7-max`, `qwen3.7-plus`,
  `qwen3.6-35b-a3b`; `alibaba` (DashScope) + `alibaba-coding-plan`: u.a. `qwen3.7-max`,
  `qwen3.6-plus`, `qwen3-coder-plus`.
- Provider-Infra: `plugins/model-providers/qwen-oauth/` (nutzt lokalen Qwen-CLI-Login
  `~/.qwen/oauth_creds.json`), HermesOverlays `qwen-oauth`/`alibaba`/`alibaba-coding-plan`
  in `hermes_cli/providers.py`.

### Kosten (OpenRouter-Preise aus `.firecrawl/qwen3-*.json`, €100 ≈ $108)

| Modell | Blend $/M (3:1 in:out) | Tokens für $108 | ~Kosten/Jarvis-Turn* | Turns für €100 |
|---|---|---|---|---|
| **qwen3.7-plus** (1M ctx, **Vision**) | 0.70 | ~154 M | ~$0.0027 | **~40.000** |
| qwen3.7-max (1M, text-only) | 1.88 | ~58 M | ~$0.008 | ~13.500 |
| qwen3.6-plus | 0.73 | ~148 M | ~$0.003 | ~36.000 |
| qwen3.5-flash | 0.11 | ~950 M | ~$0.0004 | ~250.000 |

\* Turn ≈ 5,5k Input (Kontextpack + Historie + Frage) + ~300 Output-Tokens.

**Wichtig:** Ist der €100-Plan das **Alibaba-Coding-Plan-Abo** statt PayGo-Guthaben,
gilt Request-Quota statt Token-Budget — dann Route `alibaba-coding-plan` und die
Token-Mathematik entfällt. → **Offene Frage an Piet.**

### Warum qwen3.7-plus für Jarvis passt
- **Vision:** Live-Screenshare/Bild-Uploads funktionieren — aktuell kann das nur `sol`,
  claude/kimi nicht. Qwen wäre die einzige Alternative mit Bildern.
- 1M Kontext: Kontextpack (14k Zeichen) + Historie trivial.
- Das `pa_action`-Fenced-Block-Protokoll braucht kein natives Function-Calling.
- Deutsch solide (PA-Systemprompt verlangt deutsche Kurzantworten).
- Nicht empfohlen: qwen3.7-max (teurer, kein Vision), Coder-Modelle (oversized).

### Integration (Sprint-5-Task A, ~30–60 Zeilen, Muster = `build_kimi_argv`)
1. `hermes_cli/pa_chat.py`: `QWEN_MODEL`-Konstante + `EngineSpec` in `ENGINE_REGISTRY`
   + `build_qwen_argv` (Qwen-CLI: `qwen -p <prompt> -m qwen3.7-plus` — OAuth-Login
   existiert bereits). Alternativ ohne neues Binary: `hermes chat -m qwen/qwen3.7-plus`
   über den sol-Adapter.
2. `supports_images=True`. Kein Frontend-Code nötig (Roster-getrieben); optional Label
   in `engineSelection.ts:MODEL_LABELS`.
3. Tests nach Muster der bestehenden pa_chat-Engine-Tests (Validierung, Bild-Pfad, Timeout).
4. Vorher am installierten Binary verifizieren: Qwen-CLI-Prompt-Mode-Flags/Output-Format
   (Fallstrick wie bei Kimi 0.27, siehe Kommentar in `build_kimi_argv`).

## 4. Sprint 5 — Task-Spezifikationen

| Task | Wer | Scope | Inhalt |
|---|---|---|---|
| **A: Qwen-Engine** | Codex | `hermes_cli/pa_chat.py`, `tests/**/test_pa_chat*` | Engine-Spec + argv-Builder + Tests nach §3. Binary-Flags vorher verifizieren. |
| **B: Panels live** | Grok 4.5 | `web/src/control/jarvis/`, ggf. `web_server.py` | KI-LAGE an `GET /api/pa/feed`; Filter/Top-Hubs aus bereits gepolltem Graph ableiten; Sparks aus System-Stats. Mock-Labels entfallen dann für diese Panels. |
| **C: UX-Feinschliff** | Grok 4.5 | `web/src/control/jarvis/` | Frame-Age-Indikator am Share (eingefrorener „grüner" Share sichtbar); effektive Engine des nächsten Turns am Composer anzeigen; Datumsanzeige bei älteren Bubbles; Focus-Trap im Inbox-Drawer. |
| **D: Integrator-Härtung** | Codex | `scripts/`, Merge-Gate | ENOSPC-Ursache im Merge-Gate beheben (Disk-Budget/Worktree-Hygiene), Vitest-Timeout-Flakes isolieren — killt den Revert/Re-land-Churn. |
| **E: Offene Reste** | (nach Piet-Gates) | `~/.hermes/config.yaml`, `tools/tts_tool.py` | STT `stt.provider: groq` + TTS-Voice konfigurieren (Piet-Gate, Credentials); S2-Rest-Proofs (answer_source, image→vision E2E). |

Prozess pro Task: Worktree-Disziplin, nur affected Tests (`scripts/run-affected.sh`),
Frontend-Gates über Live-`node_modules/.bin`, keine Vollsuite (Nachtlauf). Task A und D
sind backend-/infra-seitig und können parallel zu B/C laufen; B baut auf dem
Härtungs-Patch auf (Mock-Labels), C ebenfalls.

## 5. Offene Entscheidungen für Piet

1. €100-Plan: **DashScope-PayGo oder Alibaba-Coding-Plan-Abo?** (entscheidet Route)
2. Soll Qwen nach Task A **Default-Engine** werden oder opt-in im Switcher bleiben?
3. STT/TTS-Provider-Freigabe (Groq-Key, Voice-Wahl) — blockiert Task E.

# PlanSpec: /lanes → Modell-Plattform (Greenfield-Redesign)

- **Datum:** 2026-07-24 · **Agent:** Claude-Code/Fable (Orchestrator + Design + Review) · **Branch:** `claude/lanes-model-platform` (Worktree `.claude/worktrees/claude-lanes-platform`, forked von main 578a8b363)
- **Goal (Piet, /goal):** /lanes wird zur Plattform für Modell-Auswahl: (a) Reasoning-Einstellung pro Profil wo unterstützt, (b) nur erreichbare/sinnvolle Modelle, (c) Smoke-Tests mit sauber darstellbaren Ergebnissen, (d) Extra-Feature = Entscheidungshilfe Modell→Lane. Design greenfield, "top of the notch". Done = funktionierende E2E-Tests + Design-Mockup + alles dokumentiert im isolierten Worktree + Handoff via Discord.

## IST-Zustand (Audits 2026-07-24, Beleg: lane_routes.py / api.ts / Audits)

- **Backend** `plugins/kanban/dashboard/lane_routes.py` (1461 Z., gemountet `/api/plugins/kanban`): Lane-CRUD + `activate` (hot, Dispatcher liest aktive Lane pro Spawn — kein Restart) + `persist` (atomar Profil-config.yaml + Lane-Blob, Snapshot-Rollback) + `spawn-check` + `auth-smoke` (Subprozess `hermes agent --profile … "Reply with token"`, strukturierte Ergebnisse: status/duration_ms/observed_*/error_class, Summary mit `recommended_next_action`) + `openrouter-models/import`. Modell-Katalog `_lane_model_catalog` merged claude-cli-IDs + `inventory.build_models_payload` (Provider-level `authenticated`/`configured`) + Remote-Katalog + Profil-Defaults. **Live: 200 Modelle, 10 Profile, 3 Lanes** (`api-standard` aktiv).
- **Reasoning:** `agent.reasoning_effort` pro Profil-config (live: coder=medium, reviewer/critic/premium/research=high, verifier/scout=medium). Transport-Pass-through belegt für: OpenAI-compat (`extra_body.reasoning={enabled,effort}`), Kimi (`reasoning_effort` + `extra_body.thinking`), Gemini (`thinking_config`), Anthropic (adaptive `output_config.effort`, Claude 4.6+), LM Studio, Tencent. **Grok/Qwen/alibaba-token-plan: keine Transport-Verzweigung → kein Reasoning-Knopf (ehrlich).**
- **Frontend:** `LanesView.tsx` (1568 Z., Monolith) + `views/lanes/api.ts` (746 Z., self-contained Client + pure Helper: `editorRows`, `applyChoice`, `profilesFromEditorRows`, `laneEntryWarnings`, Eskalations-Logik). Bewusste Parallel-Session-Isolation: lokale Strings (nicht i18n/de.ts), lokales State-Pattern (nicht useControlData). Tab nur in `moreTabs` (nicht primär/mobil).
- **E2E:** Playwright `web/e2e/` (`watchPage()`/`assertClean()`-Pattern, 2 Projekte desktop 1440 + mobile 390, `PLAYWRIGHT_BASE_URL` überschreibbar, kein webServer-Spawn). `scripts/visual-verify.sh` = sandboxierte HERMES_HOME-Instanz aufphemeral Port + Screenshots + Console-Error/Overflow-Assert.

## Lücken → Slices

| Feature | Existiert | Neu (Slice) |
|---|---|---|
| (a) Reasoning pro Profil | Config-Key, Transport-Pass-through, Werte live gesetzt | **S1:** `LanePersistProfileEntry.reasoning_effort`, persist schreibt `agent.reasoning_effort`, Katalog liefert `reasoning_effort`+`reasoning_support` pro Profil/Modell |
| (b) Nur erreichbare/sinnvolle Modelle | Provider-`authenticated` (inventory), curated Katalog | **S1:** Modell-Optionen += `authenticated`/`configured`/`probe`; **S2:** Default-Filter "sinnvoll+erreichbar", Toggle für Rest (200→~25) |
| (c) Smoke-Tests | auth-smoke (Rollen-scope), Latenz-Messung, Cost-Lookup | **S1:** `POST /lanes/model-probe` (Modell-scope) + `POST /lanes/catalog-probe` (Batch, sequential, limit 8) + Probe-Cache (`~/.hermes/cache/lanes_model_probes.json`); **S2:** Ergebnis-Feed mit StatusChips + KPI-Summary |
| (d) Modell→Lane-Empfehlung | Capabilities-Flags, Preise (models.dev), Reasoning-Support | **S1:** Zutaten im Katalog (Preis, Kontext, reasoning_support); **S2:** `fit.ts` pure Scoring (Rollen-Anforderungsprofil × Modell-Eigenschaften × Probe-Evidenz) + "Lane-Kompass"-Panel + Bench-Vergleich (2–4 Modelle, gleicher Prompt → side-by-side) |

## API-Vertrag (gepinnt — S1 + S2 bauen dagegen)

```
GET /lanes  (erweitert)
  profiles[]:  + reasoning_effort: str|null          # aktuelles agent.reasoning_effort
               + reasoning_support: string[]         # Werte für das Profil-Default-Modell
  models[]:    + authenticated: bool, configured: bool
               + price_in_per_mtok_usd: float|null, price_out_per_mtok_usd: float|null
               + context_window: int|null
               + reasoning_support: string[]         # [] = nicht unterstützt
               + probe: ModelProbeResult|null        # letzter gecachter Probe

LanePersistProfileEntry += reasoning_effort: str|null
   null = unverändert · "" = leeren (Config-Default greift) · sonst Wert ∈ reasoning_support des Zielmodells, sonst 400 {error, profiles}

POST /lanes/model-probe   {provider, model, profile="coder", timeout_seconds=45 (cap 120)}
  → ModelProbeResult {provider, model, profile, status: ok|fallback|auth_error|quota_or_rate_limit|timeout|config_error|error|skipped,
                      duration_ms, observed_provider?, observed_model?, error_class?, reason?, at(epoch)}
POST /lanes/catalog-probe {models:[{provider,model}], profile?, timeout_seconds=45, limit=8 (cap 16)}
  → {results: ModelProbeResult[], truncated: bool}   # sequential, jeder Call capped

reasoning_support-Regeln (statisch, Backend-SoT):
  openai-codex/openai gpt-5.x → [minimal,low,medium,high] · claude-*/anthropic → [low,medium,high]
  moonshot/kimi → [low,medium,high] · google/gemini → [low,medium,high] · openrouter → [low,medium,high]
  xai/grok, alibaba-token-plan/qwen, neuralwatt → [] (keine Transport-Verzweigung = kein Knopf, ehrlich)
```

## Greenfield-Design (innerhalb DESIGN.md "Bronze auf Graphit" — Mockup = Anker, S2 baut dagegen)

```
Puls-Leiste (shared): Masthead LANES + Worker/Inbox/Kosten/Gateway
┌──────────────────────────────────────────────────────────────────────┐
│ A. LANE-LEISTE: Karten pro Lane (api-standard ● AKTIV bronze-LED,     │
│    max-abo, Premium) + [Neue Lane] — Klick=Activate (Confirm inline)  │
├─────────────────────────────────┬────────────────────────────────────┤
│ B. PROFIL-MATRIX (62%, Table):  │ C. RECHTE SEITE (38%), Subtabs:     │
│  Rolle (RoleChip+Desc) · Modell │  ┌─ RAUCH ──────────────────────┐  │
│  (Select, gefiltert) ·          │  │ Katalog messen [Batch] +     │  │
│  Reasoning-Segment              │  │ Einzel-Probes; Ergebnis-Feed │  │
│  (Standard|minimal|low|medium|  │  │ ListRows: StatusChip+LED,    │  │
│  high, disabled+Hint wenn []·   │  │ Latenz/Cost mono; KPI-Tiles: │  │
│  Fallbacks (Drawer) · Probe-    │  │ X/Y erreichbar · p50 · Σ$    │  │
│  Latenz mono + Health-LED ·     │  └──────────────────────────────┘  │
│  Override-Badge vs. Default     │  ┌─ KOMPASS ────────────────────┐  │
│  [Speichern] [Verwerfen] arm.   │  │ Rollen-Subtabs → Top-5 Fit:  │  │
│                                 │  │ MeterBar-Score + Grund-Chips │  │
│                                 │  │ (412ms · $2.50/1M · Reason✓) │  │
│                                 │  │ [Übernehmen]=staged in Matrix│  │
│                                 │  │ Bench: 2-4 Modelle →Vergleich│  │
│                                 │  └──────────────────────────────┘  │
└─────────────────────────────────┴────────────────────────────────────┘
Tablet (600-839): Matrix voll, C darunter gestapelt. Mobil (<600): Lane-Leiste
Pills horizontal-scroll, Matrix-Rows als Cards, Rauch/Kompass via DrawerShell.
```

**Doktrin (binding):** Bronze NUR interaktiv/live (aktive Lane, Primary-CTA, Focus) · nie Chip. Status-Trio nur semantisch, immer LED+Label. Provider-Identität = `data-1..7` (stabil pro Provider gehasht) — nie Status/Akzent. Mono NUR Daten (Latenz, $, Modell-IDs, Timestamps); Eyebrows/Mastheads = Archivo. Surfaces: 0 Canvas / 1 Panel / 2 Card / 3 Hover-only. Keine Roh-Hex (Ratchet-Gate `check:tokens`). Empty-States: Situation→Bewertung→Aktion, ink-2/3, kein ok-Grün auf neutral. Motion 120–160ms ease-out, reduced-motion kill switch. Touch ≥44px mobil. leitstand-Primitive: `SectionHeader`, `KpiTile`, `SubtabChips`, `DrawerShell`, `ListRow`, `StatusChip`, `MeterBar` — keine Neu-Erfindung.

## E2E-Strategie ("wirklich funktionieren", keine Tautologien)

1. **Backend-pytest (S1):** reale Endpoint-Logik, Subprocess gemonkeypatcht, Reasoning-Persist gegen tmp-Profilconfig, Validierung gegen REALE Katalog-Shape.
2. **Vitest (S2):** `fit.ts` + Filter/Choice-Helper gegen **REALE Live-Fixture** (`__fixtures__/lanes-live.json`, captured 2026-07-24, 200→~60 Modelle getrimmt) — Assert auf realem Payload, nie `vi.fn()`-Selbstreferenz (Canon: placebo-fix/tautological-test).
3. **Playwright (S3):** `web/e2e/lanes-platform.spec.ts` mit `watchPage()`/`assertClean()`; fährt ephemere Sandbox-Instanz an (`HERMES_SANDBOX_MODE=1`, eigenes HERMES_HOME, `PLAYWRIGHT_BASE_URL`): (i) /control/lanes rendert clean (0 Console-Errors, kein Overflow) desktop+mobil, (ii) Reasoning-Interaktion → persist-Request-Body-Assert (REALER Vertrag), (iii) Probe-Flow via `page.route` gegen dokumentierte Result-Shape (UI-E2E deterministisch; echte Probe-Logik deckt pytest), (iv) Filter reduziert 200→sichtbar. Sandbox stubbt KEINE Modell-Calls → echte Probes im E2E nur optional/getaggt (Token-Kosten).
4. **Visuell:** `scripts/visual-verify.sh --output-dir … /control/lanes` (390/820/desktop PNGs + summary.json, non-zero bei Console-Error/Overflow).

## Orchestrierung (Foreign-Builder-Doktrin)

- **S1 Backend → Codex** (gpt-5.6-sol, effort medium): existing-code-heavy, House-Style, Config-Writes. Review: Grok (2. Familie, read-only) + Main-Modell fresh-eyes + verifier-Gates.
- **S2 Frontend → kimi-builder** (K3): Frontend-Stärke, Brief mit Mockup-Anker + exaktem Vertrag. Review: Main-Modell fresh-eyes + ui-verifier visuell + verifier-Gates.
- **S3 E2E:** nach Integration, grok-builder (scharf geschnitten) oder Main bei Kontextkopplung.
- **S4 Mockup/Doku/Discord:** Mockup = Main-Modell (Design-Entscheid), Doku scribe, Versand `hermes send -t discord "MEDIA:…"`.
- **Mergeback:** NEIN (Goal = Worktree-Handoff). Branch bleibt `claude/lanes-model-platform`, Commits clean, Handoff-Doc `docs/handoff/2026-07-24-lanes-model-platform.md`.
- **Shared-Dateien:** kein Touch von i18n/de.ts, lib/types.ts, lib/schemas.ts, ControlShell.tsx, ControlPage.tsx, web_server.py, kanban_db.py (Parallel-Session-Schutz; Lanes-Isolations-Pattern wird fortgeführt).

## Risiken

1. **200-Modell-Katalog-Performance:** Filter/Grouping client-seitig, kein zusätzlicher Poll (Load + manuell + post-mutation reload).
2. **Probe-Kosten:** Batch auf limit≤16, timeout cap 120s, sequential; UI warnt vor Batch-Kosten (moderate Prompts "Reply with token").
3. **Reasoning-Ehrlichkeit:** unsupported = disabled Control mit Grund, nie fake-Enabled (Grok/Qwen heute ohne Transport-Knopf).
4. **Merge-Drift:** 0 Shared-Files toucht → Mergeback trivial; lanes/-Ordner bleibt self-contained.

---

## Addendum 2026-07-24 00:5x — sinnvoll-Regel, Routing, Verifikation (ohne Deploy)

### sinnvoll-Regel (verfeinert nach Live-Audit)
Live-Katalog hat **200 Modelle**, aber die meisten sind Bulk-Inventar/OpenRouter-Rauschen; `auth.json` enthält z.B. `nous`-Credentials, die der Operator nie nutzt. „Sinnvoll" ≠ „Credential vorhanden". Backend berechnet pro Modell 3 boolesche Felder:
- `used_in_profiles` = Provider ist `model.provider` in irgendeiner `~/.hermes/profiles/*/config.yaml` (live: nur `alibaba-token-plan` + `openai-codex`).
- `admitted` = Modell-ID steht in `model_catalog.providers.<p>.extra_models` der `~/.hermes/config.yaml` (bewusst zugelassen; live: neuralwatt 13, alibaba-token-plan 10, openrouter 3).
- `sinnvoll` = `runtime=="claude-cli"` ODER `used_in_profiles` ODER `admitted` ODER Modell/Provider in irgendeinem Lane-Blob. Sonst `false`.
Default-Filter im UI = `sinnvoll && erreichbar` (erreichtbar = probe ok/fallback oder ungeprüft). Toggle „Alle (200)" zeigt den Rest grau (nous/unzugelassen). Erwartet ~38 sichtbare Modelle statt 200.

### Tatsächliches Routing (Operator-Override 2026-07-24)
- **Backend S1 → Codex** (gpt-5.6-sol, effort medium) — existing-code-heavy, Config-Writes. Läuft.
- **Frontend S2 build → Qwen 3.8 Max Preview** (`claude-qwen -p`, Alibaba Token Plan, headless CC-Harness) — bewusster Trial. Phasen-Commits A–D.
- **Frontend S2 review → Qwen 3.8 Preview** (read-only `-p` Review-Lauf gegen den Diff).
- **Abschluss-Review (integrierter Diff) → GPT-5.6** (`codex:codex-rescue`, read-only).
- **Kein Kimi K3, kein Opus/integrator.** Main-Modell (ich) = Orchestrierung + Design + fresh-eyes-Review + Lander. ui-verifier + verifier-Gates wie üblich.
- **Fallback bei Qwen-Fehlschlag:** 1 Retry mit konkreten Findings; bleibt Qwen kaputt → Eskalation an Operator (kein stilles Lane-Switching wg. Restriktion); `builder`(sonnet) nur als letzte Option mit Operator-Vermerk (sonnet ≠ Opus, also nicht explizit verboten, aber nicht im Sinne des Trials).

### Verifikation OHNE Deploy / ohne Live-:9119-Berührung
Die neuen Endpoints/der neue UI-Code sind NICHT auf dem Live-Dashboard. Verifikation gegen den Worktree-Code:
1. **Gates im Worktree:** `scripts/gate-frontend.sh` (Frontend) + `pytest tests/plugins/kanban/` + `ruff` (Backend).
2. **Visual AC:** `scripts/visual-verify.sh --output-dir /tmp/lanes-ac /control/lanes` — bootet eine sandboxierte `hermes serve`-Instanz aus dem Worktree-Code auf ephemeral Port (disposable HERMES_HOME), rendert 390/820/desktop PNGs + summary.json, non-zero bei Console-Error/Overflow.
3. **Interaktions-E2E:** `web/e2e/lanes-platform.spec.ts` (Playwright) gegen eine analoge Sandbox-Instanz (`PLAYWRIGHT_BASE_URL` = ephemeral Port, `HERMES_SANDBOX_MODE=1`), gebaut aus dem Worktree (`web/dist` nach `npm run build`). Damit laufen NEUE Endpoints + NEUER UI live zusammen. Sandbox stubbt KEINE Modell-Calls → echte Probes im E2E nur optional/getaggt; UI-Flow-Asserts via `page.route` gegen dokumentierte Result-Shape + Request-Body-Asserts gegen den echten Persist-Vertrag.

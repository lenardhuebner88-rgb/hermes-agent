# Handoff: /lanes → Modell-Plattform (Greenfield-Redesign)

**Datum:** 2026-07-24 · **Branch:** `claude/lanes-model-platform` · **Worktree:** `~/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform` (forked von main `578a8b363`)
**Status:** 🚧 in Arbeit (Backend S1 = Codex, Frontend S2 = Qwen 3.8 Preview; beide laufen). Mergeback = Operator-Entscheidung (nicht automatisch).

## TL;DR
Der `/lanes`-Tab wurde vom Modell-Preset-Cockpit zur **Modell-Plattform** greenfield neu entworfen — in der binding Design-Sprache „Bronze auf Graphit". Vier Features:
1. **Reasoning pro Profil** — Segment-Control pro Editor-Row, nur wo der Provider/Modell es unterstützt (Qwen/Grok ehrlich deaktiviert mit Begründung), wird atomar mit dem Modell via `persist` in die Profil-`config.yaml` geschrieben (`agent.reasoning_effort`).
2. **Nur sinnvolle + erreichbare Modelle** — Default-Filter reduziert 200 → ~38 Modelle (`sinnvoll` = genutzt/zugelassen; `nous` ungenutzt ausgeblendet), Toggle für den Rest; Erreichbarkeit = Modell-Probe-Evidenz.
3. **Smoke-Tests mit sauberem Ergebnis-Feed** — `model-probe`/`catalog-probe` (Modell-scope, Subprozess-Reuse), StatusChips + KPI-Tiles + gecachte Latenz/Kosten.
4. **Kompass = Entscheidungshilfe Modell→Lane** (Extra-Feature) — Rollen-basiertes Fit-Ranking (pure `fit.ts`: Probe-Latenz × Preis × Reasoning × Capabilities) + Bench-Vergleich gleicher Prompt; verwoben mit den Smoke-Evidenzen statt Bolt-on.

## Design-Anker (verbindlich)
- Mockup-HTML: `docs/design/lanes-plattform-mockup.html` (4 Artboards: Desktop Rauch, Desktop Kompass, Mobil 390, Zustands-Vokabular).
- Render-PNGs: `docs/design/lanes-mockup-renders/lanes-mockup-ab{1..4}.png`.
- PlanSpec: `docs/plans/2026-07-24-lanes-model-platform.md`.
- **Discord-Checkpoint** gesendet 2026-07-24 00:5x an discord-home (chat_id `1500203113867378789`) — 4 Artboards + Richtungsfrage vor dem Frontend-Build.

## API-Vertrag (Backend liefert, Frontend konsumiert — neue Felder alle optional/additiv)
- `GET /api/plugins/kanban/lanes`:
  - `profiles[]` += `reasoning_effort`, `reasoning_support`
  - `models[]` += `authenticated`, `configured`, `price_in_per_mtok_usd`, `price_out_per_mtok_usd`, `context_window`, `reasoning_support`, `probe`, `sinnvoll`, `used_in_profiles`, `admitted`
- `POST /api/plugins/kanban/lanes/model-probe` `{provider, model, profile?, timeout_seconds?}` → `ModelProbeResult`
- `POST /api/plugins/kanban/lanes/catalog-probe` `{models[], profile?, timeout_seconds?, limit?}` → `{results[], truncated}`
- `POST /api/plugins/kanban/lanes/persist`: `LanePersistProfileEntry` += `reasoning_effort` (null=unverändert, ""=leeren, sonst validiert gegen `reasoning_support`)
- `reasoning_support`-Regeltabelle: gpt-5.x→[minimal,low,medium,high]; claude/kimi/gemini/openrouter→[low,medium,high]; xai/alibaba-token-plan/qwen/neuralwatt→[] (keine Transport-Verzweigung).

## Backend-Änderungen (S1, Codex)
- Datei: `plugins/kanban/dashboard/lane_routes.py` (+ Tests `tests/plugins/kanban/...`).
- Neu: Reasoning-Persist, `model-probe`/`catalog-probe` + Probe-Cache (`~/.hermes/cache/lanes_model_probes.json`), Katalog-Metadata + sinnvoll-Regel.
- Tests gegen reale Fixture-Shape; Subprozess gemonkeypatcht.
- Commits: _<SHA-Liste folgt>_ · Gate (`pytest tests/plugins/kanban/ -q` + `ruff`): _<Evidenz folgt>_

## Frontend-Änderungen (S2, Qwen 3.8 Preview)
- Dateien: `web/src/control/views/LanesView.tsx` (ersetzt) + `web/src/control/views/lanes/{api.ts erweitert, fit.ts, SmokePanel.tsx, Compass.tsx, ReasoningControl.tsx, ModelSelect.tsx, providerColors.ts, *.test.tsx}`.
- Shared-Files **NICHT** angefasst (i18n/de.ts, lib/types.ts, lib/schemas.ts, ControlShell.tsx, ControlPage.tsx, hooks/useControlData.ts, web/src/lib/api.ts) — Parallel-Session-Schutz + Merge-Sicherheit.
- Commits (Phasen A–D): _<SHA-Liste folgt>_ · Gate (`scripts/gate-frontend.sh`): _<Evidenz folgt>_

## Review-Kette (Provenienz)
| Slice | Builder | Reviewer (2. Blick) | Final |
|---|---|---|---|
| S1 Backend | Codex (gpt-5.6-sol) | Qwen 3.8 Preview + Main fresh-eyes + verifier | GPT-5.6 (integriert) |
| S2 Frontend | Qwen 3.8 Preview | Qwen 3.8 Preview + Main fresh-eyes + ui-verifier + verifier | GPT-5.6 (integriert) |

## Verifikation (ohne Deploy, ohne Live-:9119-Berührung)
```bash
cd ~/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform
# Frontend-Gate (voll):
bash scripts/gate-frontend.sh
# Backend-Gate:
HERMES_TEST_FILE_TIMEOUT=120 PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/kanban/ -q
/home/piet/.hermes/hermes-agent/venv/bin/ruff check plugins/kanban/dashboard/lane_routes.py
# Visual AC (sandbox-serve aus Worktree-Code):
bash scripts/visual-verify.sh --output-dir /tmp/lanes-ac /control/lanes
# Interaktions-E2E (Sandbox-Instanz, PLAYWRIGHT_BASE_URL=ephemeral):
cd web && PLAYWRIGHT_BASE_URL=<port> npm run e2e -- e2e/lanes-platform.spec.ts
```
Ergebnisse: _<folgen nach Build-Completion>_

## Mergeback-Notizen
- **0 Shared-Files** geändert → Merge nach main ist trivial konfliktfrei (lanes/-Ordner self-contained).
- **Follow-ups (nicht in diesem Scope, bewusst):**
  - Nav-Ökonomie: `/lanes` liegt weiter in `moreTabs` (kein primärer/Mobile-Slot). Promotion = Design-Board-Entscheidung (DESIGN.md: Geschmacks-/Dichte-Änderung braucht 2 Varianten + Operator-Wahl) — NICHT heimlich geändert.
  - Dispatcher-native Lane-scope-Reasoning (Reasoning im Lane-Blob statt Profil-Config) nur falls ein Profil in zwei Lanes unterschiedliches Reasoning braucht — heute nicht der Fall.
  - `nous`/ungenutzte Provider: bereits durch `sinnvoll` default ausgeblendet; echte „zuletzt genutzt"-Heuristik (Run-History) optional später.

## Offene Fragen an Piet
- Reicht die „sinnvoll"-Heuristik (genutzt/zugelassen) oder soll „zuletzt in Runs genutzt" (kanban run-history) als 4. Signal rein?
- Probe-Kosten-Limit für „Katalog messen" akzeptabel bei 8 Mini-Prompts pro Klick?
- Soll `/lanes` nach Abschluss in die Primär-Nav/Mobile-Bottom-Bar (Design-Board-Varianten nötig)?

## Coordination
- Check-IN: `/home/piet/vault/_agents/_coordination/2026-07-24_0023_claude-code-lanes-model-platform.md`. Check-OUT folgt bei Abschluss.
- Receipt: `/home/piet/vault/03-Agents/Claude-Code/receipts/...` folgt.

# Handoff: /lanes → Modell-Plattform (Greenfield-Redesign)

**Datum:** 2026-07-24 · **Branch:** `claude/lanes-model-platform` · **Worktree:** `~/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform` (forked von main `578a8b363`)
**Status:** ✅ gebaut · gates grün · E2E grün · Design belegt · 3× GPT-5.6 closed-loop reviewed · dokumentiert. **Mergeback = Operator-Entscheidung** (nicht automatisch; kein Push/Deploy erfolgt).

## TL;DR
`/lanes` greenfield vom Preset-Cockpit zur **Modell-Plattform** in der binding Design-Sprache „Bronze auf Graphit". Vier Features gebaut + belegt:
1. **Reasoning pro Profil** — Segment-Control pro Editor-Row, nur wo Provider/Modell es transportiert (Qwen/Grok/claude-cli ehrlich deaktiviert mit Begründung); schreibt atomar via `persist` in `config.yaml` (`agent.reasoning_effort`, dotted-roundtrip → `model:`-Block bleibt).
2. **Nur sinnvolle + erreichbare Modelle** — Default-Filter 200 → curated (`sinnvoll` = genutzt/zugelassen/claude-cli; `nous` ungenutzt ausgeblendet), Toggle „Alle (N)"; Erreichbarkeit = Probe-Evidenz; claude-cli aus Probes/Bench/Smoke-CTA konsistent ausgeschlossen.
3. **Smoke-Tests mit sauberem Ergebnis-Feed** — `model-probe`/`catalog-probe` (Modell-scope, Subprocess-Reuse + Cache), StatusChips (LED+Label) + KPI-Tiles (hermes-probeable Set) + Empty-State-Doktrin.
4. **Kompass = Entscheidungshilfe Modell→Lane** (Extra-Feature) — Rollen-Fit-Ranking (pure `fit.ts`: Latenz × Preis × Reasoning × Capabilities; KNOWN-unreachable ODER failed-Probe = Score 0; claude-cli korrekt von der authenticated-Klausel ausgenommen) + Bench-Vergleich gleicher Prompt; „Übernehmen" staged (Save bestätigt), respektiert `locked`.

## Design-Anker (belegt)
- Greenfield-Mockup: `docs/design/lanes-plattform-mockup.html` (4 Artboards: Desktop Rauch, Desktop Kompass, Mobil 390, Zustands-Vokabular).
- Real-Build-Render (E2E gegen echtes seeded Worktree-Backend): `docs/design/lanes-mockup-renders/lanes-e2e-{expanded,compact}-chromium-{desktop,mobile}.png`.
- Mockup-Render: `docs/design/lanes-mockup-renders/lanes-mockup-ab{1..4}.png`.
- PlanSpec `docs/plans/2026-07-24-lanes-model-platform.md`; Build-Briefs `docs/handoff/briefs/s2-frontend-qwen.md`, `…/s2-hardening-codex.md`, `…/s2-hardening-r2-codex.md`.
- **Discord:** Design-Checkpoint (4 Artboards) gesendet an discord-home (chat_id `1500203113867378789`); Final-Closeout mit Real-Rendern folgt bei Abschluss.

## API-Vertrag (gebaut, backend↔frontend abgeglichen)
- `GET /api/plugins/kanban/lanes`: `profiles[]` += `reasoning_effort`,`reasoning_support`; `models[]` += `authenticated`,`configured`,`price_in/out_per_mtok_usd`,`context_window`,`reasoning_support`,`probe`,`sinnvoll`,`used_in_profiles`,`admitted` (alle additiv/optional).
- `POST …/lanes/model-probe` / `POST …/lanes/catalog-probe` → `ModelProbeResult` / `{results[],truncated}`.
- `POST …/lanes/persist`: `LanePersistProfileEntry.reasoning_effort` (null=unverändert, `""`=leeren) + `fallback_providers[].base_url`; `LanePersistBody.removed_profiles` (pop-after-merge, Overlap mit `profiles` erlaubt → Config-Reasoning-Clear + Lane-Entry-Entfernung in einem Save).

## Commits (Branch `claude/lanes-model-platform`)
| SHA | Slice |
|---|---|
| `b3210dbf1` | S1 Backend: reasoning persist + model/catalog probes + catalog metadata |
| `91614cdbf` | S1 addendum: sinnvoll/admitted/used_in_profiles |
| `d33fc9e0c` | S1 hardening (Orchestrator): inventory-cache reset + persist-merge assertion |
| `c0ca9fd9b` | Docs/Design: Mockup + 4 Artboard-Render + PlanSpec + Handoff-Skeleton + Build-Brief |
| `ab610bd6b`/`5c50e3cc7`/`458c88077` | S2 Phase A/B/C: matrix+reasoning+filter / smoke+probes / compass+bench |
| `7328d5c8f` | E2E: isolated Playwright (real seeded backend) + Real-Build-Render |
| `a64d1b752` | Hardening R1 (F1–F9): touched-tracking, fallback-preserve, activate-before-persist, locked-disable, claude-cli probe exclusion, compass probe-gate, retryable save, responsive headers |
| `9908cc329` | Hardening R2 (R1–R9): full-map quick-switch, `removed_profiles` clear, `base_url`, lock-adopt, bench/smoke claude-cli, no-op fallback, failure reload |
| `055aea6a0` | Hardening R3: quick-switch keep-set over-inclusion fix + failure-reload revert (closed-loop) |
| _pending_ | Docs final: dieses Handoff + Receipt + finale Render + Briefs |

## Review closed-loop (GPT-5.6, cross-family; read-only, Throwaway-Worktree `/tmp/codex-lanes-review`)
- **Review #1** (vs `7328d5c8f`): **block** — 6 P1 + 4 P2 im Save/Probe/Compass-Pfad, die grüne Gates + 4/4 E2E + Happy-Path-Review nicht abdeckten (keine Schicht übte die Save-Edge-States). → Hardening R1.
- **Review #2** (vs `a64d1b752`): **block** — R1 quick-switch (replace löscht Siblings), R4 clear-override (merge kann nicht löschen), R6 adopt-ignoriert-lock, R7/R9/R5/R3. → Hardening R2 (inkl. Backend `removed_profiles` + Overlap-Semantik).
- **Review #3** (vs `9908cc329`): **block**, aber konvergiert — bestätigt R3/R4-Ordinary/R5/R6/R7/R9/`unknown_removed`/`updateLane`=lane-blob-only als **korrekt geschlossen**; 4 schmale Funde, davon 2 **vom Orchestrator-Brief in R2 eingeführte Regressionen** (F3-3 over-inclusive keep-set; F3-2 catch-reload verwirft staged edits) → in **R3 vom Orchestrator selbst gefixt** (verifiziert per Reasoning + gates + E2E). Die verbleibenden 2 = **seltene tiefe Edge-Cases** → als Follow-ups dokumentiert (s.u.), KEIN 4. Review-Lauf (Circuit-Breaker: Konvergenz auf seltene Konfigurationen; eigene Regressionen behoben, Headline-Korruption geschlossen).
- Main-Modell fresh-eyes lag über jedem Schritt zusätzlich vor (Reviewer ≠ Builder für beide Slices; Codex baute Backend, Qwen baute Frontend, Orchestrator = cross-family zu beiden).

## Verifikation (Evidenz, unabhängig vom Worker reproduziert)
- `bash scripts/gate-frontend.sh` → **GRÜN**: lint:control → `tsc -b --noEmit --force` → **vitest 2753 passed / 199 files** → build → Token-Ratchet (keine Roh-Hex in .tsx/.ts).
- Backend `pytest tests/plugins/kanban/dashboard/test_lane_model_platform.py` → passed (incl. reasoning-merge-no-clobber, persist update-one+remove-another, overlap clears reasoning + removes lane entry, reasoning_support-Regeltabelle).
- `bash scripts/lanes-e2e.sh` → **4 passed** (desktop+mobile × Expanded+Compact) gegen echtes seeded Worktree-Backend; `watchPage.assertClean` = 0 console.error / 0 4xx-5xx; Probe-POSTs route-gemockt (deterministisch, keine Kosten), Catalog/Lanes real.
- Real-Render visuell geprüft: F9 Header-Overlap behoben; F5 claude-cli „ungeprüft"; F6 Kompass gated failed-Probes; Reasoning enabled/disabled ehrlich.

## Selbst nachbauen (ohne Deploy / ohne Live-:9119)
```bash
cd ~/.hermes/hermes-agent/.claude/worktrees/claude-lanes-platform
bash scripts/gate-frontend.sh
HERMES_TEST_FILE_TIMEOUT=120 PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/kanban/ -q
bash scripts/lanes-e2e.sh                                   # bootet Worktree-Backend seeded + Playwright
bash scripts/visual-verify.sh --skip-build --output-dir /tmp/lanes-visual /control/lanes
```
`scripts/lanes-e2e.sh` erklärt den Isolation-Stack: disposable `HERMES_HOME` + `HERMES_SANDBOX_MODE=1` + `PYTHONPATH=<wt>` (Worktree-Backend statt Live) + `--skip-build` (gate-`web_dist`) + Readiness-Poll auf `HERMES_*_READY port=N` + seeded Profile-Configs; Spec route-mockt nur Probe-POSTs. Playwright-Specs = ESM → `path.dirname(fileURLToPath(import.meta.url))` (kein `__dirname`).

## Mergeback-Notizen
- **0 Shared-Files** geändert (i18n/de.ts, lib/types.ts, lib/schemas.ts, ControlShell, ControlPage, useControlData, web/src/lib/api.ts nie angefasst; Lanes-String-Isolation beibehalten) → Merge nach main trivial konfliktfrei; lanes/-Ordner self-contained.
- `git worktree remove /tmp/codex-lanes-review` nach Abschluss (Throwaway-Review-Worktree).

## Bekannte Follow-ups (dokumentiert, NICHT blockierend für den Handoff)
- **F3-4 (selten):** Clear eines Overrides bei `default_model: null` gleichzeitig mit Reasoning-Clear/Fallback-Edit sendet nur `removed_profiles` (der model-less Guard schützt vor `model.default=null`-Schreiben) → Config-Reasoning-Clear geht verloren. Vollfix braucht einen dedizierten `config_updates`-Kanal im Persist-Vertrag (schreibt Reasoning/Fallbacks OHNE `model.default` zu tasten).
- **F3-1 (selten):** Der locked-Serializer in `profilesFromEditorRows` droppt `fallback_providers`; eine catalog-gelockte Hermes-Row MIT Lane-Fallback-Override verliert über den Quick-Switch ihre Fallback-Kette (Matrix-Editor ist davon nicht betroffen).
- **R2-Concurrency (selten, Multi-Session):** activate + persist sind zwei lane-unscoped Requests; ein paralleles Activate zwischen beiden kann den Write desync'en. Vollfix = Backend `persist`-to-specified-lane (Replace-Semantik pro Lane-ID).
- **R2-Failure-Nebeneffekt (bewusst):** bei activate-ok + persist-fail bleibt der Active-Indikator stale bis zum nächsten Reload/Action; die staged Edits bleiben erhalten + Top-Banner zeigt den Fehler (retryable). Ein Reload im Fehlerpfad würde die Edits verwerfen → bewusst nicht getan.
- **F4-Nebeneffekt (bewusst):** Speichern einer selektierten inaktiven Lane aktiviert sie („Speichern + aktivieren"); „editieren ohne aktivieren" wird nicht unterstützt.
- **ModelSelect-Sandbox-Edge:** das aktuell selektierte Modell erscheint im Dropdown nur, wenn `sinnvoll`; in credential-less Umgebungen kann eine aktive Row ein Modell zeigen, das nicht in den curated Options steht (mit echten Creds kein Effekt). Follow-up: selektiertes Modell immer in die Options pinnen.
- **Nav-Ökonomie:** `/lanes` weiter in `moreTabs`; Promotion in Primär-/Mobile-Nav = Design-Board (2 Varianten + Operator-Wahl).

## Offene Fragen an Piet
- Sollen F3-4 / R2-Concurrency (Persist-Vertrag-Erweiterung) als eigener Slice finanziert werden, oder reicht die Doku?
- Nav-Promotion nach Abschluss?
- Qwen-Worker/Review-Lane: Trial-Ergebnis = guter Builder, aber headless-`-p`-Stalls auf Token-Plan (Build-Final-Turn + Review beide timeout) → nur mit Stall-Wächter + bounded timeout nutzbar; per ToS ohnehin interaktiv-only. Empfehlung: nicht als autonome Lane ohne Wächter.

## Coordination / Receipt
- Check-IN `vault/_agents/_coordination/2026-07-24_0023_claude-code-lanes-model-platform.md`; Check-OUT + Receipt `vault/03-Agents/Claude-Code/receipts/2026-07-24-lanes-model-platform-receipt.md` (folgen bei Abschluss).

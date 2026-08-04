# ABSCHLUSSBERICHT — Goal usage-observability (2026-08-04)

Auftrag: `/home/piet/.hermes/goal-briefs/2026-08-04-usage-observability.md` —
LLM-Verbrauch über Hermes UND Buzz vollständig auswertbar: belastbare
Faktenschicht mit Rekonziliation, PAYG-Äquivalent, `/control`-Tab „Verbrauch"
mit quantifizierten Hebeln. Gearbeitet im Worktree
`.claude/worktrees/usage-obs` (Branch `worktree-usage-observability`).

## Was gemessen wurde (Phase A)

- **Erzeugerlandkarte (30d, UTC):** 10 Erzeugerklassen — claude_code 101.557
  Runs, buzz_agent 6.167 (8 systemd-Units: claude, codex, fable, grok, hermes,
  kimi, qwen, terra), hermes_agent 2.360, qwen_cli 1.859, grok_cli 1.016,
  codex_cli 767, hermes_aux 398, kimi_cli 146, kanban task_runs 2.861. Voll
  in `docs/observability/usage-ist-zustand.md`.
- **Rekonziliations-Instrument:** `scripts/usage_reconcile.py` rechnet vier
  unabhängige Wege (Call-Ebene, Run-Aggregat, task_runs, rohe JSONL-
  Transkripte) mit gemessenen (nicht angenommenen) Delta-Erklärungen.
  **Weg 4 bestätigt die Faktenschicht auf 0,04–0,31 %** — sie ist gegenüber
  der Quelle vollständig.
- **Hygiene:** Fixture-Profil `w` = 3.025 Runs am 2026-06-12, Tokens NULL
  (nie_gelaufen); `profile IS NULL` auf 68.207 Runs der Faktenschicht;
  Tagesgrenze UTC verbindlich; Epoch-/Cast-Fallen (`task_runs.started_at`,
  TEXT-vs-INTEGER-Join) dokumentiert.
- **Performance-Baseline:** Zeitprädikate liefen als Full-Table-Scan
  (COALESCE schlägt Index); 0,23 s bei 130 k Runs — im Budget, als
  Konstruktionsregel für Phase C festgehalten.

## Was repariert wurde (Phase B + E1)

| Befund | Urteil | Beleg |
|---|---|---|
| B1 Provider hart „anthropic" | **widerlegt** (durch Harvester-Format v9 vorbehoben) | 0 qwen-Zeilen mit falschem Provider; 3.010 korrekte `alibaba-token-plan`-Zeilen |
| B2 `lane` = Session-UUID | **bestätigt + behoben** | 1.419 UUIDs unter 1.429 Werten; Harvester schreibt jetzt Buzz-Unit/NULL (Canon 7.6), Korrelation liest `COALESCE(parent_session_id, lane)`; Altzeilen als blinder Fleck dokumentiert |
| B3 Cache-Doppelzählung | **bestätigt + behoben** | Vorher/Nachher 30d: codex_cli $40.834→$5.048 (8,1×), kimi_cli $4.022→$499, gesamt $59.655→$19.883 (3,0× zu hoch vorher) |
| B4 UPSERT-Überschreibung | **bestätigt + behoben** (Variante: Aux-überschreibt-Aux) | Live-Beweis Run 8816: 8 Aux-Calls, 1 Zeile; Index-Seed aus `max_call_index()`; ≥35 verlorene historische Beobachtungen (unrecoverbar, dokumentiert) |
| B5 `materialize_scores` reaktiviert | **widerlegt** | Symbol existiert nirgends im Codebestand |
| B6 Buzz-Lücken | **gemessen + Hermes-Unit behoben** | Attribution läuft seit 07-30; Fall-through bei >1 cwd (9/85); buzz-agent@hermes fehlte komplett → board_facts attributiert per Prozess-cwd |
| B7 PAYG-Abdeckung | **gemessen + verbessert** | row-level 94,27 % Runs / 95,11 % Tokens; provider_missing-Klasse eliminiert (Inferenz); grok model NULL ab Deploy erfasst; 23 Remainder-Kombis (18 documented_absent mit Negativbeweis) |
| E1-R1/R2 (bindend) | **behoben** | Readmodel-Zeitprädikat ließ occurred_at-NULL-Calls (953 M Tokens, alle hermes-Origins) durchfallen → `COALESCE(c.occurred_at, f.last_call_at, f.captured_at)` |
| E1-R1#3 | **behoben** | billable_input-Subtraktion nur noch auf OpenAI-Konvention (77 Anthropic-Zeilen hätten 24,7 % Input verloren) |
| E1 weitere | **behoben** | Lazy-Warmup statt Import-Thread; Trend 7d exakt (Vorzeichen-Flip); Hebel-3-Basis Hermes-Scope statt Gesamtfenster (Zirkularität); chain-Breakdown billing_mode; Hebel-2 beide Granularitäten; metered-KPI aus Daten; Coverage token-gewichtet |

## Reviews (E1)

Zwei unabhängige Opus-5.0-Reviews mit unterschiedlicher Linse:
`docs/observability/reviews/review-1-korrektheit.md` (BLOCK, 6 Blocker) und
`docs/observability/reviews/review-2-daten.md` (BLOCK, 8 Blocker). Schnittmenge
(occurred_at-Durchfall) war bindend; alle 10 benannten Blocker sind behoben
und mit Tests belegt, keiner wurde abgewiesen.

## Die drei größten Hebel (mit Gegenrechnung)

1. **premium-model-substitution — $5.160/Fenster.** Ist: fable-5/opus-5-Calls
   $9.167. Gegenrechnung: dieselben Komponenten zu sonnet-5-Sätzen $4.014.
   Annahme: gleiche Tokenstruktur, austauschbare Qualität. Plausibilität:
   Output-p50 568 / p90 1.963 Tokens (klein = austauschbar).
2. **outlier-run-cap — $310/Lauf (aktuelles Fenster; historisch bis $6.574).**
   Ist: teuerster Einzellauf. Gegenrechnung: p99 aller Läufe. Annahme:
   Ausreißer über p99 ist Fehlverhalten. Plausibilität: Ratio Top:p99 plus
   Run-Identität zum Nachprüfen.
3. **foreign-lane-cache-hit — $603/Fenster.** Ist: billable_input der Fremd-
   Lanes zum Input-Satz. Gegenrechnung: bewegliche Tokens zum Cache-Read-
   Satz bei Claude-Hit-Rate (gemessen, 98,6 %). Annahme: stabilere Kontext-
   Prefixe. Plausibilität: Hit-Raten je Lane im Payload.

## Was ausgeliefert ist

- `scripts/usage_reconcile.py` — 4-Wege-Rekonziliation, **EXIT=0 über 30d**.
- `hermes_cli/usage_consumption_readmodel.py` — Metrikschicht (Kosten pro
  Tag/Origin/Modell/Provider/Lane/Buzz-Unit/Chain, Abo vs. Äquivalent,
  Cache-Hit-Rate, Komponentenanteile Tokens vs. Kosten, Verteilungen
  p50/p90/max, Top-Runs, Trend 7d/30d, 4 Hebel mit Gegenrechnung).
- `GET /api/plugins/kanban/stats/usage-consumption` — Contract
  `usage-consumption.v1`; Latenz warm <1 ms (TTL-Cache + Lazy-Warmup),
  kalt ~4 s (dokumentiert).
- `/control/verbrauch` — Tab „Verbrauch": Hebel-Sektion als Held oben,
  Small Multiples je Origin mit Abdeckungs-Sättigung, Fenster 7/30/90,
  Aufriss Quelle/Modell/Lane/Buzz-Agent, Sprung-Links, vier Kachelzustände
  + Contract-Mismatch, `not applicable` statt 0. Visual-Verify Desktop/
  Tablet/Mobile grün.
- Doku: `usage-ist-zustand.md`, `usage-facts.md` (Datenpfad, alle
  Metrikdefinitionen, 9 blinde Flecken), `verbrauch-tab-design.md` (D1).

## Offen geblieben (mit Begründung)

1. **grok model NULL historisch** (1.016 Runs): Fix erfasst ab Deploy;
   Backfill bräuchte Schreibzugriff auf die Live-DB → stehende Entscheidung
   1, nicht ausgeführt; Operator: `harvest_foreign_lanes --force` nach Deploy.
2. **Task-Korrelation 16,5 %** (472/2.857): kein Tokenverlust (Weg-4-Beweis),
   aber dünne Chain-Sicht — als blinder Fleck beziffert, nicht weggeglättet.
3. **Historische lane-UUIDs** (1.419): Migration beschrieben
   (usage-facts.md, blinder Fleck 3), nicht durchgeführt (stehende
   Entscheidung 1); Leser filtern UUID-förmige lanes.
4. **occurred_at für hermes-Origins** (0/19.087): additive Ergänzung im
   board_facts-Schreibpfad als Folge-Slice notiert.
5. **23 unbepreisbare (model, provider)-Kombis**, 18 davon documented_absent
   mit Negativbeweis (stehende Entscheidung 3 — `not applicable`, nie 0):
   Liste via `scripts/check_usage_facts_pricing_coverage.py --days 30`.
6. **Historischer Aux-Verlust** ≥35 Calls (B4): nicht rekonstruierbar,
   <0,01 % des Fensters.
7. **profile-NULL-Klassifikation** (68.207 Runs) und **qwen_cli-Frische**
   (32 h alt): Beobachtungen, keine Blocker.

## Gates (final, Exit-Codes)

- `scripts/run-affected.sh $(git merge-base main HEAD)`: siehe unten
- `ruff check .`: EXIT=0
- `scripts/gate-frontend.sh`: EXIT=0 (inkl. tsc, vitest, build, Hex-Ratchet
  58=Baseline, Kontrast AA)
- `scripts/collect_check.sh -q tests/`: 57.520 Tests, 0 Fehler
- `scripts/usage_reconcile.py --days 30`: EXIT=0 (0 unerklärte Deltas)
- Frontend-Visual-Verify `/control/verbrauch`: EXIT=0

## Merge-Befehl für den Operator

```bash
cd /home/piet/.hermes/hermes-agent
git rev-parse --abbrev-ref HEAD        # MUSS main sein
git status --short                      # fremde staged Änderungen: nicht mergen
git merge --no-ff worktree-usage-observability
git push piet-fork main                 # NIEMALS nach origin (NousResearch-Upstream)
CONFIRMED=1 scripts/deploy_dashboard.sh
```

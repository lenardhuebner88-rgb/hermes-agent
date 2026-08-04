# Usage-Observability — Ist-Zustand (Messbericht Phase A)

Erstellt 2026-08-04 im Goal-Lauf `usage-observability`. Alle Zahlen stammen aus Live-Messungen
(read-only) gegen `/mnt/data/hermes-observability/usage_facts.db`, `~/.hermes/kanban.db` und die
JSONL-Transkripte unter `~/.claude/projects`. Zeitfenster, wenn nicht anders genannt:
**30 Tage, UTC, halb-offen [cutoff, now)** — Cutoff jeweils am Messzeitpunkt 2026-08-04 ~20:40 UTC.

**Tagesgrenzen-/Zeitzonen-Konvention (verbindlich für alle Tagesaggregate):** UTC-Kalendertage,
Schlüssel `substr(zeitstempel,1,10)` auf dem Ereigniszeitpunkt (`occurred_at` bzw.
`first_call_at`/`last_call_at`, Fallback `captured_at`). Mischformate (`Z` vs `+00:00`) sind für
Tagesgranularität sortierstabil. **`task_runs.started_at` ist Epoch-Sekunden (INTEGER)** — ein
`datetime()`-Vergleich liefert still 0 Zeilen (gemessen, Kontrollprobe: 9.112 Runs all-time).
Facts-`task_run_id` ist TEXT, `task_runs.id` INTEGER — Joins brauchen expliziten Cast.

## A1 — Erzeugerlandkarte

| Erzeuger | origin | schreibt Fakten | Runs 30d | Tokens 30d (in/out/cache) | seit | Bemerkung |
|---|---|---|---|---|---|---|
| Claude Code (interaktiv + Kanban-Worker + Loops + Strategist) | `claude_code` | ja (Call- + Run-Ebene) | 101.557 | 99,7 M in / 80,0 M out / 14,0 Mrd cr / 371 M cw | 2026-06-30 | 96.285 Runs (14,0 Mrd in+cr) **ohne Task-Korrelation** |
| Buzz-Agent-Units (8: claude, codex, fable, grok, hermes, kimi, qwen, terra) | `buzz_agent` | ja (claude-Anteil Call-Ebene; qwen/grok/codex/kimi nur Run-Ebene) | 6.167 | 427 M in / 5,3 M out / 1,13 Mrd cr | 2026-07-30 | Split nach Quell-Präfix: claude_code 4.437, qwen_cli 1.358, grok_cli 241, codex_cli 73, kimi_cli 58 |
| Hermes-Runtime (Kanban-Worker-Prozess, Gateway, Crons) | `hermes_agent` | ja (beide Ebenen; `occurred_at` fehlt auf Call-Ebene) | 2.360 | 212 M in / 6,2 M out / 725 M cr | 2026-07-27 | `task_run_id` nur bei ~9 % der Runs; `lane` bei 2.293 von 2.360 NULL |
| Aux-Calls (Titel, Summaries) | `hermes_aux` | ja | 398 | 2,2 M in / 0,7 M out | 2026-07-27 | `provider` überwiegend NULL (Erfassungslücke) |
| Fremd-Lane Qwen | `qwen_cli` | ja (nur Run-Ebene) | 1.859 | 145 M in / 1,2 M out / 137 M cr | 2026-07-20 | letzter Schreibzugriff 2026-08-03 — Lane idle oder Harvest-Lücke, ungeprüft |
| Fremd-Lane Grok | `grok_cli` | ja (nur Run-Ebene) | 1.016 | 88 M in / 1,1 M out / 78 M cr | 2026-07-20 | **`model` durchgehend NULL** (1.016/1.016) — Erfassungslücke |
| Fremd-Lane Codex | `codex_cli` | ja (nur Run-Ebene) | 767 | 7,44 Mrd in / 19,9 M out / 7,26 Mrd cr | 2026-05-08 | größtes Tokenvolumen aller Lanes |
| Fremd-Lane Kimi | `kimi_cli` | ja (nur Run-Ebene) | 146 | 1,23 Mrd in / 5,2 M out / 1,20 Mrd cr | 2026-06-16 | |
| Kanban-Board (Worker-Abrechnung) | — (`task_runs`) | Zweitmessung, für Verbrauch abgekündigt (Canon §2) | 2.861 | 550 M in+out (527 M in / 23 M out), `cost_usd` = 0,0 | — | keine Cache-Spalten; schreibt Tokens erst bei Run-Abschluss |

Erfassungsinfrastruktur: `hermes-usage-harvest.timer` (15-min-Takt, zuletzt 14 s vor Messung
gelaufen) und `hermes-execution-facts-*.timer` laufen. Frische der `captured_at`-Stempel pro
Origin am 2026-08-04 ~20:46 UTC: hermes_agent/buzz/claude_code < 1 min, codex_cli 7 min,
kimi_cli 16 min, hermes_aux 2,6 h, grok_cli 8,5 h, **qwen_cli 32 h** (Blind Spot 5).

**Blinde Flecken (Erzeuger, die nicht eindeutig in den Daten wiederzufinden sind):**

1. `grok_cli.model` NULL in 1.016/1.016 Runs — Erfassungslücke, Verbrauch nicht bepreisbar.
2. `hermes_aux.provider` NULL in 421/442 Runs (30d).
3. Task-Korrelations-Abdeckung: nur **472 von 2.857 in-window `task_runs` (16,5 %)** sind in der
   Faktenschicht korreliert; der Rest des Kanban-Verbrauchs steckt unkorreliert in
   `claude_code`/`hermes_agent`. Kein Tokenverlust (Weg 4 beweist Vollständigkeit), aber keine
   Task-Sicht.
4. `run_llm_calls.occurred_at` ist für `hermes_agent`/`hermes_aux` in 0 von 19.087 Zeilen gefüllt —
   Call-Ebene dieser Origins hat keine Ereigniszeit (Zeitfilterung läuft über den Run).
5. `qwen_cli` letzter Fakten-Schreibzugriff 2026-08-03 08:50 UTC — unklar ob Lane inaktiv oder
   Harvester-Teilausfall.
6. Cron-/Loop-/Gateway-/Dashboard-eigene LLM-Calls tragen keine eigene Provenienz-Marke; sie sind
   nur über Session-/Task-Korrelation von interaktiver Arbeit zu trennen.
7. `profile` ist bei 68.207 Runs NULL (siehe A3) — Klassifikation produktiv/fixture/nie_gelaufen
   steht aus.

## A2 — Rekonziliations-Deltatabelle (`scripts/usage_reconcile.py --days 30`, EXIT=0)

Vier Wege, 17 paarweise Deltas, **0 unerklärt**. Kernergebnis: Weg 4 (rohe JSONL-Transkripte,
unabhängig geparst) bestätigt die Call-Ebene der Faktenschicht auf **0,04 % (Tokens), 0,05 %
(Runs), 0,31 % (Buzz-Kosten)** — die Faktenschicht ist vollständig gegenüber der Quelle.

| Paar | Metrik | Delta | Klassifikation (gemessene Ursache) |
|---|---|---|---|
| run_llm_calls vs run_usage_facts (claude_code) | Tokens/Runs/Kosten | 2,8–3,7 % | erklärt: 3.799 pre-v6-Calls ohne `occurred_at` (423 M Tokens) unsichtbar für Call-Zeitfilter |
| run_llm_calls vs run_usage_facts (hermes) | Tokens | 3,49 % | erklärt: Partial-Sum-NULL-Regel, 1.634 Runs, 17,55 M Tokens |
| run_llm_calls vs run_usage_facts (kanban) | Tokens | 2,22 % | erklärt: Partial-Sum-NULL (88 Runs) + 1,44 M pre-v6-Tokens |
| run_llm_calls vs run_usage_facts (buzz) | Tokens/Runs | 53 %/28 % | erklärt: 1.730 Foreign-CLI-Buzz-Runs sind run-level only (838 M Tokens), keine Call-Zeilen by design |
| run_llm_calls vs run_usage_facts (buzz) | Kosten | 46,2 % | erklärt: bepreister Anteil der run-level-only-Runs $497,90; Rest documented-absent qwen3.8-max-PAYG |
| transcripts vs run_llm_calls (claude+buzz) | Tokens/Runs/Kosten | ≤0,31 % | erklärt: Harvest-High-Water-Mark-Lag |
| task_runs vs run_usage_facts (hermes-geschrieben, korreliert) | in+out | **0,56 %** | erklärt: gleiche Buchführung beiderseits — Match |
| task_runs vs run_usage_facts (claude-korreliert) | in+out | 72,4 % | erklärt (benannte Ausnahme): `task_runs` für Verbrauch abgekündigt (Canon Register §2), abweichende Input-Zählkonvention |
| task_runs vs run_usage_facts | Runs | 53,8 % | erklärt: Granularität — ein task_run ↔ viele Facts-Runs (6.168 Runs über 482 task_runs) |
| task_runs vs run_usage_facts | in+out (volles Fenster) | 83,1 % | erklärt (Scope): 457 M Tokens auf unkorrelierten task_runs — in claude_code-Scope erfasst, nicht verloren; Coverage-Blindspot 3 |

## A3 — Datenhygiene

- **`profile IS NULL` in `run_usage_facts`: 68.207 Runs, 10,7 Mrd in / 99 M out / 22,2 Mrd cr**
  (all-time). Die im Brief genannten ~352 Runs / 1,98 M Tokens beziehen sich auf
  `task_runs.profile IS NULL` (gemessen: 431 Runs, 1,92 M in / 59 k out) — dort bestätigt.
  In der Faktenschicht ist die NULL-Klasse zwei Größenordnungen größer und überwiegend
  `claude_code`-Main-Sessions ohne `agentType` (produktiv, nicht fixture). Offene
  Klassifikation, Blind Spot 7.
- **Fixture-Profil `w`:** 3.025 Runs am 2026-06-12 in `task_runs`, Tokens durchgehend NULL →
  Klasse `nie_gelaufen`. In `run_usage_facts` existiert `profile='w'` nicht (0 Zeilen,
  Kontrollprobe über GROUP BY). Verzerrt nur task_runs-basierte Coverage-Zahlen, nicht die
  Faktenschicht. Roh-Quote im Juni-Fenster task_runs: 3.025 von 9.112 all-time Runs.
- **`profile` trägt teils Session-UUIDs** (gleiche Krankheit wie B2 bei `lane`): z. B. 429
  buzz_agent-Runs und 325 qwen_cli-Runs mit UUID als `profile`.
- **Tagesgrenze/Zeitzone:** verbindlich UTC, halb-offen (siehe Kopf). Stille Fallen gemessen und
  dokumentiert: Epoch-`started_at`, TEXT-vs-INTEGER-`task_run_id`, ISO-Suffix-Mischung.

## A4 — Performance-Baseline (echte DB-Größe: 130.124 Runs, 139.872 Calls)

| Abfrage (Phase-C-Kern) | Plan | Index | Laufzeit |
|---|---|---|---|
| Tag×Origin×Modell×Provider-Rollup 30d (`COALESCE(last_call_at,captured_at) >= cutoff`) | SCAN run_usage_facts + TEMP B-TREE GROUP BY | **keiner** (COALESCE schlägt Index) | 0,234 s, 414 Zeilen |
| Zeitgefilterte Call-Liste (`occurred_at >= cutoff`) | SCAN run_llm_calls + TEMP B-TREE ORDER BY | keiner | 0,132 s, 102.333 Zeilen |
| Origin-Gruppierung | SCAN USING idx_run_usage_facts_origin_model | ja | <0,01 s |

Befund: die Zeitprädikate laufen als Full-Table-Scan — bei heutiger Größe im Budget (<500 ms für
den C3-Endpoint), aber der vorhandene `captured_at`-Index wird durch das COALESCE-Prädikat nicht
benutzt. Für Phase C: Zeitprädikat so formulieren, dass der Index greift (OR-Split statt
COALESCE), und Plan bei jeder Endpoint-Abfrage erneut messen.

## Checkpoint-A-Urteil

Phase A ist erfüllt: Erzeugerlandkarte steht, das Rekonziliations-Werkzeug läuft mit EXIT=0 und
gemessenen (nicht angenommenen) Delta-Erklärungen, Hygiene-Quoten sind beziffert, die
Performance-Baseline ist gemessen. Kein Code außer `scripts/usage_reconcile.py` geändert.
Die sieben blinden Flecken gehen als Arbeitsvorrat in Phase B (1, 2, 5) bzw. als dokumentierte
Einschränkungen (3, 4, 6, 7) in `usage-facts.md` ein.

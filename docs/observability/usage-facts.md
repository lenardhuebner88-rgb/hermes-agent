# Usage-Facts — Datenpfad, Metrikdefinitionen, blinde Flecken

Stand 2026-08-04 (Goal `usage-observability`). Ergänzt `usage-ist-zustand.md`
(Phase-A-Messung) und die Canon-Entscheidungen `2026-07-27-kosten-ssot-im-lesepfad`
sowie `2026-07-31-metrik-ssot-register`. Diese Datei ist die verbindliche
Definition jeder ausgelieferten Verbrauchsmetrik.

## Datenpfad

```
Erzeuger (Kanban-Worker, Loops, Crons, Strategist, Gateway, interaktive
Claude-Code-Sessions, 8 buzz-agent@*-Units, Fremd-Lanes codex/grok/kimi/qwen)
  → Erfassung:
      board_facts-Plugin (hermes_agent/hermes_aux, live im Prozess)
      claude_code_harvester (Transkripte ~/.claude/projects, 15-min-Timer)
      foreign_lane_harvest (codex/kimi/grok/qwen, gleicher Timer)
  → Faktenschicht: /mnt/data/hermes-observability/usage_facts.db
      run_usage_facts (Run-Aggregat, SSOT Verbrauch)
      run_llm_calls   (Per-Call-Zeilen, feinste Granularität)
      run_tool_calls / run_traces
  → Bepreisung im Lesepfad: hermes_cli/usage_facts_pricing.py
      (Rohfakten × Preistabelle, USAGE_FACTS_PRICING_VERSION;
       Kosten werden NIE als Spalte gespeichert)
  → Metrikschicht: hermes_cli/usage_consumption_readmodel.py
  → API: GET /api/plugins/kanban/stats/usage-consumption
      (Contract usage-consumption.v1)
  → Tab „Verbrauch": /control/verbrauch (VerbrauchView)
```

Wahrheitsinstrument: `scripts/usage_reconcile.py --days 30` rechnet vier
unabhängige Wege (Call-Ebene, Run-Aggregat, task_runs, rohe JSONL-
Transkripte) gegeneinander; Exit ≠ 0 bei unerklärtem Delta über Schwelle.
Weg 4 bestätigt die Faktenschicht aktuell auf 0,04–0,31 %.

## Zeitkonvention

UTC-Kalendertage, halb-offenes Fenster `[now − days, now)`. Tagesgrenze
`substr(zeitstempel, 1, 10)`. Ereigniszeit: `occurred_at` (Call),
`first_call_at`/`last_call_at` (Run), Fallback `captured_at` (Schreibzeit —
einzige Zeit für hermes_agent/hermes_aux, siehe blinder Fleck 4).
**Fallen:** `task_runs.started_at` ist Epoch (kein `datetime()`-Vergleich);
`run_usage_facts.task_run_id` ist TEXT vs. `task_runs.id` INTEGER (Cast
nötig); ISO-Suffix-Mischung (`Z`/`+00:00`) ist tagesstabil, nicht sekundenstabil.

## Metrikdefinitionen (alle im Lesepfad, nie gespeichert)

| Metrik | Formel | Zähler | Nenner |
|---|---|---|---|
| **Äquivalent-Kosten** | Σ Komponente × Satz / 1M über alle bepreisbaren Gruppen | Komponentensummen (s. u.) × `usage_facts_pricing`-Raten | — |
| **billable_input** (Komponente) | zeilenweise: `input − cache_read` nur auf OpenAI-Konvention (`0 < cache_read ≤ input` UND NICHT provider=`anthropic` UND NICHT origin=`claude_code`); Anthropic-Konvention zählt Input voll (Verfeinerung nach Review 1: 77 Produktionszeilen mit `cache_read ≤ input` auf dem Anthropic-Pfad) | — | — |
| **Cache-Write-Komponenten** | liegt der TTL-Split vor (1h/5m nicht NULL), zählt nur der Split; sonst die Gesamtsumme (Register-Leseregel) | — | — |
| **Abo-real (metered)** | `0` für `subscription_included`; Äquivalent für `payg/metered/api_key`; **unbekannt** bei NULL/`unknown` (nie 0) | — | — |
| **Abo-Ersparnis** | Σ (Äquivalent − metered) über Gruppen mit bekannter Abrechnung | wie links | metered_coverage im Payload |
| **Cache-Hit-Rate** | `cache_read / prompt` mit `prompt = billable_input + cache_read` (beide Konventionen stimmen hier überein) | cache_read-Tokens | prompt-Tokens |
| **Komponentenanteile** | Komponente / Gesamt, getrennt für Tokens und für Kosten | je Komponente | Σ aller Komponenten |
| **Verteilung Tokens/Run** | p50/p90/max über Run-Summen (in+out+cr+cw) | — | Anzahl Runs |
| **Trend** | `Äquivalent/Tag über 7d` vs. `über Fenster` | 7d-Summe / 7 | Fenster-Summe / Fenstertage |
| **Preisabdeckung** | bepreisbare Runs (bzw. Tokens) / alle | priced | total |
| **Bepreisung der Gruppen** | fehlt ein Satz einer token-tragenden Komponente, ist die ganze Gruppe `unknown` (fail-closed, Canon 7.3) — dargestellt als `not applicable` | — | — |

**Granularitätsregel (aus Phase B):** die §5f-Regel ist pro Request definiert.
Token-Komponenten werden zeilenweise auf Call-Ebene in SQL berechnet und erst
dann summiert; Preis = Komponentensumme × Satz (exakt, da linear). Runs ohne
Call-Zeilen (Fremd-Lane-Harvest) werden auf Run-Ebene bepreist — ihrer
feinsten verfügbaren Granularität — und separat gezählt.

**Anzeige-Präzision:** Aggregatzahlen rechnen in `Decimal`; die Per-Run-
Heißschleife (Top-Runs, Ausreißer-Hebel) nutzt Float-Raten — Display-Pfad,
gerundet auf Cent; Autorität ist die Decimal-Rechnung.

## Hebel-Definitionen (C2)

1. **premium-model-substitution:** Ist = fable-5/opus-5-Calls zum eigenen
   Satz; Gegenrechnung = dieselben Komponenten zum sonnet-5-Satz.
   Annahme: gleiche Tokenstruktur bei austauschbarer Qualität. Plausibilität:
   Output-p50/p90 dieser Runs (niedrig = austauschbar).
2. **outlier-run-cap:** Ist = teuerster Einzellauf; Gegenrechnung = p99 aller
   Läufe. Annahme: Ausreißer über p99 ist Fehlverhalten und begrenzbar.
   Plausibilität: Verhältnis Top:p99, Run-Identität zum Prüfen.
3. **failed-outcome-runs:** Ist = Token-Anteil der task_runs mit Status
   blocked/iteration_budget_exhausted/transient_retry × **Hermes-Runtime-
   Äquivalent** (hermes_agent + hermes_aux — der nächstgelegene gemessene
   Scope, NICHT das Gesamtfenster); Gegenrechnung = Halbierung. Annahme:
   frühere Abbrüche/Retry-Deckel vermeiden die Hälfte; die Scope-
   Extrapolation über die 16,5-%-Korrelationslücke ist Teil der Annahme und
   im Payload benannt. Plausibilität: failed_runs/all_runs, Token-Anteil
   (Basis: task_runs in/out, Worker-Buchführung ohne Cache).
4. **foreign-lane-cache-hit:** Ist = billable_input der Fremd-Lanes
   (OpenAI-Konvention, beide Granularitäten — Call-Ebene wo vorhanden,
   Run-Ebene für den Fremd-Lane-Harvest) zum Input-Satz; Gegenrechnung =
   der Anteil, der bei Claude-Hit-Rate (gemessen, Referenzwert im Payload)
   stattdessen zum Cache-Read-Satz liefe. Annahme: stabilere Kontext-
   Prefixe heben die Hit-Rate auf das gemessene Claude-Niveau.
   Plausibilität: Hit-Raten je Lane im Payload.

Ein Hebel ohne Gegenrechnung kommt nicht ins Ranking (Contract).

## Latenz

Warm antwortet der Endpoint aus dem TTL-Cache (300 s, Harvest-Takt 15 min)
in < 1 ms — gemessen warm 0,000 s. Der Kaltbau (30 Tage, ~140 k Call-
Zeilen) kostet ~4 s (vier Full-Scans + Preisfunktion); ein Lazy-Warmup
beim ersten Request wärmt die Default-Variante im Hintergrund. Kein
Import-Zeit-Thread (pytest-Collection berührt keine Produktions-DB).

## Blinde Flecken (beziffert, mit angewandter Entscheidungsregel)

1. **`grok_cli.model` historisch NULL** (1.016/1.016 Runs, 200 M Tokens).
   Ab Deploy erfasst (B7-Fix: `model changed`-Events + Katalog-Singleton);
   historisches Auffüllen bräuchte `harvest_foreign_lanes --force` gegen die
   Live-DB — Schreibzugriff außer additiver Migration → **nicht ausgeführt**
   (stehende Entscheidung 1). Operator kann den Force-Lauf nach Deploy holen.
2. **Task-Korrelations-Abdeckung 16,5 %** (472 von 2.857 in-window
   task_runs). Der Rest des Kanban-Verbrauchs ist erfasst, aber nicht
   task-zuordenbar (sitzt in unkorrelierten claude_code-Runs). Kein
   Tokenverlust (Weg-4-Beweis), aber die Chain-Sicht ist dünn — im Payload
   als `unknown`-Chain ausgewiesen, nicht weggeglättet.
3. **Historische `lane`-Verschmutzung:** bis zum B2-Fix trugen claude_code-
   Zeilen Session-UUIDs in `lane` (1.419 UUIDs unter 1.429 Werten).
   Leser filtern UUID-förmige lanes als unkategorisiert (SQL im Readmodel);
   eine Migration, die Altzeilen umschreibt, wurde nicht durchgeführt
   (stehende Entscheidung 1) — die nötige Migration wäre
   `UPDATE … SET lane=NULL WHERE length(lane)=36 AND lane LIKE '%-%-%-%-%'`
   für claude-Herkünfte; Risiko: verliert die (redundante, in
   `session_id`/`parent_session_id` gesicherte) UUID, Nutzen: saubere
   historische Lane-Auswertung.
4. **`occurred_at` fehlt für hermes_agent/hermes_aux** (0 von 19.087
   Call-Zeilen). Call-Ebene dieser Origins hat keine Ereigniszeit;
   Zeitfilterung läuft über den Run (`last_call_at`/`captured_at`). Additive
   Ergänzung im board_facts-Schreibpfad wäre möglich (Hook-Zeitpunkt), ist
   als Folge-Slice notiert.
5. **Buzz-Fall-through bei >1 distinct cwd** (9 von 85 Buzz-Transkripten,
   10,6 %): Sessions mit wechselndem Arbeitsverzeichnis bleiben
   `claude_code` statt `buzz_agent` — fail-closed by design.
6. **Historischer Aux-Verlust (B4):** ≥35 Aux-Calls in 16 Runs wurden vor
   dem Fix upsert-überschrieben; die Token sind nicht rekonstruierbar
   (Trace-Evidenz belegt den Verlust, nicht die Werte). Unter 0,01 % des
   Fensters.
7. **`profile IS NULL` auf 68.207 Runs** (10,7 Mrd Input-Tokens) —
   Klassifikation produktiv/fixture/nie_gelaufen steht aus; betrifft nur
   Profil-Auswertungen, keine Kosten.
8. **Unbepreisbare Kombinationen (23, davon 18 documented_absent):** die
   vollständige Liste liefert `scripts/check_usage_facts_pricing_coverage.py
   --days 30`. Größte Token-Massen: qwen3.8-max/-preview (kein PAYG-Satz
   existiert — belegt), kimi k3 cache_write (Satz existiert nicht — belegt),
   codex-auto-review (kein API-Modell — belegt). Stehende Entscheidung 3:
   `not applicable`, nie 0. Ein open_gap: (gpt-5.6-terra, kimi-coding), 2
   Runs — Provider-Falschzuschreibung in der Erfassung, separat notiert.
9. **qwen_cli-Frische:** letzter Fakten-Schreibzugriff 2026-08-03 08:50 UTC
   (32 h alt zur Messung). Unklar ob Lane inaktiv oder Harvester-Teilausfall
   — als Beobachtung offen.

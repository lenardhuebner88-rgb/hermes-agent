# Reporting-System Gesamt-Audit — 2026-07-29

**Scope:** Gesamt-Ökosystem „Reporting" = (A) In-Repo-Cron-Subsystem (`cron/`, `hermes_cli/cron.py`, Delivery)
+ (B) Ops-Reporting-Skripte `~/.hermes/scripts/` + (C) Scheduler-Ring (Hermes-Cron-Jobs, systemd-User-Timer, Crontab)
+ (D) Discord-Delivery-Pfade.
**Worktrees:** `kimi-reporting-audit` (hermes-agent @ eb942ca905), `kimi-reporting-scripts` (~/.hermes/scripts @ aae76ed).
**Methode:** Zwei parallele Tiefen-Audits (explore-Agents) + eigene Live-Inventur (jobs.json, executions.db 7d, systemd, crontab).

---

## 0. Systembild

Drei Scheduler nebeneinander:

| Scheduler | Aktiv | Reporting-Rolle |
|---|---|---|
| Hermes-Cron (`~/.hermes/cron/jobs.json`, ~35 Jobs) | ja | ~15 Reporting-Jobs, stdout→Discord via `deliver`, `[SILENT]`-Kontrakt |
| systemd --user Timer (~39) | ja | green-gate-heartbeat, failed-unit-watch, nightly-audit, hermes-loop@*, decisions-digest |
| Crontab | 2 aktive Zeilen | ~60 `#OPENCLAW-OFF`-Leichen |

Live-Gesundheit (executions.db, 7 Tage): 1 harter Fehler (`voice-spar-smoke`: `No module named 'piper'`).
Auffällig: Daily-Jobs zeigen nur je 1 Execution im 7d-Fenster → Retention/Ledger-Frage.

Discord-Channels im Einsatz: `…645` (Ops), `…966` (Triage/Digests), `…624` (Ops-Feed), `…073` (RCA,Docstring), `…888` (Gateway-Watchdog), `…334` (Alert, crontab-ENV).

---

## A. In-Repo-Cron-Subsystem — Findings

**CRITICAL**
- **A-C1 — `cron/executions.py::EXECUTIONS_FILE` import-time eingefroren.** Anders als `jobs.py::_current_cron_store()` (ContextVar, dynamisch) nutzt `_connect()` den gefrorenen Pfad. Unter `multiplex_profiles` teilen alle Profile dasselbe Ledger → profilübergreifender Leak, Job-ID-Kollisionen, falsche Recovery-Attribution. Kein Test deckt executions-Scoping ab.

**HIGH**
- **A-H1 — Standalone-Delivery ohne Timeout.** `scheduler.py::_deliver_result`: `asyncio.run(coro)` unbounded → hängender Send blockiert Worker-Thread endlos; Job bleibt in `_running_job_ids` und wird künftig still übersprungen ("already running").
- **A-H2 — Keine Delivery-Persistenz/Eskalation.** Delivery-Failure ⇒ nur `last_delivery_error`; Failure-Summary geht über denselben evtl. kaputten Kanal. Kein Outbox/Retry/Alternativweg → stille Report-Verluste.
- **A-H3 — Multiplex-Ticker: ein kaputtes Profil blockiert alle.** `_start_multiplex`: `except BaseException` um gesamte Profil-Schleife; Fehler wird **allen** Profilen zugeschrieben.
- **A-H4 — Korruptes `jobs.json` legt Scheduler komplett lahm.** `load_jobs` wirft; kein Quarantäne-/Backup-Restore; nur `hermes cron status` zeigt es.

**MEDIUM**
- **A-M1** Tote Konstanten `TICKER_HEARTBEAT_FILE`/`TICKER_SUCCESS_FILE` (Prod liest Literale; Tests patchen die Konstanten → prüfen nichts).
- **A-M2** Lifecycle-Regex dupliziert (`hermes_cli/cron.py` vs `cron/lifecycle_guard.py`), nur SYNC-NOTE-Konvention; bereits einmal divergiert.
- **A-M3** `_jobs_lock`-Timeout degradiert still zu prozess-internem Lock → Cross-Prozess-Torn-Write möglich, ohne Metrik.
- **A-M4** CLI schmaler als Tool (`create/edit` kann model/provider/base_url/context_from/toolsets nicht).
- **A-M5** Unbounded Parallelität (`max_parallel_jobs` ohne Default-Cap).
- **A-M6** `suggestions.py`: `CRON_DIR` import-gefroren, nur In-Process-Lock (gleiche Klasse wie A-C1).
- **A-M7** `load_jobs` hat Schreib-Seiteneffekte (Auto-Repair im Getter).
- **A-M8** Fehlgeschlagene Jobs ohne Retry → transienter API-Fehler = verlorener Report.
- **A-M9** (Eigenfund Live) **Ledger-Retention ist global FIFO (`MAX_TERMINAL_EXECUTIONS`=1000).** Hochfrequenz-Jobs (Mother-Receipt */3min + Stale-Run-Sweeper */5min ≈ 770 Runs/Tag) verdrängen alles: Ledger hält <1,5 Tage, Daily-Jobs zeigen im 7d-Fenster nur 1 Execution → Observability-Verlust genau bei den seltenen Report-Jobs.

**LOW**
- **A-L1** Deprecated-Shim `gateway/run.py::_start_cron_ticker`. **A-L2** `resolve_cron_scheduler` schluckt Config-Fehler. **A-L3** `parse_schedule` validiert nur 5/6 Felder. **A-L4** `HERMES_CRON_SESSION` prozess-global ohne Reset. **A-L5** zahlreiche stille `except Exception: pass` ohne Zähler.

Tests: `tests/cron/` 34 Dateien / 749 Testfunktionen — gute Abdeckung; Lücken: executions-Multiplex, Delivery-Failure, Multiplex-Fehler-Propagation.

---

## B. Ops-Skripte `~/.hermes/scripts/` — Findings

**HIGH**
- **B-H1 — Drei uncommittete Sensor-Rewrites sind unscheduliert.** `kanban-notifier.py`, `kanban-followup-on-needs-revision.py`, `kanban-10m-admin-reconciler.py` haben weder Job noch Timer → die Fremd-Arbeit läuft nie. Schedulen oder parken.
- **B-H2 — `rca-failed-workflows` liefert nach `local`.** Job `deliver: local`, Docstring sagt `discord:…073` → RCA-Summaries erreichen Discord nie.
- **B-H3 — `cross-system-deadman-check.py` (49k) faktisch abgeschaltet** (nur in `jobs.json.bak-openclaw-mute`); prüft z.T. tote OpenClaw-Endpoints → bei Reaktivierung Fehlalarme.

**MEDIUM**
- **B-M1** Redundanz: `systemd-onfailure-watcher.py` (*/15) vs `failed-unit-watch.sh` (2×/d, Dedupe) → Doppelalarme.
- **B-M2** `discord-notify.py`: kein Retry/Backoff, kein 429/`Retry-After`-Handling, kein Attachment-Support, fixer 20s-Timeout, kein Fallback-Kanal — zentraler Single-Point der Alarmierung.
- **B-M3** README lügt („bak-retention daily 04:00" ist OPENCLAW-OFF); 7 `.bak`-Dateien + 3 `jobs.json.bak-*` reifen unkontrolliert.
- **B-M4** OpenClaw-Reste in aktiven Jobs: `cron-of-crons-review.py` liest `~/.openclaw/cron/jobs.json`; `discord-channel-health.py` prüft 2 tote Channels; `operator-quick-audit.py` liest stilles OpenClaw-Log; `daily-digest.{timer,service}`-Leiche (disabled, ExecStart→nicht-existente Datei).
- **B-M5** Crontab-Leichen (~60 OFF-Zeilen, verwaiste ENV); aktive `loop_monitor.py`-Zeile ohne flock/Timeout.
- **B-M6** Keine Timeouts in Hermes-Cron-Jobs (`timeout=None` fast überall).
- **B-M7** `kimi-k3-watch.py` Deadline 2026-07-15 überschritten, Job existiert nicht → archivieren.

**LOW**
- **B-L1** drift-watcher */15 bei Tage-Granularität. **B-L2** zwei Delivery-Pfade (`discord-notify.py` vs `hermes send`). **B-L3** `voice-spar-smoke` last_status=error (piper fehlt). **B-L4** venv-Shebangs koppeln an Managed-Release-Runtime. **B-L5** `nightly-audit.service` ohne Timeout/OnFailure.

Uncommittete Fremd-Arbeit (10 Dateien, erhalten!): green-gate Python-Gate-Isolation, kanban-Sensoren v4, morning-digest Rewrite, drift-watcher Fingerprint-Dedupe, state-db-size-monitor Rewrite u.a. — inhaltlich hochwertig, aber uncommittet und (B-H1) teils unscheduliert.

---

## C. Loop-Plan (12 Verbesserungsloops)

| # | Loop | Findings | Ort |
|---|---|---|---|
| 1 | executions.py Store-Scoping (ContextVar wie jobs.py) + per-Job-Retention statt globalem FIFO-Cap + Multiplex-/Retention-Tests | A-C1, A-M9 | hermes-worktree |
| 2 | Standalone-Delivery-Timeout (wait_for) + Test „wedged send ⇒ Job frei" | A-H1 | hermes-worktree |
| 3 | Per-Profil-Fehlerisolierung in `_start_multiplex` + Test | A-H3 | hermes-worktree |
| 4 | jobs.json Backup-Rotation + Quarantäne/Restore statt Totalausfall | A-H4 | hermes-worktree |
| 5 | Tote Ticker-Konstanten + duplizierte Lifecycle-Regex vereinheitlichen | A-M1, A-M2 | hermes-worktree |
| 6 | Delivery-Resilienz: Retry+Backoff für Runs (A-M8), max_parallel Default-Cap (A-M5), Delivery-Failure für Morning-Digest sichtbar (A-H2-Teil) | A-M5, A-M8, A-H2 | hermes-worktree |
| 7 | discord-notify.py: 429/Retry-After, Retry mit Backoff, `--file`-Attachment, konfigurierbarer Timeout + Tests | B-M2 | scripts-worktree |
| 8 | OpenClaw-Säuberung der aktiven Reporter (cron-of-crons, channel-health, quick-audit) + Deadman/k3-watch archivieren | B-M4, B-H3, B-M7 | scripts-worktree |
| 9 | Unschedulierte Sensoren: Job-Definitionen als vorbereitetes Apply-Set (kein Live-Eingriff) + Park-Entscheid Doku | B-H1, B-H2 | scripts-worktree + Vorschlag |
| 10 | Timeout-Standard für Reporting-Skripte (interne Deadline + harte Klammer) | B-M6 | scripts-worktree |
| 11 | Live-State-Change-Set als Review-fähige Proposals (Watcher-Dedupe B-M1, Schedules B-L1, Crontab/systemd-Leichen B-M5/B-M4, nightly-audit-Timeout B-L5) — dokumentiert, NICHT appliziert | B-M1, B-L1, B-M5, B-L5 | proposals/ |
| 12 | Commit-Hygiene & Doku-Wahrheit: README-Korrektur, .bak-Retention-Skript, AUDIT-Verweis; HerMES-Seite: Shim-Doku A-L1, parse_schedule 6. Feld A-L3 | B-M3, A-L1, A-L3 | beide |

Nicht im Loop-Umfang (bewusst): voller Delivery-Outbox (A-H2 komplett) — Narrow-Waist-Prinzip, stattdessen Sichtbarkeits-Teil in Loop 6; CLI-Parität A-M4; Shim-Entfernung A-L1 (nur Doku).

## E. Codex-Bewertung #1 (Baseline, vor den Loops)

**Gesamt: 5.6/10** — `eval/2026-07-29-codex-baseline.json` (codex exec, read-only, Schema-validiert,
263k Tokens, Stichproben-Verifikation gegen echten Code).

| Kategorie | Score |
|---|---|
| delivery_reliability (2×) | 5.2 |
| failure_handling (2×) | 5.6 |
| scheduler_hygiene | 4.3 |
| observability | 6.5 |
| code_quality | 6.2 |
| test_coverage | 7.3 |
| documentation_truth | 4.4 |

**Zwei NEUE Findings aus der Codex-Gegenprobe (im Audit nicht enthalten):**
- **B-M8 (neu): `systemd-onfailure-watcher.py` ist false-green.** Ohne `--plain` wird „●" als
  Unitname gelesen; trotz zweier failed Units bleibt der */15-Job stumm. (Verstärkt Proposal C1;
  Parser-Fix zusätzlich im Worktree.)
- **B-M9 (neu): `cron-of-crons-review.py` liest veraltete Felder** (`lastStatus`/`lastRun` statt
  `last_status`/`last_run_at`) → meldet ERRORING=0 und 29 HEALTHY, obwohl `voice-spar-smoke` fehlschlägt.
  Wird in Loop 8 mitgefixt.

Codex' „what_would_make_it_9" deckt sich mit dem Loop-Plan; zusätzlich gefordert: persistente
Delivery-Outbox mit Replay (→ Loop 6, lean), Ledger-Retention ≥30 Tage für Report-Jobs (→ Loop 1,
per-job), maschineller Docstring↔Live-Job-Konsistenzcheck (→ Loop 12, scripts).

## D. Positive Referenzmuster (erhalten & nachahmen)

`failed-unit-watch.sh` (Dedupe/Remind), `_open_decisions.py` (atomare Writes), `_kanban_db_guard.py`, green-gate OnFailure-Design (eigene cgroup), Fingerprint-Dedupes, honest-degradation im Morning-Digest.

---

## F. Loop-Ergebnisse (2026-07-29, Stand nach allen 12 Loops)

**hermes-agent Worktree** (Branch `kimi/reporting-audit`, Integrations-Sweep: **931 Tests grün**):

| Loop | Commit(s) | Ergebnis |
|---|---|---|
| 1 | `0e2498f3f9` | executions.py: ContextVar-Store-Scoping (gespiegelt von jobs.py, 4-stufige Präzedenz) + per-job Retention (N=50/Job, globale Kappe 1000→20000) + 11 neue Tests |
| 2/3/10 | `eb812ceede` | Delivery-Timeout `asyncio.wait_for` (Default 60s, Job-Feld `delivery_timeout_seconds`, Cancel-Semantik, `_running_job_ids`-Freigabe e2e-getestet); per-Profil-Fehlerisolation in `_start_multiplex`; per-Job `timeout_seconds` (Cap 7200) + 28 neue Tests |
| 4 | `3321c08e86` | jobs.json: Backup-Rotation (.bak.1–3) + Quarantäne (`jobs.json.corrupt-<ts>`) + validierter Auto-Restore |
| 5 | `fc446ba639` | Ticker-Konstanten live; Lifecycle-Guard kanonisiert (single source in cron/lifecycle_guard.py, CLI = Wrapper); 35 String-Konsistenz-Tests |
| 6 | `240d4bcc2f` | `cron/delivery_outbox.py`: persistente JSONL-Outbox (queued/delivered/dead, Dedupe, Backoff 5→80min, 5 Versuche → dead-letter, Replay am Tick-Anfang, Store-gescoped, fail-closed); `hermes cron status` zeigt queued/dead; per-Job Run-Retry; max_parallel Default 4 + 15 neue Tests |

**scripts Worktree** (Branch `kimi/reporting-audit`, **39 Tests grün**):

| Loop | Commit(s) | Ergebnis |
|---|---|---|
| 7 | `58f8d0b` | discord-notify: 429/`Retry-After` + Retries (exp. Backoff), 5xx/Netz-Retry, 90s-Gesamt-Deadline, `--file` Attachment (8-MiB-Guard), `--timeout`, Fallback-Channel + 7 neue Tests |
| 8 | `89c7bad`, `c5bddbe`, `61805b2`, `235dcac` | cron-of-crons: OpenClaw-Store raus, hermes-Felder normalisiert (STALE/ERRORING feuern jetzt real — Codex-Finding), Profil-Stores; channel-health: 4 tote IDs raus, 3 aktive rein, Quellenkommentare; quick-audit: ehrliche Decommissioned-Markierung; attic/ für deadman + k3-watch; systemd-onfailure-watcher: false-green Bullet-Parsing gefixt (`--plain`) + 6 Tests |
| 9/11 | `1ad1f64` | `proposals/`: Job-Definitionen für 3 unschedulierte Sensoren + rca deliver-Fix; Live-State-Change-Set C1–C8 — NICHT appliziert |
| 12 | `67f4d04`, `b770943` | README-Wahrheit + `bak-retention.sh` (--dry-run Default); `reporting-consistency-check.py` (Docstring↔Live-Job↔Kanal-Drift), gegen Live-Store verifiziert + 9 Tests |

**Bewusst nicht geändert (Erhaltung):** uncommittete Fremd-Arbeit im Live-Checkout
(10 Dateien, u.a. Morning-Digest-Rewrite, green-gate-Isolation); Live `jobs.json`,
systemd-Units, Crontab (nur Proposals). Kein Deploy, kein Merge, kein Push.

---

## G. Reproduzierbare Gates (Loop 16)

Alle Kommandos aus dem jeweiligen Worktree-Root; Interpreter-Auflösung via Wrapper
(AGENTS.md: niemals bare pytest/python im hermes-Worktree).

```bash
# hermes-agent Worktree (Branch kimi/reporting-audit)
cd /home/piet/.hermes/worktrees/kimi-reporting-audit
scripts/run_tests.sh tests/cron tests/hermes_cli/test_cron.py -q -p no:cacheprovider
# Stand Loop 1-6+10:  45 Dateien, 931 Tests passed, 0 failed (22.6s, 8 Workers)
#   (nur tests/cron allein: 44 Dateien, 915 Tests — die Differenz 16 = test_cron.py)

# scripts Worktree (Branch kimi/reporting-audit)
cd /home/piet/.hermes/worktrees/kimi-reporting-scripts
python3 -m pytest tests/ -q     # Stand Loop 7-12: 39 passed (hermetisch, kein Netz)

# Live-Verifikation des Konsistenz-Sensors (read-only gegen Live-Store)
python3 reporting-consistency-check.py
```

Codex-Bewertungen: Baseline `eval/2026-07-29-codex-baseline.json` (5.6/10),
Zweitbewertung `eval/2026-07-29-codex-final.json` (7.1/10) — beide via
`codex exec --sandbox read-only --output-schema` mit identischem Rubric-Prompt.

---

## H. Nachschärf-Loops 13–16 (auf Codex #2 = 7.1/10)

Jeder `top_issue`-Punkt aus Codex #2 und sein Status:

| Codex-Befund | Loop | Fix | Commit |
|---|---|---|---|
| cron-of-crons false-green (error ohne consecutiveErrors, nie gelaufen ⇒ HEALTHY) | 14 | `last_status∈{error,failed,crashed,timeout}` ⇒ ERRORING; NEVER_RAN-Klasse; Baseline-Test exakt auf voice-spar-smoke + Negativprobe | `e458ae2` (scripts) |
| Outbox kollabiert Runs (nur job_id+target-Key) | 13 | per-execution_id append-only, nie zusammenfalten; flock-Locking (RMW + replay_lock, ProcessPool-Test) | `da082a6428` |
| Replay nur Built-in-Tick | 13 | gemeinsamer Pfad gefunden: `run_one_job` (Chronos fire_due → run_one_job); Replay dort + Built-in, dedupliziert via replay_lock + next_retry_at-Gate | `da082a6428` |
| Relay/Config-Fehler nicht enqueued | 13 | error_class send/config/relay; config+relay dead-on-arrival (kein Endlos-Queue, dedupe pro job+target+class) | `da082a6428` |
| retry/timeout-Felder nicht konfigurierbar | 15 | `validate_retry_policy`/`validate_timeout_field` (eine Kopie in jobs.py), exponiert in cronjob-Tool (create+update, Clear-Semantik) + CLI-Flags (`--retry-attempts/--retry-backoff/--timeout-seconds/--delivery-timeout-seconds`) | `9fbff42c04` |
| Restore validiert nur Hülle; kein Rollback; keine Konkurrenztests | 15 | `_is_plausible_job_record` (Pflichtfelder/Typen), korrupte Records verworfen; Rollback der Quarantäne bei Schreibfehler; Recovery unter `_jobs_lock` (4-Thread-Test) | `9fbff42c04` |
| Retention mengenbasiert, keine 30d | 15 | Zeithorizont: löschen nur jenseits keep-N UND >30d (`HERMES_CRON_EXECUTIONS_KEEP_DAYS`) UND kein failed/unknown in Frist; Kappe 20000 hart (gepinnt) | `9fbff42c04` |
| systemctl nonzero rc ignoriert | 14 | rc-Matrix: 0=parse; 1+leer=still; sonst Sensorfehler (systemd 255 live vermessen) | `ac016ed` (scripts) |
| notify Deadline nicht durchgängig; retry_after nur Header | 14 | monotonic-Gesamtbudget (Requests+Sleeps+Fallback, 1s-Floor, Fallback-Mindestbudget); retry_after Header>JSON-Body | `43265dd` (scripts) |
| Proposals nicht appliziert | — | BEWUSST: Operator-Auftrag „kein Deploy/Merge". Liegt reviewfähig in proposals/ (C1–C9 inkl. E2E-Canary) | `ea2096e` u.a. |
| Gate-Kommandos nicht reproduzierbar | 16 | Sektion G: exakte Kommandos + Counts (931 vs 915 erklärt) | (docs) |

**Teststand final:** hermes 976/976 (49 Dateien) + tool-nah 228/228 · scripts 54/54.

---

## I. Schärf-Loops 17–19 (auf Codex #3 = 8.2/10)

| Codex-#3-Befund | Loop | Fix | Commit |
|---|---|---|---|
| execution_id nicht im Job-Dict für direkte/Tool-Runs → Outbox-Kollision | 17 | `run_one_job` schreibt execution_id vor `_deliver_result` ins Dict; Enqueue ohne ID für produktive Pfade = WARNING | `bb8d1f0160` |
| config/relay Dead-Letters überschreiben Payloads | 17 | append-only pro execution_id; Sichtbarkeits-Cap 3/(job,target,class) ersetzt ältesten, kein Payload-Verlust ≤3 | `bb8d1f0160` |
| Replay nicht crash-genau | 17 | Send-Lease (`sending`+lease_ts+pid VOR Send), Receipt (platform-message-id bzw. payload-hash), verwaischte Lease→Retry; ehrlicher Restspalt (at-least-once, sichtbar) im Docstring | `bb8d1f0160` |
| load_gateway_config-Fehler nicht in Outbox | 17 | Config-Load-Pfad enqueued error_class=config | `bb8d1f0160` |
| 30d-Retention durch 20k-Kappe brechbar; ISO-String-Vergleiche | 18 | horizon-aware Kappe (löscht nur out-of-horizon, sonst Warnung statt Garantie-Bruch); alle Zeitvergleiche via julianday (UTC-normalisiert, Offset-sicher) | `8e43f26284` |
| Restore validiert nur grobe Hülle | 18 | Full-Schema-Validierung (32 Typ-Fälle, Extra-Felder toleriert); Cross-PROCESS-Recovery-Test (2× subprocess, 1 Quarantäne) | `8e43f26284` |
| notify 1s-Floor widerspricht harter Deadline | 18b | strikt: Timeout = Restbudget, ≤0.05s ⇒ kein Request (rc=1); Fallback = exaktes Restbudget; Tests mit simulierter Request-Laufzeit | `17bed29` (scripts) |
| (Nebenfund Loop 18) load_jobs-Race: exists() vor Lock ⇒ stiller leerer Store im Quarantäne-Fenster | 19 | Re-Check unter `_jobs_lock()`; Test: Loader blockiert bis Recovery fertig | `92f6791249` |
| Sektion H überzeichnet / 228-Kommando fehlt | 19 | Diese Tabelle + präzisierte Gates unten | (docs) |

**Ehrliche Restgrenzen (von Codex akzeptierte Design-Trade-offs, keine Lücken):**
at-least-once-Restspalt zwischen Send und Receipt-Write (ohne plattformseitige
Idempotenz-Keys nicht schließbar, aber sichtbar); Prune-Warnung noisy bei
Dauer-Überlauf; Live-Proposals weiterhin nicht appliziert (Auftragsgrenze).

**Teststand final:** hermes **1004/1004** (53 Dateien) · scripts **58/58**.

### Präzisierte Gate-Kommandos (ersetzt/ergänzt Sektion G)

```bash
cd /home/piet/.hermes/worktrees/kimi-reporting-audit
scripts/run_tests.sh tests/cron tests/hermes_cli/test_cron.py -q -p no:cacheprovider
#   final: 53 Dateien, 1004 Tests passed, 0 failed (23.3s)

# tool-nahe Suiten (Loop-15-Verifikation, 228 Tests):
scripts/run_tests.sh tests/tools/test_cronjob_tools.py tests/tools/test_cronjob_run_immediate.py \
  tests/tools/test_cron_prompt_injection.py tests/gateway/test_api_server_jobs.py \
  tests/cron/test_cronjob_schema.py tests/hermes_cli/test_gateway_restart_loop.py -q -p no:cacheprovider

cd /home/piet/.hermes/worktrees/kimi-reporting-scripts
python3 -m pytest tests/ -q     # final: 58 passed
```

---

## J. Feinschliff-Loops 20–22 (auf Codex #4 = 8.8/10)

| Codex-#4-Befund | Fix | Commit |
|---|---|---|
| Hash-„Receipt" als Proof überzeichnet | strukturiert `receipt.kind`: `platform_message` (echter Provider-Beleg) vs. `local_send_witness` (ausdrücklich NUR lokaler Send-Nachweis, kein Zustellbeleg) | `cc051cb0cf` |
| `sending`/Lease/Receipt nicht in `hermes cron status` | `outbox_status()` + Statusanzeige: sending-Count, ältestes Lease-Alter, ⚠ bei TTL-überschrittenen Leases | `cc051cb0cf` |
| enqueue akzeptiert fehlende execution_id mit WARNING | fail-closed ValueError für send-class; deprecated Opt-in nur für Legacy-Tests; Zwei-Prozesse-Konkurrenz-Test | `cc051cb0cf` |
| list/latest_executions rohe ISO-Sortierung | julianday für ORDER BY + Cursor + Subquery (jetzt wirklich ALLE Ledger-Zeitvergleiche, nicht nur Prune) | `cc051cb0cf` |
| Restore-Validierung nested grob | zentrale `_SCHEDULE_KIND_PAYLOAD`-Tabelle; nested origin/retry (gespeicherte Form, `_is_plausible_retry_policy`) | `cc051cb0cf` |
| Lease-TTL Env akzeptiert NaN/inf/negativ | nur endliche positive Floats, sonst laut Default 600 | `cc051cb0cf` |
| urllib Socket-Timeout ≠ harte Wall-Clock | Thread-Klammer: join(exakt Restbudget), `_HardDeadlineExceeded`, rc=1 ohne auf Thread zu warten; Slow-Server-Test mit echter Zeitmessung | `0bbacd0` (scripts) |

**Bekannte Restgrenzen (bewusste, dokumentierte Trade-offs — keine Behauptung
von Lückenlosigkeit):** at-least-once-Restspalt zwischen Send und Receipt-Write
(ohne plattformseitige Idempotenz-Keys nicht schließbar; die `sending`-Lease
macht den Zustand sichtbar, ein Duplikat nach Lease-Ablauf bleibt möglich);
`local_send_witness` ist kein externer Zustellbeleg; verwaister Daemon-Thread
bei hard-deadline lebt bis Prozessende; Live-Proposals nicht appliziert
(Auftragsgrenze).

**Teststand final:** hermes **1058/1058** (56 Dateien) + tool-nah 228/228 · scripts **60/60**.

---

## K. Abschluss-Loops 23 + Endergebnis

| Codex-#5-Restbefund | Fix | Commit |
|---|---|---|
| HTTPError-Body außerhalb Deadline-Klammer | `_run_with_deadline` verallgemeinert; Fehlerbody im selben Bracket, 64-KiB-Cap, Header-retry_after-Fallback | `bf721c0` (scripts) |
| `DISCORD_NOTIFY_TOTAL_TIMEOUT=inf` ⇒ OverflowError | finite-positive-Validierung (Env + Flag), lauter Default | `bf721c0` (scripts) |
| Cursor überspringt Gleichstände | zusammengesetzter Cursor (claimed_at, id), rückwärtskompatibel | `ead25087d7` |
| „delivery proof"-Wording in scheduler.py | Docstring-Ehrlichkeit (local_send_witness vs. platform_message) | `ead25087d7` |
| „holder crashed" kategorisch | „stale/ambiguous — crashed OR slow; retry-due" | `ead25087d7` |
| öffentlicher Legacy-Umgehungspfad | `_allow_legacy_no_execution` privat, test-only | `ead25087d7` |

## Endergebnis

**Codex-Score: 5.6 → 7.1 → 8.2 → 8.8 → 9.1/10** (Ziel ≥9 erreicht, Bewertung #5:
`eval/2026-07-29-codex-final4.json`).
Kategorien final: delivery_reliability 9.1 · failure_handling 8.9 · scheduler_hygiene 9.4 ·
observability 9.1 · code_quality 9.0 · test_coverage 9.5 · documentation_truth 8.8.

**Tests final:** hermes **1065/1065** (57 Dateien) + tool-nah **228/228** · scripts **64/64**.
**Loops:** 23 (12 geplant + 11 Schärf-Loops auf Codex-Findings). **Commits:** 15 (hermes) + 14 (scripts).

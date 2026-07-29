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

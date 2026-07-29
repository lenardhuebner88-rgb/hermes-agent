• Ich beginne mit der Audit-Doku und den Diff-Stats, dann verteile ich die Detailprüfung.

• Jetzt lese ich die Outbox selbst (Herzstück) und starte parallel die vier Detailprüfungen.

• 1065/1065 bestätigt. Ich prüfe noch die 228er-Gate-Behauptung und verifiziere die Medium-Findings der Agents stichprobenartig selbst.

• ## K3 Schlussreview

  ### Verdict

  **Ampel: GELB (grüne Tendenz).** Die Kernsubstanz — Outbox mit Lease/Receipt/Dedupe, Store-Scoping, Restore-Validierung, Deadline-Bracket — ist real, rückwärtskompatibel (gegen Live-Daten belegt, nicht nur behauptet) und ehrlich getestet; alle drei Gates reproduziert (1065/1065 · 228/228 · 64/64). Aber: Zwei Medium-Lücken sitzen genau an den Rändern des Delivery-Timeout-Features und erzeugen dort die Duplikate/Dead-Letters, die es abschaffen sollte, `cron-of-crons-review.py` produziert **heute** konkret falsche Report-Inhalte, und die Doku hat messbare Wahrheitsabweichungen. Kein critical, kein high — aber nicht „fertig".

  ### Findings

  **Outbox / Delivery (Kernfrage: Verlust oder Duplikat?)**

  - **[medium] `cron/scheduler.py:2230-2236` (`_deliver_result`, RuntimeError-Fallback)** — Der Fallback umgeht das gesamte neue Timeout-Konzept: rohe Coroutine, hartkodierte `future.result(timeout=30)`, ignoriert `delivery_timeout_seconds` und `HERMES_CRON_DELIVERY_TIMEOUT`. Bei Timeout läuft der Send im Pool-Thread weiter (`pool.shutdown(wait=False)` cancellt nicht) — der Spät-Send kann ankommen, während `_note_failed_target()` (Z. 2251) bereits enqueued hat → **Replay sendet ein zweites Mal: garantiertes Duplikat**. Selbst verifiziert (Z. 2233-2236). *Fix: Fallback-Coroutine ebenfalls in `asyncio.wait_for(coro, delivery_timeout)` wrappen und im Timeout-Fall den Loop cancellen.*
  - **[medium] `cron/scheduler.py:2365` (`_send_outbox_entry`)** — Replay ruft `_get_delivery_timeout()` **ohne** `job`-Argument → per-Job `delivery_timeout_seconds` gilt nur für den Erstversuch. Ein Job mit legitimen 300s scheitert initial, jeder Replay timed out bei 60s, nach 5 Versuchen DEAD — ein verlorener Report trotz korrekter Job-Konfiguration. *Fix: aufgelöstes Timeout beim Enqueue auf der Entry persistieren und im Replay daraus lesen.*
  - **[low] `cron/delivery_outbox.py::begin_replay` (Z. 678-691)** — Der Stale-Lease-Retake inkrementiert `attempts` nicht (nur `send_attempt`). `attempts` wächst ausschließlich im recorded-failure-Pfad (`record_replay_result`, Z. 739). Ein Eintrag, dessen Send den Prozess reproduzierbar zwischen Lease und Record crasht, wird **unendlich** alle Lease-TTL erneut gesendet und nie dead-lettered — die MAX_ATTEMPTS-Schranke gilt nicht im Crash-Fenster. *Fix: bei Retake eines stalen Lease `attempts += 1` (Crash ist ein Versuch).*
  - **[low] Lease-TTL vs. Delivery-Timeout entkoppelt** — `HERMES_CRON_DELIVERY_TIMEOUT` ist unbegrenzt env-setzbar, per-Job-Max (600s) == Default-Lease-TTL. Timeout ≥ TTL ⇒ ein Zweitprozess hält einen **lebenden** Send für verwaist und sendet doppelt. `lease_pid` wird gespeichert, aber nie für eine Liveness-Prüfung genutzt — `executions.py::_owner_is_live` zeigt, dass das Muster existiert. *Fix: Replay-Timeout auf `< lease_ttl` clampen oder `os.kill(pid, 0)`-Check in `begin_replay`.*
  - **[low] `cron/scheduler.py::_replay_delivery_outbox`** — `begin_replay`/`record_replay_result` sind gewrappt, der `_send_outbox_entry`-Aufruf dazwischen nicht. Ein synchroner Fehler dort (Import, Coroutine-Aufbau) bricht den ganzen Tick, bevor irgendein Job feuert — der Docstring verspricht „never raises". *Fix: try/except um den Send-Aufruf, Fehler als `record_replay_result(success=False)`.*

  **Ledger / Jobs-Store**

  - **[medium] `cron/executions.py::_prune_unlocked`** — Die Doku-Behauptung „globale Kappe 1000→20000" ist irreführend: die horizon-aware Kappe löscht nur out-of-horizon. Ein minütlicher Dauer-Failer erzeugt ~43k geschützte Zeilen/30d — die „Kappe" ist kein Bound, sondern ein Alarm. Und der Alarm feuert **bei jedem `finish_execution`** (im Beispiel: jede Minute, einen Monat lang) per `logger.warning` **plus `print(..., file=sys.stderr)`** — ein `print` in Bibliothekscode, der jede Logging-Konfiguration umgeht. *Fix: Warnung zeitlich deduplizieren, `print` streichen, Doku korrigieren.*
  - **[medium] `cron/executions.py::list_executions/latest_executions`** — `ORDER BY julianday(claimed_at) DESC` schlägt beide existierenden Indizes aus (Funktion auf der Spalte). `latest_executions` läuft pro Job bei jedem `hermes cron status`/Dashboard-Call — mit dem Wachstum aus dem vorigen Finding wird jeder Status-Call zum Full-Sort. *Fix: Expression-Index `ON executions(job_id, julianday(claimed_at) DESC, id DESC)` (läuft via `IF NOT EXISTS` ohne Migration).*
  - **[low] `cron/jobs.py::load_jobs`** — Der Loop-19-Fix lässt zwei Restlücken: (a) `exists()` true → fremde Quarantäne-Rename → `open()` wirft `FileNotFoundError` → `RuntimeError` statt Re-Check; (b) `_jobs_lock()` degradiert nach 30s flock-Timeout still auf in-process — genau dann ist das Quarantäne-Fenster wieder offen. *Fix: `FileNotFoundError` im `open()` abfangen und Re-Check unter Lock wiederholen; Degraded-Mode als Restgrenze dokumentieren.*
  - **[low] `cron/jobs.py::_normalize_retry`** — `float("inf")` passiert die Validierung; `json.dump` schreibt `Infinity` → kein valides JSON mehr für strikte Parser. *Fix: `math.isfinite(delay)`.*
  - **[low] Quarantäne-Dateien akkumulieren ohne Retention; erste Korruption nach Deploy hat noch kein `.bak.1`** — Deploy-Fenster ist in der Doku unbenannt.

  **Scripts-Worktree**

  - **[medium] `cron-of-crons-review.py::classify` (Z. 124-134)** — Ein deaktivierter Job mit **rezentem** `last_run` fällt durch den `try`-Block und landet bei `return "OBSOLETE", "disabled, never recorded last_run"` — falsche Evidenz (der Job lief!) und Klassifikation gegen die eigene Docstring-Definition. Live betroffen **heute**: „Daily Langfuse Score Export" (disabled, last_run vor 3d) + drei weitere. Selbst verifiziert. *Fix: nach dem `try` nur OBSOLETE wenn kein `last_run` existiert; disabled+rezent nicht actionable.*
  - **[medium] `cron-of-crons-review.py::_load_jobs_file`** — `except Exception: return []` → korruptes `jobs.json` ergibt „0 crons inspected", `actionable == 0` → **stiller grüner Tick**. Exakt die false-green-Klasse, die dieser Zyklus bekämpft, auf Store-Ebene offen. Der neue `reporting-consistency-check.py` macht es richtig, dieser Sensor nicht. *Fix: Store-Lesefehler als SENSOR-ERROR-Klasse in `actionable` zählen.*
  - **[low] `cron-of-crons-review.py:136`** — `int(consec_errors)` ungeguardet: ein Job mit Garbage-Wert killt den **gesamten** Report (ValueError, kein Fang in `main`). *Fix: try/except → 0.*
  - **[low] `cron-of-crons-review.py`** — Drei Kleinigkeiten: `has_wiki_doc` (Z. 111): `jid == ""` matcht immer (`"" in did`); `last_delivery_error` wird bei der Klassifikation ignoriert — ein Job mit `last_status=ok` und fehlgeschlagener Zustellung liest als HEALTHY (der Silent-Report-Loss-Blindspot); toter `import subprocess` (Z. 37).
  - **[low] `reporting-consistency-check.py`** — Prüft nur den Root-Store; die 5 Live-Profil-Stores (u.a. research mit 10+ Jobs, eigenes deliver-Target) sind unsichtbar. Docstring verspricht „the reporting jobs in the Hermes cron store". *Fix: Profil-Stores iterieren wie `cron-of-crons-review.load_hermes_profile_crons()` — aber Profil-Skript-Roots beachten, sonst false-positives.*
  - **[low] `discord-notify.py`** — Drei Restrisiken, alle Richtung Duplikat, nicht Verlust: (a) rc=1 nach hard-deadline heißt *maybe-sent* (verwaister Thread kann noch zustellen) — im Docstring nicht benannt, ein blind wiederholender Caller dupliziert; (b) Sub-50ms-Fenster in `_attempt`: bei Budget ≤ 0 wird der Request-Thread trotzdem gestartet und kann physisch senden; (c) 8-MiB-Guard ist TOCTOU (stat in `main`, read pro Retry in `_build_request`).
  - **[low] `discord-channel-health.py:30-31`** — Kommentar zitiert „root jobs.json deliver (kanban-notifier)" — dieser Job existiert in **keinem** Live-Store. Ironisch: der neue Consistency-Sensor flaggt genau das als NO-JOB; der Kommentar blieb unkorrigiert.
  - **[low] `systemd-onfailure-watcher.py`** — rc=1-Varianten mit „0 loaded units listed." auf **stdout** (statt stderr) → false alarm. Fail-visible-Richtung, daher low.

  **Proposals**

  - **[medium] `proposals/2026-07-29-live-state-changes.md` (C9)** — „Job-Definitionen liegen hier bereit" ist überzogen: Weder Canary-Skript noch Verify-Job existieren (`grep -ri canary` → nichts), und die Verify-Hälfte braucht einen Discord-**GET**, den `discord-notify.py` auch im neuen Stand nicht kann. Dazu versteckte Merge-Abhängigkeit: C9 „nutzt die neuen 429/Retry-Pfade", die live gar nicht deployed sind. *Fix: als Design-Skizze kennzeichnen, Merge als Vorbedingung notieren.*
  - **[low] Apply-Pfad Proposal 2** — `_kanban_db_guard.py` ist live **untracked**; der Apply-Schritt „Commit der Fremd-Arbeit" vergisst genau diese Datei am leichtesten → alle drei neuen Sensor-Jobs schlagen mit ImportError fehl. Und: die „read-only"-Behauptung für kanban-followup gilt nur für die Live-Version — die Worktree-Version ist noch der alte mutierende Code (`INSERT INTO tasks`); wer den Worktree über Live bügelt, aktiviert mit dem */30-Job einen Board-Schreiber. *Fix: Go-Kriterium (`head` der Live-Datei) ins Proposal.*
  - **[low] C3** — Zahl stimmt nicht (56 `#OPENCLAW-OFF`-Zeilen, nicht „~60"); `oma-sync` hat bereits flock, es fehlt nur `timeout`.
  - **Verifiziert korrekt:** rca-deliver-Fix passt exakt (Live-Job `deliver: local` vs. Docstring `discord:…073`); alle Cron-Syntaxen valide; Job-Schema ist exakte Teilmenge des Live-Schemas; keine Namenskollisionen; C1/C4/C5-Fakten (Timer-Leichen, nightly-audit ohne OnFailure/Timeout, Präzedenzfall) alle gegen Live-System bestätigt.

  **Doku-Wahrheit**

  - **[low] Commit-Zählung falsch** — „Commits: 15 (hermes) + 14 (scripts)": tatsächlich **20 hermes** (12 Code + 8 Doku/Eval) und **15 scripts** (`git rev-list --count`). Auch „15" als reine Code-Commits stimmt nicht (12).
  - **[low] „globale Kappe 1000→20000"** — siehe executions-Finding oben: die Formulierung suggeriert eine Schranke, die es nicht mehr gibt.
  - **Korrektur zu meinem eigenen Briefing:** Ich hatte den Agents „Env `DISCORD_NOTIFY_LEASE_TTL` analog" als Doku-Behauptung vorgegeben — das war **mein Fehler** (Konflation mit `HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS`); die Doku enthält diese Behauptung nicht (grep-verifiziert). Kein Doku-Finding an dieser Stelle.

  **Testqualität**

  - **[low] `tests/test_discord_notify.py::TricklingFp`** — fehlende `close()`-Methode → `PytestUnraisableExceptionWarning`, die pytest irreführend `test_systemd_onfailure_watcher.py` zuschreibt. *Fix: `def close(self): pass`.*
  - **Explizit nichts gefunden:** keine zirkulären Tests (keine Mocks, die die zu prüfende Logik duplizieren) — die Stichproben (Outbox-Loop6/17/20, Composite-Cursor, Cross-Process-Recovery, Load-Race, Deadline-Tests) arbeiten gegen echte tmp-Stores, echte SQLite, echte Subprozesse/Prozesspools und echte Zeitmessung; der eine `sleep(0.2)` im Load-Race-Test kann nur Richtung false-fail wirken. Keine Timing-Flake-Muster.

  **Sicherheit — explizit nichts gefunden:** kein `shell=True`, keine String-Interpolation in Kommandos; Bot-Token aus Env/`.env`, nie in argv (nicht via `ps` sichtbar), nicht in Fehlermeldungen; Multipart-Filename sanitisiert; `allowed_mentions: {"parse": []}`; atomare Writes mit 0o600; kein fail-open bei fehlendem Token (rc=1). Einzige fail-open-Stelle ist die `_load_jobs_file`-Ausnahme oben (Verfügbarkeit, nicht Schutz).

  ### Was verifiziert korrekt ist (Stichproben-Liste)

  - **Gates exakt reproduziert:** hermes 1065/1065 (57 Dateien, 26.6s), tool-nah 228/228 (6 Dateien), scripts 64/64 — deckt sich exakt mit Sektion K der Doku.
  - **Rückwärtskompatibilität belegt, nicht behauptet:** executions.db-Schema unverändert (keine Migration nötig, Live-DB: 1001 Zeilen, String- vs. julianday-Ordnung identisch); **alle 47 Live-Job-Records über 10 Profile passieren die neue Full-Schema-Validierung** (0 Rejects — kein Quarantäne-Datenverlust); `run_one_job`-Signatur unverändert, alle 4 Caller kompatibel; alte jobs.json ohne neue Felder fallen sauber auf Defaults.
  - **Outbox-Kern (eigene Lektüre, `cron/delivery_outbox.py` vollständig):** Dedupe kollabiert nur gleiche `execution_id` bei Status `queued`, verschiedene Runs nie; `next_retry_at` wird bei Refresh nicht verschoben (wie dokumentiert); `config`/`relay` dead-on-arrival mit append-only + Cap 3; Lease steht persistiert **vor** dem Send; Receipt-Honesty (`platform_message` vs. `local_send_witness`) ist strukturell erzwungen, unbekannte Strings werden nie zu Plattform-Belegen hochgestuft; fail-closed ValueError bei send-class ohne execution_id; flock + RLock + Replay-Gate korrekt, atomare Rewrite-Compaction mit fsync + 0o600; TTL-Env-Validierung weist NaN/inf/negativ laut ab.
  - **`_running_job_ids`-Freigabe** in allen Pfaden (`finally` in `_run_and_release`, Z. 4719-4724) — e2e-getestet („wedged send ⇒ Job frei", Folge-Tick feuert erneut).
  - **ContextVar-Scoping** propagiert via `contextvars.copy_context()` in Pool-Threads; Profil-Isolation inkl. identischer Job-IDs über Profile getestet; Job-IDs sind uuid4 → keine Kollision.
  - **Composite Cursor** rückwärtskompatibel (keyword-only `before_id`, alte Caller unverändert), Vollpagination ohne Lücke/Duplikat getestet, Offset-Normalisierung (`+02:00` == `Z`) bewiesen — der String-Vergleich wäre hier tatsächlich falsch gewesen.
  - **Backup-Rotation/Quarantäne:** Rotation nur nach erfolgreichem `atomic_replace` unter Lock (kein korruptes Backup überschreibt ein gutes), Restore iteriert bak.1→3, Cross-Process-Test beweist genau eine Quarantäne bei 2 Subprozessen, Rollback bei Schreibfehler vorhanden, Lock-Reentrancy via Depth-Counter deadlockfrei.
  - **discord-notify Deadline-Klammer ist echt:** der Codex-#4-Repro (0,301s bei 0,1s-Budget) ist geschlossen — HTTPError-Body-Read jetzt im eigenen Bracket mit 64-KiB-Cap, Test `test_trickling_error_body_stays_under_deadline` grün; Retry-After Header > Body mit Clamping; Backoff kann das Budget nie überschreiten (`min(wait, budget_left)`); kein 1s-Floor mehr.
  - **Eval-Kette integer:** 5 JSONs mit den dokumentierten Scores 5.6 → 7.1 → 8.2 → 8.8 → 9.1, Befund-Ketten in den `top_issues` decken sich mit den Fix-Commits.
  - **Behauptete Testcharakteristik stimmt:** ProcessPool-flock-Test, 2×-Subprocess-Recovery, „Loader blockiert bis Recovery fertig" (Event-synchronisiert, kein nackter Race), Deadline-Tests mit echter Zeitmessung — alle existieren wie beschrieben und sind hermetisch (kein Netz, kein reales `~/.hermes`).

  **Konkrete Fix-Reihenfolge:** (1) die zwei Medium-Delivery-Lücken (Fallback-Timeout + Replay-Timeout) — sie unterlaufen das Feature an genau den Kanten, die Codex mit 9.1 bewertet hat; (2) `cron-of-crons` M1+M2 (produziert heute falsche/stille Reports); (3) C9-Ehrlichkeit + Commit-Zählung in der Doku; (4) `attempts`-Inkrement im Stale-Lease-Retake. Der Rest ist berechtigterweise Backlog.


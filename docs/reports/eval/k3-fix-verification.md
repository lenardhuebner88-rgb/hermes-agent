• Ich starte drei parallele Verifikationen — je Repo/Commit-Gruppe eine.

• ## K3 Fix-Verifikation

  ### Verdict
  🟡 **Gelb-Grün** — 15 von 17 Findings sauber behoben, alle Tests grün (hermes 1084/1084 cron-Suite, scripts 81/81). Zwei Abzüge: die **Merge-Falle in den Go-Kriterien ist nur teilweise gefixt** (die eigentlich geforderte `merge-base --is-ancestor`-Prüfung fehlt), und **L-coc(d) ist nicht eindeutig zuordenbar**.

  ### Pro Finding

  **Hermes `05b4988c9f`:**
  - M1 Fallback-Timeout: **behoben** — `wait_for(fb, delivery_timeout)` + thread-sicheres Cancel, `scheduler.py:2241-2274`
  - M2 Replay-Timeout persistiert: **behoben** — persistiert in `delivery_outbox.py:633-701`, gelesen im Replay `scheduler.py:2400-2425, 2480`
  - M3 Prune-Warnung dedup: **behoben** — `print`/`sys` raus, monotonic-Dedup 1×/h, Bedingung weiter je Prune, `executions.py:346-368`
  - M4 julianday-Indizes: **behoben** — `idx_executions_job_jd` + `idx_executions_jd` matchen die Query-Texte, `IF NOT EXISTS` im Connect-Pfad, `executions.py:215-222`
  - L1 attempts im Stale-Retake: **behoben** — Retake zählt `attempts+1`, ab 5 → dead, `delivery_outbox.py:798-814`
  - L2 Lease-Liveness + TTL-Clamp: **behoben** — `_lease_holder_is_live` (fail-safe) + `clamp_delivery_timeout_to_lease` an Enqueue und Replay
  - L3 Tick-Guard: **behoben** — try/except um `_send_outbox_entry`, Fehler wird regulärer Failed-Attempt, `scheduler.py:2568-2581`
  - L4 load_jobs FNFE-Re-Check: **behoben** — FNFE vor IOError gefangen, Re-Check unter Lock, `jobs.py:1475-1484`
  - L5 isfinite: **behoben** — `math.isfinite(delay) and delay > 0` im lenienten Pfad, `jobs.py:2309`
  - L6 Quarantäne-Retention 7d: **behoben** — mtime-basiert, Glob passt exakt zum Quarantäne-Namen, `jobs.py:1028-1055`

  **Scripts `759bf32`:**
  - M5 DISABLED-Klasse: **behoben** — ehrliche Evidence ("disabled, last run 3d ago"), "never recorded"-Lüge strukturell unmöglich, `cron-of-crons-review.py:139-153`
  - M6 SENSOR-ERROR: **behoben** — `STORE_ERRORS` gesammelt, im Report/Discord als actionable gezählt, `:75-91, 280-286, 333`
  - L-coc(a) Garbage consecutiveErrors: **behoben** — try/except → 0, `:155-158`
  - L-coc(b) leere Job-ID: **behoben** — `(jid and jid in did)`, `:126`
  - L-coc(c) Delivery-Fehler als HEALTHY: **behoben** — neue Klasse DELIVERY-FAILING, überall verdrahtet, `:171-175`
  - L-coc(d): **nicht verifizierbar** — kein Findings-Dok im Repo; zwei reale Härtungen im Diff (toter `import subprocess` entfernt, `str(last_run)`-Koercion), aber welches (d) war, ist unbelegt
  - L-rcc Profil-Stores: **behoben** — `profiles/*/cron/jobs.json` geladen, korrupt → sichtbarer STORE-ERROR, `[profile:<name>]`-Tags
  - L-dn(a/b/c): **behoben** — rc=1=MAYBE-SENT-Vertrag dokumentiert; Budget-Re-Check vor Thread-Start; Attachment 1× gelesen, Bytes durchgereicht, kein `open` im Retry
  - L-dch: **behoben** — Kanban-Channel-Kommentar korrigiert (repo-intern verifiziert; Live-jobs.json-Behauptung nicht prüfbar)
  - L-sow: **behoben** — "0 loaded units listed." auf beiden Streams benign, `:22-32`
  - L-test: **behoben** — 13 neue Tests, Suite 81/81 grün

  **Doku/Proposals `9bf6342e21` + `ab76a26`:**
  - C9 als Design-Skizze: **behoben** — "NICHT umsetzungsbereit", K3-Korrektur-Block, "Umsetzung erst nach Merge + Go"
  - C3-Zahlen: **behoben** — 56 OFF-Leichen live verifiziert (`crontab -l | grep -c` = exakt 56); 20+15-Commit-Zählung mit Herleitung, stichtagskorrekt
  - Go-Kriterien Merge-Falle: **teilweise** — neuer Abschnitt deckt Live-vs-Worktree-Skew ab (beide Beispiele live verifiziert), aber die geforderte `git merge-base --is-ancestor`/`patch-id`-Prüfung ("done+MERGED_GREEN ≠ Code auf main") kommt **nirgends** vor

  ### Neue Findings
  Keine Blocker; fünf kleine Residuen:
  1. **Enabled-Pfad `cron-of-crons-review.py:198`**: `last_run.replace()` ohne die neue `str()`-Koercion — nicht-String `last_run` bei enabled Job kann still HEALTHY lesen. Der Fix heilte nur den Disabled-Zweig.
  2. **Merge-Falle Go-Kriterien** (s.o.): `is-ancestor`-Check nachreichen — das war der Kern des Findings.
  3. **M1-Restgrenze**: `future.result(timeout=30)` kappt den Fallback-Erstversuch auf `min(30s, delivery_timeout)`; Kommentar "SAME bound" überverspricht für Jobs mit `delivery_timeout > 30` (kein Duplikat-Risiko).
  4. **`_entry_delivery_timeout`**: `int(float(raw))` bei `raw=inf` wirft ungefangenes `OverflowError` — nur über handeditierte Outbox-Files erreichbar, L3-Guard fängt es. Kosmetik.
  5. **Kosmetik**: Discord-Summary druckt `🚨 SENSOR-ERROR: 0` auch bei null Fehlern; `Union` in `delivery_outbox.py:528` nicht importiert (bricht nur `get_type_hints()`).


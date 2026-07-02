# ROUND — error-sweep: einen wiederkehrenden Service-Fehler fixen

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}} (SERVICES = zu prüfende user-Units, ZEITRAUM = journalctl --since).
Führe GENAU EINE Runde aus (ein Fund), dann beende den Turn.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md lesen — dort behandelte Fehlermuster überspringen.
2. **Finden**: pro Service aus SERVICES:
   `journalctl --user -u <service> --since "<ZEITRAUM>" -p warning --no-pager | tail -200`
   Suche WIEDERKEHRENDE Fehler/Tracebacks (≥2 Vorkommen), gruppiere nach Muster.
   Wähle EINEN Fund: wiederkehrend > einmalig, Traceback > Warnung, kundennah > kosmetisch.
   Transiente Selbstheiler (Netz-Blips, einzelne Timeouts mit Retry-Erfolg) zählen NICHT.
3. **Root-Cause im Repo**: den Code-Pfad des Fehlers finden ({{WT}} ist der Repo-Stand).
   Erst verstehen, dann fixen — kein Symptom-Pflaster (kein blindes try/except um die
   Fehlerzeile). Evidenz notieren: Log-Zeile(n) mit Zeitstempel + Datei:Zeile.
4. **Scope-Regel (hart, Per-Funktions-Regel aus dem Kimi-Loop):** reine Lese-/Parse-/
   Validier-/Format-Pfade darfst du fixen. Liegt die Ursache in einem **DB-Schreibpfad,
   im Dispatch oder in Auth/Credentials** → NICHT fixen: Fund mit Evidenz + Fix-Skizze
   in den Ledger, `BLOCKED schreibpfad <modul>` melden.
5. **Fixen**: minimaler Diff + Regressionstest, dessen Fixture das ECHTE Fehler-Artefakt
   nachstellt (Log-Payload/Input aus Schritt 2, nicht synthetisch). Test muss auf dem
   alten Code rot sein.
6. **Gate**: `git add -A && ./loops/gate.sh` (Exit-Code zählt) + den neuen Test explizit:
   `PYTHONPATH="$PWD" /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest -q \
     -p no:cacheprovider --timeout=120 <testpfad>`
7. **Grün** → GENAU EIN Commit: `loop(error-sweep): <service> <fehlermuster kurz>`
   + Ledger-Zeile: Muster, Vorkommen-Zahl, Evidenz-Zeitstempel, Datei:Zeile, Testpfad.
8. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <muster>` · `DRY` (nichts Wiederkehrendes gefunden) · `BLOCKED <grund>`.

## Verbote
NIE: push, merge, deploy, Service-Restarts (auch nicht „zum Testen"), Vollsuite,
DB-Schreibpfade/Dispatch/Auth anfassen (→ BLOCKED + Ledger), Schema-Migrationen,
kanban.db-Schreibzugriff, Secrets lesen/loggen, Upstream-Dateien, `web/package-lock.json`.

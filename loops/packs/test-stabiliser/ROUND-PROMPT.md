# ROUND — test-stabiliser: einen flaky/leaky Test stabilisieren

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}}. Führe GENAU EINE Runde aus (ein Fund), dann beende den Turn.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md lesen — dort behandelte Tests nicht wiederholen.
2. **Finden** (ein Fund, der wertvollste zuerst) — Quellen in dieser Reihenfolge:
   a. Jüngste Nacht-Gate-Ergebnisse: `ls -t ~/.hermes/logs/ | head` → green-gate-/
      Heartbeat-Logs auf rote/instabile Testdateien prüfen (Leaker-Listen zählen!).
   b. Bekannte Leaker-Muster im Code suchen:
      `rg -n "del sys.modules" tests/ | head -30` — Löschung OHNE Restore-Fixture ist
      das Top-Muster (Modul-Split); ebenso `rg -n "sys.modules\[" tests/`.
      Weitere Muster: modul-globale Mutation ohne Fixture-Restore, `time.sleep`-Timing,
      Netz-/Port-Bindung ohne Freigabe, ContextVar-Leaks.
   c. Verdächtige Datei 3× hintereinander laufen lassen (Beweis der Instabilität):
      `PYTHONPATH="$PWD" /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest -q \
        -p no:cacheprovider --timeout=120 <datei>` (3 Läufe; NIE die Vollsuite).
   WICHTIG (Repo-Eigenheit): die Suite läuft nachts per-File-isoliert — Cross-File-Leaks
   zeigen sich oft NUR, wenn zwei bestimmte Dateien nacheinander im selben Prozess
   laufen. Reproduktion dann: beide Dateien zusammen in EINEM pytest-Aufruf.
3. **Fixen**: minimaler Diff — Restore-Fixture (monkeypatch/try-finally), Isolation
   herstellen, Timing durch deterministisches Warten ersetzen. Den TEST robust machen,
   NICHT die Assertion aufweichen (kein „mach den Test grün, indem er weniger prüft" —
   das ist Reward-Hacking und fliegt im Morgen-Review raus). Wenn der Test ein echtes
   Produktions-Bug-Symptom zeigt: NICHT den Test biegen → `BLOCKED prod-bug <datei>`
   + Ledger-Notiz mit Evidenz.
4. **Stabilitäts-Beweis** (Exit-Codes zählen): die reparierte Datei 3× grün in Folge,
   bei Cross-File-Funden zusätzlich das Datei-Paar zusammen 2× grün. Danach
   `git add -A && ./loops/gate.sh`.
5. **Grün** → GENAU EIN Commit: `loop(test-stabiliser): <testdatei kurz> <muster>`
   + Ledger-Zeile mit Fund, Muster und Beweis (3/3 grün).
6. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <datei>` · `DRY` (ehrlich nichts Belegbares gefunden) · `BLOCKED <grund>`.

## Eskalation (Pflicht bei BLOCKED mit echtem Fund)
Ein BLOCKED, der nur im Ledger steht, ist ein toter Fund (Beleg 07-03 im error-sweep:
ein 40×-Auth-500-Bug blieb ohne Adressaten im Ledger liegen). Gilt hier besonders für
`BLOCKED prod-bug`: hänge ZUSÄTZLICH an {{STATE_DIR}}/ESCALATIONS.md an:

    ## <datum> — <fund-titel>
    - Evidenz: <Datei:Zeile / Testlauf-Output / Vorkommen-Zahl>
    - Blockiert weil: <Scope-Grund>
    - Fix-Skizze: <1–3 Zeilen>
    - Kanal-Vorschlag: <Kanban-Task | Operator | Pack <name>>

Die Morgen-Review liest diese Datei — so bekommt dein Fund einen Besitzer.

## Verbote
NIE: push, merge, deploy, Vollsuite, Produktions-Code ändern um einen Test grün zu
bekommen (nur echte Bugfixes mit Regressionstest sind erlaubt — im Zweifel BLOCKED),
Schema-Migrationen, Auth-/Secret-Pfade, kanban.db-Schreibzugriff, Upstream-Dateien,
`web/package-lock.json`, `tests/conftest.py`-Invarianten aufweichen.

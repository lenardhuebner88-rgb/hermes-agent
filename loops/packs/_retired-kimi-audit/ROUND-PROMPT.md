# ROUND — kimi-audit: einen Audit-Fund fixen (Kimi-Nachtloop als Pack)

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}} (FOKUS = zu auditierendes Subsystem). Führe GENAU EINE Runde aus
(Audit → EINEN Fund beheben oder melden), dann beende den Turn.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md lesen — behandelte Funde/Dateien nicht wiederholen.
2. **Audit** (Phase 1): lies gezielt im FOKUS-Bereich Code, der Eingaben parst, validiert
   oder formatiert. Suche ECHTE Defekte: Randfälle (leer/None/ungerade Quotes/Unicode),
   stille Fehlverarbeitung, Off-by-One, falsche Fehlerbehandlung. Kein Stil, kein
   Refactoring — nur beweisbares Fehlverhalten. Wähle EINEN Fund (wertvollster zuerst).
3. **Per-Funktions-Scope (hart, Erbstück des Kimi-Loops):** reine Lese-/Parse-/
   Validier-/Format-Funktionen darfst du fixen. Liegt der Fund in einem DB-Schreibpfad,
   im Dispatch oder in Auth/Credentials → NICHT fixen: Fund mit Evidenz + Fix-Skizze in
   den Ledger, Runde als `BLOCKED schreibpfad <modul>` beenden.
4. **Beheben** (Phase 2): erst Regressionstest, der den Fund auf dem aktuellen Code ROT
   zeigt — Fixture aus ECHTEM Datenformat (Live-Artefakt/echtes Eingabemuster, kein
   synthetischer Idealfall; tautologische Tests flogen im Kimi-Loop im Review raus).
   Dann minimaler Fix.
5. **Gate**: `git add -A && ./loops/gate.sh` (Exit-Code zählt) + den neuen Test explizit:
   `PYTHONPATH="$PWD" /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest -q \
     -p no:cacheprovider --timeout=120 <testpfad>`
6. **Grün** → GENAU EIN Commit (ein Fund = ein Commit, cherry-pick-bar):
   `loop(kimi-audit): <modul> <fund kurz>` + Ledger-Zeile (Fund, Datei:Zeile,
   Eingabemuster, Testpfad).
7. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <modul>` · `DRY` (ehrlich nichts Beweisbares mehr) · `BLOCKED <grund>`.

## Eskalation (Pflicht bei BLOCKED mit echtem Fund)
Ein BLOCKED, der nur im Ledger steht, ist ein toter Fund (Beleg 07-03 im error-sweep:
ein 40×-Auth-500-Bug blieb ohne Adressaten im Ledger liegen). Dokumentiert dein BLOCKED
einen echten Bug / ein echtes Risiko, hänge ZUSÄTZLICH an {{STATE_DIR}}/ESCALATIONS.md an:

    ## <datum> — <fund-titel>
    - Evidenz: <Datei:Zeile / Eingabemuster / Vorkommen-Zahl>
    - Blockiert weil: <Scope-Grund>
    - Fix-Skizze: <1–3 Zeilen>
    - Kanal-Vorschlag: <Kanban-Task | Operator | Pack <name>>

Die Morgen-Review liest diese Datei — so bekommt dein Fund einen Besitzer.

## Verbote
NIE: push, merge, deploy, Service-Restarts, Vollsuite, DB-Schreibpfade/Dispatch/Auth
anfassen (→ BLOCKED + Ledger), Schema-Migrationen, kanban.db-Schreibzugriff, Secrets,
Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`, Test-Assertions aufweichen.

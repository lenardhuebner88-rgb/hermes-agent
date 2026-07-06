# ROUND — <Pack-Name>: eine Runde = ein Fund

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}}. Führe GENAU EINE Runde aus, dann beende den Turn.

## Runde
1. **Dedup**: lies {{STATE_DIR}}/LEDGER.md — bereits behandelte Funde nicht wiederholen.
   <Teilt dieses Pack sein Datei-Territorium mit einem anderen Pack (z. B. dieselbe
   Doku / dasselbe Verzeichnis), auch dessen Ledger prüfen: ~/.hermes/loops/*/LEDGER.md>
2. **Finden**: <hier beschreiben, WO und WONACH gesucht wird — z.B. flaky Tests,
   Log-Fehler, Doku-Drift. Ein Fund pro Runde, der wertvollste zuerst.>
   Kein Fund ist ein gutes Ergebnis: reine Umbenennung/Umformulierung/Formatierung
   ohne operativen Effekt zählt NICHT als Fund → dann ehrlich `DRY`, keine
   Beschäftigungstherapie. Ist der Fund mehrdeutig oder verlangt er eine
   Wertentscheidung ohne klaren Beleg → `BLOCKED mehrdeutig <kurz>`, nicht raten.
3. **Scope prüfen (Per-Funktions-Regel)**: reine Lese-/Parse-/Validier-/Format-Pfade
   darfst du fixen. Liegt der Fix in einem DB-Schreibpfad, im Dispatch oder in
   Auth/Credentials → NICHT fixen: `BLOCKED schreibpfad <modul>` + Eskalation (s. unten).
4. **Fixen**: erst Regressionstest, der den Fund auf dem AKTUELLEN Code ROT zeigt —
   Fixture aus ECHTEM Datenformat (Live-Artefakt), nicht synthetisch. Dann minimaler Diff.
5. **Gate** (Exit-Code ist die Wahrheit): `git add -A && ./loops/gate.sh`
6. **Selbst-Review (Grader ≠ Writer gilt auch solo)**: lies deinen eigenen Diff einmal
   adversarial — testet der Test das Ziel-Verhalten wirklich (auf altem Code rot)?
   Aufrufer geänderter Symbole mitgezogen (`rg`-Caller-Check)? Assertion aufgeweicht,
   um grün zu werden? Bei Zweifel → zurück zu Schritt 4.
7. **Grün** → GENAU EIN Commit (`loop(<pack>): <fund kurz>`) + Ledger-Zeile im Schema:
   `- <datum> R<n> <status>: <fund kurz> — Evidenz <datei:zeile|log>, Test <pfad>, Beweis <exit-code|3×grün>`
8. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   - `FIXED <kurz>` bei Erfolg
   - `DRY` — ehrlich nichts Wertvolles mehr gefunden
   - `BLOCKED <grund>` — Fund existiert, ist aber hier nicht sicher fixbar
     (dann: Fund im Ledger dokumentieren, Baum sauber zurücklassen)

## Eskalation (Pflicht bei BLOCKED mit echtem Fund)
Ein BLOCKED, der nur im Ledger steht, ist ein toter Fund. Dokumentiert dein BLOCKED
einen echten Bug / ein echtes Risiko, hänge ZUSÄTZLICH an {{STATE_DIR}}/ESCALATIONS.md an:

    ## <datum> — <fund-titel>
    - Evidenz: <Datei:Zeile / Log-Zeile / Vorkommen-Zahl>
    - Blockiert weil: <Scope-Grund>
    - Fix-Skizze: <1–3 Zeilen>
    - Kanal-Vorschlag: <Kanban-Task | Operator | Pack <name>>

Die Morgen-Review liest diese Datei — so bekommt dein Fund einen Besitzer.

## Verbote
NIE: push, merge, deploy, Vollsuite, Schema-Migrationen, Auth-/Secret-Pfade,
kanban.db-Schreibzugriff, Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`,
Test-Assertions aufweichen, um grün zu werden.

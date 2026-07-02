# ROUND — <Pack-Name>: eine Runde = ein Fund

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}}. Führe GENAU EINE Runde aus, dann beende den Turn.

## Runde
1. **Dedup**: lies {{STATE_DIR}}/LEDGER.md — bereits behandelte Funde nicht wiederholen.
2. **Finden**: <hier beschreiben, WO und WONACH gesucht wird — z.B. flaky Tests,
   Log-Fehler, Doku-Drift. Ein Fund pro Runde, der wertvollste zuerst.>
3. **Fixen**: minimaler Diff + Regressionstest gegen echtes Datenformat.
4. **Gate** (Exit-Code ist die Wahrheit): `git add -A && ./loops/gate.sh`
5. **Grün** → GENAU EIN Commit (`loop(<pack>): <fund kurz>`); Ledger-Zeile anhängen.
6. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   - `FIXED <kurz>` bei Erfolg
   - `DRY` — ehrlich nichts Wertvolles mehr gefunden
   - `BLOCKED <grund>` — Fund existiert, ist aber hier nicht sicher fixbar
     (dann: Fund im Ledger dokumentieren, Baum sauber zurücklassen)

## Verbote
NIE: push, merge, deploy, Vollsuite, Schema-Migrationen, Auth-/Secret-Pfade,
kanban.db-Schreibzugriff, Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`.

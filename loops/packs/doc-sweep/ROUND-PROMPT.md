# ROUND — doc-sweep: eine Doku-Drift korrigieren

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}} (FOKUS = zu prüfende Doku-Dateien/-Verzeichnisse).
Führe GENAU EINE Runde aus (ein Fund), dann beende den Turn.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md lesen — dort behandelte Doku-Stellen nicht wiederholen.
2. **Finden** (nur Dateien im Repo — FOKUS: AGENTS.md, README, CLAUDE.md im Repo, docs/,
   plugins/*/README; KEINE Vault-/Canon-Dateien, die liegen außerhalb des Repos): vergleiche
   die Doku gegen das tatsächliche Code-Verhalten. Suche EINE Drift — eine Behauptung, die
   nicht (mehr) stimmt (falscher Pfad/Port/Flag/Default, veraltetes Kommando, überholter
   Ablauf). Wähle den wertvollsten Fund (nutzernahe Fehlinformation > kosmetisches Detail).
   Reine Politik-/Entscheidungs-Inhalte sind kein Fund für diesen Loop — nur Fakten-Drift.
3. **Beleg**: Datei:Zeile der Doku-Stelle + Beleg im Code (Datei:Zeile/Funktion, die das
   tatsächliche Verhalten zeigt). Ohne beide Belege kein Fix.
4. **Fixen**: korrigiere NUR die Doku, damit sie dem Code entspricht — der Code wird
   NIEMALS an die Doku angepasst (das wäre ein Verhaltens-Change durch die Hintertür).
   CHANGELOG-artige Dateien (Historie/Releasenotes) nicht rückwirkend umschreiben, auch
   wenn sie inzwischen veraltet wirken.
5. **Gate**: `git add -A && ./loops/gate.sh` (Exit-Code zählt; bei reinen Doku-Änderungen
   sollte das trivial grün sein — ein rotes Gate bedeutet, dass mehr als Doku berührt wurde).
6. **Grün** → GENAU EIN Commit: `loop(doc-sweep): <datei kurz>`
   + Ledger-Zeile: Doku-Stelle, Code-Beleg, was korrigiert wurde.
7. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <datei>` · `DRY` (ehrlich keine Drift gefunden) · `BLOCKED <grund>`.

## Verbote
NIE: push, merge, deploy, Service-Restarts, Vollsuite, Schema-Migrationen, Auth-/Secret-
Pfade, kanban.db-Schreibzugriff, Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`,
Dateien außerhalb des Repos (keine Vault-/Canon-Dateien), inhaltliche Politik-Änderungen
(nur Fakten-Drift), CHANGELOG-artige Dateien rückwirkend umschreiben.

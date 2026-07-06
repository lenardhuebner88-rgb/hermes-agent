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
   ABER: wirkt das CODE-Verhalten als der eigentliche Bug (die Doku beschreibt den
   offensichtlich gewollten Vertrag, z. B. sicherheits- oder datenverlust-relevant),
   dann die Doku NICHT an das kaputte Verhalten anpassen — das würde eine Regression
   legitimieren. Stattdessen `BLOCKED prod-bug <datei>` + Eskalation (s. unten).
   CHANGELOG-artige Dateien (Historie/Releasenotes) nicht rückwirkend umschreiben, auch
   wenn sie inzwischen veraltet wirken.
5. **Gate**: `git add -A && ./loops/gate.sh` (Exit-Code zählt; bei reinen Doku-Änderungen
   sollte das trivial grün sein — ein rotes Gate bedeutet, dass mehr als Doku berührt wurde).
6. **Grün** → GENAU EIN Commit: `loop(doc-sweep): <datei kurz>`
   + Ledger-Zeile: Doku-Stelle, Code-Beleg, was korrigiert wurde.
7. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <datei>` · `DRY` (ehrlich keine Drift gefunden) · `BLOCKED <grund>`.

## Eskalation (Pflicht bei BLOCKED mit echtem Fund)
Ein BLOCKED, der nur im Ledger steht, ist ein toter Fund (Beleg 07-03 im error-sweep:
ein 40×-Auth-500-Bug blieb ohne Adressaten im Ledger liegen). Gilt hier besonders für
`BLOCKED prod-bug` (Code widerspricht dem dokumentierten Vertrag): hänge ZUSÄTZLICH an
{{STATE_DIR}}/ESCALATIONS.md an:

    ## <datum> — <fund-titel>
    - Evidenz: <Doku Datei:Zeile + Code Datei:Zeile, die sich widersprechen>
    - Blockiert weil: <Code-Fix nötig, außerhalb doc-sweep-Mandat>
    - Fix-Skizze: <1–3 Zeilen>
    - Kanal-Vorschlag: <Kanban-Task | Operator | Pack <name>>

Die Morgen-Review liest diese Datei — so bekommt dein Fund einen Besitzer.

## Verbote
NIE: push, merge, deploy, Service-Restarts, Vollsuite, Schema-Migrationen, Auth-/Secret-
Pfade, kanban.db-Schreibzugriff, Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`,
Dateien außerhalb des Repos (keine Vault-/Canon-Dateien), inhaltliche Politik-Änderungen
(nur Fakten-Drift), CHANGELOG-artige Dateien rückwirkend umschreiben.

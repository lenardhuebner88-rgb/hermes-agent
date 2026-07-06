# ROUND — repo-housekeeper: einen Hygiene-Fund melden, additiv-trivial fixen

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}} (FOKUS = Fund-Kategorien). Führe GENAU EINE Runde aus (ein Fund),
dann beende den Turn.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md lesen — dort behandelte Funde nicht wiederholen.
2. **Finden**: EINEN Hygiene-Fund aus FOKUS — verwaiste Artefakte (vergessene Debug-
   Skripte, tote TODO-Marker mit erledigtem Inhalt, .gitignore-Lücken, doppelte/obsolete
   Hilfsskripte). Beleg: Datei(en)/Pfad + warum verwaist/obsolet (z. B. `rg`-Caller-Check
   zeigt keine Referenz mehr, `git log` zeigt seit wann tot).
3. **Fix-Mandat prüfen (hart):**
   - **Additiv-trivial** (darfst du selbst fixen): fehlende `.gitignore`-Zeile ergänzen,
     einen toten/erledigten Kommentar entfernen, einen Tippfehler korrigieren — Tippfehler
     aber NUR, wo er operativ in die Irre führt (Kommando, Pfad, Flag, Fehlermeldung);
     reine Prosa-/Kommentar-Kosmetik ist KEIN Fund (Churn ohne Nutzen → dann DRY).
   - **Alles was Löschen von Dateien oder Branches bedeutet** (verwaistes Skript, doppelte
     Hilfsdatei entfernen, alten Branch löschen) → NICHT ausführen. Stattdessen: Empfehlung
     mit Begründung als Ledger-Fund festhalten und `BLOCKED <fund> — nur per Löschen
     behebbar` melden (destruktives FS ist Operator-Sache, nicht dieser Loop).
4. **Gate** (nur bei additiv-trivialem Fix): `git add -A && ./loops/gate.sh`.
5. **Grün** → GENAU EIN Commit: `loop(repo-housekeeper): <fund kurz>`
   + Ledger-Zeile mit Fund und Fix (oder Lösch-Empfehlung bei BLOCKED).
6. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <fund>` (additiv-trivial gefixt) · `DRY` (ehrlich nichts Wertvolles mehr
   gefunden) · `BLOCKED <fund> — nur per Löschen behebbar` (Fund dokumentiert, kein Fix).

## Eskalation (Pflicht bei BLOCKED)
Ein BLOCKED, der nur im Ledger steht, ist ein toter Fund (Beleg 07-03 im error-sweep:
ein 40×-Auth-500-Bug blieb ohne Adressaten im Ledger liegen). Damit deine Lösch-
Empfehlung einen Besitzer bekommt, hänge ZUSÄTZLICH an {{STATE_DIR}}/ESCALATIONS.md an:

    ## <datum> — <fund-titel>
    - Evidenz: <Pfad(e) + Caller-Check/git-log-Beleg, seit wann tot>
    - Blockiert weil: <nur per Löschen behebbar — Operator-Sache>
    - Fix-Skizze: <welche Datei(en)/Branch(es) löschen>
    - Kanal-Vorschlag: <Operator>

Die Morgen-Review liest diese Datei.

## Verbote
NIE: push, merge, deploy, Service-Restarts, Vollsuite, Schema-Migrationen, Auth-/Secret-
Pfade, kanban.db-Schreibzugriff, Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`,
Dateien löschen, Branches löschen (auch nicht „nur ein offensichtlich totes Skript").

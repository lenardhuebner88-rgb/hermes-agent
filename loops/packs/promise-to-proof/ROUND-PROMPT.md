# ROUND — promise-to-proof: eine unbelegte Verhaltens-Behauptung beweisen oder widerlegen

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}} (FOKUS = Signalwörter/Suchraum). Führe GENAU EINE Runde aus
(ein Fund), dann beende den Turn.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md lesen — dort behandelte Behauptungen nicht wiederholen.
2. **Finden**: `rg -n "immer|garantiert|thread-safe|nie|automatisch" <repo-doku/docstrings/kommentare>`
   — eine testbare, aber ungetestete Verhaltens-Behauptung ("Funktion X macht immer Y",
   "garantiert atomar", "läuft nie doppelt"). Wähle die wertvollste (Kernpfad > Randnotiz).
3. **Verifizieren**: prüfe im Code, ob die Behauptung stimmt.
   - **Behauptung stimmt, ist aber ungetestet** → schreib einen **Beweis-Test**, der die
     Behauptung wirklich prüft (nicht nur den Happy-Path drumherum), gegen ECHTES
     Datenformat (Fixture aus Live-Artefakt, nicht synthetisch erfunden).
   - **Behauptung ist falsch** → korrigiere die DOKU/den Docstring/Kommentar auf das
     tatsächliche Verhalten + Ledger-Fund mit Beleg (Datei:Zeile + Code-Gegenbeweis).
     Einen Code-Fix für die falsche Behauptung machst du NUR, wenn der betroffene Pfad
     reiner Lese-/Parse-Pfad ist (keine DB-Schreibpfade/Dispatch/Auth) — sonst
     `BLOCKED <grund>` melden (Per-Funktions-Regel wie error-sweep).
4. **Gate**: `git add -A && ./loops/gate.sh` (Exit-Code zählt) + bei neuem Test explizit:
   `PYTHONPATH="$PWD" /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest -q \
     -p no:cacheprovider --timeout=120 <testpfad>`
5. **Grün** → GENAU EIN Commit: `loop(promise-to-proof): <behauptung kurz>`
   + Ledger-Zeile: Behauptung, Fundort, Beweis-Test oder Doku-Korrektur, Beleg.
6. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <behauptung>` · `DRY` (ehrlich nichts Belegbares gefunden) · `BLOCKED <grund>`.

## Eskalation (Pflicht bei BLOCKED mit echtem Fund)
Ein BLOCKED, der nur im Ledger steht, ist ein toter Fund (Beleg 07-03 im error-sweep:
ein 40×-Auth-500-Bug blieb ohne Adressaten im Ledger liegen). Gilt hier besonders für
widerlegte Behauptungen in Schreibpfaden (falsches „garantiert atomar" o. ä. ist ein
Risiko-Fund): hänge ZUSÄTZLICH an {{STATE_DIR}}/ESCALATIONS.md an:

    ## <datum> — <fund-titel>
    - Evidenz: <Datei:Zeile / Code-Gegenbeweis>
    - Blockiert weil: <Scope-Grund>
    - Fix-Skizze: <1–3 Zeilen>
    - Kanal-Vorschlag: <Kanban-Task | Operator | Pack <name>>

Die Morgen-Review liest diese Datei — so bekommt dein Fund einen Besitzer.

## Verbote
NIE: push, merge, deploy, Service-Restarts, Vollsuite, Schema-Migrationen, Auth-/Secret-
Pfade, kanban.db-Schreibzugriff, Upstream-Dateien (`web/src/App.tsx`), `web/package-lock.json`,
Code-Fixes an DB-Schreibpfaden/Dispatch/Auth (→ BLOCKED + Ledger).

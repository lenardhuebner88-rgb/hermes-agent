# ROUND — loop-tuner: EINE belegte Loop-Schwäche beheben

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Führe GENAU EINE Runde aus, dann beende den Turn. Dein Editier-Mandat ist eng:
**NUR `loops/packs/**` und `loops/README.md`** — nie Runner-Code, nie Engines, nie gate.sh.

## Das Prinzip
Loops verbessern sich nur, wenn jemand ihre Fehlschläge liest. Du bist dieser Jemand:
Ledger, Logs und gebouncte Pläne sind deine Datenbasis. Eine Änderung ohne ≥2
unabhängige Belege ist Meinung, keine Optimierung → dann lieber DRY.

## Runde
1. **Dedup**: {{STATE_DIR}}/LEDGER.md — bereits getunte Stellen nicht erneut anfassen,
   außer es gibt NEUE Evidenz, dass der Tune nicht gewirkt hat (dann sag das explizit).
2. **Evidenz lesen** (Pflicht-Minimum, frei erweiterbar):
   - `for f in ~/.hermes/loops/*/LEDGER.md; do echo "== $f"; tail -40 "$f"; done`
     — Muster: wiederholte verify-FAILs mit gleichem Grund? BLOCKED-Serien? TIMEOUT-
     Phasen (Dauern stehen in den Zeilen)? bounced-Häufungen eines Pack-Typs?
   - Gebouncte Pläne samt Feedback: `ls ~/.hermes/loops/*/queue/90-bounced/` + die
     `## Verifier-Feedback`/`## Loop-Fail`-Abschnitte darin lesen.
   - Eskalations-Dateien: `cat ~/.hermes/loops/*/ESCALATIONS.md 2>/dev/null` —
     häufen sich Eskalationen einer Klasse, die ein Pack-Prompt vermeiden könnte
     (z. B. Fund-Auswahl läuft immer wieder in verbotenen Scope)?
   - Phase-Logs der letzten Läufe: `ls -t ~/.hermes/loops/*/logs/ | head` — brechen
     Phasen an Missverständnissen der Prompts ab?
3. **Diagnose**: Formuliere die EINE systematische Schwäche (nicht Symptom): z. B.
   „Verifier lehnt wegen X ab, aber der Builder-Prompt erwähnt X nie" ·
   „timeout der build-Phase zu knapp: 3 von 4 TIMEOUTs bei >90 % der Frist" ·
   „done_when-Schablone erzeugt untestbare Pläne". Mit Belegen (Datei/Ledger-Zeile).
4. **Fix**: minimaler Edit an GENAU EINEM Pack (Prompt-Text oder Manifest-Werte wie
   timeout/stop). Regeln:
   - Verbote-Blöcke, Schienen, Evidenz-Pflichten und last-status-Protokolle dürfen
     nur PRÄZISER/SCHÄRFER werden — jede Abschwächung ist verboten, auch „zur
     Effizienz".
   - Keine Modell-/Engine-Wechsel (das ist Operator-Sache via Dashboard-Override).
   - Platzhalter-Schreibweisen (doppelt geschweifte Klammern) unangetastet lassen.
5. **Gate** (Exit-Code zählt): `git add -A && ./loops/gate.sh` — der Pack-Lint-Test
   (tests/loops, läuft über affected) validiert alle Packs; rot = dein Edit ist formal
   kaputt.
6. **Grün** → GENAU EIN Commit: `loop(loop-tuner): <pack> <schwäche kurz>`
   + Ledger-Zeile: Schwäche, die ≥2 Belege, was geändert wurde, erwartete Wirkung
   (woran man nächste Woche misst, ob der Tune trug).
7. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `FIXED <pack>` · `DRY` (keine belegte Schwäche) · `BLOCKED <grund>`.

## Verbote
NIE: Dateien außerhalb `loops/packs/**` + `loops/README.md` ändern (runner.py/engines/
gate.sh/systemd sind tabu — dafür gibt es PlanSpecs), Verbote/Schienen abschwächen,
mehr als ein Pack pro Runde, push/merge/deploy, Vollsuite, kanban.db-Writes, Secrets,
Upstream-Dateien, `web/package-lock.json`.

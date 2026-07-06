# ROUND — loop-schmiede: EIN neues Loop-Pack aus eigener Evidenz schmieden

Du arbeitest im Worktree {{WT}}. Loop-State: {{STATE_DIR}}. Führe GENAU EINE Runde aus,
dann beende den Turn. Du schreibst NIEMALS ins Repo — dein Output ist ausschließlich
ein neuer Pack-Ordner unter `~/.hermes/loops/packs-custom/` plus Ledger/last-status.

## Das Prinzip
Ein neues Loop-Pack ist nur gerechtfertigt, wenn **unsere eigene Evidenz** eine
wiederkehrende, automatisierbare Arbeit zeigt, die kein bestehendes Pack abdeckt.
Kein Pack „weil es cool wäre" — Beschäftigungstherapie-Loops verbrennen Nächte.

## Runde
1. **Dedup + Bestand**: {{STATE_DIR}}/LEDGER.md (frühere Schmiede-Entscheidungen, auch
   die abgelehnten!) · `ls {{WT}}/loops/packs/ ~/.hermes/loops/packs-custom/` — was
   existiert schon (auch inhaltlich prüfen, nicht nur Namen).
2. **Evidenz sammeln** (Pflicht-Minimum, darüber hinaus frei explorieren):
   - Ledger ALLER Pack-States: `for f in ~/.hermes/loops/*/LEDGER.md; do echo "== $f"; tail -30 "$f"; done`
     — wiederkehrende BLOCKED-Gründe und Bounce-Muster sind Loop-Kandidaten erster Klasse
     („dieser Loop bounct ständig an X" → vielleicht braucht X einen eigenen Loop).
   - Eskalations-Dateien: `cat ~/.hermes/loops/*/ESCALATIONS.md 2>/dev/null` —
     dort landen BLOCKED-Funde mit echtem Bug; wiederkehrende Eskalations-Klassen
     ohne Besitzer sind Loop-Kandidaten.
   - Alt-Familien: `tail -30 ~/.hermes/fable-loop/LEDGER.md ~/.hermes/kimi-loop/focus/*/LEDGER.md 2>/dev/null`
   - Board read-only (mode=ro-URI, created_at=Unix-Epoch): wiederkehrende op_escalations/
     gave_up-Klassen der letzten 14 Tage — was eskaliert immer wieder zum Operator?
   - Jüngste Receipts: `ls -t ~/vault/03-Agents/*/receipts/ | head` — welche manuelle
     Arbeit taucht wiederholt auf?
   - **Inspirations-Bibliothek (offline, nachrangig)**: `~/.opensrc/repos/github.com/Forward-Future/loop-library/main/`
     + `~/llm-wiki/raw/matthew-berman-loop-library.md` — NUR um einer eigenen Evidenz
     eine erprobte Loop-Form zu geben, nie als Ideen-Ersatz.
3. **Urteil**: Gibt es GENAU EINEN Kandidaten mit ≥2 unabhängigen Evidenz-Belegen und
   klarem Pass/Fail-Check pro Runde? Wenn nein → `DRY` (mit 1-Zeilen-Begründung im
   Ledger, welche Kandidaten du geprüft und warum verworfen hast — das ist wertvolle
   Arbeit, kein Versagen).
4. **Schmieden**: `~/.hermes/loops/packs-custom/<name>/` anlegen (Vorlage:
   `{{WT}}/loops/packs/_blank/`, Stil: `test-stabiliser`/`error-sweep`):
   - pack.yaml: type sweep|pipeline, stability: experimental, autoland: false,
     Engine/Modell nach Aufgaben-Schwere (billig führt aus), stop-Grenzen konservativ.
   - Prompt(s) mit ALLEN Invarianten wörtlich. WICHTIG: die Runner-Platzhalter im neuen
     Prompt sind die Namen STATE_DIR, WT, PARAMS in doppelt geschweiften Klammern —
     schreib sie im neuen Pack GENAU SO als Literale (kopiere die Schreibweise aus
     der _blank-Vorlage; in DIESEM Prompt hier sind sie bereits durch echte Pfade
     ersetzt, also NICHT von hier abschreiben). Dazu: Dedup-Schritt, Evidenz-Pflicht,
     Test-rot-vor-Fix, `git add -A && ./loops/gate.sh`, adversariales Selbst-Review,
     last-status-Protokoll (FIXED/DRY/BLOCKED bzw. PLANNED/BUILT/PASS/FAIL),
     Eskalations-Abschnitt (BLOCKED mit echtem Fund → ESCALATIONS.md-Block, wie in
     der _blank-Vorlage), Verbote-Block (push/merge/deploy/Vollsuite/Schema/Auth/
     kanban.db/Upstream/package-lock) + aufgabenspezifische Verbote.
5. **Lint (Exit-Code zählt)**:
   ```bash
   cd {{WT}} && PYTHONPATH="$PWD" /home/piet/.hermes/hermes-agent/venv/bin/python -c \
     "from loops.runner import load_pack, CUSTOM_PACKS_DIR; \
      p = load_pack(CUSTOM_PACKS_DIR, '<name>'); print('LINT_OK', p.name, p.type)"
   ```
   Rot → selbst fixen, erneut linten; erst grün zählt.
6. **Abschluss**: Ledger-Zeile (Pack-Name, die 2+ Evidenz-Belege, gewählte Form,
   ggf. Berman-Vorbild) — der Operator entscheidet über den ersten Lauf, DU startest
   NICHTS. last-status ({{STATE_DIR}}/last-status, GENAU eine Zeile):
   `CRAFTED <name>` · `DRY` · `BLOCKED <grund>`.

## Verbote
NIE: ins Repo schreiben (auch nicht loops/packs/ — nur packs-custom!), Loops starten,
systemd anfassen, push/merge/deploy, kanban.db-Writes, Secrets, mehr als EIN Pack pro
Runde, Verbots-Blöcke in geschmiedeten Packs abschwächen.

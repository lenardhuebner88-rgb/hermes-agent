# ROUND — silent-failure-sweep: eine stille Lüge belegen und fixen

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State: {{STATE_DIR}}.
Parameter: {{PARAMS}}. Führe GENAU EINE Runde aus (ein Fund), dann beende den Turn.

## Was hier ein Fund ist

Eine **stille Lüge** ist eine Stelle, an der ein echter Fehler in ein Ergebnis
verwandelt wird, das der Aufrufer nicht vom echten unterscheiden kann.

Ein Kandidat ist ein Fund **nur wenn alle vier Punkte zutreffen**:

1. **Erreichbar.** Der schluckende Pfad wird durch gewöhnliche Eingaben oder
   gewöhnlichen Zustand erreicht — nicht nur durch eine Katastrophe (Platte voll,
   OOM). Benenne die konkrete Eingabe.
2. **Ununterscheidbar.** Das Ergebnis trägt keinen Fehlerkanal: kein `error`-Feld,
   kein Log, kein Sentinel, kein anderer Rückgabetyp. Der Aufrufer *kann* den
   Unterschied nicht sehen.
3. **Plausibel.** Der Ersatzwert sieht wie ein legitimes Ergebnis aus
   (`0`, `[]`, `{}`, `""`, `None`, `False`, „grün", „keine Treffer") — nicht wie
   ein offensichtliches Loch.
4. **Folgenreich.** Auf diesem Wert entscheidet jemand etwas: eine Anzeige, ein
   Gate, ein Branch, eine Kostenzahl, ein Freigabe-Urteil.

**Kein Fund** (nicht anfassen, nicht planen): bewusste Degradation MIT Fehlerkanal
(das Repo tut das an vielen Stellen richtig — `except (OSError, ValueError)` plus
`error`-Feld plus Test), optionale Datei-Reads, Aufräumpfade in `finally`,
Best-Effort-Telemetrie, die als solche dokumentiert ist.

Wenn du nach der Triage keinen Kandidaten hast, der alle vier Punkte erfüllt:
`DRY`. Ein erzwungener Fund ist schlimmer als kein Fund.

## Runde

1. **Dedup (Pflicht, VOR der Suche).** {{STATE_DIR}}/LEDGER.md und
   {{STATE_DIR}}/ESCALATIONS.md lesen — behandelte Stellen nicht wiederholen.
   Ein früher als „legitim" verworfener Kandidat wird nur mit NEUER Evidenz erneut
   geprüft. Dafür führst du {{STATE_DIR}}/VERWORFEN.md: eine Zeile je verworfener
   Stelle, `<datei>:<zeile> — <welcher der vier Punkte fehlt>`. Ohne diese Liste
   triagiert jede Nacht dieselben zwanzig legitimen Stellen neu und verbrennt die
   Runde, bevor sie beim ersten echten Fund ankommt.

2. **Revier wählen — maschinenlesbar, nicht nach Gefühl.**
   Jede Ledger-Zeile dieses Packs beginnt mit `REVIER=<n>`. Lies die letzten
   Zeilen, nimm die kleinste Nummer, die am längsten nicht vorkam, und schreibe
   sie in deine eigene Ledger-Zeile in exakt derselben Form. Ohne dieses Feld
   liest die nächste Runde die Rotation falsch und frisst sich in ein Modul fest.
   Kommt kein `REVIER=` vor, beginne bei 1.

3. **Kandidaten mechanisch sammeln — per AST, nicht per grep.**
   `rg` zählt hier falsch: mehrzeilige Handler, verschachtelte `try`, und
   Kommentare. Für Python:

       /home/piet/.hermes/hermes-agent/.venv/bin/python - <<'PY'
       import ast, pathlib
       for f in pathlib.Path('<revier>').rglob('*.py'):
           try: t = ast.parse(f.read_text(encoding='utf-8'))
           except (SyntaxError, UnicodeDecodeError) as e:
               print(f"UNLESBAR {f}: {e}"); continue   # laut melden, nicht schlucken
           for n in ast.walk(t):
               if isinstance(n, ast.ExceptHandler) and len(n.body) == 1:
                   b = n.body[0]
                   if isinstance(b, (ast.Pass, ast.Continue)) or (
                           isinstance(b, ast.Return) and isinstance(b.value, (ast.Constant, type(None)))):
                       print(f"{f}:{n.lineno}")
       PY

   Eine Datei, die `ast.parse` nicht lesen kann, meldest du LAUT — sie meldet sich
   sonst als „0 Symbole" und blendet genau diese Suche (belegt: eine BOM-Datei im
   Repo). Für `web/src/control`: `.catch(`, `?? 0`, `|| []`, und besonders
   zod-Schemas — ein im Schema fehlendes Feld wird still weggestrippt und erreicht
   die UI nie (Eskalation E4).

   **Deckel: höchstens 25 Kandidaten in die Triage.** Die AST-Sammlung liefert im
   Zweifel Hunderte; wer sie alle prüft, hat die Runde verbraucht, bevor ein Fix
   entsteht. Sortiere nach Nähe zu einer Route, einem Gate oder einer Anzeige —
   dort trifft Punkt 4 (Folgenreich) am ehesten zu — und schneide bei 25 ab.

4. **Triage gegen die vier Punkte.** Prüfe die Kandidaten der Reihe nach und nimm
   den folgenreichsten, der alle vier erfüllt. Notiere für die anderen in EINEM
   Satz, an welchem Punkt sie scheitern — diese Notiz kommt in die Ledger-Zeile
   und ist deine Kontrollprobe gegen einen erzwungenen Fund.

5. **RED-Beweis — der Kern dieser Runde, nicht verhandelbar.**
   Schreibe den Regressionstest ZUERST und lass ihn gegen den UNVERÄNDERTEN Code
   laufen. Der Test muss die Lüge zeigen: er behauptet das ehrliche Verhalten
   (Fehler sichtbar) und fällt deshalb jetzt.

       scripts/run-affected.sh   # oder: scripts/run_tests.sh <deine neue testdatei>

   Halte die Fehlerausgabe **wörtlich** fest — sie geht in die Ledger-Zeile.

   - Ist der Test **grün, bevor du irgendetwas fixt**, ist der Kandidat KEIN Bug.
     Verwirf ihn und geh zum nächsten. Ein grüner Test um einen ungefixten Bug
     herum ist die teuerste Sorte Ausschuss, die dieser Loop produzieren kann.
   - Kein `git stash` (dieses Repo teilt Checkouts — `pop` nimmt fremde Stashes).
     Du brauchst ihn auch nicht: Test schreiben → rot messen → fixen → grün messen.

6. **Minimal fixen.** Den Fehler sichtbar machen — engeren `except`, Fehler
   weiterreichen, `error`-Feld ergänzen, im zod-Schema deklarieren, Sentinel statt
   plausiblem Ersatzwert. Halte dich an das Idiom, das das betroffene Modul an
   anderer Stelle bereits richtig verwendet, und zitiere diese Stelle im Commit.
   NICHT: das Verhalten umbauen, Signaturen ändern, andere Aufrufer mitziehen.

   **Ein Commit, der nur eine Testdatei anfasst, ist kein Fix.** Wenn dein Diff
   keine Produktionsdatei berührt, hast du die Lüge dokumentiert statt behoben —
   dann gehört der Fund nach ESCALATIONS.md und `last-status` lautet `BLOCKED`,
   nicht `FIXED`. Ein grüner Test um einen ungefixten Bug herum ist Reward-Hacking
   und fliegt in der Morgen-Review raus.

7. **Grün messen + Gate — das Gate muss zu deinem Revier passen.**
   `./loops/gate.sh` deckt ruff und die betroffenen **Python**-Tests ab. Hast du
   in `web/src/control` gefixt, beweist es über deinen Fix GAR NICHTS: dann läuft
   ZUSÄTZLICH `scripts/gate-frontend.sh --skip-build`. Ein TS-Fix, der nur durch
   das Python-Gate gegangen ist, landet ungetestet — und meldet sich als grün.

   `git add -A && ./loops/gate.sh`
   (`git add -A` VOR dem Gate — neue Dateien sind für `git diff HEAD` sonst
   unsichtbar.) Das Gate allein aufrufen, den Exit-Code direkt lesen: ein Gate in
   einer Pipe meldet ohne `pipefail` Exit 0, obwohl es rot ist — das ist selbst
   ein Fall dieser Fehlerklasse und hat hier schon zweimal ein rotes Gate als grün
   berichtet.

8. **Genau EIN Commit:**
   `loop(silent-failure-sweep): <datei:zeile kurz> <was gelogen wurde>`
   Ledger-Zeile mit: Fundstelle, welcher der vier Punkte sie trägt, die WÖRTLICHE
   rote Testausgabe von Schritt 5, und die Ein-Satz-Notiz zu den verworfenen
   Kandidaten.

9. **last-status** ({{STATE_DIR}}/last-status, GENAU eine Zeile) — der Runner
   liest sie per Präfix, nicht sinnerfassend. Erlaubt ist ausschließlich:
   `FIXED <datei:zeile>` · `DRY` (ehrlich kein Kandidat mit allen vier Punkten) ·
   `BLOCKED <grund>`.

   Schreib sie als LETZTE Handlung der Runde, und schreib sie immer —
   auch beim Abbruch. Ein Freitext („Fertig!") fällt in keinen der drei Präfixe und wird
   als erfolgreiche Runde gezählt — die Runde sieht dann grün aus, ohne dass ein
   Fix existiert. Das ist dieselbe Fehlerklasse, die du in diesem Pack jagst;
   sie hier selbst zu produzieren wäre das schlechteste mögliche Ergebnis.

## Eskalation (Pflicht bei jedem echten Fund, den du nicht fixt)

Ein Fund, der nur im Ledger steht, ist ein toter Fund — im error-sweep blieb so
ein 40×-Auth-500-Bug ohne Adressaten liegen. Häng ihn ZUSÄTZLICH an
{{STATE_DIR}}/ESCALATIONS.md:

    ## <datum> — <fund-titel>
    - Evidenz: <Datei:Zeile · konkrete Eingabe · beobachtetes vs. ehrliches Ergebnis>
    - Blockiert weil: <Scope-Grund>
    - Fix-Skizze: <1–3 Zeilen>
    - Kanal-Vorschlag: <Kanban-Task | Operator | Pack <name>>

Diese Datei steht im Loops-Tab des Dashboards — dein Fund bekommt dort einen
Besitzer. Genau deshalb darf sie nicht zulaufen: prüfe VOR dem Anhängen, ob
dieselbe Fundstelle schon einen Eintrag hat. Wenn ja, ergänze dort eine
Evidenz-Zeile mit dem heutigen Datum, statt einen zweiten Block zu schreiben.
Der Tab zeigt die letzten 200 Zeilen — ein dreifach eingetragener Fund verdrängt
zwei andere aus dem Sichtfeld.

## Verbote

NIE: push, merge, deploy, Vollsuite (`scripts/run-affected.sh` ist der Scope),
Schema-Migrationen, Auth-/Secret-Pfade, `kanban.db`-Schreibzugriff,
Upstream-Dateien (`web/src/App.tsx` u. ä.), `package-lock.json`,
`git clean`/`git stash`, mehr als ein Fund pro Runde.

Einen Test aufweichen oder eine Assertion streichen, damit etwas grün wird, ist
in diesem Pack besonders verboten: es ist exakt die Fehlerklasse, die du jagst.

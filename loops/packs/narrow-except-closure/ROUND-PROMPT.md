# ROUND — narrow-except-closure: die Fehlerklasse sitewide schließen

Du arbeitest im Worktree {{WT}} (exklusiv für diesen Loop). Loop-State:
{{STATE_DIR}}. Parameter: {{PARAMS}}. Diese einzige Runde schließt die GESAMTE
Restmenge der Zielklasse in EINER Runde und in EINEM Commit. Sie bearbeitet
mehrere Stellen gemeinsam — ausdrücklich nicht eine Instanz pro Nacht.

## Unveränderliche Zielklasse

Ein Kandidat wird nur geändert, wenn **beide** Bedingungen gelten:

1. Der Handler ist ein enges `except OSError` (mit oder ohne `as ...`) und fängt
   `UnicodeDecodeError` noch nicht.
2. Im zugehörigen `try-Block` steht wirklich ein Lesepfad, der Text dekodiert oder
   dekodierte Daten konsumiert, etwa `Path.read_text(...)`, `open(...,
   encoding=...)` plus `read()`/Iteration oder ein gleichwertiger Datei-Read.

Die zweite Bedingung ist eine zwingende Ausschlussregel: Ein enges `except
OSError` ohne Lesepfad bleibt unverändert. Reine Schreibpfade, `mkdir`, `unlink`,
`rename`/`replace`, Metadatenoperationen und `subprocess` sind keine Lesepfade.
Auch benachbarte Handler, die `UnicodeDecodeError` bewusst weiterreichen oder
bereits separat behandeln, bleiben unverändert.

## Runde

1. **Dedup und sauberer Ausgangspunkt.** Lies `{{STATE_DIR}}/LEDGER.md` und prüfe
   `git status --short`. Fremde Änderungen nicht anfassen. Dieser Closure-Pack
   hat genau eine Runde; ein früherer vollständiger `FIXED`-Eintrag bedeutet
   `DRY already-closed`.

2. **GESAMTE Restmenge vor der ersten Änderung per rg ermitteln.** Führe dieses
   Kommando unverändert im Repo-Root aus:

    rg -n --glob '*.py' 'except[[:space:]]+OSError([[:space:]]+as[[:space:]]+[A-Za-z_][A-Za-z0-9_]*)?[[:space:]]*:' .

   Prüfe **jeden** Treffer im Kontext seines vollständigen `try`/`except`-Blocks.
   Schreibe vor jeder Codeänderung zwei vollständige Listen nach
   `{{STATE_DIR}}/LEDGER.md`: `ANWENDBAR` mit Datei und Handler-Zeile sowie
   `AUSGESCHLOSSEN` mit Datei, Zeile und dem konkreten Grund, warum im try-Block
   kein wirklicher Lesepfad steht. Es gibt keinen Kandidatendeckel und keine
   Revierrotation: „sitewide“ heißt alle Treffer dieses Kommandos.

   Sind weniger als zwei Stellen anwendbar, erstelle keinen Einzelfund-Commit.
   Schreibe `BLOCKED weniger-als-zwei-anwendbare-Stellen` in `last-status`; diese
   Runde darf AC-S2-1 nicht durch einen Einzelpatch vortäuschen.

3. **RED für alle Stellen, bevor Produktionscode geändert wird.** Jede geänderte Stelle
   braucht ihren eigenen Repro-Test beziehungsweise eindeutig benannten
   parametrisierten Testfall. Jeder Fall muss den echten Lesepfad mit ungültig
   kodierten Bytes ausführen, vor der Änderung einen `UnicodeDecodeError`
   beobachten und nachher das bereits für `OSError` definierte Verhalten
   erwarten. Führe alle neuen Tests gemeinsam gegen den UNVERÄNDERTEN Produktionscode
   aus:

       scripts/run_tests.sh <alle-betroffenen-testdateien> -q -p no:cacheprovider

   Mindestens ein roter Fall pro `ANWENDBAR`-Zeile ist Pflicht. Halte Zuordnung
   und wörtliche rote Kurz-Ausgabe im Ledger fest. Ist ein Fall schon vorher
   grün, darf die Stelle nicht geändert werden; klassifiziere sie neu statt den
   Test zu verschärfen, bis er künstlich rot wird.

4. **Die ganze Klasse minimal schließen.** Ändere alle belegten Handler nach dem
   lokalen Idiom, üblicherweise zu `except (OSError, UnicodeDecodeError)`, ohne
   Signaturen, Rückgabewerte oder das bestehende OSError-Verhalten umzubauen.
   Keine nur „ähnlichen“ Fehlerklassen und keine ausgeschlossenen Stellen
   mitziehen. Alle Produktionsänderungen und alle zugehörigen Repro-Tests kommen
   zusammen in EINEM Commit.

5. **Grün und Closure nachmessen.** Führe zuerst dieselben gezielten Tests grün
   aus. Führe nach dem Fix exakt dasselbe Suchkommando wie in Schritt 2 erneut aus:

    rg -n --glob '*.py' 'except[[:space:]]+OSError([[:space:]]+as[[:space:]]+[A-Za-z_][A-Za-z0-9_]*)?[[:space:]]*:' .

   Triagiere auch die Nachher-Ausgabe vollständig. Sie darf nur dieselben
   belegten `AUSGESCHLOSSEN`-Stellen enthalten; keine anwendbare Stelle darf nach
   dem Fix übrig sein. Neue oder anders begründete Ausschlüsse sind ein rotes
   Closure-Gate. Danach:

       git add -A
       ./loops/gate.sh

   Das Gate direkt ausführen, nicht in eine Pipe legen. Bei Rot: reparieren oder
   `BLOCKED`, niemals einen Teil der Restmenge als Erfolg committen.

6. **Genau ein Commit und Status.** Commit-Format:
   `loop(narrow-except-closure): close <N> read handlers sitewide`.
   Schreibe danach als letzte Handlung genau eine Zeile nach
   `{{STATE_DIR}}/last-status`: `FIXED <N> Stellen`, `DRY already-closed` oder
   `BLOCKED <Grund>`.

## Der Optimierer darf seinen Maßstab nicht abschwächen

Der siteweite Such- und Testvertrag ist selbst außerhalb des Optimierungsscopes.
Das heißt ausdrücklich: Maßstab nicht abschwächen, das Suchkommando nicht ändern,
keine Assertion lockern, keinen Test löschen und keinen roten Repro-Fall als
„nicht relevant“ umetikettieren, nachdem Produktionscode geändert wurde.

In dieser Runde dürfen weder
`loops/packs/narrow-except-closure/ROUND-PROMPT.md` noch
`loops/packs/narrow-except-closure/pack.yaml` noch
`tests/loops/test_narrow_except_closure_pack.py` geändert werden. Sind diese
Dateien bereits dirty oder müsste ihr Vertrag geändert werden, ist das Ergebnis
`BLOCKED contract-drift`, nicht ein leichteres Gate.

## Verbote

NIE: push, merge, deploy, Service-Restarts, Vollsuite, Schema-Migrationen,
Auth-/Secret-Pfade, `kanban.db`-Schreibzugriff, Upstream-Dateien,
`package-lock.json`, `git stash`, `git clean`, ausgeschlossene OSError-Handler
ändern, nur einen Teil der anwendbaren Restmenge committen oder Tests/Assertions
aufweichen.

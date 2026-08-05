# ROUND — dead-writer-unstick: belegten Fix landefertig vorbereiten

Du arbeitest im Worktree `{{WT}}`. Der ausschließlich dauerhafte Arbeitsbereich dieser Runde ist `{{STATE_DIR}}`. Parameter: `{{PARAMS}}`.

Ziel ist ausschließlich die bereits belegte Warnung `"dead writer recovery failed"`. Diese Runde diagnostiziert genau deren Ursache in `hermes_cli/kanban_db.py` und erzeugt zwei landefertige, aber noch nicht angewandte Artefakte:

- `{{STATE_DIR}}/dead-writer-unstick.patch`
- `{{STATE_DIR}}/test_dead_writer_unstick.py`

## Unverrückbare Grenzen

- `hermes_cli/kanban_db.py nicht verändern`, weder dauerhaft noch vorübergehend. Der Patch wird außerhalb des Worktrees konstruiert und nur mit `git apply --check` geprüft.
- Keine Produktivdatei committen, keinen Branch mergen und niemals pushen.
- Keine Dienste neu starten oder Live-Konfiguration ändern.
- Der Worktree muss am Ende gegenüber dem Rundenstart sauber sein. Insbesondere muss `git diff --exit-code -- hermes_cli/kanban_db.py` erfolgreich sein.
- Dies ist eine Vorbereitung, keine Landung. Schreibe nur in `{{STATE_DIR}}`; ein gegebenenfalls für den RED-Lauf kurz kopierter Test muss vor Rundenende wieder entfernt sein.

## Closure vor neuer Suche

Lies zuerst `{{STATE_DIR}}/LEDGER.md` vollständig.

Wenn dort ein Eintrag `DIAGNOSED` ohne einen zeitlich späteren Eintrag `PREPARED` steht, gilt diese Diagnose als offen. Dann suchst du **keine neue Ursache**, erhebst keine alternative Hypothese und wiederholst keinen breiten Journal-Sweep. Du schließt ausschließlich diese Diagnose ab: roten Repro vervollständigen, Patch vervollständigen und beide Artefakte prüfen. Erst ein späterer `PREPARED`-Eintrag würde die Diagnose schließen.

## Runde

1. **Vorher-Messung mit selbst festgelegtem Vertrag**
   - Nur wenn noch keine offene `DIAGNOSED`-Zeile existiert: Wähle die tatsächlich relevante(n) systemd-User-Unit(s) und ein abgeschlossenes Zeitfenster.
   - Führe `journalctl --user -u <Unit> --since <Start> --until <Ende> --no-pager` aus und zähle ausschließlich Zeilen mit dem exakten Suchtext `"dead writer recovery failed"`.
   - Schreibe **vor der Diagnose** genau eine nachvollziehbare Ledger-Zeile in `{{STATE_DIR}}/LEDGER.md`, die mindestens diese Felder enthält: `MEASUREMENT Unit(s)=... --since=... --until=... exakter Suchtext="dead writer recovery failed" count=...`.
   - Keine geerbte Zahl verwenden. Insbesondere sind die verworfene Zahl 3188 und fremde Messfenster kein Messvertrag für diesen Lauf.

2. **Eine Ursache belegen**
   - Wähle aus der Messung eine konkrete Warnungsinstanz und verfolge ihren Code- und Datenpfad in `hermes_cli/kanban_db.py` bis zur kleinsten belegten Ursache.
   - Prüfe bestehende Tests und direkte Aufrufer. Erfinde keine zweite Fehlerklasse.
   - Schreibe die Ursache als `DIAGNOSED fingerprint=<stabiler Schlüssel> cause=<konkret belegte Ursache>` in `{{STATE_DIR}}/LEDGER.md`.

3. **Roten Repro erzeugen**
   - Schreibe den kleinsten deterministischen pytest-Repro unter dem festen Namen `{{STATE_DIR}}/test_dead_writer_unstick.py`.
   - Der Test muss auf dem unveränderten aktuellen Stand rot sein und genau die Diagnose reproduzieren, nicht nur Text oder Implementierungsdetails prüfen.
   - Führe den Repro mit dem kanonischen Wrapper `scripts/run_tests.sh` aus. Falls pytest wegen der externen Lage keine Repo-Fixtures auflöst, kopiere ausschließlich den Test kurz unter `tests/`, führe ihn dort aus und entferne die Kopie sofort wieder.
   - Halte Kommando, Exit-Code und die relevante rote Assertion im Ledger fest: `RED_REPRO command=... exit=... assertion=...`.

4. **Patch als Datei erzeugen**
   - Schreibe einen vollständigen Unified-Git-Patch unter dem festen Namen `{{STATE_DIR}}/dead-writer-unstick.patch`. Er muss die minimale Produktivänderung und den Repro-Test am passenden endgültigen Testpfad enthalten.
   - Wende ihn nicht an. Prüfe ausschließlich `git apply --check "{{STATE_DIR}}/dead-writer-unstick.patch"` gegen den unveränderten Worktree und protokolliere das Ergebnis als `PATCH_CHECK command=... exit=...` im Ledger.

5. **Abschluss prüfen und Status setzen**
   - Beide festen Dateien müssen existieren und nicht leer sein.
   - `git diff --exit-code -- hermes_cli/kanban_db.py` muss 0 liefern; `git status --short` darf keine Rundenreste zeigen.
   - Nur wenn Messvertrag, `DIAGNOSED`, roter Repro und `git apply --check` belegt sind, schreibe als erste Zeile von `{{STATE_DIR}}/last-status`: `PREPARED dead-writer-unstick.patch test_dead_writer_unstick.py`.
   - Sonst schreibe `BLOCKED <konkreter Grund>` als erste Zeile. Niemals `PREPARED` behaupten, wenn eines der beiden Artefakte fehlt oder eine Prüfung nicht lief.

Der Runner prüft die festen Artefakte bei `PREPARED` und meldet den vorbereiteten Stand über notify. Danach endet die Runde.

# Control-Bibliothek: gespeicherte Suchen und Themen-Follows

Dieser Slice hängt an der Hermes-Control-Route `/control/bibliothek` und erweitert den Lesesaal um drei Nutzerpfade:

- `Gespeicherte Suchen`: Die Ansicht lädt `/api/library/saved-searches` und zeigt die Einträge im Smart-Shelves-Block. Der Button `Suche öffnen` übernimmt die Query in das Suchfeld der Bibliothek.
- `Thema folgen`: Die Ansicht lädt Demo-Themen über `/api/library/topics`. Der Follow-Chip schaltet per `POST /api/library/topics/{id}/follow` auf `Folge ich` und per `DELETE /api/library/topics/{id}/follow` wieder zurück.
- `Beobachtungsliste`: Gefolgte Themen bilden die beobachteten Bibliotheks-Themen; die UI benennt diesen Bereich als Beobachtungsliste, damit Menschen den Follow-Status wiederfinden.

## Demo-Daten

Der lokale State liegt profile-spezifisch unter `$HERMES_HOME/control/library_state.json` und wird bei Bedarf angelegt. Ohne vorhandene Datei seeded Hermes vier Beispielthemen:

- `KI-Modelle`
- `WM 2026 Deutschland`
- `Hermes Dashboard`
- `Langfuse/LangSmith`

Für reproduzierbare Tests setzt die bestehende Teststruktur `HERMES_HOME` auf ein temporäres Verzeichnis. Dadurch entstehen gespeicherte Suchen und Follow-Status nur im Test-Home und nicht im echten Profil.

## Reproduzierbare Checks

Backend-Routen und Persistenz:

```bash
scripts/run_tests.sh tests/hermes_cli/test_library_state.py tests/hermes_cli/test_library_routes.py tests/hermes_cli/test_library_view.py
```

Frontend-Komponenten der Bibliothek:

```bash
cd web && npm exec vitest -- run src/control/views/BibliothekView.test.tsx
```

Erwartetes Verhalten: Eine gespeicherte Suche kann erstellt und wieder gelistet werden; ein Demo-Thema wechselt sichtbar von `Thema folgen` zu `Folge ich` und nach Entfolgen zurück. Die aggregierte Bibliotheksseite gruppiert Treffer aus unterschiedlichen Quellen als Regale/Smart Shelves.

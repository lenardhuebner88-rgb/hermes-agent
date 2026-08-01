# REPORT S4 — `kanban-await`

## Geändert / neu

- Neu: `scripts/kanban-await.py`, `tests/scripts/test_kanban_await.py`, `REPORT-S4.md`.
- Vault, außerhalb des Repo-Commits: `_agents/_shared/prompts/role-codex.md` nur um den vierzeiligen Unterabschnitt „Warten ist ein Prozess, kein Modell-Turn“ ergänzt; der übrige uncommittete Modus-3-Diff ist fremder Bestand.

## Gates (Endstand)

- `python3 -m py_compile scripts/kanban-await.py` — Exit 0, keine Ausgabe.
- `scripts/run_tests.sh tests/scripts/test_kanban_await.py` — Exit 0: `=== Summary: 1 files, 6 tests passed, 0 failed/errors (100% complete) in 2.0s (6 workers) ===`.
- `scripts/run-affected.sh` — außerhalb der Dateisandbox Exit 0: `=== Summary: 34 files, 443 tests passed, 0 failed/errors (100% complete) in 36.3s (6 workers) ===`.
- Kontrolllauf in der Dateisandbox: Exit 1, `438 tests passed, 5 failed/errors`; alle fünf reproduziert fremd in `tests/scripts/test_retention_reap.py` (vier read-only `~/.hermes/retention-reap.lock`, eine `/dev`-Confinement-Probe). Derselbe unveränderte Scope außerhalb der Sandbox ergab die grüne Zeile oben.
- `ruff check .` — mit Projekt-Ruff im PATH Exit 0: `All checks passed!`; nackter Erstaufruf ohne Projekt-PATH: Exit 127, `ruff: command not found`.

## Pflichtproben

- Echtes `task_events`-Schema auf temporärer SQLite-Datei: vorbestehendes `completed` mit `--since-id 0` → Exit 0, eine JSON-Zeile mit `id:1`; ohne `--since-id` → Exit 2 nach gemessenen 0,30 s.
- Cursor-Round-Trip: Ausgabe-ID 1 als `--since-id 1` → nächstes Event `blocked` mit `id:2`, Exit 0.
- Nie terminierender Task mit vier Ziel-Kinds → Exit 2 nach gemessenen 0,40 s (`--timeout 0.35`); kein Hängen.
- Passendes Event eines anderen Tasks → Exit 2 nach gemessenen 0,30 s; SIGINT/SIGTERM enden ohne Traceback mit 130/143.

## Offen

- Kein Full-Suite-Lauf (absichtlich), kein Merge/Push/Deploy/Restart. `run-affected.sh` warnte: Branch-HEAD ist drei Commits hinter `main`. Die Vault-Änderung bleibt wie beauftragt uncommittet zur Übernahme durch die Auftraggeber-Session.

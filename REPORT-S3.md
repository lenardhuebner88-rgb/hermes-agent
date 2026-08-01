# REPORT S3 — Zeitbudget-Check

- Geändert: `hermes_cli/affected_test_budget.py`, `tests/hermes_cli/test_affected_test_budget.py`, `tests/hermes_cli/test_affected_test_mapping.py`, `REPORT-S3.md`; `hermes_cli/affected_test_mapping.py` und alle Anti-Scope-Dateien blieben unberührt.
- `scripts/run_tests.sh tests/hermes_cli/test_affected_test_budget.py` — Exit 0; `=== Summary: 1 files, 11 tests passed, 0 failed/errors (100% complete) in 1.0s (6 workers) ===`.
- `scripts/run-affected.sh` — Exit 0; `=== Summary: 3 files, 73 tests passed, 0 failed/errors (100% complete) in 40.3s (6 workers) ===`.
- `ruff check .` — im nackten Worktree zunächst Exit 127, `/bin/bash: line 1: ruff: command not found`; mit kanonischem `/home/piet/.hermes/hermes-agent/.venv/bin` im `PATH` wiederholt: Exit 0, `All checks passed!`.
- Ergebnis (1), echte Live-Struktur: Worktree-Cache nicht vorhanden; `resolved_cache=/home/piet/.hermes/hermes-agent/test_durations.json`, `estimate_seconds=13.114`, `missing_forecasts=0`, `note=''`, Exit 0.
- Kontrollprobe (2), explizit fehlender `/tmp/s3-budget-sight-no-cache.json`: Exit 17; `AffectedTestBudgetConfigError` nennt den Pfad, `no test selection will run without a runtime estimate` und den Fix `HERMES_AFFECTED_BUDGET_OK=1`.
- Override (3), derselbe fehlende Pfad: Exit 0; `override_estimate=None`; Notiz enthält `skipped via deliberate HERMES_AFFECTED_BUDGET_OK=1 override` und behält die komplette Auswahl.
- Formgleiches Fixture (4): kleines JSON-Objekt `testpfad -> Sekunden` nach dem echten 182307-Byte-Cache; kein Mock von `load_test_durations`.
- Bestandstest-Anpassung (eine der sechs genannten Aufrufstellen): `test_missing_or_corrupt_explicit_cache_fails_closed_with_fix` erwartet jetzt bewusst Abbruch samt Pfad/Ursache/Fix statt stiller Notiz; die übrigen fünf Aufrufstellen blieben unverändert.
- Zusätzliche Testanpassung: Mapping-Tests setzen in ihrer bestehenden Operational-Budget-Fixture den bewussten Override, weil ihre temporären Fremd-Repos absichtlich keinen Cache besitzen; keine Mapping-/Auswahl-Produktionslogik geändert.
- Erster affected Kontrolllauf war erwartungsgemäß rot (Exit 1, 10 reproduzierte Mapping-Fixture-Fehler) und wurde erst nach der bewussten Fixture-Anpassung grün.
- Offen/ungeprüft: volle Testsuite gemäß Auftrag nicht ausgeführt; Branch-Age meldete `HEAD ist 3 Commits hinter main — rebase empfohlen`; kein Merge, Push, Deploy oder Live-Cache-Eingriff.

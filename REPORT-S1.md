# Bericht S1 — Evidenz aus dem Repo heraus

## Geänderte Dateien

- `scripts/visual-verify.sh`
- `tests/hermes_cli/test_kanban_worktrees_commit_gates.py`
- `REPORT-S1.md`

Der Default kommt nun aus
`${HERMES_REPORTS_ROOT:-$HOME/.hermes/reports}/<task_id>/<UTC-timestamp>/`, wobei
`HERMES_KANBAN_TASK` die Task-ID liefert und `local` der stabile Ersatz für leer/ungesetzt ist.
Ein explizites `--output-dir DIR` bleibt unverändert vorrangig. Verify-Logik, Viewports, Routen,
Seeds, Playwright-Aufruf und bestehende Flags wurden nicht umgebaut.

## Automatisierter Regressionstest

Das echte `scripts/visual-verify.sh` wurde mit gesetztem `HERMES_KANBAN_TASK` und
`HERMES_REPORTS_ROOT` ausgeführt. Nur Dashboard-Prozess und Node-Runner wurden im Test ersetzt;
Verarbeitung der Argumente, Default-Pfadaufbau, Verzeichnisanlage und Runner-Weitergabe stammen aus
dem echten Skript.

Command:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_worktrees_commit_gates.py -q -p no:cacheprovider -k test_visual_verify_script_defaults_to_task_reports_root
```

Exit-Code: `0`

```text
=== Summary: 1 files, 1 tests passed, 0 failed/errors (100% complete) in 1.3s (6 workers) ===
```

Vorprüfung: Die zuerst versuchte Node-ID-Form
`tests/...py::test_visual_verify_script_defaults_to_task_reports_root` wird von diesem
Repository-Wrapper nicht unterstützt und endete mit Exit-Code `1` / `No test files to run`.
Sie wurde nicht als Testbeleg oder Gate gewertet.

## Pflicht-Gates — wörtliche Ergebnisse

### `bash -n scripts/visual-verify.sh`

Exit-Code: `0`

```text
<keine Ausgabe>
```

### `scripts/run-affected.sh`

Exit-Code: `0`

```text
▶ using test Python: /home/piet/.hermes/hermes-agent/.venv/bin/python
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 1 test files (~73 tests) under ['tests/hermes_cli/test_kanban_worktrees_commit_gates.py']; running with -j 6
[100.0% |    73/~73 | ✓76 | ✗ 0] ✓ tests/hermes_cli/test_kanban_worktrees_commit_gates.py (76✓, 14.9s)

=== Summary: 1 files, 76 tests passed, 0 failed/errors (100% complete) in 14.9s (6 workers) ===
  Durations cached to test_durations.json (1 files)

=== Per-file subprocess time distribution ===
  Files:   1
  Total subprocess CPU-wall: 14.9s  (runner wall: 14.9s, parallelism: 6x)
  P50: 14.90s  P90: 14.90s  P95: 14.90s  P99: 14.90s  Max: 14.90s
  <1s: 0 files (0%)  <2s: 0 files (0%)
  Top 10 slowest:
     14.90s  tests/hermes_cli/test_kanban_worktrees_commit_gates.py
run-affected: 1 file · predicted serial 1.28s · actual wall 15.3s · dilation 11.91x (wall/serial-pred; parallel×6) · load before 1.3 after 1.4 / 12 cores (0.11→0.12 per core)
```

### `ruff check .`

Erster wörtlicher Aufruf im nackten Worktree-Shell-Pfad — Exit-Code: `127`

```text
/bin/bash: line 1: ruff: command not found
```

Wiederholung desselben Gate-Befehls nach Aktivierung der kanonischen Projekt-`.venv` — Exit-Code:
`0`

```text
All checks passed!
```

## Echte Browser-Vorprüfung

Ohne `--output-dir` erzeugte das echte Skript nach erfolgreichem Build und Browserlauf:

```text
/home/piet/.hermes/reports/t_s1_preflight_escalated/20260801T170158Z/summary.json
```

`summary.json` meldete `ok: true` und `allPassed: true`; vorhanden sind drei PNGs, drei
ARIA-YAML-Dateien, `summary.json` und `server.log`. Ein erster Sandboxlauf war nicht aussagekräftig,
weil lokales Binden dort verboten war (`could not bind ... 127.0.0.1:0`); die wiederholte
Ausführung außerhalb dieser Netzsperre endete mit Exit-Code `0`.

## Abnahme: Default außerhalb des Repos

Vorheriger Zustand:

```text
$ git status --short
<keine Ausgabe>
```

Realer Aufruf ohne `--output-dir`, mit vorgebautem `/tmp`-Web-Dist und unveränderter Verify-Logik:

```text
$ HERMES_KANBAN_TASK=t_s1_acceptance HERMES_WEB_DIST=/tmp/s1-visual-evidence-web-dist NODE_PATH=/home/piet/.hermes/hermes-agent/node_modules scripts/visual-verify.sh --skip-build --self-test
/home/piet/.hermes/reports/t_s1_acceptance/20260801T170511Z/summary.json
```

Exit-Code: `0`; `summary.json`: `ok: true`, `allPassed: true`, Git-Head
`c123e8aee7a5ac93fc47a703d6ed39d50c4b884d`.

Danach:

```text
$ git status --short
<keine Ausgabe>
```

Ergebnis: bestanden. Die drei Screenshots, drei ARIA-YAML-Dateien, `summary.json` und `server.log`
lagen ausschließlich unter dem Task-/UTC-Zeitpfad in `~/.hermes/reports/`.

## Pflicht-Kontrollprobe: explizites Repo-Ziel

Realer Aufruf mit explizitem Repo-Ziel:

```text
$ HERMES_KANBAN_TASK=t_s1_acceptance HERMES_WEB_DIST=/tmp/s1-visual-evidence-web-dist NODE_PATH=/home/piet/.hermes/hermes-agent/node_modules scripts/visual-verify.sh --skip-build --self-test --output-dir /home/piet/.hermes/hermes-agent/.worktrees/s1-visual-evidence/visual-verify-output/probe
/home/piet/.hermes/hermes-agent/.worktrees/s1-visual-evidence/visual-verify-output/probe/summary.json
```

Exit-Code: `0`; `summary.json`: `ok: true`, `allPassed: true`.

Die vorgeschriebene Kontrollmessung schlug an:

```text
$ git status --short
?? visual-verify-output/
```

Ergebnis: bestanden und aussagekräftig. Die ausschließlich für diese Probe erzeugte Evidenz wurde
anschließend recoverbar nach `/tmp/s1-visual-verify-output-probe-20260801T1706Z` verschoben; der
Worktree war danach wieder sauber.

## Offen

Funktional offen: nichts. Nicht ausgeführt: volle Testsuite, Merge, Push, Deploy oder Übertragung
nach `main` (laut Auftrag ausdrücklich ausgeschlossen).

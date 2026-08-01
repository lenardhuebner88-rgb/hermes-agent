# REPORT S2 — Slice-Diff statt Ketten-Diff

## Geänderte Dateien

- `scripts/gate_diff_base.py`
- `scripts/worker-gate-ruff.sh`
- `scripts/worker-gate-frontend.sh`
- `scripts/worker-gate-android.sh`
- `hermes_cli/affected_test_mapping.py`
- `tests/scripts/test_gate_diff_base.py`
- `REPORT-S2.md`

## Gates (wörtlich)

- `bash -n scripts/worker-gate-ruff.sh scripts/worker-gate-frontend.sh scripts/worker-gate-android.sh scripts/run-affected.sh` → Exit 0, keine Ausgabe.
- `scripts/run-affected.sh` → Exit 0: `=== Summary: 8 files, 182 tests passed, 0 failed/errors (100% complete) in 40.5s (6 workers) ===`.
- `scripts/run_tests.sh tests/scripts/test_gate_diff_base.py -q -p no:cacheprovider` → Exit 0: `=== Summary: 1 files, 5 tests passed, 0 failed/errors (100% complete) in 1.6s (6 workers) ===`.
- `ruff check .` → Exit 127: `/bin/bash: line 1: ruff: command not found`.
- `PATH="/home/piet/.hermes/hermes-agent/.venv/bin:$PATH" ruff check .` → Exit 0: `All checks passed!`.

## Done-when

1. Historischer Frontend-Slice: 7 Dateien, 0 Python-Ziele; `run-affected.sh` Exit 0 mit `run-affected: no applicable Python production paths for this diff — skipping pytest (targeted scope; full suite is nightly only)`. Der Regressionstest belegt zusätzlich Ruff-Skip, Android-Skip und genau einen Frontend-Aufruf `--skip-build`.
2. Kontrollprobe mit leerem `HERMES_GATE_DIFF_BASE`, leerer Task-ID und fehlender DB: 14 Dateien, **68 Python-Testziele**. Ausgabe zusätzlich: `affected-test time-budget check skipped: duration cache missing ...; complete selection retained (68 files)`.
3. Mischdiff-Test: Backend-Test `tests/pkg/test_backend.py`, Ruff-Aufruf für `pkg/backend.py` und Frontend-Aufruf `--skip-build` liefen alle; Testdatei insgesamt 5/5 grün.
4. SQLite-Echttest: `task_runs(id, task_id, started_at, pre_run_commit_sha)` mit 8911/8912/8913 einschließlich `NULL`; erster nichtleerer `MIN(id)` gewinnt. Read-only Live-Kontrolllauf für `t_626713b9` gab Exit 0 und `21bd9a70a64b86550a7166f19fd31caa47d192ba` aus.

## Offen

- Volle Testsuite bewusst nicht ausgeführt. Der nackten Worktree-Shell fehlt `ruff` im `PATH`; das kanonische Hauptrepo-Venv ist grün.

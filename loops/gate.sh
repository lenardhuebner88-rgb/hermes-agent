#!/usr/bin/env bash
# loops/gate.sh [<ref>] — targeted Python-Gate für Loop-Packs. Aus dem Pack-Worktree aufrufen.
#
# Ohne <ref>:  prüft den UNCOMMITTETEN Diff vs HEAD   (Builder: vorher `git add -A`,
#              sonst sind NEUE Dateien für git diff unsichtbar!)
# Mit <ref>:   prüft den Diff seit <ref>, z.B. HEAD~1  (Verifier, nach dem Commit)
#
# ruff auf geänderte .py + pytest NUR auf betroffene Testdateien, delegiert an
# scripts/run-affected.sh. Pack-Worktrees haben kein eigenes venv → Live-venv
# (hermes-agent/venv, OHNE Punkt) mit PYTHONPATH=Worktree, damit der WORKTREE-Code
# getestet wird. NIE Vollsuite. Frontend-Gates (web/ berührt) laufen separat laut
# Pack-Prompts. Kein `grep -q` an pipefail-Pipes (SIGPIPE-Race).
#
# Per-File-Isolation (2026-07-16): scripts/run-affected.sh (→ run_tests.sh →
# run_tests_parallel.py) läuft jede betroffene Testdatei in einem frischen
# Interpreter statt alle zusammen in einem pytest-Prozess — das ist die einzige
# Isolationsgrenze gegen Cross-File-Modul-State-Leaks (die dokumentierte
# Original-Flake-Quelle, siehe Docstring in run_tests_parallel.py). Ein roter
# Lauf wird dort einmal automatisch reproduziert, bevor er zählt (rerun-once);
# erst ein reproduziertes Rot ist hier ein echter GATE_FAIL.
set -uo pipefail

WT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "GATE: nicht in einem Worktree"; exit 2; }
REF="${1:-HEAD}"
LIVE_VENV="/home/piet/.hermes/hermes-agent/venv"
PY="${GATE_PY:-$LIVE_VENV/bin/python}"
RUFF="${GATE_RUFF:-$LIVE_VENV/bin/ruff}"
[ -x "$PY" ]   || { echo "GATE: live venv python fehlt: $PY"; exit 2; }
[ -x "$RUFF" ] || { echo "GATE: live venv ruff fehlt: $RUFF"; exit 2; }

export PYTHONPATH="$WT"
cd "$WT"

CHANGED=$(git diff --name-only --diff-filter=ACMR "$REF" -- '*.py' | sort -u)
if [ -n "$CHANGED" ]; then
  echo "== ruff: $(echo "$CHANGED" | tr '\n' ' ') =="
  # shellcheck disable=SC2086
  "$RUFF" check $CHANGED || { echo "GATE_FAIL: ruff"; exit 11; }
else
  echo "== ruff: keine geänderten .py =="
fi

echo "== pytest (affected, per-file isoliert via run-affected.sh) =="
"$WT/scripts/run-affected.sh" "$REF" || { echo "GATE_FAIL: pytest"; exit 12; }

echo "GATE_PASS"

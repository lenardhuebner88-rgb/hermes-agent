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

# Lastdeckel NUR für Loop-Gates. Seit der Repo-Lock verengt ist, kann mehr als
# ein Pack gleichzeitig bauen, und daneben laufen Kanban-Worker auf derselben
# Maschine (12 Kerne). `scripts/run_tests.sh` setzt sonst 8 Worker — zwei
# parallele Loop-Gates plus Kanban überzeichnen damit die Maschine, und die
# Kanban-Runs, die ein Mensch abwartet, werden langsamer.
#
# Kanban geht NICHT durch dieses Skript (es ruft `scripts/run_tests.sh` bzw.
# `run-affected.sh` direkt) und behält seine 8 — gedrosselt wird ausschliesslich
# die Hintergrundarbeit der Nachtläufe.
#
# `hermes_cli/affected_test_budget.py` liest dieselbe Variable für seine
# Laufzeit-Vorhersage, sieht also denselben Wert und rechnet konsistent.
#
# Bewusst AUTORITATIV statt `${HERMES_TEST_WORKERS:-4}`: das systemd-User-
# Environment trägt bereits `HERMES_TEST_WORKERS=8`, und jede `hermes-loop@`-
# Unit erbt das. Ein Fallback-Default hätte in Produktion nie gegriffen und in
# einer sauberen Testumgebung trotzdem grün ausgesehen. Der Operator hebt den
# Deckel deshalb über eine EIGENE Variable an, die niemand sonst setzt.
export HERMES_TEST_WORKERS="${HERMES_LOOP_TEST_WORKERS:-4}"

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
"$WT/scripts/run-affected.sh" "$REF"
affected_rc=$?
if [[ "$affected_rc" -eq 3 ]]; then
  echo "GATE_NOT_RUN: pytest"
  exit 3
elif [[ "$affected_rc" -eq 4 ]]; then
  echo "GATE_UNMAPPED: pytest"
  exit 4
elif [[ "$affected_rc" -ne 0 ]]; then
  echo "GATE_FAIL: pytest"
  exit 12
fi

echo "GATE_PASS"

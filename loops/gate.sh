#!/usr/bin/env bash
# loops/gate.sh [<ref>] — targeted Python-Gate für Loop-Packs. Aus dem Pack-Worktree aufrufen.
#
# Ohne <ref>:  prüft den UNCOMMITTETEN Diff vs HEAD   (Builder: vorher `git add -A`,
#              sonst sind NEUE Dateien für git diff unsichtbar!)
# Mit <ref>:   prüft den Diff seit <ref>, z.B. HEAD~1  (Verifier, nach dem Commit)
#
# ruff auf geänderte .py + pytest NUR auf betroffene Testdateien
# (scripts/affected-tests.sh). Pack-Worktrees haben kein eigenes venv → Live-venv
# (hermes-agent/venv, OHNE Punkt) mit PYTHONPATH=Worktree, damit der WORKTREE-Code
# getestet wird. NIE Vollsuite. Frontend-Gates (web/ berührt) laufen separat laut
# Pack-Prompts. Kein `grep -q` an pipefail-Pipes (SIGPIPE-Race).
set -uo pipefail

WT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "GATE: nicht in einem Worktree"; exit 2; }
REF="${1:-HEAD}"
LIVE_VENV="/home/piet/.hermes/hermes-agent/venv"
PY="$LIVE_VENV/bin/python"
RUFF="$LIVE_VENV/bin/ruff"
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

TESTS=$("$WT/scripts/affected-tests.sh" "$REF" 2>/dev/null || true)
if [ -z "${TESTS// /}" ]; then
  echo "== pytest: keine betroffenen Testdateien für diesen Diff (targeted scope) =="
else
  echo "== pytest (affected): $(echo "$TESTS" | tr '\n' ' ') =="
  # shellcheck disable=SC2086
  "$PY" -m pytest -q -p no:cacheprovider --timeout=120 $TESTS \
    || { echo "GATE_FAIL: pytest"; exit 12; }
fi

echo "GATE_PASS"

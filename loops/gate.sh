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
#
# Baseline-Bewusstsein (2026-07-16): ein roter affected-Lauf ist nicht automatisch
# ein echter Bruch — scope-fremde Testdateien (nicht Teil des Diffs) können an
# Reihenfolge-abhängigem Fremdzustand aus einer DRITTEN, ebenfalls scope-fremden
# Datei scheitern, obwohl sie isoliert (bzw. kombiniert nur mit den Diff-eigenen
# Tests) grün laufen. Das Gate reproduziert jeden scope-fremden Fail in einem
# frischen Prozess; bleibt er reproduzierbar, ist es FAIL, verschwindet er, wird
# er als GATE_WARN gewaived statt das gesamte Gate rot zu ziehen. Eigene Fails
# (Testdatei selbst im Diff) und Massenbrüche (>5 scope-fremde Dateien) bleiben
# sofort FAIL — siehe Ablauf unten im pytest-Block.
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

TESTS=$("$WT/scripts/affected-tests.sh" "$REF" 2>/dev/null || true)
if [ -z "${TESTS// /}" ]; then
  echo "== pytest: keine betroffenen Testdateien für diesen Diff (targeted scope) =="
else
  echo "== pytest (affected): $(echo "$TESTS" | tr '\n' ' ') =="
  GATE_TMP="$(mktemp -d)"
  trap 'rm -rf "$GATE_TMP"' EXIT
  # shellcheck disable=SC2086
  "$PY" -m pytest -q -p no:cacheprovider --timeout=120 $TESTS 2>&1 | tee "$GATE_TMP/run.log"
  PYTEST_RC="${PIPESTATUS[0]}"
  if [ "$PYTEST_RC" -ne 0 ]; then
    # Baseline-bewusste Auswertung statt sofortigem FAIL — siehe Kopfkommentar.
    FAILED_FILES=$(sed -nE 's/^FAILED ([^ ]+).*/\1/p' "$GATE_TMP/run.log" | sed -E 's/::.*//' | sort -u)
    if [ -z "$FAILED_FILES" ]; then
      # Kein parsbares FAILED (Collection-Crash o.ä.) -> kein Waiver möglich.
      echo "GATE_FAIL: pytest"
      exit 12
    fi

    IN_DIFF_FAILS=""
    FOREIGN_FAILS=""
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      if grep -Fxq -- "$f" <<<"$CHANGED"; then
        IN_DIFF_FAILS="${IN_DIFF_FAILS}${f}"$'\n'
      else
        FOREIGN_FAILS="${FOREIGN_FAILS}${f}"$'\n'
      fi
    done <<<"$FAILED_FILES"

    if [ -n "$IN_DIFF_FAILS" ]; then
      echo "GATE_FAIL: pytest (eigene Testdatei rot: $(echo "$IN_DIFF_FAILS" | xargs))"
      exit 12
    fi

    FOREIGN_COUNT=$(grep -c '[^[:space:]]' <<<"$FOREIGN_FAILS")
    if [ "$FOREIGN_COUNT" -gt 5 ]; then
      echo "GATE_FAIL: pytest (zu viele scope-fremde rote Dateien: $FOREIGN_COUNT)"
      exit 12
    fi

    DIFF_TEST_FILES=$(grep -E '(^|/)test_[^/]+\.py$' <<<"$CHANGED" || true)

    while IFS= read -r FOREIGN_FILE; do
      [ -z "$FOREIGN_FILE" ] && continue
      RERUN="$FOREIGN_FILE"
      if [ -n "$DIFF_TEST_FILES" ]; then
        RERUN="$RERUN $(echo "$DIFF_TEST_FILES" | xargs)"
      fi
      echo "== pytest (isolierter Nachlauf, scope-fremd): $RERUN =="
      # shellcheck disable=SC2086
      if ! timeout 180 "$PY" -m pytest -q -p no:cacheprovider --timeout=120 $RERUN; then
        echo "GATE_FAIL: pytest (scope-fremder Fail reproduziert isoliert: $FOREIGN_FILE)"
        exit 12
      fi
      N=$(grep -c "^FAILED $FOREIGN_FILE::" "$GATE_TMP/run.log")
      echo "GATE_WARN: scope-fremder Order-Leak gewaived: $FOREIGN_FILE ($N Fails, isoliert grün)"
    done <<<"$FOREIGN_FAILS"
  fi
fi

echo "GATE_PASS"

#!/usr/bin/env bash
# Print the pytest files affected by the current diff — the *targeted* test
# scope (full suite runs only nightly; see AGENTS.md -> Testing). Pure stdlib,
# so it works even in a worktree without the venv.
#
# This only PRINTS the affected files. To RUN them, use scripts/run-affected.sh —
# it skips pytest when nothing is affected instead of letting an empty `$(...)`
# collapse into a bare run_tests.sh = the full suite. Do NOT splice the raw
# `run_tests.sh $(scripts/affected-tests.sh)` yourself.
#   scripts/run-affected.sh         # vs merge-base with main
#   scripts/run-affected.sh HEAD~1  # since a ref
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
FALLBACK_MAX_TEST_FILES=200

RAW="$(python3 "$SCRIPT_DIR/affected_tests.py" "$@")"
TOKENS=()
read -r -a TOKENS <<< "$RAW" || true
SELECTED=()
shopt -s globstar nullglob
for token in "${TOKENS[@]}"; do
  if [[ "$token" == */ ]]; then
    test_files=("$REPO_ROOT/$token"**/test_*.py)
    if (( ${#test_files[@]} > FALLBACK_MAX_TEST_FILES )); then
      printf 'affected-tests: omitted package fallback %s (%d test files; limit %d); directly mapped/importing tests remain selected; nightly full suite remains authoritative\n' \
        "$token" "${#test_files[@]}" "$FALLBACK_MAX_TEST_FILES" >&2
      continue
    fi
  fi
  SELECTED+=("$token")
done

printf '%s\n' "${SELECTED[*]}"
